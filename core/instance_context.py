"""Fetch and store dynamic instance context from Supabase (via n8n webhooks)."""

from __future__ import annotations

import logging
import os
import re
import json
import asyncio
from typing import Any, Dict, Optional

import requests

try:
    from core.db import supabase
    from core.mcp_client import get_tools
except Exception:
    supabase = None
    get_tools = None

log = logging.getLogger("InstanceContext")
log.setLevel(logging.INFO)

# Webhooks deshabilitados: usamos MCP/Supabase directamente.
INSTANCE_LOOKUP_WEBHOOK = ""
INSTANCE_BY_CODE_WEBHOOK = ""
PROPERTY_BY_NAME_WEBHOOK = ""
PROPERTY_BY_CODE_WEBHOOK = ""
PROPERTY_BY_ID_WEBHOOK = ""
DEFAULT_PROPERTY_TABLE = os.getenv("DEFAULT_PROPERTY_TABLE", "properties")


def _normalize_phone_number(value: Optional[str]) -> str:
    return re.sub(r"\D", "", value or "").strip()


def _normalize_kb_name(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    cleaned = str(value).strip()
    cleaned = cleaned.replace("ponferrrada", "ponferrada")
    return cleaned or None


def _extract_payload(data: Any) -> Dict[str, Any]:
    if isinstance(data, dict):
        inner = data.get("data")
        if inner is None:
            inner = data.get("response")
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


def _run_async(coro):
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    if loop.is_running():
        import nest_asyncio

        nest_asyncio.apply()
    return loop.run_until_complete(coro)


def _mcp_tool_matches(name: str) -> bool:
    n = (name or "").strip().lower()
    if n in {"property id", "property_id", "propertyid"}:
        return True
    if "property" in n and "id" in n:
        return True
    return False


def _fetch_properties_by_code_mcp(table: str, hotel_code: str) -> list[Dict[str, Any]]:
    if not get_tools:
        return []

    async def _load_tools():
        for server in ("DispoPreciosAgent", "OnboardingAgent", "InfoAgent"):
            try:
                tools = await get_tools(server_name=server)
            except Exception:
                continue
            for tool in tools or []:
                if _mcp_tool_matches(getattr(tool, "name", "")):
                    return tool
        return None

    try:
        tool = _run_async(_load_tools())
    except Exception:
        tool = None

    if not tool:
        return []

    payload = {"tabla": table, "hotel_code": hotel_code}
    try:
        raw = _run_async(tool.ainvoke(payload))
    except Exception as exc:
        log.warning("MCP property tool fallo: %s", exc)
        return []

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}

    data = _extract_payload(raw)
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict) and isinstance(raw.get("response"), list):
        return raw.get("response") or []
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and data:
        return [data]
    if isinstance(raw, dict) and raw:
        return [raw]
    return []


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
    log.info("🔎 Instance fallback by code via Supabase: hotel_code=%s", hotel_code)
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
            if rows:
                log.info("✅ Instance found in Supabase: hotel_code=%s", hotel_code)
            return rows[0] if rows else {}
        except Exception as exc:
            log.warning("Fallback supabase instances fallo: %s", exc)
    return {}


def fetch_property_by_name(table: str, name: str) -> Dict[str, Any]:
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
            if rows:
                return rows[0]
        except Exception as exc:
            log.warning("Fallback supabase property_by_name fallo: %s", exc)
    payload = {"tabla": table, "name": name, "hotel_code": name}
    data = _post_json(PROPERTY_BY_NAME_WEBHOOK, payload)
    if data:
        return data
    return {}


def fetch_property_by_code(table: str, hotel_code: str) -> Dict[str, Any]:
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
            if rows:
                log.info("✅ Property found in Supabase: table=%s hotel_code=%s", table, hotel_code)
                return rows[0]
        except Exception as exc:
            log.warning("Fallback supabase property_by_code fallo: %s", exc)
    payload = {"tabla": table, "hotel_code": hotel_code}
    data = _post_json(PROPERTY_BY_CODE_WEBHOOK, payload)
    if data:
        return data
    return {}


def fetch_properties_by_code(table: str, hotel_code: str) -> list[Dict[str, Any]]:
    """
    Devuelve multiples properties por hotel_code si existen.
    Usa MCP como fuente principal.
    """
    mcp_rows = _fetch_properties_by_code_mcp(table, hotel_code)
    if mcp_rows:
        return mcp_rows
    if supabase:
        try:
            resp = (
                supabase.table(table)
                .select("*")
                .eq("hotel_code", hotel_code)
                .execute()
            )
            rows = resp.data or []
            if rows:
                return rows
        except Exception as exc:
            log.warning("Fallback supabase properties by code fallo: %s", exc)
    return []


def fetch_properties_by_query(table: str, query: str) -> list[Dict[str, Any]]:
    """
    Busca properties por coincidencia parcial en name/hotel_code/property_name.
    """
    if not query:
        return []
    q = str(query).strip()
    if not q:
        return []
    if supabase:
        try:
            pattern = f"%{q}%"
            response = (
                supabase.table(table)
                .select("*")
                .or_(
                    f"name.ilike.{pattern},hotel_code.ilike.{pattern}"
                )
                .limit(10)
                .execute()
            )
            rows = response.data or []
            return rows
        except Exception as exc:
            log.warning("Fallback supabase properties by query fallo: %s", exc)
    return []


def fetch_property_by_id(table: str, property_id: Any) -> Dict[str, Any]:
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
            if rows:
                log.info("✅ Property found in Supabase: table=%s property_id=%s", table, property_id)
                return rows[0]
        except Exception as exc:
            log.warning("Fallback supabase property_by_id fallo: %s", exc)
    payload = {"tabla": table, "property_id": property_id}
    data = _post_json(PROPERTY_BY_ID_WEBHOOK, payload)
    if data:
        return data
    return {}


