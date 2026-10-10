"""多轮省略型追问的检索锚回填（M4）与对齐规范名回填（M5）的离线守卫。

要治的是漂移例 2 的那一半：**本轮没写主语**。`有苹果手机吗` → `有什么存储类型`，
第二句一个产品词都不沾，指代通道（只认"这个/它/该设备"）接不住，于是裸串被拿去全库检索，
最近邻落到别的型号上。线上实测的形态已经从"点名兄弟型号"退化成"该设备 UFS4.1 / 12+256GB"
这种隐名漂移（安卓写法出现在 iPhone 话题里），比原来更难发现。

两条通道分工（清单 §12 M4/M5）：
- **M4** 在检索词通道上把话题实体拼回头上（只从【用户】历史取值，词表优先于正则）；
- **M5** 把实体对齐已经命中的规范名也拼回头上——`item_names` 只作用在 Milvus 标量列上，
  Dense/Sparse 两路召回看的还是那句话，只改过滤不改文本等于只治一半。

两者都**绝不写** `query_entity_text`（R2，另有 AST 守卫钉在 test_query_graph_hygiene），
且判定一律朝"宁可不回填"收：回填错产品会把答案硬收窄到错型号上，比不回填更糟；
漏回填只是退回现状（裸串继续检索）。
"""
from __future__ import annotations

import pytest
from graph_introspection import PROJECT_ROOT

from processor.query_processor.nodes import node_prepare_query as node_module
from processor.query_processor.nodes.node_prepare_query import (
    MAX_RETRIEVAL_TEXT_CHARS,
    NodePrepareQuery,
)

LOCAL_NODE = PROJECT_ROOT / "processor" / "query_processor" / "nodes" / "node_search_embedding.py"
HYDE_NODE = PROJECT_ROOT / "processor" / "query_processor" / "nodes" / "node_search_embedding_hyde.py"

ANCHOR_QUESTION = "有苹果手机吗"
ANCHOR_ANSWER = "iPhone 18 Pro 提供 256GB 与 512GB 两种存储容量，起售价 8999 元。"
PHONE_HISTORY = [
    {"role": "user", "text": ANCHOR_QUESTION},
    {"role": "assistant", "text": ANCHOR_ANSWER},
]
VOCABULARY = [{"name": "iPhone 18 Pro", "aliases": ["苹果手机"]}]


def _state(query: str, history=None, mode: str = "disabled", vocabulary=None) -> dict:
    return {
        "original_query": query,
        "library_id": 7,
        "history": list(history or []),
        "entity_recognition_mode": mode,
        "entity_vocabulary": list(vocabulary or []),
    }


@pytest.fixture()
def node(monkeypatch):
    """抽取缝打桩为空（只走正则），闲聊 L2 打桩为"不是闲聊"：全程不联网。"""
    prepared = NodePrepareQuery()
    monkeypatch.setattr(prepared, "_call_entity_llm", lambda text, history_text: [])
    monkeypatch.setattr(node_module, "classify_chitchat_llm", lambda text, history_text="": (None, ""))
    return prepared


@pytest.fixture()
def llm_calls(monkeypatch):
    """记录闲聊 L2 实际被调了几次（M4 回填后带实体信号，本就不该再花那次分类）。"""
    calls: list[str] = []

    def _spy(text: str, history_text: str = ""):
        calls.append(text)
        return None, ""

    monkeypatch.setattr(node_module, "classify_chitchat_llm", _spy)
    prepared = NodePrepareQuery()
    monkeypatch.setattr(prepared, "_call_entity_llm", lambda text, history_text: [])
    return calls


# --------------------------------------------------------------------------
# 一、M4 触发面：省略了主语的属性/概述追问
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "query",
    [
        "有什么存储类型",        # 线上实测原句
        "有什么功能介绍吗",      # 线上实测：曾被 L2 判成"助手能力"回固定话术
        "多少钱",
        "那多少钱呢",            # 顶头带一个连接词也算
        "续航呢",                # 光秃秃一个属性名
        "存储类型有哪些",
        "有多少个颜色",
        "支持卫星通信吗",
        "怎么连接wifi",
        "保修多久",
        "介绍一下有哪些型号",
    ],
)
def test_ellipsis_followups_get_the_topic_anchor(node, query: str) -> None:
    state = node.process(_state(query, PHONE_HISTORY))
    assert state["rewritten_query"].startswith("苹果手机 ")
    assert query in state["rewritten_query"]
    assert not state.get("answer")


@pytest.mark.parametrize(
    "query",
    [
        "今天天气怎么样",        # 主语写全了，且与产品无关
        "你好",
        "你能做什么",            # 主语是助手自己
        "知识库有哪些产品",      # 库级清单元问题（R16），骨架词不在顶头
        "背一下静夜思",
    ],
)
def test_non_followups_are_left_untouched(node, query: str) -> None:
    """表外写法一律不动：漏回填只是退回现状，多回填则可能把闲聊句撑成业务问题。

    “换个说法”不列在这里：它是改写型元指令，会整条接管理检索词（见
    `test_meta_instruction_gate_still_owns_the_text`），拿它做“不动”断言必假。
    """
    state = node.process(_state(query, PHONE_HISTORY))
    assert state["rewritten_query"] == query


