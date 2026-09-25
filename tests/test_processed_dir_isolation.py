"""解析目录按 task 隔离（批次二 P1-2）。

此前所有导入任务把 MinerU 的 zip 与解压产物直接平铺在 storage/processed 根下：中间产物
没有归属，想按任务清理都不知道该删哪些。改为 storage/processed/{task_id} 后，一次导入
一个工作目录，重试时整体重置自己的目录也碰不到别的任务。
"""

import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import processor.import_processor.nodes.node_pdf_to_md as pdf_node_module
from processor.import_processor.nodes.node_pdf_to_md import NodePDFToMD
from processor.import_processor.worker import ImportWorker


def make_task(task_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        config_snapshot={},
        entity_result={},
    )


def make_document() -> SimpleNamespace:
    return SimpleNamespace(
        id=31,
        storage_key="storage/uploads/4/手册_a1b2c3.pdf",
        title="手册",
        version=1,
    )


LIBRARY = SimpleNamespace(id=4, entity_recognition_mode="disabled")


def build_state(task_id: int) -> dict:
    return ImportWorker._build_state(make_task(task_id), make_document(), LIBRARY)


def test_file_dir_is_a_task_scoped_subdir() -> None:
    """file_dir 必须落到 storage/processed/{task_id}，而不是共用的上一层。"""
    state = build_state(1024)
    file_dir = Path(state["file_dir"])

    assert file_dir.name == "1024"
    assert file_dir.parent.name == "processed"
    assert file_dir.is_absolute()


def test_different_tasks_get_different_dirs() -> None:
    assert build_state(11)["file_dir"] != build_state(12)["file_dir"]


def test_same_task_reuses_its_own_dir() -> None:
    """重试用同一行 ImportTask：目录不变，节点内的 rmtree 才能整体重置自己。"""
    assert build_state(7)["file_dir"] == build_state(7)["file_dir"]


@pytest.mark.parametrize("raw_task_id", ["42", 42.0])
def test_task_id_is_normalized_to_int(raw_task_id: object) -> None:
    assert Path(build_state(raw_task_id)["file_dir"]).name == "42"  # type: ignore[arg-type]


def test_path_traversal_in_task_id_is_rejected() -> None:
    """task_id 只接受整数：拼路径前必须先归一，不能让外部输入走到别的目录。"""
    with pytest.raises((TypeError, ValueError)):
        build_state("7/../../outside")  # type: ignore[arg-type]


class _FakeResponse:
    status_code = 200

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def iter_content(self, chunk_size: int = 1024):
        for offset in range(0, len(self._payload), chunk_size):
            yield self._payload[offset:offset + chunk_size]


def make_zip_bytes(md_text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zip_file:
        zip_file.writestr("full.md", md_text)
        zip_file.writestr("images/pic_1.png", b"png-bytes")
    return buffer.getvalue()


@pytest.fixture
def fake_download(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    """把 ZIP 下载换成内存内容，按 url 区分不同任务的解析结果。"""
    payloads: dict[str, bytes] = {}

    class FakeRequests:
        @staticmethod
        def get(url: str, **_kwargs):
            return _FakeResponse(payloads[url])

    monkeypatch.setattr(pdf_node_module, "requests", FakeRequests)
    return payloads


def test_same_named_documents_do_not_touch_each_other(
    tmp_path: Path,
    fake_download: dict[str, bytes],
) -> None:
    """同名 PDF 落在各自的 task 目录里，一个任务的清理 rmtree 碰不到另一个。"""
    node = NodePDFToMD()
    stem = "用户手册_a1b2c3"
    fake_download["url-task-1"] = make_zip_bytes("任务一的内容")
    fake_download["url-task-2"] = make_zip_bytes("任务二的内容")

    dir_1 = tmp_path / "1"
    dir_2 = tmp_path / "2"
    dir_1.mkdir()
    dir_2.mkdir()

    md_1 = Path(node._step_3_download_and_extract("url-task-1", dir_1, stem))
    md_2 = Path(node._step_3_download_and_extract("url-task-2", dir_2, stem))

    assert md_1.read_text(encoding="utf-8") == "任务一的内容"
    assert md_2.read_text(encoding="utf-8") == "任务二的内容"
    # 解压产物按任务隔离：图片目录也在各自的 task 下，不会串
    assert (dir_1 / stem / "images" / "pic_1.png").exists()
    assert (dir_2 / stem / "images" / "pic_1.png").exists()
    assert md_1.parent == dir_1 / stem
    assert md_2.parent == dir_2 / stem


def test_retry_resets_only_its_own_task_dir(tmp_path: Path, fake_download: dict[str, bytes]) -> None:
    """同一任务重跑会清掉自己上一轮的残留，但别的任务目录保持原样。"""
    node = NodePDFToMD()
    stem = "用户手册_a1b2c3"
    fake_download["url-task-1"] = make_zip_bytes("第一次的结果")
    fake_download["url-task-2"] = make_zip_bytes("另一个任务")

    dir_1 = tmp_path / "1"
    dir_2 = tmp_path / "2"
    dir_1.mkdir()
    dir_2.mkdir()
    Path(node._step_3_download_and_extract("url-task-1", dir_1, stem))
    md_2 = Path(node._step_3_download_and_extract("url-task-2", dir_2, stem))
    stale_image = dir_1 / stem / "images" / "旧图片.png"
    stale_image.write_bytes(b"stale")

    md_1 = Path(node._step_3_download_and_extract("url-task-1", dir_1, stem))

    assert md_1.read_text(encoding="utf-8") == "第一次的结果"
    assert not stale_image.exists(), "本任务上一轮残留应被清掉"
    assert md_2.exists() and md_2.read_text(encoding="utf-8") == "另一个任务"


def test_worker_does_not_point_file_dir_at_the_shared_root() -> None:
    """静态锁：别再退回"所有任务共用 storage/processed 根目录"的写法。"""
    import inspect

    source = inspect.getsource(ImportWorker._build_state)
    assert "_processed_dir(" in source
    assert 'storage/processed"' not in source
