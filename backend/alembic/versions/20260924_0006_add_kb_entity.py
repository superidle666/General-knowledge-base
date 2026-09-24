"""add kb_entity table (per-library main entity)

Revision ID: 20260924_0006
Revises: 20260921_0005
Create Date: 2026-09-24
"""
from alembic import op
import sqlalchemy as sa


revision = "20260924_0006"
down_revision = "20260921_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 实体词表首期一篇文档一条主实体记录：document_id 唯一，多实体升级后再放开（见落地方案 §11）
    op.create_table(
        "kb_entity",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("library_id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("normalized_name", sa.String(length=255), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=True),
        sa.Column("source", sa.String(length=20), server_default="llm", nullable=False),
        sa.Column("maybe_series", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["library_id"], ["kb_library.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["kb_document.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", name="uq_kb_entity_document"),
    )
    # 库级隔离 + 归一化名对齐检索的复合索引
    op.create_index("ix_kb_entity_library_name", "kb_entity", ["library_id", "normalized_name"], unique=False)


def downgrade() -> None:
    # 新表无历史数据，回滚只需 drop 索引与表
    op.drop_index("ix_kb_entity_library_name", table_name="kb_entity")
    op.drop_table("kb_entity")