def test_first_turn_without_history_is_left_alone(node) -> None:
    assert node.process(_state("有什么存储类型"))["rewritten_query"] == "有什么存储类型"


def test_current_turn_already_naming_an_entity_is_not_prefixed(node) -> None:
    state = node.process(_state("苹果手机有什么存储类型", PHONE_HISTORY))
    assert state["rewritten_query"] == "苹果手机有什么存储类型"


# --------------------------------------------------------------------------
# 二、话题锚的取法：只认用户说过的，且必须唯一
# --------------------------------------------------------------------------

def test_anchor_only_borrows_from_user_turns_not_assistant() -> None:
    """助手回答动辄罗列一排型号，从里面挑"话题"等于替用户猜对象（R2 同口径）。"""
    state = _state(
        "有什么存储类型",
        [
            {"role": "user", "text": "有哪些手机"},
            {"role": "assistant", "text": "本店有 iPhone 18 Pro、一加 Ace 6T、小米 15。"},
        ],
    )
    assert NodePrepareQuery._topic_entity_from_history(state) == ""


def test_two_topic_entities_are_not_guessed() -> None:
    state = _state(
        "有什么存储类型",
        [
            {"role": "user", "text": "有苹果手机吗"},
            {"role": "user", "text": "华为平板多少钱"},
        ],
    )
    assert NodePrepareQuery._topic_entity_from_history(state) == ""
    assert NodePrepareQuery._backfill_ellipsis_anchor("有什么存储类型", state) == "有什么存储类型"


def test_anchor_looks_back_only_a_bounded_window() -> None:
    """窗口外的更早轮次不该继续把话题钉住：用户已经换了三个产品时不猜。"""
    history = [{"role": "user", "text": "有苹果手机吗"}] + [
        {"role": "user", "text": f"第{i}轮问的问题"} for i in range(6)
    ]
    assert NodePrepareQuery._topic_entity_from_history(_state("多少钱", history)) == ""


def test_vocab_canonical_is_preferred_over_regex_surface(node) -> None:
    """required 库用词表规范名当锚：它才是 Milvus item_name 那个写法，也才喂得进过滤。"""
    state = node.process(_state("有什么存储类型", PHONE_HISTORY, mode="required", vocabulary=VOCABULARY))
    assert state["rewritten_query"] == "iPhone 18 Pro 有什么存储类型"
    assert state["query_entity_text"] == "有什么存储类型"


def test_vocab_entity_without_shape_is_not_prefixed_again(node) -> None:
    """纯中文自有产品名正则抽不到，只能靠词表探测拦下第二道：本轮已经点名了就不再补锚。"""
    state = node.process(
        _state("星辰一号有多少颜色", [{"role": "user", "text": "星辰一号多少钱"}],
               mode="required", vocabulary=[{"name": "星辰一号", "aliases": []}])
    )
    assert state["rewritten_query"] == "星辰一号有多少颜色"


def test_reference_channel_result_is_not_doubled(node) -> None:
    """指代通道刚借到的实体不能被 M4 再拼一遍（前缀重复会稀释向量语义）。"""
    state = node.process(_state("它的存储类型是什么", [{"role": "user", "text": "有 HAK-180 吗"}]))
    assert state["rewritten_query"] == "HAK-180 它的存储类型是什么"


def test_backfill_respects_the_retrieval_text_cap() -> None:
    long_turn = "有什么存储类型" + "，顺便再讲讲细节" * 80
    anchored = NodePrepareQuery._backfill_ellipsis_anchor(
        long_turn, _state(long_turn, PHONE_HISTORY)
    )
    assert len(anchored) <= MAX_RETRIEVAL_TEXT_CHARS
    assert anchored.startswith("苹果手机 ")


# --------------------------------------------------------------------------
# 三、R2 边界：回填只影响检索词，不影响拒答/清单判定
# --------------------------------------------------------------------------

def test_anchored_model_code_absent_from_vocab_does_not_refuse(node) -> None:
    """最危险的一类误伤：锚是从历史借来的型号代码，本库词表里没有。

    拒答判定只认【本轮原文】，所以"用户本轮点名了查无此型"不成立，必须退回不带过滤检索；
    一旦哪天有人拿 rewritten_query 去判拒答，这条会立刻红。
    """
    state = node.process(
        _state("有多少内存", [{"role": "user", "text": "有 APL-005 吗"}],
               mode="required", vocabulary=[{"name": "HAK-180", "aliases": []}])
    )
    assert state["rewritten_query"] == "APL-005 有多少内存"
    assert state["query_entity_text"] == "有多少内存"
    assert not state.get("answer"), "历史借来的型号绝不能把本轮泛问题拒掉"
    assert state["item_names"] == []


