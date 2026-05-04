"""Add internal conversation fields: title, conversation_type, odoo_user_id, odoo_user_login.
Drop unique constraint on contact_id to allow multiple internal threads per user.

Revision ID: s6h7i8j9k0l1
Revises: r5g6h7i8j9k0
Create Date: 2026-04-25
"""

import sqlalchemy as sa
from alembic import op

revision = "s6h7i8j9k0l1"
down_revision = "r5g6h7i8j9k0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("title", sa.String(255), nullable=True))
    op.add_column("conversations", sa.Column("conversation_type", sa.String(20), nullable=False, server_default="guest"))
    op.add_column("conversations", sa.Column("odoo_user_id", sa.Integer(), nullable=True))
    op.add_column("conversations", sa.Column("odoo_user_login", sa.String(255), nullable=True))

    # Drop unique constraint on contact_id (allow multiple internal threads)
    op.drop_constraint("uq_conversation_contact", "conversations", type_="unique")


def downgrade() -> None:
    op.create_unique_constraint("uq_conversation_contact", "conversations", ["contact_id"])
    op.drop_column("conversations", "odoo_user_login")
    op.drop_column("conversations", "odoo_user_id")
    op.drop_column("conversations", "conversation_type")
    op.drop_column("conversations", "title")
