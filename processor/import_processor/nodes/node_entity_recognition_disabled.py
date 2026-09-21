"""实体识别禁用模式节点。"""

from typing import Any

from processor.import_processor.base import BaseNode, setup_logging
from processor.import_processor.exceptions import StateFieldError
from processor.import_processor.state import ImportGraphState


class NodeEntityRecognitionDisabled(BaseNode):
    """跳过实体识别，保留后续嵌入和向量入库所需的统一字段。"""

    name = "node_entity_recognition_disabled"

    def process(self, state: ImportGraphState) -> ImportGraphState:
        """清空实体字段，并将切片交给后续嵌入和入库节点。"""
        chunks = state.get("chunks")
        if not isinstance(chunks, list) or not chunks:
            raise StateFieldError(
                node_name=self.name,
                field_name="chunks",
                expected_type=list,
                message="禁用实体识别前需要有效的文档切片",
            )

        for chunk in chunks:
            if not isinstance(chunk, dict):
                raise StateFieldError(
                    node_name=self.name,
                    field_name="chunks",
                    expected_type=list,
                    message="文档切片必须是对象列表",
                )
            chunk["item_name"] = ""

        state["item_name"] = ""
        self.logger.info("实体识别已禁用，跳过识别服务调用")
        return state


if __name__ == "__main__":
    setup_logging()
    sample_state: dict[str, Any] = {
        "task_id": "entity-disabled-test",
        "file_title": "设备使用说明书",
        "chunks": [{"title": "概述", "content": "设备安装与使用说明"}],
    }
    result = NodeEntityRecognitionDisabled()(sample_state)
    assert result["item_name"] == ""
    assert result["chunks"][0]["item_name"] == ""

    multiple_state: dict[str, Any] = {
        "task_id": "entity-disabled-multiple-test",
        "file_title": "通用资料",
        "item_name": "旧实体",
        "chunks": [
            {"title": "概述", "content": "通用说明", "item_name": "旧实体"},
            {"title": "参数", "content": "技术参数", "item_name": "旧实体"},
        ],
    }
    result = NodeEntityRecognitionDisabled()(multiple_state)
    assert result["item_name"] == ""
    assert all(chunk["item_name"] == "" for chunk in result["chunks"])
    print("disabled mode test passed")
