"""Fetch and store dynamic instance context from Supabase (via n8n webhooks)."""

from __future__ import annotations

import logging
import os
import re
import json
import asyncio
import time
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
_PROPERTIES_BY_CODE_CACHE_TTL_SECONDS = 30
_properties_by_code_cache: dict[tuple[str, str], tuple[float, list[Dict[str, Any]]]] = {}


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


def _mcp_tool_matches(name: str, description: str | None = None) -> bool:
    n = (name or "").strip().lower()
    d = (description or "").strip().lower()
    if n in {"property id", "property_id", "propertyid"}:
        return True
    if "property" in n and "id" in n:
        return True
    if d and ("property" in d and "id" in d):
        return True
    if d and ("properties" in d and "instance" in d):
        return True
    return False


def _fetch_properties_by_code_mcp(table: str, instance_id: str) -> list[Dict[str, Any]]:
    if not get_tools:
        return []
    if not instance_id:
        return []

    cache_key = (str(table or "").strip().lower(), str(instance_id or "").strip().lower())
    cached = _properties_by_code_cache.get(cache_key)
    now_ts = time.time()
    if cached:
        cached_at, cached_rows = cached
        if now_ts - cached_at <= _PROPERTIES_BY_CODE_CACHE_TTL_SECONDS:
            return list(cached_rows or [])
        _properties_by_code_cache.pop(cache_key, None)

    async def _load_tools():
        for server in ("DispoPreciosAgent", "OnboardingAgent", "InfoAgent"):
            try:
                tools = await get_tools(server_name=server)
            except Exception:
                continue
            for tool in tools or []:
                if _mcp_tool_matches(getattr(tool, "name", ""), getattr(tool, "description", None)):
                    return tool
            # Fallback: si hay alguna tool relacionada con property, úsala.
            for tool in tools or []:
                name = (getattr(tool, "name", "") or "").lower()
                desc = (getattr(tool, "description", "") or "").lower()
                if "property" in name or "properties" in name or "property" in desc or "properties" in desc:
                    return tool
        return None

    try:
        tool = _run_async(_load_tools())
    except Exception:
        tool = None

    if not tool:
        try:
            for server in ("DispoPreciosAgent", "OnboardingAgent", "InfoAgent"):
                tools = _run_async(get_tools(server_name=server))
                if tools:
                    log.warning(
                        "MCP property tool no encontrado en %s. Tools disponibles: %s",
                        server,
                        [getattr(t, "name", "") for t in tools],
                    )
        except Exception:
            pass
        return []

    log.info("MCP property tool seleccionado: %s", getattr(tool, "name", ""))
    payloads = [
        {"instance_id": instance_id},
        {"tabla": table, "instance_id": instance_id},
    ]
    raw = None
    last_exc = None
    for payload in payloads:
        try:
            raw = _run_async(tool.ainvoke(payload))
            if raw:
                break
        except Exception as exc:
            last_exc = exc
            continue
    if raw is None and last_exc:
        log.warning("MCP property tool fallo: %s", last_exc)
        return []

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}

    data = _extract_payload(raw)
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict) and isinstance(raw.get("response"), list):
        rows = raw.get("response") or []
    elif isinstance(data, list):
        rows = data
    elif isinstance(data, dict) and data:
        rows = [data]
    elif isinstance(raw, dict) and raw:
        rows = [raw]
    else:
        rows = []

    _properties_by_code_cache[cache_key] = (time.time(), rows)
    return rows


def fetch_instance_by_number(whatsapp_number: str) -> Dict[str, Any]:
    normalized = _normalize_phone_number(whatsapp_number or "")
    payload = {"whatsApp_number": normalized or whatsapp_number}
    data = _post_json(INSTANCE_LOOKUP_WEBHOOK, payload)
    if data:
        return data
    if supabase:
        try:
            candidates = []
            raw = (whatsapp_number or "").strip()
            if raw:
                candidates.append(raw)
            if normalized:
                candidates.append(normalized)
                candidates.append(f"+{normalized}")
            # Evita duplicados
            candidates = [c for i, c in enumerate(candidates) if c and c not in candidates[:i]]
            if candidates:
                or_filters = ",".join([f"whatsapp_number.eq.{c}" for c in candidates])
                resp = (
                    supabase.table("instances")
                    .select("*")
                    .or_(or_filters)
                    .limit(1)
                    .execute()
                )
            else:
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


