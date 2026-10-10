"""任务领取改条件更新（批次二 P1-6）。

原写法是"读—判—写"：`session.get` 拿到任务 → 看 status → 再给 ORM 对象赋值。两个 Worker
同时读到 queued 时，两边都认为自己领到了同一份导入。领取改为一条带状态条件的 UPDATE，
rowcount 为 0 就放弃；progress / started_at 也在 SQL 侧算，避免拿刚读到的旧值参与写。

这里分两层验证：假 session 锁"语句形状与调用顺序"，真 SQLite 跑同一条 UPDATE 锁
CASE / COALESCE 的实际语义（假对象证明不了 SQL 写得对不对）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import create_engine, insert
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Update

from backend.app.db.models import Base, Document, ImportTask, Library, User
from processor.import_processor.worker import CLAIMABLE_TASK_STATUSES, ImportWorker

TASK_ID = 41
DOCUMENT_ID = 7
LIBRARY_ID = 3


class Row:
    """假 ORM 行：把 Python 侧的属性写入都记下来，用来证明领取不再靠逐字段赋值。"""

    def __init__(self, **values: Any) -> None:
        object.__setattr__(self, "_values", dict(values))
        object.__setattr__(self, "written", [])

    def __getattr__(self, name: str) -> Any:
        values = object.__getattribute__(self, "_values")
        if name in values:
            return values[name]
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        self._values[name] = value
        self.written.append(name)


def task_row(status: str = "queued", **overrides: Any) -> Row:
    values: dict[str, Any] = dict(
        id=TASK_ID,
        status=status,
        current_step="",
        progress=0,
        started_at=None,
        document_id=DOCUMENT_ID,
        library_id=LIBRARY_ID,
        config_snapshot={},
        entity_result={},
    )
    values.update(overrides)
    return Row(**values)


def document_row() -> Row:
    return Row(
        id=DOCUMENT_ID,
        status="uploaded",
        storage_key="storage/uploads/41_人工确认.md",
        version=1,
        title="用户手册",
    )


def library_row() -> Row:
    return Row(id=LIBRARY_ID, name="手机库", entity_recognition_mode="disabled")


class _Tx:
    def __init__(self, session: "ClaimSession") -> None:
        self._session = session

    async def __aenter__(self) -> "_Tx":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is None:
            self._session.commits += 1
        else:
            self._session.rollbacks += 1
        return False


class ClaimSession:
    """记录 Worker 发出的语句；rowcount 可配置，用来模拟领取成功/被抢先。"""

    def __init__(self, *, rowcount: int, task: Any, document: Any, library: Any) -> None:
        self.rowcount = rowcount
        self.rows = {ImportTask: task, Document: document, Library: library}
        self.statements: list[Any] = []
        self.loaded: list[Any] = []
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self) -> "ClaimSession":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    def begin(self) -> _Tx:
        return _Tx(self)

    async def execute(self, statement: Any) -> Any:
        self.statements.append(statement)
        return type("Result", (), {"rowcount": self.rowcount})()

    async def get(self, model: Any, pk: Any) -> Any:
        self.loaded.append(model.__name__)
        return self.rows.get(model)


def make_worker(session: ClaimSession) -> ImportWorker:
    return ImportWorker(
        session_factory=lambda: session,
        redis_client=None,
        workflow_factory=object,  # 领取阶段不碰图
    )


# --------------------------------------------------------------------------
# 一、语句形状：领取必须是第一条语句，而且是一条条件 UPDATE
# --------------------------------------------------------------------------

async def run_claim(session: ClaimSession) -> Any:
    return await make_worker(session)._mark_processing_and_build_state(TASK_ID)


@pytest.mark.asyncio
async def test_claim_is_one_conditional_update_before_any_read() -> None:
    session = ClaimSession(rowcount=1, task=task_row(), document=document_row(), library=library_row())
    await run_claim(session)

    claim = session.statements[0]
    assert isinstance(claim, Update), "领取必须是 UPDATE，而不是先 SELECT 回来再判断"
    assert claim.table.name == "kb_import_task"
    sql = str(claim)
    where_part = sql.split("WHERE", 1)[1]
    assert "kb_import_task.status IN" in where_part, "状态白名单要进 WHERE，由数据库做原子判定"
    # 正常领取只回读一次（拿写完后的行），不存在“先读回来判一眼再写”
    assert session.loaded == ["ImportTask", "Document", "Library"]


@pytest.mark.parametrize("status", ["queued", "processing"])
@pytest.mark.asyncio
async def test_both_claimable_statuses_pass_the_filter(status: str) -> None:
    assert status in CLAIMABLE_TASK_STATUSES
    session = ClaimSession(rowcount=1, task=task_row(status=status), document=document_row(), library=library_row())
    state = await run_claim(session)
    assert state is not None


@pytest.mark.asyncio
async def test_terminal_status_is_not_claimable() -> None:
    for status in ("cancelled", "failed", "needs_review", "success", "completed"):
        assert status not in CLAIMABLE_TASK_STATUSES


# --------------------------------------------------------------------------
# 二、SQL 侧计算：progress / started_at 不拿"刚读到的旧值"参与写
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_progress_and_started_at_are_computed_in_sql() -> None:
    session = ClaimSession(rowcount=1, task=task_row(), document=document_row(), library=library_row())
    await run_claim(session)

    claim = session.statements[0]
    for dialect in (mysql.dialect(), sqlite.dialect()):
        sql = str(claim.compile(dialect=dialect)).upper()
        assert "CASE" in sql, "progress 用 CASE 而不是 GREATEST，避免锁死方言"
        assert "COALESCE" in sql, "started_at 只能用 SQL 侧的 COALESCE 保住首次领取时间"
    # 写的值不来自 Python 侧刚读到的对象：progress 得是一个 SQL 表达式而不是一个参数
    params = claim.compile().params
    assert params["status"] == "processing"
    assert "progress" not in params, "progress 又变成参数，就是回到了读—判—写"


@pytest.mark.asyncio
async def test_claim_does_not_assign_task_fields_in_python() -> None:
    task = task_row()
    session = ClaimSession(rowcount=1, task=task, document=document_row(), library=library_row())
    await run_claim(session)

    assert task.written == [], "再给 ORM 对象赋值就等于回到读—判—写"


@pytest.mark.asyncio
async def test_document_status_is_still_marked_processing() -> None:
    document = document_row()
    session = ClaimSession(rowcount=1, task=task_row(), document=document, library=library_row())
    await run_claim(session)

    assert document.written == ["status"]
    assert document.status == "processing"


# --------------------------------------------------------------------------
# 三、rowcount 为 0：说明被别的 Worker 抢先或已终结，直接放弃
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lost_claim_returns_none_and_leaves_document_untouched() -> None:
    """就是本项要堵的那个窗口：读到的是 queued，但 UPDATE 一行也没改。

    “一行也没改”意味着另一个 Worker 已经抢先把它改成了 processing。旧写法没有这个
    信号可读（它只看刚读到的 status），于是两边都会继续跑图；现在 rowcount 就是信号。
    """
    document = document_row()
    session = ClaimSession(
        rowcount=0, task=task_row(status="queued"), document=document, library=library_row()
    )
    assert await run_claim(session) is None
    assert document.written == []
    assert session.commits == 1 and session.rollbacks == 0


@pytest.mark.asyncio
async def test_missing_task_returns_none() -> None:
    session = ClaimSession(rowcount=0, task=None, document=document_row(), library=library_row())
    assert await run_claim(session) is None


@pytest.mark.asyncio
async def test_missing_relations_roll_back_the_claim() -> None:
    """关联数据缺失时抛错，事务回滚会把领取一起撤销，任务留在原状态等重试。"""
    session = ClaimSession(rowcount=1, task=task_row(), document=None, library=library_row())
    with pytest.raises(ValueError):
        await run_claim(session)
    assert session.rollbacks == 1 and session.commits == 0


@pytest.mark.asyncio
async def test_claim_builds_graph_state() -> None:
    session = ClaimSession(rowcount=1, task=task_row(), document=document_row(), library=library_row())
    state = await run_claim(session)

    assert state is not None
    assert state["task_id"] == str(TASK_ID)
    assert state["document_id"] == DOCUMENT_ID
    assert state["knowledge_base_id"] == LIBRARY_ID
    # file_dir 按知识库分层：storage/processed/{库ID}-{库名slug}（不再是 task 级）
    assert state["file_dir"].replace("\\", "/").endswith(f"storage/processed/{LIBRARY_ID}-手机库")


# --------------------------------------------------------------------------
# 四、把 Worker 生成的那条 UPDATE 拿到真库上跑，验 CASE / COALESCE 的语义
# --------------------------------------------------------------------------

@pytest.fixture
def sqlite_engine() -> Any:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine, tables=[User.__table__, Library.__table__, Document.__table__, ImportTask.__table__]
    )
    with engine.begin() as conn:
        conn.execute(insert(Library.__table__).values(id=LIBRARY_ID, code="net", name="网络库"))
        conn.execute(
            insert(Document.__table__).values(
                id=DOCUMENT_ID,
                library_id=LIBRARY_ID,
                title="用户手册",
                original_filename="41.md",
                storage_key="storage/uploads/41.md",
                status="uploaded",
            )
        )
    return engine


def capture_claim_statement(task_status: str = "queued") -> Any:
    """让 Worker 真的构造一次领取语句并抓下来。"""
    session = ClaimSession(
        rowcount=1, task=task_row(status=task_status), document=document_row(), library=library_row()
    )
    asyncio.run(run_claim(session))
    return session.statements[0]


def seed_task(engine: Any, *, status: str, progress: int, started_at: datetime | None) -> None:
    with engine.begin() as conn:
        conn.execute(ImportTask.__table__.delete())
        conn.execute(
            insert(ImportTask.__table__).values(
                id=TASK_ID,
                library_id=LIBRARY_ID,
                document_id=DOCUMENT_ID,
                status=status,
                current_step="",
                progress=progress,
                started_at=started_at,
            )
        )


def read_task(engine: Any) -> dict[str, Any]:
    with engine.begin() as conn:
        row = conn.execute(ImportTask.__table__.select().where(ImportTask.id == TASK_ID)).mappings().one()
    return dict(row)


def test_real_database_applies_the_claim(sqlite_engine: Any) -> None:
    seed_task(sqlite_engine, status="queued", progress=0, started_at=None)
    claim = capture_claim_statement()
    with Session(sqlite_engine) as session:
        assert session.execute(claim).rowcount == 1
        session.commit()

    after = read_task(sqlite_engine)
    assert after["status"] == "processing"
    assert after["current_step"] == "worker_claimed"
    assert after["progress"] == 1
    assert after["started_at"] is not None


def test_real_database_never_moves_progress_backwards(sqlite_engine: Any) -> None:
    """重试/僵尸恢复再次领取时，已推进的进度不能被抹回 1。"""
    seed_task(sqlite_engine, status="processing", progress=60, started_at=None)
    claim = capture_claim_statement()
    with Session(sqlite_engine) as session:
        assert session.execute(claim).rowcount == 1
        session.commit()
    assert read_task(sqlite_engine)["progress"] == 60


def test_real_database_keeps_the_first_started_at(sqlite_engine: Any) -> None:
    first_start = datetime(2026, 9, 1, 8, 0, 0)
    seed_task(sqlite_engine, status="processing", progress=60, started_at=first_start)
    claim = capture_claim_statement()
    with Session(sqlite_engine) as session:
        assert session.execute(claim).rowcount == 1
        session.commit()
    # 必须先确认“真的领到了”再比时间：否则一句什么也没改的 UPDATE 也能让这条用例绿
    assert read_task(sqlite_engine)["started_at"] == first_start


def test_real_database_refuses_a_cancelled_task(sqlite_engine: Any) -> None:
    seed_task(sqlite_engine, status="cancelled", progress=60, started_at=datetime(2026, 9, 1, 8, 0, 0))
    claim = capture_claim_statement()
    with Session(sqlite_engine) as session:
        assert session.execute(claim).rowcount == 0
        session.commit()

    after = read_task(sqlite_engine)
    assert after["status"] == "cancelled"
    assert after["current_step"] == ""  # 一条都没改，才是"没领到"
