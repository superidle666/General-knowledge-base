"""Redis worker for asynchronous knowledge-base imports."""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if __package__ in {None, ""}:
    project_root = Path(__file__).resolve().parents[2]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

from backend.app.db.models import Document, ImportTask, Library
from backend.app.db.session import SessionLocal
from config.redis_config import redis_config
from processor.import_processor.main_graph import KBImportWorkflow
from processor.import_processor.state import ImportGraphState
from utils.import_queue import (
    acknowledge_import_task,
    create_redis_client,
    dequeue_import_task,
    enqueue_import_task,
    is_import_cancelled,
    recover_expired_import_tasks,
    release_processing_import_task,
    refresh_import_lease,
)


class ImportTaskCancelled(Exception):
    """Raised internally when cancellation is observed during graph execution."""


class ImportWorker:
    """Consume Redis import jobs and execute the existing import graph."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
        redis_client: Any | None = None,
        workflow_factory: Callable[[], KBImportWorkflow] = KBImportWorkflow,
        logger: logging.Logger | None = None,
    ) -> None:
        """Initialize the worker with injectable dependencies for integration tests."""
        self.session_factory = session_factory
        self.redis_client = redis_client
        self.workflow_factory = workflow_factory
        self.logger = logger or logging.getLogger("import.worker")
        self._stopping = False

    async def run_forever(self) -> None:
        """Continuously consume Redis jobs until ``stop`` is called or the process exits."""
        client = self.redis_client or create_redis_client(socket_timeout=10)
        owns_client = self.redis_client is None
        try:
            await client.ping()
            self.logger.info("Redis 导入 Worker 已连接：队列=%s", redis_config.queue_name)
            recovered = await recover_expired_import_tasks(redis_client=client)
            if recovered:
                self.logger.warning("已恢复过期导入任务：%s", recovered)
            stale = await self._recover_stale_database_tasks()
            if stale:
                self.logger.warning("已恢复 MySQL 中断的处理中任务：%s", stale)
            queued = await self._enqueue_queued_database_tasks(client)
            if queued:
                self.logger.info("已重新加入队列的数据库任务：%s", queued)
            while not self._stopping:
                job = await dequeue_import_task(redis_client=client, timeout=5)
                if job is None:
                    continue
                task_id, attempt = job
                await self.process_job(task_id, attempt)
        finally:
            if owns_client:
                await client.aclose()

    def stop(self) -> None:
        """Request graceful shutdown after the current polling cycle."""
        self._stopping = True

    async def process_job(self, task_id: int, attempt: int = 0) -> None:
        """Load one MySQL task, run the graph, and retry or fail it on error."""
        try:
            state = await self._mark_processing_and_build_state(task_id)
        except Exception as error:
            self.logger.error("导入任务领取失败：任务=%s，错误=%s", task_id, error, exc_info=True)
            await self._retry_or_fail(task_id, attempt, error)
            return
        if state is None:
            await acknowledge_import_task(task_id, redis_client=self.redis_client)
            return
        if await is_import_cancelled(task_id, redis_client=self.redis_client):
            await self._mark_cancelled(task_id)
            await acknowledge_import_task(task_id, redis_client=self.redis_client)
            return
        heartbeat = asyncio.create_task(self._lease_heartbeat(task_id))
        try:
            loop = asyncio.get_running_loop()
            state["database_event_loop"] = loop
            await asyncio.to_thread(self._run_graph_with_progress, state, task_id, loop)
        except ImportTaskCancelled:
            await self._mark_cancelled(task_id)
            await acknowledge_import_task(task_id, redis_client=self.redis_client)
        except Exception as error:
            self.logger.error("导入任务失败：任务=%s，错误=%s", task_id, error, exc_info=True)
            await self._retry_or_fail(task_id, attempt, error)
        else:
            await acknowledge_import_task(task_id, redis_client=self.redis_client)
            self.logger.info("导入任务完成：任务=%s", task_id)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _lease_heartbeat(self, task_id: int) -> None:
        """Refresh a task lease periodically while its graph runs."""
        interval = max(1, redis_config.visibility_timeout_seconds // 3)
        while True:
            await asyncio.sleep(interval)
            await refresh_import_lease(task_id, redis_client=self.redis_client)

    def _run_graph_with_progress(self, state: ImportGraphState, task_id: int, loop: asyncio.AbstractEventLoop) -> None:
        """Run the synchronous graph stream and synchronously wait for progress callbacks."""
        workflow = self.workflow_factory()
        for event in workflow.run(state, stream=True):
            node_name = next(iter(event), "unknown") if isinstance(event, dict) else "unknown"
            progress = self._progress_for_node(node_name)
            callback = asyncio.run_coroutine_threadsafe(
                self._record_progress_and_check_cancel(task_id, node_name, progress),
                loop,
            )
            try:
                callback.result()
            except ImportTaskCancelled:
                raise
            except Exception as error:
                raise RuntimeError(f"Worker 进度更新失败：{error}") from error

    async def _record_progress_and_check_cancel(self, task_id: int, node_name: str, progress: int) -> None:
        """Persist the current node and stop before the next node if cancellation was requested."""
        if await is_import_cancelled(task_id, redis_client=self.redis_client):
            raise ImportTaskCancelled(f"导入任务已取消：task_id={task_id}")
        async with self.session_factory() as session:
            async with session.begin():
                task = await session.get(ImportTask, task_id)
                if task is None or task.status == "cancelled":
                    raise ImportTaskCancelled(f"导入任务已取消：task_id={task_id}")
                task.current_step = node_name
                task.progress = progress

    @staticmethod
    def _progress_for_node(node_name: str) -> int:
        """Map graph nodes to monotonic user-facing progress percentages."""
        progress_map = {
            "node_entry": 5,
            "node_pdf_to_md": 20,
            "node_md_img": 35,
            "node_document_split": 50,
            "node_entity_recognition_disabled": 60,
            "node_entity_recognition_optional": 60,
            "node_entity_recognition_required": 60,
            "node_bge_embedding": 80,
            "node_import_milvus": 92,
            "node_persist_chunks": 100,
        }
        return progress_map.get(node_name, 1)

    async def _recover_stale_database_tasks(self) -> list[int]:
        """Reset old processing tasks left behind by a crashed Worker."""
        cutoff = datetime.now().timestamp() - redis_config.processing_timeout_seconds
        cutoff_datetime = datetime.fromtimestamp(cutoff)
        recovered: list[int] = []
        async with self.session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    select(ImportTask).where(
                        ImportTask.status == "processing",
                        ImportTask.started_at.is_not(None),
                        ImportTask.started_at < cutoff_datetime,
                    )
                )
                for task in result.scalars():
                    task.status = "queued"
                    task.current_step = "recovered_after_worker_restart"
                    task.progress = 0
                    task.error_message = "Worker 中断后自动恢复"
                    task.started_at = None
                    document = await session.get(Document, task.document_id)
                    if document is not None:
                        document.status = "uploaded"
                    recovered.append(int(task.id))
        return recovered

    async def _enqueue_queued_database_tasks(self, client: Any) -> list[int]:
        """Requeue durable MySQL tasks not present in Redis after a restart."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(ImportTask.id).where(ImportTask.status == "queued")
            )
            task_ids = [int(task_id) for task_id in result.scalars()]
        queued_ids: list[int] = []
        for task_id in task_ids:
            try:
                if await enqueue_import_task(task_id, redis_client=client):
                    queued_ids.append(task_id)
            except Exception as error:
                await self._mark_queue_pending(task_id, error)
        return queued_ids

    async def _mark_queue_pending(self, task_id: int, error: Exception) -> None:
        """Record a Redis enqueue failure while keeping the durable task queued for later recovery."""
        async with self.session_factory() as session:
            async with session.begin():
                task = await session.get(ImportTask, task_id)
                if task is not None and task.status == "queued":
                    task.current_step = "queue_pending"
                    task.error_message = f"Redis 入队失败：{error}"[:65535]

    async def _retry_or_fail(self, task_id: int, attempt: int, error: Exception) -> None:
        """Requeue a failed task until the retry limit, then mark it permanently failed."""
        if attempt < redis_config.max_retries:
            await self._mark_retry(task_id, attempt + 1, error)
            await release_processing_import_task(task_id, redis_client=self.redis_client)
            client = self.redis_client or create_redis_client()
            owns_client = self.redis_client is None
            try:
                await enqueue_import_task(task_id, redis_client=client, attempt=attempt + 1)
            finally:
                if owns_client:
                    await client.aclose()
        else:
            await self._mark_failed(task_id, error)
            await acknowledge_import_task(task_id, redis_client=self.redis_client)

    async def _mark_processing_and_build_state(self, task_id: int) -> ImportGraphState | None:
        """Atomically claim a queued task and build the graph input from MySQL records."""
        async with self.session_factory() as session:
            async with session.begin():
                task = await session.get(ImportTask, task_id)
                if task is None:
                    self.logger.warning("Import task not found: task_id=%s", task_id)
                    return None
                if task.status not in {"queued", "processing"}:
                    self.logger.info("Skip task with terminal status: task_id=%s status=%s", task_id, task.status)
                    return None
                document = await session.get(Document, task.document_id)
                library = await session.get(Library, task.library_id)
                if document is None or library is None:
                    raise ValueError(f"导入任务关联数据不存在：task_id={task_id}")
                task.status = "processing"
                task.current_step = "worker_claimed"
                task.progress = max(task.progress or 0, 1)
                task.started_at = task.started_at or datetime.now()
                document.status = "processing"
                return self._build_state(task, document, library)

    @staticmethod
    def _build_state(task: ImportTask, document: Document, library: Library) -> ImportGraphState:
        """Convert MySQL task records into the state expected by ``KBImportWorkflow``."""
        import_file_path = ImportWorker._resolve_project_path(document.storage_key)
        return {
            "task_id": str(task.id),
            "import_task_id": task.id,
            "knowledge_base_id": library.id,
            "library_id": library.id,
            "document_id": document.id,
            "document_version": document.version,
            "entity_recognition_mode": library.entity_recognition_mode or "disabled",
            "config_snapshot": task.config_snapshot or {},
            "import_file_path": str(import_file_path),
            "file_dir": str(ImportWorker._resolve_project_path("storage/processed")),
            "file_title": document.title,
        }

    @staticmethod
    def _resolve_project_path(value: str) -> Path:
        """Resolve a relative storage key against the repository root, not process CWD."""
        path = Path(value)
        if path.is_absolute():
            return path
        return Path(__file__).resolve().parents[2] / path

    async def _mark_retry(self, task_id: int, attempt: int, error: Exception) -> None:
        """Keep a retryable task queued and record the latest error."""
        async with self.session_factory() as session:
            async with session.begin():
                task = await session.get(ImportTask, task_id)
                if task is not None:
                    task.status = "queued"
                    task.current_step = f"retry_{attempt}"
                    task.error_message = str(error)[:65535]

    async def _mark_failed(self, task_id: int, error: Exception) -> None:
        """Mark a task and its document as permanently failed after retries are exhausted."""
        async with self.session_factory() as session:
            async with session.begin():
                task = await session.get(ImportTask, task_id)
                if task is None:
                    return
                task.status = "failed"
                task.current_step = "worker_failed"
                task.error_message = str(error)[:65535]
                task.finished_at = datetime.now()
                document = await session.get(Document, task.document_id)
                if document is not None:
                    document.status = "failed"
                    document.parse_error = str(error)[:65535]

    async def _mark_cancelled(self, task_id: int) -> None:
        """Mark a queued task and its document as cancelled before graph execution."""
        async with self.session_factory() as session:
            async with session.begin():
                task = await session.get(ImportTask, task_id)
                if task is None:
                    return
                task.status = "cancelled"
                task.current_step = "worker_cancelled"
                task.finished_at = datetime.now()
                document = await session.get(Document, task.document_id)
                if document is not None:
                    document.status = "uploaded"


async def main() -> None:
    """Start the Redis import worker process."""
    worker = ImportWorker()
    await worker.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
