"""Add verify_token to channel_endpoints

Revision ID: 6a7b8c9d0e1f
Revises: 5f6a7b8c9d0e
Create Date: 2026-03-30
"""

from alembic import op
import sqlalchemy as sa

revision = "6a7b8c9d0e1f"
down_revision = "5f6a7b8c9d0e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "channel_endpoints",
        sa.Column("verify_token", sa.String(255), nullable=True),
    )
    op.create_unique_constraint(
        "uq_channel_endpoints_verify_token",
        "channel_endpoints",
        ["verify_token"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_channel_endpoints_verify_token", "channel_endpoints", type_="unique"
    )
    op.drop_column("channel_endpoints", "verify_token")
