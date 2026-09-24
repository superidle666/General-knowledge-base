"""add library use_hyde switch

Revision ID: 20260921_0005
Revises: 20260906_0004
"""
from alembic import op
import sqlalchemy as sa


revision = "20260921_0005"
down_revision = "20260906_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 默认关闭：HyDE 每条问题会多一次大模型调用，成本与时延由知识库所有者显式承担
    op.add_column(
        "kb_library",
        sa.Column("use_hyde", sa.Boolean(), server_default=sa.text("0"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("kb_library", "use_hyde")
