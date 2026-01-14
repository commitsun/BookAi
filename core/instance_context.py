"""Fetch and store dynamic instance context from Supabase (via n8n webhooks)."""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, Optional

import requests

try:
    from core.db import supabase
except Exception:
    supabase = None

log = logging.getLogger("InstanceContext")
log.setLevel(logging.INFO)

INSTANCE_LOOKUP_WEBHOOK = os.getenv(
    "INSTANCE_LOOKUP_WEBHOOK",
    "https://n8n-n8n.d6aq21.easypanel.host/webhook/3fa1f333-b61b-4436-9104-ecedd635967e",
)
INSTANCE_BY_CODE_WEBHOOK = os.getenv(
    "INSTANCE_BY_CODE_WEBHOOK",
    "https://n8n-n8n.d6aq21.easypanel.host/webhook/3fa1f333-b61b-4436-9104-ecedd635967e",
)
PROPERTY_BY_NAME_WEBHOOK = os.getenv(
    "PROPERTY_BY_NAME_WEBHOOK",
    "https://n8n-n8n.d6aq21.easypanel.host/webhook/c7eb1821-7a5c-4273-b82d-68e852ce8df7",
)
PROPERTY_BY_CODE_WEBHOOK = os.getenv(
    "PROPERTY_BY_CODE_WEBHOOK",
    "https://n8n-n8n.d6aq21.easypanel.host/webhook/c7eb1821-7a5c-4273-b82d-68e852ce8df7",
)
PROPERTY_BY_ID_WEBHOOK = os.getenv(
    "PROPERTY_BY_ID_WEBHOOK",
    "https://n8n-n8n.d6aq21.easypanel.host/webhook/bbd715b6-2a23-4cdb-9107-8e73849bb6ce",
)
DEFAULT_PROPERTY_TABLE = os.getenv("DEFAULT_PROPERTY_TABLE", "alda_hotels")


def _normalize_phone_number(value: Optional[str]) -> str:
    return re.sub(r"\D", "", value or "").strip()


def _extract_payload(data: Any) -> Dict[str, Any]:
    if isinstance(data, dict):
        inner = data.get("data")
        if isinstance(inner, list):
            return inner[0] if inner else {}
        if isinstance(inner, dict):
            return inner
        return data
    if isinstance(data, list):
        return data[0] if data else {}
    return {}


