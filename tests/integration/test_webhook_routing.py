"""
Integration tests for Flow 2 inbound routing logic.

Calls process_inbound_webhook() directly to avoid the background-task layer
of the HTTP webhook endpoint. All data is rolled back after each test.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.channel import ChannelEndpoint
from app.models.contact import Contact
from app.models.folio import Folio, FolioStatus, SessionFolio
from app.models.instance import Property
from app.models.message import DeliveryStatus, Message, MessageDirection, MessageSender, RoutingStatus
from app.models.session import AttentionSession, SessionStatus
from app.schemas.webhook import (
    MetaWebhookPayload,
    WebhookChange,
    WebhookContact,
    WebhookEntry,
    WebhookMessage,
    WebhookMetadata,
    WebhookStatus,
    WebhookTextContent,
    WebhookValue,
)
from app.services.webhook_service import process_inbound_webhook
from app.services.whatsapp_client import WhatsAppClient
from tests.conftest import GUEST_PHONE, TEST_PHONE_NUMBER_ID


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_payload(
    phone_number_id: str,
    from_phone: str,
    msg_id: str,
    body: str = "hello",
) -> MetaWebhookPayload:
    return MetaWebhookPayload(
        object="whatsapp_business_account",
        entry=[
            WebhookEntry(
                id="0",
                changes=[
                    WebhookChange(
                        field="messages",
                        value=WebhookValue(
                            messaging_product="whatsapp",
                            metadata=WebhookMetadata(
                                display_phone_number="34600000099",
                                phone_number_id=phone_number_id,
                            ),
                            contacts=[
                                WebhookContact(
                                    wa_id=from_phone,
                                    profile={"name": "Test Guest"},
                                )
                            ],
                            messages=[
                                WebhookMessage(
                                    id=msg_id,
                                    from_=from_phone,
                                    timestamp="1700000001",
                                    type="text",
                                    text=WebhookTextContent(body=body),
                                )
                            ],
                        ),
                    )
                ],
            )
        ],
    )


def _make_status_payload(
    wa_message_id: str, status: str, error: dict | None = None
) -> MetaWebhookPayload:
    return MetaWebhookPayload(
        object="whatsapp_business_account",
        entry=[
            WebhookEntry(
                id="0",
                changes=[
                    WebhookChange(
                        field="messages",
                        value=WebhookValue(
                            messaging_product="whatsapp",
                            statuses=[
                                WebhookStatus(
                                    id=wa_message_id,
                                    status=status,
                                    errors=[error] if error else None,
                                )
                            ],
                        ),
                    )
                ],
            )
        ],
    )


async def _run_webhook(
    payload: MetaWebhookPayload,
    db: AsyncSession,
    endpoint: ChannelEndpoint,
) -> None:
    mock_http = httpx.AsyncClient()
    wa_client = WhatsAppClient(mock_http)
    mock_sio = AsyncMock()
    try:
        await process_inbound_webhook(payload, db, wa_client, mock_sio)
    finally:
        await mock_http.aclose()


# ---------------------------------------------------------------------------
# Tests — routing
# ---------------------------------------------------------------------------


async def test_inbound_single_property_creates_session(
    db: AsyncSession,
    seed_endpoint: ChannelEndpoint,
    seed_property: Property,
) -> None:
    """0 sessions + 1 property → routing_status=routed, session auto-created."""
    payload = _make_payload(TEST_PHONE_NUMBER_ID, GUEST_PHONE, "wamid.route.single.001")

    await _run_webhook(payload, db, seed_endpoint)

    result = await db.execute(select(Message).where(Message.wa_message_id == "wamid.route.single.001"))
    msg = result.scalar_one()

    assert msg.routing_status == RoutingStatus.routed
    assert msg.attention_session_id is not None

    session_result = await db.execute(
        select(AttentionSession).where(AttentionSession.id == msg.attention_session_id)
    )
    session = session_result.scalar_one()
    assert session.status == SessionStatus.active
    assert session.property_id == seed_property.id


async def test_inbound_multi_property_unassigned(
    db: AsyncSession,
    seed_endpoint: ChannelEndpoint,
    seed_instance,
) -> None:
    """0 sessions + N>1 properties → routing_status=unassigned, unrouted session."""
    # Add two properties linked to the same endpoint
    prop1 = Property(
        instance_id=seed_instance.id,
        name="First Hotel",
        roomdoo_external_code="TEST-HOTEL-001B",
        channel_endpoint_id=seed_endpoint.id,
    )
    prop2 = Property(
        instance_id=seed_instance.id,
        name="Second Hotel",
        roomdoo_external_code="TEST-HOTEL-002",
        channel_endpoint_id=seed_endpoint.id,
    )
    db.add(prop1)
    db.add(prop2)
    await db.flush()

    payload = _make_payload(TEST_PHONE_NUMBER_ID, GUEST_PHONE, "wamid.route.multi.001")
    await _run_webhook(payload, db, seed_endpoint)

    result = await db.execute(
        select(Message).where(Message.wa_message_id == "wamid.route.multi.001")
    )
    msg = result.scalar_one()

    assert msg.routing_status == RoutingStatus.unassigned
    assert msg.attention_session_id is not None  # unrouted session created

    from app.models.session import AttentionSession
    session_result = await db.execute(
        select(AttentionSession).where(AttentionSession.id == msg.attention_session_id)
    )
    unrouted = session_result.scalar_one()
    assert unrouted.property_id is None  # unrouted → no specific property


async def test_inbound_multi_session_picks_most_recent(
    db: AsyncSession,
    seed_endpoint: ChannelEndpoint,
    seed_property: Property,
    seed_conversation,
    seed_instance,
) -> None:
    """2 active sessions → picks the one with the most recent message (no ambiguous)."""
    from datetime import datetime, timedelta, timezone

    prop2 = Property(
        instance_id=seed_instance.id,
        name="Hotel Two",
        roomdoo_external_code="TEST-HOTEL-PICK",
        channel_endpoint_id=seed_endpoint.id,
    )
    db.add(prop2)
    await db.flush()

    now = datetime.now(timezone.utc)

    # Both sessions have a non-terminal folio → always active regardless of recency
    s1 = AttentionSession(
        conversation_id=seed_conversation.id,
        property_id=seed_property.id,
        status=SessionStatus.active,
        opened_at=now,
    )
    s2 = AttentionSession(
        conversation_id=seed_conversation.id,
        property_id=prop2.id,
        status=SessionStatus.active,
        opened_at=now,
    )
    db.add(s1)
    db.add(s2)
    await db.flush()

    folio1 = Folio(odoo_external_code="FOLIO-PICK-001", status=FolioStatus.onboard)
    folio2 = Folio(odoo_external_code="FOLIO-PICK-002", status=FolioStatus.onboard)
    db.add(folio1)
    db.add(folio2)
    await db.flush()

    db.add(SessionFolio(session_id=s1.id, folio_id=folio1.id))
    db.add(SessionFolio(session_id=s2.id, folio_id=folio2.id))
    await db.flush()

    # s1 has an older message, s2 has a more recent one
    db.add(Message(
        conversation_id=seed_conversation.id,
        channel_endpoint_id=seed_endpoint.id,
        attention_session_id=s1.id,
        direction=MessageDirection.outbound,
        sender=MessageSender.agent,
        content="older",
        wa_message_type="text",
        delivery_status=DeliveryStatus.delivered,
        created_at=now - timedelta(hours=5),
    ))
    db.add(Message(
        conversation_id=seed_conversation.id,
        channel_endpoint_id=seed_endpoint.id,
        attention_session_id=s2.id,
        direction=MessageDirection.outbound,
        sender=MessageSender.agent,
        content="newer",
        wa_message_type="text",
        delivery_status=DeliveryStatus.delivered,
        created_at=now - timedelta(hours=1),
    ))
    await db.flush()

    payload = _make_payload(TEST_PHONE_NUMBER_ID, GUEST_PHONE, "wamid.route.pick.001")
    await _run_webhook(payload, db, seed_endpoint)

    result = await db.execute(
        select(Message).where(Message.wa_message_id == "wamid.route.pick.001")
    )
    msg = result.scalar_one()

    assert msg.routing_status == RoutingStatus.routed
    assert msg.attention_session_id == s2.id  # s2 had the most recent message


async def test_inbound_deduplication(
    db: AsyncSession,
    seed_endpoint: ChannelEndpoint,
    seed_property: Property,
) -> None:
    """Same wa_message_id processed twice → only 1 message persisted."""
    payload = _make_payload(TEST_PHONE_NUMBER_ID, GUEST_PHONE, "wamid.dedup.001")

    await _run_webhook(payload, db, seed_endpoint)
    await _run_webhook(payload, db, seed_endpoint)  # duplicate

    result = await db.execute(
        select(Message).where(Message.wa_message_id == "wamid.dedup.001")
    )
    messages = result.scalars().all()
    assert len(messages) == 1


async def test_inbound_unknown_phone_number_id(
    db: AsyncSession,
    seed_endpoint: ChannelEndpoint,
    seed_property: Property,
) -> None:
    """Unknown phone_number_id → message ignored, nothing persisted."""
    payload = _make_payload("UNKNOWN_PHONE_ID_999", GUEST_PHONE, "wamid.unknown.001")
    await _run_webhook(payload, db, seed_endpoint)

    result = await db.execute(
        select(Message).where(Message.wa_message_id == "wamid.unknown.001")
    )
    assert result.scalar_one_or_none() is None


async def test_inbound_new_contact_created(
    db: AsyncSession,
    seed_endpoint: ChannelEndpoint,
    seed_property: Property,
) -> None:
    """Unknown phone → Contact and Conversation created automatically."""
    new_phone = "34699777002"
    payload = _make_payload(TEST_PHONE_NUMBER_ID, new_phone, "wamid.newcontact.001")
    await _run_webhook(payload, db, seed_endpoint)

    result = await db.execute(select(Contact).where(Contact.phone_code == new_phone))
    contact = result.scalar_one()
    assert contact.display_name == "Test Guest"


async def test_inbound_existing_contact_reused(
    db: AsyncSession,
    seed_endpoint: ChannelEndpoint,
    seed_property: Property,
    seed_contact,
) -> None:
    """Known phone → existing Contact reused, no duplicate created."""
    payload = _make_payload(TEST_PHONE_NUMBER_ID, GUEST_PHONE, "wamid.existing.001")
    await _run_webhook(payload, db, seed_endpoint)

    result = await db.execute(select(Contact).where(Contact.phone_code == GUEST_PHONE))
    contacts = result.scalars().all()
    assert len(contacts) == 1


# ---------------------------------------------------------------------------
# Tests — session activity (is_session_active integration)
# ---------------------------------------------------------------------------


async def test_inbound_closed_session_done_no_charges_old(
    db: AsyncSession,
    seed_endpoint: ChannelEndpoint,
    seed_property: Property,
    seed_conversation,
) -> None:
    """Session with done folio + no charges + last message > 7d → inactive → auto-create new."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)

    # Pre-existing session that should be considered inactive
    old_session = AttentionSession(
        conversation_id=seed_conversation.id,
        property_id=seed_property.id,
        status=SessionStatus.active,
        opened_at=now - timedelta(days=30),
    )
    db.add(old_session)
    await db.flush()

    folio = Folio(odoo_external_code="FOLIO-CLOSED-001", status=FolioStatus.done)
    db.add(folio)
    await db.flush()
    db.add(SessionFolio(session_id=old_session.id, folio_id=folio.id))

    # Message older than 7 days → recency condition fails
    db.add(Message(
        conversation_id=seed_conversation.id,
        channel_endpoint_id=seed_endpoint.id,
        attention_session_id=old_session.id,
        direction=MessageDirection.inbound,
        sender=MessageSender.guest,
        content="old message",
        wa_message_type="text",
        delivery_status=DeliveryStatus.delivered,
        created_at=now - timedelta(days=8),
    ))
    await db.flush()

    payload = _make_payload(TEST_PHONE_NUMBER_ID, GUEST_PHONE, "wamid.closed.done.001")
    await _run_webhook(payload, db, seed_endpoint)

    result = await db.execute(
        select(Message).where(Message.wa_message_id == "wamid.closed.done.001")
    )
    msg = result.scalar_one()

    # Old session inactive → 0 active → 1 property → auto-create new session → routed
    assert msg.routing_status == RoutingStatus.routed
    assert msg.attention_session_id != old_session.id  # a new session was created


