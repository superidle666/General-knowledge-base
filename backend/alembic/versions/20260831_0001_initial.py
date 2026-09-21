"""create initial knowledge base tables

Revision ID: 20260831_0001
Revises:
Create Date: 2026-08-31
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260831_0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sys_user",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("phone", sa.String(32), nullable=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("avatar_url", sa.String(500), nullable=True),
        sa.Column("status", sa.String(20), server_default="active", nullable=False),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("username"), sa.UniqueConstraint("email"), sa.UniqueConstraint("phone"),
    )
    op.create_index("ix_sys_user_status", "sys_user", ["status"])
    op.create_table(
        "sys_role",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("code"),
    )
    op.create_table(
        "sys_permission",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(100), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("code"),
    )
    op.create_table(
        "sys_user_role",
        sa.Column("user_id", sa.BigInteger(), nullable=False), sa.Column("role_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["role_id"], ["sys_role.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["user_id"], ["sys_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "role_id"), sa.UniqueConstraint("user_id", "role_id", name="uq_sys_user_role"),
    )
    op.create_table(
        "sys_role_permission",
        sa.Column("role_id", sa.Integer(), nullable=False), sa.Column("permission_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["permission_id"], ["sys_permission.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["role_id"], ["sys_role.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("role_id", "permission_id"), sa.UniqueConstraint("role_id", "permission_id", name="uq_sys_role_permission"),
    )
    op.create_table(
        "kb_library",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False), sa.Column("name", sa.String(150), nullable=False), sa.Column("code", sa.String(64), nullable=False), sa.Column("description", sa.Text(), nullable=True), sa.Column("cover_color", sa.String(20), nullable=True), sa.Column("status", sa.String(20), server_default="active", nullable=False), sa.Column("document_count", sa.Integer(), server_default="0", nullable=False), sa.Column("created_by", sa.BigInteger(), nullable=True), sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False), sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False), sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["sys_user.id"], ondelete="SET NULL"), sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("code"),
    )
    op.create_index("ix_kb_library_status", "kb_library", ["status"])
    op.create_table(
        "kb_library_member",
        sa.Column("library_id", sa.Integer(), nullable=False), sa.Column("user_id", sa.BigInteger(), nullable=False), sa.Column("access_level", sa.String(20), server_default="read", nullable=False), sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["library_id"], ["kb_library.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["user_id"], ["sys_user.id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("library_id", "user_id"), sa.UniqueConstraint("library_id", "user_id", name="uq_kb_library_member"),
    )
    op.create_table(
        "kb_document",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False), sa.Column("library_id", sa.Integer(), nullable=False), sa.Column("title", sa.String(255), nullable=False), sa.Column("original_filename", sa.String(255), nullable=False), sa.Column("storage_key", sa.String(500), nullable=False), sa.Column("mime_type", sa.String(120), nullable=True), sa.Column("file_size", sa.BigInteger(), nullable=True), sa.Column("file_hash", sa.String(128), nullable=True), sa.Column("version", sa.Integer(), server_default="1", nullable=False), sa.Column("status", sa.String(20), server_default="uploaded", nullable=False), sa.Column("parse_error", sa.Text(), nullable=True), sa.Column("uploaded_by", sa.BigInteger(), nullable=True), sa.Column("published_at", sa.DateTime(), nullable=True), sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False), sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False), sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["library_id"], ["kb_library.id"], ondelete="RESTRICT"), sa.ForeignKeyConstraint(["uploaded_by"], ["sys_user.id"], ondelete="SET NULL"), sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_kb_document_library_status", "kb_document", ["library_id", "status"])
    op.create_index("ix_kb_document_hash", "kb_document", ["file_hash"])
    op.create_table(
        "kb_document_chunk",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False), sa.Column("document_id", sa.BigInteger(), nullable=False), sa.Column("chunk_index", sa.Integer(), nullable=False), sa.Column("content", sa.Text(), nullable=False), sa.Column("token_count", sa.Integer(), nullable=True), sa.Column("page_number", sa.Integer(), nullable=True), sa.Column("section_title", sa.String(255), nullable=True), sa.Column("vector_id", sa.String(255), nullable=True), sa.Column("metadata", sa.JSON(), nullable=True), sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["kb_document.id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("document_id", "chunk_index", name="uq_kb_document_chunk_index"),
    )
    op.create_index("ix_kb_document_chunk_document", "kb_document_chunk", ["document_id"])
    op.create_table("kb_tag", sa.Column("id", sa.Integer(), autoincrement=True, nullable=False), sa.Column("name", sa.String(100), nullable=False), sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False), sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False), sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("name"))
    op.create_table("kb_document_tag", sa.Column("document_id", sa.BigInteger(), nullable=False), sa.Column("tag_id", sa.Integer(), nullable=False), sa.ForeignKeyConstraint(["document_id"], ["kb_document.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["tag_id"], ["kb_tag.id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("document_id", "tag_id"), sa.UniqueConstraint("document_id", "tag_id", name="uq_kb_document_tag"))
    op.create_table("sys_operation_log", sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False), sa.Column("user_id", sa.BigInteger(), nullable=True), sa.Column("operation", sa.String(100), nullable=False), sa.Column("resource_type", sa.String(64), nullable=True), sa.Column("resource_id", sa.String(64), nullable=True), sa.Column("request_ip", sa.String(45), nullable=True), sa.Column("detail", sa.JSON(), nullable=True), sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False), sa.ForeignKeyConstraint(["user_id"], ["sys_user.id"], ondelete="SET NULL"), sa.PrimaryKeyConstraint("id"))
    op.create_index("ix_sys_operation_log_user_created", "sys_operation_log", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_sys_operation_log_user_created", table_name="sys_operation_log")
    op.drop_table("sys_operation_log")
    op.drop_table("kb_document_tag")
    op.drop_table("kb_tag")
    op.drop_index("ix_kb_document_chunk_document", table_name="kb_document_chunk")
    op.drop_table("kb_document_chunk")
    op.drop_index("ix_kb_document_hash", table_name="kb_document")
    op.drop_index("ix_kb_document_library_status", table_name="kb_document")
    op.drop_table("kb_document")
    op.drop_table("kb_library_member")
    op.drop_index("ix_kb_library_status", table_name="kb_library")
    op.drop_table("kb_library")
    op.drop_table("sys_role_permission")
    op.drop_table("sys_user_role")
    op.drop_table("sys_permission")
    op.drop_table("sys_role")
    op.drop_index("ix_sys_user_status", table_name="sys_user")
    op.drop_table("sys_user")