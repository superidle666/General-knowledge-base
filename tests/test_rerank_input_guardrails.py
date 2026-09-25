"""Rerank 入参与返回校验（批次二 P1-4）。

精排是整条问答链路里唯一"把所有候选一次性送给交叉编码器"的环节：
- 候选数不设上限 → RRF 融合后可能有几十条长切片全量送出去，首字延迟被顶高；
- 单条不截断 → 一条超长切片就能让整批请求超限失败；
- web 缺 snippet 会传 None → 同样可能让整次外部调用失败；
- 旧实现对返回结果只补 0 不校验 → 服务漏打分时，那些文档被扮成"相关性为 0"，
  看着像模型判的，实际是数据缺失，属于最难查的那种错。
"""

from types import SimpleNamespace
from typing import Any

import pytest

import processor.query_processor.nodes.node_rerank as rerank_node_module
from processor.query_processor.nodes.node_rerank import (
    RERANK_MAX_CONTENT_CHARS,
    RERANK_MAX_TOPK,
    NodeRerank,
)
import utils.reranker_http_utils as reranker_module
from utils.reranker_http_utils import RerankError, rerank_documents


def make_response(scores: list[float], indices: list[int] | None = None) -> dict:
    """构造一个 dashscope 风格的响应字典（同时支持 .get 取键）。"""
    targets = indices if indices is not None else list(range(len(scores)))
    return {
        "status_code": 200,
        "output": {
            "results": [
                {"index": index, "relevance_score": score}
                for index, score in zip(targets, scores)
            ]
        },
    }


