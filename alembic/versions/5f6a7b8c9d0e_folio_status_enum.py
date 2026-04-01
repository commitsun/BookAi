"""folio status enum: draft, confirm, onboard, done, cancel

Converts folios.status from VARCHAR(50) to a typed PostgreSQL enum.
Also migrates demo/existing data to the canonical values.

Revision ID: 5f6a7b8c9d0e
Revises: 4e5f6a7b8c9d
Create Date: 2026-03-30
"""

import sqlalchemy as sa
from alembic import op

revision = "5f6a7b8c9d0e"
down_revision = "4e5f6a7b8c9d"
branch_labels = None
depends_on = None

_VALUES = ("draft", "confirm", "onboard", "done", "cancel")
_TYPE = sa.Enum(*_VALUES, name="folio_status")


def upgrade() -> None:
    # Normalise any pre-existing string values to the canonical enum members
    op.execute("UPDATE folios SET status = 'confirm'  WHERE status IN ('confirmed')")
    op.execute("UPDATE folios SET status = 'onboard'  WHERE status IN ('checkin', 'checked_in')")
    op.execute("UPDATE folios SET status = 'done'     WHERE status IN ('checkout', 'checked_out')")
    op.execute("UPDATE folios SET status = 'cancel'   WHERE status IN ('cancelled', 'canceled')")
    # NULL out any remaining unknown values so the CAST below does not fail
    op.execute(
        f"UPDATE folios SET status = NULL "
        f"WHERE status IS NOT NULL AND status NOT IN {_VALUES}"
    )

    _TYPE.create(op.get_bind())
    op.execute(
        "ALTER TABLE folios "
        "ALTER COLUMN status TYPE folio_status USING status::folio_status"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE folios ALTER COLUMN status TYPE VARCHAR(50) USING status::VARCHAR"
    )
    _TYPE.drop(op.get_bind())
