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

两条已经定死的口径（新用例不得往回改）：
- L2 必须收到**带说话人标注的最近几轮**（M1）：只给当前句会把"省略宾语的追问"判成
  "让助手自我介绍"（线上实测）；因此所有 stub 都收两个位置参数 (text, history_text)。
- 话术**一律**取自静态表：other 子类型也不采纳模型顺带生成的 reply（M9）。
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
    is_knowledge_question,
    should_ask_llm,
)
from processor.query_processor.prompt.intent_classify import (
    MAX_INTENT_HISTORY_CHARS,
    INTENT_CLASSIFY_SYSTEM,
    build_intent_classify_prompt,
)
from processor.query_processor.nodes.node_prepare_query import NodePrepareQuery

QUERY_STATE = PROJECT_ROOT / "processor" / "query_processor" / "state.py"


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch):
    """全局守卫：离线用例里任何走到真实 LLM 的调用都立即报错，绝不允许静默联网。"""

    def _boom(text: str, history_text: str = ""):
        raise AssertionError(f"离线用例不应触发真实意图分类 LLM 调用：{text!r} / 历史={history_text!r}")

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


def test_ellipsis_object_followups_are_not_caught_by_l1() -> None:
    """M2 实测回归（例 1/例 3 原句）：省略主语的属性追问与能力裸词同形，不得在 L1 被零降级吃掉。

    它们不是"不拦了"，而是改走 L2：那里有历史可看，也有"拿不准就判 knowledge"的降级余地。
    带"你"主语锚的合法打探必须仍由 _CAPABILITY_RES 接住，摘条目的代价不能外溢。"""
    for text in ("有什么功能介绍吗", "有什么存储类型", "有什么作用", "能回答什么", "简单介绍一下"):
        assert detect_chitchat(text, l1_only=True) is None, f"属性追问被 L1 误拦：{text}"
        assert should_ask_llm(text) is True, f"摘掉裸词后又进不了 L2：{text}"
    assert detect_chitchat("你能提供什么", l1_only=True) == "capability"
    assert detect_chitchat("你能做什么", l1_only=True) == "capability"
    assert detect_chitchat("你有什么功能", l1_only=True) == "capability"


# --------------------------------------------------------------------------
# 二d、R1：高误拦裸词摘除 + 知识问法骨架护栏（跨领域失配的治本项）
# --------------------------------------------------------------------------

def test_knowledge_skeleton_questions_are_not_chitchat_by_rules() -> None:
    """实证现场：这些都在问概念/问文档，旧版被 model_probe 组的裸词"模型"在 L1 直接吃掉。"""
    for text in (
        "什么是大语言模型",
        "什么是llm",
        "llm是什么",
        "什么是Agent",
        "agent和llm的区别",
        "测试方法是什么",
        "模型1的固件版本",
    ):
        assert detect_chitchat(text, l1_only=True) is None, f"知识问法被 L1 误拦：{text}"


def test_knowledge_skeleton_skips_the_l2_classification_call() -> None:
    """命中骨架就已经确定是业务问题，不该再为它付一次分类调用（最坏 4s）与误判风险。"""
    for text in ("什么是大语言模型", "什么是llm", "agent和llm的区别", "测试方法是什么", "llm是什么"):
        assert is_knowledge_question(text) is True, f"未识别为知识问法：{text}"
        assert should_ask_llm(text) is False, f"仍会去问小模型：{text}"


def test_knowledge_skeleton_does_not_swallow_chitchat_templates() -> None:
    """护栏只认"问概念"的骨架：寒暄/存在/情绪类问法一律不能被它放行到检索链路。"""
    for text in ("你好", "你是谁", "你是什么模型", "你在哪", "今天心情怎么样", "你能做什么", "谢谢"):
        assert is_knowledge_question(text) is False, f"闲聊被骨架护栏错放：{text}"


