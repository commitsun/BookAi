"""Add property sync fields: odoo_property_id, bookai_mode, tz, email, phone.
Replace ai_enabled boolean with bookai_mode string.

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-04-20
"""

import sqlalchemy as sa
from alembic import op

revision = "f3a4b5c6d7e8"
down_revision = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- Add new columns ---
    op.add_column("properties", sa.Column("odoo_property_id", sa.Integer(), nullable=True))
    op.create_unique_constraint("uq_properties_odoo_property_id", "properties", ["odoo_property_id"])

    op.add_column(
        "properties",
        sa.Column("bookai_mode", sa.String(20), nullable=False, server_default="disabled"),
    )
    op.add_column("properties", sa.Column("tz", sa.String(50), nullable=True))
    op.add_column("properties", sa.Column("email", sa.String(255), nullable=True))
    op.add_column("properties", sa.Column("phone", sa.String(50), nullable=True))

    # --- Migrate ai_enabled → bookai_mode ---
    op.execute("UPDATE properties SET bookai_mode = 'ai' WHERE ai_enabled = true")
    op.execute("UPDATE properties SET bookai_mode = 'disabled' WHERE ai_enabled = false")

    # --- Drop ai_enabled ---
    op.drop_column("properties", "ai_enabled")


def downgrade() -> None:
    op.add_column(
        "properties",
        sa.Column("ai_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.execute("UPDATE properties SET ai_enabled = true WHERE bookai_mode = 'ai'")

    op.drop_column("properties", "phone")
    op.drop_column("properties", "email")
    op.drop_column("properties", "tz")
    op.drop_column("properties", "bookai_mode")
    op.drop_constraint("uq_properties_odoo_property_id", "properties", type_="unique")
    op.drop_column("properties", "odoo_property_id")
