"""P0-3：区分"检索零结果"与"检索故障"。

hybrid_search 曾在工具层吞掉一切异常返回 None，上层 normalize_hits 再转成空列表——
Milvus 连接失败、filter 解析错误、向量维度不匹配全部被伪装成"知识库没查到"，
用户看到无相关回答，运维毫无线索。这里锁住新的失败语义：
1 工具层异常原样上抛，不再吞掉返回 None；
2 本地主路检索节点捕获后置 embedding_search_failed 并降级为空召回；
3 真正的零命中不得置失败标记（两种语义不能互相吞并）；
4 HyDE 链路故障沿既有降级路径置 hyde_failed；
5 R3 的“带实体过滤零召回 → 去掉过滤重查”只在无故障时发生，重查自己出错仍算故障。
"""

from __future__ import annotations

import pytest

import processor.query_processor.nodes.node_search_embedding as local_module
import processor.query_processor.nodes.node_search_embedding_hyde as hyde_module
from graph_introspection import PROJECT_ROOT, typed_dict_keys
from processor.query_processor.nodes.node_search_embedding import NodeSearchEmbedding
from processor.query_processor.nodes.node_search_embedding_hyde import NodeSearchEmbeddingHyde
from utils import milvus_utils

QUERY_STATE = PROJECT_ROOT / "processor" / "query_processor" / "state.py"

# 集合需要出现的结构字段：库隔离列缺了节点会直接抛错
_FAKE_FIELDS = {"knowledge_base_id", "item_name", "chunk_id", "content"}


def _fake_client() -> _ClientStub:
    return _ClientStub()


class _ClientStub:
    def has_collection(self, collection_name: str) -> bool:
        return True


def _prepare_local_node(monkeypatch, hybrid_search_impl) -> NodeSearchEmbedding:
    """把本地检索节点的外部依赖换成桩，只保留故障注入点 hybrid_search。"""
    monkeypatch.setattr(local_module, "generate_embeddings", lambda texts: {
        "dense": [[0.1, 0.2, 0.3]],
        "sparse": [{7: 0.9}],
    })
    monkeypatch.setattr(local_module, "get_milvus_client", _fake_client)
    monkeypatch.setattr(local_module, "collection_field_names", lambda client, name: set(_FAKE_FIELDS))
    monkeypatch.setattr(local_module, "create_hybrid_search_requests", lambda **kwargs: [])
    monkeypatch.setattr(local_module, "hybrid_search", hybrid_search_impl)
    return NodeSearchEmbedding()


def _local_state() -> dict:
    return {"rewritten_query": "HAK180 怎么调节温度", "library_id": 3, "item_names": ["HAK180"]}


# --------------------------------------------------------------------------
# 一、工具层：故障必须上抛
# --------------------------------------------------------------------------

def test_hybrid_search_propagates_infrastructure_errors() -> None:
    """底层调用异常不得再被吞成 None——那是把故障伪装成零命中的源头。"""

    class _BrokenClient:
        def hybrid_search(self, **kwargs):
            raise ConnectionError("Milvus 不可达")

    with pytest.raises(ConnectionError):
        milvus_utils.hybrid_search(
            client=_BrokenClient(),
            collection_name="kb_v2",
            reqs=[],
            limit=5,
        )


def test_hybrid_search_returns_normal_empty_result() -> None:
    """真正的零命中是正常返回值，不该抛错——两种语义不能互相吞并。"""

    class _EmptyClient:
        def hybrid_search(self, **kwargs):
            return [[]]

    result = milvus_utils.hybrid_search(
        client=_EmptyClient(), collection_name="kb_v2", reqs=[], limit=5, output_fields=["chunk_id"],
    )
    assert result == [[]]


# --------------------------------------------------------------------------
# 二、本地主路：失败置标志并降级，零命中不误标
# --------------------------------------------------------------------------

def test_local_search_failure_sets_flag_and_degrades(monkeypatch) -> None:
    def broken_hybrid_search(**kwargs):
        raise ConnectionError("Milvus 不可达")

    node = _prepare_local_node(monkeypatch, broken_hybrid_search)
    state = node.process(_local_state())

    assert state["embedding_search_failed"] is True, "检索故障必须留下显式失败标记"
    assert state["embedding_chunks"] == [], "故障路降级为空召回，不阻断链路"


def test_local_search_zero_hits_is_not_marked_failed(monkeypatch) -> None:
    def empty_hybrid_search(**kwargs):
        return [[]]

    node = _prepare_local_node(monkeypatch, empty_hybrid_search)
    state = node.process(_local_state())

    assert state["embedding_search_failed"] is False, "零命中不是故障"
    assert state["embedding_chunks"] == []
    # R3 放宽后仍然零召回：只能说“这个库里没有相关内容”，不能伪装成“已经放宽过”
    assert state["entity_filter_relaxed"] is False


def test_local_search_success_clears_failure_flag(monkeypatch) -> None:
    def working_hybrid_search(**kwargs):
        return [[{"chunk_id": 1, "content": "操作面板位于...", "distance": 0.91}]]

    node = _prepare_local_node(monkeypatch, working_hybrid_search)
    state = node.process(_local_state())

    assert state["embedding_search_failed"] is False
    assert [item["chunk_id"] for item in state["embedding_chunks"]] == [1]


# --------------------------------------------------------------------------
# 三、R3：带实体过滤零召回时同参去掉过滤重查一次（存量切片 item_name 全空的兜底）
# --------------------------------------------------------------------------

