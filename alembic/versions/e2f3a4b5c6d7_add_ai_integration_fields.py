"""Add AI integration fields: Odoo/LLM config on instances, ai_enabled on
properties, active_agent_id on sessions, and 'ai' value to message_sender enum.

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-04-20
"""

import sqlalchemy as sa
from alembic import op

revision = "e2f3a4b5c6d7"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- instances: Roomdoo SDK connection fields ---
    op.add_column("instances", sa.Column("roomdoo_db", sa.String(255), nullable=True))
    op.add_column("instances", sa.Column("roomdoo_username", sa.String(255), nullable=True))
    op.add_column("instances", sa.Column("roomdoo_password", sa.Text(), nullable=True))

    # --- instances: Router LLM credentials ---
    op.add_column("instances", sa.Column("router_llm_provider", sa.String(50), nullable=True))
    op.add_column("instances", sa.Column("router_llm_api_key", sa.Text(), nullable=True))
    op.add_column("instances", sa.Column("router_llm_api_base_url", sa.Text(), nullable=True))
    op.add_column("instances", sa.Column("router_llm_model", sa.String(255), nullable=True))

    # --- properties: AI feature flag ---
    op.add_column(
        "properties",
        sa.Column("ai_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    # --- attention_sessions: pinned agent from Odoo ---
    op.add_column("attention_sessions", sa.Column("active_agent_id", sa.Integer(), nullable=True))

    # --- message_sender enum: add 'ai' value ---
    # NOTE: ALTER TYPE ... ADD VALUE is not transactional in PostgreSQL < 12,
    # and cannot be rolled back even in PG 12+. This is a one-way operation.
    op.execute("ALTER TYPE message_sender ADD VALUE IF NOT EXISTS 'ai'")


def downgrade() -> None:
    op.drop_column("attention_sessions", "active_agent_id")
    op.drop_column("properties", "ai_enabled")
    op.drop_column("instances", "router_llm_model")
    op.drop_column("instances", "router_llm_api_base_url")
    op.drop_column("instances", "router_llm_api_key")
    op.drop_column("instances", "router_llm_provider")
    op.drop_column("instances", "roomdoo_password")
    op.drop_column("instances", "roomdoo_username")
    op.drop_column("instances", "roomdoo_db")
    # NOTE: Cannot remove 'ai' from message_sender enum in PostgreSQL.
    # A full enum rebuild would be needed, which is out of scope for downgrade.
