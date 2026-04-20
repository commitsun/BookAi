"""Add message_media table for media attachments.

Revision ID: k8f9a0b1c2d3
Revises: j7e8f9a0b1c2
Create Date: 2026-04-20
"""

import sqlalchemy as sa
from alembic import op

revision = "k8f9a0b1c2d3"
down_revision = "j7e8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "message_media",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(20), nullable=False),
        sa.Column("mime_type", sa.String(100), nullable=True),
        sa.Column("filename", sa.String(255), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("wa_media_id", sa.String(255), nullable=True),
        sa.Column("storage_backend", sa.String(20), nullable=False, server_default="local"),
        sa.Column("storage_key", sa.Text(), nullable=True),
        sa.Column("transcription", sa.Text(), nullable=True),
        sa.Column("vision_description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("message_media")
