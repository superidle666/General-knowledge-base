"""知识库实体覆盖接口的口径守卫。

实体识别只在导入期决定，事后改 mode 不追溯已导入文档；这个差异过去在界面上完全看不出来
（库 1 实测：mode=required，Entity 表 0 条，617 切片 item_name 全空）。接口把差值量化出来之后，
最可能的回归是"派生布尔算错"和"新接口没登记授权规则"，两者都能离线钉住，所以只测这两点。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.api.mysql import document_entity_names, library_entity_coverage
from backend.app.api.schemas import LibraryEntityCoverageOut
from backend.app.core.auth import _management_permission
from backend.app.core.permissions import REGISTERED_PERMISSION_CODES


class _FakeResult:
    """只实现被测代码用到的两个取值方法，避免为了单测拉起整个数据库层。"""

    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one(self) -> object:
        return self._value

    def all(self) -> object:
        return self._value


class _FakeSession:
    """按调用顺序返回预置结果，并记录查询次数用于证明没有逐行发 SQL。"""

    def __init__(self, *values: object) -> None:
        self._values = list(values)
        self.execute_count = 0

    async def execute(self, _statement: object) -> _FakeResult:
        value = self._values[self.execute_count]
        self.execute_count += 1
        return _FakeResult(value)


@pytest.mark.parametrize(
    ("mode", "ready", "covered", "expected_effective", "expected_backfill"),
    [
        # required 但存在覆盖差：对齐不起作用，必须提示重导入（库 1 现场）。
        ("required", 3, 0, False, True),
        # required 且全覆盖：正常生效，不需要任何提示。
        ("required", 3, 3, True, False),
        # 空库不该被算成"对齐可用"，否则提示会说谎。
        ("required", 0, 0, False, False),
        # 关闭方向：永远不存在补齐需求；已识别的实体记录会保留，可随时切回。
        ("disabled", 4, 1, False, False),
        ("disabled", 0, 0, False, False),
    ],
)
async def test_coverage_derives_effective_and_backfill(
    mode: str, ready: int, covered: int, expected_effective: bool, expected_backfill: bool
) -> None:
    session = _FakeSession(ready, covered)
    payload = await library_entity_coverage(session, SimpleNamespace(id=1, entity_recognition_mode=mode))
    assert payload["ready_document_count"] == ready
    assert payload["entity_document_count"] == covered
    assert payload["uncovered_document_count"] == max(0, ready - covered)
    assert payload["alignment_effective"] is expected_effective
    assert payload["backfill_needed"] is expected_backfill
    # 返回字典必须能被 response_model 接住：字段增删在这里失败，而不是等线上 500。
    LibraryEntityCoverageOut(**payload)
    # 两条聚合查询：统计成本不随文档数增长。
    assert session.execute_count == 2


async def test_coverage_returns_mode_so_frontend_need_not_guess() -> None:
    """响应自带当前模式，前端确认文案不必再依赖列表页缓存的旧值。"""
    session = _FakeSession(3, 1)
    payload = await library_entity_coverage(session, SimpleNamespace(id=7, entity_recognition_mode="required"))
    assert payload["library_id"] == 7
    assert payload["entity_recognition_mode"] == "required"


def test_entity_coverage_route_reuses_library_read() -> None:
    """新接口必须落在已有授权映射里，否则所有人（含 admin）都会 403。"""
    request = SimpleNamespace(
        url=SimpleNamespace(path="/api/v1/libraries/1/entity-coverage"),
        method="GET",
    )
    code = _management_permission(request)
    assert code == "library:read"
    assert code in REGISTERED_PERMISSION_CODES


async def test_document_entity_names_maps_in_single_query() -> None:
    """一次查询建映射；没有实体记录的文档不进字典，前端据此显示"待重导入"。"""
    session = _FakeSession([(83, "一加 Ace 6T"), (80, "华为平板 C7")])
    mapping = await document_entity_names(session, [83, 80, 86])
    assert mapping == {83: "一加 Ace 6T", 80: "华为平板 C7"}
    assert 86 not in mapping
    assert session.execute_count == 1


async def test_document_entity_names_skips_query_without_ids() -> None:
    """空列表直接返回，不给数据库发无意义的 IN () 查询。"""
    session = _FakeSession()
    assert await document_entity_names(session, []) == {}
    assert session.execute_count == 0
