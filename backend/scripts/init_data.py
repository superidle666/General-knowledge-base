"""初始化本地开发所需的角色、示例用户、知识库和文档数据。

请在配置 MYSQL_URL 并执行 ``alembic upgrade head`` 后运行：
    uv run python -m backend.scripts.init_data
"""

import asyncio
import hashlib
import os
import secrets

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from backend.app.core.permissions import PERMISSION_REGISTRY, REGISTERED_PERMISSION_CODES

from backend.app.db.models import (
    Document, DocumentChunk, DocumentTag, Library, LibraryMember,
    OperationLog, Permission, Role, RolePermission, Tag, User,
)
from backend.app.db.session import SessionLocal


def hash_password(password: str) -> str:
    """生成与 API 创建用户接口一致的 PBKDF2 密码摘要。"""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 310_000).hex()
    return f"pbkdf2_sha256$310000${salt}${digest}"


async def seed() -> None:
    """幂等写入演示数据，重复执行不会创建重复业务记录。"""
    async with SessionLocal.begin() as session:
        roles: dict[str, Role] = {}
        for code, name, description in [
            ("admin", "系统管理员", "管理所有平台资源"),
            ("user", "普通用户", "使用知识库和创建会话"),
            ("operator", "运营人员", "维护知识库内容"),
        ]:
            role = (await session.execute(select(Role).where(Role.code == code))).scalar_one_or_none()
            if role is None:
                role = Role(code=code, name=name, description=description)
                session.add(role)
            roles[code] = role
        await session.flush()

        permissions_by_code: dict[str, Permission] = {}
        for parent_code, definition in PERMISSION_REGISTRY.items():
            permission = (await session.execute(
                select(Permission).where(Permission.code == parent_code)
            )).scalar_one_or_none()
            if permission is None:
                permission = Permission(code=parent_code, name=definition["name"], parent_id=None)
                session.add(permission)
            else:
                permission.name = definition["name"]
                permission.parent_id = None
                permission.deleted_at = None
                permission.deleted_by = None
            permissions_by_code[parent_code] = permission
        await session.flush()

        for parent_code, definition in PERMISSION_REGISTRY.items():
            parent = permissions_by_code[parent_code]
            for code, name in definition["children"].items():
                permission = (await session.execute(
                    select(Permission).where(Permission.code == code)
                )).scalar_one_or_none()
                if permission is None:
                    permission = Permission(code=code, name=name, parent_id=parent.id)
                    session.add(permission)
                else:
                    permission.name = name
                    permission.parent_id = parent.id
                    permission.deleted_at = None
                    permission.deleted_by = None
                permissions_by_code[code] = permission
        await session.flush()

        role_permission_codes = {
            "admin": set(REGISTERED_PERMISSION_CODES),
            "operator": {
                "library:manage", "library:read", "library:create", "library:modify", "library:delete",
                "document:manage", "document:view", "document:upload", "document:modify", "document:delete", "document:download",
            },
        }
        for role_code, codes in role_permission_codes.items():
            role = roles[role_code]
            await session.execute(
                RolePermission.__table__.delete().where(RolePermission.role_id == role.id)
            )
            session.add_all([
                RolePermission(role_id=role.id, permission_id=permissions_by_code[code].id)
                for code in codes
            ])
        users: dict[str, User] = {}
        user_defs = [
            ("admin", "系统管理员", "admin", "admin@example.com"),
            ("lin", "林工", "user", "lin.engineer@example.com"),
            ("zhang", "张师傅", "operator", "zhang.service@example.com"),
        ]
        for username, display_name, role_code, email in user_defs:
            user = (await session.execute(select(User).options(selectinload(User.roles)).where(User.username == username))).scalar_one_or_none()
            if user is None:
                user = User(username=username, display_name=display_name, email=email, password_hash=hash_password(os.getenv("DEFAULT_ADMIN_PASSWORD", "admin123")), status="active", roles=[roles[role_code]])
                session.add(user)
            elif roles[role_code] not in user.roles:
                user.roles.append(roles[role_code])
            users[username] = user
        await session.flush()

        library_defs = [
            ("工业设备手册", "industrial-equipment", "工业设备产品手册和操作指南", "#2563eb"),
            ("网络设备资料", "network-equipment", "网络设备配置和维护资料", "#0f9d78"),
            ("售后服务知识", "after-sales", "售后服务标准和故障处理知识", "#c47f1d"),
        ]
        libraries: dict[str, Library] = {}
        for name, code, description, color in library_defs:
            library = (await session.execute(select(Library).where(Library.code == code))).scalar_one_or_none()
            if library is None:
                library = Library(name=name, code=code, description=description, cover_color=color, created_by=users["admin"].id)
                session.add(library)
            libraries[code] = library
        await session.flush()

        for library_code, username in [("industrial-equipment", "lin"), ("industrial-equipment", "zhang"), ("network-equipment", "lin"), ("after-sales", "zhang")]:
            exists = (await session.execute(select(LibraryMember).where(LibraryMember.library_id == libraries[library_code].id, LibraryMember.user_id == users[username].id))).scalar_one_or_none()
            if exists is None:
                session.add(LibraryMember(library_id=libraries[library_code].id, user_id=users[username].id, access_level="write" if username == "zhang" else "read"))

        document_defs = [
            ("industrial-equipment", "HAK180 用户手册", "HAK180 用户手册.pdf", "documents/industrial/HAK180-manual.pdf", "application/pdf", 12400000, "ready"),
            ("industrial-equipment", "HAK180 维护指南", "HAK180 维护指南.pdf", "documents/industrial/HAK180-maintenance.pdf", "application/pdf", 8700000, "ready"),
            ("network-equipment", "LA2608 网关配置说明", "LA2608 网关配置说明.docx", "documents/network/LA2608-config.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", 2100000, "ready"),
            ("after-sales", "售后服务标准流程", "售后服务标准流程.pdf", "documents/after-sales/service-process.pdf", "application/pdf", 5600000, "processing"),
        ]
        documents: dict[str, Document] = {}
        for library_code, title, filename, storage_key, mime_type, file_size, doc_status in document_defs:
            document = (await session.execute(select(Document).where(Document.storage_key == storage_key))).scalar_one_or_none()
            if document is None:
                document = Document(library_id=libraries[library_code].id, title=title, original_filename=filename, storage_key=storage_key, mime_type=mime_type, file_size=file_size, status=doc_status, uploaded_by=users["admin"].id, published_at=func.now() if doc_status == "ready" else None)
                session.add(document)
                libraries[library_code].document_count += 1
            documents[storage_key] = document
        await session.flush()

        chunks = [
            ("documents/industrial/HAK180-manual.pdf", 0, "转印温度建议根据材料类型调整。PVC/PU 材料参考范围为 105-120°C。", 42),
            ("documents/industrial/HAK180-manual.pdf", 1, "每次调节温度后等待约 30 秒，再进行下一次试印。", 43),
            ("documents/industrial/HAK180-maintenance.pdf", 0, "设备每日使用后应清洁加热板和转印区域，检查压力是否均匀。", 18),
            ("documents/network/LA2608-config.docx", 0, "恢复 LA2608 网关出厂设置前，请先备份当前网络配置。", 6),
        ]
        for storage_key, index, content, page in chunks:
            document = documents[storage_key]
            exists = (await session.execute(select(DocumentChunk).where(DocumentChunk.document_id == document.id, DocumentChunk.chunk_index == index))).scalar_one_or_none()
            if exists is None:
                session.add(DocumentChunk(document_id=document.id, chunk_index=index, content=content, page_number=page, token_count=len(content), vector_id=f"demo-{document.id}-{index}"))

        tag_defs = ["Brother", "HAK180", "LA2608", "烫金机", "维护手册", "故障排查"]
        tags: dict[str, Tag] = {}
        for name in tag_defs:
            tag = (await session.execute(select(Tag).where(Tag.name == name))).scalar_one_or_none()
            if tag is None:
                tag = Tag(name=name)
                session.add(tag)
            tags[name] = tag
        await session.flush()
        for storage_key, tag_name in [("documents/industrial/HAK180-manual.pdf", "Brother"), ("documents/industrial/HAK180-manual.pdf", "HAK180"), ("documents/industrial/HAK180-maintenance.pdf", "维护手册"), ("documents/network/LA2608-config.docx", "LA2608")]:
            document = documents[storage_key]
            exists = (await session.execute(select(DocumentTag).where(DocumentTag.document_id == document.id, DocumentTag.tag_id == tags[tag_name].id))).scalar_one_or_none()
            if exists is None:
                session.add(DocumentTag(document_id=document.id, tag_id=tags[tag_name].id))

        log_count = await session.scalar(select(func.count()).select_from(OperationLog))
        if not log_count:
            session.add_all([
                OperationLog(user_id=users["admin"].id, operation="seed", resource_type="library", detail={"message": "初始化示例知识库"}),
                OperationLog(user_id=users["admin"].id, operation="seed", resource_type="document", detail={"message": "初始化示例文档"}),
            ])


if __name__ == "__main__":
    asyncio.run(seed())
