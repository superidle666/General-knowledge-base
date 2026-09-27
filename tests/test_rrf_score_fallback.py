"""RRF 分数保留与精排失败时的降级排序（批次二 P1-5）。

改前的两个隐性毛病：
1. `node_rrf` 算完融合分就在下一步被丢弃（`[doc for doc, _ in results]`），下游拿不到；
2. `node_rerank` 失败回退时用 `vector_score` 排序——它是 Milvus 的 distance，COSINE 集合
   落在 0~1、IP 集合无上界，混在一起比大小没有意义；而且它返回的是"召回序 + 分数"却没有
   排序，后面的断崖检测假定输入降序，非单调序列上"差距"会变成负数，截断位置面向未知。
"""

from typing import Any

import pytest

import processor.query_processor.nodes.node_rerank as rerank_node_module
from processor.query_processor.nodes.node_rerank import (
    RERANK_FALLBACK_WEB_SCORE,
    RERANK_MAX_TOPK,
    NodeRerank,
)
from processor.query_processor.nodes.node_rrf import NodeRrf


def rrf(rank: int, weight: float = 1.0, k: int = 60) -> float:
    """RRF 公式，测试里独立算一遍，避免和被测实现共用同一段代码。"""
    return weight / (k + rank)


def hit(chunk_id: str, content: str, vector_score: float) -> dict[str, Any]:
    return {"entity": {"chunk_id": chunk_id, "content": content, "vector_score": vector_score, "source": "local"}}


# --------------------------------------------------------------------------
# 一、RRF 输出必须带上分数与名次
# --------------------------------------------------------------------------

def test_rrf_output_carries_score_and_rank() -> None:
    state = {
        "embedding_chunks": [hit("c1", "A", 0.20), hit("c2", "B", 0.50), hit("c3", "C", 0.99)],
        "hyde_embedding_chunks": [hit("c4", "D", 0.30), hit("c1", "A", 0.10)],
    }
    chunks = NodeRrf()(state)["rrf_chunks"]

    # c1 两路都靠前，只按名次算就该第一；c3 的向量分最高（0.99）但只出现在一路第 3 名
    assert [doc["chunk_id"] for doc in chunks] == ["c1", "c4", "c2", "c3"]
    assert [doc["rrf_rank"] for doc in chunks] == [1, 2, 3, 4]
    assert chunks[0]["rrf_score"] == pytest.approx(rrf(1) + rrf(2), abs=1e-6)
    assert chunks[-1]["rrf_score"] == pytest.approx(rrf(3), abs=1e-6)


def test_rrf_scores_depend_only_on_ranks_not_vector_metrics() -> None:
    """同一名次在 COSINE 集合和 IP 集合里向量分天差地别，融合分必须一致。"""
    def run(vector_scores: list[float]) -> list[dict[str, Any]]:
        state = {
            "embedding_chunks": [
                hit(f"c{i}", f"内容{i}", score) for i, score in enumerate(vector_scores, start=1)
            ],
            "hyde_embedding_chunks": [],
        }
        return NodeRrf()(state)["rrf_chunks"]

    cosine = run([0.91, 0.72, 0.55])
    inner_product = run([12.5, 9.3, 4.1])
    cosine_scores = [doc["rrf_score"] for doc in cosine]
    ip_scores = [doc["rrf_score"] for doc in inner_product]

    assert cosine_scores == ip_scores
    assert cosine_scores == [pytest.approx(rrf(rank), abs=1e-6) for rank in (1, 2, 3)]
    # 向量分再大也不能抢名次：IP 集合的 12.5 不会比 COSINE 集合的 0.91 “更相关”
    assert [doc["chunk_id"] for doc in inner_product] == ["c1", "c2", "c3"]
    # 落盘前四舍五入到 6 位：融合分会随 state 进日志与上下文 JSON，17 位小数只是噪声
    assert all(score == round(score, 6) for score in cosine_scores)


