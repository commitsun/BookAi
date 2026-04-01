"""message translations

Revision ID: 2c5d8e1f3a6b
Revises: 1b3f2a9c4d7e
Create Date: 2026-03-25

Changes:
  - messages: add content_language VARCHAR(10) nullable.
  - New table message_translations: (message_id, language) → content.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "2c5d8e1f3a6b"
down_revision: Union[str, None] = "1b3f2a9c4d7e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""

    ALTER TABLE messages
        ADD COLUMN content_language VARCHAR(10);

    CREATE TABLE message_translations (
        message_id  BIGINT      NOT NULL REFERENCES messages(id),
        language    VARCHAR(10) NOT NULL,
        content     TEXT        NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (message_id, language)
    );

    CREATE INDEX ix_message_translations_message_id
        ON message_translations (message_id);

    """)


def downgrade() -> None:
    op.execute("""

    DROP INDEX IF EXISTS ix_message_translations_message_id;
    DROP TABLE IF EXISTS message_translations;
    ALTER TABLE messages DROP COLUMN IF EXISTS content_language;

    """)
