"""内容块中间表示的回归用例。

ContentBlock 是"解析产物 → 切块"之间的唯一中间表示：PDF、Markdown、OCR 三种来源先归一到这里，
再由 node_document_split 切块。这里锁住几条一旦破坏就会静默污染全部下游向量内容的结构约定：
标题路径、页码归属、代码围栏不被当标题、表格保留表头。
"""

from __future__ import annotations

from processor.import_processor.content_blocks import (
    ContentBlock,
    build_content_blocks,
    primary_block_content,
)

SOURCE_FILE = "guide.md"


def _blocks(markdown: str, **kwargs: object) -> list[ContentBlock]:
    return build_content_blocks(markdown, source_file=SOURCE_FILE, **kwargs)  # type: ignore[arg-type]


def _types(blocks: list[ContentBlock]) -> list[str]:
    return [block["block_type"] for block in blocks]


def test_empty_input_produces_no_blocks() -> None:
    """空内容必须返回空列表，而不是抛异常打断导入。"""
    assert _blocks("") == []
    assert _blocks("   \n\n  ") == []


def test_heading_path_is_attached_to_following_paragraph() -> None:
    """正文块要带上完整的标题路径，切块后检索才能回答"第几章第几节"这类定位问题。"""
    blocks = _blocks("# 安装指南\n\n## 接线步骤\n\n请先断开电源再接线。\n")
    paragraphs = [block for block in blocks if block["block_type"] == "paragraph"]
    assert len(paragraphs) == 1
    assert paragraphs[0]["title_path"] == ["安装指南", "接线步骤"]
    assert paragraphs[0]["content"] == "请先断开电源再接线。"


def test_heading_levels_are_restored_and_nested_paths_pop() -> None:
    """同级标题应替换路径、子级标题追加路径，避免章节串台。"""
    blocks = _blocks("# 手册\n\n## 第一章\n内容一\n\n## 第二章\n内容二\n")
    paragraphs = [block for block in blocks if block["block_type"] == "paragraph"]
    assert [block["title_path"] for block in paragraphs] == [["手册", "第一章"], ["手册", "第二章"]]


def test_fake_heading_inside_code_fence_is_not_a_title() -> None:
    """代码块里的 `#` 注释不是标题，否则会被误判成章节结构并污染标题路径。"""
    blocks = _blocks(
        "# 真实标题\n\n"
        "```bash\n"
        "# 这是 shell 注释\n"
        "echo ok\n"
        "```\n\n"
        "正文内容。\n"
    )
    headings = [block for block in blocks if block["block_type"] == "heading"]
    assert [block["content"] for block in headings] == ["真实标题"]
    paragraph = next(block for block in blocks if block["block_type"] == "paragraph")
    assert paragraph["title_path"] == ["真实标题"]


def test_page_marker_is_consumed_and_applied_to_following_blocks() -> None:
    """页码注释是给机器读的元数据，既不能进正文，也要正确挂到后续块上。"""
    blocks = _blocks("<!-- page: 7 -->\n\n第七页的正文。\n\n<!-- page: 8 -->\n\n第八页的正文。\n")
    paragraphs = [block for block in blocks if block["block_type"] == "paragraph"]
    assert [block["page_number"] for block in paragraphs] == [7, 8]
    assert all("page" not in block["content"] for block in paragraphs)


def test_markdown_table_keeps_header_and_rows() -> None:
    """表格必须留住表头，否则切块后"99 元"这类数值会失去列含义。"""
    blocks = _blocks("## 价目表\n\n| 名称 | 价格 |\n| --- | --- |\n| 键盘 | 99 |\n| 鼠标 | 59 |\n")
    table_blocks = [block for block in blocks if block["block_type"] in {"table", "table_row"}]
    assert table_blocks, f"未解析出表格块，实际块类型：{_types(blocks)}"
    headers = table_blocks[0].get("table_headers")
    assert headers == ["名称", "价格"], f"表头解析结果异常：{headers}"


def test_image_block_keeps_source_and_adjacent_caption_is_separate() -> None:
    """图片块要保留原始引用路径；紧跟其后的图注单独成块，供图片问答与摘要使用。"""
    blocks = _blocks("![键盘照片](images/keyboard.png)\n图 1：键盘外观\n")
    images = [block for block in blocks if block["block_type"] == "image"]
    captions = [block for block in blocks if block["block_type"] == "image_caption"]
    assert len(images) == 1
    assert images[0]["image_source"] == "images/keyboard.png"
    assert len(captions) == 1
    assert captions[0]["content"].startswith("图 1")
    # 图注必须归到同一序号，否则无法把图与说明配对
    assert captions[0]["image_order"] == images[0]["image_order"]


def test_ocr_content_is_appended_as_ocr_blocks() -> None:
    """PDF 图片型页面的 OCR 文本走独立通道，块类型必须是 ocr_text 才能被区别切分。"""
    blocks = _blocks("# 标题\n", ocr_content="第一行识别结果\n第二行识别结果\n")
    assert "ocr_text" in _types(blocks)
    assert all(block["source_file"] == SOURCE_FILE for block in blocks)


def test_primary_block_content_drops_derived_table_rows() -> None:
    """表头行以整块 table 形式保留，派生的 table_row 不得重复进入正文，否则同一张表会被喂两遍。"""
    blocks = _blocks("## 价目表\n\n| 名称 | 价格 |\n| --- | --- |\n| 键盘 | 99 |\n| 鼠标 | 59 |\n")
    rows = [block for block in blocks if block["block_type"] == "table_row"]
    tables = [block for block in blocks if block["block_type"] == "table"]
    assert len(rows) == 2 and len(tables) == 1
    assert all(not row.get("primary", True) for row in rows), "派生表格行必须是 primary=False"
    assert tables[0]["table_headers"] == ["名称", "价格"]
    text = primary_block_content(blocks)
    assert "价目表" in text
    assert text.count("| 键盘 | 99 |") == 1, f"表格行在正文里重复出现：\n{text}"


def test_order_is_monotonic_across_blocks() -> None:
    """order 是切块阶段还原阅读顺序的唯一依据，必须严格递增。"""
    blocks = _blocks("# A\n\n段落\n\n| x |\n| --- |\n| 1 |\n\n![i](a.png)\n")
    orders = [block["order"] for block in blocks]
    assert orders == sorted(orders) and len(set(orders)) == len(orders)