def test_model_probe_still_intercepted_after_removing_bare_words() -> None:
    """摘裸词的边界：真正指向助手自身的打探必须由细粒度正则/竞品名接住，不能靠裸词兜。"""
    assert detect_chitchat("你是什么模型", l1_only=True) == "model_probe"
    assert detect_chitchat("你用的是什么模型", l1_only=True) == "model_probe"
    assert detect_chitchat("你是哪一家的模型", l1_only=True) == "model_probe"
    assert detect_chitchat("底层跑的是什么", l1_only=True) == "model_probe"
    assert detect_chitchat("你们是哪家公司开发的", l1_only=True) == "model_probe"
    assert detect_chitchat("你是gpt几", l1_only=True) == "model_probe"
    assert detect_chitchat("你是谁", l1_only=True) == "identity"


def test_bare_document_vocabulary_words_are_not_registered_probes() -> None:
    """静态钉住：文档高频词与**无主语锚的能力裸词**都不得回到子串表（与 L158-161 的注释同一口径）。"""
    banned_terms = (
        "模型", "model", "Model", "测试",
        # M2：这四条与省略宾语的属性追问同形（"（这台设备）有什么功能介绍吗"），
        # 而 L1 命中是零降级路径——不经模型、没有"拿不准就放行"的余地。
        "能提供什么", "有什么功能", "能回答什么", "有什么作用",
    )
    for name, phrases in chitchat_gate_module._MATCH_ORDER:
        for banned in banned_terms:
            assert banned not in phrases, f"{name} 组又登记了裸词 {banned!r}"


# --------------------------------------------------------------------------
# 二 e、A：identity 模板锁定"你必须是话题主语" + 对话回指句不被闲聊吃
# 实证现场：用户问“刚刚我问了你什么”被回了整句自我介绍——历史真的在存/在读，
# 是闲聊闸短路了整条检索+生成链路，压根没走到“用历史生成答案”那一步。
# --------------------------------------------------------------------------

def test_identity_regex_requires_you_as_the_topic() -> None:
    """“你”在宾语位置（我问了**你**什么）不是打探身份，不得拦截。"""
    for text in (
        "刚刚我问了你什么",
        "刚才我问了你什么",
        "我刚刚问了什么",
        "总结一下我问你的问题",
        # 只验证锚定本身（无回指词，靠不到骨架护栏）：“你”在宾语位置就不是问助手身份。
        # 旧正则 `你(是|叫|用)?(谁|什么|哪位|啥)` 无左边界，这句会被直接当自我介绍回。
        "老板问你什么时候下班",
    ):
        assert detect_chitchat(text, l1_only=True) is None, f"回指/宾语句式被 L1 误拦：{text}"


def test_identity_still_hits_after_anchoring_the_template() -> None:
    """锚定的代价不能外溢：真正在问助手身份的变体必须原样接住。"""
    for text in ("你是谁", "你是谁呀", "你是什么", "你叫什么名字", "你是哪位", "请问你是谁", "你到底是谁"):
        assert detect_chitchat(text, l1_only=True) == "identity", f"身份打探漏拦：{text}"
    # 锚定后不得被 identity 抢走：这句归 capability（正则模板顺序比 identity 靠后）
    assert detect_chitchat("你能做什么", l1_only=True) == "capability"
    # 子串表继续兜住非句首位置的身份短语（锚定只作用于正则模板）
    assert detect_chitchat("所以你是干什么的", l1_only=True) == "identity"
    # 既有行为不变：这句由 capability 正则模板先接住（模板循环跑在子串表之前）
    assert detect_chitchat("你有什么功能", l1_only=True) == "capability"
    # 有实际诉求的“你是…”仍不得拦（原有用例的外溢守卫）
    assert detect_chitchat("你是技术支持吗", l1_only=True) is None


def test_conversation_reference_questions_skip_l2_call() -> None:
    """只修 L1 不够：这类句子≤ 12 字且无实体信号，L2 分类同样可能把它再短路一次。"""
    for text in ("刚刚我问了你什么", "我之前问的是什么", "刚才的答案再说一遍", "上面我说的问题"):
        assert is_knowledge_question(text) is True, f"未识别为对话回指问法：{text}"
        assert should_ask_llm(text) is False, f"仍会去问小模型：{text}"


