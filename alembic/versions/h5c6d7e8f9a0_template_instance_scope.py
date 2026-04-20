"""Add instance_id to whatsapp_templates and scope code uniqueness per instance.

Revision ID: h5c6d7e8f9a0
Revises: g4b5c6d7e8f9
Create Date: 2026-04-20
"""

import sqlalchemy as sa
from alembic import op

revision = "h5c6d7e8f9a0"
down_revision = "g4b5c6d7e8f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add instance_id as nullable first
    op.add_column("whatsapp_templates", sa.Column("instance_id", sa.Integer(), nullable=True))

    # Backfill existing templates with instance 1
    op.execute("UPDATE whatsapp_templates SET instance_id = 1 WHERE instance_id IS NULL")

    # Make NOT NULL
    op.alter_column("whatsapp_templates", "instance_id", nullable=False)

    # Add FK
    op.create_foreign_key(
        "fk_whatsapp_templates_instance_id",
        "whatsapp_templates", "instances",
        ["instance_id"], ["id"],
    )

    # Drop old unique on code alone
    op.drop_constraint("uq_whatsapp_templates_code", "whatsapp_templates", type_="unique")

    # Create new unique (code, instance_id)
    op.create_unique_constraint(
        "uq_template_code_instance",
        "whatsapp_templates",
        ["code", "instance_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_template_code_instance", "whatsapp_templates", type_="unique")
    op.drop_constraint("fk_whatsapp_templates_instance_id", "whatsapp_templates", type_="foreignkey")
    op.create_unique_constraint("uq_whatsapp_templates_code", "whatsapp_templates", ["code"])
    op.drop_column("whatsapp_templates", "instance_id")
