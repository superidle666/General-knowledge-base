"""强制实体识别枚举与主实体落库的离线守卫（P2）。

不连 LLM、不连 MySQL：recognizer 全部注入，`_build_entity_row` / `_normalize_entity_name`
为纯函数，只校验"采样→枚举→主实体覆盖→状态/疑似系列→kb_entity 行"这条契约的形状。
"""

from __future__ import annotations

import pytest

from backend.app.db.models import Entity
from processor.import_processor.exceptions import ImportProcessError
from processor.import_processor.nodes.node_entity_recognition_required import (
    NodeEntityRecognitionRequired,
)
from processor.import_processor.nodes.node_persist_chunks import NodePersistChunks


def _state(chunks: list[dict[str, str]], *, title: str = "网关说明书") -> dict[str, object]:
    """构造带最小必填字段且已完成切片的导入状态。"""
    return {
        "task_id": "p2-guard",
        "file_title": title,
        "entity_recognition_mode": "required",
        "chunks": [dict(chunk) for chunk in chunks],
    }


def _run(recognizer, state: dict[str, object]) -> dict[str, object]:
    """以注入识别器的方式执行强制识别节点。"""
    return NodeEntityRecognitionRequired(recognizer=recognizer)(state)


def test_ok_covers_every_chunk_with_single_main_entity() -> None:
    """枚举成功时主实体覆盖全部切片，且只落一个 item_names 元素。"""
    state = _state([
        {"title": "简介", "content": "LA2608 网关"},
        {"title": "参数", "content": "LA2608 规格"},
        {"title": "附录", "content": "保修"},
    ])
    result = _run(lambda _title, _sample: {"main_entity": "H3C LA2608", "candidates": ["H3C LA2608"], "status": "ok"}, state)
    assert result["item_name"] == "H3C LA2608"
    assert result["item_names"] == ["H3C LA2608"]
    assert all(chunk["item_name"] == "H3C LA2608" for chunk in result["chunks"])
    assert result["entity_status"] == "ok"
    assert result["entity_maybe_series"] is False


def test_multi_candidate_marks_maybe_series_but_keeps_one_main() -> None:
    """多候选只打疑似系列标记，主实体仍唯一。"""
    state = _state([{"title": "简介", "content": "X100 与 X200"}])
    result = _run(
        lambda _title, _sample: {"main_entity": "X100", "candidates": ["X100", "X200"], "status": "ok"},
        state,
    )
    assert result["item_name"] == "X100"
    assert result["entity_maybe_series"] is True
    assert result["entity_candidates"] == ["X100", "X200"]


def test_plain_string_result_is_accepted_as_legacy() -> None:
    """注入识别器返回纯文本时按单实体兼容处理。"""
    state = _state([{"title": "简介", "content": "LA2608"}])
    result = _run(lambda _title, _sample: "H3C LA2608", state)
    assert result["item_name"] == "H3C LA2608"
    assert result["entity_status"] == "ok"


def test_filename_model_fallback_when_body_has_none() -> None:
    """正文无主实体但文件名含型号 token 时回退到文件名（去扩展名）。"""
    state = _state([{"title": "安装", "content": "通用说明"}], title="HAK-180 安装手册.pdf")
    result = _run(lambda _title, _sample: {"status": "none"}, state)
    assert result["item_name"] == "HAK-180 安装手册"
    assert result["entity_status"] == "ok"


def test_uncertain_returns_for_review_without_raising() -> None:
    """uncertain 不再抛错；保留候选供人工确认，不自动写 item_name（由条件边转 END）。"""
    state = _state([{"title": "对比", "content": "A 与 B 并列"}], title="多机型对比")
    result = _run(
        lambda _title, _sample: {"main_entity": "", "candidates": ["A 机型", "B 机型"], "status": "uncertain"},
        state,
    )
    assert result["entity_status"] == "uncertain"
    assert result["entity_maybe_series"] is True
    assert result.get("item_name", "") == ""


