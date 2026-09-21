"""add library batch upload limit

Revision ID: 20260903_0003
Revises: 20260903_0002
"""
from alembic import op
import sqlalchemy as sa


revision = "20260903_0003"
down_revision = "20260903_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("kb_library", sa.Column("max_upload_file_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("kb_library", "max_upload_file_count")
