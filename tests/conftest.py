"""pytest 共享装置：把仓库根加入 sys.path，使 tests/ 能直接 import 生产代码。

生产代码使用 `processor.*` / `utils.*` / `backend.*` 绝对导入，依赖仓库根在 sys.path 上；
pytest 默认只把测试文件所在目录插入 sys.path，因此这里显式补齐，避免调用方必须从仓库根运行。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
