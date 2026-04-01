"""
POST /api/v1/chatter/send-message  —  Flow 3
"""

import socketio
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_instance, get_sio, get_wa_client
from app.models.instance import Instance
from app.schemas.message import SendMessageRequest, SendMessageResponse
from app.services.chatter_service import process_send_message
from app.services.whatsapp_client import WhatsAppClient

router = APIRouter(prefix="/chatter", tags=["chatter"])

_SUMMARY = "Send a message from a hotel operator to a guest"

_DESCRIPTION = """
Sent by the Roomdoo app when a hotel operator types a reply in the chatter.

**Channel resolution** (in order):

1. If `channel_endpoint_id` is provided → use that channel.
2. Otherwise → default to the channel most recently used in the conversation.

**Messaging window:** the guest must have sent an inbound message within the
last 24 hours on the selected channel, otherwise a `422` is returned.
Outside the window, only templates can be sent (use `/whatsapp/send-template`).

**What this endpoint does:**

1. Resolves the Conversation (404 if not found).
2. Resolves the target channel endpoint.
3. Checks the 24-hour messaging window for that channel.
4. Sends the text message via the channel adapter.
5. Persists the message with full agent traceability.
6. Emits Socket.IO events:
   - `message.created` → `chat:{phone_code}` (conversation view)
   - `conversation.updated` → `property:{id}` (inbox), includes `last_message`
     and the current `unread_count` for that property.
"""

_RESPONSES = {
    401: {"description": "Missing or invalid Bearer token"},
    404: {"description": "Conversation or channel endpoint not found"},
    422: {"description": "Messaging window is closed for the selected channel"},
    502: {"description": "Channel API (e.g. Meta) returned an error"},
}


@router.post(
    "/send-message",
    response_model=SendMessageResponse,
    summary=_SUMMARY,
    description=_DESCRIPTION,
    responses=_RESPONSES,
)
async def send_message(
    body: SendMessageRequest,
    instance: Instance = Depends(get_instance),
    db: AsyncSession = Depends(get_db),
    wa_client: WhatsAppClient = Depends(get_wa_client),
    sio: socketio.AsyncServer = Depends(get_sio),
) -> SendMessageResponse:
    return await process_send_message(body, instance, db, wa_client, sio)
