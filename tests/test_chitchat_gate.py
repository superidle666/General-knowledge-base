"""查询侧闲聊意图闸（v2：L1 规则模板 + L2 门禁式 LLM 兜底）离线守卫。

分两层：
1. 纯函数：contains_entity_signal / detect_chitchat(l1_only) / should_ask_llm /
   _parse_intent_verdict 直接断言，重点验证"实体护栏 + 默认放行检索"——产品问题
   绝不因带'你/能/温度'被误判为闲聊。
2. 节点集成：驱动 NodePrepareQuery，验证 L1 命中不走 L2、L2 命中预置 answer + intent
   并跳过实体对齐、LLM 异常一律放行；并静态守卫 intent 通道已在 QueryGraphState 声明。
3. 本库词表探测（P1-3）：点名了库内实体的短问题直接进检索，不再为“确认不是闲聊”
   付一次小模型调用；探测只作用于 L2，不放宽 L1 规则面。

离线约束：本文件顶部的 autouse fixture 把 classify_chitchat_llm 在 gate 与节点两个
模块命名空间都 stub 成报错实现，任何用例误触真实 LLM 调用都会直接失败而不是静默联网；
需要验证 L2 行为时用 monkeypatch 显式覆盖成假返回。
"""

from __future__ import annotations

import pytest
from graph_introspection import PROJECT_ROOT, typed_dict_keys

import processor.query_processor.chitchat_gate as chitchat_gate_module
import processor.query_processor.nodes.node_prepare_query as node_module
from processor.query_processor.chitchat_gate import (
    CHITCHAT_REPLIES,
    _parse_intent_verdict,
    chitchat_reply,
    contains_entity_signal,
    detect_chitchat,
    should_ask_llm,
)
from processor.query_processor.nodes.node_prepare_query import NodePrepareQuery

QUERY_STATE = PROJECT_ROOT / "processor" / "query_processor" / "state.py"


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch):
    """全局守卫：离线用例里任何走到真实 LLM 的调用都立即报错，绝不允许静默联网。"""

    def _boom(text: str):
        raise AssertionError(f"离线用例不应触发真实意图分类 LLM 调用：{text!r}")

    monkeypatch.setattr(chitchat_gate_module, "classify_chitchat_llm", _boom)
    monkeypatch.setattr(node_module, "classify_chitchat_llm", _boom)


def _state(query: str, mode: str = "required") -> dict:
    return {
        "original_query": query,
        "library_id": 7,
        "entity_recognition_mode": mode,
        "entity_vocabulary": [{"name": "HAK-180", "aliases": []}],
    }


# --------------------------------------------------------------------------
# 一、实体护栏：含产品/型号信号一律不判闲聊
# --------------------------------------------------------------------------

def test_entity_signal_blocks_product_queries() -> None:
    assert contains_entity_signal("HAK-180 怎么开机") is True  # 型号代码
    assert contains_entity_signal("G740 报错") is True  # 字母+数字型号串
    assert contains_entity_signal("一加手机参数") is True  # 品牌
    assert contains_entity_signal("iPhone 16 价格") is True
    assert contains_entity_signal("烫金机怎么换膜") is True
    assert contains_entity_signal("你好") is False
    assert contains_entity_signal("你是什么模型") is False


# --------------------------------------------------------------------------
# 二、闲聊子类型命中
# --------------------------------------------------------------------------

def test_greeting_identity_capability_are_chitchat() -> None:
    assert detect_chitchat("你好", l1_only=True) == "greeting"
    assert detect_chitchat("在吗", l1_only=True) == "greeting"
    assert detect_chitchat("快速自我介绍一下你自己", l1_only=True) == "self_intro"
    assert detect_chitchat("你是谁", l1_only=True) == "identity"
    assert detect_chitchat("谢谢", l1_only=True) == "thanks"
    assert detect_chitchat("你能做什么", l1_only=True) == "capability"


def test_model_probe_takes_priority_over_identity() -> None:
    # "你是什么模型"要落到 model_probe（按策略不透露底层模型），而不是被 identity 抢先
    assert detect_chitchat("你是什么模型", l1_only=True) == "model_probe"
    assert detect_chitchat("你是GPT几", l1_only=True) == "model_probe"


