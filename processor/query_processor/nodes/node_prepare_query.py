"""查询参数校验与默认值准备节点。"""

import re

from processor.query_processor.base import NodeBase
from processor.query_processor.state import QueryGraphState


MODEL_PATTERN = re.compile(r"\b[A-Z][A-Z0-9]{1,}(?:-[A-Z0-9]+)+\b")
PRODUCT_PATTERN = re.compile(r"(?:华为|Huawei)[^，。！？\n]{0,24}(?:平板|电脑|笔记本|手机|设备)")
REFERENCE_PATTERN = re.compile(r"(?:这款|这个|该设备|该产品|它|上述(?:设备|产品))")


class NodePrepareQuery(NodeBase):
    """校验问题和知识库，并关闭高成本检索增强的默认开关。"""

    name = "node_prepare_query"

    def process(self, state: QueryGraphState) -> QueryGraphState:
        query = str(state.get("original_query") or "").strip()
        library_id = state.get("library_id")
        if not query:
            state["answer"] = "请输入要查询的问题。"
        elif not isinstance(library_id, int) or library_id <= 0:
            state["answer"] = "请选择有效的知识库后再进行查询。"
        state["history"] = list(state.get("history") or [])
        state["rewritten_query"] = self._rewrite_query_with_history(query, state["history"])
        state["item_names"] = list(state.get("item_names") or [])
        state["session_id"] = str(state.get("session_id") or "query-session")
        state["is_stream"] = bool(state.get("is_stream", False))
        state["use_hyde"] = bool(state.get("use_hyde", False))
        state["use_web_search"] = bool(state.get("use_web_search", False))
        state["retrieval_top_k"] = int(state.get("retrieval_top_k") or 20)
        state["top_k"] = int(state.get("top_k") or 5)
        return state

    @staticmethod
    def _rewrite_query_with_history(query: str, history: list[dict]) -> str:
        """将多轮对话中的最近型号或产品实体补充到当前检索问题。"""
        if not history or not REFERENCE_PATTERN.search(query):
            return query

        recent_text = "\n".join(
            str(message.get("text") or "")
            for message in reversed(history)
            if isinstance(message, dict)
        )
        entities: list[str] = []
        for pattern in (MODEL_PATTERN, PRODUCT_PATTERN):
            for match in pattern.findall(recent_text):
                value = match.strip()
                if value and value not in entities:
                    entities.append(value)
        if not entities:
            return query
        rewritten = f"{' '.join(entities[:3])} {query}".strip()
        return rewritten[:500]
