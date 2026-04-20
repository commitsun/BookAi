"""
GET  /webhook/whatsapp  —  Meta verification challenge
POST /webhook/whatsapp  —  Flow 2: inbound messages + delivery status updates
"""

import logging

import socketio
from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_llm_client, get_sdk_registry, get_sio, get_wa_client
from app.core.database import SessionLocal
from app.repositories import instance_repo
from app.schemas.webhook import MetaWebhookPayload
from app.services.instance_sdk_registry import InstanceSDKRegistry
from app.services.llm_client import LLMProvider
from app.services.webhook_service import process_inbound_webhook
from app.services.whatsapp_client import WhatsAppClient

log = logging.getLogger("webhooks")
router = APIRouter(prefix="/webhook", tags=["webhooks"])


@router.get(
    "/whatsapp",
    summary="Meta webhook verification",
    description=(
        "Called once by Meta when the webhook URL is first registered. "
        "Looks up `hub.verify_token` in `channel_endpoints` and echoes back "
        "`hub.challenge` if found. Each channel endpoint has its own token."
    ),
    response_class=PlainTextResponse,
)
async def verify_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> PlainTextResponse:
    params = request.query_params
    token = params.get("hub.verify_token", "")
    if params.get("hub.mode") == "subscribe" and token:
        endpoint = await instance_repo.find_channel_endpoint_by_verify_token(db, token)
        if endpoint is not None:
            log.info("WhatsApp webhook verified for endpoint id=%s", endpoint.id)
            return PlainTextResponse(params.get("hub.challenge", ""), status_code=200)
    log.warning("WhatsApp webhook verification failed (token=%s)", token)
    return PlainTextResponse("Forbidden", status_code=403)


@router.post(
    "/whatsapp",
    status_code=200,
    summary="Receive WhatsApp events from Meta",
    description=(
        "Receives all event types from the Meta Cloud API: inbound messages "
        "(text, interactive, audio, image) and delivery status updates "
        "(sent, delivered, read, failed).\n\n"
        "Responds **200 immediately** to satisfy Meta's 5-second timeout, "
        "then processes the payload in a background task.\n\n"
        "**No Bearer token required** — Meta does not send one. "
        "Messages are reconciled internally via `phone_number_id`."
    ),
)
async def whatsapp_webhook(
    payload: MetaWebhookPayload,
    background_tasks: BackgroundTasks,
    wa_client: WhatsAppClient = Depends(get_wa_client),
    sio: socketio.AsyncServer = Depends(get_sio),
    sdk_registry: InstanceSDKRegistry = Depends(get_sdk_registry),
    llm_client: LLMProvider = Depends(get_llm_client),
) -> dict:
    background_tasks.add_task(
        _process_webhook_bg, payload, wa_client, sio,
        sdk_registry, llm_client,
    )
    return {"status": "ok"}


async def _process_webhook_bg(
    payload: MetaWebhookPayload,
    wa_client: WhatsAppClient,
    sio: socketio.AsyncServer,
    sdk_registry: InstanceSDKRegistry,
    llm_client: LLMProvider,
) -> None:
    try:
        async with SessionLocal() as db:
            await process_inbound_webhook(
                payload, db, wa_client, sio, sdk_registry, llm_client,
            )
    except Exception as exc:
        log.error("Error processing webhook in background: %s", exc, exc_info=True)
