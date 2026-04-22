"""Add guest_language to attention_sessions.

Revision ID: n1c2d3e4f5g6
Revises: m0b1c2d3e4f5
Create Date: 2026-04-22
"""

import sqlalchemy as sa
from alembic import op

revision = "n1c2d3e4f5g6"
down_revision = "m0b1c2d3e4f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "attention_sessions",
        sa.Column("guest_language", sa.String(10), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("attention_sessions", "guest_language")
