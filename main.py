from contextlib import asynccontextmanager

import httpx
import socketio
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api.v1 import (
    chatter,
    conversations,
    email,
    email_webhooks,
    folios,
    property_webhooks,
    template_crud,
    templates,
    webhooks,
)
from app.core.config import settings
from app.core.database import engine
from app.realtime.socket_manager import create_socket_server
from app.services.email_channel_client import EmailChannelClient
from app.services.instance_sdk_registry import InstanceSDKRegistry
from app.services.llm_litellm import LiteLLMProvider
from app.services.whatsapp_client import WhatsAppClient

_DESCRIPTION = """
BookAI is the conversational channel backend that sits between
**Roomdoo/Odoo** (hotel PMS), **WhatsApp** (Meta Cloud API), and the
**Roomdoo real-time app** (Socket.IO).

## Flows

| # | Description |
|---|---|
| 1 | Roomdoo sends a template → BookAI delivers via channel + persists |
| 2 | Guest replies on WhatsApp → BookAI persists + notifies app via Socket.IO |
| 3 | Hotel operator replies from app → BookAI delivers to WhatsApp + persists |

## Authentication

All REST endpoints (except `GET /webhook/whatsapp` and `POST /webhook/whatsapp`)
require a **Bearer token** in the `Authorization` header.

Tokens are issued per Roomdoo instance and stored in the `instances` table.

## Real-time (Socket.IO)

Connect to the root path with `{ auth: { token: "<bearer_token>" } }`.
See the **AsyncAPI spec** (`docs/asyncapi.yaml`) for full event documentation.
"""

_TAGS = [
    {
        "name": "templates",
        "description": "Flow 1 — send a template message from Roomdoo to a guest via channel.",
    },
    {
        "name": "chatter",
        "description": "Flow 3 — send a free-text message from a hotel operator to a guest.",
    },
    {
        "name": "conversations",
        "description": "Inbox listing, search, and message history.",
    },
    {
        "name": "folios",
        "description": "Folio cache — pushed by Roomdoo when reservations change.",
    },
    {
        "name": "webhooks",
        "description": "Flow 2 — Meta Cloud API webhook (verification + inbound events).",
    },
    {
        "name": "ops",
        "description": "Operational endpoints (health check, etc.).",
    },
]


sio = create_socket_server(cors_origins=settings.socket_cors_origins)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))

    http_client = httpx.AsyncClient()
    app.state.wa_client = WhatsAppClient(http_client)
    app.state.email_client = EmailChannelClient(http_client)
    app.state.llm_client = LiteLLMProvider()
    app.state.sdk_registry = InstanceSDKRegistry()
    app.state.sio = sio

    yield

    await app.state.sdk_registry.close_all()
    await http_client.aclose()
    await engine.dispose()


app = FastAPI(
    title="BookAI",
    version="0.1.0",
    description=_DESCRIPTION,
    openapi_tags=_TAGS,
    license_info={"name": "MIT"},
    lifespan=lifespan,
)

app.include_router(templates.router, prefix="/api/v1")
app.include_router(template_crud.router, prefix="/api/v1")
app.include_router(chatter.router, prefix="/api/v1")
app.include_router(conversations.router, prefix="/api/v1")
app.include_router(folios.router, prefix="/api/v1")
app.include_router(email.router, prefix="/api/v1")
app.include_router(property_webhooks.api_router, prefix="/api/v1")
app.include_router(webhooks.router)
app.include_router(email_webhooks.router)
app.include_router(property_webhooks.webhook_router)
app.mount("/media", StaticFiles(directory="/app/media"), name="media")
app.mount("/dev-ui", StaticFiles(directory="dev_ui", html=True), name="dev-ui")


@app.get("/health", tags=["ops"], summary="Health check")
async def health() -> dict:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        from fastapi import status as http_status
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "degraded", "detail": "database unavailable"},
        )
    return {"status": "ok"}


# Mount Socket.IO — must be the outermost ASGI app
socket_app = socketio.ASGIApp(sio, other_asgi_app=app)
