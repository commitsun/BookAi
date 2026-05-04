"""Add body_example and header_example to template translations.

Revision ID: u8j9k0l1m2n3
Revises: t7i8j9k0l1m2
Create Date: 2026-04-26
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "u8j9k0l1m2n3"
down_revision = "t7i8j9k0l1m2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "whatsapp_template_translations",
        sa.Column("body_example", JSONB, nullable=True),
    )
    op.add_column(
        "whatsapp_template_translations",
        sa.Column("header_example", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("whatsapp_template_translations", "header_example")
    op.drop_column("whatsapp_template_translations", "body_example")
