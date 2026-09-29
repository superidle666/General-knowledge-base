"""文档重新处理入口的受理闸门守卫。

实体识别与切片参数都只在导入期生效，改配置不追溯历史文档，P2 补的是"按当前配置重跑"这一步。
重跑会把文档从 ready 拉回 uploaded 并覆盖本档切片与实体，所以受理前的判定必须钉死：
这些规则全都在"配置变更之后"才可能失效，而端点本身有写库副作用，离线只能测纯判定。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.api.mysql import reprocess_block_reason
from backend.app.core.auth import _management_permission
from backend.app.core.permissions import REGISTERED_PERMISSION_CODES


def _document(status: str = "ready", *, deleted_at=None, title: str = "使用手册", filename: str = "手册.pdf") -> SimpleNamespace:
    return SimpleNamespace(id=1, title=title, status=status, original_filename=filename, deleted_at=deleted_at)


def _library(
    *,
    mode: str = "disabled",
    status: str = "active",
    deleted_at=None,
    name: str = "工业设备手册",
    allowed_file_types: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=1, name=name, status=status, deleted_at=deleted_at,
        entity_recognition_mode=mode, allowed_file_types=allowed_file_types,
    )


def _reason(**kwargs: object) -> str | None:
    """按"一切正常"为基线，只覆盖被测的那一个条件。"""
    document = kwargs.pop("document", None) or _document()
    library = kwargs.pop("library", None) or _library()
    return reprocess_block_reason(
        document, library,
        is_system_admin=bool(kwargs.pop("is_system_admin", True)),
        has_active_task=bool(kwargs.pop("has_active_task", False)),
        source_file_exists=bool(kwargs.pop("source_file_exists", True)),
    )


def test_healthy_ready_document_is_accepted() -> None:
    """默认参数就该放行：闸门误严会把功能锁死。"""
    assert _reason() is None


def test_disabled_library_allows_normal_admin() -> None:
    """关闭实体识别的库不额外设闸，普通管理员也能重跑。"""
    assert _reason(is_system_admin=False, library=_library(mode="disabled")) is None


def test_required_library_stays_admin_only() -> None:
    """与上传同闸：required 库的重处理会重写实体，普通管理员不得触发。"""
    reason = _reason(is_system_admin=False, library=_library(mode="required"))
    assert reason is not None and "仅管理员" in reason
    assert _reason(is_system_admin=True, library=_library(mode="required")) is None


@pytest.mark.parametrize(
    ("kwargs", "keyword"),
    [
        ({"document": _document(status="processing")}, "不是已就绪状态"),
        ({"document": _document(status="failed")}, "不是已就绪状态"),
        ({"has_active_task": True}, "排队或处理中"),
        ({"source_file_exists": False}, "原始文件已不存在"),
        ({"document": _document(deleted_at="2026-09-01")}, "已删除"),
        ({"library": _library(status="disabled")}, "已删除或停用"),
        ({"library": _library(deleted_at="2026-09-01")}, "已删除或停用"),
        ({"document": _document(filename="手册.md"), "library": _library(allowed_file_types=["pdf"])}, "不在本库允许范围内"),
    ],
)
def test_blocked_cases_each_name_their_own_reason(kwargs: dict, keyword: str) -> None:
    reason = _reason(**kwargs)
    assert reason is not None, f"该场景本应拒绝受理：{kwargs}"
    assert keyword in reason


def test_allowed_file_type_spelling_variants_are_accepted() -> None:
    """配置里写 "PDF" 或 ".pdf" 都要认，与上传接口的归一化口径一致。"""
    document = _document(filename="手册.PDF")
    assert _reason(document=document, library=_library(allowed_file_types=["PDF"])) is None
    assert _reason(document=document, library=_library(allowed_file_types=[".pdf"])) is None


def test_size_limit_change_does_not_block_reprocess() -> None:
    """单文件上限调低不该把存量文档永久锁死在"不能重跑"里。"""
    library = _library()
    library.max_file_size_mb = 1
    assert _reason(document=_document(), library=library) is None


@pytest.mark.parametrize(
    ("path", "method"),
    [("/api/v1/documents/reprocess", "POST"), ("/api/v1/documents/7/reprocess", "POST")],
)
def test_reprocess_route_requires_document_modify(path: str, method: str) -> None:
    """新接口必须落在已有授权映射里，否则连 admin 都会 403。"""
    request = SimpleNamespace(url=SimpleNamespace(path=path), method=method)
    code = _management_permission(request)
    assert code == "document:modify"
    assert code in REGISTERED_PERMISSION_CODES
