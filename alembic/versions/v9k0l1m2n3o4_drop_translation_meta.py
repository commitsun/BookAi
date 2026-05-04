"""Drop meta_template_id and meta_status from whatsapp_template_translations.

These fields are now tracked per-WABA in template_translation_waba.

Revision ID: v9k0l1m2n3o4
Revises: u8j9k0l1m2n3
Create Date: 2026-04-29
"""

from alembic import op
import sqlalchemy as sa

revision = "v9k0l1m2n3o4"
down_revision = "u8j9k0l1m2n3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("whatsapp_template_translations", "meta_template_id")
    op.drop_column("whatsapp_template_translations", "meta_status")


def downgrade() -> None:
    op.add_column(
        "whatsapp_template_translations",
        sa.Column("meta_status", sa.String(20), nullable=False, server_default="draft"),
    )
    op.add_column(
        "whatsapp_template_translations",
        sa.Column("meta_template_id", sa.String(255), nullable=True),
    )
