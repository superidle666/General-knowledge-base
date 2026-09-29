"""pytest 共享装置：把仓库根加入 sys.path，使 tests/ 能直接 import 生产代码。

生产代码使用 `processor.*` / `utils.*` / `backend.*` 绝对导入，依赖仓库根在 sys.path 上；
pytest 默认只把测试文件所在目录插入 sys.path，因此这里显式补齐，避免调用方必须从仓库根运行。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _isolate_runtime_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认阻断运行时配置快照（P0-1）对真实 Redis 的访问，并重置进程内缓存。

    开发机 Redis 是真实存在的，不拦的话单测会读到库上的真快照，行为随环境漂移；
    统一把 _get_redis_client 换成抛错，_fetch_snapshot 按设计走「不可用 → env 兜底」路径。
    需要快照行为的用例自行 monkeypatch _fetch_snapshot（在本 fixture 之后生效，可覆盖）。
    """
    from utils import runtime_settings

    monkeypatch.setattr(runtime_settings, "_snapshot", None)
    monkeypatch.setattr(runtime_settings, "_snapshot_at", 0.0)

    def _unreachable() -> None:
        raise RuntimeError("unit tests must not touch the real Redis")

    monkeypatch.setattr(runtime_settings, "_get_redis_client", _unreachable)
