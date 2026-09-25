"""
图片摘要节点的客户端复用与限流口径测试（批次二 P1-8）。

修的是两件事：
1. 每张图片都新建一次 `ChatOpenAI`——连接池从没被复用到，白付 N-1 次建连，而且没设
   超时，一次卡住的请求能占住 Worker 几十分钟（SDK 默认 600 秒再叠默认重试）；
2. 限流上限在节点里硬编码成 10，而配置项 `ImportConfig.requests_per_minute` 是 15，
   两套口径各说各话，改配置不生效。

建连与请求体分离：这里只允许「复用 + 超时」改变行为，请求体参数必须与改前一致。
"""
from pathlib import Path
from typing import Any

import pytest

from processor.import_processor.import_config import ImportConfig
from processor.import_processor.nodes import node_md_img
from processor.import_processor.nodes.node_md_img import NodeMDImg

IMAGE_COUNT = 3  # 必须大于 1：单张图时“每次调用新建”也只建一个实例，抓不出问题


class RecordingChatModel:
    """记录构造参数的假客户端：构造即计数，invoke 返回固定摘要。"""

    instances: list["RecordingChatModel"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        RecordingChatModel.instances.append(self)

    def invoke(self, messages: Any) -> Any:
        class _Response:
            content = "一张设备正面图"

        return _Response()


@pytest.fixture(autouse=True)
def _reset_chat_model_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    RecordingChatModel.instances = []
    monkeypatch.setattr(node_md_img, "ChatOpenAI", RecordingChatModel)


def _images(tmp_path: Path) -> list[tuple[str, str, tuple[str, str]]]:
    """造若干真实存在的图片文件：_summarize_image 会真的读盘做 base64。"""
    targets = []
    for index in range(IMAGE_COUNT):
        image_file = f"pic{index}.jpg"
        (tmp_path / image_file).write_bytes(b"fake-image-bytes")
        targets.append((image_file, str(tmp_path / image_file), ("上文", "下文")))
    return targets


def test_client_is_built_once_for_the_whole_batch(tmp_path: Path) -> None:
    """一整批图片只建一个客户端。"""
    node = NodeMDImg()
    summaries = node._step_3_generate_summaries("doc", _images(tmp_path))

    assert len(summaries) == IMAGE_COUNT
    assert len(RecordingChatModel.instances) == 1, "客户端没被复用，仍在按图片数量反复建连"


def test_client_carries_a_request_timeout() -> None:
    """没传 timeout 就等于吃 SDK 默认的 600 秒，必须显式设。"""
    NodeMDImg()._get_chat_model()

    built = RecordingChatModel.instances[0].kwargs
    assert built.get("timeout") == node_md_img.VL_REQUEST_TIMEOUT_SECONDS
    assert node_md_img.VL_REQUEST_TIMEOUT_SECONDS <= 120, "超时设得过长，仍会长时间占住 Worker"


def test_request_body_keeps_the_previous_parameters() -> None:
    """请求体只允许多一个 timeout，不得混进文本模型专用的 extra_body / model_kwargs。"""
    NodeMDImg()._get_chat_model()

    built = RecordingChatModel.instances[0].kwargs
    assert set(built) == {"model", "api_key", "base_url", "temperature", "timeout"}
    assert built["model"] == node_md_img.lm_config.vl_model, "不该改用文本模型"


def test_rate_limit_follows_the_configured_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """限流上限取配置，不再硬编码；配置变了行为跟着变。"""
    seen_limits: list[int] = []
    real_apply = NodeMDImg._apply_api_rate_limit

    def spy(self, request_times, max_requests, window_seconds=60):
        seen_limits.append(max_requests)
        return real_apply(self, request_times, max_requests, window_seconds)

    monkeypatch.setattr(NodeMDImg, "_apply_api_rate_limit", spy)
    config = ImportConfig(requests_per_minute=7)
    NodeMDImg(config=config)._step_3_generate_summaries("doc", _images(tmp_path))

    assert seen_limits == [7] * IMAGE_COUNT, f"限流上限未取配置值：{seen_limits}"


def test_default_rate_limit_is_the_shared_config_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """默认口径必须与全局配置一致：两边都是 15，节点里不该另有一套数字。"""
    seen_limits: list[int] = []
    monkeypatch.setattr(
        NodeMDImg,
        "_apply_api_rate_limit",
        lambda self, request_times, max_requests, window_seconds=60: seen_limits.append(max_requests),
    )
    node = NodeMDImg()  # 不传 config，走全局单例

    node._step_3_generate_summaries("doc", _images(tmp_path))

    assert seen_limits == [node.config.requests_per_minute] * IMAGE_COUNT
    assert node.config.requests_per_minute == ImportConfig(requests_per_minute=15).requests_per_minute


def test_rate_limit_number_is_not_hardcoded_in_the_node() -> None:
    """静态锁：速率上限必须来自配置，不能又写回一个字面量。"""
    import inspect

    source = inspect.getsource(node_md_img.NodeMDImg._step_3_generate_summaries)
    assert "max_requests=self.config.requests_per_minute" in source
    assert "max_requests=10" not in source, "节点里又出现了硬编码的限流数字"


def test_summary_failure_still_falls_back_to_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超时/调用失败仍按原语义吞成占位符，不阻断导入——本项不改这个行为。"""
    class FailingChatModel(RecordingChatModel):
        def invoke(self, messages: Any) -> Any:
            raise RuntimeError("请求超时")

    monkeypatch.setattr(node_md_img, "ChatOpenAI", FailingChatModel)
    node = NodeMDImg()

    summaries = node._step_3_generate_summaries("doc", _images(tmp_path))

    assert set(summaries.values()) == {"图片描述"}


def test_reused_client_is_used_on_the_real_call_path(tmp_path: Path) -> None:
    """复用必须真的发生在摘要调用链上，而不是只多出一个没人调的工厂方法。"""
    node = NodeMDImg()
    images = _images(tmp_path)

    for _, image_path, context in images:
        node._summarize_image(image_path, root_folder="doc", image_content=context)

    assert len(RecordingChatModel.instances) == 1