def test_node_answers_history_reference_question_with_generation(monkeypatch) -> None:
    """端到端：这句话必须走到带历史的生成，而不是被预置话术吃掉。"""
    node = NodePrepareQuery()
    calls: dict[str, object] = {"llm": False, "align": False}

    def _spy(_text: str, _history_text: str = ""):
        calls["llm"] = True
        return "identity", "不该被调用"

    monkeypatch.setattr(node_module, "classify_chitchat_llm", _spy)
    node._align_entity = lambda state: calls.__setitem__("align", True)
    state = node.process(_state("刚刚我问了你什么"))
    assert state.get("intent") is None
    assert not state.get("answer")
    assert calls["llm"] is False, "对话回指问法不该问小模型"
    assert calls["align"] is True


# --------------------------------------------------------------------------
# 二c、L2 门禁与返回解析（纯函数，不触发调用）
# --------------------------------------------------------------------------

def test_intent_prompt_shows_recent_dialog_and_declines_the_generated_reply() -> None:
    """M1 提示词侧守卫：分类器必须拿到【最近对话】（带说话人），且 system 里要明写
    "省掉主语/宾语时先看上文"与"reply 不会直接发给用户"——前者是误拦的直接成因，
    后者不让模型为了"回得好听"去执行背诗/报参数这类任务。"""
    without = build_intent_classify_prompt("有什么存储类型")
    assert "最近对话" not in without
    assert "本轮输入：有什么存储类型" in without

    with_history = build_intent_classify_prompt(
        "有什么存储类型", "用户：有苹果手机吗\n助手：有，iPhone 18 Pro 是新一代苹果智能手机。"
    )
    assert "用户：有苹果手机吗" in with_history
    assert "助手：" in with_history
    assert "本轮输入：有什么存储类型" in with_history

    # 历史按预算从尾部裁：不能因为一句长回答把整轮窗口顶爆
    over = build_intent_classify_prompt("这个呢", "开" * (MAX_INTENT_HISTORY_CHARS + 500) + "尾")
    assert "尾" in over
    assert "开" * (MAX_INTENT_HISTORY_CHARS + 1) not in over

    assert "最近对话" in INTENT_CLASSIFY_SYSTEM
    assert "省掉了主语" in INTENT_CLASSIFY_SYSTEM
    assert "不会直接发给用户" in INTENT_CLASSIFY_SYSTEM


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


def test_node_does_not_intercept_or_call_llm_for_concept_question(monkeypatch) -> None:
    # 回归现场（真实对话日志）："什么是大语言模型"被闲聊闸判成"打探底层模型"，
    # 完全没进检索链路。现在既不短路、也不付 L2 分类调用，直接进实体对齐与检索。
    node = NodePrepareQuery()
    calls: dict[str, object] = {"llm": False, "align": False}

    def _spy(_text: str, _history_text: str = ""):
        calls["llm"] = True
        return "model_probe", "不该被调用"

    monkeypatch.setattr(node_module, "classify_chitchat_llm", _spy)
    node._align_entity = lambda state: calls.__setitem__("align", True)
    state = node.process(_state("什么是大语言模型"))
    assert state.get("intent") is None
    assert not state.get("answer")
    assert calls["llm"] is False, "命中知识问法骨架就不该问小模型"
    assert calls["align"] is True


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

    def _fake_llm(text: str, history_text: str = ""):
        calls["llm_text"] = text
        return "emotion", "听起来心情不错！有需要查的产品问题随时告诉我。"

    monkeypatch.setattr(node_module, "classify_chitchat_llm", _fake_llm)
    state = node.process(_state("今天心情怎么样"))
    assert state["intent"] == "chitchat"
    assert state["answer"] in CHITCHAT_REPLIES["emotion"]  # 已知子类型用静态表（候选内随机），口径统一
    assert calls["llm_text"] == "今天心情怎么样"
    assert calls["align"] is False


