"""add encrypted system settings

Revision ID: b84f6c8d1e2a
Revises: 4c729ab0e1f2
"""
from alembic import op
import sqlalchemy as sa

revision = "b84f6c8d1e2a"
down_revision = "4c729ab0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sys_setting",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("setting_key", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("encrypted_value", sa.Text(), nullable=True),
        sa.Column("is_sensitive", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("setting_key"),
    )
    op.create_index("ix_sys_setting_category", "sys_setting", ["category"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_sys_setting_category", table_name="sys_setting")
    op.drop_table("sys_setting")