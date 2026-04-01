"""multichannel conversations

Revision ID: 1b3f2a9c4d7e
Revises: 0ae7c78a7d91
Create Date: 2026-03-25

Changes:
  - conversations: remove channel_endpoint_id and wa_last_inbound_at;
                   UNIQUE constraint changes to (contact_id) only.
  - messages: add channel_endpoint_id NOT NULL.
  - New table conversation_channel_states: per-channel state
    (replaces wa_last_inbound_at on conversations).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "1b3f2a9c4d7e"
down_revision: Union[str, None] = "0ae7c78a7d91"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""

    -- =========================================================
    -- conversation_channel_states
    -- =========================================================

    CREATE TABLE conversation_channel_states (
        conversation_id     INTEGER     NOT NULL REFERENCES conversations(id),
        channel_endpoint_id INTEGER     NOT NULL REFERENCES channel_endpoints(id),
        last_inbound_at     TIMESTAMPTZ,
        PRIMARY KEY (conversation_id, channel_endpoint_id)
    );

    -- =========================================================
    -- Migrate wa_last_inbound_at → conversation_channel_states
    -- =========================================================

    -- Carry over existing window data: create one state row per conversation
    -- that already has a channel_endpoint and a non-null wa_last_inbound_at.
    INSERT INTO conversation_channel_states (conversation_id, channel_endpoint_id, last_inbound_at)
    SELECT id, channel_endpoint_id, wa_last_inbound_at
    FROM conversations
    WHERE channel_endpoint_id IS NOT NULL
      AND wa_last_inbound_at IS NOT NULL;

    -- Also create state rows (with null last_inbound_at) for conversations
    -- that have a channel_endpoint but no inbound message yet.
    INSERT INTO conversation_channel_states (conversation_id, channel_endpoint_id, last_inbound_at)
    SELECT id, channel_endpoint_id, NULL
    FROM conversations
    WHERE channel_endpoint_id IS NOT NULL
      AND wa_last_inbound_at IS NULL
    ON CONFLICT DO NOTHING;

    -- =========================================================
    -- messages: add channel_endpoint_id
    -- =========================================================

    -- Add as nullable first so existing rows don't violate the constraint.
    ALTER TABLE messages ADD COLUMN channel_endpoint_id INTEGER REFERENCES channel_endpoints(id);

    -- Back-fill from the conversation's current channel_endpoint_id.
    UPDATE messages m
    SET channel_endpoint_id = c.channel_endpoint_id
    FROM conversations c
    WHERE m.conversation_id = c.id
      AND c.channel_endpoint_id IS NOT NULL;

    -- Now enforce NOT NULL (any remaining nulls are orphaned rows with no channel — acceptable to fail loudly).
    ALTER TABLE messages ALTER COLUMN channel_endpoint_id SET NOT NULL;

    -- =========================================================
    -- conversations: drop old columns + update unique constraint
    -- =========================================================

    ALTER TABLE conversations DROP CONSTRAINT IF EXISTS uq_conversation;
    ALTER TABLE conversations DROP COLUMN IF EXISTS channel_endpoint_id;
    ALTER TABLE conversations DROP COLUMN IF EXISTS wa_last_inbound_at;

    ALTER TABLE conversations
        ADD CONSTRAINT uq_conversation_contact UNIQUE (contact_id);

    """)


def downgrade() -> None:
    op.execute("""

    -- Reverse unique constraint
    ALTER TABLE conversations DROP CONSTRAINT IF EXISTS uq_conversation_contact;

    -- Restore columns
    ALTER TABLE conversations ADD COLUMN channel_endpoint_id INTEGER REFERENCES channel_endpoints(id);
    ALTER TABLE conversations ADD COLUMN wa_last_inbound_at  TIMESTAMPTZ;

    -- Restore data from channel states (best-effort: take the single state row per conversation)
    UPDATE conversations c
    SET channel_endpoint_id = s.channel_endpoint_id,
        wa_last_inbound_at  = s.last_inbound_at
    FROM conversation_channel_states s
    WHERE s.conversation_id = c.id;

    ALTER TABLE conversations
        ADD CONSTRAINT uq_conversation UNIQUE (contact_id, channel_endpoint_id);

    -- Remove channel_endpoint_id from messages
    ALTER TABLE messages DROP COLUMN IF EXISTS channel_endpoint_id;

    -- Drop channel states table
    DROP TABLE IF EXISTS conversation_channel_states;

    """)