def ensure_instance_credentials(
    memory_manager: Any,
    chat_id: str,
) -> None:
    """
    Asegura credenciales de WhatsApp en memoria usando property_id/hotel_code.
    Útil en flujos donde no se invocó la tool de envío.
    """
    if not memory_manager or not chat_id:
        return

    try:
        property_table = memory_manager.get_flag(chat_id, "property_table") or DEFAULT_PROPERTY_TABLE
        property_id = memory_manager.get_flag(chat_id, "property_id")
        hotel_code = memory_manager.get_flag(chat_id, "property_name")
        last_property_id = memory_manager.get_flag(chat_id, "wa_context_property_id")
        last_hotel_code = memory_manager.get_flag(chat_id, "wa_context_hotel_code")

        existing_phone = memory_manager.get_flag(chat_id, "whatsapp_phone_id")
        existing_token = memory_manager.get_flag(chat_id, "whatsapp_token")
        if (
            existing_phone
            and existing_token
            and property_id is not None
            and last_property_id == property_id
        ):
            return
        if (
            existing_phone
            and existing_token
            and hotel_code
            and last_hotel_code
            and str(last_hotel_code).strip().lower() == str(hotel_code).strip().lower()
        ):
            return

        if property_id:
            prop_payload = fetch_property_by_id(property_table, property_id)
            hotel_code = prop_payload.get("hotel_code") or prop_payload.get("name") or hotel_code

        if not hotel_code:
            log.info("🏨 [WA_CTX] no hotel_code/property_id for chat_id=%s", chat_id)
            return

        inst_payload = fetch_instance_by_code(str(hotel_code))
        if not inst_payload:
            log.info("🏨 [WA_CTX] no instance for hotel_code=%s (chat_id=%s)", hotel_code, chat_id)
            return

        for key in ("whatsapp_phone_id", "whatsapp_token", "whatsapp_verify_token"):
            val = inst_payload.get(key)
            if val:
                memory_manager.set_flag(chat_id, key, val)

        memory_manager.set_flag(chat_id, "wa_context_property_id", property_id)
        if hotel_code:
            memory_manager.set_flag(chat_id, "wa_context_hotel_code", str(hotel_code))

        log.info(
            "🏨 [WA_CTX] creds set via ensure_instance_credentials chat_id=%s hotel_code=%s phone_id=%s",
            chat_id,
            hotel_code,
            memory_manager.get_flag(chat_id, "whatsapp_phone_id"),
        )
    except Exception as exc:
        log.warning("🏨 [WA_CTX] error ensuring WA creds: %s", exc)


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

    instance_hotel_code = instance_payload.get("hotel_code")
    if instance_hotel_code:
        memory_manager.set_flag(chat_id, "instance_hotel_code", instance_hotel_code)

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
        should_set_property_id = True
        if not memory_manager.get_flag(chat_id, "property_id"):
            instance_code = instance_payload.get("hotel_code")
            if instance_code and property_table:
                try:
                    rows = fetch_properties_by_code(property_table, str(instance_code))
                except Exception:
                    rows = []
                if len(rows) > 1:
                    should_set_property_id = False
        if should_set_property_id:
            memory_manager.set_flag(chat_id, "property_id", property_id)
            log.info("🏷️ property_id=%s (chat_id=%s)", property_id, chat_id)

    if not property_id and property_table:
        property_name = memory_manager.get_flag(chat_id, "property_name")
        instance_code = memory_manager.get_flag(chat_id, "instance_hotel_code") or instance_payload.get("hotel_code")
        if instance_code:
            memory_manager.set_flag(chat_id, "instance_hotel_code", instance_code)
        if property_name:
            prop_rows = fetch_properties_by_code(property_table, str(property_name))
        elif instance_code:
            prop_rows = fetch_properties_by_code(property_table, str(instance_code))
        else:
            prop_rows = []
        if len(prop_rows) > 1:
            candidates = []
            for row in prop_rows:
                candidates.append(
                    {
                        "property_id": row.get("property_id"),
                        "name": row.get("name") or row.get("property_name"),
                        "hotel_code": row.get("hotel_code"),
                    }
                )
            memory_manager.set_flag(chat_id, "property_disambiguation_candidates", candidates)
            if property_name:
                memory_manager.set_flag(chat_id, "property_disambiguation_hotel_code", str(property_name))
            elif instance_code:
                memory_manager.set_flag(chat_id, "property_disambiguation_hotel_code", str(instance_code))
            log.info(
                "🏨 property disambiguation needed hotel_code=%s candidates=%s",
                property_name or instance_code,
                len(candidates),
            )
        else:
            prop_by_code = prop_rows[0] if prop_rows else {}
            resolved_id = _resolve_property_id(prop_by_code)
            if not resolved_id:
                fallback_name = property_name or instance_code
                if fallback_name:
                    prop_by_name = fetch_property_by_name(property_table, str(fallback_name))
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
        kb_name = _normalize_kb_name(
            prop_details.get("kb") or prop_details.get("kb_name") or prop_details.get("knowledge_base")
        )
        if kb_name:
            memory_manager.set_flag(chat_id, "kb", kb_name)
            memory_manager.set_flag(chat_id, "knowledge_base", kb_name)
        prop_code = prop_details.get("hotel_code")
        prop_display = prop_details.get("name") or prop_details.get("property_name")
        prop_name = prop_code or prop_display
        if prop_name:
            memory_manager.set_flag(chat_id, "property_name", prop_name)
        if prop_display:
            memory_manager.set_flag(chat_id, "property_display_name", prop_display)
