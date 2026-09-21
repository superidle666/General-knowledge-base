# -*- coding: utf-8 -*-
"""
权限注册表及同步逻辑。
@author: 项目维护者
@date: 2026-09-02
@desc: 定义可授予权限及其父子层级，并在应用启动时同步到数据库。
@business: 父级管理权限包含该模块全部子权限，数据库不能自由创建未注册权限。
"""
"""权限注册表及其数据库同步逻辑。"""

from collections.abc import Iterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.models import Permission



# 权限定义的唯一来源：接口授权和后台分配都必须使用此注册表，防止出现无实际校验能力的权限。
PERMISSION_REGISTRY = {
    "library:manage": {
        "name": "管理知识库",
        "children": {
            "library:read": "查看知识库",
            "library:create": "创建知识库",
            "library:modify": "修改知识库",
            "library:delete": "删除知识库",
        },
    },
    "document:manage": {
        "name": "管理文档",
        "children": {
            "document:view": "查看文档",
            "document:upload": "上传文档",
            "document:modify": "修改文档",
            "document:delete": "删除文档",
            "document:download": "下载文档",
        },
    },
    "user:manage": {
        "name": "管理用户",
        "children": {
            "user:view": "查看用户",
            "user:create": "创建用户",
            "user:modify": "修改用户",
            "user:delete": "删除用户",
            "user:assign_role": "分配用户角色",
        },
    },
    "role:manage": {
        "name": "管理角色",
        "children": {
            "role:view": "查看角色",
            "role:create": "创建角色",
            "role:modify": "修改角色",
            "role:delete": "删除角色",
            "role:assign_permission": "分配角色权限",
        },
    },
    "permission:manage": {
        "name": "管理权限",
        "children": {
            "permission:view": "查看权限",
            "permission:toggle": "启用/停用权限",
        },
    },
    "session:manage": {
        "name": "管理会话",
        "children": {
            "session:view_all": "查看全部会话",
            "session:delete": "删除会话",
        },
    },
}

REGISTERED_PERMISSION_CODES = frozenset(
    code
    for parent_code, definition in PERMISSION_REGISTRY.items()
    for code in (parent_code, *definition["children"])
)


def iter_registered_permissions() -> Iterator[tuple[str, str, str | None]]:
    """按父级优先的顺序产出注册权限。"""

    for parent_code, definition in PERMISSION_REGISTRY.items():
        yield parent_code, definition["name"], None
        for child_code, child_name in definition["children"].items():
            yield child_code, child_name, parent_code


def is_registered_permission(code: str) -> bool:
    """判断权限编码是否来自代码注册表。"""

    return code in REGISTERED_PERMISSION_CODES



# NOTE: 分配父级“管理”权限时，鉴权阶段自动展开全部子权限，前端勾选逻辑与后端鉴权保持一致。
def expand_permissions(codes: set[str] | frozenset[str]) -> frozenset[str]:
    """Return effective permissions, including children of selected managers."""

    expanded = set(codes)
    for parent_code, definition in PERMISSION_REGISTRY.items():
        if parent_code in expanded:
            expanded.update(definition["children"])
    return frozenset(expanded)

async def sync_permission_registry(session: AsyncSession) -> None:
    """将代码注册表同步到数据库，并维护权限父子关系。"""

    result = await session.execute(
        select(Permission).where(Permission.code.in_(REGISTERED_PERMISSION_CODES))
    )
    permissions_by_code = {permission.code: permission for permission in result.scalars()}

    for parent_code, definition in PERMISSION_REGISTRY.items():
        parent = permissions_by_code.get(parent_code)
        if parent is None:
            parent = Permission(code=parent_code, name=definition["name"])
            session.add(parent)
            await session.flush()
            permissions_by_code[parent_code] = parent
        else:
            parent.name = definition["name"]
            parent.description = None
            parent.parent_id = None
            parent.deleted_at = None
            parent.deleted_by = None

        for child_code, child_name in definition["children"].items():
            child = permissions_by_code.get(child_code)
            if child is None:
                child = Permission(
                    code=child_code,
                    name=child_name,
                    parent_id=parent.id,
                )
                session.add(child)
                permissions_by_code[child_code] = child
            else:
                child.name = child_name
                child.description = None
                child.parent_id = parent.id
                child.deleted_at = None
                child.deleted_by = None

    await session.commit()