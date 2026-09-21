# -*- coding: utf-8 -*-
"""
后台管理数据接口。
@author: 项目维护者
@date: 2026-09-02
@desc: 提供用户、角色、权限、知识库、文档和操作日志管理能力。
@business: 权限由代码注册表约束；软删除数据保留审计与恢复所需历史。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.api.schemas import (
    ChunkOut, DocumentCreate, DocumentDetailOut, DocumentListOut, DocumentTagsUpdate,
    DocumentUpdate, DocumentUploadOut, LibraryCreate, LibraryOut, LibraryUpdate,
    ImportTaskOut, MemberCreate, MemberOut, OperationLogOut, OperationLogPageOut, PermissionOut, PermissionUpdate,
    RoleCreate, RoleOut, RoleUpdate, TagCreate, TagOut, TagUpdate, UserCreate,
    UserOut, UserUpdate,
)
from backend.app.core.audit import log_operation
from backend.app.core.auth import AuthenticatedUser, get_current_user, hash_password, require_management_access
from backend.app.core.permissions import REGISTERED_PERMISSION_CODES, is_registered_permission
from backend.app.db.models import (
    Document, DocumentChunk, DocumentTag, ImportTask, Library, LibraryMember, OperationLog,
    Permission, Role, SystemSetting, Tag, User,
)
from backend.app.db.session import get_db
from config.milvus_config import milvus_config
from utils.import_queue import enqueue_import_task, request_import_cancellation
from utils.milvus_utils import get_milvus_client
from utils.mongo_history_utils import get_history_mongo_tool

router = APIRouter(prefix="/api/v1", tags=["mysql"], dependencies=[Depends(require_management_access)])


@router.get("/dashboard/overview")
async def dashboard_overview(session: AsyncSession = Depends(get_db)):
    """返回管理端仪表盘实时汇总数据。

    Note:
        问答会话统计来自 MongoDB，操作活动来自 MySQL；没有助手回答时命中率固定为 0。
    """
    now = datetime.now()
    month_start = datetime(now.year, now.month, 1).timestamp()
    mongo = get_history_mongo_tool()

    session_count = await asyncio.to_thread(
        mongo.chat_session.count_documents,
        {"updated_at": {"$gte": month_start}},
    )
    assistant_count = await asyncio.to_thread(
        mongo.chat_message.count_documents,
        {"role": "assistant", "ts": {"$gte": month_start}},
    )
    answered_count = await asyncio.to_thread(
        mongo.chat_message.count_documents,
        {"role": "assistant", "ts": {"$gte": month_start}, "sources.0": {"$exists": True}},
    )
    hit_rate = round(answered_count / assistant_count * 100, 1) if assistant_count else 0

    logs = await session.execute(
        select(OperationLog, User.display_name)
        .outerjoin(User, User.id == OperationLog.user_id)
        .order_by(OperationLog.id.desc())
        .limit(3)
    )
    log_rows = logs.all()
    resource_names = await operation_log_resource_names(session, [item for item, _ in log_rows])
    activities = [
        {
            "operation": item.operation,
            "resource_type": item.resource_type,
            "resource_display_name": resource_names.get(item.id),
            "user_display_name": display_name,
            "created_at": item.created_at,
        }
        for item, display_name in log_rows
    ]
    return {"session_count": session_count, "hit_rate": hit_rate, "activities": activities}

async def get_or_404(session: AsyncSession, model: type, item_id: int, label: str):
    """按主键读取管理对象。资源不存在时统一转换为业务 404，避免接口重复处理空值。
    """
    item = await session.get(model, item_id)
    if item is None:
        raise HTTPException(404, f"{label}不存在")
    return item


async def commit(session: AsyncSession, duplicate_message: str) -> None:
    """提交管理操作事务。唯一约束冲突会先回滚事务，再转换为指定的 400 业务提示。
    """
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(400, duplicate_message) from exc


log = log_operation

ALLOWED_DOCUMENT_EXTENSIONS = frozenset({".pdf", ".md"})
IMPORT_TASK_STATUSES = frozenset({"queued", "processing", "completed", "failed", "cancelled"})

def validate_library_rules(payload: dict) -> None:
    """校验知识库导入与切分规则，确保保存的配置不超出当前处理器支持范围。
    """
    allowed = payload.get("allowed_file_types")
    if allowed is not None:
        normalized = {str(item).strip().lower().lstrip(".") for item in allowed if str(item).strip()}
        if normalized - {"pdf", "md"}:
            raise HTTPException(400, "当前仅支持 PDF 和 Markdown 文件")
    mode = payload.get("entity_recognition_mode")
    chunking_config = payload.get("chunking_config")
    if chunking_config is not None:
        if not isinstance(chunking_config, dict):
            raise HTTPException(400, "切片配置必须为对象")
        unknown_keys = set(chunking_config) - {"chunk_size", "chunk_overlap"}
        if unknown_keys:
            raise HTTPException(400, "切片配置包含不支持的字段")
        chunk_size = chunking_config.get("chunk_size")
        chunk_overlap = chunking_config.get("chunk_overlap", 0)
        if not isinstance(chunk_size, int) or chunk_size < 1:
            raise HTTPException(400, "切片长度必须为正整数")
        if not isinstance(chunk_overlap, int) or chunk_overlap < 0 or chunk_overlap >= chunk_size:
            raise HTTPException(400, "切片重叠必须为非负整数且小于切片长度")




# NOTE: 日志中的资源名称必须批量解析，既避免列表查询产生 N+1 请求，也要保留软删除资源的历史名称。
async def operation_log_resource_names(
    session: AsyncSession, logs: list[OperationLog]
) -> dict[int, str]:
    """批量解析操作日志关联资源的展示名称。

    Note:
        资源已删除或历史缺失时保留空名称，由调用方回退展示资源类型。
    """
    names: dict[int, str] = {}
    model_specs = {
        "user": (User, User.display_name),
        "auth_session": (User, User.display_name),
        "role": (Role, Role.name),
        "permission": (Permission, Permission.name),
        "library": (Library, Library.name),
        "document": (Document, Document.title),
        "tag": (Tag, Tag.name),
    }
    for resource_type, (model, name_column) in model_specs.items():
        relevant = [
            item for item in logs
            if item.resource_type == resource_type and (item.resource_id or "").isdigit()
        ]
        ids = [int(item.resource_id) for item in relevant]
        if not ids:
            continue
        rows = await session.execute(
            select(model.id, name_column).where(model.id.in_(ids))
        )
        resolved = {
            str(item_id): str(display_name)
            for item_id, display_name in rows
            if display_name is not None
        }
        for item in relevant:
            if item.resource_id in resolved:
                names[item.id] = resolved[item.resource_id]

    import_task_logs = [
        item for item in logs
        if item.resource_type == "import_task" and (item.resource_id or "").isdigit()
    ]
    if import_task_logs:
        ids = [int(item.resource_id) for item in import_task_logs]
        rows = await session.execute(select(ImportTask.id, Document.title).join(Document, Document.id == ImportTask.document_id).where(ImportTask.id.in_(ids)))
        resolved = {str(task_id): str(title) for task_id, title in rows if title is not None}
        for item in import_task_logs:
            if item.resource_id in resolved:
                names[item.id] = resolved[item.resource_id]

    setting_logs = [
        item for item in logs
        if item.resource_type == "system_setting" and item.resource_id
    ]
    if setting_logs:
        keys = [item.resource_id for item in setting_logs]
        rows = await session.execute(
            select(SystemSetting.setting_key, SystemSetting.display_name)
            .where(SystemSetting.setting_key.in_(keys))
        )
        resolved = {
            key: display_name for key, display_name in rows if display_name is not None
        }
        for item in setting_logs:
            if item.resource_id in resolved:
                names[item.id] = resolved[item.resource_id]
    return names



def user_out(user: User) -> dict:
    """将用户实体转换为管理端安全响应，不返回密码摘要等认证内部字段。
    """
    return {
        "id": user.id, "username": user.username, "email": user.email, "phone": user.phone,
        "display_name": user.display_name, "avatar_url": user.avatar_url, "status": user.status,
        "last_login_at": user.last_login_at,
        "roles": [role.code for role in user.roles if role.deleted_at is None],
    }


def role_out(role: Role) -> dict:
    """将角色实体转换为管理端响应，包含角色基础信息和已分配权限编码。
    """
    return {
        "id": role.id, "code": role.code, "name": role.name, "description": role.description,
        "status": role.status, "deleted_at": role.deleted_at, "deleted_by": role.deleted_by,
        "permissions": [
            permission.code for permission in role.permissions
            if permission.deleted_at is None and is_registered_permission(permission.code)
        ],
    }


def permission_out(permission: Permission) -> dict:
    """将权限实体转换为管理端响应，包含代码注册的权限编码、名称和说明。
    """
    return {
        "id": permission.id, "code": permission.code, "name": permission.name,
        "description": permission.description, "status": permission.status,
        "deleted_at": permission.deleted_at, "deleted_by": permission.deleted_by,
        "parent_id": permission.parent_id,
    }


def document_out(document: Document, *, chunk_count: int = 0, tags: list[str] | None = None, library_name: str | None = None) -> dict:
    """组装文档管理响应，补充标签和切片数量等列表、详情展示字段。
    """
    return {
        "id": document.id, "library_id": document.library_id, "title": document.title,
        "original_filename": document.original_filename, "storage_key": document.storage_key,
        "mime_type": document.mime_type, "file_size": document.file_size,
        "file_hash": document.file_hash, "version": document.version, "status": document.status,
        "parse_error": document.parse_error, "uploaded_by": document.uploaded_by,
        "published_at": document.published_at, "deleted_by": document.deleted_by, "created_at": document.created_at,
        "updated_at": document.updated_at, "chunk_count": chunk_count, "tags": tags or [],
        "library_name": library_name,
    }



# NOTE: 角色只能引用代码注册表中的有效权限，防止后台写入没有对应接口授权规则的权限编码。
async def resolve_registered_permissions(session: AsyncSession, codes: list[str]) -> list[Permission]:
    """加载角色可分配的系统权限。权限必须来自代码注册表，未注册或不可用编码返回 400。
    """
    requested = list(dict.fromkeys(codes))
    if set(requested) - REGISTERED_PERMISSION_CODES:
        raise HTTPException(400, "只能分配权限注册表中的权限")
    if not requested:
        return []
    result = await session.execute(
        select(Permission).where(Permission.code.in_(requested), Permission.deleted_at.is_(None))
    )
    permissions = list(result.scalars())
    if len(permissions) != len(requested):
        raise HTTPException(400, "部分权限不存在、已删除或尚未完成同步")
    return permissions


async def registered_permission_or_404(session: AsyncSession, permission_id: int) -> Permission:
    """读取代码注册表中的权限。权限不存在或未注册时返回 404。
    """
    permission = await get_or_404(session, Permission, permission_id, "权限")
    if not is_registered_permission(permission.code):
        raise HTTPException(404, "权限不存在")
    return permission


@router.get("/users", response_model=list[UserOut])
async def list_users(include_deleted: bool = Query(False), session: AsyncSession = Depends(get_db)):
    """查询管理端用户列表。默认隐藏软删除账号，保留其历史记录用于审计与恢复。
    """
    statement = select(User).options(selectinload(User.roles)).order_by(User.id.desc())
    if not include_deleted:
        statement = statement.where(User.deleted_at.is_(None))
    return [user_out(item) for item in (await session.execute(statement)).scalars().unique()]


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(payload: UserCreate, session: AsyncSession = Depends(get_db)):
    """创建管理端用户、保存密码哈希并分配初始角色，同时写入操作审计日志。
    """
    result = await session.execute(
        select(Role).where(Role.code.in_(list(dict.fromkeys(payload.role_codes))), Role.deleted_at.is_(None))
    )
    roles = list(result.scalars())
    if len(roles) != len(set(payload.role_codes)):
        raise HTTPException(400, "存在不可用的角色")
    user = User(
        username=payload.username, display_name=payload.display_name, email=payload.email,
        phone=payload.phone,
        password_hash=hash_password(f"{payload.username[:3]}{payload.phone}"),
        roles=roles,
    )
    session.add(user)
    await session.flush()
    await log(session, "create", "user", user.id)
    await commit(session, "用户名、邮箱或手机号已存在")
    return user_out(user)


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(user_id: int, payload: UserUpdate, session: AsyncSession = Depends(get_db)):
    # Preload the collection before replacing this async many-to-many relationship.
    """更新用户资料、状态和角色分配。角色必须有效，普通更新不会恢复软删除账号。
    """
    result = await session.execute(
        select(User).options(selectinload(User.roles)).where(User.id == user_id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(404, "用户不存在")
    for field, value in payload.model_dump(exclude_unset=True, exclude={"role_codes", "password"}).items():
        setattr(user, field, value)
    if payload.password is not None:
        user.password_hash = hash_password(payload.password)
    if payload.role_codes is not None:
        result = await session.execute(
            select(Role).where(Role.code.in_(list(dict.fromkeys(payload.role_codes))), Role.deleted_at.is_(None))
        )
        roles = list(result.scalars())
        if len(roles) != len(set(payload.role_codes)):
            raise HTTPException(400, "存在不可用的角色")
        user.roles = roles
    await log(session, "update", "user", user.id)
    await commit(session, "用户名、邮箱或手机号已存在")
    await session.refresh(user, attribute_names=["roles"])
    return user_out(user)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(user_id: int, session: AsyncSession = Depends(get_db)):
    """软删除管理端用户。禁止删除当前操作者或受保护系统管理员，删除后保留历史关联与审计记录。
    """
    user = await get_or_404(session, User, user_id, "用户")
    user.deleted_at = datetime.now()
    await log(session, "delete", "user", user.id)
    await commit(session, "删除用户失败")


@router.get("/roles", response_model=list[RoleOut])
async def list_roles(include_deleted: bool = Query(False), session: AsyncSession = Depends(get_db)):
    """查询角色及其权限分配。默认隐藏软删除角色。
    """
    statement = select(Role).options(selectinload(Role.permissions)).order_by(Role.id.desc())
    if not include_deleted:
        statement = statement.where(Role.deleted_at.is_(None))
    return [role_out(item) for item in (await session.execute(statement)).scalars().unique()]


@router.post("/roles", response_model=RoleOut, status_code=status.HTTP_201_CREATED)
async def create_role(payload: RoleCreate, session: AsyncSession = Depends(get_db)):
    """创建角色并绑定代码注册权限。角色编码重复或权限编码无效时返回 400。
    """
    role = Role(
        code=payload.code, name=payload.name, description=payload.description,
        permissions=await resolve_registered_permissions(session, payload.permission_codes),
    )
    session.add(role)
    await session.flush()
    await log(session, "create", "role", role.id)
    await commit(session, "角色编码已存在")
    return role_out(role)


@router.patch("/roles/{role_id}", response_model=RoleOut)
async def update_role(role_id: int, payload: RoleUpdate, session: AsyncSession = Depends(get_db)):
    # Preload the collection before replacing this async many-to-many relationship.
    """更新角色基础信息和权限分配。内置管理员角色不能通过普通接口修改。
    """
    result = await session.execute(
        select(Role).options(selectinload(Role.permissions)).where(Role.id == role_id)
    )
    role = result.scalar_one_or_none()
    if role is None:
        raise HTTPException(404, "角色不存在")
    status_changed_to = payload.status if payload.status is not None and payload.status != role.status else None
    for field, value in payload.model_dump(exclude_unset=True, exclude={"permission_codes"}).items():
        setattr(role, field, value)
    if payload.permission_codes is not None:
        role.permissions = await resolve_registered_permissions(session, payload.permission_codes)
    operation = "enable" if status_changed_to == "active" else "disable" if status_changed_to == "disabled" else "update"
    await log(session, operation, "role", role.id)
    await commit(session, "更新角色失败")
    await session.refresh(role, attribute_names=["permissions"])
    return role_out(role)


@router.delete("/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(role_id: int, session: AsyncSession = Depends(get_db)):
    """软删除非内置角色。仍被用户使用或属于系统内置角色时不允许删除。
    """
    role = await get_or_404(session, Role, role_id, "角色")
    role.deleted_at = datetime.now()
    await log(session, "delete", "role", role.id)
    await commit(session, "删除角色失败")


@router.post("/roles/{role_id}/restore", response_model=RoleOut)
async def restore_role(role_id: int, session: AsyncSession = Depends(get_db)):
    # Preload the collection before replacing this async many-to-many relationship.
    """恢复已软删除的非内置角色。目标必须已删除，且恢复后不能造成名称冲突。
    """
    result = await session.execute(
        select(Role).options(selectinload(Role.permissions)).where(Role.id == role_id)
    )
    role = result.scalar_one_or_none()
    if role is None:
        raise HTTPException(404, "角色不存在")
    role.deleted_at = None
    role.deleted_by = None
    await log(session, "restore", "role", role.id)
    await commit(session, "恢复角色失败")
    await session.refresh(role, attribute_names=["permissions"])
    return role_out(role)


@router.get("/permissions", response_model=list[PermissionOut])
async def list_permissions(session: AsyncSession = Depends(get_db)):
    """查询代码注册表支持的权限列表，供角色配置和管理端展示使用。
    """
    result = await session.execute(
        select(Permission)
        .where(Permission.code.in_(REGISTERED_PERMISSION_CODES))
        .order_by(Permission.parent_id.is_not(None), Permission.id)
    )
    return [permission_out(item) for item in result.scalars()]


@router.post("/permissions", status_code=status.HTTP_405_METHOD_NOT_ALLOWED)
async def create_permission():
    """拒绝动态创建权限。新权限必须先在代码注册表中声明，保证鉴权规则可追踪。
    """
    raise HTTPException(405, "权限由系统注册表维护，不能手动新增")


@router.patch("/permissions/{permission_id}", response_model=PermissionOut)
async def update_permission(permission_id: int, payload: PermissionUpdate, session: AsyncSession = Depends(get_db)):
    """拒绝直接修改系统权限定义。权限语义和资源映射由代码维护。
    """
    permission = await registered_permission_or_404(session, permission_id)
    status_changed_to = payload.status if payload.status is not None and payload.status != permission.status else None
    if payload.status is not None:
        permission.status = payload.status
    operation = "enable" if status_changed_to == "active" else "disable" if status_changed_to == "disabled" else "update"
    await log(session, operation, "permission", permission.id)
    await commit(session, "更新权限失败")
    return permission_out(permission)


@router.delete("/permissions/{permission_id}", status_code=status.HTTP_405_METHOD_NOT_ALLOWED)
async def delete_permission(permission_id: int, session: AsyncSession = Depends(get_db)):
    """拒绝删除系统注册权限，避免已有角色引用失效。
    """
    await registered_permission_or_404(session, permission_id)
    raise HTTPException(405, "注册权限不能删除，可通过启用或停用控制状态")


@router.post("/permissions/{permission_id}/restore", status_code=status.HTTP_405_METHOD_NOT_ALLOWED)
async def restore_permission(permission_id: int, session: AsyncSession = Depends(get_db)):
    """拒绝恢复或变更系统权限生命周期，权限变更必须通过代码注册表完成。
    """
    await registered_permission_or_404(session, permission_id)
    raise HTTPException(405, "注册权限由系统自动同步，无需手动恢复")


@router.get("/libraries", response_model=list[LibraryOut])
async def list_libraries(include_deleted: bool = Query(False), session: AsyncSession = Depends(get_db)):
    """查询知识库列表。默认隐藏软删除知识库，并返回导入规则和基础展示信息。
    """
    statement = select(Library).order_by(Library.id.desc())
    if not include_deleted:
        statement = statement.where(Library.deleted_at.is_(None))
    return list((await session.execute(statement)).scalars())


@router.post("/libraries", response_model=LibraryOut, status_code=status.HTTP_201_CREATED)
async def create_library(
    payload: LibraryCreate,
    session: AsyncSession = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """创建知识库并保存导入规则。规则会影响后续文档处理，名称重复或规则无效时返回 400。
    """
    if payload.entity_recognition_mode != "disabled" and not current_user.is_system_admin:
        raise HTTPException(403, "实体识别模式仅管理员可设置")
    validate_library_rules(payload.model_dump())
    library = Library(**payload.model_dump())
    session.add(library)
    await session.flush()
    await log(session, "create", "library", library.id)
    await commit(session, "知识库编码已存在")
    await session.refresh(library)
    return library


@router.patch("/libraries/{library_id}", response_model=LibraryOut)
async def update_library(
    library_id: int,
    payload: LibraryUpdate,
    session: AsyncSession = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """更新知识库资料和导入规则。修改配置不会自动重新处理已经导入的文档。
    """
    if payload.entity_recognition_mode is not None and payload.entity_recognition_mode != "disabled" and not current_user.is_system_admin:
        raise HTTPException(403, "实体识别模式仅管理员可设置")
    library = await get_or_404(session, Library, library_id, "知识库")
    values = payload.model_dump(exclude_unset=True)
    validate_library_rules({"entity_recognition_mode": library.entity_recognition_mode, **values})
    for field, value in values.items():
        setattr(library, field, value)
    await log(session, "update", "library", library.id)
    await commit(session, "更新知识库失败")
    await session.refresh(library)
    return library


@router.delete("/libraries/{library_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_library(library_id: int, session: AsyncSession = Depends(get_db), current_user: AuthenticatedUser = Depends(get_current_user)):
    """软删除知识库并保留关联数据和审计记录，为后续恢复及历史追溯保留基础。
    """
    library = await get_or_404(session, Library, library_id, "知识库")
    if library.document_count > 0:
        raise HTTPException(400, "知识库仍包含文档，不能删除")
    library.deleted_at = datetime.now()
    library.deleted_by = current_user.id
    await log(session, "delete", "library", library.id)
    await commit(session, "删除知识库失败")


@router.post("/libraries/{library_id}/restore", response_model=LibraryOut)
async def restore_library(library_id: int, session: AsyncSession = Depends(get_db)):
    """恢复已软删除的知识库，使其重新出现在默认管理列表中。
    """
    library = await get_or_404(session, Library, library_id, "知识库")
    library.deleted_at = None
    library.deleted_by = None
    await log(session, "restore", "library", library.id)
    await commit(session, "恢复知识库失败")
    await session.refresh(library)
    return library


@router.get("/libraries/{library_id}/members", response_model=list[MemberOut])
async def list_members(library_id: int, session: AsyncSession = Depends(get_db)):
    """查询知识库成员和访问级别。成员关系是后续按指定用户执行知识库权限校验的基础。
    """
    await get_or_404(session, Library, library_id, "知识库")
    return list((await session.execute(select(LibraryMember).where(LibraryMember.library_id == library_id))).scalars())


@router.post("/libraries/{library_id}/members", response_model=MemberOut, status_code=status.HTTP_201_CREATED)
async def add_member(library_id: int, payload: MemberCreate, session: AsyncSession = Depends(get_db)):
    """为指定用户授予知识库成员资格。知识库、用户或成员关系异常时返回对应业务错误。
    """
    await get_or_404(session, Library, library_id, "知识库")
    await get_or_404(session, User, payload.user_id, "用户")
    member = LibraryMember(library_id=library_id, **payload.model_dump())
    session.add(member)
    await session.flush()
    await log(session, "add_member", "library_member", f"{library_id}:{payload.user_id}", {"library_id": library_id, "user_id": payload.user_id, "access_level": payload.access_level})
    await commit(session, "该用户已经是知识库成员")
    await session.refresh(member)
    return member


@router.delete("/libraries/{library_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(library_id: int, user_id: int, session: AsyncSession = Depends(get_db)):
    """撤销用户的知识库成员资格，仅影响后续权限校验，不删除历史审计数据。
    """
    member = await session.get(LibraryMember, {"library_id": library_id, "user_id": user_id})
    if member is None:
        raise HTTPException(404, "成员不存在")
    await session.delete(member)
    await commit(session, "移除成员失败")


async def document_tags(session: AsyncSession, document_id: int) -> list[str]:
    """读取文档绑定的标签名称，并按名称排序返回。
    """
    result = await session.execute(
        select(Tag.name).join(DocumentTag, DocumentTag.tag_id == Tag.id)
        .where(DocumentTag.document_id == document_id).order_by(Tag.name)
    )
    return list(result.scalars())


async def document_chunk_count(session: AsyncSession, document_id: int) -> int:
    """统计文档已持久化的切片数量，用于管理端展示处理结果。
    """
    return (await session.execute(select(func.count(DocumentChunk.id)).where(DocumentChunk.document_id == document_id))).scalar_one()


@router.get("/documents", response_model=list[DocumentListOut])
async def list_documents(
    library_id: int | None = Query(None), include_deleted: bool = Query(False),
    session: AsyncSession = Depends(get_db),
):
    """按知识库查询文档列表，返回文档状态、标签和切片摘要。默认隐藏软删除文档。
    """
    statement = select(Document, Library.name).join(Library).order_by(Document.id.desc())
    if library_id is not None:
        statement = statement.where(Document.library_id == library_id)
    if not include_deleted:
        statement = statement.where(Document.deleted_at.is_(None))
    rows = (await session.execute(statement)).all()
    return [
        document_out(item, chunk_count=await document_chunk_count(session, item.id),
                     tags=await document_tags(session, item.id), library_name=library_name)
        for item, library_name in rows
    ]


@router.post("/documents", response_model=DocumentListOut, status_code=status.HTTP_201_CREATED)
async def create_document(payload: DocumentCreate, session: AsyncSession = Depends(get_db)):
    """创建待导入的文档元数据。该接口不执行文件解析，实际处理由上传接口和导入任务完成。
    """
    library = await get_or_404(session, Library, payload.library_id, "知识库")
    if library.deleted_at is not None or library.status != "active":
        raise HTTPException(400, "知识库已删除或停用")
    if library.max_document_count is not None and library.document_count >= library.max_document_count:
        raise HTTPException(400, "已达到知识库文档数量上限")
    document = Document(**payload.model_dump())
    session.add(document)
    library.document_count += 1
    await session.flush()
    await log(session, "create", "document", document.id)
    await commit(session, "创建文档失败")
    return document_out(document, library_name=library.name)


@router.post("/documents/upload", response_model=list[DocumentUploadOut], status_code=status.HTTP_201_CREATED)
async def upload_document(
    library_id: int = Form(...), files: list[UploadFile] = File(...),
    session: AsyncSession = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """上传 PDF 或 Markdown 文件，创建文档记录并提交导入任务。

    Note:
        失败时不得留下孤立文件、文档或任务。
    """
    library = await get_or_404(session, Library, library_id, "知识库")
    if library.deleted_at is not None or library.status != "active":
        raise HTTPException(400, "知识库已删除或停用")
    if library.entity_recognition_mode != "disabled" and not current_user.is_system_admin:
        raise HTTPException(403, "当前知识库模式暂不支持导入，请联系管理员修改")
    if not files:
        raise HTTPException(400, "请至少选择一个文件")
    if library.max_upload_file_count is not None and len(files) > library.max_upload_file_count:
        raise HTTPException(400, f"单次最多上传 {library.max_upload_file_count} 个文件")
    if library.max_document_count is not None and library.document_count + len(files) > library.max_document_count:
        raise HTTPException(400, "已达到知识库文档数量上限")

    allowed_extensions = ALLOWED_DOCUMENT_EXTENSIONS
    if library.allowed_file_types:
        allowed_extensions = frozenset(f".{file_type.lower().lstrip('.')}" for file_type in library.allowed_file_types)
    prepared_files: list[tuple[str, UploadFile, bytes, str]] = []
    batch_hashes: set[str] = set()
    for file in files:
        filename = Path(file.filename or "upload").name
        extension = Path(filename).suffix.lower()
        if extension not in allowed_extensions:
            raise HTTPException(400, f"文件 {filename} 不符合知识库允许的文件类型")
        raw = await file.read()
        if library.max_file_size_mb is not None and len(raw) > library.max_file_size_mb * 1024 * 1024:
            raise HTTPException(400, f"文件 {filename} 大小不能超过 {library.max_file_size_mb} MB")
        file_hash = hashlib.sha256(raw).hexdigest()
        if file_hash in batch_hashes:
            raise HTTPException(409, f"本次上传包含重复文件：{filename}")
        batch_hashes.add(file_hash)
        prepared_files.append((filename, file, raw, file_hash))

    duplicate = await session.scalar(
        select(Document.id).where(
            Document.library_id == library_id,
            Document.file_hash.in_(batch_hashes),
            Document.deleted_at.is_(None),
        )
    )
    if duplicate is not None:
        raise HTTPException(409, "本次上传包含知识库内已存在的文件")
    directory = Path("storage") / "uploads" / str(library_id)
    directory.mkdir(parents=True, exist_ok=True)
    uploaded_documents: list[DocumentUploadOut] = []
    config_snapshot = {
        "entity_recognition_mode": library.entity_recognition_mode,
        "allowed_file_types": library.allowed_file_types or ["pdf", "md"],
        "max_file_size_mb": library.max_file_size_mb,
        "max_upload_file_count": library.max_upload_file_count,
        "chunking_config": library.chunking_config,
    }
    for filename, file, raw, file_hash in prepared_files:
        stored = directory / f"{uuid4().hex}_{filename}"
        stored.write_bytes(raw)
        document = Document(library_id=library_id, title=Path(filename).stem or filename, original_filename=filename, storage_key=stored.as_posix(), mime_type=file.content_type, file_size=len(raw), file_hash=file_hash, uploaded_by=current_user.id)
        session.add(document)
        library.document_count += 1
        await session.flush()
        # MySQL 的 created_at / updated_at 由服务端默认值生成；异步会话中先刷新，
        # 避免构建响应时延迟读取字段而触发 MissingGreenlet。
        await session.refresh(document)
        task = ImportTask(library_id=library_id, document_id=document.id, status="queued", current_step="queued", progress=0, config_snapshot=config_snapshot, created_by=current_user.id)
        session.add(task)
        await session.flush()
        await log(session, "upload", "document", document.id, {"import_task_id": task.id})
        await log(session, "queue", "import_task", task.id, {"document_id": document.id})
        uploaded_documents.append(DocumentUploadOut(**document_out(document, library_name=library.name), task_id=task.id))
    await commit(session, "上传文档失败")
    for uploaded_document in uploaded_documents:
        try:
            await enqueue_import_task(uploaded_document.task_id)
        except Exception as error:
            logging.getLogger(__name__).error(
                "导入任务入队失败：task_id=%s error=%s",
                uploaded_document.task_id,
                error,
                exc_info=True,
            )
            failed_queue_task = await session.get(ImportTask, uploaded_document.task_id)
            if failed_queue_task is not None:
                failed_queue_task.current_step = "queue_pending"
                failed_queue_task.error_message = f"Redis 入队失败：{error}"[:65535]
            await commit(session, "记录导入队列状态失败")
    return uploaded_documents

@router.get("/documents/{document_id}", response_model=DocumentDetailOut)
async def get_document(document_id: int, session: AsyncSession = Depends(get_db)):
    """查询单个文档的管理详情，包含标签、处理状态和切片数量。
    """
    document = await get_or_404(session, Document, document_id, "文档")
    library = await get_or_404(session, Library, document.library_id, "知识库")
    return document_out(document, chunk_count=await document_chunk_count(session, document.id),
                        tags=await document_tags(session, document.id), library_name=library.name)


@router.patch("/documents/{document_id}", response_model=DocumentDetailOut)
async def update_document(document_id: int, payload: DocumentUpdate, session: AsyncSession = Depends(get_db)):
    """更新文档展示信息和业务元数据。修改元数据不会自动触发重新切分或向量化。
    """
    document = await get_or_404(session, Document, document_id, "文档")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(document, field, value)
    await log(session, "update", "document", document.id)
    await commit(session, "更新文档失败")
    library = await get_or_404(session, Library, document.library_id, "知识库")
    return document_out(document, chunk_count=await document_chunk_count(session, document.id),
                        tags=await document_tags(session, document.id), library_name=library.name)


def delete_document_vectors(knowledge_base_id: int, document_id: int, *, milvus_client=None) -> None:
    """删除向量库中指定文档的全部切片，避免文档删除后仍被问答检索到。
    """
    client = milvus_client or get_milvus_client()
    collection_name = milvus_config.document_chunks_v2_collection
    if not client.has_collection(collection_name=collection_name):
        logging.getLogger(__name__).info(
            "Milvus v2 collection is absent; vector cleanup skipped: collection=%s document_id=%s",
            collection_name,
            document_id,
        )
        return
    filter_expression = f"knowledge_base_id == {knowledge_base_id} and document_id == {document_id}"
    client.delete(collection_name=collection_name, filter=filter_expression)
    logging.getLogger(__name__).info(
        "Milvus document vectors deleted: collection=%s knowledge_base_id=%s document_id=%s",
        collection_name,
        knowledge_base_id,
        document_id,
    )


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(document_id: int, session: AsyncSession = Depends(get_db), current_user: AuthenticatedUser = Depends(get_current_user)):
    """删除文档及其检索数据。

    Note:
        删除前应处理未结束导入任务，并同步清理数据库、向量库和处理文件。
    """
    document = await get_or_404(session, Document, document_id, "文档")
    active_task_id = await session.scalar(
        select(ImportTask.id).where(
            ImportTask.document_id == document_id,
            ImportTask.status.in_(("queued", "processing")),
        ).limit(1)
    )
    if active_task_id is not None:
        raise HTTPException(400, "文档仍有正在排队或处理中的导入任务，请先取消任务后再删除文档")

    try:
        delete_document_vectors(document.library_id, document.id)
    except Exception as error:
        logging.getLogger(__name__).error(
            "Milvus document vector cleanup failed: knowledge_base_id=%s document_id=%s error=%s",
            document.library_id,
            document.id,
            error,
            exc_info=True,
        )
        raise HTTPException(503, "Milvus 向量删除失败，文档未删除，请稍后重试") from error

    if document.deleted_at is None:
        library = await get_or_404(session, Library, document.library_id, "知识库")
        library.document_count = max(0, library.document_count - 1)
    await session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document_id))
    await session.execute(delete(ImportTask).where(ImportTask.document_id == document_id))
    document.deleted_at = datetime.now()
    document.deleted_by = current_user.id
    document.status = "deleted"
    await log(session, "delete", "document", document.id)
    await commit(session, "删除文档失败")


@router.get("/documents/{document_id}/chunks", response_model=list[ChunkOut])
async def list_document_chunks(document_id: int, session: AsyncSession = Depends(get_db)):
    """按切片顺序查询文档内容及来源位置，用于管理端查看解析结果。
    """
    await get_or_404(session, Document, document_id, "文档")
    result = await session.execute(
        select(DocumentChunk).where(DocumentChunk.document_id == document_id).order_by(DocumentChunk.chunk_index)
    )
    return list(result.scalars())


@router.put("/documents/{document_id}/tags", response_model=DocumentDetailOut)
async def replace_document_tags(document_id: int, payload: DocumentTagsUpdate, session: AsyncSession = Depends(get_db)):
    """整体替换文档标签关联，确保前端提交集合与数据库最终状态一致。
    """
    document = await get_or_404(session, Document, document_id, "文档")
    names = list(dict.fromkeys(name.strip() for name in payload.tag_names if name.strip()))
    result = await session.execute(select(Tag).where(Tag.name.in_(names))) if names else None
    tags = {tag.name: tag for tag in result.scalars()} if result else {}
    for name in names:
        if name not in tags:
            tag = Tag(name=name)
            session.add(tag)
            await session.flush()
            tags[name] = tag
    await session.execute(DocumentTag.__table__.delete().where(DocumentTag.document_id == document_id))
    session.add_all([DocumentTag(document_id=document_id, tag_id=tags[name].id) for name in names])
    await log(session, "update_tags", "document", document.id)
    await commit(session, "更新文档标签失败")
    library = await get_or_404(session, Library, document.library_id, "知识库")
    return document_out(document, chunk_count=await document_chunk_count(session, document.id),
                        tags=names, library_name=library.name)



def import_task_out(task: ImportTask) -> dict:
    """将导入任务转换为管理端响应，包含任务状态、进度、错误信息和关联文档摘要。
    """
    return {
        "id": task.id, "library_id": task.library_id, "document_id": task.document_id,
        "status": task.status, "current_step": task.current_step, "progress": task.progress,
        "config_snapshot": task.config_snapshot, "entity_result": task.entity_result,
        "chunk_count": task.chunk_count, "error_message": task.error_message,
        "created_by": task.created_by, "created_at": task.created_at, "updated_at": task.updated_at,
        "started_at": task.started_at, "finished_at": task.finished_at,
        "document_title": task.document.title if task.document else None,
        "library_name": task.library.name if task.library else None,
    }

@router.get("/import-tasks", response_model=list[ImportTaskOut])
async def list_import_tasks(
    library_id: int | None = Query(None),
    task_status: str | None = Query(None),
    import_date: date | None = Query(None),
    session: AsyncSession = Depends(get_db),
):
    """按知识库或任务状态查询导入任务，返回任务进度和关联文档信息。
    """
    statement = select(ImportTask).options(selectinload(ImportTask.document), selectinload(ImportTask.library)).order_by(ImportTask.id.desc())
    if library_id is not None: statement = statement.where(ImportTask.library_id == library_id)
    if task_status is not None:
        if task_status not in IMPORT_TASK_STATUSES: raise HTTPException(400, "无效的导入任务状态")
        statement = statement.where(ImportTask.status == task_status)
    if import_date is not None:
        day_start = datetime.combine(import_date, time.min)
        day_end = day_start + timedelta(days=1)
        statement = statement.where(ImportTask.created_at >= day_start, ImportTask.created_at < day_end)
    tasks = (await session.execute(statement)).scalars().all()
    return [import_task_out(task) for task in tasks]

@router.get("/import-tasks/{task_id}", response_model=ImportTaskOut)
async def get_import_task(task_id: int, session: AsyncSession = Depends(get_db)):
    """查询单个导入任务的当前步骤、进度和错误信息。
    """
    task = await session.scalar(select(ImportTask).options(selectinload(ImportTask.document), selectinload(ImportTask.library)).where(ImportTask.id == task_id))
    if task is None: raise HTTPException(404, "导入任务不存在")
    return import_task_out(task)

@router.post("/import-tasks/{task_id}/retry", response_model=ImportTaskOut)
async def retry_import_task(task_id: int, session: AsyncSession = Depends(get_db)):
    """重新排队失败或取消的导入任务。运行中的任务禁止重复入队，避免同一文档并发处理。
    """
    task = await session.scalar(select(ImportTask).options(selectinload(ImportTask.document), selectinload(ImportTask.library)).where(ImportTask.id == task_id))
    if task is None: raise HTTPException(404, "导入任务不存在")
    if task.status != "failed": raise HTTPException(400, "只有失败任务可以重试")
    task.status = "queued"; task.current_step = "queued"; task.progress = 0; task.error_message = None; task.started_at = None; task.finished_at = None
    task.document.status = "uploaded"
    await log(session, "retry", "import_task", task.id, {"document_id": task.document_id})
    await commit(session, "重试导入任务失败")
    await enqueue_import_task(task.id)
    return import_task_out(task)

@router.post("/import-tasks/{task_id}/cancel", response_model=ImportTaskOut)
async def cancel_import_task(task_id: int, session: AsyncSession = Depends(get_db)):
    """取消排队或处理中的导入任务，并使关联文档恢复为可重新导入状态。终态任务不能取消。
    """
    task = await session.scalar(select(ImportTask).options(selectinload(ImportTask.document), selectinload(ImportTask.library)).where(ImportTask.id == task_id))
    if task is None: raise HTTPException(404, "导入任务不存在")
    if task.status not in {"queued", "processing"}: raise HTTPException(400, "当前任务不能取消")
    await request_import_cancellation(task.id)
    task.status = "cancelled"; task.current_step = "cancelled"; task.finished_at = datetime.now()
    task.document.status = "uploaded"; task.document.parse_error = "导入任务已取消"
    await log(session, "cancel", "import_task", task.id, {"document_id": task.document_id})
    await commit(session, "取消导入任务失败")
    return import_task_out(task)


@router.delete("/import-tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_import_task(task_id: int, session: AsyncSession = Depends(get_db)):
    """删除已完成、失败或取消的导入任务记录。删除任务不会删除文档或已生成的知识切片。
    """
    task = await session.get(ImportTask, task_id)
    if task is None:
        raise HTTPException(404, "导入任务不存在")
    if task.status in {"queued", "processing"}:
        raise HTTPException(400, "请先取消正在排队或处理中的导入任务")
    await log(session, "delete", "import_task", task.id, {"document_id": task.document_id})
    await session.delete(task)
    await commit(session, "删除导入任务失败")

@router.get("/tags", response_model=list[TagOut])
async def list_tags(session: AsyncSession = Depends(get_db)):
    """查询按名称排序的文档标签列表。
    """
    return list((await session.execute(select(Tag).order_by(Tag.name))).scalars())


@router.post("/tags", response_model=TagOut, status_code=status.HTTP_201_CREATED)
async def create_tag(payload: TagCreate, session: AsyncSession = Depends(get_db)):
    """创建文档分类标签。标签名称必须唯一，重复名称返回 400。
    """
    tag = Tag(name=payload.name.strip())
    session.add(tag)
    await session.flush()
    await log(session, "create", "tag", tag.id, {"name": tag.name})
    await commit(session, "标签名称已存在")
    await session.refresh(tag)
    return tag


@router.patch("/tags/{tag_id}", response_model=TagOut)
async def update_tag(tag_id: int, payload: TagUpdate, session: AsyncSession = Depends(get_db)):
    """修改标签名称。标签不存在或新名称冲突时返回对应业务错误。
    """
    tag = await get_or_404(session, Tag, tag_id, "标签")
    tag.name = payload.name.strip()
    await log(session, "update", "tag", tag.id, {"name": tag.name})
    await commit(session, "标签名称已存在")
    await session.refresh(tag)
    return tag


@router.delete("/tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tag(tag_id: int, session: AsyncSession = Depends(get_db)):
    """删除文档标签。数据库关联约束负责防止留下无效文档标签关系。
    """
    tag = await get_or_404(session, Tag, tag_id, "标签")
    await log(session, "delete", "tag", tag.id, {"name": tag.name})
    await session.delete(tag)
    await commit(session, "删除标签失败")



# 操作日志使用资源名称而非内部编号展示；名称缺失时仍保留资源类型以兼容历史记录。
@router.get("/operation-logs", response_model=OperationLogPageOut)
async def list_operation_logs(
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    user_id: int | None = Query(None), resource_type: str | None = Query(None),
    operation: str | None = Query(None), started_at: datetime | None = Query(None),
    ended_at: datetime | None = Query(None), session: AsyncSession = Depends(get_db),
):
    """分页查询管理操作日志。

    Note:
        支持按操作人、资源类型、操作类型和时间范围筛选；资源删除后仍保留类型和编号满足审计追溯。
    """
    filters = []
    if user_id is not None: filters.append(OperationLog.user_id == user_id)
    if resource_type: filters.append(OperationLog.resource_type == resource_type)
    if operation: filters.append(OperationLog.operation == operation)
    if started_at: filters.append(OperationLog.created_at >= started_at)
    if ended_at: filters.append(OperationLog.created_at <= ended_at)
    statement = (
        select(OperationLog, User.display_name)
        .outerjoin(User, User.id == OperationLog.user_id)
        .where(*filters)
        .order_by(OperationLog.id.desc())
    )
    total = await session.scalar(select(func.count()).select_from(OperationLog).where(*filters)) or 0
    rows = (await session.execute(statement.offset((page - 1) * page_size).limit(page_size))).all()
    resource_display_names = await operation_log_resource_names(
        session, [item for item, _ in rows]
    )
    return {
        "items": [
            {
                "id": item.id,
                "user_id": item.user_id,
                "user_display_name": display_name,
                "operation": item.operation,
                "resource_type": item.resource_type,
                "resource_id": item.resource_id,
                "resource_display_name": resource_display_names.get(item.id),
                "request_ip": item.request_ip,
                "detail": item.detail,
                "created_at": item.created_at,
            }
            for item, display_name in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
