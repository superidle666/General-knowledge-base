"""库级清单型元问题前置拒答闸（R16）的离线守卫。

为什么需要它：用户问"知识库里有哪些华为的产品"时，内容检索链路本质上答不了——库里每份
文档只描述自己，不存在"产品清单"这种切片，于是与该 query 最近的总是手册前言/法律声明段
（"本指南仅供参考，产品信息请以实物为准" + 官网链接），模型照着它答，用户看到的就是
"基于 5 个来源 + 一堆官方渠道"这种答非所问。

设想过的"把本库实体词表枚举给模型"方案要多花 token 再加一次生成调用，库一大成本就失控；
本闸按用户决策改成**纯规则前置短路**：命中清单型问法、且用户没点名任何可对齐实体 →
直接回固定话术，零检索零生成零外呼（也省掉后面那次实体抽取 LLM）。

两层守卫：
1. 行为：驱动 NodePrepareQuery，正例短路成固定话术，并断言实体对齐整段没跑；
2. 反护栏：提到任何实体形状/本库词表里的名字/带指代的问法一律照常走检索（宁查勿否），
   disabled 库与 required 空壳库（词表为空、无可对齐能力）整段不生效，保证零回归。
"""

from __future__ import annotations

import pytest
from graph_introspection import PROJECT_ROOT, typed_dict_keys

from processor.query_processor.inventory_gate import (
    INVENTORY_REJECT_REPLY,
    is_library_inventory_question,
)
from processor.query_processor.nodes import node_prepare_query as node_module
from processor.query_processor.nodes.node_prepare_query import NodePrepareQuery

QUERY_STATE = PROJECT_ROOT / "processor" / "query_processor" / "state.py"

# 现场库的样子：required 模式，两条华为实体（规范名按导入侧规则由文件名派生）
VOCABULARY = [
    {"name": "华为平板 C7 用户指南", "aliases": []},
    {"name": "华为擎云 B530 用户指南", "aliases": []},
]


def _state(
    query: str,
    mode: str = "required",
    vocabulary: object = VOCABULARY,
    history: list | None = None,
) -> dict:
    return {
        "original_query": query,
        "library_id": 7,
        "entity_recognition_mode": mode,
        "entity_vocabulary": vocabulary,
        "history": history or [],
    }


@pytest.fixture()
def node(monkeypatch):
    """离线节点：实体抽取 LLM 与闲聊 L2 兜底都打桩，测试只走正则 + 确定性规则。

    L2 统一打桩成"不是闲聊"：本文件关心的是清单闸，若让闲聊闸先短路就测不到它了。
    """
    prepared = NodePrepareQuery()
    monkeypatch.setattr(prepared, "_call_entity_llm", lambda text, history_text: [])
    monkeypatch.setattr(node_module, "classify_chitchat_llm", lambda text, history_text="": (None, ""))
    return prepared


# --------------------------------------------------------------------------
# 一、骨架判定本身（纯函数，零外呼）
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "query",
    [
        "当前知识库有哪些华为的产品",  # 用户实测原句
        "有哪些华为产品信息",  # 用户实测原句
        "知识库里都有哪些型号",
        "你们知识库都收录了哪些产品",
        "库里有哪些烫金机",
        "这个知识库的产品清单",
    ],
)
def test_inventory_skeleton_matches_list_questions(query: str) -> None:
    assert is_library_inventory_question(query) is True


@pytest.mark.parametrize(
    "query",
    [
        "华为平板 C7 怎么连接 wifi",
        "HAK-180 的烫印温度调多少",
        "知识库里有没有华为平板的说明书",  # "有没有"是存在性追问，不是要清单
        "这款产品的说明书目录在哪",  # 指代 + 问文档位置
        "星辰一号的故障代码表怎么看",
        "大语言模型是什么",
    ],
)
def test_inventory_skeleton_ignores_content_questions(query: str) -> None:
    assert is_library_inventory_question(query) is False


# --------------------------------------------------------------------------
# 二、行为：命中清单问法就零成本短路
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "query",
    [
        "当前知识库有哪些华为的产品",
        "有哪些华为产品信息",
        "知识库里都有哪些型号",
    ],
)
def test_inventory_question_is_refused_before_any_alignment(node, query: str) -> None:
    """拒答必须发生在实体对齐之前：省掉抽取 LLM + 全部检索 + 生成，这是本条的全部动机。"""
    called = {"align": False}
    node._align_entity = lambda state: called.__setitem__("align", True)

    state = node.process(_state(query))

    assert state["answer"] == INVENTORY_REJECT_REPLY
    assert state["intent"] == "inventory_refusal"
    assert state["item_names"] == []
    # J2：短路要产出空来源列表，而不是"根本没有 sources 这个键"
    assert state["sources"] == []
    assert called["align"] is False, "清单型元问题不该再花一次实体抽取调用"


