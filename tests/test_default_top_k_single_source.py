"""top_k 缺省口径的单一来源守卫。

tests/test_default_top_k_single_source.py

背景：出口宽度（送生成与展示的来源条数）由请求里的 top_k 决定，而这个缺省值此前被抄在
接口 schema、node_prepare_query、node_rerank 的两个方法、node_web_search_mcp 以及前端请求体
里各写一遍。抬一档要同时改六处，而漏改任何一处的表现**不是报错**，是"候选预算按一套缺省取、
出口窗口按另一套缺省收"这种口径漂移，只能靠逐题 trace 才发现。这里把"只许住在一处"钉成静态守卫。

同时钉住前端：来源面板一直按接口返回的 sources 实际长度渲染，前端任何写死的 top_k 数字都会
把服务端抬宽的证据宽度钉回原样（历史上就是 index.vue 里那个 top_k: 5）。
"""

from __future__ import annotations

import ast
from pathlib import Path

from backend.app.api.schemas import QueryRequest
from processor.query_processor.nodes.node_rerank import (
    RERANK_MAX_TOPK,
    RERANK_MIN_TOPK,
    NodeRerank,
)
from processor.query_processor.nodes.node_web_search_mcp import NodeWebSearchMcp
from processor.query_processor.state import DEFAULT_TOP_K

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCANNED_PY_DIRS = (
    PROJECT_ROOT / "processor" / "query_processor",
    PROJECT_ROOT / "backend" / "app",
)
SCANNED_TS_GLOBS = (
    "frontend/src/**/*.vue",
    "frontend/src/**/*.ts",
)


def _top_k_default_expressions(tree: ast.AST) -> list[tuple[ast.AST, ast.AST]]:
    """找出所有 `state.get("top_k") or <缺省表达式>` 形态，返回 (整句, 缺省右值)。

    同时覆盖 `int(state.get("top_k") or X)`：那才是本仓库里实际写法。
    """
    found: list[tuple[ast.AST, ast.AST]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
            continue
        left, right = node.values[0], node.values[-1]
        if not (isinstance(left, ast.Call) and isinstance(left.func, ast.Attribute)
                and left.func.attr == "get"):
            continue
        if not left.args or not isinstance(left.args[0], ast.Constant):
            continue
        if left.args[0].value != "top_k":
            continue
        found.append((node, right))
    return found


def test_default_top_k_is_ten() -> None:
    """默认读满允许的证据宽度：DEFAULT_TOP_K 与接口上限、重排硬上限同值。"""
    assert DEFAULT_TOP_K == 10
    assert DEFAULT_TOP_K == RERANK_MAX_TOPK, "默认值与硬上限脱钩时，抬默认值不会真的变宽（静默无效）"


def test_missing_top_k_in_state_falls_back_to_single_default() -> None:
    """缺省 top_k 时，窗口/候选预算/搜索条数三处必须给出同一个缺省值推出的结果。"""
    expected_cap = max(RERANK_MIN_TOPK, min(DEFAULT_TOP_K, RERANK_MAX_TOPK))
    assert NodeRerank._final_window_cap({}) == expected_cap

    local_limit, web_limit = NodeRerank._candidate_limits({})
    assert web_limit == min(max(DEFAULT_TOP_K, RERANK_MIN_TOPK), 10)
    assert local_limit == max(RERANK_MAX_TOPK, min(DEFAULT_TOP_K * 3, 30))
    assert local_limit >= expected_cap, "候选少于窗口时断崖看不到下降段，截断退化成末尾一刀"

    assert NodeWebSearchMcp._resolve_count({}) == max(min(DEFAULT_TOP_K * 2, 10), 1)


def test_entry_node_normalizes_top_k_at_all() -> None:
    """入口节点必须把 top_k 归一写回 state（静态断言，跑 process 会拖出整条闸链与 LLM 调用）。

    下游四个节点都是 `state.get("top_k") or 缺省` 的防御式读法；如果入口这一步被删了，
    各处不会报错、只会各自拿一份缺省值，恰好是本守卫要防的漂移形态。
    """
    path = PROJECT_ROOT / "processor" / "query_processor" / "nodes" / "node_prepare_query.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    normalized = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not (isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant)
                and target.slice.value == "top_k"):
            continue
        names = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
        assert "DEFAULT_TOP_K" in names, f"入口归一不再引用 DEFAULT_TOP_K（{path.name}:{node.lineno}）"
        normalized = True
    assert normalized, "node_prepare_query 里找不到 top_k 归一赋值，守卫已失效"


def test_api_schema_default_matches_engine() -> None:
    """接口默认值必须等于引擎常量：两条链路（走接口 / 直调工作流）不能有各自的 5。"""
    field = QueryRequest.model_fields["top_k"]
    assert field.default == DEFAULT_TOP_K
    assert field.metadata, "top_k 缺少 ge/le 约束，客户端可传任意大的宽度放大精排成本"


def test_no_hardcoded_top_k_default_left_in_python_sources() -> None:
    """`state.get("top_k") or <数字>` 一律禁止：缺省只能引用 DEFAULT_TOP_K。"""
    offenders: list[str] = []
    references = 0
    for base in SCANNED_PY_DIRS:
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node, right in _top_k_default_expressions(tree):
                if isinstance(right, ast.Constant) and isinstance(right.value, (int, float)):
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}")
                elif isinstance(right, ast.Name) and right.id == "DEFAULT_TOP_K":
                    references += 1
    assert not offenders, f"top_k 缺省又被人抄成字面量了：{offenders}"
    # 防空过：改名或写法变化导致一条都没扫到时，本守卫必须失效而不是静默通过
    assert references >= 4, f"只扫到 {references} 处 DEFAULT_TOP_K 缺省引用，守卫可能已失效"


def test_frontend_does_not_pin_top_k_number() -> None:
    """前端请求体里不得出现写死的 top_k 数字。"""
    pinned: list[str] = []
    scanned = 0
    for pattern in SCANNED_TS_GLOBS:
        for path in sorted(PROJECT_ROOT.glob(pattern)):
            if "node_modules" in path.parts:
                continue
            scanned += 1
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                stripped = line.strip()
                if stripped.startswith("//") or stripped.startswith("*"):
                    continue
                if "top_k:" in stripped and any(char.isdigit() for char in stripped.split("top_k:")[1]):
                    pinned.append(f"{path.relative_to(PROJECT_ROOT)}:{lineno}")
    assert scanned > 10, "前端源码没扫到，守卫已失效"
    assert not pinned, f"前端把来源条数写死了（会钉住服务端宽度）：{pinned}"
