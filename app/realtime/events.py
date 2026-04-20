"""
Socket.IO event name constants and payload builder functions.

Rooms:
  - property:{id}    — hotel inbox (all conversations for a property).
                       Clients join this room automatically on connect.
  - property:0       — virtual room for unrouted conversations (no active
                       AttentionSession). Connect with property_id=0.
  - chat:{phone_code} — individual conversation (open chat view).
                        Clients join/leave explicitly via join_chat / leave_chat.

Events emitted to property:{id} and property:0:
  - conversation.created  — first message in a previously-unknown conversation.
  - conversation.updated  — any subsequent inbound message.
  Both carry the full ConversationPayload (see below), including the
  per-property ``unread_count`` at the moment of emission.

Events emitted to chat:{phone_code}:
  - message.created          — a message was persisted (any direction).
  - message.delivery_updated — delivery status changed (delivered/read/failed).

ConversationPayload fields:
  id, created_at, updated_at, unread_count,
  contact: {id, phone_code, display_name},
  last_message: {id, direction, sender, content, created_at}
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.contact import Contact
    from app.models.conversation import Conversation
    from app.models.message import Message

EVENT_CONVERSATION_CREATED = "conversation.created"
EVENT_CONVERSATION_UPDATED = "conversation.updated"
EVENT_MESSAGE_CREATED = "message.created"
EVENT_MESSAGE_DELIVERY_UPDATED = "message.delivery_updated"


def build_conversation_payload(
    conversation: Conversation,
    contact: Contact | None = None,
    last_message: Message | None = None,
    unread_count: int = 0,
    needs_attention: bool = False,
) -> dict:
    payload: dict = {
        "id": conversation.id,
        "created_at": (
            conversation.created_at.isoformat()
            if conversation.created_at else None
        ),
        "updated_at": (
            conversation.updated_at.isoformat()
            if conversation.updated_at else None
        ),
        "unread_count": unread_count,
        "needs_attention": needs_attention,
    }
    if contact:
        payload["contact"] = {
            "id": contact.id,
            "phone_code": contact.phone_code,
            "display_name": contact.display_name,
        }
    if last_message:
        payload["last_message"] = {
            "id": last_message.id,
            "direction": last_message.direction.value if last_message.direction else None,
            "sender": last_message.sender.value if last_message.sender else None,
            "content": last_message.content,
            "created_at": last_message.created_at.isoformat() if last_message.created_at else None,
        }
    return payload


def build_message_created_payload(
    message: Message,
    contact: Contact | None = None,
) -> dict:
    ep = message.channel_endpoint if hasattr(message, "channel_endpoint") else None
    payload: dict = {
        "id": message.id,
        "conversation_id": message.conversation_id,
        "channel_endpoint_id": message.channel_endpoint_id,
        "channel": ep.channel if ep else None,
        "direction": (
            message.direction.value if message.direction else None
        ),
        "sender": message.sender.value if message.sender else None,
        "content": message.content,
        "content_language": message.content_language,
        "wa_message_id": message.wa_message_id,
        "wa_message_type": message.wa_message_type,
        "delivery_status": (
            message.delivery_status.value
            if message.delivery_status else None
        ),
        "routing_status": (
            message.routing_status.value
            if message.routing_status else None
        ),
        "template_code": message.template_code,
        "agent_user_id": message.agent_user_id,
        "agent_display_name": message.agent_display_name,
        "created_at": (
            message.created_at.isoformat()
            if message.created_at else None
        ),
    }
    if contact:
        payload["contact"] = {
            "id": contact.id,
            "phone_code": contact.phone_code,
            "display_name": contact.display_name,
        }
    return payload


def build_delivery_updated_payload(message: Message) -> dict:
    return {
        "id": message.id,
        "conversation_id": message.conversation_id,
        "wa_message_id": message.wa_message_id,
        "delivery_status": message.delivery_status.value if message.delivery_status else None,
        "delivery_error": message.delivery_error,
    }