def test_rrf_does_not_mutate_retrieval_docs() -> None:
    """写分数用副本：embedding_chunks 是检索节点的原地输出，混进融合字段会让排查时对不上。"""
    embedding = [hit("c1", "A", 0.9)]
    state = {"embedding_chunks": embedding, "hyde_embedding_chunks": []}
    chunks = NodeRrf()(state)["rrf_chunks"]

    assert "rrf_score" in chunks[0]
    assert "rrf_score" not in embedding[0]["entity"]


# --------------------------------------------------------------------------
# 二、精排失败时的降级排序
# --------------------------------------------------------------------------

@pytest.fixture
def rerank_down(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(query: str, contents: list[str]) -> list[float]:
        raise RuntimeError("精排服务不可用")

    monkeypatch.setattr(rerank_node_module, "rerank_documents", boom)


def test_fallback_ranks_by_normalized_rrf_score(rerank_down: None) -> None:
    state = {
        "original_query": "怎么配",
        "top_k": 5,
        "rrf_chunks": [
            {"chunk_id": "low", "content": "低", "rrf_score": 0.005, "vector_score": 0.8},
            {"chunk_id": "best", "content": "高", "rrf_score": 0.02, "vector_score": 0.3},
            {"chunk_id": "mid", "content": "中", "rrf_score": 0.01, "vector_score": 0.6},
        ],
        "web_search_docs": [],
    }
    docs = NodeRerank()(state)["reranked_docs"]

    assert [doc["chunk_id"] for doc in docs] == ["best", "mid", "low"]
    # 组内 max 归一：最强本地切片是 1.0，下游拿不到 0.02 这种看着像“全不相关”的融合原值
    assert [round(doc["score"], 4) for doc in docs] == [1.0, 0.5, 0.25]


def test_fallback_ignores_mixed_vector_metrics(rerank_down: None) -> None:
    """IP 集合的 12.5 不该压过 COSINE 集合的 0.9——降级排序只看向量分以外的东西。

    故意把向量分最高、融合分最低的文档放在召回序首位：无论“按向量分排”还是“不排”，
    结果都跟这里期望的不同，才能真拦住量纲混排。
    """
    state = {
        "original_query": "q",
        "top_k": 5,
        "rrf_chunks": [
            {"chunk_id": "ip", "content": "乙", "rrf_score": 0.01, "vector_score": 12.5},
            {"chunk_id": "cosine", "content": "甲", "rrf_score": 0.02, "vector_score": 0.9},
        ],
        "web_search_docs": [],
    }
    docs = NodeRerank()(state)["reranked_docs"]
    assert [doc["chunk_id"] for doc in docs] == ["cosine", "ip"]


def test_fallback_gives_web_a_median_score_and_keeps_search_order(rerank_down: None) -> None:
    state = {
        "original_query": "q",
        "top_k": 5,
        "rrf_chunks": [
            {"chunk_id": "strong", "content": "甲", "rrf_score": 0.02},
            {"chunk_id": "tail", "content": "乙", "rrf_score": 0.002},
        ],
        "web_search_docs": [
            {"snippet": "网页一" * 20, "title": "一", "url": "http://a/1"},
            {"snippet": "网页二" * 20, "title": "二", "url": "http://a/2"},
        ],
    }
    docs = NodeRerank()(state)["reranked_docs"]

    by_id = {doc["chunk_id"]: doc["score"] for doc in docs if doc["source"] == "local"}
    assert by_id["strong"] == pytest.approx(1.0)
    assert by_id["tail"] < RERANK_FALLBACK_WEB_SCORE  # 本地长尾让位给 web 中位分
    web_docs = [doc for doc in docs if doc["source"] == "web"]
    assert [doc["score"] for doc in web_docs] == [RERANK_FALLBACK_WEB_SCORE] * 2
    # 分数相同保持召回顺序，web 仍是搜索引擎给的那个序（sort 稳定性是这里的前提）
    assert [doc["url"] for doc in web_docs] == ["http://a/1", "http://a/2"]
    assert [(doc["source"], doc["chunk_id"]) for doc in docs] == [
        ("local", "strong"), ("web", None), ("web", None), ("local", "tail"),
    ]


def test_fallback_handles_docs_without_any_score(rerank_down: None) -> None:
    """既没 rrf_score 也没 vector_score 的文档要排在末尾，而不是把整次降级炸掉。"""
    state = {
        "original_query": "q",
        "top_k": 5,
        "rrf_chunks": [
            {"chunk_id": "no_score", "content": "无分"},
            {"chunk_id": "legacy", "content": "旧数据", "vector_score": 0.7},
            {"chunk_id": "rrf", "content": "有融合分", "rrf_score": 0.02},
        ],
        "web_search_docs": [],
    }
    docs = NodeRerank()(state)["reranked_docs"]

    assert [doc["chunk_id"] for doc in docs] == ["rrf", "legacy", "no_score"]
    assert docs[-1]["score"] is None


def test_fallback_keeps_vector_score_order_when_no_rrf_at_all(rerank_down: None) -> None:
    """整批都没有融合分时退回旧行为（按向量分），不比改前更差。"""
    ranked = NodeRerank._fallback_ranked_docs([
        {"chunk_id": "a", "source": "local", "vector_score": 0.4},
        {"chunk_id": "b", "source": "local", "vector_score": 0.8},
    ])
    assert [doc["chunk_id"] for doc in ranked] == ["b", "a"]


# --------------------------------------------------------------------------
# 三、降级路径不再套断崖阈值，正常路径仍然要
# --------------------------------------------------------------------------

CLIFF_SCORES = [1.0, 0.9, 0.85, 0.3, 0.29, 0.28]


def _docs_with_scores(scores: list[float]) -> list[dict[str, Any]]:
    # 两个分数序列故意一致：旧实现拿 vector_score 当降级分、新实现拿归一化 rrf_score，
    # 这样两条路上唯一差异就只剩“降级时还要不要跑断崖检测”。
    return [
        {"chunk_id": f"c{i}", "content": f"内容{i}", "rrf_score": score, "vector_score": score}
        for i, score in enumerate(scores, start=1)
    ]


def test_cliff_detection_is_skipped_when_rerank_failed(rerank_down: None) -> None:
    # 第 3、4 条之间差 0.55：按正常路径的阈值会被截到 3 条。
    # top_k 给到 10（>候选数），确保 6 条全活只可能来自"降级不套断崖"、而非被 top_k 封顶截走
    # （Phase3 决策1a 后降级出口也会按 clamp(top_k,..) 收窄，这里要让封顶不构成干扰变量）。
    state = {"original_query": "q", "top_k": 10, "rrf_chunks": _docs_with_scores(CLIFF_SCORES), "web_search_docs": []}
    result = NodeRerank()(state)

    assert result["rerank_failed"] is True
    assert len(result["reranked_docs"]) == len(CLIFF_SCORES)


def test_cliff_detection_still_applies_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_rerank(query: str, contents: list[str]) -> list[float]:
        return list(CLIFF_SCORES)

    monkeypatch.setattr(rerank_node_module, "rerank_documents", fake_rerank)
    state = {"original_query": "q", "top_k": 5, "rrf_chunks": _docs_with_scores(CLIFF_SCORES), "web_search_docs": []}
    result = NodeRerank()(state)

    assert result["rerank_failed"] is False
    assert len(result["reranked_docs"]) == 3  # 断崖生效，截在第 3 条之后


def test_fallback_output_window_never_exceeds_max_topk(rerank_down: None) -> None:
    state = {
        "original_query": "q",
        "top_k": 20,  # 候选上限 30，输出窗口仍应是 RERANK_MAX_TOPK
        "rrf_chunks": _docs_with_scores([0.03 - index * 0.0005 for index in range(25)]),
        "web_search_docs": [],
    }
    docs = NodeRerank()(state)["reranked_docs"]
    assert len(docs) == RERANK_MAX_TOPK
