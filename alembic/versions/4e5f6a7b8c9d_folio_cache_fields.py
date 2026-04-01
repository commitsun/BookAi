"""folio cache fields: status, pending_payment, synced_at

Adds dynamic reservation fields to the folios table so BookAI can
cache the data pushed by Roomdoo and the app can search without
querying Odoo directly.

Revision ID: 4e5f6a7b8c9d
Revises: 3d4f1e2a8c5b
Create Date: 2026-03-28
"""

import sqlalchemy as sa
from alembic import op

revision = "4e5f6a7b8c9d"
down_revision = "3d4f1e2a8c5b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("folios", sa.Column("status", sa.String(50), nullable=True))
    op.add_column(
        "folios",
        sa.Column("pending_payment_amount", sa.Numeric(10, 2), nullable=True),
    )
    op.add_column(
        "folios", sa.Column("pending_payment_currency", sa.String(3), nullable=True)
    )
    op.add_column(
        "folios",
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("folios", "synced_at")
    op.drop_column("folios", "pending_payment_currency")
    op.drop_column("folios", "pending_payment_amount")
    op.drop_column("folios", "status")
