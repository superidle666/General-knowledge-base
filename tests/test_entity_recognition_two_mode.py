"""实体识别二模式（disabled/required）收敛的静态守卫。

配套 P0：取消 optional 模式、删除孤立的 item_name 节点与独立集合配置。这里全部用 AST /
文本静态校验，不实例化任何会拉起 Milvus / LLM / Redis 的生产对象，锁定"不得再回潮"：

1. 导入图的模式路由字典只剩 disabled/required 两项。
2. 图里既不再注册 optional 节点，也不再注册孤立的 node_item_name_recognition。
3. 全仓不再有对已删除的 item_name_collection / ITEM_NAME_COLLECTION 的引用。
4. 后端 schema 的实体模式校验 pattern 已收敛为二值。
"""

from __future__ import annotations

import ast
from pathlib import Path

from graph_introspection import (
    IMPORT_MAIN_GRAPH,
    IMPORT_WORKER,
    PROJECT_ROOT,
    collection_literal,
    registered_node_names,
)

API_SCHEMAS = PROJECT_ROOT / "backend" / "app" / "api" / "schemas.py"

# 只扫生产代码目录，避开 .venv / frontend 依赖树与本守卫自身（文中字面量会自匹配）。
SCAN_DIRS = ["processor", "backend", "config", "utils", "tool"]


def _module_dict_keys(path: Path, variable: str) -> set[str]:
    """读取模块级 dict 字面量的键集合，兼容 `X: dict = {...}` 注解赋值。"""
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        elif isinstance(node, ast.Assign) and node.targets:
            target, value = node.targets[0], node.value
        else:
            continue
        if isinstance(target, ast.Name) and target.id == variable and isinstance(value, ast.Dict):
            return {key.value for key in value.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)}
    raise AssertionError(f"{path.as_posix()} 中找不到模块级 dict {variable}，可能已被重构，请同步更新用例")


def test_entity_recognition_mode_router_has_only_two_modes() -> None:
    """导入图的实体识别路由字典必须恰好是 disabled/required 二值。"""
    modes = _module_dict_keys(IMPORT_MAIN_GRAPH, "ENTITY_RECOGNITION_NODE_NAMES")
    assert modes == {"disabled", "required"}, f"实体识别模式应仅二值，实际为：{sorted(modes)}"


def test_optional_and_legacy_entity_nodes_are_unregistered() -> None:
    """optional 节点与孤立的 node_item_name_recognition 都不得再出现在图注册表里。"""
    import_nodes = registered_node_names(IMPORT_MAIN_GRAPH)
    assert "node_entity_recognition_optional" not in import_nodes
    assert "node_item_name_recognition" not in import_nodes
    assert "node_entity_recognition_disabled" in import_nodes
    assert "node_entity_recognition_required" in import_nodes


def test_worker_progress_map_drops_optional_node() -> None:
    """进度表若还留着 optional 节点，说明删除不彻底。"""
    mapped = collection_literal(IMPORT_WORKER, "progress_map")
    assert "node_entity_recognition_optional" not in mapped


def test_no_reference_to_removed_item_name_collection() -> None:
    """全仓生产与用例代码都不得再引用已删除的独立实体集合配置。"""
    offenders: list[str] = []
    for sub in SCAN_DIRS:
        base = PROJECT_ROOT / sub
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "item_name_collection" in text or "ITEM_NAME_COLLECTION" in text:
                offenders.append(path.relative_to(PROJECT_ROOT).as_posix())
    assert not offenders, f"以下文件仍引用已删除的 item_name_collection：{sorted(offenders)}"


def test_deleted_orphan_files_stay_deleted() -> None:
    """P0 删除的孤立文件不得被重新引入。"""
    removed = [
        Path("processor") / "import_processor" / "nodes" / "node_entity_recognition_optional.py",
        Path("processor") / "import_processor" / "nodes" / "node_item_name_recognition.py",
        Path("processor") / "query_processor" / "nodes" / "node_item_name_confirm.py",
        Path("processor") / "query_processor" / "prompt" / "item_name_confirm.py",
    ]
    present = [p.as_posix() for p in removed if (PROJECT_ROOT / p).exists()]
    assert not present, f"以下应删除的孤立文件仍存在：{present}"


def test_backend_schema_pattern_is_two_mode() -> None:
    """后端实体模式校验 pattern 必须收敛为 disabled|required。"""
    text = API_SCHEMAS.read_text(encoding="utf-8")
    assert "disabled|optional|required" not in text, "schema 校验仍允许 optional 模式"
    assert "^(disabled|required)$" in text, "schema 校验未收敛为二模式 pattern"
