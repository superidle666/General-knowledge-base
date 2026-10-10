"""库级元数据问题闸（元数据检索阶段一）的离线守卫。

这一闸取代 R16 的固定话术拒答：同样的零检索零生成，但**真把答案答出来**——事实由接口层
预载在 `library_facts` 画像里，图内只读 dict、用模板拼句子，模型完全不参与生成。

守卫分四层，缺一不可：
1. **句式**：该命中的命中、不该命中的（存在性追问、内容问句）不命中；
2. **反护栏**：本轮给了任何可对齐线索就放走（宁查勿否，误拒答是用户可见硬错误）；
3. **生效范围**：disabled 库与 required 空壳库整段不生效——与旧 R16 同一条判据，零回归可证；
4. **语义槽分离**：库里没证据 / 检索没跑成 / 元数据查挂了，三句话术不许互串，
   画像读挂时必须置 `metadata_query_failed`，绝不能退成"库里没有这些内容"。

行为用例一律把实体抽取 LLM 与闲聊 L2 兜底打桩掉，只走确定性规则，不发任何外部调用。
"""

from __future__ import annotations

import pytest
from graph_introspection import PROJECT_ROOT, typed_dict_keys

from processor.query_processor.intents import (
    INTENT_CHITCHAT,
    INTENT_INVENTORY_REFUSAL,
    INTENT_METADATA_CONFIG,
    INTENT_METADATA_DOCUMENT,
    INTENT_METADATA_ENTITY,
    INTENT_METADATA_STATS,
    INTENT_METADATA_UNAVAILABLE,
    INTENT_METADATA_UNSUPPORTED,
)
from processor.query_processor.metadata_gate import (
    MAX_LISTED_ITEMS,
    METADATA_OVERFLOW_TEMPLATE,
    METADATA_TRUNCATED_SUFFIX,
    METADATA_UNAVAILABLE_REPLY,
    METADATA_UNSUPPORTED_REPLY,
    is_metadata_question,
    is_unsupported_metadata_question,
    render_metadata_answer,
    resolve_metadata_intent,
)
from processor.query_processor.nodes import node_prepare_query as node_module
from processor.query_processor.nodes.node_prepare_query import (
    METADATA_ANSWER_DISABLE_ENV,
    NodePrepareQuery,
)

STATE_FILE = PROJECT_ROOT / "processor" / "query_processor" / "state.py"

# 现场库的样子：required 模式，两条华为实体（规范名按导入侧规则由文件名派生）
VOCABULARY = [
    {"name": "华为平板 C7 用户指南", "aliases": []},
    {"name": "华为擎云 B530 用户指南", "aliases": []},
]


def _facts(**overrides) -> dict:
    """一份"画像可用"的默认返回值：数字刻意互相不等，好验证答案没把列出数当成总数。"""
    base = {
        "loaded": True,
        "library_id": 7,
        "library_name": "消费电子资料库",
        "entity_recognition_mode": "required",
        "entity_confirm_mode": "strict",
        "use_hyde": False,
        "visibility": "private",
        "as_of": "2026-10-10 09:30",
        "total_document_count": 12,
        "ready_document_count": 10,
        "failed_document_count": 1,
        "pending_document_count": 1,
        "chunk_total": 3456,
        "entity_count": 2,
        "entities": [
            {"name": "华为平板 C7 用户指南", "aliases": ["C7"], "source": "llm",
             "maybe_series": False, "document_id": 1},
            {"name": "华为擎云 B530 用户指南", "aliases": [], "source": "manual",
             "maybe_series": False, "document_id": 2},
        ],
        "documents": [
            {"id": 1, "title": "华为平板 C7 用户指南", "status": "ready", "version": 1},
            {"id": 2, "title": "华为擎云 B530 用户指南", "status": "ready", "version": 1},
            {"id": 3, "title": "FANUC R-2000iC165F 技术文档", "status": "processing", "version": 2},
        ],
        "entities_truncated": False,
        "documents_truncated": False,
    }
    base.update(overrides)
    return base


def _state(query: str, mode: str = "required", vocabulary: object = VOCABULARY,
           facts: dict | None = None, history: list | None = None) -> dict:
    return {
        "original_query": query,
        "library_id": 7,
        "entity_recognition_mode": mode,
        "entity_vocabulary": vocabulary,
        "library_facts": _facts() if facts is None else facts,
        "history": history or [],
    }


