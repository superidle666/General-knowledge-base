"""Remove unused library content type."""

from alembic import op

revision = "20260906_0004"
down_revision = "20260903_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("kb_library", "content_type")


def downgrade() -> None:
    op.add_column("kb_library", op.Column("content_type", op.String(length=20), nullable=False, server_default="general"))
