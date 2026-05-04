"""
Unit tests for app/services/template_service.py.

Uses AsyncMock/MagicMock to isolate the service from the database and
external HTTP calls. Each test patches the repo layer at the module level.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.models.message import DeliveryStatus
from app.schemas.template import (
    SendTemplateRequest,
    SourceHotel,
    TemplatePayload,
    TemplateRecipient,
    TemplateSource,
)
from app.services.template_service import process_send_template
from app.services.whatsapp_client import ChannelError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(
    hotel_code: str = "HOTEL-001",
    phone: str = "+34699000001",
    template_code: str = "welcome",
    idempotency_key: str | None = None,
) -> SendTemplateRequest:
    return SendTemplateRequest(
        source=TemplateSource(hotel=SourceHotel(external_code=hotel_code)),
        recipient=TemplateRecipient(phone=phone, country="ES"),
        template=TemplatePayload(
            code=template_code, language="es", components=[]
        ),
        idempotency_key=idempotency_key,
    )


def _make_property(channel_endpoint_id: int | None = 1) -> MagicMock:
    p = MagicMock()
    p.id = 10
    p.channel_endpoint_id = channel_endpoint_id
    return p


def _make_translation() -> MagicMock:
    t = MagicMock()
    t.whatsapp_name = "welcome_es"
    t.language = "es"
    t.meta_status = "approved"
    return t


def _make_channel_endpoint() -> MagicMock:
    e = MagicMock()
    e.id = 1
    e.mock_mode = False
    return e


def _make_message(msg_id: int = 99) -> MagicMock:
    m = MagicMock()
    m.id = msg_id
    m.conversation_id = 7
    m.wa_message_id = None
    return m


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_idempotency_returns_cached_response():
    """If idempotency_key already processed, return cached result."""
    existing = _make_message(42)
    existing.wa_message_id = "wamid.cached"
    existing.conversation_id = 5

    with patch("app.services.template_service.message_repo") as msg_repo:
        msg_repo.find_by_idempotency_key = AsyncMock(return_value=existing)

        result = await process_send_template(
            request=_make_request(idempotency_key="key-001"),
            instance=MagicMock(),
            db=AsyncMock(),
            wa_client=AsyncMock(),
            sio=AsyncMock(),
        )

    assert result.idempotent is True
    assert result.message_id == 42
    assert result.wa_message_id == "wamid.cached"
    assert result.conversation_id == 5


async def test_no_idempotency_key_skips_cache_lookup():
    """Without idempotency_key, the cache lookup is never performed."""
    with (
        patch("app.services.template_service.message_repo") as msg_repo,
        patch("app.services.template_service.instance_repo") as inst_repo,
    ):
        msg_repo.find_by_idempotency_key = AsyncMock()
        inst_repo.find_property_by_roomdoo_code = AsyncMock(return_value=None)

        with pytest.raises(HTTPException) as exc_info:
            await process_send_template(
                request=_make_request(),  # no idempotency_key
                instance=MagicMock(),
                db=AsyncMock(),
                wa_client=AsyncMock(),
                sio=AsyncMock(),
            )

    msg_repo.find_by_idempotency_key.assert_not_called()
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Property resolution
# ---------------------------------------------------------------------------


async def test_unknown_hotel_raises_404():
    """Property not found → HTTPException 404."""
    with (
        patch("app.services.template_service.message_repo") as msg_repo,
        patch("app.services.template_service.instance_repo") as inst_repo,
    ):
        msg_repo.find_by_idempotency_key = AsyncMock(return_value=None)
        inst_repo.find_property_by_roomdoo_code = AsyncMock(return_value=None)

        with pytest.raises(HTTPException) as exc_info:
            await process_send_template(
                request=_make_request(hotel_code="NONEXISTENT"),
                instance=MagicMock(),
                db=AsyncMock(),
                wa_client=AsyncMock(),
                sio=AsyncMock(),
            )

    assert exc_info.value.status_code == 404
    assert "Property not found" in exc_info.value.detail


async def test_property_without_channel_endpoint_raises_422():
    """Property exists but has no channel_endpoint → HTTPException 422."""
    with (
        patch("app.services.template_service.message_repo") as msg_repo,
        patch("app.services.template_service.instance_repo") as inst_repo,
    ):
        msg_repo.find_by_idempotency_key = AsyncMock(return_value=None)
        inst_repo.find_property_by_roomdoo_code = AsyncMock(
            return_value=_make_property(channel_endpoint_id=None)
        )

        with pytest.raises(HTTPException) as exc_info:
            await process_send_template(
                request=_make_request(),
                instance=MagicMock(),
                db=AsyncMock(),
                wa_client=AsyncMock(),
                sio=AsyncMock(),
            )

    assert exc_info.value.status_code == 422
    assert "no linked channel endpoint" in exc_info.value.detail


# ---------------------------------------------------------------------------
# Template resolution
# ---------------------------------------------------------------------------


async def test_unknown_template_raises_404():
    """Template translation not found → HTTPException 404."""
    with (
        patch("app.services.template_service.message_repo") as msg_repo,
        patch("app.services.template_service.instance_repo") as inst_repo,
        patch("app.services.template_service.template_repo") as tmpl_repo,
    ):
        msg_repo.find_by_idempotency_key = AsyncMock(return_value=None)
        inst_repo.find_property_by_roomdoo_code = AsyncMock(
            return_value=_make_property()
        )
        tmpl_repo.find_translation_for_property = AsyncMock(return_value=None)
        tmpl_repo.find_translation_for_property_by_prefix = AsyncMock(return_value=None)

        with pytest.raises(HTTPException) as exc_info:
            await process_send_template(
                request=_make_request(template_code="nonexistent"),
                instance=MagicMock(),
                db=AsyncMock(),
                wa_client=AsyncMock(),
                sio=AsyncMock(),
            )

    assert exc_info.value.status_code == 404
    assert "Template not found" in exc_info.value.detail


# ---------------------------------------------------------------------------
# Phone normalization
# ---------------------------------------------------------------------------


async def test_invalid_phone_raises_422():
    """Unparseable phone number → HTTPException 422."""
    with (
        patch("app.services.template_service.message_repo") as msg_repo,
        patch("app.services.template_service.instance_repo") as inst_repo,
        patch("app.services.template_service.template_repo") as tmpl_repo,
    ):
        msg_repo.find_by_idempotency_key = AsyncMock(return_value=None)
        inst_repo.find_property_by_roomdoo_code = AsyncMock(
            return_value=_make_property()
        )
        inst_repo.find_channel_endpoint_by_id = AsyncMock(
            return_value=_make_channel_endpoint()
        )
        tmpl_repo.find_translation_for_property = AsyncMock(
            return_value=_make_translation()
        )
        tmpl_repo.find_waba_entries = AsyncMock(return_value=[])

        with pytest.raises(HTTPException) as exc_info:
            await process_send_template(
                request=_make_request(phone="not-a-phone"),
                instance=MagicMock(),
                db=AsyncMock(),
                wa_client=AsyncMock(),
                sio=AsyncMock(),
            )

    assert exc_info.value.status_code == 422


# ---------------------------------------------------------------------------
# WhatsApp API errors
# ---------------------------------------------------------------------------


async def test_whatsapp_error_sets_message_failed_and_raises_502():
    """WhatsAppError → message delivery_status=failed, HTTPException 502."""
    msg = _make_message(55)
    wa_client = AsyncMock()
    wa_client.send_template = AsyncMock(
        side_effect=ChannelError(
            status_code=400, body='{"error":"bad request"}'
        )
    )

    with (
        patch("app.services.template_service.message_repo") as msg_repo,
        patch("app.services.template_service.instance_repo") as inst_repo,
        patch("app.services.template_service.template_repo") as tmpl_repo,
        patch("app.services.template_service.contact_repo") as contact_repo,
        patch("app.services.template_service.conversation_repo") as conv_repo,
        patch("app.services.template_service.session_repo") as sess_repo,
        patch("app.services.template_service.folio_repo"),
    ):
        msg_repo.find_by_idempotency_key = AsyncMock(return_value=None)
        msg_repo.create = AsyncMock(return_value=msg)
        msg_repo.update_delivery = AsyncMock()
        inst_repo.find_property_by_roomdoo_code = AsyncMock(
            return_value=_make_property()
        )
        inst_repo.find_channel_endpoint_by_id = AsyncMock(
            return_value=_make_channel_endpoint()
        )
        tmpl_repo.find_translation_for_property = AsyncMock(
            return_value=_make_translation()
        )
        tmpl_repo.find_waba_entries = AsyncMock(return_value=[])
        contact = MagicMock()
        contact.id = 1
        contact_repo.get_or_create = AsyncMock(return_value=(contact, True))
        conv = MagicMock()
        conv.id = 7
        conv.contact = contact
        conv_repo.get_or_create = AsyncMock(return_value=(conv, True))
        conv_repo.get_or_create_channel_state = AsyncMock()
        conv_repo.get_unread_counts = AsyncMock(return_value={})
        session = MagicMock()
        session.id = 3
        sess_repo.get_or_create_active = AsyncMock(
            return_value=(session, True)
        )

        db = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await process_send_template(
                request=_make_request(),
                instance=MagicMock(),
                db=db,
                wa_client=wa_client,
                sio=AsyncMock(),
            )

    assert exc_info.value.status_code == 502
    msg_repo.update_delivery.assert_called_once_with(
        db, msg, DeliveryStatus.failed, error='{"error":"bad request"}'
    )


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


async def test_success_returns_sent_response():
    """Happy path: message created with status=sent, idempotent=False."""
    msg = _make_message(77)
    wa_client = AsyncMock()
    wa_client.send_template = AsyncMock(return_value="wamid.new-message-id")

    with (
        patch("app.services.template_service.message_repo") as msg_repo,
        patch("app.services.template_service.instance_repo") as inst_repo,
        patch("app.services.template_service.template_repo") as tmpl_repo,
        patch("app.services.template_service.contact_repo") as contact_repo,
        patch("app.services.template_service.conversation_repo") as conv_repo,
        patch("app.services.template_service.session_repo") as sess_repo,
        patch("app.services.template_service.folio_repo"),
    ):
        msg_repo.find_by_idempotency_key = AsyncMock(return_value=None)
        msg_repo.create = AsyncMock(return_value=msg)
        msg_repo.update_delivery = AsyncMock()
        inst_repo.find_property_by_roomdoo_code = AsyncMock(
            return_value=_make_property()
        )
        inst_repo.find_channel_endpoint_by_id = AsyncMock(
            return_value=_make_channel_endpoint()
        )
        tmpl_repo.find_translation_for_property = AsyncMock(
            return_value=_make_translation()
        )
        tmpl_repo.find_waba_entries = AsyncMock(return_value=[])
        contact = MagicMock()
        contact.id = 1
        contact_repo.get_or_create = AsyncMock(return_value=(contact, True))
        conv = MagicMock()
        conv.id = 7
        conv.contact = contact
        conv_repo.get_or_create = AsyncMock(return_value=(conv, False))
        conv_repo.get_or_create_channel_state = AsyncMock()
        conv_repo.get_unread_counts = AsyncMock(return_value={7: 0})
        session = MagicMock()
        session.id = 3
        sess_repo.get_or_create_active = AsyncMock(
            return_value=(session, True)
        )

        result = await process_send_template(
            request=_make_request(idempotency_key="key-new"),
            instance=MagicMock(),
            db=AsyncMock(),
            wa_client=wa_client,
            sio=AsyncMock(),
        )

    assert result.idempotent is False
    assert result.message_id == 77
    assert result.wa_message_id == "wamid.new-message-id"
    assert result.conversation_id == 7
    # update_delivery called with sent status and the wa_message_id
    call_args = msg_repo.update_delivery.call_args
    assert call_args.args[2] == DeliveryStatus.sent
    assert call_args.kwargs.get("wa_message_id") == "wamid.new-message-id"
