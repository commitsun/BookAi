"""Add roomdoo_staff_phones JSONB to instances.

Revision ID: m0b1c2d3e4f5
Revises: l9a0b1c2d3e4
Create Date: 2026-04-21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "m0b1c2d3e4f5"
down_revision = "l9a0b1c2d3e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "instances",
        sa.Column("roomdoo_staff_phones", postgresql.JSONB(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("instances", "roomdoo_staff_phones")
