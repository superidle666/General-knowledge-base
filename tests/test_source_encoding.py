"""源码编码守卫：防止"中文被写成 ?"这类不可逆事故再次发生。

历史事故：processor/ 下曾有两个文件的全部中文注释与报错文案被替换成 ASCII 问号，
且已经提交进 git，无法从历史恢复（问号就是真实字节）。同类事故还包括 U+FEFF BOM
导致 ast.parse / 代码工具链直接报错。这三类问题都能在 CI 里用纯文本扫描拦住。

本文件内的两个正则在自身源码上不会自匹配，因此无需把本文件排除在检查之外。
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 参与守卫的源码目录；test/ 是本地临时脚本目录，不纳入仓库也不纳入检查。
SCANNED_DIRECTORIES = ("backend", "config", "processor", "tests", "tool", "utils")

# 连续 4 个以上 ASCII 问号：中文被破坏后的典型形态。
MOJIBAKE_PATTERN = re.compile(r"\?{4,}")
# 字符串字面量里的中文 unicode 转义；能绕过乱码检查，但让人类读者和全文检索失效。
UNICODE_ESCAPE_PATTERN = re.compile(r"\\u([0-9a-fA-F]{4})")
CJK_START = 0x4E00
CJK_END = 0x9FFF
UTF8_BOM = b"\xef\xbb\xbf"
REPLACEMENT_CHAR = "\ufffd"


def _iter_source_files() -> list[Path]:
    """收集受守卫覆盖的 Python 源文件。"""
    files: list[Path] = []
    for directory in SCANNED_DIRECTORIES:
        base = PROJECT_ROOT / directory
        if not base.exists():
            continue
        files.extend(
            path
            for path in base.rglob("*.py")
            if "__pycache__" not in path.parts and ".venv" not in path.parts
        )
    return sorted(files)


def test_source_files_are_discovered() -> None:
    """守卫本身不能空转：至少要扫到导入与检索两条链路的文件。"""
    files = _iter_source_files()
    assert len(files) > 50, f"仅扫描到 {len(files)} 个文件，检查 SCANNED_DIRECTORIES 是否漏配"


def test_no_chinese_text_replaced_by_question_marks() -> None:
    """不允许出现连续问号形式的中文丢失。"""
    offenders: list[str] = []
    for path in _iter_source_files():
        text = path.read_bytes().decode("utf-8", errors="replace")
        matches = MOJIBAKE_PATTERN.findall(text)
        if matches:
            rel = path.relative_to(PROJECT_ROOT).as_posix()
            offenders.append(f"{rel}: {len(matches)} 处，最长 {max(len(m) for m in matches)} 个问号")
    assert not offenders, "以下文件的中文已退化为问号，需人工恢复：\n" + "\n".join(offenders)


def _escaped_chinese_lines(path: Path) -> list[str]:
    """找出用 unicode 转义写中文的字符串字面量所在行。

    只检查 tokenize 的 STRING token，不扫全文行：node_document_split.py 里有合法解释
    ensure_ascii 输出形式的注释，本守卫自己的模式串也含转义形式，只看字符串才能避开误报。
    """
    text = path.read_bytes().decode("utf-8", errors="replace")
    offenders: list[str] = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return [f"{path.as_posix()}: 无法 tokenize，请手工检查编码"]
    for token in tokens:
        if token.type != tokenize.STRING:
            continue
        for match in UNICODE_ESCAPE_PATTERN.finditer(token.string):
            code_point = int(match.group(1), 16)
            if CJK_START <= code_point <= CJK_END:
                offenders.append(f"{path.relative_to(PROJECT_ROOT).as_posix()}:{token.start[0]}")
                break
    return offenders


def test_no_unicode_escaped_chinese() -> None:
    """不允许用 unicode 转义写中文，文案必须是可读明文。"""
    offenders: list[str] = []
    for path in _iter_source_files():
        offenders.extend(_escaped_chinese_lines(path))
    assert not offenders, "以下字符串用 unicode 转义写中文：\n" + "\n".join(offenders)


def test_no_utf8_bom_and_broken_characters() -> None:
    """BOM 会让 ast.parse 等工具链直接失败；替换符说明文件已被破坏过。"""
    offenders: list[str] = []
    for path in _iter_source_files():
        raw = path.read_bytes()
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        if raw.startswith(UTF8_BOM):
            offenders.append(f"{rel}: 文件头存在 UTF-8 BOM")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            offenders.append(f"{rel}: 不是合法 UTF-8（{exc}）")
            continue
        if REPLACEMENT_CHAR in text:
            offenders.append(f"{rel}: 含 U+FFFD 替换符")
    assert not offenders, "以下文件编码不干净：\n" + "\n".join(offenders)