async def test_inbound_active_session_done_with_charges_old(
    db: AsyncSession,
    seed_endpoint: ChannelEndpoint,
    seed_property: Property,
    seed_conversation,
) -> None:
    """Session with done folio but pending charges + old message → still active → routed."""
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal

    now = datetime.now(timezone.utc)

    session = AttentionSession(
        conversation_id=seed_conversation.id,
        property_id=seed_property.id,
        status=SessionStatus.active,
        opened_at=now - timedelta(days=15),
    )
    db.add(session)
    await db.flush()

    folio = Folio(
        odoo_external_code="FOLIO-CHARGES-001",
        status=FolioStatus.done,
        pending_payment_amount=Decimal("250.00"),
        pending_payment_currency="EUR",
    )
    db.add(folio)
    await db.flush()
    db.add(SessionFolio(session_id=session.id, folio_id=folio.id))

    db.add(Message(
        conversation_id=seed_conversation.id,
        channel_endpoint_id=seed_endpoint.id,
        attention_session_id=session.id,
        direction=MessageDirection.inbound,
        sender=MessageSender.guest,
        content="dispute",
        wa_message_type="text",
        delivery_status=DeliveryStatus.delivered,
        created_at=now - timedelta(days=8),
    ))
    await db.flush()

    payload = _make_payload(TEST_PHONE_NUMBER_ID, GUEST_PHONE, "wamid.charges.001")
    await _run_webhook(payload, db, seed_endpoint)

    result = await db.execute(
        select(Message).where(Message.wa_message_id == "wamid.charges.001")
    )
    msg = result.scalar_one()

    # done folio but charges > 0 → session still active → routed to same session
    assert msg.routing_status == RoutingStatus.routed
    assert msg.attention_session_id == session.id


