"""Add template_translation_waba table for multi-WABA template support.

Revision ID: t7i8j9k0l1m2
Revises: s6h7i8j9k0l1
Create Date: 2026-04-25
"""

import sqlalchemy as sa
from alembic import op

revision = "t7i8j9k0l1m2"
down_revision = "s6h7i8j9k0l1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "template_translation_waba",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("translation_id", sa.Integer(),
                  sa.ForeignKey("whatsapp_template_translations.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("waba_id", sa.String(255), nullable=False),
        sa.Column("meta_template_id", sa.String(255), nullable=True),
        sa.Column("meta_status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("translation_id", "waba_id", name="uq_translation_waba"),
    )


def downgrade() -> None:
    op.drop_table("template_translation_waba")
