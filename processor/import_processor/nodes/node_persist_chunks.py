"""Persist imported document chunks and their Milvus vector identifiers in MySQL."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List

from sqlalchemy import delete

from backend.app.db.models import Document, DocumentChunk, ImportTask
from backend.app.db.session import SessionLocal
from config.milvus_config import milvus_config
from processor.import_processor.base import BaseNode
from processor.import_processor.exceptions import StorageError
from processor.import_processor.state import ImportGraphState
from utils.milvus_utils import get_milvus_client


class NodePersistChunks(BaseNode):
    """Atomically persist one document's chunks after Milvus has assigned vector IDs."""

    name: str = "node_persist_chunks"

    def __init__(
        self,
        config=None,
        session_factory: Callable[[], Any] = SessionLocal,
        milvus_client: Any | None = None,
        client_factory: Callable[[], Any] = get_milvus_client,
        collection_name: str | None = None,
        failure_reporter: Callable[[int, int, str], Awaitable[None]] | None = None,
    ):
        """Initialize dependencies, allowing database and Milvus clients to be mocked in tests."""
        super().__init__(config=config)
        self._session_factory = session_factory
        self._milvus_client = milvus_client
        self._client_factory = client_factory
        self._collection_name = collection_name or milvus_config.document_chunks_v2_collection
        self._failure_reporter = failure_reporter or self._mark_task_failed

    def process(self, state: ImportGraphState) -> ImportGraphState:
        """Persist chunks in MySQL and compensate Milvus if the database transaction fails."""
        task_id, library_id, document_id, chunks = self._validate_state(state)
        try:
            self._run_database_coroutine(
                state,
                lambda: self._persist_chunks(task_id, library_id, document_id, chunks),
            )
        except Exception as error:
            compensation_error = self._compensate_milvus(chunks)
            failure_message = self._build_failure_message(error, compensation_error)
            self._report_failure(state, task_id, document_id, failure_message)
            raise StorageError(
                message=f"Chunk MySQL 持久化失败，已执行 Milvus 补偿：{failure_message}",
                node_name=self.name,
                cause=error,
            ) from error

        state["current_step"] = self.name
        state["progress"] = 100
        self.logger.info(
            "Chunk persistence completed: import_task_id=%s document_id=%s chunks=%s",
            task_id,
            document_id,
            len(chunks),
        )
        return state

    @staticmethod
    def _run_database_coroutine(
        state: ImportGraphState,
        coroutine_factory: Callable[[], Awaitable[None]],
    ) -> None:
        """Run database work on the Worker's event loop when the graph runs in a thread."""
        database_event_loop = state.get("database_event_loop")
        if database_event_loop is not None and database_event_loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coroutine_factory(), database_event_loop)
            future.result()
            return
        asyncio.run(coroutine_factory())

    def _validate_state(self, state: Dict[str, Any]) -> tuple[int, int, int, List[Dict[str, Any]]]:
        """Validate task/document identity and ensure every chunk has a Milvus vector ID."""
        task_id = self._require_positive_int(state.get("import_task_id"), "import_task_id")
        library_id = self._require_positive_int(state.get("library_id"), "library_id")
        document_id = self._require_positive_int(state.get("document_id"), "document_id")
        chunks = state.get("chunks")
        if not isinstance(chunks, list) or not chunks:
            raise ValueError("MySQL 持久化需要至少一个 Chunk")

        chunk_indexes: set[int] = set()
        for fallback_index, chunk in enumerate(chunks):
            if not isinstance(chunk, dict):
                raise ValueError(f"第 {fallback_index} 个 Chunk 不是字典类型")
            if not str(chunk.get("content", "")).strip():
                raise ValueError(f"第 {fallback_index} 个 Chunk 缺少 content")
            if not str(chunk.get("vector_id", "")).strip():
                raise ValueError(f"第 {fallback_index} 个 Chunk 缺少 vector_id")
            chunk_index = self._require_non_negative_int(
                chunk.get("chunk_index", fallback_index), "chunk_index"
            )
            if chunk_index in chunk_indexes:
                raise ValueError(f"Chunk 索引重复：{chunk_index}")
            chunk_indexes.add(chunk_index)
        return task_id, library_id, document_id, chunks

    @staticmethod
    def _require_positive_int(value: Any, field_name: str) -> int:
        """Return a positive integer identifier or raise a validation error."""
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
        """Return a non-negative integer index or raise a validation error."""
        if isinstance(value, bool):
            raise ValueError(f"{field_name} 必须为非负整数")
        try:
            parsed_value = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{field_name} 必须为非负整数") from error
        if parsed_value < 0:
            raise ValueError(f"{field_name} 必须为非负整数")
        return parsed_value

    async def _persist_chunks(
        self,
        task_id: int,
        library_id: int,
        document_id: int,
        chunks: List[Dict[str, Any]],
    ) -> None:
        """Replace a document's Chunk rows and update the import task in one MySQL transaction."""
        async with self._session_factory() as session:
            async with session.begin():
                document = await session.get(Document, document_id)
                if document is None:
                    raise ValueError(f"文档不存在：document_id={document_id}")
                if document.library_id != library_id:
                    raise ValueError("文档不属于当前知识库")

                import_task = await session.get(ImportTask, task_id)
                if import_task is None:
                    raise ValueError(f"导入任务不存在：import_task_id={task_id}")
                if import_task.document_id != document_id or import_task.library_id != library_id:
                    raise ValueError("导入任务与当前知识库或文档不匹配")

                await session.execute(
                    delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
                )
                for fallback_index, chunk in enumerate(chunks):
                    session.add(self._build_document_chunk(document_id, chunk, fallback_index))

                import_task.chunk_count = len(chunks)
                import_task.status = "completed"
                import_task.current_step = self.name
                import_task.progress = 100
                import_task.error_message = None
                import_task.finished_at = datetime.now()
                document.status = "ready"
                document.parse_error = None

    @classmethod
    def _build_document_chunk(
        cls,
        document_id: int,
        chunk: Dict[str, Any],
        fallback_index: int,
    ) -> DocumentChunk:
        """Map graph Chunk fields to the stable MySQL DocumentChunk schema."""
        chunk_index = cls._require_non_negative_int(chunk.get("chunk_index", fallback_index), "chunk_index")
        source_page = chunk.get("source_page")
        page_number = cls._require_non_negative_int(source_page, "source_page") if source_page else None
        content = str(chunk["content"])
        return DocumentChunk(
            document_id=document_id,
            chunk_index=chunk_index,
            content=content,
            token_count=len(content),
            page_number=page_number,
            section_title=str(chunk.get("title", "")) or None,
            vector_id=str(chunk["vector_id"]),
            extra_metadata={
                "knowledge_base_id": chunk.get("knowledge_base_id"),
                "document_version": chunk.get("document_version"),
                "parent_title": str(chunk.get("parent_title", "")),
                "file_name": str(chunk.get("file_name", "")),
                "file_title": str(chunk.get("file_title", "")),
                "item_name": str(chunk.get("item_name", "")),
                "item_names": list(chunk.get("item_names", [])),
                "entity_recognition_mode": str(chunk.get("entity_recognition_mode", "")),
                "image_sources": list(chunk.get("image_sources", [])),
                "image_contexts": list(chunk.get("image_contexts", [])),
                "content_block_types": list(chunk.get("content_block_types", [])),
                "chunk_type": str(chunk.get("chunk_type", "")),
                "source_kind": str(chunk.get("source_kind", "")),
                "title_path": list(chunk.get("title_path", [])),
                "content_block_orders": list(chunk.get("content_block_orders", [])),
                "image_metadata": list(chunk.get("image_metadata", [])),
            },
        )

    def _compensate_milvus(self, chunks: List[Dict[str, Any]]) -> Exception | None:
        """Delete only vectors created by this failed import, preserving other documents' vectors."""
        vector_ids = self._extract_numeric_vector_ids(chunks)
        if not vector_ids:
            return None
        try:
            client = self._milvus_client or self._client_factory()
            if client is None:
                raise RuntimeError("Milvus 客户端不可用")
            identifiers = ", ".join(str(vector_id) for vector_id in vector_ids)
            client.delete(
                collection_name=self._collection_name,
                filter=f"chunk_id in [{identifiers}]",
            )
            self.logger.warning(
                "Milvus compensation completed: collection=%s vector_ids=%s",
                self._collection_name,
                vector_ids,
            )
            return None
        except Exception as error:
            self.logger.error("Milvus compensation failed: %s", error, exc_info=True)
            return error

    @staticmethod
    def _extract_numeric_vector_ids(chunks: List[Dict[str, Any]]) -> List[int]:
        """Return unique numeric Milvus primary keys from chunk vector IDs."""
        vector_ids: list[int] = []
        for chunk in chunks:
            try:
                vector_id = int(str(chunk.get("vector_id", "")))
            except (TypeError, ValueError):
                continue
            if vector_id not in vector_ids:
                vector_ids.append(vector_id)
        return vector_ids

    def _report_failure(
        self,
        state: ImportGraphState,
        task_id: int,
        document_id: int,
        message: str,
    ) -> None:
        """Best-effort update of task and document status after a failed persistence transaction."""
        try:
            self._run_database_coroutine(
                state,
                lambda: self._failure_reporter(task_id, document_id, message),
            )
        except Exception as error:
            self.logger.error("Unable to record import failure: %s", error, exc_info=True)

    async def _mark_task_failed(self, task_id: int, document_id: int, message: str) -> None:
        """Mark the import task and document as failed in an independent compensation transaction."""
        async with self._session_factory() as session:
            async with session.begin():
                import_task = await session.get(ImportTask, task_id)
                if import_task is not None:
                    import_task.status = "failed"
                    import_task.current_step = self.name
                    import_task.error_message = message[:65535]
                    import_task.finished_at = datetime.now()
                await session.execute(
                    delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
                )
                document = await session.get(Document, document_id)
                if document is not None:
                    document.status = "failed"
                    document.parse_error = message[:65535]

    @staticmethod
    def _build_failure_message(error: Exception, compensation_error: Exception | None) -> str:
        """Build an error message that explicitly states whether Milvus compensation succeeded."""
        if compensation_error is None:
            return f"MySQL 事务失败：{error}；Milvus 新向量已删除"
        return f"MySQL 事务失败：{error}；Milvus 补偿失败：{compensation_error}"
