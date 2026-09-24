"""导入流程状态类型定义。"""

from copy import deepcopy
import asyncio
from typing import Any, Literal, TypedDict

from processor.import_processor.content_blocks import ContentBlock

EntityRecognitionMode = Literal["disabled", "required"]


class ImportGraphState(TypedDict, total=False):
    """导入流程在 LangGraph 节点之间传递的状态。"""

    task_id: str | int
    import_task_id: int
    knowledge_base_id: int
    library_id: int
    document_id: int
    document_version: int

    is_md_read_enabled: bool
    is_pdf_read_enabled: bool

    import_file_path: str
    file_dir: str
    pdf_path: str
    md_path: str

    file_title: str
    item_name: str
    item_names: list[str]
    entity_recognition_mode: EntityRecognitionMode
    # 强制枚举结果元信息：status 为 ok/uncertain/none，供落库与人工兜底判定。
    entity_status: str
    entity_candidates: list[str]
    entity_maybe_series: bool
    # 主实体来源：llm 为模型枚举，manual 为人工确认（确认后跳过模型直接复用）。
    entity_source: str
    config_snapshot: dict[str, Any]

    md_content: str
    ocr_content: str
    content_blocks: list[ContentBlock]
    image_metadata: list[dict[str, Any]]
    chunks: list[dict[str, Any]]

    current_step: str
    progress: int
    cancel_requested: bool
    database_event_loop: asyncio.AbstractEventLoop


GRAPH_DEFAULT_STATE: ImportGraphState = {
    "task_id": "",
    "import_task_id": 0,
    "knowledge_base_id": 0,
    "library_id": 0,
    "document_id": 0,
    "document_version": 1,
    "is_pdf_read_enabled": False,
    "is_md_read_enabled": False,
    "import_file_path": "",
    "file_dir": "",
    "pdf_path": "",
    "md_path": "",
    "file_title": "",
    "item_name": "",
    "item_names": [],
    "entity_recognition_mode": "disabled",
    "entity_status": "",
    "entity_candidates": [],
    "entity_maybe_series": False,
    "entity_source": "llm",
    "config_snapshot": {},
    "md_content": "",
    "ocr_content": "",
    "content_blocks": [],
    "image_metadata": [],
    "chunks": [],
    "current_step": "",
    "progress": 0,
    "cancel_requested": False,
}


def get_default_state() -> ImportGraphState:
    """返回不会污染全局默认值的导入状态副本。"""
    return deepcopy(GRAPH_DEFAULT_STATE)