@pytest.fixture()
def node(monkeypatch):
    """离线节点：实体抽取 LLM 与闲聊 L2 兜底都打桩，测试只走正则 + 确定性规则。"""
    prepared = NodePrepareQuery()
    monkeypatch.setattr(prepared, "_call_entity_llm", lambda text, history_text: [])
    monkeypatch.setattr(node_module, "classify_chitchat_llm", lambda text, history_text="": (None, ""))
    return prepared


# --------------------------------------------------------------------------
# 一、句式判定（纯函数，零外呼；接口层预探测用的就是它）
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "query",
    [
        "当前知识库有哪些华为的产品",  # 用户实测原句（清单型）
        "知识库里都有哪些型号",
        "这个知识库的产品清单",
        "知识库里有多少份文档",  # 计数型
        "库里有多少文档",  # 计数型省掉量词（线上实测写法，当时漏吃落到了确认闸的反问上）
        "知识库里有多少切片",
        "本库收录了多少个产品",
        "库里一共有几篇资料",
        "知识库开了HyDE吗",  # 配置型
        "这个知识库的实体识别是什么模式",
        "库里的可见范围设置成什么了",
    ],
)
def test_metadata_skeleton_matches_library_level_questions(query: str) -> None:
    assert is_metadata_question(query) is True


@pytest.mark.parametrize(
    "query",
    [
        "华为平板 C7 怎么连接 wifi",
        "HAK-180 的烫印温度调多少",
        "知识库里有没有华为平板的说明书",  # "有没有"是存在性追问，内容检索答得了
        "这款产品的说明书目录在哪",  # 指代 + 问文档位置
        "这个电池有多少个电芯",  # 计数骨架但问的是文档内容（量词后面不是库级名词）
        "这块电池有多少电芯",  # 省掉量词也不收：没有库级范围词托底，这条骨架与内容问句无法区分
        "大语言模型是什么",
        "星辰一号的故障代码表怎么看",
    ],
)
def test_metadata_skeleton_ignores_content_questions(query: str) -> None:
    assert is_metadata_question(query) is False


@pytest.mark.parametrize(
    "query",
    [
        "这个产品多少钱",
        "有没有现货",
        "什么时候发货",
        "你们的客户名单能给我吗",
    ],
)
def test_unsupported_skeleton_matches_out_of_library_facts(query: str) -> None:
    assert is_unsupported_metadata_question(query) is True


@pytest.mark.parametrize(
    "query",
    [
        "HAK-180 的保修期是几年",  # 保修期写在手册里，是内容问题
        "这份资料的售价表在哪一页",  # 点了具体对象，库里真有价格表时答得了
        "怎么联系售后",  # 服务渠道在文档里，不算商业交易诉求
    ],
)
def test_unsupported_skeleton_does_not_eat_content_questions(query: str) -> None:
    assert is_unsupported_metadata_question(query) is False


# --------------------------------------------------------------------------
# 二、口径分流与模板渲染（不驱动整张图，只验拼接本身）
# --------------------------------------------------------------------------

def test_entity_inventory_lists_names_and_total() -> None:
    intent = resolve_metadata_intent("当前知识库有哪些华为的产品", _facts())
    assert intent == INTENT_METADATA_ENTITY
    answer = render_metadata_answer(intent, "当前知识库有哪些华为的产品", _facts())
    assert "华为平板 C7 用户指南" in answer
    assert "华为擎云 B530 用户指南" in answer
    assert "截至 2026-10-10 09:30" in answer
    assert "共 2 个" in answer


def test_stats_intent_and_numbers_are_all_passed_through() -> None:
    intent = resolve_metadata_intent("知识库里有多少份文档", _facts())
    assert intent == INTENT_METADATA_STATS
    answer = render_metadata_answer(intent, "知识库里有多少份文档", _facts())
    # 三项计数一次给全并声明未限定时间窗：宁可多说，也不反问"您要统计哪一项"
    assert "12 份文档" in answer
    assert "3456 个切片" in answer
    assert "未限定时间范围" in answer


