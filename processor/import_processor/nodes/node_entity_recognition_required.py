"""实体识别强制模式节点：一篇文档枚举一个主实体。"""

from collections.abc import Callable
import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from processor.import_processor.base import BaseNode, setup_logging
from processor.import_processor.exceptions import ImportProcessError, StateFieldError
from processor.import_processor.state import ImportGraphState
from utils.llm_utils import get_llm_client

Recognizer = Callable[[str, list[dict[str, Any]]], object]

# 文件名型号 token：字母开头、含连字符的型号串（如 HAK-180、LA2608-A）。
_MODEL_TOKEN = re.compile(r"[A-Za-z]{2,}[A-Za-z0-9]*(?:[-][A-Za-z0-9]+)+")
_FILE_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,5}$")


class NodeEntityRecognitionRequired(BaseNode):
    """从采样切片与文件名枚举单一主实体；调用异常或无有效实体时阻断导入。"""

    name = "node_entity_recognition_required"

    def __init__(self, recognizer: Recognizer | None = None, **kwargs: Any):
        """初始化节点，可注入识别器以隔离单元测试和真实服务。"""
        super().__init__(**kwargs)
        self.recognizer = recognizer or self._recognize_with_llm

    def process(self, state: ImportGraphState) -> ImportGraphState:
        """采样后调用识别器枚举主实体；未确定时不阻断，改由条件边转入人工确认。"""
        file_title, chunks = self._get_inputs(state)

        # 人工确认过的重跑：worker 已在 state 预置已确认实体，直接复用，不再调模型。
        confirmed = self._confirmed_entity(state)
        if confirmed:
            self._apply_item_name(state, chunks, confirmed)
            state.setdefault("entity_source", "manual")
            self.logger.info("强制实体识别使用已确认主实体：%s", confirmed)
            return state

        sample = self._sample_chunks(chunks, self.config.item_name_chunk_k)
        try:
            raw_result = self.recognizer(file_title, sample)
            self.logger.info("主实体枚举大模型原始返回：%r", raw_result)
            result = self._parse_result(raw_result)
        except Exception as exc:
            raise ImportProcessError("强制实体识别服务调用失败", node_name=self.name, cause=exc) from exc

        main_entity = result["main_entity"] or self._filename_model(file_title)
        status = result["status"]
        if status != "uncertain":
            status = "ok" if main_entity else "none"
        candidates = result["candidates"] or ([main_entity] if main_entity else [])
        maybe_series = len({name for name in candidates if name}) > 1

        state["entity_status"] = status
        state["entity_candidates"] = candidates
        state["entity_maybe_series"] = maybe_series
        state["entity_source"] = "llm"

        if status != "ok" or not main_entity:
            # 不写臆测实体也不阻断；留待 worker 根据 entity_status 将任务转 needs_review。
            self.logger.warning("强制实体识别未确定主实体（status=%s），转入人工确认", status)
            return state

        self._apply_item_name(state, chunks, main_entity)
        self.logger.info(
            "强制实体识别成功：主实体=%s 候选数=%s 疑似系列=%s",
            main_entity, len(candidates), maybe_series,
        )
        return state

    @staticmethod
    def _confirmed_entity(state: ImportGraphState) -> str:
        """人工确认后重跑时，worker 预置的已确认主实体；未确认时返回空串。"""
        if state.get("entity_status") != "ok" or state.get("entity_source") != "manual":
            return ""
        return str(state.get("item_name") or "").strip()

    def _get_inputs(self, state: ImportGraphState) -> tuple[str, list[dict[str, Any]]]:
        """校验并取出文件标题与文档切片，供实体识别使用。"""
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
    def _sample_chunks(chunks: list[dict[str, Any]], k: int) -> list[dict[str, Any]]:
        """取首切片并均匀补样至多 k 个，用于一次枚举整篇主实体，避免全量喂入超长上下文。"""
        total = len(chunks)
        if total == 0:
            return []
        if k <= 1 or total <= k:
            return list(chunks) if total <= k else [chunks[0]]
        step = (total - 1) / (k - 1)
        picked = sorted({min(total - 1, round(index * step)) for index in range(k)})
        return [chunks[index] for index in picked]

    def _format_sample(self, sample: list[dict[str, Any]]) -> str:
        """按采样片数动态分配单片上限（单切片可用满总预算），再拼接为模型上下文。"""
        budget = self.config.entity_enum_char_budget
        count = max(1, len(sample))
        # 片数越少单片预算越大，单切片短文档不再被固定 800 字截断而丢失实体。
        per_chunk_limit = max(self.config.item_name_chunk_size, budget // count)
        parts: list[str] = []
        used = 0
        for chunk in sample:
            segment = f"标题：{chunk.get('title', '')}\n内容：{chunk.get('content', '')}"[:per_chunk_limit]
            if parts and used + len(segment) > budget:
                break
            parts.append(segment)
            used += len(segment)
        return "\n---\n".join(parts)[:budget]

    @staticmethod
    def _filename_model(file_title: str) -> str:
        """文件名含型号 token 时，返回去掉扩展名的文件名作为主实体先验。"""
        stem = _FILE_EXTENSION.sub("", file_title.strip()).strip()
        return stem if _MODEL_TOKEN.search(stem) else ""

    def _parse_result(self, raw: object) -> dict[str, Any]:
        """把模型或注入识别器的返回统一规整为 {main_entity, candidates, status}。"""
        data = raw
        if isinstance(data, str):
            text = data.replace("\r", "").strip()
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = text
        if isinstance(data, dict):
            main = str(data.get("main_entity") or data.get("item_name") or data.get("name") or "").strip()
            raw_candidates = data.get("candidates")
            if raw_candidates is None:
                raw_candidates = data.get("entities") or data.get("item_names")
            candidates = self._clean_names(raw_candidates) if raw_candidates is not None else ([] if not main else [main])
            status = str(data.get("status") or "").strip().lower()
            if not main and candidates:
                main = candidates[0]
            if not status:
                status = "ok" if (main or candidates) else "none"
            return {"main_entity": main, "candidates": candidates, "status": status}
        names = self._clean_names(data)
        return {"main_entity": names[0] if names else "", "candidates": names, "status": "ok" if names else "none"}

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
                return NodeEntityRecognitionRequired._clean_names(parsed)
            return [part.strip().strip('"\'') for part in text.split("\n") if part.strip()]
        if isinstance(value, dict):
            for key in ("item_names", "entities", "entity_names", "items", "item_name", "entity_name", "name", "product_name", "device_name"):
                if key in value:
                    return NodeEntityRecognitionRequired._clean_names(value[key])
            return []
        if isinstance(value, list):
            names: list[str] = []
            for item in value:
                for name in NodeEntityRecognitionRequired._clean_names(item):
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
        """将主实体覆盖式回填到全部切片，保证一篇文档一个主实体。"""
        state["item_name"] = item_name
        state["item_names"] = [item_name] if item_name else []
        for chunk in chunks:
            chunk["item_name"] = item_name
            chunk["item_names"] = [item_name] if item_name else []

    def _recognize_with_llm(self, file_title: str, sample_chunks: list[dict[str, Any]]) -> object:
        """用一次 JSON 模式调用，从采样切片枚举整篇文档的单一主实体。"""
        context = self._format_sample(sample_chunks)
        response = get_llm_client(model=self.config.item_model or None, json_mode=True).invoke([
            SystemMessage(content=(
                "你是设备技术文档实体识别专家。给定同一篇文档的若干采样切片与文件名，"
                "判断这篇文档主要描述的单一主体（产品、设备或型号），并从候选中选出出现频率最高者作为主实体。"
                "只返回 JSON 对象，字段：main_entity(字符串，无法确定时为空串)、"
                "candidates(去重后的候选实体字符串数组)、"
                "status(ok=能确定主实体；uncertain=存在多个同等可能主体需人工确认；none=文中无明确实体)。"
            )),
            HumanMessage(content=f"文件名：{file_title}\n\n采样内容：\n{context}"),
        ])
        return self._extract_response_content(response)

    @staticmethod
    def _extract_response_content(response: object) -> object:
        """兼容不同模型返回体，抽取可解析的文本内容。"""
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
    sample_state: dict[str, Any] = {
        "task_id": "entity-required-test",
        "file_title": "网关说明书",
        "chunks": [{"title": "产品简介", "content": "LA2608 室内无线网关"}],
    }
    result = NodeEntityRecognitionRequired(recognizer=lambda _title, _chunks: "H3C LA2608")(sample_state)
    assert result["item_name"] == "H3C LA2608"
    assert result["chunks"][0]["item_name"] == "H3C LA2608"
    assert result["entity_status"] == "ok"

    series_state: dict[str, Any] = {
        "task_id": "entity-required-series-test",
        "file_title": "整机手册",
        "chunks": [
            {"title": "简介", "content": "介绍 X100 与 X200 两款机型"},
            {"title": "参数", "content": "X100 参数"},
            {"title": "附录", "content": "X200 参数"},
        ],
    }
    series_result = NodeEntityRecognitionRequired(
        recognizer=lambda _title, _chunks: {"main_entity": "X100", "candidates": ["X100", "X200"], "status": "ok"}
    )(series_state)
    assert series_result["item_name"] == "X100"
    assert series_result["entity_maybe_series"] is True
    assert all(chunk["item_name"] == "X100" for chunk in series_result["chunks"])

    # 文件名型号先验：正文没给主实体时，回退到含型号的文件名。
    filename_state: dict[str, Any] = {
        "task_id": "entity-required-filename-test",
        "file_title": "HAK-180 安装手册.pdf",
        "chunks": [{"title": "安装", "content": "通用安装说明"}],
    }
    filename_result = NodeEntityRecognitionRequired(recognizer=lambda _title, _chunks: {"status": "none"})(filename_state)
    assert filename_result["item_name"] == "HAK-180 安装手册", filename_result["item_name"]

    failed_state: dict[str, Any] = {
        "task_id": "entity-required-empty-test",
        "file_title": "未知资料",
        "chunks": [{"title": "说明", "content": "无实体"}],
    }
    empty_result = NodeEntityRecognitionRequired(recognizer=lambda _title, _chunks: "")(failed_state)
    assert empty_result["entity_status"] == "none"
    assert empty_result.get("item_name", "") == "", "无实体时不应写 item_name"

    uncertain_state: dict[str, Any] = {
        "task_id": "entity-required-uncertain-test",
        "file_title": "多机型对比",
        "chunks": [{"title": "对比", "content": "A 与 B 并列"}],
    }
    uncertain_result = NodeEntityRecognitionRequired(
        recognizer=lambda _title, _chunks: {"main_entity": "", "candidates": ["A 机型", "B 机型"], "status": "uncertain"}
    )(uncertain_state)
    assert uncertain_result["entity_status"] == "uncertain"
    assert uncertain_result["entity_maybe_series"] is True
    assert uncertain_result.get("item_name", "") == "", "uncertain 时应留给人工确认，不自动定实体"

    # 人工确认后重跑：worker 预置已确认实体，节点应跳过模型直接采用。
    confirmed_state: dict[str, Any] = {
        "task_id": "entity-required-confirmed-test",
        "file_title": "多机型对比",
        "item_name": "A 机型",
        "entity_status": "ok",
        "entity_source": "manual",
        "entity_candidates": ["A 机型", "B 机型"],
        "entity_maybe_series": True,
        "chunks": [{"title": "对比", "content": "A 与 B 并列"}],
    }

    def _must_not_call_llm(_title: str, _chunks: list[dict[str, Any]]) -> str:
        """确认重跑时不应再调用识别器。"""
        raise AssertionError("已确认实体时不应再调用大模型")

    confirmed_result = NodeEntityRecognitionRequired(recognizer=_must_not_call_llm)(confirmed_state)
    assert confirmed_result["item_name"] == "A 机型"
    assert confirmed_result["chunks"][0]["item_name"] == "A 机型"

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
        raise AssertionError("识别服务异常时应终止导入")

    print("required mode test passed")
