"""检索来源整理节点。"""

import math
from typing import Any

from processor.query_processor.base import NodeBase
from processor.query_processor.state import QueryGraphState

MIN_SOURCE_CONTENT_LENGTH = 60
# 搜索引擎返回的摘要天然比知识库切片短，沿用 60 字阈值会把联网来源全部静默丢掉
MIN_WEB_CONTENT_LENGTH = 20
# 联网来源用绝对阈值：外部内容宁缺毋滥，不够相关就不该进入产品答案
MIN_RERANK_SCORE = 0.5
# 本地来源不能用同一个绝对线：实测同一问题上，Cross-Encoder 给完整网页摘要 0.7+，
# 给真正对应的知识库切片却只有 0.44；沿用绝对阈值会让本地资料在源头就被判死、根本轮不到配额。
# 因此本地改按自己组内高低截断，只用地板分兜住“整组都不相关”的情况。
MIN_LOCAL_SCORE = 0.3
LOCAL_RELATIVE_SCORE_RATIO = 0.8
# 本地知识库至少占据的来源槽位比例。
# 即便过了阈值，两路共用同一份 top_k 顺位时排序靠前的联网结果仍会把本地全部挤出窗口；
# 本地资料才是这个产品的主来源，所以给它保底槽位（保底而非上限，本地不足时仍由联网补满）。
LOCAL_MIN_SLOT_RATIO = 0.6


class NodeSourceCollection(NodeBase):
    """将排序后的本地 Chunk 和联网摘要统一整理为用户端来源结构。"""

    name = "node_source_collection"

    def process(self, state: QueryGraphState) -> QueryGraphState:
        """按来源分桶筛选，各自套用自己的相关性阈值，再按“本地保底 + 联网补位”分配 top_k 槽位。

        先分桶再算阈值，是为了让两路各按自己的分数分布判定相关性：
        把本地和联网放在同一把绝对尺子下比，本质上是拿碎片去和完整文章比。
        """
        limit = max(int(state.get("top_k") or 5), 1)
        rerank_failed = bool(state.get("rerank_failed"))
        buckets: dict[str, list[dict[str, Any]]] = {"local": [], "web": []}
        for document in state.get("reranked_docs") or []:
            source = dict(document)
            source.setdefault("source", "local")
            source["score"] = self._number(source.get("score", source.get("vector_score")))
            if not self._has_meaningful_content(source):
                continue
            buckets["web" if source["source"] == "web" else "local"].append(source)

        local_candidates = self._keep_relevant_local(buckets["local"], rerank_failed)
        web_candidates = (
            buckets["web"] if rerank_failed
            else [item for item in buckets["web"] if self._meets_rerank_threshold(item["score"])]
        )

        local_quota = min(limit, math.ceil(limit * LOCAL_MIN_SLOT_RATIO))
        picked_local = local_candidates[:local_quota]
        picked_web = web_candidates[: limit - len(picked_local)]
        # 联网没填满让出的槽位时，本地继续补上：配额不能反过来减少总来源数
        picked_local += local_candidates[len(picked_local): len(picked_local) + (limit - len(picked_local) - len(picked_web))]

        sources = picked_local + picked_web
        sources.sort(key=lambda item: item["score"] or 0.0, reverse=True)
        state["sources"] = sources
        return state

    @classmethod
    def _keep_relevant_local(cls, candidates: list[dict[str, Any]], rerank_failed: bool) -> list[dict[str, Any]]:
        """本地相关性按组内相对高低判定；精排失败时已回退成向量分数，不能再卡阈值。"""
        scores = [item["score"] for item in candidates if item["score"] is not None]
        if rerank_failed or not scores:
            return candidates
        floor = max(MIN_LOCAL_SCORE, max(scores) * LOCAL_RELATIVE_SCORE_RATIO)
        return [item for item in candidates if item["score"] is not None and item["score"] >= floor]

    @staticmethod
    def _has_meaningful_content(source: dict[str, Any]) -> bool:
        """按来源类型判最短正文：本地切片要去掉无信息量碎片，联网摘要只去掉空串。"""
        content = str(source.get("content") or "").strip()
        threshold = MIN_WEB_CONTENT_LENGTH if source.get("source") == "web" else MIN_SOURCE_CONTENT_LENGTH
        return len(content) >= threshold

    @staticmethod
    def _meets_rerank_threshold(score: float | None) -> bool:
        return score is not None and score >= MIN_RERANK_SCORE

    @staticmethod
    def _number(value: object) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