def test_stats_intent_survives_the_missing_quantifier() -> None:
    """"库里有多少文档"不带量词：曾经不被认为元问题，于是落到确认闸去了——
    用户问数字，拿到的却是一句"您想问哪个产品"。问的是数字就不是清单口径。"""
    intent = resolve_metadata_intent("库里有多少文档", _facts())
    assert intent == INTENT_METADATA_STATS
    answer = render_metadata_answer(intent, "库里有多少文档", _facts())
    assert "12 份文档" in answer
    assert "FANUC R-2000iC165F 技术文档" not in answer, "计数口径不该念文档名"


def test_config_intent_answers_library_settings() -> None:
    intent = resolve_metadata_intent("知识库开了HyDE吗", _facts())
    assert intent == INTENT_METADATA_CONFIG
    answer = render_metadata_answer(intent, "知识库开了HyDE吗", _facts())
    assert "增强检索（HyDE）=未开启" in answer
    assert "消费电子资料库" in answer


def test_document_inventory_is_used_when_asking_for_documents() -> None:
    intent = resolve_metadata_intent("知识库里有哪些文档", _facts())
    assert intent == INTENT_METADATA_DOCUMENT
    answer = render_metadata_answer(intent, "知识库里有哪些文档", _facts())
    assert "FANUC R-2000iC165F 技术文档" in answer
    assert "本库共有 12 份文档" in answer


def test_inventory_falls_back_to_documents_when_no_entities() -> None:
    """画像里没有实体时退到文档口径：粒度变粗但句子仍然真，比"本库没有产品"好。"""
    facts = _facts(entity_count=0, entities=[])
    intent = resolve_metadata_intent("知识库里有哪些产品", facts)
    assert intent == INTENT_METADATA_DOCUMENT
    assert "FANUC R-2000iC165F 技术文档" in render_metadata_answer(intent, "知识库里有哪些产品", facts)


def test_brand_filter_narrows_the_list_without_sql() -> None:
    """品牌是过滤槽不是放走线索：本库有华为也有别的，只报华为那几款。"""
    facts = _facts(
        entity_count=3,
        entities=[
            {"name": "华为平板 C7 用户指南", "aliases": [], "source": "llm",
             "maybe_series": False, "document_id": 1},
            {"name": "iPhone 18 Pro 用户指南", "aliases": [], "source": "llm",
             "maybe_series": False, "document_id": 2},
            {"name": "华为擎云 B530 用户指南", "aliases": [], "source": "llm",
             "maybe_series": False, "document_id": 3},
        ],
    )
    answer = render_metadata_answer(INTENT_METADATA_ENTITY, "当前知识库有哪些华为的产品", facts)
    assert "华为平板 C7 用户指南" in answer
    assert "iPhone 18 Pro 用户指南" not in answer
    # 品牌过滤后的数不能写成全库总数，否则"共 3 个"配两条名单，用户会以为漏了两条
    assert "共 2 个" in answer


def test_unlisted_brand_says_so_and_shows_what_we_do_have() -> None:
    facts = _facts(
        entity_count=1,
        entities=[{"name": "iPhone 18 Pro 用户指南", "aliases": [], "source": "llm",
                   "maybe_series": False, "document_id": 1}],
    )
    answer = render_metadata_answer(INTENT_METADATA_ENTITY, "知识库里有哪些小米的产品", facts)
    assert "未收录 小米" in answer
    assert "iPhone 18 Pro 用户指南" in answer


def test_long_list_is_capped_but_total_is_not_shrunk() -> None:
    """列出数被上界截断时，总数照旧报真值并补一句"另有 N 项未列出"。"""
    many = [
        {"name": f"华为擎云 W{index:03} 用户指南", "aliases": [], "source": "llm",
         "maybe_series": False, "document_id": index}
        for index in range(1, 11)
    ]
    facts = _facts(entity_count=len(many), entities=many)
    answer = render_metadata_answer(INTENT_METADATA_ENTITY, "知识库里有哪些产品", facts)
    assert "本库共收录 10 个产品实体" in answer
    assert METADATA_OVERFLOW_TEMPLATE.format(rest=len(many) - MAX_LISTED_ITEMS) in answer
    assert answer.count("用户指南") == MAX_LISTED_ITEMS


