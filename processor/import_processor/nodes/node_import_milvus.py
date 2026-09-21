"""Milvus v2 document chunk import node."""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Tuple

from pymilvus import DataType

from config.milvus_config import milvus_config
from processor.import_processor.base import BaseNode
from processor.import_processor.state import ImportGraphState
from utils.milvus_utils import get_milvus_client


class NodeImportMilvus(BaseNode):
    """Write embedded document chunks into the isolated Milvus v2 collection."""

    name: str = "node_import_milvus"

    def __init__(
        self,
        config=None,
        milvus_client: Any | None = None,
        client_factory: Callable[[], Any] = get_milvus_client,
        collection_name: str | None = None,
    ):
        """Initialize the node and allow a client to be injected for unit tests."""
        super().__init__(config=config)
        self._milvus_client = milvus_client
        self._client_factory = client_factory
        self._collection_name = collection_name or milvus_config.document_chunks_v2_collection

    def process(self, state: ImportGraphState) -> ImportGraphState:
        """Validate chunks, replace the current document's old vectors, then insert new vectors."""
        chunks, vector_dimension, identity = self._validate_input(state)
        client = self._prepare_collection(vector_dimension)
        self._delete_document_chunks(client, identity)
        state["chunks"] = self._insert_chunks(client, chunks, identity)
        return state

    def _validate_input(
        self, state: Dict[str, Any]
    ) -> Tuple[List[Dict[str, Any]], int, Tuple[int, int, int]]:
        """Validate required vectors and document metadata before Milvus is modified."""
        chunks = state.get("chunks")
        if not isinstance(chunks, list) or not chunks:
            raise ValueError("Milvus 入库需要至少一个切片")

        knowledge_base_id = self._require_positive_int(
            state.get("knowledge_base_id"), "knowledge_base_id"
        )
        document_id = self._require_positive_int(state.get("document_id"), "document_id")
        document_version = self._require_non_negative_int(
            state.get("document_version", 0), "document_version"
        )

        first_vector = chunks[0].get("dense_vector")
        if not isinstance(first_vector, list) or not first_vector:
            raise ValueError("第一个切片缺少有效 dense_vector")
        vector_dimension = len(first_vector)

        for index, chunk in enumerate(chunks):
            if not isinstance(chunk, dict):
                raise ValueError(f"第 {index} 个切片不是字典类型")
            dense_vector = chunk.get("dense_vector")
            if not isinstance(dense_vector, list) or len(dense_vector) != vector_dimension:
                raise ValueError(f"第 {index} 个切片的 dense_vector 维度不一致")
            if "sparse_vector" not in chunk:
                raise ValueError(f"第 {index} 个切片缺少 sparse_vector")
            if not str(chunk.get("content", "")).strip():
                raise ValueError(f"第 {index} 个切片缺少 content")

            chunk_knowledge_base_id = chunk.get("knowledge_base_id", knowledge_base_id)
            chunk_document_id = chunk.get("document_id", document_id)
            if chunk_knowledge_base_id != knowledge_base_id or chunk_document_id != document_id:
                raise ValueError("同一次入库的切片必须属于同一知识库和文档")

        self.logger.info(
            "Milvus v2 input validated: collection=%s chunks=%s dimension=%s knowledge_base_id=%s document_id=%s document_version=%s",
            self._collection_name,
            len(chunks),
            vector_dimension,
            knowledge_base_id,
            document_id,
            document_version,
        )
        return chunks, vector_dimension, (knowledge_base_id, document_id, document_version)

    @staticmethod
    def _require_positive_int(value: Any, field_name: str) -> int:
        """Return a positive integer identifier or raise a clear validation error."""
        if isinstance(value, bool):
            raise ValueError(f"{field_name} 必须为正整数")
        try:
            parsed_value = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{field_name} 必须为正整数") from error
        if parsed_value <= 0:
            raise ValueError(f"{field_name} 必须为正整数")
        return parsed_value

    @staticmethod
    def _require_non_negative_int(value: Any, field_name: str) -> int:
        """Return a non-negative integer version number or raise a validation error."""
        if isinstance(value, bool):
            raise ValueError(f"{field_name} 必须为非负整数")
        try:
            parsed_value = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{field_name} 必须为非负整数") from error
        if parsed_value < 0:
            raise ValueError(f"{field_name} 必须为非负整数")
        return parsed_value

    def _prepare_collection(self, vector_dimension: int) -> Any:
        """Get the client and create the v2 collection with fixed schema when absent."""
        if not self._collection_name:
            raise ValueError("未配置 DOCUMENT_CHUNKS_V2_COLLECTION 集合名称")
        client = self._milvus_client or self._client_factory()
        if client is None:
            raise ValueError("Milvus 客户端不可用")
        if not client.has_collection(collection_name=self._collection_name):
            self.logger.info("Creating Milvus v2 collection: %s", self._collection_name)
            self._create_collection(client, vector_dimension)
        return client

    def _create_collection(self, client: Any, vector_dimension: int) -> None:
        """Create the isolated document chunk collection and its dense/sparse indexes."""
        schema = client.create_schema(auto_id=True, enable_dynamic_fields=False)
        schema.add_field(field_name="chunk_id", datatype=DataType.INT64, is_primary=True, auto_id=True)
        schema.add_field(field_name="knowledge_base_id", datatype=DataType.INT64)
        schema.add_field(field_name="document_id", datatype=DataType.INT64)
        schema.add_field(field_name="document_version", datatype=DataType.INT64)
        schema.add_field(field_name="chunk_index", datatype=DataType.INT32)
        schema.add_field(field_name="content", datatype=DataType.VARCHAR, max_length=65535)
        schema.add_field(field_name="title", datatype=DataType.VARCHAR, max_length=8192)
        schema.add_field(field_name="parent_title", datatype=DataType.VARCHAR, max_length=8192)
        schema.add_field(field_name="file_name", datatype=DataType.VARCHAR, max_length=2048)
        schema.add_field(field_name="file_title", datatype=DataType.VARCHAR, max_length=8192)
        schema.add_field(field_name="source_page", datatype=DataType.INT32)
        schema.add_field(field_name="item_name", datatype=DataType.VARCHAR, max_length=8192)
        schema.add_field(field_name="entity_recognition_mode", datatype=DataType.VARCHAR, max_length=32)
        schema.add_field(field_name="chunk_type", datatype=DataType.VARCHAR, max_length=128)
        schema.add_field(field_name="source_kind", datatype=DataType.VARCHAR, max_length=64)
        schema.add_field(field_name="image_sources", datatype=DataType.VARCHAR, max_length=65535)
        schema.add_field(field_name="image_metadata", datatype=DataType.VARCHAR, max_length=65535)
        schema.add_field(field_name="sparse_vector", datatype=DataType.SPARSE_FLOAT_VECTOR)
        schema.add_field(field_name="dense_vector", datatype=DataType.FLOAT_VECTOR, dim=vector_dimension)

        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name="dense_vector",
            index_name="dense_vector_index",
            index_type="AUTOINDEX",
            metric_type="COSINE",
        )
        index_params.add_index(
            field_name="sparse_vector",
            index_name="sparse_vector_index",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="IP",
            params={"inverted_index_algo": "DAAT_MAXSCORE", "quantization": "none"},
        )
        client.create_collection(
            collection_name=self._collection_name,
            schema=schema,
            index_params=index_params,
        )
        self.logger.info("Milvus v2 collection created: %s", self._collection_name)

    def _delete_document_chunks(self, client: Any, identity: Tuple[int, int, int]) -> None:
        """Delete only the old chunks of the current knowledge-base document."""
        knowledge_base_id, document_id, _ = identity
        filter_expression = (
            f"knowledge_base_id == {knowledge_base_id} and document_id == {document_id}"
        )
        client.delete(collection_name=self._collection_name, filter=filter_expression)
        self.logger.info(
            "Milvus v2 document cleanup completed: knowledge_base_id=%s document_id=%s",
            knowledge_base_id,
            document_id,
        )

    def _insert_chunks(
        self,
        client: Any,
        chunks: List[Dict[str, Any]],
        identity: Tuple[int, int, int],
    ) -> List[Dict[str, Any]]:
        """Insert whitelisted v2 fields and copy generated vector IDs back to chunks."""
        collection_fields = self._get_collection_field_names(client)
        data_to_insert = [
            self._build_insert_row(chunk, index, identity)
            for index, chunk in enumerate(chunks)
        ]
        if collection_fields is not None:
            data_to_insert = [
                {key: value for key, value in row.items() if key in collection_fields}
                for row in data_to_insert
            ]
        insert_result = client.insert(collection_name=self._collection_name, data=data_to_insert)
        inserted_ids = self._extract_inserted_ids(insert_result)
        if len(inserted_ids) != len(chunks):
            raise ValueError(
                f"Milvus 返回主键数量异常：expected={len(chunks)} actual={len(inserted_ids)}"
            )

        updated_chunks = []
        for chunk, vector_id in zip(chunks, inserted_ids):
            updated_chunk = chunk.copy()
            updated_chunk["vector_id"] = str(vector_id)
            updated_chunk["chunk_id"] = vector_id
            updated_chunks.append(updated_chunk)
        self.logger.info("Milvus v2 insert completed: collection=%s chunks=%s", self._collection_name, len(chunks))
        return updated_chunks

    def _get_collection_field_names(self, client: Any) -> set[str] | None:
        """Read an existing collection schema so older v2 collections remain writable."""
        describe_collection = getattr(client, "describe_collection", None)
        if describe_collection is None:
            return None
        description = describe_collection(collection_name=self._collection_name)
        if isinstance(description, dict):
            schema = description.get("schema")
            fields = (
                schema.get("fields", [])
                if isinstance(schema, dict)
                else description.get("fields", [])
            )
        else:
            schema = getattr(description, "schema", None)
            fields = getattr(schema, "fields", None) or getattr(description, "fields", [])
        field_names: set[str] = set()
        for field in fields or []:
            if isinstance(field, dict):
                name = field.get("name") or field.get("field_name")
            else:
                name = getattr(field, "name", None) or getattr(field, "field_name", None)
            if name:
                field_names.add(str(name))
        return field_names or None

    @staticmethod
    def _build_insert_row(
        chunk: Dict[str, Any],
        fallback_chunk_index: int,
        identity: Tuple[int, int, int],
    ) -> Dict[str, Any]:
        """Build a schema-only Milvus row, including safe defaults for optional metadata."""
        knowledge_base_id, document_id, document_version = identity
        chunk_index = chunk.get("chunk_index", fallback_chunk_index)
        source_page = chunk.get("source_page") or 0
        return {
            "knowledge_base_id": knowledge_base_id,
            "document_id": document_id,
            "document_version": document_version,
            "chunk_index": int(chunk_index),
            "content": str(chunk.get("content", "")),
            "title": str(chunk.get("title", "")),
            "parent_title": str(chunk.get("parent_title", "")),
            "file_name": str(chunk.get("file_name", "")),
            "file_title": str(chunk.get("file_title", "")),
            "source_page": int(source_page),
            "item_name": str(chunk.get("item_name", "")),
            "entity_recognition_mode": str(chunk.get("entity_recognition_mode", "")),
            "chunk_type": str(chunk.get("chunk_type") or ",".join(chunk.get("content_block_types", []))),
            "source_kind": str(chunk.get("source_kind", "")),
            "image_sources": json.dumps(chunk.get("image_sources", []), ensure_ascii=False),
            "image_metadata": json.dumps(chunk.get("image_metadata", []), ensure_ascii=False),
            "sparse_vector": chunk["sparse_vector"],
            "dense_vector": chunk["dense_vector"],
        }

    @staticmethod
    def _extract_inserted_ids(insert_result: Any) -> List[Any]:
        """Read generated primary keys from the pymilvus insert response."""
        if isinstance(insert_result, dict):
            inserted_ids = insert_result.get("ids")
        else:
            inserted_ids = getattr(insert_result, "primary_keys", None)
            if inserted_ids is None:
                inserted_ids = getattr(insert_result, "ids", None)
        if inserted_ids is None:
            raise ValueError("Milvus 插入结果未返回 ids")
        if isinstance(inserted_ids, (str, bytes)):
            return [inserted_ids]
        try:
            return list(inserted_ids)
        except TypeError as error:
            raise ValueError("Milvus 插入结果 ids 格式无效") from error
