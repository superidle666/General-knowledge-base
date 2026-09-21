"""实体识别强制模式节点。"""

from collections.abc import Callable
from typing import Any

from processor.import_processor.base import BaseNode, setup_logging
from processor.import_processor.exceptions import ImportProcessError
from processor.import_processor.nodes.node_entity_recognition_optional import NodeEntityRecognitionOptional, Recognizer
from processor.import_processor.state import ImportGraphState


class NodeEntityRecognitionRequired(BaseNode):
    """调用实体识别模型，失败或无结果时阻断当前导入。"""

    name = "node_entity_recognition_required"

    def __init__(self, recognizer: Recognizer | None = None, **kwargs: Any):
        """初始化节点，可注入识别器以隔离单元测试和真实服务。"""
        super().__init__(**kwargs)
        self.recognizer = recognizer or self._recognize_with_llm

    def process(self, state: ImportGraphState) -> ImportGraphState:
        """调用识别器并回填实体；异常或空结果均终止导入。"""
        helper = NodeEntityRecognitionOptional(config=self.config)
        file_title, chunks = helper._get_inputs(state)
        try:
            raw_result = self.recognizer(file_title, chunks)
            self.logger.info("实体识别大模型原始返回：%r", raw_result)
            item_name = helper._clean_name(raw_result)
        except Exception as exc:
            raise ImportProcessError("强制实体识别服务调用失败", node_name=self.name, cause=exc) from exc

        if not item_name:
            raise ImportProcessError("强制实体识别未得到有效实体名称", node_name=self.name)

        helper._apply_item_name(state, chunks, item_name)
        self.logger.info("强制实体识别成功，规范化结果：%s", item_name)
        return state

    def _recognize_with_llm(self, file_title: str, chunks: list[dict[str, Any]]) -> object:
        """调用实体识别模型并返回模型响应内容。"""
        helper = NodeEntityRecognitionOptional(config=self.config)
        return helper._recognize_with_llm(file_title, chunks)


if __name__ == "__main__":
    setup_logging()
    sample_state: dict[str, Any] = {
        "task_id": "entity-required-test",
        "file_title": "网关说明书",
        "chunks": [{"title": "产品简介", "content": "LA2608 室内无线网关"}],
    }
    result = NodeEntityRecognitionRequired(recognizer=lambda _title, _chunks: "H3C LA2608")(sample_state)
    assert result["item_name"] == "H3C LA2608"
    assert result["chunks"][0]["item_name"] == "H3C LA2608"

    failed_state: dict[str, Any] = {
        "task_id": "entity-required-empty-test",
        "file_title": "未知资料",
        "chunks": [{"title": "说明", "content": "无实体"}],
    }
    try:
        NodeEntityRecognitionRequired(recognizer=lambda _title, _chunks: "")(failed_state)
    except ImportProcessError as exc:
        assert "强制实体识别未得到有效实体名称" in str(exc)
    else:
        raise AssertionError("强制实体识别未识别到实体时应失败")

    service_error_state: dict[str, Any] = {
        "task_id": "entity-required-error-test",
        "file_title": "异常资料",
        "chunks": [{"title": "说明", "content": "识别服务异常"}],
    }

    def raise_recognition_error(_title: str, _chunks: list[dict[str, Any]]) -> str:
        """模拟实体识别服务异常。"""
        raise RuntimeError("识别服务不可用")

    try:
        NodeEntityRecognitionRequired(recognizer=raise_recognition_error)(service_error_state)
    except ImportProcessError as exc:
        assert "强制实体识别服务调用失败" in str(exc)
    else:
        raise AssertionError("强制实体识别服务异常时应失败")

    print("required mode test passed")