def test_truncated_profile_admits_the_cap() -> None:
    """画像本身被 MAX_FACTS_ENTITIES 截断时必须明说，不能把"前 200 项"说成全部。"""
    facts = _facts(entity_count=500, entities_truncated=True)
    answer = render_metadata_answer(INTENT_METADATA_ENTITY, "知识库里有哪些产品", facts)
    assert METADATA_TRUNCATED_SUFFIX in answer


def test_unavailable_profile_never_claims_the_library_is_empty() -> None:
    """画像不可用时的那句里不得出现"库里没有"，也不得出现任何数字。"""
    answer = render_metadata_answer(INTENT_METADATA_ENTITY, "知识库里有哪些产品", {})
    assert answer == METADATA_UNAVAILABLE_REPLY
    assert "没有这些内容" in answer  # 明说这不是"库里没有"


# --------------------------------------------------------------------------
# 三、行为：命中元问题就零成本短路，且不进实体对齐
# --------------------------------------------------------------------------

def test_inventory_question_is_answered_not_refused(node) -> None:
    """R16 那批"拒答"断言在这里全部反过来：同样的触发句，现在必须真答出来。"""
    called = {"align": False}
    node._align_entity = lambda state: called.__setitem__("align", True)

    state = node.process(_state("当前知识库有哪些华为的产品"))

    assert state["intent"] == INTENT_METADATA_ENTITY
    assert "华为平板 C7 用户指南" in state["answer"]
    assert state["sources"] == []  # J2：短路要产出空来源列表，前端据此不显示"基于 0 个来源"
    assert state["item_names"] == []
    assert called["align"] is False, "元数据问题不该再花一次实体抽取调用"
    assert not state.get("metadata_query_failed")


def test_stats_and_config_questions_short_circuit_too(node) -> None:
    for query, expected_intent in (
        ("知识库里有多少份文档", INTENT_METADATA_STATS),
        ("库里有多少文档", INTENT_METADATA_STATS),
        ("知识库开了HyDE吗", INTENT_METADATA_CONFIG),
    ):
        state = node.process(_state(query))
        assert state["intent"] == expected_intent
        assert state["sources"] == []
        assert state["answer"]


def test_unsupported_question_is_blocked_before_retrieval(node) -> None:
    """"这个产品多少钱"必须一句顶死。放行走检索的结局就是那次线上复现：
    从前言与免责声明里编出一段"请咨询官方渠道"，还带着 sources=5。
    """
    called = {"align": False}
    node._align_entity = lambda state: called.__setitem__("align", True)

    state = node.process(_state("这个产品多少钱"))

    assert state["intent"] == INTENT_METADATA_UNSUPPORTED
    assert state["answer"] == METADATA_UNSUPPORTED_REPLY
    assert state["sources"] == []
    assert called["align"] is False


def test_unsupported_releases_question_naming_a_real_model(node) -> None:
    """点了具体型号的价格问句不顶死：手册里真有价格表时那是能答的内容问题，顶死就是误拒答。"""
    called = {"align": False}
    node._align_entity = lambda state: called.__setitem__("align", True)

    state = node.process(_state("华为平板 C7 多少钱"))

    assert state.get("intent") != INTENT_METADATA_UNSUPPORTED
    assert not state.get("answer")
    assert called["align"] is True


def test_missing_profile_marks_failure_and_uses_the_degraded_reply(node) -> None:
    """画像缺失（接口层读库失败）时置故障标记，且话术必须不是"库里没有"。"""
    state = node.process(_state("当前知识库有哪些华为的产品", facts={"loaded": False, "library_id": 7}))

    assert state["intent"] == INTENT_METADATA_UNAVAILABLE
    assert state["metadata_query_failed"] is True
    assert state["answer"] == METADATA_UNAVAILABLE_REPLY
    assert state["sources"] == []


def test_decision_reads_current_turn_text_only(node) -> None:
    """判定只能吃本轮原文（与 R2 的拒答护栏同口径）：改写串里被历史回填的清单句不算
    "用户本轮在要清单"，否则上一轮问过清单之后本轮的普通追问会被连环误答。
    """
    called = {"align": False}
    node._align_entity = lambda state: called.__setitem__("align", True)
    node._rewrite_query_with_history = lambda query, history: "当前知识库有哪些产品"

    state = node.process(_state("HAK-180 怎么设置温度", vocabulary=[{"name": "HAK-180", "aliases": []}]))

    assert state["rewritten_query"] == "当前知识库有哪些产品"  # 检索词仍走改写通道
    assert state["query_entity_text"] == "HAK-180 怎么设置温度"  # 判定拿的是原文
    assert not state.get("answer")
    assert called["align"] is True


