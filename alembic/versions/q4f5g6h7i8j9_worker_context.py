"""Add worker_context JSONB to attention_sessions.

Revision ID: q4f5g6h7i8j9
Revises: p3e4f5g6h7i8
Create Date: 2026-04-23
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "q4f5g6h7i8j9"
down_revision = "p3e4f5g6h7i8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "attention_sessions",
        sa.Column("worker_context", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("attention_sessions", "worker_context")
