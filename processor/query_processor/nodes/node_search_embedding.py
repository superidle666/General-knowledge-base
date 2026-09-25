"""按知识库隔离的本地 Milvus 混合检索节点。"""

from __future__ import annotations

from config.milvus_config import milvus_config
from processor.query_processor.base import NodeBase
from processor.query_processor.retrieval_common import (
    HYBRID_RANKER_WEIGHTS,
    LIBRARY_FILTER_FIELD,
    build_item_name_filter,
    build_library_filter,
    collection_field_names,
    combine_filters,
    normalize_hits,
    resolve_output_fields,
)
from processor.query_processor.state import QueryGraphState
from tool.logger import logger
from utils.embedding_utils import generate_embeddings
from utils.milvus_utils import create_hybrid_search_requests, get_milvus_client, hybrid_search


class NodeSearchEmbedding(NodeBase):
    """使用 BGE-M3 对当前知识库执行 Dense/Sparse 混合检索。"""

    name = "node_search_embedding"

    def process(self, state: QueryGraphState) -> QueryGraphState:
        query = str(state.get("rewritten_query") or state.get("original_query") or "").strip()
        library_id = state.get("library_id")
        if not query:
            state["embedding_chunks"] = []
            return state
        if not isinstance(library_id, int) or library_id <= 0:
            raise ValueError("本地检索需要有效的 library_id")

        embeddings = generate_embeddings([query])
        dense_vector = embeddings["dense"][0]
        sparse_vector = embeddings["sparse"][0]
        collection_name = milvus_config.document_chunks_v2_collection
        client = get_milvus_client()
        if not client.has_collection(collection_name=collection_name):
            logger.warning("Milvus 检索集合不存在：%s", collection_name)
            state["embedding_chunks"] = []
            return state
        field_names = collection_field_names(client, collection_name)
        if LIBRARY_FILTER_FIELD not in field_names:
            raise ValueError(f"检索集合缺少 {LIBRARY_FILTER_FIELD} 字段：{collection_name}")
        expression = combine_filters(
            build_library_filter(library_id),
            build_item_name_filter(state.get("item_names"), field_names),
        )
        output_fields = resolve_output_fields(field_names)
        search_limit = max(int(state.get("retrieval_top_k") or 20), 10)
        requests = create_hybrid_search_requests(
            dense_vector=dense_vector,
            sparse_vector=sparse_vector,
            expr=expression,
            limit=search_limit,
        )
        try:
            result = hybrid_search(
                client=client,
                collection_name=collection_name,
                reqs=requests,
                ranker_weights=HYBRID_RANKER_WEIGHTS,
                limit=search_limit,
                output_fields=output_fields,
            )
        except Exception as error:
            # 检索故障 ≠ 零命中：Milvus 不可用/filter 解析失败必须留下可排查的现场并置
            # 失败标记。本路降级为空召回（有多少资料答多少），但不能伪装成“没查到”。
            state["embedding_search_failed"] = True
            state["embedding_chunks"] = []
            logger.error(
                "知识库 %s 本地混合检索故障，本路召回置空：collection=%s filter=%s dense_dim=%s 错误=%s",
                library_id, collection_name, expression, len(dense_vector), error, exc_info=True,
            )
            return state
        state["embedding_search_failed"] = False
        state["embedding_chunks"] = normalize_hits(result)
        logger.info("知识库 %s 本地检索完成，命中 %s 个 Chunk", library_id, len(state["embedding_chunks"]))
        return state