def _post_json(url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not url:
        return {}
    try:
        resp = requests.post(url, json=payload, timeout=12)
    except Exception as exc:
        log.warning("No se pudo conectar al webhook (%s): %s", url, exc)
        return {}

    if resp.status_code >= 400:
        log.warning("Webhook %s fallo (%s): %s", url, resp.status_code, resp.text)
        return {}

    try:
        data = resp.json()
    except Exception as exc:
        log.warning("Respuesta no es JSON en %s: %s", url, exc)
        return {}

    return _extract_payload(data)


def fetch_instance_by_number(whatsapp_number: str) -> Dict[str, Any]:
    payload = {"whatsApp_number": whatsapp_number}
    data = _post_json(INSTANCE_LOOKUP_WEBHOOK, payload)
    if data:
        return data
    if supabase:
        try:
            resp = (
                supabase.table("instances")
                .select("*")
                .eq("whatsapp_number", whatsapp_number)
                .limit(1)
                .execute()
            )
            rows = resp.data or []
            return rows[0] if rows else {}
        except Exception as exc:
            log.warning("Fallback supabase instances (numero) fallo: %s", exc)
    return {}


def fetch_instance_by_code(hotel_code: str) -> Dict[str, Any]:
    payload = {"hotel_code": hotel_code}
    data = _post_json(INSTANCE_BY_CODE_WEBHOOK, payload)
    if data:
        return data
    if supabase:
        try:
            resp = (
                supabase.table("instances")
                .select("*")
                .eq("hotel_code", hotel_code)
                .limit(1)
                .execute()
            )
            rows = resp.data or []
            return rows[0] if rows else {}
        except Exception as exc:
            log.warning("Fallback supabase instances fallo: %s", exc)
    return {}


def fetch_property_by_name(table: str, name: str) -> Dict[str, Any]:
    payload = {"tabla": table, "name": name, "hotel_code": name}
    data = _post_json(PROPERTY_BY_NAME_WEBHOOK, payload)
    if data:
        return data
    if supabase:
        try:
            resp = (
                supabase.table(table)
                .select("*")
                .eq("name", name)
                .limit(1)
                .execute()
            )
            rows = resp.data or []
            return rows[0] if rows else {}
        except Exception as exc:
            log.warning("Fallback supabase property_by_name fallo: %s", exc)
    return {}


def fetch_property_by_code(table: str, hotel_code: str) -> Dict[str, Any]:
    payload = {"tabla": table, "hotel_code": hotel_code}
    data = _post_json(PROPERTY_BY_CODE_WEBHOOK, payload)
    if data:
        return data
    if supabase:
        try:
            resp = (
                supabase.table(table)
                .select("*")
                .eq("hotel_code", hotel_code)
                .limit(1)
                .execute()
            )
            rows = resp.data or []
            return rows[0] if rows else {}
        except Exception as exc:
            log.warning("Fallback supabase property_by_code fallo: %s", exc)
    return {}


def fetch_property_by_id(table: str, property_id: Any) -> Dict[str, Any]:
    payload = {"tabla": table, "property_id": property_id}
    data = _post_json(PROPERTY_BY_ID_WEBHOOK, payload)
    if data:
        return data
    if supabase:
        try:
            resp = (
                supabase.table(table)
                .select("*")
                .eq("property_id", property_id)
                .limit(1)
                .execute()
            )
            rows = resp.data or []
            return rows[0] if rows else {}
        except Exception as exc:
            log.warning("Fallback supabase property_by_id fallo: %s", exc)
    return {}


def _resolve_property_table(instance_payload: Dict[str, Any]) -> Optional[str]:
    for key in (
        "tabla",
        "table",
        "table_name",
        "property_table",
        "hotel_table",
        "supabase_table",
        "instance_table",
        "db_table",
    ):
        value = instance_payload.get(key)
        if value:
            return str(value)
    return None


def _resolve_property_id(payload: Dict[str, Any]) -> Optional[Any]:
    for key in ("property_id", "propertyId", "pms_property_id", "pmsPropertyId"):
        if key in payload and payload.get(key) is not None:
            return payload.get(key)
    return None


def hydrate_dynamic_context(
    *,
    state,
    chat_id: str,
    instance_number: Optional[str] = None,
) -> None:
    """Fetch instance + property metadata and store into MemoryManager flags."""
    memory_manager = getattr(state, "memory_manager", None)
    if not memory_manager or not chat_id:
        return

    normalized_number = _normalize_phone_number(instance_number or "")
    cached_number = memory_manager.get_flag(chat_id, "instance_number")

    instance_payload: Dict[str, Any] = {}
    if normalized_number and (cached_number != normalized_number or not memory_manager.get_flag(chat_id, "instance_url")):
        if cached_number and cached_number != normalized_number:
            memory_manager.clear_flag(chat_id, "property_id")
            memory_manager.clear_flag(chat_id, "kb")
            memory_manager.clear_flag(chat_id, "knowledge_base")
            memory_manager.clear_flag(chat_id, "property_name")
        log.info("🔎 Buscando instancia para numero=%s chat_id=%s", normalized_number, chat_id)
        instance_payload = fetch_instance_by_number(normalized_number)
        if instance_payload:
            log.info("✅ Instancia encontrada: %s", list(instance_payload.keys()))
        else:
            log.warning("⚠️ Sin datos de instancia para numero=%s", normalized_number)
        if instance_payload:
            memory_manager.set_flag(chat_id, "instance_number", normalized_number)

    if not instance_payload:
        instance_payload = {}

    instance_url = instance_payload.get("instance_url") or memory_manager.get_flag(chat_id, "instance_url")
    if instance_url:
        memory_manager.set_flag(chat_id, "instance_url", instance_url)
        log.info("🔗 instance_url=%s (chat_id=%s)", instance_url, chat_id)

    property_table = memory_manager.get_flag(chat_id, "property_table") or _resolve_property_table(instance_payload)
    if not property_table and DEFAULT_PROPERTY_TABLE:
        property_table = DEFAULT_PROPERTY_TABLE
    if property_table:
        memory_manager.set_flag(chat_id, "property_table", property_table)
        log.info("🗂️ property_table=%s (chat_id=%s)", property_table, chat_id)

    for key in ("whatsapp_phone_id", "whatsapp_token", "whatsapp_verify_token"):
        val = instance_payload.get(key)
        if val:
            memory_manager.set_flag(chat_id, key, val)

    property_id = memory_manager.get_flag(chat_id, "property_id") or _resolve_property_id(instance_payload)
    if property_id:
        memory_manager.set_flag(chat_id, "property_id", property_id)
        log.info("🏷️ property_id=%s (chat_id=%s)", property_id, chat_id)

    if not property_id and property_table:
        property_name = memory_manager.get_flag(chat_id, "property_name")
        if not property_name:
            property_name = instance_payload.get("hotel_code") or instance_payload.get("instance_id")
            if property_name:
                memory_manager.set_flag(chat_id, "property_name", property_name)
                log.info("🏨 property_name=%s (chat_id=%s)", property_name, chat_id)
        if property_name:
            prop_by_code = fetch_property_by_code(property_table, str(property_name))
            resolved_id = _resolve_property_id(prop_by_code)
            if not resolved_id:
                prop_by_name = fetch_property_by_name(property_table, str(property_name))
                resolved_id = _resolve_property_id(prop_by_name)
            if resolved_id:
                property_id = resolved_id
                memory_manager.set_flag(chat_id, "property_id", resolved_id)

    if not instance_url:
        hotel_code = memory_manager.get_flag(chat_id, "property_name")
        if hotel_code:
            inst_by_code = fetch_instance_by_code(str(hotel_code))
            instance_url = inst_by_code.get("instance_url")
            if instance_url:
                memory_manager.set_flag(chat_id, "instance_url", instance_url)
                log.info("🔗 instance_url=%s (chat_id=%s)", instance_url, chat_id)

    if property_id and property_table and not memory_manager.get_flag(chat_id, "kb"):
        prop_details = fetch_property_by_id(property_table, property_id)
        kb_name = prop_details.get("kb") or prop_details.get("kb_name") or prop_details.get("knowledge_base")
        if kb_name:
            memory_manager.set_flag(chat_id, "kb", kb_name)
            memory_manager.set_flag(chat_id, "knowledge_base", kb_name)
        prop_code = prop_details.get("hotel_code")
        prop_name = prop_code or prop_details.get("name")
        if prop_name:
            memory_manager.set_flag(chat_id, "property_name", prop_name)
