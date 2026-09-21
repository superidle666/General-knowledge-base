"""按知识库隔离的本地 Milvus 混合检索节点。"""

from __future__ import annotations

from typing import Any

from config.milvus_config import milvus_config
from processor.query_processor.base import NodeBase
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
        field_names = self._get_collection_field_names(client, collection_name)
        if "knowledge_base_id" not in field_names:
            raise ValueError(f"检索集合缺少 knowledge_base_id 字段：{collection_name}")
        expression = f"knowledge_base_id == {library_id}"
        output_fields = [
            field for field in self.OUTPUT_FIELDS
            if field in field_names
        ]
        requests = create_hybrid_search_requests(
            dense_vector=dense_vector,
            sparse_vector=sparse_vector,
            expr=expression,
            limit=max(int(state.get("retrieval_top_k") or 20), 10),
        )
        result = hybrid_search(
            client=client,
            collection_name=collection_name,
            reqs=requests,
            ranker_weights=(0.8, 0.2),
            limit=max(int(state.get("retrieval_top_k") or 20), 10),
            output_fields=output_fields,
        )
        state["embedding_chunks"] = self._normalize_results(result)
        logger.info("知识库 %s 本地检索完成，命中 %s 个 Chunk", library_id, len(state["embedding_chunks"]))
        return state

    OUTPUT_FIELDS = (
        "chunk_id", "knowledge_base_id", "document_id", "document_version",
        "chunk_index", "content", "title", "parent_title", "file_name",
        "file_title", "source_page", "item_name", "entity_recognition_mode",
        "chunk_type", "source_kind", "image_sources", "image_metadata",
    )

    @staticmethod
    def _get_collection_field_names(client: Any, collection_name: str) -> set[str]:
        description = client.describe_collection(collection_name=collection_name)
        if isinstance(description, dict):
            schema = description.get("schema")
            fields = schema.get("fields", []) if isinstance(schema, dict) else description.get("fields", [])
        else:
            schema = getattr(description, "schema", None)
            fields = getattr(schema, "fields", None) or getattr(description, "fields", [])
        names: set[str] = set()
        for field in fields or []:
            name = field.get("name") if isinstance(field, dict) else getattr(field, "name", None)
            if name:
                names.add(str(name))
        return names

    @staticmethod
    def _normalize_results(result: Any) -> list[dict[str, Any]]:
        """兼容 MilvusClient 返回的 entity 嵌套和扁平两种结果格式。"""
        if not result:
            return []
        hits = result[0] if isinstance(result, list) and result and isinstance(result[0], list) else result
        normalized: list[dict[str, Any]] = []
        for hit in hits or []:
            if not isinstance(hit, dict):
                continue
            entity = hit.get("entity") if isinstance(hit.get("entity"), dict) else hit
            item = dict(entity)
            if "chunk_id" not in item and hit.get("id") is not None:
                item["chunk_id"] = hit["id"]
            if hit.get("distance") is not None:
                item["vector_score"] = float(hit["distance"])
            item.setdefault("source", "local")
            if item.get("chunk_id") is not None:
                normalized.append(item)
        return normalized
