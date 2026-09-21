"""add refresh token persistence

Revision ID: 4c729ab0e1f2
Revises: 653972cf4482
Create Date: 2026-09-02
"""

from alembic import op
import sqlalchemy as sa

revision = "4c729ab0e1f2"
down_revision = "653972cf4482"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sys_auth_refresh_token",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("token_id", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("replaced_by_token_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["sys_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_id"),
    )
    op.create_index("ix_sys_auth_refresh_token_user_expires", "sys_auth_refresh_token", ["user_id", "expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_sys_auth_refresh_token_user_expires", table_name="sys_auth_refresh_token")
    op.drop_table("sys_auth_refresh_token")

