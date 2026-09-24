"""文档切分策略的回归用例。

切分决定"向量库里到底存了什么"，是最容易静默劣化的一环：切坏了自己看不出来，只能在检索效果上
事后发现。这里用假配置驱动生产节点，锁住长度上限、标题边界、元数据完整性和内容无损四条底线。

注意 md_path 必须指向临时目录：节点会把 chunks-kb-*.json 备份写到 md_path 同级目录，
历史上这些含全文的备份文件被提交进过仓库，测试绝不能再往 storage/ 里写。
"""

from __future__ import annotations

from pathlib import Path

from processor.import_processor.nodes.node_document_split import NodeDocumentSplit


class SplitConfig:
    """最小切分配置，避免读取全局 get_config() 带来的环境耦合。"""

    max_content_length = 200
    min_content_length = 40


def _split(content: str, tmp_path: Path, *, chunk_size: int = 200, min_length: int = 40) -> list[dict]:
    """驱动生产切分节点，返回 chunks 列表。"""
    markdown_file = tmp_path / "guide.md"
    markdown_file.write_text(content, encoding="utf-8")
    state = {
        "task_id": "split-strategy-test",
        "knowledge_base_id": 1,
        "library_id": 1,
        "document_id": 11,
        "document_version": 3,
        "entity_recognition_mode": "disabled",
        "import_file_path": str(markdown_file),
        "md_path": str(markdown_file),
        "file_title": "guide",
        "md_content": content,
        "config_snapshot": {
            "chunking_config": {
                "chunk_size": chunk_size,
                "min_content_length": min_length,
                "chunk_overlap": 0,
            },
        },
    }
    return NodeDocumentSplit(config=SplitConfig())(state)["chunks"]


def test_long_section_is_split_and_content_is_lossless(tmp_path: Path) -> None:
    """超长章节必须被拆开，同时原文关键内容不能丢。"""
    body = "第一步：按下电源键自检。" * 40
    content = f"# 操作流程\n\n{body}\n"
    chunks = _split(content, tmp_path, chunk_size=200, min_length=40)
    assert len(chunks) > 1, "长文档没有被切分，说明 chunk_size 未生效"
    joined = "\n".join(chunk["content"] for chunk in chunks)
    assert "按下电源键自检" in joined
    assert all(chunk["content"].strip() for chunk in chunks), "存在空白切片，会污染向量库"


def test_chunk_index_is_contiguous_from_zero(tmp_path: Path) -> None:
    """chunk_index 参与 MySQL 唯一约束 (document_id, chunk_index)，必须连续且不重复。"""
    content = "# A\n\n段落一内容。\n\n# B\n\n段落二内容。\n\n# C\n\n段落三内容。\n"
    chunks = _split(content, tmp_path)
    indexes = [chunk["chunk_index"] for chunk in chunks]
    assert indexes == list(range(len(chunks))), f"chunk_index 不连续：{indexes}"


def test_chunk_carries_full_locator_metadata(tmp_path: Path) -> None:
    """每个切片都要带定位字段，否则检索命中后无法回表拿来源、也无法按版本清理旧数据。"""
    chunks = _split("# 标题\n\n正文内容在此。\n", tmp_path)
    required = {
        "chunk_index",
        "knowledge_base_id",
        "library_id",
        "document_id",
        "document_version",
        "file_name",
        "item_name",
        "entity_recognition_mode",
        "content",
    }
    for chunk in chunks:
        missing = required - chunk.keys()
        assert not missing, f"切片缺少字段 {sorted(missing)}"
        assert chunk["document_version"] == 3
        assert chunk["knowledge_base_id"] == 1 and chunk["library_id"] == 1


def test_short_sections_do_not_merge_across_headings(tmp_path: Path) -> None:
    """父子章节的正文不得混进同一切片，否则"卸载步骤"的答案会带上安装内容。"""
    content = "# 总览\n\n总览说明。\n\n## 安装\n\n安装说明。\n\n## 卸载\n\n卸载说明。\n"
    chunks = _split(content, tmp_path, chunk_size=200, min_length=0)
    assert len(chunks) == 3, f"章节没有被独立切开，实际切片数 {len(chunks)}"
    for chunk in chunks:
        mentioned = [name for name in ("总览说明", "安装说明", "卸载说明") if name in chunk["content"]]
        assert len(mentioned) == 1, f"切片混入了多个章节正文：{mentioned}"
    titles = [chunk["title"] for chunk in chunks]
    assert len(set(titles)) == len(titles), f"切片标题重复，无法定位来源章节：{titles}"


def test_tiny_document_still_produces_one_chunk(tmp_path: Path) -> None:
    """短于 min_content_length 的文档也要产出一个切片，否则整篇文档会凭空消失。"""
    chunks = _split("# 提示\n\n只有一句话。\n", tmp_path, chunk_size=200, min_length=40)
    assert len(chunks) == 1, f"短文档切片数 = {len(chunks)}"
    assert "只有一句话" in chunks[0]["content"]


def test_missing_md_content_raises_instead_of_silently_producing_nothing(tmp_path: Path) -> None:
    """缺输入必须显式报错，让导入任务落到 failed 而不是"成功但零切片"。"""
    markdown_file = tmp_path / "empty.md"
    markdown_file.write_text("", encoding="utf-8")
    state = {
        "task_id": "split-missing-input",
        "knowledge_base_id": 1,
        "document_id": 1,
        "document_version": 1,
        "import_file_path": str(markdown_file),
        "md_path": str(markdown_file),
        "file_title": "empty",
        "md_content": "",
        "config_snapshot": {},
    }
    try:
        NodeDocumentSplit(config=SplitConfig())(state)["chunks"]
    except Exception as exc:  # noqa: BLE001 - 只要求"抛出"，不绑定具体异常类型
        assert "md_content" in str(exc)
    else:
        raise AssertionError("md_content 为空时节点静默通过，会产出零切片且任务显示成功")
