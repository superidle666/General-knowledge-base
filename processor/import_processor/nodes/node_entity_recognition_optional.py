"""实体识别可选模式节点。"""

from collections.abc import Callable
import json
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from processor.import_processor.base import BaseNode, setup_logging
from processor.import_processor.exceptions import StateFieldError
from processor.import_processor.state import ImportGraphState
from utils.llm_utils import get_llm_client

Recognizer = Callable[[str, list[dict[str, Any]]], object]


class NodeEntityRecognitionOptional(BaseNode):
    """逐个切片调用实体识别模型，允许一个切片返回多个实体。"""

    name = "node_entity_recognition_optional"

    def __init__(self, recognizer: Recognizer | None = None, **kwargs: Any):
        super().__init__(**kwargs)
        self.recognizer = recognizer or self._recognize_with_llm

    def process(self, state: ImportGraphState) -> ImportGraphState:
        """逐个切片识别实体；单个切片失败不阻断整个导入。"""
        file_title, chunks = self._get_inputs(state)
        all_names: list[str] = []
        for index, chunk in enumerate(chunks):
            try:
                raw_result = self.recognizer(file_title, [chunk])
                names = self._clean_names(raw_result)
                self.logger.info("切片 %s 实体识别原始返回：%r", index, raw_result)
            except Exception as exc:
                self.logger.warning("切片 %s 可选实体识别失败，按无实体继续：%s", index, exc)
                names = []
            chunk["item_names"] = names
            chunk["item_name"] = names[0] if names else ""
            for name in names:
                if name not in all_names:
                    all_names.append(name)
        state["item_names"] = all_names
        state["item_name"] = all_names[0] if all_names else ""
        self.logger.info("可选实体识别完成，共识别 %s 个实体", len(all_names))
        return state

    def _get_inputs(self, state: ImportGraphState) -> tuple[str, list[dict[str, Any]]]:
        file_title = state.get("file_title")
        chunks = state.get("chunks")
        if not isinstance(file_title, str) or not file_title.strip():
            raise StateFieldError(node_name=self.name, field_name="file_title", expected_type=str, message="实体识别需要文件标题")
        if not isinstance(chunks, list) or not chunks:
            raise StateFieldError(node_name=self.name, field_name="chunks", expected_type=list, message="实体识别需要有效的文档切片")
        if not all(isinstance(chunk, dict) for chunk in chunks):
            raise StateFieldError(node_name=self.name, field_name="chunks", expected_type=list, message="文档切片必须是对象列表")
        return file_title.strip(), chunks

    @staticmethod
    def _clean_names(value: object) -> list[str]:
        """将模型文本或结构化响应转换成去重后的实体名称列表。"""
        if value is None:
            return []
        if isinstance(value, str):
            text = value.replace("\r", "").replace("\t", "").strip()
            if not text:
                return []
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if parsed is not None:
                return NodeEntityRecognitionOptional._clean_names(parsed)
            return [part.strip().strip('"\'') for part in text.split("\n") if part.strip()]
        if isinstance(value, dict):
            for key in ("item_names", "entities", "entity_names", "items", "item_name", "entity_name", "name", "product_name", "device_name"):
                if key in value:
                    return NodeEntityRecognitionOptional._clean_names(value[key])
            return []
        if isinstance(value, list):
            names: list[str] = []
            for item in value:
                for name in NodeEntityRecognitionOptional._clean_names(item):
                    if name and name not in names:
                        names.append(name)
            return names
        text = str(value).replace("\r", "").replace("\t", "").strip()
        return [text] if text else []

    @classmethod
    def _clean_name(cls, value: object) -> str:
        """兼容旧调用方，返回第一个实体。"""
        names = cls._clean_names(value)
        return names[0] if names else ""

    @staticmethod
    def _apply_item_name(state: ImportGraphState, chunks: list[dict[str, Any]], item_name: str) -> None:
        """兼容强制模式旧逻辑，将单个实体回填到全部切片。"""
        state["item_name"] = item_name
        state["item_names"] = [item_name] if item_name else []
        for chunk in chunks:
            chunk["item_name"] = item_name
            chunk["item_names"] = [item_name] if item_name else []

    def _recognize_with_llm(self, file_title: str, chunks: list[dict[str, Any]]) -> object:
        """调用模型识别当前切片中的全部实体。"""
        chunk = chunks[0]
        context = f"标题：{chunk.get('title', '')}\n内容：{chunk.get('content', '')}"[: self.config.item_name_chunk_size]
        response = get_llm_client(model=self.config.item_model or None).invoke([
            SystemMessage(content=(
                "你是设备技术文档实体识别专家。请从当前切片中识别所有明确出现的产品、设备、型号或主体名称。"
                "返回 JSON 字符串数组，例如 [\"Apple iPhone 17 Pro\", \"APL-002\"]；没有明确实体时返回 []。"
            )),
            HumanMessage(content=f"文件名：{file_title}\n\n当前切片：\n{context}"),
        ])
        return self._extract_response_content(response)

    @staticmethod
    def _extract_response_content(response: object) -> object:
        content = getattr(response, "content", response)
        if isinstance(content, list):
            return "".join(
                part if isinstance(part, str) else part.get("text", "")
                for part in content
                if isinstance(part, str) or isinstance(part, dict)
            )
        return content


if __name__ == "__main__":
    setup_logging()
    state: dict[str, Any] = {"task_id": "test", "file_title": "商品档案", "chunks": [{"content": "A 与 B"}, {"content": "C"}]}
    result = NodeEntityRecognitionOptional(
        recognizer=lambda _title, chunks: ["A", "B"] if chunks[0].get("content") == "A 与 B" else {"entities": ["C"]},
    )(state)
    assert result["chunks"][0]["item_names"] == ["A", "B"]
    assert result["chunks"][1]["item_names"] == ["C"]
    print("optional mode test passed")