def test_refusal_decision_reads_current_turn_text_only(node) -> None:
    """判定只能吃本轮原文（与 R2 的拒答护栏同口径）。

    改写串是为检索服务的，历史回填的内容一律不算"用户本轮在要清单"——否则上一轮问过
    "库里有哪些产品"之后，本轮的普通追问会被连环误拒。这里直接把改写桩成一句清单问法，
    正面验证判定没去看它（桩同时接管了抽取输入，所以不断言 item_names，只断言没被误拒）。
    """
    called = {"align": False}
    node._align_entity = lambda state: called.__setitem__("align", True)
    node._rewrite_query_with_history = lambda query, history: "当前知识库有哪些产品"

    state = node.process(
        _state("HAK-180 怎么设置温度", vocabulary=[{"name": "HAK-180", "aliases": []}])
    )

    assert state["rewritten_query"] == "当前知识库有哪些产品"  # 检索词仍走改写通道
    assert state["query_entity_text"] == "HAK-180 怎么设置温度"  # 对齐判定拿的是原文
    assert not state.get("answer")
    assert state.get("intent") != "inventory_refusal"
    assert called["align"] is True, "不该被清单闸短路，正常交给实体对齐与检索"


# --------------------------------------------------------------------------
# 三、反护栏：任何"用户给了可对齐线索"的问法都照常走检索（宁查勿否）
# --------------------------------------------------------------------------

def test_inventory_gate_releases_question_naming_a_library_entity(node) -> None:
    """点名了本库词表里的实体就不是清单问题：纯中文名正则抽不到形状，只能靠词表探测放走。"""
    state = node.process(
        _state("星辰一号有哪些型号", vocabulary=[{"name": "星辰一号", "aliases": []}])
    )

    assert not state.get("answer")
    assert state.get("intent") != "inventory_refusal"


def test_inventory_gate_releases_question_naming_any_model_shape(node) -> None:
    """提到任何像型号的东西（哪怕本库没有）也不拒答：误拒是用户可见硬错误，漏拒只是回到现状。

    选无连字符的 G740 而非 DBY-W09：后者会撞既有的"具体型号代码未收录"拒答（R2/R4 那条护栏），
    不是本闸要验证的东西。
    """
    state = node.process(_state("有哪些产品支持 G740"))

    assert not state.get("answer")
    assert state.get("intent") != "inventory_refusal"


def test_inventory_gate_releases_coreference_question(node) -> None:
    """"这款有哪些型号"问的是上下文里那个对象，交给改写 + 检索，不能当清单问题拒掉。"""
    state = node.process(_state("这款有哪些型号", history=[{"role": "user", "text": "华为平板 C7 怎么样"}]))

    assert not state.get("answer")
    assert state.get("intent") != "inventory_refusal"


def test_inventory_gate_never_touches_disabled_library(node) -> None:
    """disabled 库整段不生效：没有词表就没有"可对齐线索"护栏，启用只会误伤中文实体名。"""
    called = {"align": False}
    node._align_entity = lambda state: called.__setitem__("align", True)

    state = node.process(_state("当前知识库有哪些产品", mode="disabled"))

    assert not state.get("answer")
    assert called["align"] is False  # disabled 库本来就不对齐（零回归）


def test_inventory_gate_never_touches_empty_required_vocabulary(node) -> None:
    """required 但词表为空（切了模式未重导入的空壳库）同样不启用：与实体对齐可用性同口径。"""
    state = node.process(_state("当前知识库有哪些产品", vocabulary=[]))

    assert not state.get("answer")
    assert state.get("intent") != "inventory_refusal"


# --------------------------------------------------------------------------
# 四、静态守卫：新意图值必须在状态里注释清楚
# --------------------------------------------------------------------------

def test_refusal_state_keys_are_declared() -> None:
    """短路写入的键必须在 TypedDict 里声明，否则 LangGraph 静默丢弃、前端永远看不到。"""
    declared = typed_dict_keys(QUERY_STATE, "QueryGraphState")
    assert {"answer", "intent", "sources", "item_names", "query_entity_text"} <= declared
    assert INVENTORY_REJECT_REPLY.strip()
