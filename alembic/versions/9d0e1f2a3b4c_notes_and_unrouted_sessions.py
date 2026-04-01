"""Add message kind, delivery skipped, unrouted sessions

Revision ID: 9d0e1f2a3b4c
Revises: 8c9d0e1f2a3b
Create Date: 2026-03-31
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "9d0e1f2a3b4c"
down_revision = "8c9d0e1f2a3b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. New enum: message_kind
    message_kind = postgresql.ENUM("message", "note", name="message_kind")
    message_kind.create(op.get_bind())

    # 2. Add 'skipped' to existing delivery_status enum
    op.execute("ALTER TYPE delivery_status ADD VALUE IF NOT EXISTS 'skipped'")

    # 3. Add kind column to messages (default 'message')
    op.add_column(
        "messages",
        sa.Column(
            "kind",
            sa.Enum("message", "note", name="message_kind"),
            nullable=False,
            server_default="message",
        ),
    )

    # 4. Make messages.channel_endpoint_id nullable (notes have no WA channel)
    op.alter_column("messages", "channel_endpoint_id", nullable=True)

    # 5. Make attention_sessions.property_id nullable (unrouted sessions)
    op.alter_column("attention_sessions", "property_id", nullable=True)

    # 6. Partial index for quick note lookups
    op.create_index(
        "ix_messages_kind_note",
        "messages",
        ["kind"],
        postgresql_where=sa.text("kind = 'note'"),
    )


def downgrade() -> None:
    # NOTE: removing a value from a PostgreSQL enum requires recreating it.
    # The 'skipped' value in delivery_status cannot be removed automatically.
    # Ensure no rows use delivery_status='skipped' before running downgrade.

    op.drop_index("ix_messages_kind_note", table_name="messages")

    op.alter_column("attention_sessions", "property_id", nullable=False)

    op.alter_column("messages", "channel_endpoint_id", nullable=False)

    op.drop_column("messages", "kind")

    op.execute("DROP TYPE message_kind")
