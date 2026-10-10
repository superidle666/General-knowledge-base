# -*- coding: utf-8 -*-
"""文件系统目录名片段的安全清洗工具。

上传目录与 MinIO 图片目录都按"人类可读 + 唯一"两段命名：光靠库 ID 或光靠文件名都难辨
认，这里把知识库名清洗成一段文件系统安全的 slug，再配合库 ID 前缀使用，兼顾可读与唯一。
"""

from __future__ import annotations

import re

# Windows/Linux 共用的非法字符，外加控制字符一并挡掉（中文保留：现代文件系统均 UTF-8 安全）。
_INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")

# 目录名截断上限，避免整段路径过长；ID 前缀不受此限制。
DEFAULT_MAX_LENGTH = 30


def slugify(name: str, *, max_length: int = DEFAULT_MAX_LENGTH) -> str:
    """把任意名称清洗成一段文件系统安全的目录名片段。

    规则：去非法字符 → 连续空白折叠为单个下划线 → 截断到 max_length → 去掉首尾点/下划线。
    清洗后为空时回退为占位串，保证目录名始终非空。
    """
    cleaned = _INVALID_CHARS.sub("", str(name or "")).strip()
    cleaned = _WHITESPACE.sub("_", cleaned)
    cleaned = cleaned[:max_length].strip("._")
    return cleaned or "library"