def test_chitchat_reply_returns_matching_static_template() -> None:
    assert chitchat_reply("你好") in CHITCHAT_REPLIES["greeting"]  # 每类 3 条变体随机取，断言 membership 而非定值
    probe_reply = chitchat_reply("你是什么模型")
    assert probe_reply in CHITCHAT_REPLIES["model_probe"]
    # 不泄露真实厂商/模型名是硬约束；措辞（"不便透露"/"保密"）允许随变体变化。
    assert not any(name in probe_reply.lower() for name in ("gpt", "claude", "qwen", "千问", "通义", "deepseek", "文心"))
    assert chitchat_reply("HAK-180 怎么用") is None


def test_replies_have_three_variants_and_random_pick() -> None:
    # 结构约定：每个子类型 3 条候选，随机抽取降低"多次询问回复一模一样"的机器感。
    assert len(CHITCHAT_REPLIES) == 10
    for subtype, candidates in CHITCHAT_REPLIES.items():
        assert len(candidates) == 3, f"{subtype} 候选数不是 3"
        assert len(set(candidates)) == 3, f"{subtype} 候选内有重复文案"
        assert all(c.strip() for c in candidates), f"{subtype} 存在空话术"
    drawn = {chitchat_gate_module.reply_for("greeting") for _ in range(200)}
    assert drawn == set(CHITCHAT_REPLIES["greeting"]), "200 次抽取应能覆盖全部候选（随机而非固定首条）"
    assert chitchat_gate_module.reply_for(None) == ""
    assert chitchat_gate_module.reply_for("alien_type") == ""


def test_ultra_short_exact_match_catches_bare_you_are() -> None:
    # 回归现场：用户单独发"你是"，子串白名单接不住，漏进实体抽取+检索+生成烧了两次大模型。
    assert detect_chitchat("你是", l1_only=True) == "identity"
    assert detect_chitchat("你是？", l1_only=True) == "identity"  # 去标点后仍命中全等表
    assert detect_chitchat("您是", l1_only=True) == "identity"
    # 全等通道不能外溢：同前缀但有实际诉求的问法必须继续走检索。
    assert detect_chitchat("你是技术支持吗", l1_only=True) is None
    # "你是哪位"含"哪位"身份疑问词，被 identity 模板接住是预期行为。
    assert detect_chitchat("你是哪位", l1_only=True) == "identity"


# --------------------------------------------------------------------------
# 二b、L1 正则模板：中文插入/语气词变体与新类目（线上漏网原句进回归）
# --------------------------------------------------------------------------

def test_regex_templates_catch_insertion_variants() -> None:
    # 线上回归原句："哪一家的模型" vs 登记表里的"哪家的模型"，子串差一个字→旧版漏网进检索
    assert detect_chitchat("你是哪一家的模型", l1_only=True) == "model_probe"
    assert detect_chitchat("你们是哪家公司开发的", l1_only=True) == "model_probe"
    assert detect_chitchat("底层跑的是什么", l1_only=True) == "model_probe"
    assert detect_chitchat("你是谁呀", l1_only=True) == "identity"
    assert detect_chitchat("你都能干啥", l1_only=True) == "capability"
    # 存在/情绪类目只收 ≤12 字整句（字面宽松，收紧防误伤）
    assert detect_chitchat("你在哪", l1_only=True) == "presence"
    assert detect_chitchat("你吃什么", l1_only=True) is None  # 不在模板内：不硬猜，交给检索/L2
    assert detect_chitchat("今天心情不错", l1_only=True) == "emotion"
    assert detect_chitchat("这个功能让人心情不错吗为什么", l1_only=True) is None  # 超长不拦


def test_capability_regex_does_not_swallow_knowledge_phrases() -> None:
    # 旧版裸子串"会什么/能做什么/在不在"会咬进普通知识问法，收窄为"你"锚定模板后的回归：
    assert detect_chitchat("设置里会不会什么字样提示", l1_only=True) is None
    assert detect_chitchat("保修卡还在不在身边无所谓吗", l1_only=True) is None
    assert detect_chitchat("哪里能买到配件", l1_only=True) is None


# --------------------------------------------------------------------------
# 二c、L2 门禁与返回解析（纯函数，不触发调用）
# --------------------------------------------------------------------------

