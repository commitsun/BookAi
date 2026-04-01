"""Add mock_mode to channel_endpoints

Revision ID: 7b8c9d0e1f2a
Revises: 6a7b8c9d0e1f
Create Date: 2026-03-30
"""

from alembic import op
import sqlalchemy as sa

revision = "7b8c9d0e1f2a"
down_revision = "6a7b8c9d0e1f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "channel_endpoints",
        sa.Column(
            "mock_mode",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("channel_endpoints", "mock_mode")