def test_node_l2_other_subtype_never_adopts_generated_reply(monkeypatch) -> None:
    """M9：话术一律走静态表，other 子类型也不再采纳模型现编的那句。

    原实现把模型生成内容直接当 answer，等于在检索链路之外开了一个零来源、且被 R5
    无证据守卫第一行（`answer` 非空即放行）直接放过的自由生成入口——线上实测「背一下
    静夜思」真的背了诗，"这个药一次吃多少"一类问法同样会被凭空作答。"""
    text = "帮我测测这个新功能呀"
    node = NodePrepareQuery()
    monkeypatch.setattr(
        node_module, "classify_chitchat_llm",
        lambda _text, _history_text="": ("other", "床前明月光，疑是地上霜。"),
    )
    state = node.process(_state(text))
    assert state["answer"] in CHITCHAT_REPLIES["other"]
    assert "床前明月光" not in state["answer"], "模型生成内容不得进入 answer"

    # 已知子类型同样只用静态表（口径统一，不区分 other）
    node2 = NodePrepareQuery()
    monkeypatch.setattr(
        node_module, "classify_chitchat_llm",
        lambda _text, _history_text="": ("greeting", "自己现编的一句话"),
    )
    state2 = node2.process(_state(text))
    assert state2["answer"] in CHITCHAT_REPLIES["greeting"]
    assert state2["answer"] != "自己现编的一句话"


def test_node_l2_says_not_chitchat_then_aligns_normally(monkeypatch) -> None:
    # L2 判非闲聊（返回 None）：不预置 answer，流程回到实体对齐，宁查勿否。
    node = NodePrepareQuery()
    called = {"align": False}
    node._align_entity = lambda state: called.__setitem__("align", True)
    monkeypatch.setattr(node_module, "classify_chitchat_llm", lambda text, history_text="": (None, ""))
    state = node.process(_state("温度调多少合适"))
    assert state.get("intent") is None
    assert not state.get("answer")
    assert called["align"] is True


def test_node_disabled_lib_gate_is_stricter_than_required(monkeypatch) -> None:
    # 8~12 字的无实体短句：required 库走 L2（可省抽取调用），disabled 库无调用可省、不付费。
    text = "帮我测测这个新功能呀"
    node = NodePrepareQuery()
    reached: dict[str, bool] = {"llm": False}

    def _spy(text_: str, _history_text: str = ""):
        reached["llm"] = True
        return None, ""

    monkeypatch.setattr(node_module, "classify_chitchat_llm", _spy)
    node.process(_state(text, mode="disabled"))
    assert reached["llm"] is False
    node.process(_state(text, mode="required"))
    assert reached["llm"] is True


def test_node_l2_passes_recent_dialog_with_speaker_labels(monkeypatch) -> None:
    """M1：节点必须把带说话人标注的最近几轮（**含助手回答**）交给分类器。

    只给当前句时分类器只能看到"让助手自我介绍"，必然误拦（线上实测的同一形状）。
    本例的两个约束都是必须的（否则 M4/指代通道会先把本轮接走，根本走不到 L2）：
    - 本轮不带"有什么/多少"这类属性骨架：否则 M4 先回填话题锚，回填后带上了实体信号，
      就不再需要一次分类（确定通道优于模型临场判断，见 tests/test_multi_turn_entity_anchor.py）；
    - 上一轮的**用户句**里没有实体（只是"有哪些手机"），否则指代通道已经替本轮把锚补上了。
      这时实体锚只在助手回答里，而抽取/指代两条通道都刻意不看助手——只有 L2 看。"""
    seen: list[tuple[str, str]] = []

    def _spy(text: str, history_text: str = ""):
        seen.append((text, history_text))
        return None, ""

    monkeypatch.setattr(node_module, "classify_chitchat_llm", _spy)
    node = NodePrepareQuery()
    node._align_entity = lambda state: None
    state = _state("这个能干什么")
    state["history"] = [
        {"role": "user", "text": "有哪些手机"},
        {"role": "assistant", "text": "有，iPhone 18 Pro 是新一代苹果智能手机。" + "细节描述" * 100},
    ]
    node.process(state)

    assert seen, "该走 L2 却没调用分类器"
    query, history_text = seen[0]
    assert query == "这个能干什么"
    assert "用户：有哪些手机" in history_text, history_text
    assert "助手：" in history_text, "分类器拿不到助手回答就没有实体锚"
    assert len(history_text) <= node_module.INTENT_HISTORY_CHARS


