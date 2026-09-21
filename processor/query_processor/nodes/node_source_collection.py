"""检索来源整理节点。"""

from typing import Any

from processor.query_processor.base import NodeBase
from processor.query_processor.state import QueryGraphState

MIN_SOURCE_CONTENT_LENGTH = 60
MIN_RERANK_SCORE = 0.5


class NodeSourceCollection(NodeBase):
    """将排序后的本地 Chunk 统一整理为用户端来源结构。"""

    name = "node_source_collection"

    def process(self, state: QueryGraphState) -> QueryGraphState:
        limit = max(int(state.get("top_k") or 5), 1)
        sources: list[dict[str, Any]] = []
        rerank_failed = bool(state.get("rerank_failed"))
        for document in state.get("reranked_docs") or []:
            source = dict(document)
            source.setdefault("source", "local")
            source["score"] = self._number(source.get("score", source.get("vector_score")))
            if not self._has_meaningful_content(source):
                continue
            if not rerank_failed and not self._meets_rerank_threshold(source["score"]):
                continue
            sources.append(source)
            if len(sources) >= limit:
                break
        state["sources"] = sources
        return state

    @staticmethod
    def _has_meaningful_content(source: dict[str, Any]) -> bool:
        content = str(source.get("content") or "").strip()
        return len(content) >= MIN_SOURCE_CONTENT_LENGTH

    @staticmethod
    def _meets_rerank_threshold(score: float | None) -> bool:
        return score is not None and score >= MIN_RERANK_SCORE

    @staticmethod
    def _number(value: object) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
