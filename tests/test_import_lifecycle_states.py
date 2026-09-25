"""导入任务状态词表的一致性守卫。

ImportTask.status 在数据库里只是 String(20)，既没有枚举也没有 CHECK 约束；Worker、导入图节点和
后台 API 三处都在直接赋值字符串，全仓唯一的一份白名单是 api/mysql.py 的 IMPORT_TASK_STATUSES。
这类"多写入方 + 无约束"的组合出过一次实际故障：Worker 写了白名单里没有的状态，前端于是把任务
永远显示为处理中。这里在不连数据库的前提下，用 AST 交叉校验各写入方的状态词表。
"""

from __future__ import annotations

from graph_introspection import (
    API_MYSQL,
    IMPORT_MAIN_GRAPH,
    IMPORT_WORKER,
    PERSIST_CHUNKS,
    collection_literal,
    literal_assignments,
    registered_node_names,
)

# 变量名 -> 归属对象。导入任务在不同文件里叫 task 或 import_task，文档对象叫 document。
# 带上模型名是因为领取已改成一条条件 UPDATE（`update(ImportTask).values(status=...)`）：
# 写入落进 SQL 后只有模型名能认得出它写的是哪张表，不补上守卫就会当这个写入点不存在。
IMPORT_TASK_OWNERS = {"task", "import_task", "ImportTask"}
DOCUMENT_OWNERS = {"document", "Document"}

# 任务进入后不再被 Worker 重新领取的状态。
TERMINAL_TASK_STATUSES = {"completed", "failed", "cancelled"}
# 文档生命周期，需与 models.py 中 kb_document.status 的注释保持一致。
# needs_review 为强制实体识别未定主实体时的待人工确认态（方案 i：此时尚未向量化入库）。
DOCUMENT_STATUS_VOCABULARY = {"uploaded", "processing", "needs_review", "ready", "failed", "deleted"}

STATUS_SOURCES = [API_MYSQL, IMPORT_WORKER, PERSIST_CHUNKS]


def _written_task_statuses() -> dict[str, list[str]]:
    return literal_assignments(STATUS_SOURCES, "status", IMPORT_TASK_OWNERS)


def test_import_task_whitelist_covers_every_writer() -> None:
    """各写入方使用的导入任务状态都必须落在白名单内。"""
    whitelist = collection_literal(API_MYSQL, "IMPORT_TASK_STATUSES")
    unknown = {
        status: places
        for status, places in _written_task_statuses().items()
        if status not in whitelist
    }
    assert not unknown, "以下导入任务状态不在白名单内：" + "; ".join(
        f"{status} <- {places}" for status, places in sorted(unknown.items())
    )


def test_import_task_vocabulary_is_fully_exercised() -> None:
    """白名单里的每个状态都要有真实写入点；只出现在读侧说明有未实现或已废弃的状态。"""
    whitelist = collection_literal(API_MYSQL, "IMPORT_TASK_STATUSES")
    written = set(_written_task_statuses())
    assert written == whitelist, f"写入状态 {sorted(written)} 与白名单 {sorted(whitelist)} 不一致"


def test_every_terminal_status_has_a_writer() -> None:
    """成功、失败、取消三个终态都必须有写入点，缺一个就意味着某条路径永远不落终态。"""
    written = set(_written_task_statuses())
    not_written = TERMINAL_TASK_STATUSES - written
    assert not not_written, f"以下终态没有任何代码位置写入：{sorted(not_written)}"


def test_document_status_values_stay_in_vocabulary() -> None:
    """文档状态同样没有数据库约束，必须靠用例守住词表。"""
    written = literal_assignments(STATUS_SOURCES, "status", DOCUMENT_OWNERS)
    outside = {status: places for status, places in written.items() if status not in DOCUMENT_STATUS_VOCABULARY}
    assert not outside, "以下文档状态不在 kb_document 生命周期内：" + "; ".join(
        f"{status} <- {places}" for status, places in sorted(outside.items())
    )


def test_sql_side_status_write_counts_as_a_write_point() -> None:
    """领取写在实际落在 SQL 侧（update(ImportTask).values(status=...)），守卫必须认得它。

    这不是为了让用例通过而放宽断言：P1-6 把领取从“读—判—写”改成一条条件 UPDATE 后，
    `processing` 不再以 ORM 赋值的形式出现，只认属性赋值的守卫会误报“这个状态没人写”。
    owners 只给模型名可以把检测面限定在 SQL 写入这一路（属性赋值那一路要的是变量名）。
    """
    written = literal_assignments([IMPORT_WORKER], "status", {"ImportTask"})
    assert written.get("processing"), "领取任务的那条条件 UPDATE 没被识别成写入点"


def test_plain_values_call_is_not_mistaken_for_a_write(tmp_path) -> None:
    """只有链上真有 update/insert 才算写入，普通 `dict.values(...)` / `row.values(...)` 不能认。"""
    source = tmp_path / "not_a_writer.py"
    source.write_text('payload.values(status="processing")\nrow.values(status="ready")\n', encoding="utf-8")
    assert literal_assignments([source], "status", {"ImportTask", "Document"}) == {}


def test_worker_progress_map_covers_every_import_node() -> None:
    """新增导入节点若忘了登记进度百分比，前端会一直显示 1%，这里直接拦住。"""
    registered = registered_node_names(IMPORT_MAIN_GRAPH)
    mapped = collection_literal(IMPORT_WORKER, "progress_map")
    unmapped = registered - mapped
    assert not unmapped, f"以下导入节点缺少进度映射，会被显示为 1%：{sorted(unmapped)}"
