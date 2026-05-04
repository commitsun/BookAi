"""Add notified_at to escalations for timeout notifications.

Revision ID: r5g6h7i8j9k0
Revises: q4f5g6h7i8j9
Create Date: 2026-04-24
"""

import sqlalchemy as sa
from alembic import op

revision = "r5g6h7i8j9k0"
down_revision = "q4f5g6h7i8j9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "escalations",
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("escalations", "notified_at")
