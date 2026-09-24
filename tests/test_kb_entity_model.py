"""kb_entity 表与迁移一致性的静态/离线守卫（P1）。

不连 MySQL：只用 ORM 元数据、Alembic 迁移源码做交叉校验，锁定首期实体词表的形状，
防止模型与迁移漂移、迁移链分叉、以及关系声明拼写错误在运行期才暴露。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import configure_mappers

from backend.app.db import models

VERSIONS_DIR = Path(__file__).resolve().parents[1] / "backend" / "alembic" / "versions"
ENTITY_MIGRATION = VERSIONS_DIR / "20260924_0006_add_kb_entity.py"

EXPECTED_COLUMNS = {
    "id", "library_id", "document_id", "name", "normalized_name",
    "aliases", "source", "maybe_series", "created_at", "updated_at",
}


def test_kb_entity_table_shape() -> None:
    """表名、列集合、库级隔离索引与文档唯一约束必须符合首期设计。"""
    table = models.Entity.__table__
    assert table.name == "kb_entity"
    assert set(table.columns.keys()) == EXPECTED_COLUMNS

    # 首期一篇一条：document_id 唯一
    doc_unique = [
        c for c in table.constraints
        if isinstance(c, UniqueConstraint) and {col.name for col in c.columns} == {"document_id"}
    ]
    assert doc_unique, "kb_entity 必须对 document_id 建唯一约束"

    # 库级隔离 + 归一化名对齐检索的复合索引，列顺序敏感
    index = next((i for i in table.indexes if i.name == "ix_kb_entity_library_name"), None)
    assert index is not None and [c.name for c in index.columns] == ["library_id", "normalized_name"]

    # 外键只指向库与文档
    assert {fk.target_fullname for fk in table.foreign_keys} == {"kb_library.id", "kb_document.id"}


def test_migration_columns_match_model() -> None:
    """迁移里 create_table 的列必须与模型列完全一致，避免模型改了忘改迁移。"""
    text = ENTITY_MIGRATION.read_text(encoding="utf-8")
    migrated = set(re.findall(r'sa\.Column\(\s*"([A-Za-z0-9_]+)"', text))
    assert migrated == EXPECTED_COLUMNS, f"迁移列与模型不一致：{sorted(migrated ^ EXPECTED_COLUMNS)}"


def test_entity_relationships_configure() -> None:
    """强制解析所有关系，捕获 back_populates 拼写错误而不必等到运行期。"""
    configure_mappers()
    assert "entities" in models.Library.__mapper__.relationships
    assert "entities" in models.Document.__mapper__.relationships
    assert models.Entity.__mapper__.relationships["library"].back_populates == "entities"


def _const(node: ast.stmt, name: str) -> object | None:
    """读取模块级 `name = <常量>` 或 `name: T = <常量>` 的字面量值。"""
    if isinstance(node, ast.Assign):
        targets, value = node.targets, node.value
    elif isinstance(node, ast.AnnAssign):
        targets, value = [node.target], node.value
    else:
        return None
    for target in targets:
        if isinstance(target, ast.Name) and target.id == name and isinstance(value, ast.Constant):
            return value.value
    return None


def test_alembic_head_is_single_and_is_kb_entity() -> None:
    """迁移链必须只有一个 head，且为本次新增的 kb_entity 迁移（防分叉）。"""
    revisions: set[str] = set()
    referenced: set[str] = set()
    for path in VERSIONS_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rev = down = None
        for node in tree.body:
            if (value := _const(node, "revision")) is not None:
                rev = value
            elif (value := _const(node, "down_revision")) is not None:
                down = value
        assert isinstance(rev, str), f"{path.name} 缺少 revision"
        revisions.add(rev)
        if isinstance(down, str):
            referenced.add(down)
    heads = revisions - referenced
    assert heads == {"20260924_0006"}, f"迁移链 head 应唯一且为 kb_entity，实际：{sorted(heads)}"