@pytest.mark.parametrize("query", ["有什么存储类型", "多少钱", "续航呢"])
def test_original_text_channel_stays_the_bare_turn(node, query: str) -> None:
    state = node.process(_state(query, PHONE_HISTORY))
    assert state["query_entity_text"] == query


def test_backfilled_anchor_skips_the_chitchat_llm_call(llm_calls) -> None:
    """序列 3 的确定性出口：锚带上实体信号后，本轮不再落进闲聊 L2 的门禁。

    回填排在意图分类**之前**是刻意的——不靠模型临场判断"这句是不是在追问产品"，
    与 M3 把「简单介绍一下」挪到规则通道同一个思路。
    """
    state = NodePrepareQuery().process(_state("有什么功能介绍吗", PHONE_HISTORY))
    assert llm_calls == []
    assert state["rewritten_query"] == "苹果手机 有什么功能介绍吗"
    assert not state.get("answer")


def test_meta_instruction_gate_still_owns_the_text(node) -> None:
    """改写型元指令排在 M4 之后执行，它换的是"上一轮原话"，不该被本轮锚污染。"""
    state = node.process(_state("继续", PHONE_HISTORY))
    assert state["rewritten_query"] == ANCHOR_QUESTION


# --------------------------------------------------------------------------
# 四、M5：对齐命中的规范名补进检索词
# --------------------------------------------------------------------------

def test_aligned_canonical_name_is_prepended(monkeypatch) -> None:
    """用户写简称（ace6t）、对齐出规范名（一加 Ace 6T）：检索词里原本一个品牌词都没有。"""
    prepared = NodePrepareQuery()
    monkeypatch.setattr(prepared, "_call_entity_llm", lambda text, history_text: ["ace6t"])
    monkeypatch.setattr(node_module, "classify_chitchat_llm", lambda text, history_text="": (None, ""))
    state = prepared.process(
        _state("ace6t 有什么存储类型", [], mode="required", vocabulary=[{"name": "一加 Ace 6T", "aliases": []}])
    )
    assert state["item_names"] == ["一加 Ace 6T"]
    assert state["rewritten_query"] == "一加 Ace 6T ace6t 有什么存储类型"


def test_canonical_name_already_in_text_is_not_duplicated() -> None:
    """连字符写法差异不算"没写过"：归一化后比较，避免 HAK-180 与 HAK180 各拼一遍。"""
    state = {"item_names": ["HAK-180"], "rewritten_query": "HAK180 怎么调温"}
    NodePrepareQuery._inject_aligned_entity(state)
    assert state["rewritten_query"] == "HAK180 怎么调温"


def test_injection_prepends_at_most_two_names() -> None:
    """三个实体说明这句在比多个对象，全拼上去等于没收窄，只取前两个。"""
    state = {"item_names": ["A 型号", "B 型号", "C 型号"], "rewritten_query": "有什么区别"}
    NodePrepareQuery._inject_aligned_entity(state)
    assert state["rewritten_query"] == "A 型号 B 型号 有什么区别"


@pytest.mark.parametrize("state", [
    {"item_names": [], "rewritten_query": "有什么存储类型"},
    {"item_names": ["HAK-180"], "rewritten_query": "   "},
    {"item_names": [""], "rewritten_query": "有什么存储类型"},
    {"item_names": None, "rewritten_query": "有什么存储类型"},
])
def test_injection_is_a_no_op_without_a_usable_entity(state: dict) -> None:
    before = state["rewritten_query"]
    NodePrepareQuery._inject_aligned_entity(state)
    assert state["rewritten_query"] == before


def test_m4_anchor_and_m5_injection_are_idempotent(monkeypatch) -> None:
    """M4 已经把规范名写进句子里时，M5 不该再前置一次（两条回填共用归一化口径）。"""
    prepared = NodePrepareQuery()
    monkeypatch.setattr(prepared, "_call_entity_llm", lambda text, history_text: ["iPhone 18 Pro"])
    monkeypatch.setattr(node_module, "classify_chitchat_llm", lambda text, history_text="": (None, ""))
    state = prepared.process(_state("有什么存储类型", PHONE_HISTORY, mode="required", vocabulary=VOCABULARY))
    assert state["item_names"] == ["iPhone 18 Pro"]
    assert state["rewritten_query"] == "iPhone 18 Pro 有什么存储类型"


# --------------------------------------------------------------------------
# 五、静态守卫：回填的文本必须真的进检索
# --------------------------------------------------------------------------

def test_both_retrieval_nodes_read_the_rewritten_channel() -> None:
    """M4/M5 写的是 rewritten_query；哪天检索节点改回读原话，这两步就静默失效。"""
    for path in (LOCAL_NODE, HYDE_NODE):
        text = path.read_text(encoding="utf-8")
        assert 'state.get("rewritten_query")' in text, f"{path.name} 不再优先使用改写后的检索词"
