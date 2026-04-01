"""initial schema

Revision ID: 0ae7c78a7d91
Revises:
Create Date: 2026-03-24

"""

from typing import Sequence, Union

from alembic import op

revision: str = "0ae7c78a7d91"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""

    -- =========================================================
    -- ENUMS
    -- =========================================================

    CREATE TYPE session_status      AS ENUM ('active', 'closed');
    CREATE TYPE message_direction   AS ENUM ('inbound', 'outbound');
    CREATE TYPE message_sender      AS ENUM ('guest', 'agent', 'system');
    CREATE TYPE delivery_status     AS ENUM ('pending', 'sent', 'delivered', 'read', 'failed');
    CREATE TYPE routing_status      AS ENUM ('routed', 'unassigned', 'ambiguous');

    -- =========================================================
    -- INSTANCES
    -- =========================================================

    CREATE TABLE instances (
        id              SERIAL PRIMARY KEY,
        instance_url    TEXT        NOT NULL UNIQUE,
        bearer_token    TEXT        NOT NULL UNIQUE,
        bookai_enabled  BOOLEAN     NOT NULL DEFAULT true,
        active          BOOLEAN     NOT NULL DEFAULT true,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    -- =========================================================
    -- CHANNEL ENDPOINTS
    -- =========================================================

    CREATE TABLE channel_endpoints (
        id              SERIAL PRIMARY KEY,
        channel         VARCHAR(50) NOT NULL DEFAULT 'whatsapp',
        external_code   VARCHAR(255) NOT NULL UNIQUE,
        access_token    TEXT        NOT NULL,
        account_id      VARCHAR(255),
        display_number  VARCHAR(50),
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    -- =========================================================
    -- PROPERTIES
    -- =========================================================

    CREATE TABLE properties (
        id                      SERIAL PRIMARY KEY,
        instance_id             INTEGER     NOT NULL REFERENCES instances(id),
        name                    VARCHAR(255) NOT NULL,
        roomdoo_external_code   VARCHAR(255) NOT NULL,
        channel_endpoint_id     INTEGER     REFERENCES channel_endpoints(id),
        created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    -- =========================================================
    -- CONTACTS
    -- =========================================================

    CREATE TABLE contacts (
        id              SERIAL PRIMARY KEY,
        phone_code      VARCHAR(20) NOT NULL UNIQUE,
        display_name    VARCHAR(255),
        country_code    CHAR(2),
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    -- =========================================================
    -- CONVERSATIONS
    -- =========================================================

    CREATE TABLE conversations (
        id                      SERIAL PRIMARY KEY,
        contact_id              INTEGER     NOT NULL REFERENCES contacts(id),
        channel_endpoint_id     INTEGER     NOT NULL REFERENCES channel_endpoints(id),
        wa_last_inbound_at      TIMESTAMPTZ,
        created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_conversation UNIQUE (contact_id, channel_endpoint_id)
    );

    -- =========================================================
    -- ATTENTION SESSIONS
    -- =========================================================

    CREATE TABLE attention_sessions (
        id              SERIAL PRIMARY KEY,
        conversation_id INTEGER         NOT NULL REFERENCES conversations(id),
        property_id     INTEGER         NOT NULL REFERENCES properties(id),
        status          session_status  NOT NULL DEFAULT 'active',
        opened_at       TIMESTAMPTZ     NOT NULL DEFAULT now(),
        closed_at       TIMESTAMPTZ,
        created_at      TIMESTAMPTZ     NOT NULL DEFAULT now()
    );

    CREATE INDEX ix_attention_sessions_conversation_status
        ON attention_sessions (conversation_id, status);

    -- =========================================================
    -- FOLIOS
    -- =========================================================

    CREATE TABLE folios (
        id                  SERIAL PRIMARY KEY,
        odoo_external_code  VARCHAR(255) NOT NULL UNIQUE,
        odoo_folio_id       INTEGER,
        checkin_date        DATE,
        checkout_date       DATE,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    -- =========================================================
    -- SESSION FOLIOS (N:M)
    -- =========================================================

    CREATE TABLE session_folios (
        session_id  INTEGER     NOT NULL REFERENCES attention_sessions(id),
        folio_id    INTEGER     NOT NULL REFERENCES folios(id),
        attached_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (session_id, folio_id)
    );

    -- =========================================================
    -- WHATSAPP TEMPLATES
    -- =========================================================

    CREATE TABLE whatsapp_templates (
        id              SERIAL PRIMARY KEY,
        code            VARCHAR(255) NOT NULL,
        whatsapp_name   VARCHAR(255) NOT NULL,
        language        VARCHAR(10)  NOT NULL DEFAULT 'es',
        components      JSONB        NOT NULL,
        active          BOOLEAN      NOT NULL DEFAULT true,
        created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
        CONSTRAINT uq_template_code_language UNIQUE (code, language)
    );

    CREATE TABLE template_properties (
        template_id INTEGER NOT NULL REFERENCES whatsapp_templates(id),
        property_id INTEGER NOT NULL REFERENCES properties(id),
        PRIMARY KEY (template_id, property_id)
    );

    -- =========================================================
    -- MESSAGES (unified log)
    -- =========================================================

    CREATE TABLE messages (
        id                  BIGSERIAL           PRIMARY KEY,
        conversation_id     INTEGER             NOT NULL REFERENCES conversations(id),
        attention_session_id INTEGER            REFERENCES attention_sessions(id),
        direction           message_direction   NOT NULL,
        sender              message_sender      NOT NULL,
        content             TEXT,
        agent_user_id       INTEGER,
        agent_display_name  VARCHAR(255),
        wa_message_id       VARCHAR(255)        UNIQUE,
        wa_message_type     VARCHAR(50)         NOT NULL DEFAULT 'text',
        read_at             TIMESTAMPTZ,
        template_code       VARCHAR(255),
        template_language   VARCHAR(10),
        template_payload    JSONB,
        routing_status      routing_status,
        idempotency_key     VARCHAR(255)        UNIQUE,
        delivery_status     delivery_status     NOT NULL DEFAULT 'pending',
        delivery_error      TEXT,
        delivered_at        TIMESTAMPTZ,
        created_at          TIMESTAMPTZ         NOT NULL DEFAULT now()
    );

    CREATE INDEX ix_messages_conversation_id ON messages (conversation_id);
    CREATE INDEX ix_messages_created_at      ON messages (created_at DESC);

    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS messages;
        DROP TABLE IF EXISTS template_properties;
        DROP TABLE IF EXISTS whatsapp_templates;
        DROP TABLE IF EXISTS session_folios;
        DROP TABLE IF EXISTS folios;
        DROP TABLE IF EXISTS attention_sessions;
        DROP TABLE IF EXISTS conversations;
        DROP TABLE IF EXISTS contacts;
        DROP TABLE IF EXISTS properties;
        DROP TABLE IF EXISTS channel_endpoints;
        DROP TABLE IF EXISTS instances;

        DROP TYPE IF EXISTS routing_status;
        DROP TYPE IF EXISTS delivery_status;
        DROP TYPE IF EXISTS message_sender;
        DROP TYPE IF EXISTS message_direction;
        DROP TYPE IF EXISTS session_status;
    """)