def fetch_instance_by_phone_id(whatsapp_phone_id: str) -> Dict[str, Any]:
    if not whatsapp_phone_id:
        return {}
    payload = {"whatsapp_phone_id": whatsapp_phone_id}
    data = _post_json(INSTANCE_LOOKUP_WEBHOOK, payload)
    if data:
        return data
    if supabase:
        try:
            resp = (
                supabase.table("instances")
                .select("*")
                .eq("whatsapp_phone_id", whatsapp_phone_id)
                .limit(1)
                .execute()
            )
            rows = resp.data or []
            return rows[0] if rows else {}
        except Exception as exc:
            log.warning("Fallback supabase instances (phone_id) fallo: %s", exc)
    return {}


def fetch_instance_by_code(instance_id: str) -> Dict[str, Any]:
    payload = {"instance_id": instance_id}
    data = _post_json(INSTANCE_BY_CODE_WEBHOOK, payload)
    if data:
        return data
    log.info("🔎 Instance fallback by code via Supabase: instance_id=%s", instance_id)
    if supabase:
        try:
            resp = (
                supabase.table("instances")
                .select("*")
                .eq("instance_id", instance_id)
                .limit(1)
                .execute()
            )
            rows = resp.data or []
            if rows:
                log.info("✅ Instance found in Supabase: instance_id=%s", instance_id)
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
    payload = {"tabla": table, "name": name}
    data = _post_json(PROPERTY_BY_NAME_WEBHOOK, payload)
    if data:
        return data
    return {}


def fetch_property_by_code(table: str, instance_id: str) -> Dict[str, Any]:
    rows = _fetch_properties_by_code_mcp(table, instance_id)
    if rows:
        return rows[0]
    return {}


def fetch_properties_by_code(table: str, instance_id: str) -> list[Dict[str, Any]]:
    """
    Devuelve multiples properties por instance_id si existen.
    Usa MCP como fuente única.
    """
    mcp_rows = _fetch_properties_by_code_mcp(table, instance_id)
    return mcp_rows or []


