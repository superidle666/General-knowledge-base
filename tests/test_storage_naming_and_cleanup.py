"""Storage 命名改造与清理机制（命名可读 + 导入后清 processed + 文档删除级联）。

改造目标：
1. uploads 目录名含库名 slug（不再纯数字 ID）
2. 导入成功后自动清理 processed/{task_id} 工作区
3. 文档删除时级联清理本地文件与 MinIO 图片
"""

from pathlib import Path
from types import SimpleNamespace

from backend.app.core.path_utils import slugify
from processor.import_processor.worker import ImportWorker


# --------------------------------------------------------------------------
# slugify 单元测试
# --------------------------------------------------------------------------


class TestSlugify:
    def test_chinese_preserved(self):
        assert slugify("我的手机知识库") == "我的手机知识库"

    def test_spaces_replaced(self):
        assert slugify("iPhone 18 Pro Manual") == "iPhone_18_Pro_Manual"

    def test_illegal_chars_removed(self):
        result = slugify('A/B:C*D?E"F<G>H|I')
        # 所有非法字符移除后拼在一起
        assert "/" not in result
        assert ":" not in result
        assert "*" not in result

    def test_truncation(self):
        long_name = "A" * 100
        result = slugify(long_name, max_length=30)
        assert len(result) <= 30

    def test_empty_fallback(self):
        assert slugify("") == "library"
        assert slugify("///") == "library"

    def test_leading_trailing_dots_removed(self):
        assert slugify("...hello...") == "hello"


# --------------------------------------------------------------------------
# ImportWorker._processed_dir 命名与产物保留测试
# --------------------------------------------------------------------------


class TestProcessedDirNaming:
    """解析产物目录：按知识库分层、每个上传文件独立成夹、成功后不再删除。"""

    def test_processed_dir_is_library_scoped(self):
        library = SimpleNamespace(id=33, name="手机商品介绍")
        result = str(ImportWorker._processed_dir(library)).replace("\\", "/")
        assert result.endswith("storage/processed/33-手机商品介绍")

    def test_processed_dir_sanitizes_library_name(self):
        library = SimpleNamespace(id=7, name="my/ka:se *test")
        folder = ImportWorker._processed_dir(library).name
        # 非法字符被 slugify 洗掉，仍带库ID前缀
        assert folder.startswith("7-")
        assert "/" not in folder and ":" not in folder and "*" not in folder

    def test_worker_no_longer_deletes_artifacts(self):
        """改造后 Worker 成功分支不再删 processed：既不引用清理方法，也不出现 rmtree。"""
        import inspect

        source = inspect.getsource(ImportWorker.process_job)
        assert "_cleanup_task_artifacts" not in source
        assert "rmtree" not in source


# --------------------------------------------------------------------------
# 文档删除级联清理测试
# --------------------------------------------------------------------------


class TestDeleteCascade:
    """_cleanup_document_files 删本地文件 + MinIO。"""

    def test_local_source_and_json_removed(self, tmp_path: Path, monkeypatch):
        """模拟文档删除：源 .md + chunks json 被清理。"""
        # Mock MinIO 客户端为 None（不连外部）
        import backend.app.api.mysql as mysql_module
        monkeypatch.setattr(mysql_module, "get_minio_client", lambda: None)

        uploads_dir = tmp_path / "storage" / "uploads" / "33_kb"
        uploads_dir.mkdir(parents=True)
        source_file = uploads_dir / "iphone18_abc12345.md"
        source_file.write_text("# iPhone 18", encoding="utf-8")
        chunks_json = uploads_dir / "chunks-kb-33-doc-90-v-1.json"
        chunks_json.write_text("{}", encoding="utf-8")

        document = SimpleNamespace(
            id=90,
            storage_key=str(source_file),
            original_filename="iphone18.md",
        )
        mysql_module._cleanup_document_files(document)

        assert not source_file.exists(), "源文件应被清理"
        assert not chunks_json.exists(), "chunks json 应被清理"

    def test_minio_unreachable_does_not_raise(self, tmp_path: Path, monkeypatch):
        """MinIO 不可达时只记日志不抛异常。"""
        import backend.app.api.mysql as mysql_module

        def _raise(*a, **kw):
            raise ConnectionError("MinIO down")

        monkeypatch.setattr(mysql_module, "get_minio_client", _raise)
        document = SimpleNamespace(
            id=99,
            storage_key=str(tmp_path / "nonexistent.md"),
            original_filename="test.md",
        )
        # 不应抛出异常
        mysql_module._cleanup_document_files(document)
