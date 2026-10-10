"""needs_review 短路路由与确认重跑预置的离线守卫（P3）。

方案 (i)：强制识别未定主实体时图在实体步提前结束（走 END），worker 据最终 state
把任务转 needs_review；确认接口预置已确认实体后重新入队，节点据此跳过模型。
本用例只验证这些纯决策函数，不连 LLM / MySQL / Milvus。
"""

from __future__ import annotations

from types import SimpleNamespace

from langgraph.constants import END

from processor.import_processor.main_graph import KBImportWorkflow
from processor.import_processor.worker import ImportWorker


def test_route_after_entity_required_sends_ok_forward_and_review_to_end() -> None:
    """ok 继续向量化；uncertain/none/缺省一律走 END，不进入向量化。"""
    assert KBImportWorkflow.route_after_entity_required({"entity_status": "ok"}) == "node_bge_embedding"
    assert KBImportWorkflow.route_after_entity_required({"entity_status": "uncertain"}) == END
    assert KBImportWorkflow.route_after_entity_required({"entity_status": "none"}) == END
    assert KBImportWorkflow.route_after_entity_required({}) == END


def test_is_needs_review_only_for_required_unresolved() -> None:
    """仅强制模式且 entity_status 未定时判为待确认；ok、禁用模式、空态都不算。"""
    assert ImportWorker._is_needs_review({"entity_recognition_mode": "required", "entity_status": "uncertain"}) is True
    assert ImportWorker._is_needs_review({"entity_recognition_mode": "required", "entity_status": "none"}) is True
    assert ImportWorker._is_needs_review({"entity_recognition_mode": "required", "entity_status": "ok"}) is False
    assert ImportWorker._is_needs_review({"entity_recognition_mode": "disabled", "entity_status": ""}) is False
    assert ImportWorker._is_needs_review(None) is False


def test_build_state_seeds_confirmed_manual_entity() -> None:
    """确认后重跑：已确认主实体被预置进 state，供节点跳过模型、以 manual 落库。"""
    task = SimpleNamespace(
        id=5,
        config_snapshot={},
        entity_result={"confirmed": True, "manual_name": "A 机型", "candidates": ["A 机型", "B 机型"], "maybe_series": True},
    )
    document = SimpleNamespace(id=9, version=1, title="多机型对比", storage_key="storage/uploads/x.pdf")
    # name 不能缺：_build_state 会算 _processed_dir(library)=f"{id}-{slugify(name)}"
    library = SimpleNamespace(id=3, name="工业设备手册", entity_recognition_mode="required")

    state = ImportWorker._build_state(task, document, library)
    assert state["item_name"] == "A 机型"
    assert state["entity_status"] == "ok"
    assert state["entity_source"] == "manual"
    assert state["entity_candidates"] == ["A 机型", "B 机型"]
    assert state["entity_maybe_series"] is True


def test_build_state_without_confirmation_does_not_seed_entity() -> None:
    """未确认（首次导入或 entity_result 为空）时不预置实体，节点走正常枚举。"""
    task = SimpleNamespace(id=5, config_snapshot={}, entity_result=None)
    document = SimpleNamespace(id=9, version=1, title="普通文档", storage_key="storage/uploads/y.pdf")
    library = SimpleNamespace(id=3, name="工业设备手册", entity_recognition_mode="required")

    state = ImportWorker._build_state(task, document, library)
    assert "item_name" not in state
    assert state.get("entity_status") is None
    assert state.get("entity_source") is None
