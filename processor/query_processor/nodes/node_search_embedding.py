"""按知识库隔离的本地 Milvus 混合检索节点。

故障与零命中严格分开（置 embedding_search_failed + 留现场）；带实体过滤零召回时
会去掉实体过滤同参重查一次（entity_filter_relaxed），不把“过滤卡空”当成“库里没东西”。
"""

from __future__ import annotations

from config.milvus_config import milvus_config
from processor.query_processor.base import NodeBase
from processor.query_processor.retrieval_common import (
    HYBRID_RANKER_WEIGHTS,
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
        library_filter = build_library_filter(library_id)
        item_filter = build_item_name_filter(state.get("item_names"), field_names)
        expression = combine_filters(library_filter, item_filter)
        search_limit = max(int(state.get("retrieval_top_k") or 20), 10)
        state["entity_filter_relaxed"] = False

        chunks, error = self._search_once(
            client=client,
            collection_name=collection_name,
            dense_vector=dense_vector,
            sparse_vector=sparse_vector,
            expression=expression,
            search_limit=search_limit,
            field_names=field_names,
            library_id=library_id,
        )
        # R3：实体对齐命中了词表，却一条切片也没召回来——最常见的成因是存量切片的
        # item_name 与词表对不上（文档以 disabled 导入后库才切成 required，那批切片该列全空）。
        # 旧逻辑把这种“过滤卡空”一路空到底，体验比不开实体识别的库还差：宁可丢掉
        # 实体收窄，也不能没得可答。代价最多一次本地向量检索（百毫秒级），远小于一次大模型调用。
        if error is None and not chunks and item_filter is not None:
            chunks, relaxed_error = self._search_once(
                client=client,
                collection_name=collection_name,
                dense_vector=dense_vector,
                sparse_vector=sparse_vector,
                # 只拆实体过滤，库隔离必须原样保留
                expression=library_filter,
                search_limit=search_limit,
                field_names=field_names,
                library_id=library_id,
            )
            if relaxed_error is not None:
                # 重查自己出错也是故障：不能用“带过滤那一次成功了”把它洗成零命中
                error = relaxed_error
            elif chunks:
                state["entity_filter_relaxed"] = True
                logger.info(
                    "知识库 %s 带实体过滤零召回，已放宽为仅库隔离条件重查（命中 %s 个 Chunk）：%s",
                    library_id, len(chunks), item_filter,
                )
        if error is not None:
            # 检索故障 ≠ 零命中：失败标记已在 _search_once 里随现场日志一并留下
            state["embedding_search_failed"] = True
            state["embedding_chunks"] = []
            return state
        state["embedding_search_failed"] = False
        state["embedding_chunks"] = chunks
        logger.info("知识库 %s 本地检索完成，命中 %s 个 Chunk", library_id, len(chunks))
        return state

    def _search_once(
            self,
            *,
            client,
            collection_name: str,
            dense_vector: list,
            sparse_vector: dict,
            expression: str | None,
            search_limit: int,
            field_names: set[str],
            library_id: int,
    ) -> tuple[list, Exception | None]:
        """执行一次混合检索，返回 (归一化命中, 故障异常)。

        故障不在这里伪装成空结果：只记现场、原样把异常交给调用方，否则放宽重查
        会拿着一个假“零命中”再白跑一次。集合结构硬校验放在 try 里：缺关键字段是“检索
        故障”而不是“没查到”，走与连接失败同一条降级路。
        """
        requests = create_hybrid_search_requests(
            dense_vector=dense_vector,
            sparse_vector=sparse_vector,
            expr=expression,
            limit=search_limit,
        )
        try:
            output_fields = resolve_output_fields(field_names)
            result = hybrid_search(
                client=client,
                collection_name=collection_name,
                reqs=requests,
                ranker_weights=HYBRID_RANKER_WEIGHTS,
                limit=search_limit,
                output_fields=output_fields,
            )
        except Exception as error:
            # 本路降级为空召回（有多少资料答多少），但不能伪装成“没查到”：
            # Milvus 不可用/filter 解析失败必须留下可排查的现场并置失败标记。
            logger.error(
                "知识库 %s 本地混合检索故障，本路召回置空：collection=%s filter=%s dense_dim=%s 错误=%s",
                library_id, collection_name, expression, len(dense_vector), error, exc_info=True,
            )
            return [], error
        return normalize_hits(result), None
