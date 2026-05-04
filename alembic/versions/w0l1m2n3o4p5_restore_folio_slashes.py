"""Restore original slashes in folio external codes.

The previous normalization replaced '/' with '_' for URL safety, but
FastAPI's {param:path} handles slashes natively. Storing the original
PMS code avoids confusion between what Odoo shows and what BookAI returns.

Revision ID: w0l1m2n3o4p5
Revises: v9k0l1m2n3o4
Create Date: 2026-05-04
"""

from alembic import op

revision = "w0l1m2n3o4p5"
down_revision = "v9k0l1m2n3o4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Odoo folio codes follow the pattern NNN/NN/NNNNNN.
    # The previous migration replaced '/' with '_', producing NNN_NN_NNNNNN.
    # Restore the original format by reversing that transformation.
    # Only targets codes matching the known Odoo pattern to avoid false positives.
    op.execute("""
        UPDATE folios
        SET odoo_external_code = REGEXP_REPLACE(
            odoo_external_code,
            '^([0-9]+)_([0-9]+)_([0-9]+)$',
            '\\1/\\2/\\3'
        )
        WHERE odoo_external_code ~ '^[0-9]+_[0-9]+_[0-9]+$'
    """)


def downgrade() -> None:
    op.execute("""
        UPDATE folios
        SET odoo_external_code = REPLACE(odoo_external_code, '/', '_')
        WHERE odoo_external_code LIKE '%/%'
    """)