@pytest.fixture
def fake_rerank_call(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """拦住 SDK 调用：记录入参，返回值由用例塞进 box。"""
    box: dict[str, Any] = {"recorded": [], "response": make_response([0.9, 0.8]), "error": None}

    def fake_call(**kwargs):
        box["recorded"].append(kwargs)
        if box["error"] is not None:
            raise box["error"]
        return box["response"]

    monkeypatch.setattr(reranker_module, "dashscope", SimpleNamespace(
        api_key="",
        TextReRank=SimpleNamespace(call=fake_call),
    ))
    return box


# --------------------------------------------------------------------------
# 一、返回校验：不完整的分数一律视为失败
# --------------------------------------------------------------------------

def test_scores_are_returned_in_input_order(fake_rerank_call) -> None:
    # 服务按任意顺序返回，都要还原成与入参一一对应
    fake_rerank_call["response"] = make_response([0.3, 0.9], indices=[1, 0])
    assert rerank_documents("q", ["a", "b"]) == [0.9, 0.3]


def test_empty_documents_skip_the_request_without_calling(fake_rerank_call) -> None:
    assert rerank_documents("q", []) == []
    assert fake_rerank_call["recorded"] == []


@pytest.mark.parametrize(
    "response, keyword",
    [
        ({"status_code": 200, "output": {"results": [{"index": 0, "relevance_score": 0.5}]}}, "条数与入参不符"),
        ({"status_code": 200, "output": {}}, "results 不是列表"),
        ({"status_code": 200}, "缺少 output"),
        ({"status_code": 400, "message": "bad request"}, "调用失败"),
    ],
)
def test_incomplete_or_error_response_raises(fake_rerank_call, response, keyword) -> None:
    fake_rerank_call["response"] = response
    with pytest.raises(RerankError) as error:
        rerank_documents("q", ["a", "b"])
    assert keyword in str(error.value)


def test_nan_and_infinite_scores_are_rejected(fake_rerank_call) -> None:
    for bad in (float("nan"), float("inf"), "NaN"):
        fake_rerank_call["response"] = make_response([bad, 0.8])
        with pytest.raises(RerankError):
            rerank_documents("q", ["a", "b"])


def test_out_of_range_and_duplicate_index_are_rejected(fake_rerank_call) -> None:
    fake_rerank_call["response"] = make_response([0.5, 0.6], indices=[0, 9])
    with pytest.raises(RerankError):
        rerank_documents("q", ["a", "b"])

    fake_rerank_call["response"] = make_response([0.5, 0.6], indices=[0, 0])
    with pytest.raises(RerankError):
        rerank_documents("q", ["a", "b"])


def test_non_numeric_score_raises_rerank_error_not_type_error(fake_rerank_call) -> None:
    fake_rerank_call["response"] = make_response([None, 0.6])  # type: list
    with pytest.raises(RerankError):
        rerank_documents("q", ["a", "b"])


def test_sdk_exception_is_wrapped_as_rerank_error(fake_rerank_call) -> None:
    fake_rerank_call["error"] = ConnectionError("网络中断")
    with pytest.raises(RerankError):
        rerank_documents("q", ["a", "b"])


# --------------------------------------------------------------------------
# 二、入参护栏：候选数、单条长度、None 占位
# --------------------------------------------------------------------------

def _docs(count: int, prefix: str, content: str = "内容") -> list[dict]:
    return [{"chunk_id": f"{prefix}{i}", "content": content} for i in range(count)]


@pytest.mark.parametrize(
    "top_k, expected",
    [
        (1, (RERANK_MAX_TOPK, 3)),      # 小 top_k 也不许削召回：候选不得少于断崖输出上限
        (5, (15, 5)),
        (20, (30, 10)),                 # 客户端放大 top_k 时由绝对上限兜住成本
        ("abc", (15, 5)),               # 非法值退回默认 5
        (None, (15, 5)),
    ],
)
def test_candidate_limits(top_k: Any, expected: tuple[int, int]) -> None:
    assert NodeRerank._candidate_limits({"top_k": top_k}) == expected


def test_only_capped_candidates_reach_the_reranker(monkeypatch: pytest.MonkeyPatch) -> None:
    node = NodeRerank()
    recorded: dict[str, Any] = {}

    def fake_rerank(query: str, contents: list[str]) -> list[float]:
        recorded["contents"] = contents
        return [0.9 - index * 0.01 for index in range(len(contents))]

    monkeypatch.setattr(rerank_node_module, "rerank_documents", fake_rerank)
    state = {
        "rewritten_query": "怎么配",
        "top_k": 5,
        "rrf_chunks": _docs(40, "local"),
        "web_search_docs": [{"snippet": f"片段{i}", "title": f"网页{i}", "url": f"http://x/{i}"} for i in range(6)],
    }
    node.process(state)

    sent = recorded["contents"]
    assert len(sent) == 15 + 5  # 本地 3*top_k、web 截到 top_k
    assert sent[:1] == ["内容"]
    assert sent[-5:] == [f"片段{i}" for i in range(5)]


def test_missing_snippet_becomes_empty_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    # web 结果没有 snippet：旧实现把 None 塞进批量请求，可能让整次调用失败
    node = NodeRerank()
    sent: dict[str, Any] = {}

    def fake_rerank(query: str, contents: list[str]) -> list[float]:
        sent["contents"] = contents
        return [0.5] * len(contents)

    monkeypatch.setattr(rerank_node_module, "rerank_documents", fake_rerank)
    state = {
        "original_query": "怎么配",
        "top_k": 3,
        "rrf_chunks": _docs(1, "local"),
        "web_search_docs": [{"snippet": None, "title": "无摘要页", "url": "http://x/1"}],
    }
    node.process(state)

    assert None not in sent["contents"]
    assert sent["contents"] == ["内容", ""]


def test_long_chunk_is_truncated_for_scoring_only(monkeypatch: pytest.MonkeyPatch) -> None:
    node = NodeRerank()
    long_content = "长" * (RERANK_MAX_CONTENT_CHARS + 500)
    sent: dict[str, Any] = {}

    def fake_rerank(query: str, contents: list[str]) -> list[float]:
        sent["contents"] = contents
        return [0.7] * len(contents)

    monkeypatch.setattr(rerank_node_module, "rerank_documents", fake_rerank)
    state = {"original_query": "q", "top_k": 3, "rrf_chunks": _docs(1, "local", content=long_content), "web_search_docs": []}
    result = node.process(state)

    assert len(sent["contents"][0]) == RERANK_MAX_CONTENT_CHARS
    # 截断只作用于送精排的副本：生成阶段还要用完整切片
    assert result["reranked_docs"][0]["content"] == long_content


def test_rerank_failure_degrades_to_recall_order(monkeypatch: pytest.MonkeyPatch) -> None:
    node = NodeRerank()

    def boom(query: str, contents: list[str]) -> list[float]:
        raise RerankError("服务返回条数与入参不符：文档 2 条、分数 1 条")

    monkeypatch.setattr(rerank_node_module, "rerank_documents", boom)
    local = _docs(2, "local")
    local[0]["vector_score"] = 0.81
    local[1]["vector_score"] = 0.44
    state = {"original_query": "q", "top_k": 3, "rrf_chunks": local, "web_search_docs": []}
    result = node.process(state)

    assert result["rerank_failed"] is True
    assert [doc["chunk_id"] for doc in result["reranked_docs"]] == ["local0", "local1"]  # 召回原序，不抛异常


def test_partial_scores_never_silently_become_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """服务只给了一半文档打分时必须整体降级，不能把缺分文档扮成"相关性 0"继续排。"""
    node = NodeRerank()
    monkeypatch.setattr(rerank_node_module, "rerank_documents", rerank_documents)
    monkeypatch.setattr(
        reranker_module,
        "dashscope",
        SimpleNamespace(
            api_key="",
            TextReRank=SimpleNamespace(call=lambda **_kwargs: {"status_code": 200, "output": {"results": [{"index": 0, "relevance_score": 0.9}]}}),
        ),
    )
    local = _docs(2, "local")
    local[0]["vector_score"] = 0.2
    local[1]["vector_score"] = 0.9
    state = {"original_query": "q", "top_k": 3, "rrf_chunks": local, "web_search_docs": []}
    result = node.process(state)

    assert result["rerank_failed"] is True
    scores = {doc["chunk_id"]: doc["score"] for doc in result["reranked_docs"]}
    assert scores == {"local0": 0.2, "local1": 0.9}  # 不被补成 0，也不冒充精排分数