def test_llm_gate_admits_only_short_entity_free_text() -> None:
    assert should_ask_llm("今天心情不错") is True
    assert should_ask_llm("温度调多少合适") is True  # "温度"不在护栏词表：短且无型号信号，交给 L2 判断
    assert should_ask_llm("HAK-180在哪里") is False  # 型号信号：护栏拦截，不浪费分类调用
    assert should_ask_llm("手机屏幕碎了怎么办") is False  # 品牌词：交给检索
    assert should_ask_llm("帮我看看这个设备最近总是自动重启是什么原因") is False  # 超长
    assert should_ask_llm("") is False


def test_parse_intent_verdict_tolerates_model_output() -> None:
    assert _parse_intent_verdict('{"is_chitchat": false}') is None
    assert _parse_intent_verdict("not json at all") is None
    assert _parse_intent_verdict("") is None
    assert _parse_intent_verdict([{"is_chitchat": False, "reason": "业务问题"}]) is None
    parsed = _parse_intent_verdict('{"is_chitchat": true, "subtype": "Greeting", "reply": "嗨～"}')
    assert parsed == ("greeting", "嗨～")  # 大小写归一
    assert _parse_intent_verdict('{"is_chitchat": true, "subtype": "alien_type"}') == ("other", "")
    assert _parse_intent_verdict('{"is_chitchat": true}') == ("other", "")


# --------------------------------------------------------------------------
# 三、默认放行：产品问题/追问/超长句/泛问题一律走检索
# --------------------------------------------------------------------------

def test_product_followup_and_long_query_are_not_chitchat() -> None:
    # 本组验"规则面不拦"（l1_only）："继续"这类字面 ambiguous 的追问不再硬编码，
    # 交给 L2 结合语义判断，规则层不能误吞。
    assert detect_chitchat("你能介绍下HAK-180吗", l1_only=True) is None  # 带'你能'但有实体
    assert detect_chitchat("介绍一下华为平板", l1_only=True) is None
    assert detect_chitchat("继续", l1_only=True) is None
    assert detect_chitchat("今天天气", l1_only=True) is None  # 不在白名单
    assert detect_chitchat("", l1_only=True) is None
    long_q = "帮我总结一下这份用户手册第三章的常见故障与对应处理方法以及注意事项"
    assert detect_chitchat(long_q, l1_only=True) is None  # 超短句上限，交给检索


# --------------------------------------------------------------------------
# 四、节点集成
# --------------------------------------------------------------------------

def test_node_presets_answer_and_skips_alignment_for_chitchat() -> None:
    node = NodePrepareQuery()
    called = {"align": False}
    node._align_entity = lambda state: called.__setitem__("align", True)
    state = node.process(_state("快速自我介绍一下你自己"))
    assert state["intent"] == "chitchat"
    assert state["answer"] in CHITCHAT_REPLIES["self_intro"]
    assert state["item_names"] == []
    assert called["align"] is False  # 闲聊跳过了 required 库的实体对齐（省一次抽取 LLM）


def test_node_chitchat_applies_to_disabled_library_too() -> None:
    node = NodePrepareQuery()
    state = node.process(_state("你好", mode="disabled"))
    assert state["intent"] == "chitchat"
    assert state["answer"] in CHITCHAT_REPLIES["greeting"]


def test_node_gates_bare_ni_shi_without_llm_in_required_lib() -> None:
    # required 库单独发"你是"：闲聊闸必须在实体对齐之前拦住，否则白白多一次抽取 LLM 调用
    node = NodePrepareQuery()
    called = {"align": False}
    node._align_entity = lambda state: called.__setitem__("align", True)
    state = node.process(_state("你是"))
    assert state["intent"] == "chitchat"
    assert state["answer"] in CHITCHAT_REPLIES["identity"]
    assert called["align"] is False


def test_node_l1_hit_never_reaches_llm_gate() -> None:
    # "你是哪一家的模型"（线上漏网原句）现在由 L1 正则模板接住：
    # autouse 守卫下任何 L2 调用都会报错，能平静走完即证明未触发分类调用。
    node = NodePrepareQuery()
    called = {"align": False}
    node._align_entity = lambda state: called.__setitem__("align", True)
    state = node.process(_state("你是哪一家的模型"))
    assert state["intent"] == "chitchat"
    assert state["answer"] in CHITCHAT_REPLIES["model_probe"]
    assert called["align"] is False


