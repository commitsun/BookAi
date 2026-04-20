"""Add Meta integration fields to templates: category, meta_template_id,
meta_status, header_text, body_text, footer_text, button_texts.

Revision ID: i6d7e8f9a0b1
Revises: h5c6d7e8f9a0
Create Date: 2026-04-20
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "i6d7e8f9a0b1"
down_revision = "h5c6d7e8f9a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # WhatsAppTemplate: category
    op.add_column(
        "whatsapp_templates",
        sa.Column("category", sa.String(20), nullable=False, server_default="UTILITY"),
    )

    # WhatsAppTemplateTranslation: Meta state + text fields
    op.add_column(
        "whatsapp_template_translations",
        sa.Column("meta_template_id", sa.String(255), nullable=True),
    )
    op.add_column(
        "whatsapp_template_translations",
        sa.Column("meta_status", sa.String(20), nullable=False, server_default="draft"),
    )
    op.add_column(
        "whatsapp_template_translations",
        sa.Column("header_text", sa.Text(), nullable=True),
    )
    op.add_column(
        "whatsapp_template_translations",
        sa.Column("body_text", sa.Text(), nullable=True),
    )
    op.add_column(
        "whatsapp_template_translations",
        sa.Column("footer_text", sa.Text(), nullable=True),
    )
    op.add_column(
        "whatsapp_template_translations",
        sa.Column("button_texts", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("whatsapp_template_translations", "button_texts")
    op.drop_column("whatsapp_template_translations", "footer_text")
    op.drop_column("whatsapp_template_translations", "body_text")
    op.drop_column("whatsapp_template_translations", "header_text")
    op.drop_column("whatsapp_template_translations", "meta_status")
    op.drop_column("whatsapp_template_translations", "meta_template_id")
    op.drop_column("whatsapp_templates", "category")