def fetch_properties_by_query(table: str, query: str) -> list[Dict[str, Any]]:
    """
    Busca properties por coincidencia parcial en name/property_name.
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
                    f"name.ilike.{pattern},property_name.ilike.{pattern}"
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
    Asegura credenciales de WhatsApp en memoria usando property_id/instance_id.
    Útil en flujos donde no se invocó la tool de envío.
    """
    if not memory_manager or not chat_id:
        return

    try:
        property_table = memory_manager.get_flag(chat_id, "property_table") or DEFAULT_PROPERTY_TABLE
        property_id = memory_manager.get_flag(chat_id, "property_id")
        instance_id = memory_manager.get_flag(chat_id, "instance_id") or memory_manager.get_flag(chat_id, "instance_hotel_code")
        last_property_id = memory_manager.get_flag(chat_id, "wa_context_property_id")
        last_instance_id = memory_manager.get_flag(chat_id, "wa_context_instance_id")

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
            and instance_id
            and last_instance_id
            and str(last_instance_id).strip().lower() == str(instance_id).strip().lower()
        ):
            return

        if property_id:
            prop_payload = fetch_property_by_id(property_table, property_id)
            instance_id = prop_payload.get("instance_id") or instance_id
            if not instance_id:
                instance_id = prop_payload.get("name") or instance_id

        if not instance_id:
            log.info("🏨 [WA_CTX] no instance_id/property_id for chat_id=%s", chat_id)
            return

        inst_payload = fetch_instance_by_code(str(instance_id))
        if not inst_payload:
            log.info("🏨 [WA_CTX] no instance for instance_id=%s (chat_id=%s)", instance_id, chat_id)
            return

        for key in ("whatsapp_phone_id", "whatsapp_token", "whatsapp_verify_token"):
            val = inst_payload.get(key)
            if val:
                memory_manager.set_flag(chat_id, key, val)

        memory_manager.set_flag(chat_id, "wa_context_property_id", property_id)
        if instance_id:
            memory_manager.set_flag(chat_id, "wa_context_instance_id", str(instance_id))

        log.info(
            "🏨 [WA_CTX] creds set via ensure_instance_credentials chat_id=%s instance_id=%s phone_id=%s",
            chat_id,
            instance_id,
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
    instance_phone_id: Optional[str] = None,
) -> None:
    """Fetch instance + property metadata and store into MemoryManager flags."""
    memory_manager = getattr(state, "memory_manager", None)
    if not memory_manager or not chat_id:
        return

    normalized_number = _normalize_phone_number(instance_number or "")
    cached_number = memory_manager.get_flag(chat_id, "instance_number")
    cached_phone_id = memory_manager.get_flag(chat_id, "whatsapp_phone_id")

    instance_payload: Dict[str, Any] = {}
    if instance_phone_id and (cached_phone_id != instance_phone_id or not memory_manager.get_flag(chat_id, "instance_url")):
        log.info("🔎 Buscando instancia por phone_id=%s chat_id=%s", instance_phone_id, chat_id)
        instance_payload = fetch_instance_by_phone_id(instance_phone_id)
        if instance_payload:
            log.info("✅ Instancia encontrada por phone_id: %s", list(instance_payload.keys()))
            memory_manager.set_flag(chat_id, "whatsapp_phone_id", instance_phone_id)
        else:
            log.warning("⚠️ Sin datos de instancia para phone_id=%s", instance_phone_id)

    if not instance_payload and normalized_number and (cached_number != normalized_number or not memory_manager.get_flag(chat_id, "instance_url")):
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

    instance_id = instance_payload.get("instance_id") or instance_payload.get("instance_url")
    if instance_id:
        memory_manager.set_flag(chat_id, "instance_id", instance_id)
        memory_manager.set_flag(chat_id, "instance_hotel_code", instance_id)

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

    # No fijar property_id desde payload de instancia para evitar mezcla entre instancias.
    # Solo fijar si ya estaba en memoria o si la instancia tiene UNA sola property.
    property_id = memory_manager.get_flag(chat_id, "property_id")
    prop_rows_from_instance: list[Dict[str, Any]] = []
    if not property_id:
        instance_code = instance_payload.get("instance_id") or instance_payload.get("instance_url")
        if instance_code and property_table:
            try:
                prop_rows_from_instance = fetch_properties_by_code(property_table, str(instance_code))
            except Exception:
                prop_rows_from_instance = []
            if len(prop_rows_from_instance) == 1:
                property_id = prop_rows_from_instance[0].get("property_id")
    if property_id:
        memory_manager.set_flag(chat_id, "property_id", property_id)
        log.info("🏷️ property_id=%s (chat_id=%s)", property_id, chat_id)

    if not property_id and property_table:
        property_name = memory_manager.get_flag(chat_id, "property_name")
        instance_code = memory_manager.get_flag(chat_id, "instance_id") or instance_payload.get("instance_id") or instance_payload.get("instance_url")
        if instance_code:
            memory_manager.set_flag(chat_id, "instance_id", instance_code)
            memory_manager.set_flag(chat_id, "instance_hotel_code", instance_code)
        if property_name:
            prop_rows = fetch_properties_by_query(property_table, str(property_name))
        elif prop_rows_from_instance:
            prop_rows = prop_rows_from_instance
        elif instance_code:
            prop_rows = fetch_properties_by_code(property_table, str(instance_code))
        else:
            prop_rows = []
        if len(prop_rows) > 1:
            candidates = []
            for row in prop_rows:
                address = (
                    row.get("address")
                    or row.get("direccion")
                    or row.get("full_address")
                    or row.get("address_line")
                    or row.get("address1")
                )
                street = row.get("street") or row.get("street_address") or address
                city = row.get("city") or row.get("ciudad") or row.get("town") or row.get("locality")
                candidates.append(
                    {
                        "property_id": row.get("property_id"),
                        "name": row.get("name") or row.get("property_name"),
                        "instance_id": row.get("instance_id"),
                        "city": city,
                        "street": street,
                        "address": address,
                    }
                )
            memory_manager.set_flag(chat_id, "property_disambiguation_candidates", candidates)
            if property_name:
                memory_manager.set_flag(chat_id, "property_disambiguation_instance_id", str(property_name))
            elif instance_code:
                memory_manager.set_flag(chat_id, "property_disambiguation_instance_id", str(instance_code))
            log.info(
                "🏨 property disambiguation needed instance_id=%s candidates=%s",
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
        instance_code = memory_manager.get_flag(chat_id, "instance_id")
        if instance_code:
            inst_by_code = fetch_instance_by_code(str(instance_code))
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
        prop_display = prop_details.get("name") or prop_details.get("property_name")
        prop_name = prop_display
        if prop_name:
            memory_manager.set_flag(chat_id, "property_name", prop_name)
        if prop_display:
            memory_manager.set_flag(chat_id, "property_display_name", prop_display)
