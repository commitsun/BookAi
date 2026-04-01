"""Add default_language to instances

Revision ID: ae1b2c3d4e5f
Revises: 9d0e1f2a3b4c
Create Date: 2026-03-31
"""

from alembic import op
import sqlalchemy as sa

revision = "ae1b2c3d4e5f"
down_revision = "9d0e1f2a3b4c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "instances",
        sa.Column(
            "default_language",
            sa.String(5),
            nullable=False,
            server_default="es",
        ),
    )


def downgrade() -> None:
    op.drop_column("instances", "default_language")
