from __future__ import annotations

import logging
import os
from threading import Thread
from typing import Any

log = logging.getLogger("MessageBackup")

DEFAULT_BACKUP_TABLE = os.getenv("MESSAGE_BACKUP_TABLE", "message_backup")


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_identifier(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).replace("+", "").strip()
    return text or None


def _normalize_payload(payload: Any) -> dict | list | None:
    if payload is None:
        return None
    if isinstance(payload, (dict, list)):
        return payload
    return {"value": payload}


def safe_backup_message(
    *,
    conversation_id: Any = None,
    original_chat_id: Any = None,
    channel: Any = None,
    property_id: Any = None,
    instance_id: Any = None,
    role: Any = None,
    direction: Any = None,
    content: Any = None,
    external_message_id: Any = None,
    backup_payload: Any = None,
    backup_source: Any = None,
    supabase_client: Any = None,
    table: str | None = None,
) -> None:
    try:
        client = supabase_client
        if client is None:
            from core.db import supabase as default_supabase

            client = default_supabase

        direction_value = _clean_text(direction) or "outbound"
        content_value = "" if content is None else str(content)
        payload: dict[str, Any] = {
            "direction": direction_value,
            "content": content_value,
            "backup_source": _clean_text(backup_source) or "unknown",
        }

        conversation_value = _clean_identifier(conversation_id)
        if conversation_value:
            payload["conversation_id"] = conversation_value

        original_value = _clean_identifier(original_chat_id)
        if original_value:
            payload["original_chat_id"] = original_value

        channel_value = _clean_text(channel)
        if channel_value:
            payload["channel"] = channel_value

        property_value = _clean_text(property_id)
        if property_value:
            payload["property_id"] = property_value

        instance_value = _clean_text(instance_id)
        if instance_value:
            payload["instance_id"] = instance_value

        role_value = _clean_text(role)
        if role_value:
            payload["role"] = role_value

        external_value = _clean_text(external_message_id)
        if external_value:
            payload["external_message_id"] = external_value

        normalized_backup_payload = _normalize_payload(backup_payload)
        if normalized_backup_payload is not None:
            payload["backup_payload"] = normalized_backup_payload

        client.table(table or DEFAULT_BACKUP_TABLE).insert(payload).execute()
    except Exception as exc:
        log.warning("⚠️ No se pudo guardar backup de mensaje: %s", exc, exc_info=True)


def schedule_message_backup(**kwargs: Any) -> None:
    try:
        Thread(
            target=safe_backup_message,
            kwargs=kwargs,
            daemon=True,
            name="message-backup",
        ).start()
    except Exception as exc:
        log.warning("⚠️ No se pudo planificar backup de mensaje: %s", exc, exc_info=True)