def test_node_l2_fallback_hit_in_required_lib_skips_alignment(monkeypatch) -> None:
    # 规则没接住的短而无实体输入（"今天心情怎么样"带疑问标记，L1 情绪类刻意不吞）：
    # required 库走 L2，命中后不进实体对齐。
    node = NodePrepareQuery()
    calls: dict[str, object] = {"align": False, "llm_text": None}
    node._align_entity = lambda state: calls.__setitem__("align", True)

    def _fake_llm(text: str):
        calls["llm_text"] = text
        return "emotion", "听起来心情不错！有需要查的产品问题随时告诉我。"

    monkeypatch.setattr(node_module, "classify_chitchat_llm", _fake_llm)
    state = node.process(_state("今天心情怎么样"))
    assert state["intent"] == "chitchat"
    assert state["answer"] in CHITCHAT_REPLIES["emotion"]  # 已知子类型用静态表（候选内随机），口径统一
    assert calls["llm_text"] == "今天心情怎么样"
    assert calls["align"] is False


def test_node_l2_other_subtype_uses_generated_reply_with_static_fallback(monkeypatch) -> None:
    # other 子类型：优先用模型顺带生成的回复；生成缺失时退 other 静态话术，绝不预置空 answer。
    text = "帮我测测这个新功能呀"
    node = NodePrepareQuery()
    monkeypatch.setattr(
        node_module, "classify_chitchat_llm",
        lambda _text: ("other", "我在呢～有产品资料要查随时说。"),
    )
    state = node.process(_state(text))
    assert state["answer"] == "我在呢～有产品资料要查随时说。"

    node2 = NodePrepareQuery()
    monkeypatch.setattr(node2, "_align_entity", lambda state: None)
    monkeypatch.setattr(node_module, "classify_chitchat_llm", lambda _text: ("other", ""))
    state2 = node2.process(_state(text))
    assert state2["answer"] in CHITCHAT_REPLIES["other"]


def test_node_l2_says_not_chitchat_then_aligns_normally(monkeypatch) -> None:
    # L2 判非闲聊（返回 None）：不预置 answer，流程回到实体对齐，宁查勿否。
    node = NodePrepareQuery()
    called = {"align": False}
    node._align_entity = lambda state: called.__setitem__("align", True)
    monkeypatch.setattr(node_module, "classify_chitchat_llm", lambda text: (None, ""))
    state = node.process(_state("温度调多少合适"))
    assert state.get("intent") is None
    assert not state.get("answer")
    assert called["align"] is True


def test_node_disabled_lib_gate_is_stricter_than_required(monkeypatch) -> None:
    # 8~12 字的无实体短句：required 库走 L2（可省抽取调用），disabled 库无调用可省、不付费。
    text = "帮我测测这个新功能呀"
    node = NodePrepareQuery()
    reached: dict[str, bool] = {"llm": False}

    def _spy(text_: str):
        reached["llm"] = True
        return None, ""

    monkeypatch.setattr(node_module, "classify_chitchat_llm", _spy)
    node.process(_state(text, mode="disabled"))
    assert reached["llm"] is False
    node.process(_state(text, mode="required"))
    assert reached["llm"] is True


def test_node_llm_exception_falls_through_to_retrieval(monkeypatch) -> None:
    # 分类调用报意外异常（真实实现内部已降级，这里测节点调用点的兜底）：放行检索。
    node = NodePrepareQuery()
    called = {"align": False}
    node._align_entity = lambda state: called.__setitem__("align", True)

    def _raise(text: str):
        raise RuntimeError("模拟上游挂起")

    monkeypatch.setattr(node_module, "classify_chitchat_llm", _raise)
    state = node.process(_state("今天心情怎么样"))
    assert state.get("intent") is None
    assert not state.get("answer")
    assert called["align"] is True


def test_node_leaves_product_query_to_retrieval() -> None:
    node = NodePrepareQuery()
    node._call_entity_llm = lambda text, history_text: []  # 离线：只用正则，不联网
    state = node.process(_state("HAK-180 报错 E12 怎么处理"))
    assert state.get("intent") is None  # 未拦截
    assert not state.get("answer")  # 未预置答案，继续检索与生成
    assert state["item_names"] == ["HAK-180"]  # 正常走实体对齐命中


