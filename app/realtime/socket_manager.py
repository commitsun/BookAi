"""
Socket.IO server setup and connection lifecycle.

### Authentication

Clients must pass the following fields in the Socket.IO handshake auth object:

    { "token": "<bearer_token>", "property_id": <int> }

- ``token``       — same bearer token used for REST API calls.
- ``property_id`` — ID of the hotel property to subscribe to. Pass ``0``
                    to subscribe to unrouted conversations (no active
                    AttentionSession).

### Rooms (server-managed)

- ``property:{id}`` — automatically joined on connect. Receives inbox
                       events for that property.
- ``property:0``    — virtual room for unrouted conversations.

### Rooms (client-managed)

- ``chat:{phone_code}`` — joined/left on demand via ``join_chat`` /
                          ``leave_chat`` client events. Receives per-message
                          events for that guest conversation.

### Client → Server events

- ``join_chat``   ``{ phone_code: string }``  — enter a conversation view.
- ``leave_chat``  ``{ phone_code: string }``  — leave a conversation view.

### Server → Client events (property room)

- ``conversation.created`` — first message in a new conversation.
  Payload: ``{ id, created_at, updated_at, unread_count, contact, last_message }``
- ``conversation.updated`` — new message in an existing conversation.
  Same payload as ``conversation.created``.

### Server → Client events (chat room)

- ``message.created``          — a message was persisted (any direction).
  Payload: full message fields including ``direction``, ``sender``, ``content``,
  ``delivery_status``, ``agent_display_name``, etc.
- ``message.delivery_updated`` — delivery status changed (sent/delivered/read/failed).
  Payload: ``{ id, conversation_id, wa_message_id, delivery_status, delivery_error }``
"""

import logging

import socketio
from sqlalchemy import select

from app.core.database import SessionLocal
from app.models.instance import Property
from app.repositories import instance_repo

log = logging.getLogger("socket_manager")


def create_socket_server(cors_origins: list[str]) -> socketio.AsyncServer:
    sio = socketio.AsyncServer(
        async_mode="asgi",
        cors_allowed_origins=cors_origins,
        logger=False,
        engineio_logger=False,
    )

    @sio.event
    async def connect(sid: str, environ: dict, auth: dict | None) -> bool:
        auth = auth or {}
        token = auth.get("token", "")
        property_id = auth.get("property_id")

        if not token:
            log.warning("Socket.IO connect rejected: no token (sid=%s)", sid)
            return False
        if property_id is None:
            log.warning("Socket.IO connect rejected: no property_id (sid=%s)", sid)
            return False

        async with SessionLocal() as db:
            instance = await instance_repo.find_by_bearer_token(db, token)
            if instance is None:
                log.warning("Socket.IO connect rejected: invalid token (sid=%s)", sid)
                return False

            # property_id=0 is the virtual "unrouted" inbox — no property check needed
            if property_id != 0:
                result = await db.execute(
                    select(Property.id).where(
                        Property.id == property_id,
                        Property.instance_id == instance.id,
                    )
                )
                if result.scalar_one_or_none() is None:
                    log.warning(
                        "Socket.IO connect rejected: property %s not in instance %s (sid=%s)",
                        property_id,
                        instance.id,
                        sid,
                    )
                    return False

        await sio.save_session(sid, {"instance_id": instance.id, "property_id": property_id})
        await sio.enter_room(sid, f"property:{property_id}")
        log.info(
            "Socket.IO connected sid=%s instance=%s property=%s",
            sid,
            instance.id,
            property_id,
        )
        return True

    @sio.event
    async def disconnect(sid: str) -> None:
        log.info("Socket.IO disconnected sid=%s", sid)

    @sio.event
    async def join_chat(sid: str, data: dict) -> None:
        """Client joins a specific chat room to receive messages for a guest conversation."""
        phone_code = (data or {}).get("phone_code", "")
        if not phone_code:
            return
        await sio.enter_room(sid, f"chat:{phone_code}")
        log.debug("sid=%s joined chat:%s", sid, phone_code)

    @sio.event
    async def leave_chat(sid: str, data: dict) -> None:
        phone_code = (data or {}).get("phone_code", "")
        if not phone_code:
            return
        await sio.leave_room(sid, f"chat:{phone_code}")

    return sio
