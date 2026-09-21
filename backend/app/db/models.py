"""
知识库平台的 SQLAlchemy ORM 模型。

模型覆盖用户权限、知识库、文档、文档分段、标签和操作日志；
软删除数据通过 deleted_at 保留审计和恢复所需的信息。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .session import Base


class TimestampMixin:
    """为业务表提供创建时间和自动更新时间字段。"""

    # 记录业务数据首次创建时间，使用数据库时间保证多实例一致。
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    # 更新记录时由 SQLAlchemy 根据 onupdate 自动刷新。
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class SoftDeleteMixin:
    """为需要保留历史记录的业务表提供软删除时间字段。"""

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class User(TimestampMixin, SoftDeleteMixin, Base):
    """系统用户模型；通过角色关系获得平台操作权限。"""
    __tablename__ = "sys_user"
    __table_args__ = (Index("ix_sys_user_status", "status"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # active、disabled、locked 分别表示正常、停用和锁定。
    status: Mapped[str] = mapped_column(String(20), default="active", server_default="active", nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    roles: Mapped[list["Role"]] = relationship(secondary="sys_user_role", back_populates="users")
    library_memberships: Mapped[list["LibraryMember"]] = relationship(back_populates="user")


class Role(TimestampMixin, SoftDeleteMixin, Base):
    """角色模型；角色通过多对多关系绑定权限和用户。"""
    __tablename__ = "sys_role"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active", server_default="active", nullable=False)
    deleted_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    users: Mapped[list[User]] = relationship(secondary="sys_user_role", back_populates="roles")
    permissions: Mapped[list["Permission"]] = relationship(secondary="sys_role_permission", back_populates="roles")


class UserRole(Base):
    """用户与角色的关联表，组合主键防止重复授权。"""
    __tablename__ = "sys_user_role"
    __table_args__ = (UniqueConstraint("user_id", "role_id", name="uq_sys_user_role"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("sys_user.id", ondelete="CASCADE"), primary_key=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("sys_role.id", ondelete="CASCADE"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)


class Permission(TimestampMixin, SoftDeleteMixin, Base):
    """系统权限模型，code 是稳定的程序权限标识。"""
    __tablename__ = "sys_permission"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active", server_default="active", nullable=False)
    deleted_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("sys_permission.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    parent: Mapped["Permission | None"] = relationship(
        back_populates="children",
        remote_side="Permission.id",
    )
    children: Mapped[list["Permission"]] = relationship(back_populates="parent")
    roles: Mapped[list[Role]] = relationship(secondary="sys_role_permission", back_populates="permissions")


class RolePermission(Base):
    """角色与权限的关联表。"""
    __tablename__ = "sys_role_permission"
    __table_args__ = (UniqueConstraint("role_id", "permission_id", name="uq_sys_role_permission"),)

    role_id: Mapped[int] = mapped_column(ForeignKey("sys_role.id", ondelete="CASCADE"), primary_key=True)
    permission_id: Mapped[int] = mapped_column(ForeignKey("sys_permission.id", ondelete="CASCADE"), primary_key=True)


class AuthRefreshToken(Base):
    """可撤销的刷新令牌记录；数据库只保存令牌哈希。"""

    __tablename__ = "sys_auth_refresh_token"
    __table_args__ = (
        Index("ix_sys_auth_refresh_token_user_expires", "user_id", "expires_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("sys_user.id", ondelete="CASCADE"), nullable=False)
    token_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    replaced_by_token_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

class Library(TimestampMixin, SoftDeleteMixin, Base):
    """知识库模型；文档和成员均归属于具体知识库。"""
    __tablename__ = "kb_library"
    __table_args__ = (Index("ix_kb_library_status", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_color: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # 知识库停用后禁止新增文档，但历史文档仍保留。
    status: Mapped[str] = mapped_column(String(20), default="active", server_default="active", nullable=False)
    document_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("sys_user.id", ondelete="SET NULL"), nullable=True)
    entity_recognition_mode: Mapped[str] = mapped_column(String(20), default="disabled", server_default="disabled", nullable=False)
    allowed_file_types: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    max_file_size_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_document_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_upload_file_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunking_config: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    visibility: Mapped[str] = mapped_column(String(20), default="private", server_default="private", nullable=False)
    deleted_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    documents: Mapped[list["Document"]] = relationship(back_populates="library")
    members: Mapped[list["LibraryMember"]] = relationship(back_populates="library")
    import_tasks: Mapped[list["ImportTask"]] = relationship(back_populates="library")


class LibraryMember(Base):
    """知识库成员关系模型，access_level 控制 read/write/admin 能力。"""
    __tablename__ = "kb_library_member"
    __table_args__ = (UniqueConstraint("library_id", "user_id", name="uq_kb_library_member"),)

    library_id: Mapped[int] = mapped_column(ForeignKey("kb_library.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("sys_user.id", ondelete="CASCADE"), primary_key=True)
    access_level: Mapped[str] = mapped_column(String(20), default="read", server_default="read", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    library: Mapped[Library] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="library_memberships")


class Document(TimestampMixin, SoftDeleteMixin, Base):
    """文档元数据模型；原始文件和向量索引通过 storage_key 与分段关联。"""
    __tablename__ = "kb_document"
    __table_args__ = (Index("ix_kb_document_library_status", "library_id", "status"), Index("ix_kb_document_hash", "file_hash"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    library_id: Mapped[int] = mapped_column(ForeignKey("kb_library.id", ondelete="RESTRICT"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    file_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    # uploaded、processing、ready、failed、deleted 驱动文档生命周期。
    status: Mapped[str] = mapped_column(String(20), default="uploaded", server_default="uploaded", nullable=False)
    parse_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("sys_user.id", ondelete="SET NULL"), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    library: Mapped[Library] = relationship(back_populates="documents")
    chunks: Mapped[list["DocumentChunk"]] = relationship(back_populates="document", cascade="all, delete-orphan")
    import_tasks: Mapped[list["ImportTask"]] = relationship(back_populates="document", cascade="all, delete-orphan")


class DocumentChunk(Base):
    """文档分段模型；每个分段可关联一个向量数据库记录。"""
    __tablename__ = "kb_document_chunk"
    __table_args__ = (UniqueConstraint("document_id", "chunk_index", name="uq_kb_document_chunk_index"), Index("ix_kb_document_chunk_document", "document_id"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("kb_document.id", ondelete="CASCADE"), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    section_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vector_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    extra_metadata: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    document: Mapped[Document] = relationship(back_populates="chunks")


class ImportTask(TimestampMixin, Base):
    """持久化文档导入任务，记录处理状态、进度和配置快照。"""
    __tablename__ = "kb_import_task"
    __table_args__ = (
        Index("ix_kb_import_task_library_status", "library_id", "status"),
        Index("ix_kb_import_task_document_created", "document_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    library_id: Mapped[int] = mapped_column(ForeignKey("kb_library.id", ondelete="RESTRICT"), nullable=False)
    document_id: Mapped[int] = mapped_column(ForeignKey("kb_document.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="queued", server_default="queued", nullable=False)
    current_step: Mapped[str | None] = mapped_column(String(40), nullable=True)
    progress: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    config_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    entity_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    chunk_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("sys_user.id", ondelete="SET NULL"), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    library: Mapped[Library] = relationship(back_populates="import_tasks")
    document: Mapped[Document] = relationship(back_populates="import_tasks")

class Tag(TimestampMixin, Base):
    """可复用的文档标签模型。"""
    __tablename__ = "kb_tag"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)


class DocumentTag(Base):
    """文档与标签的关联表。"""
    __tablename__ = "kb_document_tag"
    __table_args__ = (UniqueConstraint("document_id", "tag_id", name="uq_kb_document_tag"),)

    document_id: Mapped[int] = mapped_column(ForeignKey("kb_document.id", ondelete="CASCADE"), primary_key=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("kb_tag.id", ondelete="CASCADE"), primary_key=True)


class OperationLog(Base):
    """管理操作日志模型，用于记录资源变更和审计信息。"""
    __tablename__ = "sys_operation_log"
    __table_args__ = (Index("ix_sys_operation_log_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("sys_user.id", ondelete="SET NULL"), nullable=True)
    operation: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)


class SystemSetting(Base):
    """An administrator-managed application setting stored in encrypted form."""

    __tablename__ = "sys_setting"
    __table_args__ = (Index("ix_sys_setting_category", "category"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    setting_key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    encrypted_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_sensitive: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    updated_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