def test_empty_returns_for_review_without_raising() -> None:
    """无实体且文件名无型号先验时不抛错、不写臆测实体，交由 worker 转 needs_review。"""
    state = _state([{"title": "说明", "content": "无实体"}], title="未知资料")
    result = _run(lambda _title, _sample: "", state)
    assert result["entity_status"] == "none"
    assert result.get("item_name", "") == ""


def test_confirmed_manual_entity_skips_recognizer() -> None:
    """worker 预置已确认实体时，节点直接复用不再调用识别器。"""
    def boom(_title: str, _sample: list[dict[str, str]]) -> str:
        raise AssertionError("确认重跑不应调用识别器")

    state = _state([{"title": "对比", "content": "A 与 B"}], title="多机型对比")
    state["item_name"] = "A 机型"
    state["entity_status"] = "ok"
    state["entity_source"] = "manual"
    result = _run(boom, state)
    assert result["item_name"] == "A 机型"
    assert all(chunk["item_name"] == "A 机型" for chunk in result["chunks"])


def test_service_error_maps_to_import_process_error() -> None:
    """识别服务异常统一包成"服务调用失败"，交由 worker 重试。"""
    def boom(_title: str, _sample: list[dict[str, str]]) -> str:
        raise RuntimeError("识别服务不可用")

    state = _state([{"title": "说明", "content": "异常"}], title="异常资料")
    with pytest.raises(ImportProcessError) as exc:
        _run(boom, state)
    assert "强制实体识别服务调用失败" in str(exc.value)


def test_sample_chunks_includes_first_and_last_within_budget() -> None:
    """采样必须含首尾且不超过 k，保证一次枚举能覆盖整篇分布。"""
    chunks = [{"title": f"t{i}", "content": str(i)} for i in range(10)]
    sample = NodeEntityRecognitionRequired._sample_chunks(chunks, 5)
    assert sample[0] is chunks[0]
    assert sample[-1] is chunks[-1]
    assert len(sample) <= 5
    assert NodeEntityRecognitionRequired._sample_chunks(chunks, 5)[0] == chunks[0]


def test_build_entity_row_on_ok() -> None:
    """ok 状态构造 kb_entity 行：归一化名、别名去重排除主名、来源标记为 llm。"""
    state = {
        "entity_recognition_mode": "required",
        "entity_status": "ok",
        "item_name": "H3C LA2608",
        "entity_candidates": ["H3C LA2608", "LA2608", "LA2608", "LA2608-W2"],
        "entity_maybe_series": True,
    }
    row = NodePersistChunks._build_entity_row(state, library_id=7, document_id=9)
    assert isinstance(row, Entity)
    assert row.library_id == 7 and row.document_id == 9
    assert row.name == "H3C LA2608"
    assert row.normalized_name == "h3cla2608"
    assert row.aliases == ["LA2608", "LA2608-W2"]
    assert row.source == "llm"
    assert row.maybe_series is True


@pytest.mark.parametrize(
    "state",
    [
        {"entity_recognition_mode": "disabled", "entity_status": "ok", "item_name": "X"},
        {"entity_recognition_mode": "required", "entity_status": "uncertain", "item_name": "X"},
        {"entity_recognition_mode": "required", "entity_status": "none", "item_name": ""},
        {"entity_recognition_mode": "required", "entity_status": "ok", "item_name": "   "},
    ],
)
def test_build_entity_row_skips_non_ok(state: dict[str, object]) -> None:
    """禁用模式、非 ok 状态或空主名一律不落 kb_entity。"""
    assert NodePersistChunks._build_entity_row(state, library_id=1, document_id=1) is None


def test_normalize_entity_name_drops_spaces_and_case() -> None:
    """归一化仅用于确定性精确对齐：去空白、忽略大小写。"""
    assert NodePersistChunks._normalize_entity_name("  H3C  LA2608 ") == "h3cla2608"
