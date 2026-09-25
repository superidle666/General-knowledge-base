"""Worker 复用共享 Redis 客户端（批次一 P1-1）。

原缺陷：run_forever 建了 client 却没往下传，process_job 及其心跳/进度/ack/release 全部
读 self.redis_client；生产入口 `ImportWorker()` 不注入客户端，于是每步都走 import_queue 的
"自建自关"兜底——Redis 在虚拟机上且带密码，一个任务能堆出十几次"连接-AUTH-关闭"握手。
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

import processor.import_processor.worker as worker_module
from processor.import_processor.worker import ImportWorker

TASK_ID = 7


class FakeRedisClient:
    """身份标记用的假客户端：Worker 既不该再新建，也不该关掉别人给的连接。"""

    def __init__(self) -> None:
        self.closed = False

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        self.closed = True


class FakeTask:
    def __init__(self) -> None:
        self.id = TASK_ID
        self.status = "processing"
        self.current_step = ""
        self.progress = 0
        self.error_message: str | None = None


class _NoopTx:
    async def __aenter__(self) -> "_NoopTx":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False


class FakeSession:
    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    def begin(self) -> _NoopTx:
        return _NoopTx()

    async def execute(self, _stmt: Any) -> Any:
        class _Empty:
            def scalars(self) -> Any:
                return iter(())

        return _Empty()

    async def get(self, _model: Any, _pk: Any) -> Any:
        return FakeTask()


class FakeWorkflow:
    """图只跑一个节点，够触发一次"进度 + 取消检查"回调。"""

    def run(self, state: dict[str, Any], stream: bool = True) -> Any:
        yield {"node_entry": {**state, "current_step": "node_entry"}}


@pytest.fixture
def queue_spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """拦住 Worker 用到的队列函数，记录每次调用实际拿到的客户端。"""
    calls: dict[str, list[Any]] = {
        "is_import_cancelled": [],
        "refresh_import_lease": [],
        "acknowledge_import_task": [],
        "release_processing_import_task": [],
        "enqueue_import_task": [],
        "dequeue_import_task": [],
        "recover_expired_import_tasks": [],
    }

    async def is_import_cancelled(task_id: int, *, redis_client: Any = None, queue_name: Any = None) -> bool:
        calls["is_import_cancelled"].append(redis_client)
        return False

    async def refresh_import_lease(task_id: int, *, redis_client: Any = None, queue_name: Any = None) -> None:
        calls["refresh_import_lease"].append(redis_client)

    async def acknowledge_import_task(task_id: int, *, redis_client: Any = None, queue_name: Any = None) -> None:
        calls["acknowledge_import_task"].append(redis_client)

    async def release_processing_import_task(task_id: int, *, redis_client: Any = None, queue_name: Any = None) -> None:
        calls["release_processing_import_task"].append(redis_client)

    async def enqueue_import_task(task_id: int, *, redis_client: Any = None, attempt: int = 0, queue_name: Any = None) -> bool:
        calls["enqueue_import_task"].append(redis_client)
        return True

    async def dequeue_import_task(*, redis_client: Any = None, timeout: int = 5, queue_name: Any = None) -> Any:
        calls["dequeue_import_task"].append(redis_client)
        return None

    async def recover_expired_import_tasks(*, redis_client: Any = None, queue_name: Any = None) -> list[Any]:
        calls["recover_expired_import_tasks"].append(redis_client)
        return []

    monkeypatch.setattr(worker_module, "is_import_cancelled", is_import_cancelled)
    monkeypatch.setattr(worker_module, "refresh_import_lease", refresh_import_lease)
    monkeypatch.setattr(worker_module, "acknowledge_import_task", acknowledge_import_task)
    monkeypatch.setattr(worker_module, "release_processing_import_task", release_processing_import_task)
    monkeypatch.setattr(worker_module, "enqueue_import_task", enqueue_import_task)
    monkeypatch.setattr(worker_module, "dequeue_import_task", dequeue_import_task)
    monkeypatch.setattr(worker_module, "recover_expired_import_tasks", recover_expired_import_tasks)
    return calls


@pytest.fixture
def client_factory_spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """拦住 create_redis_client，用来统计"又新建了几条连接"。"""
    stats: dict[str, int] = {"created": 0}

    def create_redis_client(*, socket_timeout: float | None = 5) -> FakeRedisClient:
        stats["created"] += 1
        return FakeRedisClient()

    monkeypatch.setattr(worker_module, "create_redis_client", create_redis_client)
    return stats


def make_worker() -> ImportWorker:
    return ImportWorker(session_factory=FakeSession, redis_client=None, workflow_factory=FakeWorkflow)


def stub_state_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    """跳过 MySQL 领取任务那一步，只保留本文件关心的队列逻辑。"""

    async def fake_build_state(self: ImportWorker, task_id: int) -> dict[str, Any]:
        return {"task_id": str(task_id), "current_step": "worker_claimed"}

    monkeypatch.setattr(ImportWorker, "_mark_processing_and_build_state", fake_build_state)


@pytest.mark.asyncio
async def test_process_job_reuses_the_client_passed_in(
    monkeypatch: pytest.MonkeyPatch,
    queue_spy: dict[str, list[Any]],
    client_factory_spy: dict[str, int],
) -> None:
    """传进来的客户端要一路用到底：不再新建连接，也不把它关掉。"""
    worker = make_worker()
    shared = FakeRedisClient()
    stub_state_builder(monkeypatch)

    await worker.process_job(TASK_ID, 0, redis_client=shared)

    used = queue_spy["is_import_cancelled"] + queue_spy["acknowledge_import_task"]
    assert used, "队列操作应当发生过"
    assert all(client is shared for client in used)
    assert client_factory_spy["created"] == 0
    assert shared.closed is False


@pytest.mark.asyncio
async def test_node_progress_callback_shares_the_connection(
    monkeypatch: pytest.MonkeyPatch,
    queue_spy: dict[str, list[Any]],
    client_factory_spy: dict[str, int],
) -> None:
    """每个节点一次的"取消检查 + 进度写入"是握手次数的大头。"""
    worker = make_worker()
    shared = FakeRedisClient()
    stub_state_builder(monkeypatch)

    await worker.process_job(TASK_ID, 0, redis_client=shared)

    # 进图前一次 + 一个节点的进度回调一次
    assert len(queue_spy["is_import_cancelled"]) == 2
    assert all(client is shared for client in queue_spy["is_import_cancelled"])
    assert client_factory_spy["created"] == 0


@pytest.mark.asyncio
async def test_lease_heartbeat_shares_the_connection(
    monkeypatch: pytest.MonkeyPatch,
    queue_spy: dict[str, list[Any]],
    client_factory_spy: dict[str, int],
) -> None:
    """心跳刷租约同样不许重连。"""
    worker = make_worker()
    shared = FakeRedisClient()
    real_sleep = asyncio.sleep
    first_wait_done = False

    async def fake_sleep(seconds: float) -> None:
        # 第一次等待立刻返回让心跳真的刷一次租约，之后回到真实等待由测试 cancel 打断
        nonlocal first_wait_done
        if not first_wait_done:
            first_wait_done = True
            return
        await real_sleep(seconds)

    monkeypatch.setattr(worker_module.asyncio, "sleep", fake_sleep)

    heartbeat = asyncio.create_task(worker._lease_heartbeat(TASK_ID, redis_client=shared))
    await real_sleep(0.05)
    heartbeat.cancel()
    await asyncio.gather(heartbeat, return_exceptions=True)

    assert first_wait_done, "心跳应当真的执行过一次租约刷新"
    assert queue_spy["refresh_import_lease"]
    assert all(client is shared for client in queue_spy["refresh_import_lease"])
    assert client_factory_spy["created"] == 0


@pytest.mark.asyncio
async def test_retry_path_reuses_the_shared_client(
    monkeypatch: pytest.MonkeyPatch,
    queue_spy: dict[str, list[Any]],
    client_factory_spy: dict[str, int],
) -> None:
    """失败重试要 release + enqueue 两次队列操作，也必须复用连接。"""
    worker = make_worker()
    shared = FakeRedisClient()

    async def fake_build_state(self: ImportWorker, task_id: int) -> dict[str, Any]:
        raise RuntimeError("图跑挂了")

    monkeypatch.setattr(ImportWorker, "_mark_processing_and_build_state", fake_build_state)

    await worker.process_job(TASK_ID, 0, redis_client=shared)

    assert queue_spy["release_processing_import_task"]
    assert queue_spy["enqueue_import_task"]
    assert all(client is shared for client in queue_spy["release_processing_import_task"])
    assert all(client is shared for client in queue_spy["enqueue_import_task"])
    assert client_factory_spy["created"] == 0
    assert shared.closed is False


@pytest.mark.asyncio
async def test_run_forever_processes_a_job_with_exactly_one_connection(
    monkeypatch: pytest.MonkeyPatch,
    queue_spy: dict[str, list[Any]],
    client_factory_spy: dict[str, int],
) -> None:
    """回归锁：整条"出队→处理→心跳→进度→ack"链路只允许有一条连接。

    修复前这里会失败——run_forever 自建了连接却没往下传，process_job 与各队列函数
    各自走兜底分支，created 会随节点数一路往上涨。
    """
    worker = make_worker()
    shared = FakeRedisClient()
    stub_state_builder(monkeypatch)

    jobs = [(TASK_ID, 0)]

    async def dequeue_job(*, redis_client: Any = None, timeout: int = 5, queue_name: Any = None) -> Any:
        queue_spy["dequeue_import_task"].append(redis_client)
        if jobs:
            return jobs.pop(0)
        worker.stop()
        return None

    monkeypatch.setattr(worker_module, "create_redis_client", lambda **_kwargs: shared)
    monkeypatch.setattr(worker_module, "dequeue_import_task", dequeue_job)

    await worker.run_forever()

    all_clients = [client for clients in queue_spy.values() for client in clients]
    assert all_clients, "队列操作应当发生过"
    assert all(client is shared for client in all_clients)
    assert shared.closed is True, "自建的连接退出时要关一次"


@pytest.mark.asyncio
async def test_run_forever_keeps_injected_client_open(
    monkeypatch: pytest.MonkeyPatch,
    queue_spy: dict[str, list[Any]],
    client_factory_spy: dict[str, int],
) -> None:
    """注入进来的客户端归调用方管：不自建、也不关。"""
    injected = FakeRedisClient()
    worker = ImportWorker(session_factory=FakeSession, redis_client=injected, workflow_factory=FakeWorkflow)

    async def dequeue_and_stop(*, redis_client: Any = None, timeout: int = 5, queue_name: Any = None) -> Any:
        queue_spy["dequeue_import_task"].append(redis_client)
        worker.stop()
        return None

    monkeypatch.setattr(worker_module, "dequeue_import_task", dequeue_and_stop)

    await worker.run_forever()

    assert client_factory_spy["created"] == 0
    assert injected.closed is False
    assert all(client is injected for client in queue_spy["dequeue_import_task"])


@pytest.mark.asyncio
async def test_process_job_without_any_client_keeps_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
    queue_spy: dict[str, list[Any]],
) -> None:
    """兜底保留：独立调 process_job（测试/手工脚本）时仍由各队列函数自建连接。"""
    worker = make_worker()
    stub_state_builder(monkeypatch)

    await worker.process_job(TASK_ID, 0)

    assert all(client is None for client in queue_spy["is_import_cancelled"])
    assert all(client is None for client in queue_spy["acknowledge_import_task"])


def test_worker_module_declares_no_unbound_client_reference() -> None:
    """Worker 里所有队列调用都必须显式带客户端参数，别再漏成读 self.redis_client。"""
    import ast
    import inspect

    source = Path(inspect.getfile(worker_module)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    queue_calls = {
        "is_import_cancelled",
        "acknowledge_import_task",
        "release_processing_import_task",
        "refresh_import_lease",
        "enqueue_import_task",
    }
    missing: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Name) or func.id not in queue_calls:
            continue
        keywords = {kw.arg for kw in node.keywords}
        if "redis_client" not in keywords:
            missing.append(f"{func.id} at line {node.lineno}")
    assert missing == [], f"以下队列调用未显式传 redis_client：{missing}"
