"""Redis worker for asynchronous knowledge-base imports."""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if __package__ in {None, ""}:
    project_root = Path(__file__).resolve().parents[2]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

from backend.app.core.path_utils import slugify
from backend.app.db.models import Document, ImportTask, Library
from backend.app.db.session import SessionLocal
from config.redis_config import redis_config
from processor.import_processor.main_graph import KBImportWorkflow
from processor.import_processor.state import ImportGraphState
from tool.logger import configure_logging
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


# 领取任务时允许的状态。queued 是正常待办；processing 看着矛盾（不是已经有人在跑了吗），
# 但它对应一个真实场景：租约到期后 recover_expired_import_tasks 只会重入 Redis 队列，
# 没人把 MySQL 状态改回 queued——不认它，这类任务就永远卡在 processing 里没人再跑。
CLAIMABLE_TASK_STATUSES: tuple[str, ...] = ("queued", "processing")


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
                await self.process_job(task_id, attempt, redis_client=client)
        finally:
            if owns_client:
                await client.aclose()

    def _client(self, redis_client: Any | None = None) -> Any | None:
        """解析本次操作应使用的 Redis 客户端：显式传入 > 传递下来的运行期客户端 > 注入客户端。

        返回 None 时由 import_queue 自己兜底建连接（仅适用于单独调 process_job 的测试/
        脚本）；长驻 Worker 路径上必须拿到同一个客户端，否则入队/锁/租约/心跳/ack
        每次都要重连+AUTH，而 Redis 在虚拟机上、带密码，一个任务能堆出十几次握手。
        """
        return redis_client or self.redis_client

    def stop(self) -> None:
        """Request graceful shutdown after the current polling cycle."""
        self._stopping = True

    async def process_job(
        self,
        task_id: int,
        attempt: int = 0,
        *,
        redis_client: Any | None = None,
    ) -> None:
        """Load one MySQL task, run the graph, and retry or fail it on error.

        ``redis_client`` 是 ``run_forever`` 已建的长驻连接；不传则回退到构造时注入的
        客户端（独立测试/脚本路径），两者都没有时才由工具函数逐次新建。
        """
        client = self._client(redis_client)
        try:
            state = await self._mark_processing_and_build_state(task_id)
        except Exception as error:
            self.logger.error("导入任务领取失败：任务=%s，错误=%s", task_id, error, exc_info=True)
            await self._retry_or_fail(task_id, attempt, error, redis_client=client)
            return
        if state is None:
            await acknowledge_import_task(task_id, redis_client=client)
            return
        if await is_import_cancelled(task_id, redis_client=client):
            await self._mark_cancelled(task_id)
            await acknowledge_import_task(task_id, redis_client=client)
            return
        heartbeat = asyncio.create_task(self._lease_heartbeat(task_id, redis_client=client))
        final_state: dict[str, Any] | None = None
        try:
            loop = asyncio.get_running_loop()
            state["database_event_loop"] = loop
            final_state = await asyncio.to_thread(
                self._run_graph_with_progress, state, task_id, loop, client
            )
        except ImportTaskCancelled:
            await self._mark_cancelled(task_id)
            await acknowledge_import_task(task_id, redis_client=client)
        except Exception as error:
            self.logger.error("导入任务失败：任务=%s，错误=%s", task_id, error, exc_info=True)
            await self._retry_or_fail(task_id, attempt, error, redis_client=client)
        else:
            await acknowledge_import_task(task_id, redis_client=client)
            if self._is_needs_review(final_state):
                await self._mark_needs_review(task_id, final_state or {})
                self.logger.info("导入任务转入人工确认实体：任务=%s", task_id)
            else:
                # 解析产物不再删除：按知识库分层保留于 processed，便于排查图片描述/切片质量；
                # 文档删除时再由管理端级联清理。
                self.logger.info("导入任务完成：任务=%s，解析产物保留于 %s", task_id, state.get("file_dir"))
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _lease_heartbeat(self, task_id: int, *, redis_client: Any | None = None) -> None:
        """Refresh a task lease periodically while its graph runs."""
        client = self._client(redis_client)
        interval = max(1, redis_config.visibility_timeout_seconds // 3)
        while True:
            await asyncio.sleep(interval)
            await refresh_import_lease(task_id, redis_client=client)

    def _run_graph_with_progress(
        self,
        state: ImportGraphState,
        task_id: int,
        loop: asyncio.AbstractEventLoop,
        redis_client: Any | None = None,
    ) -> dict[str, Any]:
        """Run the synchronous graph stream and synchronously wait for progress callbacks."""
        workflow = self.workflow_factory()
        final_state: dict[str, Any] = dict(state)
        for event in workflow.run(state, stream=True):
            node_name = next(iter(event), "unknown") if isinstance(event, dict) else "unknown"
            if isinstance(event, dict) and event:
                node_output = next(iter(event.values()))
                if isinstance(node_output, dict):
                    final_state = node_output
            progress = self._progress_for_node(node_name)
            # 进度回调回到事件循环上执行，复用的是同一个长驻 Redis 客户端。
            callback = asyncio.run_coroutine_threadsafe(
                self._record_progress_and_check_cancel(
                    task_id, node_name, progress, redis_client=redis_client
                ),
                loop,
            )
            try:
                callback.result()
            except ImportTaskCancelled:
                raise
            except Exception as error:
                raise RuntimeError(f"Worker 进度更新失败：{error}") from error
        return final_state

    @staticmethod
    def _is_needs_review(final_state: dict[str, Any] | None) -> bool:
        """强制模式下图在实体步提前结束且未定主实体，即为待人工确认。"""
        if not isinstance(final_state, dict):
            return False
        return final_state.get("entity_recognition_mode") == "required" and final_state.get("entity_status") in {"uncertain", "none"}

    async def _record_progress_and_check_cancel(
        self,
        task_id: int,
        node_name: str,
        progress: int,
        *,
        redis_client: Any | None = None,
    ) -> None:
        """Persist the current node and stop before the next node if cancellation was requested."""
        if await is_import_cancelled(task_id, redis_client=self._client(redis_client)):
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

    async def _retry_or_fail(
        self,
        task_id: int,
        attempt: int,
        error: Exception,
        *,
        redis_client: Any | None = None,
    ) -> None:
        """Requeue a failed task until the retry limit, then mark it permanently failed."""
        client = self._client(redis_client)
        if attempt < redis_config.max_retries:
            await self._mark_retry(task_id, attempt + 1, error)
            await release_processing_import_task(task_id, redis_client=client)
            owns_client = client is None
            if owns_client:
                # 兜底：没注入也没传客户端时才临时建一条，用完即关。
                client = create_redis_client()
            try:
                await enqueue_import_task(task_id, redis_client=client, attempt=attempt + 1)
            finally:
                if owns_client:
                    await client.aclose()
        else:
            await self._mark_failed(task_id, error)
            await acknowledge_import_task(task_id, redis_client=client)

    async def _mark_processing_and_build_state(self, task_id: int) -> ImportGraphState | None:
        """原子领取一个待处理任务并从 MySQL 组装图输入。

        领取用一条带状态条件的 UPDATE 完成，而不是“读—判—写”：两个 Worker 同时拿到同一个
        task_id 时，只有一条 UPDATE 能把它从 queued 改成 processing，另一条 rowcount 为 0，
        看到 0 就直接放弃。旧写法两边都能读到 queued，于是都认为自己领到了。
        progress / started_at 也在 SQL 侧算（CASE 与 COALESCE），避开“拿刚读到的旧值参与写”；
        用 CASE 而不是 GREATEST 是为了不把语句锁在某个方言上。

        这道防线盖的是“同时读到 queued”这个窗口；对于上面那个 processing 重入场景，两边看到
        的状态都合法、都能过条件检查，要做真互斥得给领取加围栏（fence：领取时写一个凭据，
        后续每步写入前先校验凭据还是不是自己的），不属本项范围。
        """
        now = datetime.now()
        async with self.session_factory() as session:
            async with session.begin():
                claimed = await session.execute(
                    update(ImportTask)
                    .where(
                        ImportTask.id == task_id,
                        ImportTask.status.in_(CLAIMABLE_TASK_STATUSES),
                    )
                    .values(
                        status="processing",
                        current_step="worker_claimed",
                        progress=case((ImportTask.progress < 1, 1), else_=ImportTask.progress),
                        started_at=func.coalesce(ImportTask.started_at, now),
                    )
                )
                if not claimed.rowcount:
                    # 区分“任务不存在”与“已被别的 Worker 或已终结”，前者警告后者常规跳过
                    existing = await session.get(ImportTask, task_id)
                    if existing is None:
                        self.logger.warning("Import task not found: task_id=%s", task_id)
                    else:
                        self.logger.info(
                            "Skip task with non-claimable status: task_id=%s status=%s", task_id, existing.status
                        )
                    return None
                task = await session.get(ImportTask, task_id)
                if task is None:  # 理论上不可达：上面的 UPDATE 已命中这一行
                    self.logger.warning("Import task vanished after claim: task_id=%s", task_id)
                    return None
                document = await session.get(Document, task.document_id)
                library = await session.get(Library, task.library_id)
                if document is None or library is None:
                    # 抛异常会回滚整个事务，连上面的领取一起撤销：任务留在 queued 等重试
                    raise ValueError(f"导入任务关联数据不存在：task_id={task_id}")
                document.status = "processing"
                return self._build_state(task, document, library)

    @staticmethod
    def _build_state(task: ImportTask, document: Document, library: Library) -> ImportGraphState:
        """Convert MySQL task records into the state expected by ``KBImportWorkflow``."""
        import_file_path = ImportWorker._resolve_project_path(document.storage_key)
        state: ImportGraphState = {
            "task_id": str(task.id),
            "import_task_id": task.id,
            "knowledge_base_id": library.id,
            "library_id": library.id,
            "document_id": document.id,
            "document_version": document.version,
            "entity_recognition_mode": library.entity_recognition_mode or "disabled",
            "config_snapshot": task.config_snapshot or {},
            "import_file_path": str(import_file_path),
            "file_dir": str(ImportWorker._processed_dir(library)),
            "file_title": document.title,
        }
        # 人工确认后重跑：把已确认主实体预置进 state，强制识别节点据此跳过模型。
        entity_result = task.entity_result or {}
        manual_name = str(entity_result.get("manual_name") or "").strip()
        if entity_result.get("confirmed") and manual_name:
            state["item_name"] = manual_name
            state["entity_status"] = "ok"
            state["entity_source"] = "manual"
            state["entity_candidates"] = list(entity_result.get("candidates") or [manual_name])
            state["entity_maybe_series"] = bool(entity_result.get("maybe_series"))
        return state

    @staticmethod
    def _processed_dir(library: Library) -> Path:
        """按知识库分层的解析工作目录：storage/processed/{库ID}-{库名slug}。

        两层设计让产物既可读又彼此隔离：
        - 库ID 前缀防同名库冲突、库改名后不致散落多目录；slug 走 slugify 清洗（去非法字符、
          折叠空白、截断），便于肉眼辨认归属，同时杜绝把库名里的路径分隔符拼进目录。
        - “每个上传文件一个独立文件夹”由 node_pdf_to_md 用 pdf_stem 建子目录实现（pdf_stem
          取自带 uuid 的 storage_key，天然唯一）：zip 落在本目录根，解压产物落在 {pdf_stem}/
          子目录，重试时节点自行清空同名子目录，互不干扰。
        解析产物在导入成功后不再删除，保留以便排查图片描述与切片质量；文档删除时由管理端级联清理。
        """
        folder = f"{library.id}-{slugify(library.name)}"
        return ImportWorker._resolve_project_path(f"storage/processed/{folder}")

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

    async def _mark_needs_review(self, task_id: int, final_state: dict[str, Any]) -> None:
        """强制识别未确定主实体时，把任务停在待确认态并保留候选供人工确认。

        采用方案 (i)：不做向量化/入库，仅落任务态与候选；确认后重新入队跑整条导入。
        """
        entity_result = {
            "status": str(final_state.get("entity_status") or "uncertain"),
            "guess": str(final_state.get("item_name") or ""),
            "candidates": [str(name) for name in (final_state.get("entity_candidates") or []) if str(name).strip()],
            "maybe_series": bool(final_state.get("entity_maybe_series")),
            "confirmed": False,
        }
        async with self.session_factory() as session:
            async with session.begin():
                task = await session.get(ImportTask, task_id)
                if task is None or task.status != "processing":
                    return
                task.status = "needs_review"
                task.current_step = "entity_review"
                task.progress = 60
                task.error_message = "强制实体识别未确定主实体，等待人工确认"
                task.entity_result = entity_result
                task.finished_at = None
                document = await session.get(Document, task.document_id)
                if document is not None:
                    document.status = "needs_review"

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
    # Worker 是一个独立进程，日志在这里显式配置；图节点内部的 import.* logger 全部向上冒泡。
    configure_logging()
    worker = ImportWorker()
    await worker.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
