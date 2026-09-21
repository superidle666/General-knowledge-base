"""normalize document lifecycle statuses

Revision ID: 20260903_0002
Revises: 4574bd21512a
"""
from alembic import op
import sqlalchemy as sa

revision = "20260903_0002"
down_revision = "4574bd21512a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("UPDATE kb_document SET status = 'ready' WHERE status = 'published'"))
    op.execute(sa.text("UPDATE kb_document SET status = 'deleted' WHERE status = 'archived'"))


def downgrade() -> None:
    op.execute(sa.text("UPDATE kb_document SET status = 'published' WHERE status = 'ready'"))
    op.execute(sa.text("UPDATE kb_document SET status = 'archived' WHERE status = 'deleted'"))
