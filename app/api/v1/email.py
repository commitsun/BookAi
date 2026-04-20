"""
POST /api/v1/email/send — send an email from Odoo/Roomdoo or from the app.
"""

import socketio
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    get_db,
    get_email_client,
    get_instance,
    get_sio,
)
from app.models.instance import Instance
from app.schemas.email import EmailSendRequest, EmailSendResponse
from app.services.email_channel_client import EmailChannelClient
from app.services.email_send_service import process_send_email

router = APIRouter(prefix="/email", tags=["email"])


@router.post(
    "/send",
    response_model=EmailSendResponse,
    summary="Send an email to a guest",
    description=(
        "Proxy endpoint: Odoo (or the app) posts the already-resolved email "
        "content here; BookAI calls Mailgun and persists the message.\n\n"
        "**Caller modes**\n\n"
        "- **Odoo/Roomdoo**: provide `source.hotel.external_code` + "
        "`recipient.email`. BookAI resolves the Property and ChannelEndpoint "
        "automatically.\n"
        "- **App**: provide `conversation_id` (and optionally "
        "`channel_endpoint_id`).\n\n"
        "Both modes require `subject` and at least one of `text_body` / "
        "`html_body`.\n\n"
        "The response includes `provider_message_id` (the Mailgun Message-ID) "
        "which is stored for RFC\u00a02822 threading."
    ),
    responses={
        404: {"description": "Property or conversation not found"},
        422: {"description": "Validation error or wrong channel type"},
        502: {"description": "Mailgun returned an error"},
    },
)
async def send_email(
    request: EmailSendRequest,
    instance: Instance = Depends(get_instance),
    db: AsyncSession = Depends(get_db),
    email_client: EmailChannelClient = Depends(get_email_client),
    sio: socketio.AsyncServer = Depends(get_sio),
) -> EmailSendResponse:
    return await process_send_email(request, instance, db, email_client, sio)
