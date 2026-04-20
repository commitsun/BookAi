"""Expand contacts.phone_code to VARCHAR(255) to accommodate email-derived synthetic codes.

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
Create Date: 2026-04-01 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

revision = "d1e2f3a4b5c6"
down_revision = "c0d1e2f3a4b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "contacts",
        "phone_code",
        type_=sa.String(255),
        existing_type=sa.String(20),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Only safe if no value exceeds 20 chars
    op.alter_column(
        "contacts",
        "phone_code",
        type_=sa.String(20),
        existing_type=sa.String(255),
        existing_nullable=False,
    )
