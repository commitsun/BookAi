"""Add email channel support: config JSONB on channel_endpoints, email on contacts,
email_message_metadata table, email_attachments table, and extend delivery_status enum.

Revision ID: c0d1e2f3a4b5
Revises: bf2c3d4e5f6a
Create Date: 2026-04-01
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c0d1e2f3a4b5"
down_revision = "bf2c3d4e5f6a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- channel_endpoints: add config JSONB ---
    op.add_column(
        "channel_endpoints",
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )

    # --- contacts: add email ---
    op.add_column(
        "contacts",
        sa.Column("email", sa.String(255), nullable=True),
    )
    op.create_unique_constraint("uq_contacts_email", "contacts", ["email"])

    # --- delivery_status enum: add accepted, bounced ---
    # PostgreSQL requires adding enum values outside a transaction.
    op.execute("COMMIT")
    op.execute("ALTER TYPE delivery_status ADD VALUE IF NOT EXISTS 'accepted'")
    op.execute("ALTER TYPE delivery_status ADD VALUE IF NOT EXISTS 'bounced'")
    op.execute("BEGIN")

    # --- email_message_metadata table ---
    op.create_table(
        "email_message_metadata",
        sa.Column(
            "id", sa.BigInteger(), autoincrement=True, nullable=False
        ),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.Column("in_reply_to", sa.Text(), nullable=True),
        sa.Column("references", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=False, server_default=""),
        sa.Column("from_address", sa.Text(), nullable=False),
        sa.Column("from_name", sa.Text(), nullable=True),
        sa.Column(
            "to_addresses",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "cc_addresses",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("reply_to", sa.Text(), nullable=True),
        sa.Column("text_body", sa.Text(), nullable=True),
        sa.Column("html_body", sa.Text(), nullable=True),
        sa.Column("mailgun_id", sa.Text(), nullable=True),
        sa.Column("provider_event_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["message_id"], ["messages.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id", name="uq_email_meta_message_id"),
        sa.UniqueConstraint(
            "provider_message_id", name="uq_email_meta_provider_message_id"
        ),
    )
    op.create_index(
        "idx_email_meta_in_reply_to",
        "email_message_metadata",
        ["in_reply_to"],
        postgresql_where=sa.text("in_reply_to IS NOT NULL"),
    )

    # --- email_attachments table ---
    op.create_table(
        "email_attachments",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("email_metadata_id", sa.BigInteger(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column(
            "content_type",
            sa.Text(),
            nullable=False,
            server_default="application/octet-stream",
        ),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column(
            "inline", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("content_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["message_id"], ["messages.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["email_metadata_id"],
            ["email_message_metadata.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_email_attachments_message",
        "email_attachments",
        ["message_id"],
    )


def downgrade() -> None:
    op.drop_table("email_attachments")
    op.drop_table("email_message_metadata")
    op.drop_constraint("uq_contacts_email", "contacts", type_="unique")
    op.drop_column("contacts", "email")
    op.drop_column("channel_endpoints", "config")
    # NOTE: PostgreSQL does not support removing enum values.
    # The 'accepted' and 'bounced' values remain in the delivery_status enum after downgrade.
