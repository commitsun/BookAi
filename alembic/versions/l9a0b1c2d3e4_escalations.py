"""Add escalations table, escalation_id FK on messages, caller_type on sessions.

Revision ID: l9a0b1c2d3e4
Revises: k8f9a0b1c2d3
Create Date: 2026-04-21
"""

import sqlalchemy as sa
from alembic import op

revision = "l9a0b1c2d3e4"
down_revision = "k8f9a0b1c2d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Escalations table
    op.create_table(
        "escalations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("escalation_type", sa.String(30), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("context", sa.Text(), nullable=True),
        sa.Column("guest_message", sa.Text(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("draft_response", sa.Text(), nullable=True),
        sa.Column("ai_was_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("resolved_by", sa.String(255), nullable=True),
        sa.Column("resolution_medium", sa.String(30), nullable=True),
        sa.Column("resolution_notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["session_id"], ["attention_sessions.id"]),
    )

    # escalation_id FK on messages
    op.add_column(
        "messages",
        sa.Column("escalation_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_messages_escalation_id",
        "messages", "escalations",
        ["escalation_id"], ["id"],
    )

    # caller_type on sessions
    op.add_column(
        "attention_sessions",
        sa.Column("caller_type", sa.String(20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("attention_sessions", "caller_type")
    op.drop_constraint("fk_messages_escalation_id", "messages", type_="foreignkey")
    op.drop_column("messages", "escalation_id")
    op.drop_table("escalations")
