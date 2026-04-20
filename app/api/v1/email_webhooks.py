"""
POST /webhook/email/inbound  — Mailgun inbound parsed-mail webhook
POST /webhook/email/events   — Mailgun delivery event webhook
"""

from __future__ import annotations

import logging

import socketio
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_sio
from app.core.database import SessionLocal
from app.repositories import instance_repo
from app.services.email_channel_client import validate_mailgun_signature
from app.services.email_inbound_service import (
    process_delivery_event,
    process_inbound_email,
)

log = logging.getLogger("email_webhooks")
router = APIRouter(prefix="/webhook/email", tags=["email-webhooks"])


async def _get_signing_key(recipient: str, db: AsyncSession) -> str | None:
    """Look up the signing_key for the ChannelEndpoint matching recipient."""
    ep = await instance_repo.find_channel_endpoint_by_external_code(
        db, recipient.lower()
    )
    if ep is None:
        return None
    return (ep.config or {}).get("signing_key")


# ---------------------------------------------------------------------------
# Inbound email
# ---------------------------------------------------------------------------


@router.post(
    "/inbound",
    status_code=200,
    summary="Receive inbound emails from Mailgun",
    description=(
        "Mailgun calls this endpoint for every email received on any of "
        "the configured inbound routes.\n\n"
        "Responds **200 immediately** and processes the email in a "
        "background task (Mailgun has a short timeout).\n\n"
        "**No Bearer token required** — authenticated by Mailgun HMAC "
        "signature (`timestamp`, `token`, `signature` fields)."
    ),
)
async def inbound_email(
    background_tasks: BackgroundTasks,
    request: Request,
    db: AsyncSession = Depends(get_db),
    sio: socketio.AsyncServer = Depends(get_sio),
    # Mailgun sends multipart/form-data
    sender: str = Form(default=""),
    recipient: str = Form(default=""),
    subject: str = Form(default=""),
    body_plain: str | None = Form(default=None, alias="body-plain"),
    body_html: str | None = Form(default=None, alias="body-html"),
    message_id: str | None = Form(default=None, alias="Message-Id"),
    in_reply_to: str | None = Form(default=None, alias="In-Reply-To"),
    references: str | None = Form(default=None, alias="References"),
    timestamp: str = Form(default=""),
    token: str = Form(default=""),
    signature: str = Form(default=""),
) -> dict:
    # Verify HMAC signature
    signing_key = await _get_signing_key(recipient, db)
    if signing_key and not validate_mailgun_signature(
        token=token,
        timestamp=timestamp,
        signature=signature,
        signing_key=signing_key,
    ):
        log.warning("Invalid Mailgun HMAC for recipient=%s", recipient)
        raise HTTPException(status_code=403, detail="Invalid signature")

    # Snapshot values for background task (session will be closed after response)
    snapshot = dict(
        sender=sender,
        recipient=recipient,
        subject=subject,
        body_plain=body_plain,
        body_html=body_html,
        message_id=message_id,
        in_reply_to=in_reply_to,
        references=references,
    )

    async def _process() -> None:
        async with SessionLocal() as bg_db:
            try:
                await process_inbound_email(**snapshot, db=bg_db, sio=sio)
            except Exception as exc:  # noqa: BLE001
                log.exception("Error processing inbound email: %s", exc)

    background_tasks.add_task(_process)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Delivery events
# ---------------------------------------------------------------------------


@router.post(
    "/events",
    status_code=200,
    summary="Receive Mailgun delivery events",
    description=(
        "Mailgun calls this endpoint for delivery status updates: "
        "delivered, failed, bounced, opened.\n\n"
        "Authenticated by Mailgun HMAC signature.\n\n"
        "Updates `delivery_status` on the corresponding message and emits "
        "a `message.delivery_updated` Socket.IO event."
    ),
)
async def delivery_events(
    background_tasks: BackgroundTasks,
    request: Request,
    db: AsyncSession = Depends(get_db),
    sio: socketio.AsyncServer = Depends(get_sio),
) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    sig_data = body.get("signature", {})
    event_data = body.get("event-data", {})
    msg_info = event_data.get("message", {})
    headers = msg_info.get("headers", {})
    provider_message_id = headers.get("message-id", "")
    event = event_data.get("event", "")
    recipient = event_data.get("recipient", "")
    error = event_data.get("error")

    # Verify HMAC if we can resolve the endpoint
    signing_key = await _get_signing_key(recipient, db)
    if signing_key and not validate_mailgun_signature(
        token=sig_data.get("token", ""),
        timestamp=sig_data.get("timestamp", ""),
        signature=sig_data.get("signature", ""),
        signing_key=signing_key,
    ):
        log.warning("Invalid Mailgun HMAC for delivery event recipient=%s", recipient)
        raise HTTPException(status_code=403, detail="Invalid signature")

    if not provider_message_id:
        return {"status": "ok"}

    async def _process() -> None:
        async with SessionLocal() as bg_db:
            try:
                await process_delivery_event(
                    event=event,
                    provider_message_id=provider_message_id,
                    error=error,
                    db=bg_db,
                    sio=sio,
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("Error processing delivery event: %s", exc)

    background_tasks.add_task(_process)
    return {"status": "ok"}
