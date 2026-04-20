"""Add parameters JSONB to whatsapp_template_translations for named placeholder mapping.

Revision ID: j7e8f9a0b1c2
Revises: i6d7e8f9a0b1
Create Date: 2026-04-20
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "j7e8f9a0b1c2"
down_revision = "i6d7e8f9a0b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "whatsapp_template_translations",
        sa.Column("parameters", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("whatsapp_template_translations", "parameters")