# --------------------------------------------------------------------------
# 四b、本库词表探测（P1-3）：点名了库内实体的短问题不再走 L2 分类
# --------------------------------------------------------------------------

def _vocab_state(query: str, mode: str = "required", vocabulary: object = ...) -> dict:
    state = _state(query, mode)
    state["entity_vocabulary"] = (
        [{"name": "星辰一号", "aliases": ["XT-1"]}]
        if vocabulary is ...
        else vocabulary
    )
    return state


def test_vocab_probe_skips_l2_for_own_product_name(monkeypatch) -> None:
    # “星辰一号怎么办”：通用品牌词护栏认不出自有产品名，旧行为是为它多付一次
    # 小模型分类调用（最坏 4s）；现在词表探测接住，直接进检索链路。
    node = NodePrepareQuery()
    calls: dict[str, object] = {"llm": False, "align": False}

    def _spy(_text: str):
        calls["llm"] = True
        return None, ""

    monkeypatch.setattr(node_module, "classify_chitchat_llm", _spy)
    node._align_entity = lambda state: calls.__setitem__("align", True)
    state = node.process(_vocab_state("星辰一号怎么办"))
    assert calls["llm"] is False, "命中本库词表就不该再问 L2"
    assert calls["align"] is True
    assert state.get("intent") is None
    assert not state.get("answer")


def test_vocab_probe_still_calls_l2_for_unknown_entity(monkeypatch) -> None:
    # 词表里没有的短输入（“今天心情怎么样”）保持原样走 L2，探测不能把门禁关掉。
    node = NodePrepareQuery()
    reached = {"llm": False}

    def _spy(_text: str):
        reached["llm"] = True
        return None, ""

    monkeypatch.setattr(node_module, "classify_chitchat_llm", _spy)
    node._align_entity = lambda state: None
    node.process(_vocab_state("今天心情怎么样"))
    assert reached["llm"] is True


def test_vocab_probe_matches_names_aliases_and_spacing() -> None:
    probe = NodePrepareQuery._query_names_known_entity
    state = _vocab_state("")
    assert probe(state, "星辰一号怎么办") is True  # 规范名
    assert probe(state, "XT-1 报错") is True  # 别名
    assert probe(state, "xt1报错") is True  # 同一口径去标点/空格：不写连字符也对得上
    assert probe(state, "星辰 一号 怎么办") is True  # 中间夹空格不能造成漏判
    assert probe(state, "XT-2 报错") is False  # 表里没有的型号
    assert probe(state, "") is False


def test_vocab_probe_ignores_fragment_keys() -> None:
    # 过短的键（如 "X1"）不参与探测，否则会把不相关的短句误判成业务问题。
    probe = NodePrepareQuery._query_names_known_entity
    state = _vocab_state("", vocabulary=[{"name": "X1", "aliases": []}])
    assert probe(state, "x1 是谁") is False


def test_vocab_probe_is_inert_without_a_vocabulary() -> None:
    # disabled 库接口侧不载入词表：探测自然不生效，不改变原有的收紧门禁。
    probe = NodePrepareQuery._query_names_known_entity
    assert probe(_vocab_state("", vocabulary=[]), "星辰一号怎么办") is False
    assert probe(_vocab_state("", vocabulary=[{"name": "星辰一号", "aliases": []}]), "星辰一号怎么办") is True
    assert probe({"entity_vocabulary": "星辰一号"}, "星辰一号怎么办") is False  # 非法形状不报错
    assert probe({}, "星辰一号怎么办") is False


def test_vocab_probe_does_not_loosen_l1() -> None:
    # 探测只作用于 L2；L1 规则面不变：“星辰一号 你是谁”仍按寒暄接住。
    node = NodePrepareQuery()
    node._align_entity = lambda state: None
    state = node.process(_vocab_state("星辰一号 你是谁"))
    assert state["intent"] == "chitchat"
    assert state["answer"] in CHITCHAT_REPLIES["identity"]


# --------------------------------------------------------------------------
# 五、静态守卫：intent 通道必须声明（否则 LangGraph 静默丢弃）
# --------------------------------------------------------------------------

def test_intent_channel_is_declared_in_state() -> None:
    declared = typed_dict_keys(QUERY_STATE, "QueryGraphState")
    assert "intent" in declared