# ---------------------------------------------------------------------------
# Tests — delivery status updates
# ---------------------------------------------------------------------------


async def test_delivery_status_sent(
    db: AsyncSession,
    seed_endpoint: ChannelEndpoint,
    seed_property: Property,
    seed_conversation,
) -> None:
    """Status webhook 'sent' → delivery_status updated on existing message."""
    from datetime import datetime, timezone

    msg = Message(
        conversation_id=seed_conversation.id,
        channel_endpoint_id=seed_endpoint.id,
        direction="outbound",
        sender="agent",
        content="test",
        wa_message_id="wamid.delivery.sent.001",
        wa_message_type="text",
        delivery_status=DeliveryStatus.pending,
    )
    db.add(msg)
    await db.flush()

    payload = _make_status_payload("wamid.delivery.sent.001", "sent")
    await _run_webhook(payload, db, seed_endpoint)

    await db.refresh(msg)
    assert msg.delivery_status == DeliveryStatus.sent


async def test_delivery_status_failed_with_error(
    db: AsyncSession,
    seed_endpoint: ChannelEndpoint,
    seed_property: Property,
    seed_conversation,
) -> None:
    """Status webhook 'failed' with error → delivery_error persisted."""
    from datetime import datetime, timezone

    msg = Message(
        conversation_id=seed_conversation.id,
        channel_endpoint_id=seed_endpoint.id,
        direction="outbound",
        sender="agent",
        content="test",
        wa_message_id="wamid.delivery.fail.001",
        wa_message_type="text",
        delivery_status=DeliveryStatus.pending,
    )
    db.add(msg)
    await db.flush()

    error = {"code": 131047, "title": "Message failed to send"}
    payload = _make_status_payload("wamid.delivery.fail.001", "failed", error)
    await _run_webhook(payload, db, seed_endpoint)

    await db.refresh(msg)
    assert msg.delivery_status == DeliveryStatus.failed
    assert msg.delivery_error is not None