def test_recent_dialog_text_covers_both_speakers_and_bounds_length() -> None:
    """历史窗口的三个硬约束：只收 user/assistant、单条按头部截断、总量不超预算。

    单条截断而不是尾部截：助手回答的产品名几乎都在首句（"iPhone 18 Pro 是…"），
    去掉尾部还能留下实体锚，去掉头部就只剩下"……详见附录"了。"""
    dialog = NodePrepareQuery._recent_dialog_text
    assert dialog([]) == ""
    assert dialog(None) == ""
    assert dialog("不是列表") == ""
    assert dialog([{"role": "system", "text": "你是一个助手"}]) == ""
    assert dialog([{"role": "user"}]) == ""

    text = dialog([
        {"role": "user", "text": "有苹果手机吗"},
        {"role": "assistant", "text": "A" * 500},
        {"role": "user", "text": "有什么存储类型"},
        {"role": "assistant", "text": "B" * 500},
        {"role": "user", "text": "这个能干什么"},
    ])
    assert "这个能干什么" in text, "最近一轮必须在窗口里"
    assert "有什么存储类型" in text
    assert all(line.split("：")[1] and len(line) <= node_module.INTENT_TURN_CHARS + 3 for line in text.split("\n"))
    assert len(text) <= node_module.INTENT_HISTORY_CHARS


# --------------------------------------------------------------------------
# 四c、required 空壳库（切了模式但未重导入、词表为空）不得享受宽门禁
# --------------------------------------------------------------------------

def test_entity_alignment_available_requires_mode_and_non_empty_vocabulary() -> None:
    available = NodePrepareQuery._entity_alignment_available
    assert available({"entity_recognition_mode": "required", "entity_vocabulary": [{"name": "HAK-180"}]}) is True
    assert available({"entity_recognition_mode": "required", "entity_vocabulary": []}) is False
    assert available({"entity_recognition_mode": "disabled", "entity_vocabulary": [{"name": "HAK-180"}]}) is False
    # 非法形状一律视为不可用，不能让门禁因脏数据被误放宽
    assert available({"entity_recognition_mode": "required", "entity_vocabulary": "HAK-180"}) is False
    assert available({}) is False


def test_required_lib_without_vocabulary_uses_strict_gate(monkeypatch) -> None:
    # 回归现场：后台把库改成 required 但未重导入，Entity 表 0 条 → 对齐整段跳过。
    # 此时宽门禁得不到任何收益（无抽取调用可省），只多付一次分类延迟与误拦风险，
    # 故 8~12 字短句退回 disabled 的严门禁。
    text = "帮我测测这个新功能呀"  # 10 字：宽门禁放行、严门禁拦住
    reached = {"llm": False}

    def _spy(_text: str, _history_text: str = ""):
        reached["llm"] = True
        return None, ""

    monkeypatch.setattr(node_module, "classify_chitchat_llm", _spy)
    node = NodePrepareQuery()
    node._align_entity = lambda state: None
    node.process(_vocab_state(text, mode="required", vocabulary=[]))
    assert reached["llm"] is False, "词表为空的 required 库不该走宽门禁"

    # 同一句话、词表非空（对齐真的会跑）时仍走宽门禁：本改动不能把收益场景关掉
    node.process(_vocab_state(text, mode="required"))
    assert reached["llm"] is True


def test_required_lib_without_vocabulary_still_gates_truly_short_text(monkeypatch) -> None:
    # 收紧不等于关掉：≤ 8 字的无实体短句在空壳 required 库里仍然会送 L2 判定。
    reached = {"llm": False}

    def _spy(_text: str, _history_text: str = ""):
        reached["llm"] = True
        return None, ""

    monkeypatch.setattr(node_module, "classify_chitchat_llm", _spy)
    node = NodePrepareQuery()
    node._align_entity = lambda state: None
    node.process(_vocab_state("今天心情怎么样", mode="required", vocabulary=[]))  # 7 字
    assert reached["llm"] is True


def test_node_llm_exception_falls_through_to_retrieval(monkeypatch) -> None:
    # 分类调用报意外异常（真实实现内部已降级，这里测节点调用点的兜底）：放行检索。
    node = NodePrepareQuery()
    called = {"align": False}
    node._align_entity = lambda state: called.__setitem__("align", True)

    def _raise(text: str, history_text: str = ""):
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

    def _spy(_text: str, _history_text: str = ""):
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

    def _spy(_text: str, _history_text: str = ""):
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
