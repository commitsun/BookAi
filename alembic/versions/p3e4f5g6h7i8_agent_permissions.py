"""Add agents table for persisted permission checks, and odoo_user_id to sessions.

Revision ID: p3e4f5g6h7i8
Revises: o2d3e4f5g6h7
Create Date: 2026-04-22
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "p3e4f5g6h7i8"
down_revision = "o2d3e4f5g6h7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("instance_id", sa.Integer(), sa.ForeignKey("instances.id"), nullable=False),
        sa.Column("odoo_agent_id", sa.Integer(), nullable=False),
        sa.Column("technical_name", sa.String(255), nullable=False),
        sa.Column("is_supervisor", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("god_mode", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("caller_type", sa.String(50), nullable=False, server_default="any"),
        sa.Column("property_scope_ids", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("allowed_user_ids", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("allowed_agent_names", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("synced_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("instance_id", "odoo_agent_id", name="uq_agent_instance_odoo_id"),
        sa.UniqueConstraint("instance_id", "technical_name", name="uq_agent_instance_name"),
    )

    op.add_column(
        "attention_sessions",
        sa.Column("odoo_user_id", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("attention_sessions", "odoo_user_id")
    op.drop_table("agents")
