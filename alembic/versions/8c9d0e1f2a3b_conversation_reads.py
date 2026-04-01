"""Add conversation_reads table

Revision ID: 8c9d0e1f2a3b
Revises: 7b8c9d0e1f2a
Create Date: 2026-03-30
"""

from alembic import op
import sqlalchemy as sa

revision = "8c9d0e1f2a3b"
down_revision = "7b8c9d0e1f2a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_reads",
        sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("conversations.id"), primary_key=True),
        sa.Column("property_id", sa.Integer(), sa.ForeignKey("properties.id"), primary_key=True),
        sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("conversation_reads")
