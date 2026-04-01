"""Normalize folio external codes: replace URL-unsafe chars with '_'

Revision ID: bf2c3d4e5f6a
Revises: ae1b2c3d4e5f
Create Date: 2026-03-31
"""

from alembic import op

revision = "bf2c3d4e5f6a"
down_revision = "ae1b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Replace '/' with '_' in all existing folio codes.
    # Additional unsafe chars (?, #, %, &, =, space) are also replaced
    # to match the normalize_code() function in folio_repo.py.
    op.execute("""
        UPDATE folios
        SET odoo_external_code = REGEXP_REPLACE(
            odoo_external_code,
            '[/?#%&= ]',
            '_',
            'g'
        )
        WHERE odoo_external_code ~ '[/?#%&= ]'
    """)


def downgrade() -> None:
    # Not reversible — the original codes with special chars cannot be
    # recovered from the normalized form without external knowledge.
    pass
