"""
Shared pytest fixtures for the BookAI test suite.

Strategy
--------
- Every test runs inside a connection-level transaction that is rolled back
  when the test ends (SAVEPOINT pattern via join_transaction_mode="create_savepoint").
- Socket.IO is replaced by AsyncMock.
- Channel provider API calls are suppressed by using channel_endpoints with mock_mode=True.
- The test bearer token is only visible within the SAVEPOINT, so it never conflicts
  with prod or demo data.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api.dependencies import get_db, get_sio, get_wa_client
from app.core.config import settings
from app.models.channel import ChannelEndpoint
from app.models.contact import Contact
from app.models.conversation import Conversation, ConversationChannelState
from app.models.instance import Instance, Property
from app.models.session import AttentionSession, SessionStatus
from app.services.whatsapp_client import WhatsAppClient
from main import app

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

TEST_TOKEN = "bookai-unit-test-token-xxxx"
TEST_PHONE_NUMBER_ID = "TEST_WA_PHONE_ID_UNIT"
TEST_VERIFY_TOKEN = "unit-test-verify-token"
GUEST_PHONE = "34600099001"  # dedicated test-only number, not in demo data


# ---------------------------------------------------------------------------
# Core DB fixture — per-test SAVEPOINT rollback
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    """
    Yields an AsyncSession joined to a connection-level transaction.
    Each session.commit() creates/releases a SAVEPOINT instead of a real commit,
    so all data is invisible outside this test and rolled back automatically.

    The engine is created inside the fixture so it is bound to the same event
    loop as the test function, avoiding asyncpg cross-loop errors.
    """
    engine = create_async_engine(settings.database_url, echo=False)
    try:
        async with engine.connect() as conn:
            await conn.begin()
            session = AsyncSession(
                conn,
                join_transaction_mode="create_savepoint",
                expire_on_commit=False,
            )
            try:
                yield session
            finally:
                await session.close()
                await conn.rollback()
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Seed fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def seed_instance(db: AsyncSession) -> Instance:
    """Instance with a known bearer token, used to authenticate REST requests."""
    inst = Instance(
        instance_url=f"https://test-{uuid.uuid4().hex[:8]}.roomdoo.test",
        bearer_token=TEST_TOKEN,
        bookai_enabled=True,
        active=True,
    )
    db.add(inst)
    await db.flush()
    return inst


@pytest_asyncio.fixture
async def seed_endpoint(db: AsyncSession) -> ChannelEndpoint:
    """ChannelEndpoint with mock_mode=True so no real Meta calls are made."""
    ep = ChannelEndpoint(
        channel="whatsapp",
        external_code=TEST_PHONE_NUMBER_ID,
        access_token="fake-access-token",
        account_id="fake-account-id",
        verify_token=TEST_VERIFY_TOKEN,
        mock_mode=True,
        display_number="+34 600 000 099",
    )
    db.add(ep)
    await db.flush()
    return ep


@pytest_asyncio.fixture
async def seed_property(
    db: AsyncSession,
    seed_instance: Instance,
    seed_endpoint: ChannelEndpoint,
) -> Property:
    """Single property linked to seed_endpoint (1-property routing case)."""
    prop = Property(
        instance_id=seed_instance.id,
        name="Test Hotel",
        roomdoo_external_code="TEST-HOTEL-001",
        channel_endpoint_id=seed_endpoint.id,
    )
    db.add(prop)
    await db.flush()
    return prop


@pytest_asyncio.fixture
async def seed_contact(db: AsyncSession) -> Contact:
    contact = Contact(phone_code=GUEST_PHONE, display_name="Test Guest")
    db.add(contact)
    await db.flush()
    return contact


@pytest_asyncio.fixture
async def seed_conversation(db: AsyncSession, seed_contact: Contact) -> Conversation:
    conv = Conversation(contact_id=seed_contact.id)
    db.add(conv)
    await db.flush()
    return conv


@pytest_asyncio.fixture
async def seed_channel_state_open(
    db: AsyncSession,
    seed_conversation: Conversation,
    seed_endpoint: ChannelEndpoint,
) -> ConversationChannelState:
    """Channel state with last_inbound_at = 1 hour ago (window open)."""
    state = ConversationChannelState(
        conversation_id=seed_conversation.id,
        channel_endpoint_id=seed_endpoint.id,
        last_inbound_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db.add(state)
    await db.flush()
    return state


@pytest_asyncio.fixture
async def seed_attention_session(
    db: AsyncSession,
    seed_conversation: Conversation,
    seed_property: Property,
) -> AttentionSession:
    """Active AttentionSession linking conversation to the test property."""
    session = AttentionSession(
        conversation_id=seed_conversation.id,
        property_id=seed_property.id,
        status=SessionStatus.active,
        opened_at=datetime.now(timezone.utc),
    )
    db.add(session)
    await db.flush()
    return session


# ---------------------------------------------------------------------------
# Convenience fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_headers(seed_instance: Instance) -> dict[str, str]:
    """Authorization headers using the test token.
    Depending on this fixture also ensures seed_instance exists in DB."""
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


# ---------------------------------------------------------------------------
# HTTP test client
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(db: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """
    httpx AsyncClient pointed at the FastAPI app with dependency overrides:
    - get_db    → test session (SAVEPOINT-backed)
    - get_sio   → AsyncMock
    - get_wa_client → real WhatsAppClient (mock_mode=True per endpoint)
    """
    mock_sio = AsyncMock()
    mock_http = httpx.AsyncClient()
    wa = WhatsAppClient(mock_http)

    async def _override_db() -> AsyncGenerator[AsyncSession, None]:
        yield db

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_sio] = lambda: mock_sio
    app.dependency_overrides[get_wa_client] = lambda: wa

    transport = httpx.ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    app.dependency_overrides.clear()
    await mock_http.aclose()
