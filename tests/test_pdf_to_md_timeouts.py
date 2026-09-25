"""PDF 解析外部调用超时回归测试（P0-1）。

MinerU 侧不设 timeout 是导入链路最危险的挂死路径：请求卡住会永久阻塞导入线程，
而心跳是独立 asyncio 任务、仍在续租，过期恢复永不触发，唯一 Worker 整体卡死。
这里用桩 requests 锁住三件事：
1 每个外部调用都带 (连接, 读取) 二元超时；
2 轮询存在总时长上限，不会无限等待；
3 ZIP 下载走流式分块写盘且带超时。
"""

from __future__ import annotations

import io
import types
import zipfile

import pytest

from config.mineru_config import mineru_config
from processor.import_processor.nodes import node_pdf_to_md as pdf_node_module
from processor.import_processor.nodes.node_pdf_to_md import NodePDFToMD


class _FakeResponse:
    """覆盖 json / iter_content / status_code 的最小 requests.Response 替身。"""

    def __init__(self, payload: dict | None = None, chunks: list[bytes] | None = None, status_code: int = 200):
        self.status_code = status_code
        self._payload = payload or {}
        self._chunks = chunks or []

    def json(self) -> dict:
        return self._payload

    def iter_content(self, chunk_size: int = 1):
        return iter(self._chunks)


class _Recorder:
    """记录节点发出的每一次 HTTP 调用（api 名 + kwargs），替代真实 requests。"""

    def __init__(self, get_response: _FakeResponse):
        self.calls: list[dict] = []
        self._get_response = get_response

    def _module(self) -> types.SimpleNamespace:
        return types.SimpleNamespace(post=self._post, put=self._put, get=self._get)

    def _post(self, url, **kwargs):
        self.calls.append({"api": "post", "url": url, **kwargs})
        return _FakeResponse(payload={
            "code": 0,
            "data": {"file_urls": ["http://upload.test/signed"], "batch_id": "batch-1"},
        })

    def _put(self, url, **kwargs):
        self.calls.append({"api": "put", "url": url, **kwargs})
        return _FakeResponse()

    def _get(self, url, **kwargs):
        self.calls.append({"api": "get", "url": url, **kwargs})
        return self._get_response

    def calls_for(self, api: str) -> list[dict]:
        return [call for call in self.calls if call["api"] == api]


def _prepare(monkeypatch, recorder: _Recorder) -> NodePDFToMD:
    """切断真实网络与真实服务地址，节点只对着桩运行。"""
    monkeypatch.setattr(pdf_node_module, "requests", recorder._module())
    monkeypatch.setattr(mineru_config, "base_url", "http://mineru.test")
    monkeypatch.setattr(mineru_config, "api_token", "test-token")
    return NodePDFToMD()


def _assert_valid_timeout(timeout: object) -> None:
    assert isinstance(timeout, tuple) and len(timeout) == 2, f"超时必须是 (连接, 读取) 二元组，实际：{timeout!r}"
    connect_seconds, read_seconds = timeout
    assert isinstance(connect_seconds, (int, float)) and connect_seconds > 0
    assert isinstance(read_seconds, (int, float)) and read_seconds > 0


def test_upload_and_poll_every_request_carries_timeout(monkeypatch, tmp_path) -> None:
    """取链接 / 上传 / 轮询三处调用全部带超时，模型版本取自配置而非硬编码。"""
    pdf_path = tmp_path / "样例手册.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")
    done_response = _FakeResponse(payload={
        "code": 0,
        "data": {"extract_result": [{"state": "done", "full_zip_url": "http://zip.test/result.zip"}]},
    })
    recorder = _Recorder(done_response)
    node = _prepare(monkeypatch, recorder)

    zip_url = node._step_2_upload_and_poll(pdf_path)
    assert zip_url == "http://zip.test/result.zip"

    assert recorder.calls_for("post"), "应发起取上传链接请求"
    assert recorder.calls_for("put"), "应发起文件上传请求"
    assert recorder.calls_for("get"), "应发起任务轮询请求"
    for call in recorder.calls:
        _assert_valid_timeout(call.get("timeout")), f"{call['api']} 请求缺少有效 timeout"

    post_payload = recorder.calls_for("post")[0]["json"]
    assert post_payload["model_version"] == mineru_config.model_version, "model_version 必须来自配置"


def test_poll_gives_up_after_total_budget(monkeypatch, tmp_path) -> None:
    """任务一直 processing 时，轮询必须在总时长上限后抛 TimeoutError 而非无限等待。"""
    pdf_path = tmp_path / "样例手册.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")
    processing_response = _FakeResponse(payload={
        "code": 0,
        "data": {"extract_result": [{"state": "running"}]},
    })
    recorder = _Recorder(processing_response)
    node = _prepare(monkeypatch, recorder)

    monkeypatch.setattr(mineru_config, "poll_total_timeout_seconds", 5)
    monkeypatch.setattr(mineru_config, "poll_interval_seconds", 0)
    # 假时钟：首次读取记 0 秒，之后每次推进 10 秒，模拟时间流逝且不真的 sleep。
    clock = types.SimpleNamespace(now=0.0)

    def fake_time() -> float:
        value = clock.now
        clock.now += 10.0
        return value

    monkeypatch.setattr(pdf_node_module, "time", types.SimpleNamespace(time=fake_time, sleep=lambda _s: None))

    with pytest.raises(TimeoutError):
        node._step_2_upload_and_poll(pdf_path)


def test_zip_download_streams_to_disk_with_timeout(monkeypatch, tmp_path) -> None:
    """ZIP 下载必须带超时、流式读取，且分块内容完整落盘并可解压。"""
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as zip_file:
        zip_file.writestr("full.md", "# 标题\n正文内容")
    payload = zip_buffer.getvalue()
    # 切成两块模拟流式传输：若实现仍读 response.content 会因缺少该属性直接失败。
    recorder = _Recorder(_FakeResponse(chunks=[payload[:40], payload[40:]]))
    node = _prepare(monkeypatch, recorder)

    md_path = node._step_3_download_and_extract("http://zip.test/result.zip", tmp_path, "样例手册")

    get_call = recorder.calls_for("get")[0]
    _assert_valid_timeout(get_call.get("timeout"))
    assert get_call.get("stream") is True, "ZIP 下载应使用流式模式"
    assert md_path.endswith(".md")
    assert "# 标题" in open(md_path, encoding="utf-8").read()
