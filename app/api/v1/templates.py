"""
POST /api/v1/whatsapp/send-template  —  Flow 1
"""

import socketio
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_instance, get_sio, get_wa_client
from app.models.instance import Instance
from app.schemas.template import SendTemplateRequest, SendTemplateResponse
from app.services.template_service import process_send_template
from app.services.whatsapp_client import WhatsAppClient

router = APIRouter(prefix="/whatsapp", tags=["templates"])

_SUMMARY = "Send a template message to a guest"

_DESCRIPTION = """
Triggered by Roomdoo when it wants to initiate (or re-initiate) a conversation
with a guest via the configured channel.

**What this endpoint does:**

1. Resolves the property, template and channel endpoint from the request.
2. Normalises the phone number to E.164.
3. Gets or creates the Contact, Conversation, AttentionSession and Folio.
4. Persists the message with `delivery_status=pending`.
5. Sends the template via the Meta Cloud API.
6. Updates `delivery_status` to `sent` (or `failed`).
7. Emits `conversation.created` and `message.created` Socket.IO events.

**Idempotency:** supply `idempotency_key` to make retries safe.
If the key was already processed the cached result is returned immediately
with `idempotent=true`.
"""

_RESPONSES = {
    401: {"description": "Missing or invalid Bearer token"},
    403: {"description": "BookAI is disabled for this instance"},
    404: {"description": "Property or template not found for this instance"},
    422: {"description": "Invalid phone number or property has no channel endpoint"},
    502: {"description": "Channel provider returned an error"},
}


@router.post(
    "/send-template",
    response_model=SendTemplateResponse,
    summary=_SUMMARY,
    description=_DESCRIPTION,
    responses=_RESPONSES,
)
async def send_template(
    body: SendTemplateRequest,
    instance: Instance = Depends(get_instance),
    db: AsyncSession = Depends(get_db),
    wa_client: WhatsAppClient = Depends(get_wa_client),
    sio: socketio.AsyncServer = Depends(get_sio),
) -> SendTemplateResponse:
    return await process_send_template(body, instance, db, wa_client, sio)
