"""Add mcp_server_configs table for auto-reconnect on restart.

Revision ID: o2d3e4f5g6h7
Revises: n1c2d3e4f5g6
Create Date: 2026-04-22
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "o2d3e4f5g6h7"
down_revision = "n1c2d3e4f5g6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_server_configs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("instance_id", sa.Integer(), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("transport_type", sa.String(20), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["instance_id"], ["instances.id"]),
        sa.UniqueConstraint("instance_id", "server_id", name="uq_mcp_instance_server"),
    )


def downgrade() -> None:
    op.drop_table("mcp_server_configs")
