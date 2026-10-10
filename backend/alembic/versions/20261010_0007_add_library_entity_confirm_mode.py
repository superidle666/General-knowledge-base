"""add kb_library.entity_confirm_mode (实体确认闸严格程度，库级)

Revision ID: 20261010_0007
Revises: 20260924_0006
Create Date: 2026-10-10
"""
from alembic import op
import sqlalchemy as sa


revision = "20261010_0007"
down_revision = "20260924_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 默认严格：确认不了型号就先问一句。宽松档（只筛选不反问）＝改造前行为，
    # 由管理端按库显式切过去，或用全局兜底开关 ENTITY_CONFIRM_GATE_DISABLE=1 一键退回。
    op.add_column(
        "kb_library",
        sa.Column("entity_confirm_mode", sa.String(length=20), server_default="strict", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("kb_library", "entity_confirm_mode")
