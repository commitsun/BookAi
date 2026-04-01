"""template strategy b: parent + per-language translations

Revision ID: 3d4f1e2a8c5b
Revises: 2c5d8e1f3a6b
Create Date: 2026-03-26

Separates whatsapp_templates into:
  - whatsapp_templates          (concept/parent: just code)
  - whatsapp_template_translations  (per-language Meta entity)
  - template_translation_properties (translation ↔ property link)

The old template_properties table (template → property) is removed;
property availability is now scoped at the translation level since
a property may only support certain languages of a given template.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3d4f1e2a8c5b"
down_revision: Union[str, None] = "2c5d8e1f3a6b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create whatsapp_template_translations
    op.create_table(
        "whatsapp_template_translations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "template_id",
            sa.Integer(),
            sa.ForeignKey("whatsapp_templates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("whatsapp_name", sa.String(255), nullable=False),
        sa.Column("language", sa.String(10), nullable=False, server_default="es"),
        sa.Column(
            "components",
            postgresql.JSONB(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("template_id", "language", name="uq_template_translation_lang"),
    )

    # 2. Create template_translation_properties (translation ↔ property)
    op.create_table(
        "template_translation_properties",
        sa.Column(
            "translation_id",
            sa.Integer(),
            sa.ForeignKey("whatsapp_template_translations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "property_id",
            sa.Integer(),
            sa.ForeignKey("properties.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )

    # 3. Migrate existing data: move each current template row into a translation row
    #    then seed template_translation_properties from old template_properties.
    #    In dev the tables are empty; this handles production if needed.
    op.execute("""
        INSERT INTO whatsapp_template_translations
            (template_id, whatsapp_name, language, components, active, created_at)
        SELECT id, whatsapp_name, language, components, active, created_at
        FROM whatsapp_templates
    """)

    op.execute("""
        INSERT INTO template_translation_properties (translation_id, property_id)
        SELECT wtt.id, tp.property_id
        FROM template_properties tp
        JOIN whatsapp_templates wt ON wt.id = tp.template_id
        JOIN whatsapp_template_translations wtt
            ON wtt.template_id = wt.id AND wtt.language = wt.language
    """)

    # 4. Drop old junction table
    op.drop_table("template_properties")

    # 5. Drop constraint uq_template_code_language before removing columns
    op.drop_constraint("uq_template_code_language", "whatsapp_templates", type_="unique")

    # 6. Remove columns that moved to whatsapp_template_translations
    op.drop_column("whatsapp_templates", "whatsapp_name")
    op.drop_column("whatsapp_templates", "language")
    op.drop_column("whatsapp_templates", "components")
    op.drop_column("whatsapp_templates", "active")

    # 7. Add unique constraint on code alone
    op.create_unique_constraint("uq_whatsapp_templates_code", "whatsapp_templates", ["code"])


def downgrade() -> None:
    # Reverse: restore old columns and rebuild old tables
    op.drop_constraint("uq_whatsapp_templates_code", "whatsapp_templates", type_="unique")

    op.add_column("whatsapp_templates", sa.Column("whatsapp_name", sa.String(255), nullable=True))
    op.add_column("whatsapp_templates", sa.Column("language", sa.String(10), nullable=True))
    op.add_column(
        "whatsapp_templates",
        sa.Column("components", postgresql.JSONB(), nullable=True, server_default="[]"),
    )
    op.add_column(
        "whatsapp_templates",
        sa.Column("active", sa.Boolean(), nullable=True, server_default="true"),
    )

    # Restore data from translations (first translation per template wins)
    op.execute("""
        UPDATE whatsapp_templates wt
        SET whatsapp_name = wtt.whatsapp_name,
            language      = wtt.language,
            components    = wtt.components,
            active        = wtt.active
        FROM (
            SELECT DISTINCT ON (template_id)
                template_id, whatsapp_name, language, components, active
            FROM whatsapp_template_translations
            ORDER BY template_id, id
        ) wtt
        WHERE wt.id = wtt.template_id
    """)

    op.alter_column("whatsapp_templates", "whatsapp_name", nullable=False)
    op.alter_column("whatsapp_templates", "language", nullable=False)
    op.alter_column("whatsapp_templates", "components", nullable=False)
    op.alter_column("whatsapp_templates", "active", nullable=False)

    op.create_unique_constraint(
        "uq_template_code_language", "whatsapp_templates", ["code", "language"]
    )

    op.create_table(
        "template_properties",
        sa.Column(
            "template_id",
            sa.Integer(),
            sa.ForeignKey("whatsapp_templates.id"),
            primary_key=True,
        ),
        sa.Column(
            "property_id",
            sa.Integer(),
            sa.ForeignKey("properties.id"),
            primary_key=True,
        ),
    )

    # Restore template_properties from template_translation_properties
    op.execute("""
        INSERT INTO template_properties (template_id, property_id)
        SELECT DISTINCT wtt.template_id, ttp.property_id
        FROM template_translation_properties ttp
        JOIN whatsapp_template_translations wtt ON wtt.id = ttp.translation_id
    """)

    op.drop_table("template_translation_properties")
    op.drop_table("whatsapp_template_translations")
