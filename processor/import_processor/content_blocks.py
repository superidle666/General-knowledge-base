"""将解析后的 Markdown/OCR 内容转换为统一的内部内容块。"""

from __future__ import annotations

import re
from typing import Callable, Literal, TypedDict


ContentBlockType = Literal[
    "heading",
    "paragraph",
    "table",
    "table_row",
    "image",
    "image_caption",
    "ocr_text",
]


class ContentBlock(TypedDict, total=False):
    """导入阶段的结构化内容单元，尚未等同于最终 Chunk。"""

    block_type: ContentBlockType
    content: str
    raw_content: str
    order: int
    page_number: int | None
    title_path: list[str]
    source_file: str
    source_kind: str
    heading_level: int
    image_source: str
    image_order: int
    image_context: str
    table_headers: list[str]
    table_rows: list[str]
    table_header_content: str
    primary: bool


_HEADING_PATTERN = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*$")
_IMAGE_PATTERN = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_PAGE_MARKER_PATTERN = re.compile(
    r"^\s*<!--\s*(?:page|page_number|页码)\s*[:=]\s*(\d+)\s*-->\s*$",
    re.IGNORECASE,
)
_HTML_TABLE_START_PATTERN = re.compile(r"<table\b", re.IGNORECASE)
_HTML_TABLE_END_PATTERN = re.compile(r"</table>", re.IGNORECASE)
_HTML_ROW_PATTERN = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_HTML_CELL_PATTERN = re.compile(r"<t[hd]\b[^>]*>(.*?)</t[hd]>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
_CAPTION_PATTERN = re.compile(r"^\s*(?:图\s*\d*|figure\s*\d*|fig\.\s*\d*)[：:.\s]", re.IGNORECASE)
_CODE_FENCE_PATTERN = re.compile(r"^\s*(`{3,}|~{3,})")

BlockAppender = Callable[..., None]


def build_content_blocks(
    markdown_content: str,
    *,
    source_file: str,
    source_kind: str = "markdown",
    ocr_content: str = "",
) -> list[ContentBlock]:
    """将 Markdown、PDF 解析文本或 OCR 文本转换为统一的内容块列表。"""
    lines = markdown_content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[ContentBlock] = []
    title_path: list[str] = []
    current_page: int | None = None
    paragraph_lines: list[str] = []
    code_fence: str | None = None
    image_order = 0

    def append_block(
        block_type: ContentBlockType,
        content: str,
        raw_content: str,
        *,
        primary: bool = True,
        **extra: object,
    ) -> None:
        text = content.strip()
        if not text and block_type not in {"image"}:
            return
        block: ContentBlock = {
            "block_type": block_type,
            "content": text,
            "raw_content": raw_content,
            "order": len(blocks),
            "page_number": current_page,
            "title_path": list(title_path),
            "source_file": source_file,
            "source_kind": source_kind,
            "primary": primary,
        }
        block.update(extra)
        blocks.append(block)

    def flush_paragraph() -> None:
        nonlocal paragraph_lines
        raw_content = "\n".join(paragraph_lines).strip()
        if raw_content:
            append_block("paragraph", raw_content, raw_content)
        paragraph_lines = []

    index = 0
    while index < len(lines):
        line = lines[index]
        fence_match = _CODE_FENCE_PATTERN.match(line)
        if code_fence:
            paragraph_lines.append(line)
            if fence_match and fence_match.group(1) == code_fence:
                code_fence = None
            index += 1
            continue
        if fence_match:
            paragraph_lines.append(line)
            code_fence = fence_match.group(1)
            index += 1
            continue

        page_match = _PAGE_MARKER_PATTERN.match(line)
        if page_match:
            flush_paragraph()
            current_page = int(page_match.group(1))
            index += 1
            continue

        heading_match = _HEADING_PATTERN.match(line)
        if heading_match:
            flush_paragraph()
            level = len(heading_match.group(1))
            heading_text = heading_match.group(2).strip()
            title_path = title_path[: level - 1]
            title_path.append(heading_text)
            append_block(
                "heading",
                heading_text,
                line,
                heading_level=level,
            )
            index += 1
            continue

        if _HTML_TABLE_START_PATTERN.search(line):
            flush_paragraph()
            table_lines = [line]
            index += 1
            while index < len(lines) and not _HTML_TABLE_END_PATTERN.search(table_lines[-1]):
                table_lines.append(lines[index])
                index += 1
            _append_html_table_blocks(table_lines, append_block)
            continue

        if _is_markdown_table_line(line):
            flush_paragraph()
            table_lines = [line]
            index += 1
            while index < len(lines) and _is_markdown_table_line(lines[index]):
                table_lines.append(lines[index])
                index += 1
            _append_markdown_table_blocks(table_lines, append_block)
            continue

        image_match = _IMAGE_PATTERN.search(line)
        if image_match:
            flush_paragraph()
            image_order += 1
            image_alt = image_match.group(1).strip()
            append_block(
                "image",
                image_alt,
                line,
                image_source=image_match.group(2).strip(),
                image_order=image_order,
                image_context=image_alt,
            )
            if index + 1 < len(lines) and _CAPTION_PATTERN.match(lines[index + 1]):
                caption = lines[index + 1].strip()
                append_block(
                    "image_caption",
                    caption,
                    caption,
                    image_order=image_order,
                    image_context=f"{image_alt}；{caption}" if image_alt else caption,
                )
                index += 1
            index += 1
            continue

        if not line.strip():
            flush_paragraph()
        else:
            paragraph_lines.append(line)
        index += 1

    flush_paragraph()

    normalized_ocr = ocr_content.strip()
    if normalized_ocr:
        append_block(
            "ocr_text",
            normalized_ocr,
            normalized_ocr,
            source_kind="ocr",
        )
    return blocks


def primary_block_content(blocks: list[ContentBlock]) -> str:
    """按原始顺序重建供当前切分器使用的主内容，不重复表格行等派生块。"""
    return "\n\n".join(
        block["raw_content"].strip()
        for block in blocks
        if block.get("primary", True) and block.get("raw_content", "").strip()
    )


def _is_markdown_table_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.count("|") >= 2


def _is_markdown_table_separator(line: str) -> bool:
    cells = [cell.strip().replace(":", "").replace("-", "") for cell in line.strip().strip("|").split("|")]
    return bool(cells) and all(not cell for cell in cells)


def _split_markdown_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _append_markdown_table_blocks(table_lines: list[str], append_block: BlockAppender) -> None:
    add_block = append_block
    table_content = "\n".join(table_lines)
    headers = _split_markdown_row(table_lines[0]) if table_lines else []
    row_contents: list[str] = []
    raw_rows: list[str] = []
    for row in table_lines[1:]:
        if _is_markdown_table_separator(row):
            continue
        values = _split_markdown_row(row)
        pairs = [f"{headers[position] if position < len(headers) else f'列{position + 1}'}：{value}" for position, value in enumerate(values)]
        row_contents.append("；".join(pairs))
        raw_rows.append(row)
    header_content = "\n".join(table_lines[:2])
    add_block(
        "table",
        table_content,
        table_content,
        table_headers=headers,
        table_rows=raw_rows,
        table_header_content=header_content,
    )
    for row, row_content in zip(raw_rows, row_contents):
        add_block("table_row", row_content, row, primary=False, table_headers=headers)


def _append_html_table_blocks(table_lines: list[str], append_block: BlockAppender) -> None:
    add_block = append_block
    table_content = "\n".join(table_lines)
    rows: list[list[str]] = []
    for row_html in _HTML_ROW_PATTERN.findall(table_content):
        cells = [_clean_html(cell) for cell in _HTML_CELL_PATTERN.findall(row_html)]
        if cells:
            rows.append(cells)
    headers = rows[0] if rows else []
    row_contents: list[str] = []
    raw_rows: list[str] = []
    for values in rows[1:]:
        pairs = [f"{headers[position] if position < len(headers) else f'列{position + 1}'}：{value}" for position, value in enumerate(values)]
        row_contents.append("；".join(pairs))
        raw_rows.append(" | ".join(values))
    header_content = ""
    if rows:
        header_match = _HTML_ROW_PATTERN.search(table_content)
        header_content = header_match.group(0).strip() if header_match else ""
    add_block(
        "table",
        table_content,
        table_content,
        table_headers=headers,
        table_rows=raw_rows,
        table_header_content=header_content,
    )
    for raw_row, row_content in zip(raw_rows, row_contents):
        add_block("table_row", row_content, raw_row, primary=False, table_headers=headers)


def _clean_html(value: str) -> str:
    return re.sub(r"\s+", " ", _HTML_TAG_PATTERN.sub("", value)).strip()