def _node_recording_expressions(monkeypatch, hits_by_attempt) -> tuple[NodeSearchEmbedding, list[str]]:
    """本地检索节点的桩：记录每次检索用到的过滤表达式，按第几次调用决定返回什么。"""
    expressions: list[str] = []

    def record_requests(**kwargs):
        expressions.append(str(kwargs.get("expr") or ""))
        return []

    monkeypatch.setattr(local_module, "generate_embeddings", lambda texts: {
        "dense": [[0.1, 0.2, 0.3]],
        "sparse": [{7: 0.9}],
    })
    monkeypatch.setattr(local_module, "get_milvus_client", _fake_client)
    monkeypatch.setattr(local_module, "collection_field_names", lambda client, name: set(_FAKE_FIELDS))
    monkeypatch.setattr(local_module, "create_hybrid_search_requests", record_requests)
    monkeypatch.setattr(local_module, "hybrid_search", lambda **kwargs: hits_by_attempt(len(expressions)))
    return NodeSearchEmbedding(), expressions


def test_entity_filter_zero_recall_requeries_without_filter(monkeypatch) -> None:
    """文档以 disabled 导入后库切成 required：词表有实体但存量切片 item_name 全空，
    带过滤检索永远 0 条。旧逻辑一路空到底，比“禁用实体识别”的库还差。"""
    hit = {"chunk_id": 21, "content": "调温步骤……", "distance": 0.62}
    node, expressions = _node_recording_expressions(
        monkeypatch, lambda attempt: [[]] if attempt == 1 else [[hit]]
    )

    state = node.process(_local_state())

    assert len(expressions) == 2, "带过滤零召回后必须再查一次"
    assert "item_name" in expressions[0] and "item_name" not in expressions[1]
    assert "knowledge_base_id" in expressions[1], "放宽只放宽实体过滤，库隔离不能一起拆掉"
    assert state["entity_filter_relaxed"] is True
    assert state["embedding_search_failed"] is False, "放宽是兜底不是故障"
    assert [item["chunk_id"] for item in state["embedding_chunks"]] == [21]


def test_relaxed_requery_failure_is_not_disguised_as_zero_hits(monkeypatch) -> None:
    """§6-7：重查自己出错时必须置故障标记，不能把真故障重试成“没查到”。"""

    def fail_on_second_attempt(attempt: int) -> list:
        if attempt >= 2:
            raise ConnectionError("Milvus 不可达")
        return [[]]

    node, _ = _node_recording_expressions(monkeypatch, fail_on_second_attempt)

    state = node.process(_local_state())

    assert state["embedding_search_failed"] is True
    assert state["embedding_chunks"] == []
    assert state["entity_filter_relaxed"] is False, "放宽后故障不能留下“已拿到放宽结果”的假象"


def test_zero_recall_without_entity_filter_is_not_retried(monkeypatch) -> None:
    """没有实体过滤时不得白跑第二次检索：放宽只针对“过滤把结果卡空”这一种成因。"""
    node, expressions = _node_recording_expressions(monkeypatch, lambda attempt: [[]])
    state = {"rewritten_query": "怎么连接电源适配器", "library_id": 3, "item_names": []}

    result = node.process(state)

    assert len(expressions) == 1
    assert "item_name" not in expressions[0]
    assert result["entity_filter_relaxed"] is False
    assert result["embedding_search_failed"] is False


# --------------------------------------------------------------------------
# 四、HyDE 路：故障沿既有降级路径置 hyde_failed
# --------------------------------------------------------------------------

def test_hyde_search_failure_marks_hyde_failed(monkeypatch) -> None:
    """hybrid_search 不再吞异常后，HyDE 外层降级必须仍然接得住（置 hyde_failed 而非炸链路）。"""
    monkeypatch.setattr(hyde_module, "get_llm_client", lambda: SimpleLlm())
    monkeypatch.setattr(hyde_module, "generate_embeddings", lambda texts: {
        "dense": [[0.1, 0.2, 0.3]],
        "sparse": [{7: 0.9}],
    })
    monkeypatch.setattr(hyde_module, "get_milvus_client", _fake_client)
    monkeypatch.setattr(hyde_module, "collection_field_names", lambda client, name: set(_FAKE_FIELDS))
    monkeypatch.setattr(hyde_module, "create_hybrid_search_requests", lambda **kwargs: [])

    def broken_hybrid_search(**kwargs):
        raise ConnectionError("Milvus 不可达")

    monkeypatch.setattr(hyde_module, "hybrid_search", broken_hybrid_search)

    result = NodeSearchEmbeddingHyde().process({"rewritten_query": "怎么调温", "library_id": 3})
    assert result["hyde_failed"] is True
    assert result["hyde_embedding_chunks"] == []


class SimpleLlm:
    def invoke(self, prompt: str) -> object:
        return type("Response", (), {"content": "假设性答案"})()


# --------------------------------------------------------------------------
# 四、静态守卫：失败标记必须在状态里声明
# --------------------------------------------------------------------------

def test_embedding_search_failed_declared_in_state() -> None:
    """LangGraph 会静默丢弃未在状态里声明的键——失败标记丢了，日志和降级都看不见。"""
    declared = typed_dict_keys(QUERY_STATE, "QueryGraphState")
    assert {"embedding_search_failed", "hyde_failed", "rerank_failed", "web_search_failed"} <= declared
    # R3 的放宽标记同样要声明：丢了不会报错，只会让“放宽过”这件事在日志和前端彻底消失
    assert "entity_filter_relaxed" in declared