# --------------------------------------------------------------------------
# 四、反护栏：任何"用户给了可对齐线索"的问法都照常走检索（宁查勿否）
# --------------------------------------------------------------------------

def test_gate_releases_question_naming_a_library_entity(node) -> None:
    """点名了本库词表里的实体就不是元问题：纯中文名正则抽不到形状，只能靠词表探测放走。"""
    state = node.process(_state("星辰一号有哪些型号", vocabulary=[{"name": "星辰一号", "aliases": []}]))

    assert not state.get("answer")
    assert state.get("intent") != INTENT_METADATA_ENTITY


def test_gate_releases_question_naming_any_model_shape(node) -> None:
    """提到任何像型号的东西（哪怕本库没有）也不当元问题：误拒是硬错误，漏拒只是回到现状。"""
    state = node.process(_state("有哪些产品支持 G740"))

    assert not state.get("answer")
    assert state.get("intent") != INTENT_METADATA_ENTITY


def test_gate_releases_coreference_question(node) -> None:
    """"这款有哪些型号"问的是上下文里那个对象，交给改写 + 检索，不能当清单问题答掉。"""
    state = node.process(_state("这款有哪些型号", history=[{"role": "user", "text": "华为平板 C7 怎么样"}]))

    assert not state.get("answer")
    assert state.get("intent") != INTENT_METADATA_ENTITY


# --------------------------------------------------------------------------
# 五、生效范围与回滚开关
# --------------------------------------------------------------------------

def test_gate_never_touches_disabled_library(node) -> None:
    """disabled 库整段不启用（本阶段只管强制实体库）：没有实体可列，行为与改造前逐字一致。"""
    called = {"align": False}
    node._align_entity = lambda state: called.__setitem__("align", True)

    state = node.process(_state("当前知识库有哪些产品", mode="disabled"))

    assert not state.get("answer")
    assert "metadata_query_failed" not in state
    assert called["align"] is False  # disabled 库本来就不对齐（零回归）


def test_gate_never_touches_empty_required_vocabulary(node) -> None:
    """required 但词表为空（切了模式未重导入的空壳库）同样不启用：与实体对齐可用性同口径。"""
    state = node.process(_state("当前知识库有哪些产品", vocabulary=[]))

    assert not state.get("answer")
    assert state.get("intent") != INTENT_METADATA_ENTITY


def test_rollback_switch_restores_the_r16_refusal(node, monkeypatch) -> None:
    """`METADATA_ANSWER_DISABLE=1` 一键退回改造前：清单句仍旧话术拒答，计数句仍旧交给对齐与检索。"""
    monkeypatch.setenv(METADATA_ANSWER_DISABLE_ENV, "1")
    called = {"align": False}
    node._align_entity = lambda state: called.__setitem__("align", True)

    inventory = node.process(_state("当前知识库有哪些华为的产品"))
    assert inventory["intent"] == INTENT_INVENTORY_REFUSAL
    assert inventory["answer"]  # 旧固定话术

    called["align"] = False
    stats = node.process(_state("知识库里有多少份文档"))
    assert stats.get("intent") != INTENT_METADATA_STATS
    assert called["align"] is True, "开关打开时计数型不该被元数据闸吃掉，应回到改造前那条链"


def test_chitchat_still_wins_when_the_turn_is_chitchat(node) -> None:
    """闸序不变：闲聊仍排在元数据闸之前，"你是谁"不该被当成在问库配置。"""
    state = node.process(_state("你是谁"))

    assert state["intent"] == INTENT_CHITCHAT


# --------------------------------------------------------------------------
# 六、静态守卫：新键必须在状态里声明
# --------------------------------------------------------------------------

def test_metadata_state_keys_are_declared() -> None:
    """未声明的键会被 LangGraph 静默丢弃：画像进不来、故障标记出不去，都表现为静默不答。"""
    declared = typed_dict_keys(STATE_FILE, "QueryGraphState")
    assert {"library_facts", "metadata_query_failed", "intent", "answer", "sources"} <= declared
