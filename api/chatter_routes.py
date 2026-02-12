"""Rutas FastAPI para el chatter de Roomdoo."""

from __future__ import annotations

import logging
import re
from urllib.parse import unquote
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from core.config import Settings, ModelConfig, ModelTier
from core.db import supabase
from core.escalation_db import list_pending_escalations, resolve_latest_pending_escalation
from core.template_registry import TemplateRegistry, TemplateDefinition
from core.instance_context import ensure_instance_credentials
from tools.superintendente_tool import create_consulta_reserva_persona_tool
from core.db import upsert_chat_reservation, get_active_chat_reservation

log = logging.getLogger("ChatterRoutes")


# ---------------------------------------------------------------------------
# Modelos de entrada
# ---------------------------------------------------------------------------
class SendMessageRequest(BaseModel):
    user_id: str = Field(..., description="ID del usuario en Roomdoo")
    user_first_name: Optional[str] = Field(default=None, description="Nombre del usuario")
    user_last_name: Optional[str] = Field(default=None, description="Primer apellido del usuario")
    user_last_name2: Optional[str] = Field(default=None, description="Segundo apellido del usuario")
    chat_id: str = Field(..., description="ID del chat (telefono)")
    message: str = Field(..., description="Texto del mensaje a enviar")
    channel: str = Field(default="whatsapp", description="Canal de salida")
    sender: Optional[str] = Field(default="bookai", description="Emisor (guest/cliente, bookai)")
    property_id: Optional[str] = Field(default=None, description="ID de property (opcional)")


class ToggleBookAiRequest(BaseModel):
    bookai_enabled: bool = Field(..., description="Activa o desactiva BookAI para el hilo")


class SendTemplateRequest(BaseModel):
    chat_id: str = Field(..., description="ID del chat (telefono)")
    template_code: str = Field(..., description="Codigo interno de la plantilla")
    instance_id: Optional[str] = Field(default=None, description="ID de instancia (opcional)")
    language: Optional[str] = Field(default="es", description="Idioma de la plantilla")
    parameters: Dict[str, Any] = Field(default_factory=dict, description="Parametros para placeholders")
    rendered_text: Optional[str] = Field(
        default=None,
        description="Texto renderizado de la plantilla (opcional, para contexto)",
    )
    channel: str = Field(default="whatsapp", description="Canal de salida")
    property_id: Optional[str] = Field(default=None, description="ID de property (opcional)")


class ProposedResponseRequest(BaseModel):
    instruction: str = Field(..., description="Instrucciones para ajustar la respuesta")
    original_response: Optional[str] = Field(
        default=None,
        description="Respuesta base a refinar (opcional)",
    )
    original_response_id: Optional[str] = Field(
        default=None,
        description="ID de la respuesta original (opcional, no usado por ahora)",
    )


class EscalationChatRequest(BaseModel):
    message: str = Field(..., description="Mensaje del operador hacia la IA")


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def _verify_bearer(auth_header: Optional[str] = Header(None, alias="Authorization")) -> None:
    """Verifica Bearer Token contra el valor configurado."""
    expected = (Settings.ROOMDOO_BEARER_TOKEN or "").strip()
    if not expected:
        log.error("ROOMDOO_BEARER_TOKEN no configurado.")
        raise HTTPException(status_code=401, detail="Token de integracion no configurado")

    if not auth_header or not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Autenticacion Bearer requerida")

    token = auth_header.split(" ", 1)[1].strip()
    if token != expected:
        raise HTTPException(status_code=403, detail="Token invalido")


def _clean_chat_id(chat_id: str) -> str:
    return re.sub(r"\D", "", str(chat_id or "")).strip()


def _normalize_property_id(value: Optional[str]) -> Optional[str | int]:
    if value is None:
        return None
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    return text or None


def _map_sender(role: str) -> str:
    role = (role or "").lower()
    if role in {"user", "guest", "bookai", "system", "tool"}:
        return role
    if role in {"assistant", "ai"}:
        return "bookai"
    return "bookai"


def _normalize_pending_key(guest_id: str) -> str:
    raw = str(guest_id or "").strip()
    if not raw:
        return ""
    if ":" in raw:
        tail = raw.split(":")[-1]
        return _clean_chat_id(tail) or tail.strip()
    clean = _clean_chat_id(raw)
    if clean:
        return clean
    return raw


def _extract_reservation_fields(params: Dict[str, Any]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    folio_keys = (
        "folio_id",
        "folioId",
        "folio",
        "localizador",
        "reservation_id",
        "reserva_id",
        "id_reserva",
        "locator",
    )
    checkin_keys = ("checkin", "check_in", "fecha_entrada", "entrada", "arrival", "checkin_date")
    checkout_keys = ("checkout", "check_out", "fecha_salida", "salida", "departure", "checkout_date")

    def _pick(keys):
        for key in keys:
            if key in params and params.get(key) not in (None, ""):
                return str(params.get(key)).strip()
        return None

    return _pick(folio_keys), _pick(checkin_keys), _pick(checkout_keys)


def _extract_property_name(params: Dict[str, Any]) -> Optional[str]:
    if not params:
        return None
    for key in ("hotel", "hotel_name", "property_name", "property"):
        val = params.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _extract_reservation_locator(params: Dict[str, Any]) -> Optional[str]:
    if not params:
        return None
    for key in ("reservation_locator", "locator", "reservation_code", "code", "name"):
        val = params.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _extract_dates_from_reservation(payload: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    if not isinstance(payload, dict):
        return None, None
    if payload.get("checkin") or payload.get("checkout"):
        return payload.get("checkin"), payload.get("checkout")
    if payload.get("firstCheckin") or payload.get("lastCheckout"):
        return payload.get("firstCheckin"), payload.get("lastCheckout")
    reservations = payload.get("reservations") or payload.get("reservation") or []
    if isinstance(reservations, dict):
        reservations = [reservations]
    if isinstance(reservations, list) and reservations:
        res = reservations[0] or {}
        if isinstance(res, dict):
            return res.get("checkin"), res.get("checkout")
    return None, None


def _extract_locator_from_reservation(payload: Dict[str, Any]) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in ("reservation_locator", "locator", "name", "code"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _extract_from_text(text: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    if not text:
        return None, None, None
    folio_match = re.search(r"(folio(?:_id)?)\s*[:#]?\s*([A-Za-z0-9]{4,})", text, re.IGNORECASE)
    if not folio_match:
        folio_match = re.search(r"reserva\s*[:#]?\s*([A-Za-z0-9]{4,})", text, re.IGNORECASE)
    checkin_match = re.search(r"(entrada|check[- ]?in)\s*[:#]?\s*([0-9]{1,2}[-/][0-9]{1,2}[-/][0-9]{2,4})", text, re.IGNORECASE)
    checkout_match = re.search(r"(salida|check[- ]?out)\s*[:#]?\s*([0-9]{1,2}[-/][0-9]{1,2}[-/][0-9]{2,4})", text, re.IGNORECASE)
    if folio_match:
        folio_id = folio_match.group(2) if folio_match.lastindex and folio_match.lastindex >= 2 else folio_match.group(1)
        if not re.fullmatch(r"(?=.*\d)[A-Za-z0-9]{4,}", folio_id):
            folio_id = None
    else:
        folio_id = None
    checkin = checkin_match.group(2) if checkin_match else None
    checkout = checkout_match.group(2) if checkout_match else None
    return folio_id, checkin, checkout


def _pending_actions(limit: int = 200) -> Dict[str, str]:
    """Devuelve un mapa guest_chat_id -> texto pendiente."""
    pending = list_pending_escalations(limit=limit) or []
    result: Dict[str, str] = {}
    for esc in pending:
        guest_id = _normalize_pending_key(esc.get("guest_chat_id"))
        if not guest_id:
            continue
        if guest_id in result:
            continue
        question = (esc.get("guest_message") or "").strip()
        if not question:
            continue
        result[guest_id] = question
    return result


def _pending_reasons(limit: int = 200) -> Dict[str, str]:
    """Devuelve un mapa guest_chat_id -> razón de escalación (texto humano)."""
    pending = list_pending_escalations(limit=limit) or []
    result: Dict[str, str] = {}
    for esc in pending:
        guest_id = _normalize_pending_key(esc.get("guest_chat_id"))
        if not guest_id:
            continue
        if guest_id in result:
            continue
        reason = (esc.get("escalation_reason") or esc.get("reason") or "").strip()
        if not reason:
            continue
        result[guest_id] = reason
    return result


def _pending_types(limit: int = 200) -> Dict[str, str]:
    """Devuelve un mapa guest_chat_id -> tipo de escalación (ej. info_not_found)."""
    pending = list_pending_escalations(limit=limit) or []
    result: Dict[str, str] = {}
    for esc in pending:
        guest_id = _normalize_pending_key(esc.get("guest_chat_id"))
        if not guest_id:
            continue
        if guest_id in result:
            continue
        esc_type = (esc.get("escalation_type") or esc.get("type") or "").strip()
        if not esc_type:
            continue
        result[guest_id] = esc_type
    return result


def _pending_responses(limit: int = 200) -> Dict[str, str]:
    """Devuelve un mapa guest_chat_id -> respuesta propuesta."""
    pending = list_pending_escalations(limit=limit) or []
    result: Dict[str, str] = {}
    for esc in pending:
        guest_id = _normalize_pending_key(esc.get("guest_chat_id"))
        if not guest_id:
            continue
        if guest_id in result:
            continue
        proposed = (esc.get("draft_response") or "").strip()
        if not proposed:
            continue
        result[guest_id] = proposed
    return result


def _pending_messages(limit: int = 200) -> Dict[str, list]:
    """Devuelve un mapa guest_chat_id -> historial de mensajes de escalación."""
    pending = list_pending_escalations(limit=limit) or []
    result: Dict[str, list] = {}
    for esc in pending:
        guest_id = _normalize_pending_key(esc.get("guest_chat_id"))
        if not guest_id:
            continue
        if guest_id in result:
            continue
        messages = esc.get("messages")
        if not messages:
            continue
        if isinstance(messages, list):
            result[guest_id] = messages
    return result


def _bookai_settings(state) -> Dict[str, bool]:
    settings = state.tracking.setdefault("bookai_enabled", {})
    if not isinstance(settings, dict):
        state.tracking["bookai_enabled"] = {}
        settings = state.tracking["bookai_enabled"]
    return settings


def _template_registry(state) -> Optional[TemplateRegistry]:
    registry = getattr(state, "template_registry", None)
    if registry and isinstance(registry, TemplateRegistry):
        return registry
    return None


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _resolve_property_id_from_history(chat_id: str, channel: str = "whatsapp") -> Optional[str | int]:
    """Busca el ultimo property_id no nulo para el chat en DB."""
    decoded_id = str(chat_id or "").strip()
    clean_id = _clean_chat_id(decoded_id) or decoded_id
    id_candidates = {clean_id}
    if decoded_id and decoded_id != clean_id:
        id_candidates.add(decoded_id)
    tail = decoded_id.split(":")[-1] if ":" in decoded_id else ""
    tail_clean = _clean_chat_id(tail) or tail
    if tail_clean:
        id_candidates.add(tail_clean)
    like_patterns = {f"%:{candidate}" for candidate in id_candidates if candidate}

    try:
        or_filters = [f"conversation_id.eq.{candidate}" for candidate in id_candidates if candidate]
        or_filters += [f"conversation_id.like.{pattern}" for pattern in like_patterns]
        rows = []
        if or_filters:
            query = (
                supabase.table("chat_history")
                .select("property_id, created_at")
                .eq("channel", channel)
                .or_(",".join(or_filters))
                .order("created_at", desc=True)
                .limit(10)
            )
            rows = query.execute().data or []
        if not rows and clean_id:
            rows = (
                supabase.table("chat_history")
                .select("property_id, created_at")
                .eq("conversation_id", clean_id)
                .order("created_at", desc=True)
                .limit(20)
                .execute()
                .data
                or []
            )
        for row in rows:
            prop_id = row.get("property_id")
            if prop_id is not None:
                return prop_id
    except Exception as exc:
        log.debug("No se pudo inferir property_id desde history (%s): %s", chat_id, exc)
    return None


def _related_memory_ids(state, chat_id: str) -> list[str]:
    """Intenta alinear aliases (ej. instance:phone) para flags sin duplicar mensajes."""
    ids = set()
    raw = str(chat_id or "").strip()
    if raw:
        ids.add(raw)
    clean = _clean_chat_id(raw) or raw
    if clean:
        ids.add(clean)

    memory_manager = getattr(state, "memory_manager", None)
    if not memory_manager:
        return list(ids)

    last_mem = memory_manager.get_flag(clean, "last_memory_id") if clean else None
    if last_mem and isinstance(last_mem, str):
        ids.add(last_mem.strip())

    suffix = f":{clean}" if clean else ""
    if not suffix:
        return list(ids)

    for store_name in ("state_flags", "runtime_memory"):
        store = getattr(memory_manager, store_name, None)
        if isinstance(store, dict):
            for key in list(store.keys()):
                if isinstance(key, str) and key.endswith(suffix):
                    ids.add(key)

    return list(ids)


def _resolve_whatsapp_context_id(state, chat_id: str) -> Optional[str]:
    """Resuelve el context_id (ej. instancia:telefono) para enrutar WhatsApp."""
    if not state or not chat_id:
        return None

    memory_manager = getattr(state, "memory_manager", None)
    clean = _clean_chat_id(chat_id) or str(chat_id).strip()
    if memory_manager and clean:
        last_mem = memory_manager.get_flag(clean, "last_memory_id")
        if isinstance(last_mem, str) and last_mem.strip():
            return last_mem.strip()

    related = _related_memory_ids(state, chat_id)
    for mem_id in related:
        if not isinstance(mem_id, str) or ":" not in mem_id:
            continue
        tail = mem_id.split(":")[-1]
        if _clean_chat_id(tail) == clean or tail.strip() == clean:
            if memory_manager and clean:
                memory_manager.set_flag(clean, "last_memory_id", mem_id.strip())
            return mem_id.strip()

    return None


# ---------------------------------------------------------------------------
# Registro de rutas
# ---------------------------------------------------------------------------
def register_chatter_routes(app, state) -> None:
    router = APIRouter(prefix="/api/v1/chatter", tags=["chatter"])

    def _rooms(chat_id: str, property_id: Optional[str | int], channel: str) -> list[str]:
        rooms = [f"chat:{chat_id}"]
        if property_id is not None:
            rooms.append(f"property:{property_id}")
        if channel:
            rooms.append(f"channel:{channel}")
        return rooms

    async def _emit(event: str, payload: dict) -> None:
        socket_mgr = getattr(state, "socket_manager", None)
        if not socket_mgr or not getattr(socket_mgr, "enabled", False):
            return
        try:
            await socket_mgr.emit(event, payload, rooms=payload.get("rooms"))
        except Exception as exc:
            log.debug("No se pudo emitir evento socket: %s", exc)

    @router.get("/chats")
    async def list_chats(
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        channel: str = Query(default="whatsapp"),
        property_id: Optional[str] = Query(default=None),
        _: None = Depends(_verify_bearer),
    ):
        channel = (channel or "whatsapp").strip().lower()
        if channel not in {"whatsapp", "telegram"}:
            raise HTTPException(status_code=422, detail="Canal no soportado")
        property_id = _normalize_property_id(property_id)

        target = page * page_size
        batch_size = max(200, page_size * 10)
        offset = 0
        ordered_keys: List[str] = []
        summaries: Dict[str, Dict[str, Any]] = {}

        while len(ordered_keys) < target:
            query = (
                supabase.table("chat_history")
                .select("conversation_id, property_id, content, created_at, client_name, channel")
                .eq("channel", channel)
            )
            if property_id is not None:
                query = query.eq("property_id", property_id)
            resp = query.order("created_at", desc=True).range(
                offset,
                offset + batch_size - 1,
            ).execute()
            rows = resp.data or []
            if not rows:
                break
            for row in rows:
                cid = str(row.get("conversation_id") or "").strip()
                prop_id = row.get("property_id")
                key = cid
                content = (row.get("content") or "").strip()
                if (
                    not cid
                    or key in summaries
                    or content.startswith("[Superintendente]")
                ):
                    if key in summaries:
                        existing = summaries[key]
                        existing_prop = existing.get("property_id")
                        if existing_prop is None and prop_id is not None:
                            summaries[key] = row
                    continue
                ordered_keys.append(key)
                summaries[key] = row
                if len(ordered_keys) >= target:
                    break
            if len(rows) < batch_size:
                break
            offset += batch_size

        page_keys = ordered_keys[(page - 1) * page_size:page * page_size]
        pending_map = _pending_actions()
        pending_reason_map = _pending_reasons()
        pending_type_map = _pending_types()
        proposed_map = _pending_responses()
        pending_messages_map = _pending_messages()
        bookai_flags = _bookai_settings(state)
        client_names: Dict[str, str] = {}
        if page_keys:
            conv_ids = [
                summaries[key].get("conversation_id")
                for key in page_keys
                if summaries.get(key) and summaries[key].get("conversation_id")
            ]
            if conv_ids:
                try:
                    query = (
                        supabase.table("chat_history")
                        .select("conversation_id, client_name, created_at")
                        .in_("conversation_id", conv_ids)
                        .eq("channel", channel)
                        .in_("role", ["guest"])
                    )
                    if property_id is not None:
                        query = query.eq("property_id", property_id)
                    resp_names = query.order("created_at", desc=True).limit(500).execute()
                    for row in resp_names.data or []:
                        cid = str(row.get("conversation_id") or "").strip()
                        name = row.get("client_name")
                        if cid and name and cid not in client_names:
                            client_names[cid] = name
                except Exception as exc:
                    log.warning("No se pudo cargar client_name: %s", exc)

        items = []
        memory_manager = getattr(state, "memory_manager", None)
        for key in page_keys:
            last = summaries.get(key, {})
            cid = str(last.get("conversation_id") or "").strip()
            prop_id = last.get("property_id")
            phone = _clean_chat_id(cid)
            folio_id = None
            reservation_locator = None
            checkin = None
            checkout = None
            reservation_status = None
            room_number = None
            if memory_manager and cid:
                try:
                    folio_id = memory_manager.get_flag(cid, "folio_id") or memory_manager.get_flag(cid, "origin_folio_id")
                    reservation_locator = memory_manager.get_flag(cid, "reservation_locator") or memory_manager.get_flag(cid, "origin_folio_code")
                    checkin = memory_manager.get_flag(cid, "checkin") or memory_manager.get_flag(cid, "origin_folio_min_checkin")
                    checkout = memory_manager.get_flag(cid, "checkout") or memory_manager.get_flag(cid, "origin_folio_max_checkout")
                    reservation_status = memory_manager.get_flag(cid, "reservation_status")
                    room_number = memory_manager.get_flag(cid, "room_number")
                except Exception:
                    pass
            # Siempre prioriza la reserva más próxima por checkin para la property actual.
            try:
                active = get_active_chat_reservation(chat_id=cid, property_id=prop_id)
                if active:
                    folio_id = active.get("folio_id") or folio_id
                    reservation_locator = active.get("reservation_locator") if isinstance(active, dict) else reservation_locator
                    checkin = active.get("checkin") or checkin
                    checkout = active.get("checkout") or checkout
                    if memory_manager and folio_id:
                        memory_manager.set_flag(cid, "folio_id", folio_id)
                    if memory_manager and reservation_locator:
                        memory_manager.set_flag(cid, "reservation_locator", reservation_locator)
                    if memory_manager and checkin:
                        memory_manager.set_flag(cid, "checkin", checkin)
                    if memory_manager and checkout:
                        memory_manager.set_flag(cid, "checkout", checkout)
            except Exception:
                pass
            items.append(
                {
                    "chat_id": cid,
                    "property_id": prop_id,
                    "reservation_id": folio_id,
                    "reservation_locator": reservation_locator,
                    "reservation_status": reservation_status,
                    "room_number": room_number,
                    "checkin": checkin,
                    "checkout": checkout,
                    "channel": last.get("channel") or "whatsapp",
                    "last_message": last.get("content"),
                    "last_message_at": last.get("created_at"),
                    "avatar": None,
                    "client_name": client_names.get(cid) or last.get("client_name"),
                    "client_phone": phone or cid,
                    "bookai_enabled": bool(bookai_flags.get(cid, True)),
                    "unread_count": 0,
                    "needs_action": pending_map.get(cid),
                    "needs_action_type": pending_type_map.get(cid),
                    "needs_action_reason": pending_reason_map.get(cid),
                    "proposed_response": proposed_map.get(cid),
                    "is_final_response": bool(proposed_map.get(cid)),
                    "escalation_messages": pending_messages_map.get(cid),
                    "folio_id": folio_id,
                }
            )

        return {
            "page": page,
            "page_size": page_size,
            "items": items,
        }

    @router.get("/chats/{chat_id}/messages")
    async def list_messages(
        chat_id: str,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        property_id: Optional[str] = Query(default=None),
        _: None = Depends(_verify_bearer),
    ):
        decoded_id = unquote(chat_id or "").strip()
        clean_id = _clean_chat_id(decoded_id) or decoded_id
        id_candidates = {clean_id}
        if decoded_id and decoded_id != clean_id:
            id_candidates.add(decoded_id)
        tail = decoded_id.split(":")[-1] if ":" in decoded_id else ""
        tail_clean = _clean_chat_id(tail) or tail
        if tail_clean:
            id_candidates.add(tail_clean)
        property_id = _normalize_property_id(property_id)
        offset = (page - 1) * page_size
        like_patterns = {f"%:{candidate}" for candidate in id_candidates}

        base_fields = "role, content, created_at, read_status, original_chat_id, property_id"
        extended_fields = f"{base_fields}, user_id, user_first_name, user_last_name, user_last_name2, id"
        try:
            query = supabase.table("chat_history").select(extended_fields)
            if property_id is not None:
                query = query.eq("conversation_id", clean_id).eq("property_id", property_id)
            else:
                or_filters = [f"conversation_id.eq.{candidate}" for candidate in id_candidates]
                or_filters += [f"conversation_id.like.{pattern}" for pattern in like_patterns]
                query = query.or_(",".join(or_filters))
            resp = query.order("created_at", desc=True).range(
                offset,
                offset + page_size - 1,
            ).execute()
        except Exception:
            try:
                fallback_fields = f"{base_fields}, user_id, user_first_name, user_last_name, user_last_name2, message_id"
                query = supabase.table("chat_history").select(fallback_fields)
                if property_id is not None:
                    query = query.eq("conversation_id", clean_id).eq("property_id", property_id)
                else:
                    or_filters = [f"conversation_id.eq.{candidate}" for candidate in id_candidates]
                    or_filters += [f"conversation_id.like.{pattern}" for pattern in like_patterns]
                    query = query.or_(",".join(or_filters))
                resp = query.order("created_at", desc=True).range(
                    offset,
                    offset + page_size - 1,
                ).execute()
            except Exception:
                query = supabase.table("chat_history").select(base_fields)
                if property_id is not None:
                    query = query.eq("conversation_id", clean_id).eq("property_id", property_id)
                else:
                    or_filters = [f"conversation_id.eq.{candidate}" for candidate in id_candidates]
                    or_filters += [f"conversation_id.like.{pattern}" for pattern in like_patterns]
                    query = query.or_(",".join(or_filters))
                resp = query.order("created_at", desc=True).range(
                    offset,
                    offset + page_size - 1,
                ).execute()

        rows = resp.data or []
        rows.reverse()

        items = []
        for row in rows:
            items.append(
                {
                    "message_id": row.get("id") or row.get("message_id"),
                    "chat_id": clean_id,
                    "created_at": row.get("created_at"),
                    "read_status": row.get("read_status"),
                    "content": row.get("content"),
                    "message": row.get("content"),
                    "sender": _map_sender(row.get("role")),
                    "original_chat_id": row.get("original_chat_id"),
                    "property_id": row.get("property_id"),
                    "user_id": row.get("user_id"),
                    "user_first_name": row.get("user_first_name"),
                    "user_last_name": row.get("user_last_name"),
                    "user_last_name2": row.get("user_last_name2"),
                }
            )

        return {
            "chat_id": clean_id,
            "page": page,
            "page_size": page_size,
            "items": items,
        }

    @router.post("/messages")
    async def send_message(payload: SendMessageRequest, _: None = Depends(_verify_bearer)):
        chat_id = _clean_chat_id(payload.chat_id) or payload.chat_id
        property_id = _normalize_property_id(payload.property_id)
        if payload.channel.lower() != "whatsapp":
            raise HTTPException(status_code=422, detail="Canal no soportado")
        if not payload.message.strip():
            raise HTTPException(status_code=422, detail="Mensaje vacio")

        context_id = _resolve_whatsapp_context_id(state, chat_id)
        instance_id = None
        if state.memory_manager:
            try:
                instance_id = state.memory_manager.get_flag(context_id or chat_id, "instance_id") or state.memory_manager.get_flag(context_id or chat_id, "instance_hotel_code")
            except Exception:
                instance_id = None
        # Si hay context_id compuesto y no viene property_id, es ambiguo en multi-instancia.
        if property_id is None and context_id and ":" in str(context_id):
            raise HTTPException(
                status_code=422,
                detail="property_id requerido para enviar mensajes en WhatsApp multi-instancia",
            )
        if state.memory_manager and property_id is not None:
            # Si se especifica property_id, forzar contexto al chat_id para evitar
            # usar instance:telefono de otra property.
            for mem_id in [chat_id]:
                state.memory_manager.set_flag(mem_id, "property_id", property_id)
            ensure_instance_credentials(state.memory_manager, chat_id)
            # Evita usar context_id de otra instancia al enviar manualmente.
            context_id = None
        elif state.memory_manager:
            ensure_instance_credentials(state.memory_manager, context_id or chat_id)

        await state.channel_manager.send_message(
            chat_id,
            payload.message,
            channel="whatsapp",
            context_id=context_id,
        )
        try:
            sender = (payload.sender or "bookai").strip().lower()
            if sender in {"user", "hotel", "staff"}:
                role = "user"
            elif sender in {"guest", "cliente"}:
                role = "guest"
            elif sender in {"bookai", "assistant", "ai", "system", "tool"}:
                role = "bookai"
            else:
                role = "bookai"
            related_ids = _related_memory_ids(state, chat_id)
            if property_id is None:
                for mem_id in related_ids:
                    try:
                        candidate = state.memory_manager.get_flag(mem_id, "property_id")
                    except Exception:
                        candidate = None
                    if candidate is not None:
                        property_id = candidate
                        break
            if property_id is None and ":" not in str(chat_id):
                property_id = _resolve_property_id_from_history(chat_id, payload.channel.lower())
            for mem_id in related_ids:
                if property_id is not None:
                    state.memory_manager.set_flag(mem_id, "property_id", property_id)
                state.memory_manager.set_flag(mem_id, "default_channel", payload.channel.lower())
                # Al responder manualmente, limpiamos posibles pendientes antiguos.
                state.memory_manager.clear_flag(mem_id, "escalation_in_progress")
                state.memory_manager.clear_flag(mem_id, "escalation_confirmation_pending")
                state.memory_manager.clear_flag(mem_id, "consulta_base_realizada")
                state.memory_manager.clear_flag(mem_id, "inciso_enviado")
            state.memory_manager.save(
                chat_id,
                role,
                payload.message,
                user_id=payload.user_id if role == "user" else None,
                user_first_name=payload.user_first_name if role == "user" else None,
                user_last_name=payload.user_last_name if role == "user" else None,
                user_last_name2=payload.user_last_name2 if role == "user" else None,
                channel=payload.channel.lower(),
                original_chat_id=context_id or None,
                bypass_force_guest_role=role == "user",
            )
            for mem_id in related_ids:
                if mem_id == chat_id:
                    continue
                state.memory_manager.add_runtime_message(
                    mem_id,
                    role,
                    payload.message,
                    channel=payload.channel.lower(),
                    original_chat_id=chat_id,
                    bypass_force_guest_role=role == "user",
                    user_id=payload.user_id if role == "user" else None,
                    user_first_name=payload.user_first_name if role == "user" else None,
                    user_last_name=payload.user_last_name if role == "user" else None,
                    user_last_name2=payload.user_last_name2 if role == "user" else None,
                )
        except Exception as exc:
            log.warning("No se pudo guardar el mensaje en memoria: %s", exc)

        rooms = _rooms(chat_id, property_id, payload.channel.lower())
        try:
            resolved_id = resolve_latest_pending_escalation(chat_id, final_response=payload.message)
            if resolved_id:
                log.info("Escalación %s resuelta automáticamente tras enviar mensaje.", resolved_id)
                await _emit(
                    "escalation.resolved",
                    {
                        "rooms": rooms,
                        "chat_id": chat_id,
                        "escalation_id": resolved_id,
                        "final_response": payload.message,
                    },
                )
                await _emit(
                    "chat.updated",
                    {
                        "rooms": rooms,
                        "chat_id": chat_id,
                        "property_id": property_id,
                        "channel": payload.channel.lower(),
                        "needs_action": None,
                        "needs_action_type": None,
                        "needs_action_reason": None,
                        "proposed_response": None,
                        "is_final_response": False,
                    },
                )
        except Exception as exc:
            log.warning("No se pudo auto-resolver escalación para %s: %s", chat_id, exc)

        now_iso = datetime.now(timezone.utc).isoformat()
        await _emit(
            "chat.message.created",
            {
                "rooms": rooms,
                "chat_id": chat_id,
                "property_id": property_id,
                "channel": payload.channel.lower(),
                "sender": role,
                "message": payload.message,
                "created_at": now_iso,
            },
        )
        await _emit(
            "chat.updated",
            {
                "rooms": rooms,
                "chat_id": chat_id,
                "property_id": property_id,
                "channel": payload.channel.lower(),
                "last_message": payload.message,
                "last_message_at": now_iso,
            },
        )

        return {
            "status": "sent",
            "chat_id": chat_id,
            "user_id": payload.user_id,
            "sender": role,
        }

    @router.post("/chats/{chat_id}/proposed-response")
    async def refine_proposed_response(
        chat_id: str,
        payload: "ProposedResponseRequest",
        _: None = Depends(_verify_bearer),
    ):
        clean_id = _clean_chat_id(chat_id) or chat_id
        instruction = (payload.instruction or "").strip()
        if not instruction:
            raise HTTPException(status_code=422, detail="instruction requerida")

        from core.escalation_db import get_latest_pending_escalation, update_escalation

        esc = get_latest_pending_escalation(clean_id)
        if not esc:
            raise HTTPException(status_code=404, detail="No hay escalación pendiente")

        escalation_id = str(esc.get("escalation_id") or "").strip()
        if not escalation_id:
            raise HTTPException(status_code=404, detail="Escalación inválida")

        from agents.interno_agent import InternoAgent
        from tools.interno_tool import ESCALATIONS_STORE, Escalation
        interno_agent = getattr(state, "interno_agent", None)
        if not interno_agent or not isinstance(interno_agent, InternoAgent):
            interno_agent = InternoAgent(memory_manager=state.memory_manager)

        if escalation_id not in ESCALATIONS_STORE:
            ESCALATIONS_STORE[escalation_id] = Escalation(
                escalation_id=escalation_id,
                guest_chat_id=clean_id,
                guest_message=(esc.get("guest_message") or "").strip(),
                escalation_type=(esc.get("escalation_type") or "manual"),
                escalation_reason=(esc.get("escalation_reason") or esc.get("reason") or "").strip(),
                context=(esc.get("context") or "").strip(),
                timestamp=str(esc.get("timestamp") or ""),
                draft_response=(esc.get("draft_response") or "").strip() or None,
                manager_confirmed=bool(esc.get("manager_confirmed") or False),
                final_response=(esc.get("final_response") or None),
                sent_to_guest=bool(esc.get("sent_to_guest") or False),
            )

        base_response = (payload.original_response or esc.get("draft_response") or "").strip()
        if not base_response:
            base_response = (esc.get("guest_message") or "").strip()
        if base_response:
            ESCALATIONS_STORE[escalation_id].draft_response = base_response
            update_escalation(escalation_id, {"draft_response": base_response})

        from tools.interno_tool import generar_borrador
        result = generar_borrador(
            escalation_id=escalation_id,
            manager_response=base_response,
            adjustment=instruction,
        )

        from core.message_utils import extract_clean_draft

        def _strip_instruction_block(text: str) -> str:
            if not text:
                return text
            cut_markers = [
                "📝 *Nuevo borrador generado según tus ajustes:*",
                "📝 *BORRADOR DE RESPUESTA PROPUESTO:*",
                "Se ha generado el siguiente borrador",
                "Se ha generado el siguiente borrador según tus indicaciones:",
                "el texto, escribe tus ajustes directamente.",
                "✏️ Si deseas modificar",
                "✏️ Si deseas más cambios",
                "✅ Si estás conforme",
                "Si deseas modificar el texto",
                "Si deseas más cambios",
                "responde con 'OK' para enviarlo al huésped",
            ]
            for marker in cut_markers:
                if marker in text:
                    parts = text.split(marker, 1)
                    # Si el marcador es encabezado, nos quedamos con lo que viene después.
                    if marker.startswith("📝"):
                        text = parts[1].strip() if len(parts) > 1 else ""
                    elif marker.startswith("Se ha generado"):
                        text = parts[1].strip() if len(parts) > 1 else ""
                    else:
                        text = parts[0].strip()
            # Limpia líneas vacías o restos  de instrucciones.
            lines = []
            for ln in text.splitlines():
                stripped = ln.strip()
                if not stripped:
                    continue
                if stripped.startswith("- Para la escalación"):
                    continue
                if stripped.startswith("- La escalación"):
                    continue
                if stripped.lower().startswith("la escalación"):
                    continue
                if stripped.lower().startswith("si deseas"):
                    continue
                if stripped.lower().startswith("si estás conforme"):
                    continue
                lines.append(stripped)
            return "\n".join(lines).strip()

        refined = extract_clean_draft(result or "").strip() or result.strip()
        refined = _strip_instruction_block(refined)
        update_escalation(escalation_id, {"draft_response": refined})

        await _emit(
            "escalation.updated",
            {
                "rooms": _rooms(clean_id, None, "whatsapp"),
                "chat_id": clean_id,
                "escalation_id": escalation_id,
                "draft_response": refined,
            },
        )
        await _emit(
            "chat.proposed_response.updated",
            {
                "rooms": _rooms(clean_id, None, "whatsapp"),
                "chat_id": clean_id,
                "proposed_response": refined,
                "is_final_response": True,
            },
        )

        return {
            "chat_id": clean_id,
            "escalation_id": escalation_id,
            "proposed_response": refined,
            "is_final_response": True,
        }

    @router.post("/chats/{chat_id}/escalation-chat")
    async def escalation_chat(
        chat_id: str,
        payload: EscalationChatRequest,
        _: None = Depends(_verify_bearer),
    ):
        clean_id = _clean_chat_id(chat_id) or chat_id
        message = (payload.message or "").strip()
        if not message:
            raise HTTPException(status_code=422, detail="message requerida")

        from core.escalation_db import get_latest_pending_escalation, append_escalation_message

        esc = get_latest_pending_escalation(clean_id)
        if not esc:
            raise HTTPException(status_code=404, detail="No hay escalación pendiente")

        escalation_id = str(esc.get("escalation_id") or "").strip()
        if not escalation_id:
            raise HTTPException(status_code=404, detail="Escalación inválida")

        def _classify_operator_intent(text: str) -> str:
            """
            Clasifica la intención del operador:
            - "draft": quiere que se genere un borrador al huésped
            - "adjustment": quiere ajustar/refinar un borrador existente
            - "context": solo pregunta contexto o hace consultas internas
            """
            if not text:
                return "context"
            system_prompt = (
                "Clasifica la intención del operador en SOLO una etiqueta:\n"
                "draft, adjustment, context.\n"
                "draft = el operador pide redactar/generar/enviar una respuesta al huésped.\n"
                "adjustment = el operador pide modificar/ajustar un borrador existente.\n"
                "context = el operador pide contexto o info interna, sin generar respuesta.\n"
                "Ejemplos:\n"
                "- \"pregúntale qué le ocurre\" => draft\n"
                "- \"añade que le subiremos algo\" => adjustment\n"
                "- \"quita la última frase\" => adjustment\n"
                "- \"¿qué dijo exactamente?\" => context\n"
                "- \"responde al huésped\" => draft\n"
                "- \"hazlo más corto\" => adjustment\n"
                "Responde SOLO con la etiqueta."
            )
            user_prompt = f"Mensaje del operador:\n{text}\nEtiqueta:"
            try:
                llm = ModelConfig.get_llm(ModelTier.INTERNAL)
                raw = llm.invoke(
                    [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ]
                )
                label = (getattr(raw, "content", None) or str(raw or "")).strip().lower()
                if label in {"draft", "adjustment", "context"}:
                    return label
            except Exception:
                pass
            return "context"

        pre_messages = esc.get("messages") or []
        first_interaction = not isinstance(pre_messages, list) or len(pre_messages) == 0

        operator_ts = datetime.now(timezone.utc).isoformat()
        messages = append_escalation_message(
            escalation_id=escalation_id,
            role="operator",
            content=message,
            timestamp=operator_ts,
        )

        guest_message = (esc.get("guest_message") or "").strip()
        esc_type = (esc.get("escalation_type") or esc.get("type") or "").strip()
        reason = (esc.get("escalation_reason") or esc.get("reason") or "").strip()
        context = (esc.get("context") or "").strip()
        draft_response = (esc.get("draft_response") or "").strip()

        intent = _classify_operator_intent(message)
        log.info(
            "Escalation-chat intent=%s chat_id=%s escalation_id=%s message=%s",
            intent,
            clean_id,
            escalation_id,
            message,
        )
        wants_draft = intent == "draft"
        wants_adjustment = intent == "adjustment"

        if wants_draft or wants_adjustment:
            from tools.interno_tool import ESCALATIONS_STORE, Escalation, generar_borrador
            from core.message_utils import extract_clean_draft

            if escalation_id not in ESCALATIONS_STORE:
                ESCALATIONS_STORE[escalation_id] = Escalation(
                    escalation_id=escalation_id,
                    guest_chat_id=clean_id,
                    guest_message=guest_message,
                    escalation_type=esc_type or "manual",
                    escalation_reason=reason,
                    context=context,
                    timestamp=str(esc.get("timestamp") or ""),
                    draft_response=draft_response or None,
                    manager_confirmed=bool(esc.get("manager_confirmed") or False),
                    final_response=(esc.get("final_response") or None),
                    sent_to_guest=bool(esc.get("sent_to_guest") or False),
                )

            base_response = draft_response or guest_message or ""
            if base_response:
                ESCALATIONS_STORE[escalation_id].draft_response = base_response

            raw_borrador = (generar_borrador(
                escalation_id=escalation_id,
                manager_response=base_response,
                adjustment=message,
            ) or "").strip()

            def _strip_instruction_block(text: str) -> str:
                if not text:
                    return text
                cut_markers = [
                    "📝 *Nuevo borrador generado según tus ajustes:*",
                    "📝 *BORRADOR DE RESPUESTA PROPUESTO:*",
                    "Se ha generado el siguiente borrador",
                    "Se ha generado el siguiente borrador según tus indicaciones:",
                    "el texto, escribe tus ajustes directamente.",
                    "✏️ Si deseas modificar",
                    "✏️ Si deseas más cambios",
                    "✅ Si estás conforme",
                    "Si deseas modificar el texto",
                    "Si deseas más cambios",
                    "responde con 'OK' para enviarlo al huésped",
                ]
                for marker in cut_markers:
                    if marker in text:
                        parts = text.split(marker, 1)
                        if marker.startswith("📝") or marker.startswith("Se ha generado"):
                            text = parts[1].strip() if len(parts) > 1 else ""
                        else:
                            text = parts[0].strip()
                lines = []
                for ln in text.splitlines():
                    stripped = ln.strip()
                    if not stripped:
                        continue
                    if stripped.lower().startswith("si deseas"):
                        continue
                    if stripped.lower().startswith("si estás conforme"):
                        continue
                    lines.append(stripped)
                return "\n".join(lines).strip()

            clean_draft = extract_clean_draft(raw_borrador or "").strip() or raw_borrador
            clean_draft = _strip_instruction_block(clean_draft)
            if not clean_draft:
                clean_draft = "No tengo suficiente información para generar un borrador."

            def _brief_escalation_summary(reason: str) -> str:
                base_reason = reason or "Sin motivo registrado"
                return f"Motivo de la escalación: {base_reason}."

            if first_interaction:
                ai_message = _brief_escalation_summary(reason)
            else:
                ai_message = ""

            draft_response = (
                (ESCALATIONS_STORE.get(escalation_id).draft_response or "").strip()
                if ESCALATIONS_STORE.get(escalation_id)
                else draft_response
            )
        else:
            system_prompt = (
                "Eres un asistente interno para operadores de hotel. "
                "Responde preguntas sobre el contexto de la escalación con claridad y brevedad. "
                "No generes la respuesta final al huésped a menos que el operador lo solicite explícitamente. "
                "Si falta información, indícalo."
            )
            user_prompt = (
                "Contexto de escalación:\n"
                f"- Mensaje del huésped: {guest_message or 'No disponible'}\n"
                f"- Tipo: {esc_type or 'No disponible'}\n"
                f"- Motivo: {reason or 'No disponible'}\n"
                f"- Contexto: {context or 'No disponible'}\n"
                f"- Borrador actual: {draft_response or 'No disponible'}\n\n"
                f"Pregunta del operador:\n{message}"
            )

            llm = ModelConfig.get_llm(ModelTier.INTERNAL)
            ai_raw = llm.invoke(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ]
            )
            ai_message = (getattr(ai_raw, "content", None) or str(ai_raw or "")).strip()
            if not ai_message:
                ai_message = "No tengo suficiente información para responder."

        ai_ts = datetime.now(timezone.utc).isoformat()
        messages = append_escalation_message(
            escalation_id=escalation_id,
            role="ai",
            content=ai_message,
            timestamp=ai_ts,
        )

        await _emit(
            "escalation.chat.updated",
            {
                "rooms": _rooms(clean_id, None, "whatsapp"),
                "chat_id": clean_id,
                "escalation_id": escalation_id,
                "messages": messages,
                "ai_message": ai_message,
            },
        )
        await _emit(
            "escalation.updated",
            {
                "rooms": _rooms(clean_id, None, "whatsapp"),
                "chat_id": clean_id,
                "escalation_id": escalation_id,
                "messages": messages,
                "ai_message": ai_message,
                "draft_response": draft_response or None,
            },
        )

        if draft_response:
            await _emit(
                "chat.proposed_response.updated",
                {
                    "rooms": _rooms(clean_id, None, "whatsapp"),
                    "chat_id": clean_id,
                    "proposed_response": draft_response,
                    "is_final_response": True,
                },
            )

        if wants_draft or wants_adjustment:
            proposed_response = draft_response or None
            is_final_response = bool(draft_response)
        else:
            proposed_response = None
            is_final_response = False

        return {
            "chat_id": clean_id,
            "escalation_id": escalation_id,
            "ai_message": ai_message,
            "messages": messages,
            "proposed_response": proposed_response,
            "is_final_response": is_final_response,
        }

    @router.get("/chats/{chat_id}/escalation-chat")
    async def get_escalation_chat(
        chat_id: str,
        escalation_id: Optional[str] = Query(default=None),
        _: None = Depends(_verify_bearer),
    ):
        clean_id = _clean_chat_id(chat_id) or chat_id

        from core.escalation_db import (
            get_escalation,
            get_latest_pending_escalation,
            get_escalation_messages,
        )

        esc = get_latest_pending_escalation(clean_id)
        # Si el frontend envía un escalation_id distinto al pendiente, no devolvemos su historial.
        if not esc:
            raise HTTPException(status_code=404, detail="No hay escalación pendiente")
        pending_id = str(esc.get("escalation_id") or "").strip()
        if escalation_id and pending_id and escalation_id.strip() != pending_id:
            return {
                "chat_id": clean_id,
                "escalation_id": pending_id,
                "messages": [],
                "proposed_response": (esc.get("draft_response") or "").strip() or None,
                "is_final_response": bool((esc.get("draft_response") or "").strip()),
            }

        escalation_id = pending_id or str(escalation_id or "").strip()
        if not escalation_id:
            raise HTTPException(status_code=404, detail="Escalación inválida")

        draft_response = (esc.get("draft_response") or "").strip()
        messages = get_escalation_messages(escalation_id)

        return {
            "chat_id": clean_id,
            "escalation_id": escalation_id,
            "messages": messages,
            "proposed_response": draft_response or None,
            "is_final_response": bool(draft_response),
        }

    @router.patch("/chats/{chat_id}/bookai")
    async def toggle_bookai(
        chat_id: str,
        payload: ToggleBookAiRequest,
        _: None = Depends(_verify_bearer),
    ):
        clean_id = _clean_chat_id(chat_id) or chat_id
        bookai_flags = _bookai_settings(state)
        bookai_flags[clean_id] = payload.bookai_enabled
        state.save_tracking()

        await _emit(
            "chat.bookai.toggled",
            {
                "rooms": _rooms(clean_id, None, "whatsapp"),
                "chat_id": clean_id,
                "bookai_enabled": payload.bookai_enabled,
            },
        )
        await _emit(
            "chat.updated",
            {
                "rooms": _rooms(clean_id, None, "whatsapp"),
                "chat_id": clean_id,
                "bookai_enabled": payload.bookai_enabled,
            },
        )

        return {
            "chat_id": clean_id,
            "bookai_enabled": payload.bookai_enabled,
        }

    @router.patch("/chats/{chat_id}/read")
    async def mark_chat_read(
        chat_id: str,
        property_id: Optional[str] = Query(default=None),
        _: None = Depends(_verify_bearer),
    ):
        clean_id = _clean_chat_id(chat_id) or chat_id
        property_id = _normalize_property_id(property_id)
        like_pattern = f"%:{clean_id}"
        query = supabase.table("chat_history").update({"read_status": True}).eq(
            "read_status",
            False,
        )
        if property_id is not None:
            query = query.eq("conversation_id", clean_id).eq("property_id", property_id)
        else:
            query = query.or_(
                f"conversation_id.eq.{clean_id},conversation_id.like.{like_pattern}"
            )
        query.execute()

        await _emit(
            "chat.read",
            {
                "rooms": _rooms(clean_id, property_id, "whatsapp"),
                "chat_id": clean_id,
                "property_id": property_id,
                "read_status": True,
            },
        )
        await _emit(
            "chat.updated",
            {
                "rooms": _rooms(clean_id, property_id, "whatsapp"),
                "chat_id": clean_id,
                "property_id": property_id,
                "read_status": True,
            },
        )

        return {
            "chat_id": clean_id,
            "read_status": True,
        }

    @router.get("/templates")
    async def list_templates(
        instance_id: Optional[str] = Query(default=None),
        language: Optional[str] = Query(default=None),
        _: None = Depends(_verify_bearer),
    ):
        registry = _template_registry(state)
        if not registry:
            raise HTTPException(status_code=500, detail="Template registry no disponible")

        items: List[TemplateDefinition] = registry.list_templates()
        results = []
        for tpl in items:
            if instance_id and (tpl.instance_id or "").upper() != instance_id.upper():
                continue
            if language and (tpl.language or "").lower() != language.lower():
                continue
            results.append(
                {
                    "code": tpl.code,
                    "whatsapp_name": tpl.whatsapp_name,
                    "language": tpl.language,
                    "instance_id": tpl.instance_id,
                    "description": tpl.description,
                    "content": tpl.content,
                    "parameter_format": tpl.parameter_format,
                    "parameter_order": tpl.parameter_order,
                    "parameter_hints": tpl.parameter_hints,
                }
            )

        return {"items": results}

    @router.post("/templates/send")
    async def send_template(payload: SendTemplateRequest, _: None = Depends(_verify_bearer)):
        chat_id = _clean_chat_id(payload.chat_id) or payload.chat_id
        property_id = _normalize_property_id(payload.property_id)
        if payload.channel.lower() != "whatsapp":
            raise HTTPException(status_code=422, detail="Canal no soportado")

        registry = _template_registry(state)
        template_code = payload.template_code
        language = (payload.language or "es").lower()
        instance_id = (payload.instance_id or "").strip() or None

        template_def = None
        if registry:
            template_def = registry.resolve(
                instance_id=instance_id,
                template_code=template_code,
                language=language,
            )

        if template_def:
            template_name = template_def.whatsapp_name or template_code
            parameters = template_def.build_meta_parameters(payload.parameters)
            language = template_def.language or language
        else:
            template_name = template_code
            parameters = list((payload.parameters or {}).values())

        context_id = _resolve_whatsapp_context_id(state, chat_id)
        folio_id = None
        reservation_locator = None
        checkin = None
        checkout = None
        folio_from_params = False
        try:
            if payload.parameters:
                f_id, ci, co = _extract_reservation_fields(payload.parameters)
                folio_id = f_id
                folio_from_params = bool(f_id)
                checkin = ci
                checkout = co
                reservation_locator = _extract_reservation_locator(payload.parameters)
            if payload.rendered_text:
                f_id, ci, co = _extract_from_text(payload.rendered_text)
                folio_id = folio_id or f_id
                checkin = checkin or ci
                checkout = checkout or co
        except Exception as exc:
            log.warning("No se pudo extraer folio/checkin/checkout: %s", exc)

        if state.memory_manager:
            if property_id is not None:
                for mem_id in [context_id, chat_id]:
                    if mem_id:
                        state.memory_manager.set_flag(mem_id, "property_id", property_id)
            if instance_id:
                for mem_id in [context_id, chat_id]:
                    if mem_id:
                        state.memory_manager.set_flag(mem_id, "instance_id", instance_id)
                        state.memory_manager.set_flag(mem_id, "instance_hotel_code", instance_id)
            if payload.parameters:
                inferred_name = _extract_property_name(payload.parameters)
                if inferred_name:
                    for mem_id in [context_id, chat_id]:
                        if mem_id:
                            state.memory_manager.set_flag(mem_id, "property_name", inferred_name)
            if folio_id:
                for mem_id in [context_id, chat_id]:
                    if mem_id:
                        state.memory_manager.set_flag(mem_id, "folio_id", folio_id)
            if reservation_locator:
                for mem_id in [context_id, chat_id]:
                    if mem_id:
                        state.memory_manager.set_flag(mem_id, "reservation_locator", reservation_locator)
            if checkin:
                for mem_id in [context_id, chat_id]:
                    if mem_id:
                        state.memory_manager.set_flag(mem_id, "checkin", checkin)
            if checkout:
                for mem_id in [context_id, chat_id]:
                    if mem_id:
                        state.memory_manager.set_flag(mem_id, "checkout", checkout)
            ensure_instance_credentials(state.memory_manager, context_id or chat_id)
            if not instance_id:
                try:
                    instance_id = state.memory_manager.get_flag(context_id or chat_id, "instance_id") or state.memory_manager.get_flag(context_id or chat_id, "instance_hotel_code")
                except Exception:
                    instance_id = None

        try:
            await state.channel_manager.send_template_message(
                chat_id,
                template_name,
                parameters=parameters,
                language=language,
                channel="whatsapp",
                context_id=context_id,
            )
        except Exception as exc:
            log.error("Error enviando plantilla: %s", exc, exc_info=True)
            raise HTTPException(status_code=500, detail="Error enviando plantilla")

        if folio_id and folio_from_params:
            try:
                log.info(
                    "🧾 chatter upsert_chat_reservation chat_id=%s folio_id=%s checkin=%s checkout=%s property_id=%s instance_id=%s",
                    chat_id,
                    folio_id,
                    checkin,
                    checkout,
                    property_id,
                    instance_id,
                )
                upsert_chat_reservation(
                    chat_id=chat_id,
                    folio_id=folio_id,
                    checkin=checkin,
                    checkout=checkout,
                    property_id=property_id,
                    instance_id=instance_id,
                    original_chat_id=context_id or None,
                    reservation_locator=reservation_locator,
                    source="template",
                )
            except Exception as exc:
                log.warning("No se pudo persistir reserva en tabla: %s", exc)

        if folio_id and folio_from_params and (not checkin or not checkout):
            try:
                consulta_tool = create_consulta_reserva_persona_tool(
                    memory_manager=state.memory_manager,
                    chat_id=context_id or chat_id,
                )
                raw = await consulta_tool.ainvoke(
                    {
                        "folio_id": folio_id,
                        "property_id": property_id,
                        "instance_id": instance_id,
                    }
                )
                parsed = None
                if isinstance(raw, str):
                    try:
                        import json

                        parsed = json.loads(raw)
                    except Exception:
                        parsed = None
                elif isinstance(raw, dict):
                    parsed = raw
                if parsed:
                    ci, co = _extract_dates_from_reservation(parsed)
                    locator = _extract_locator_from_reservation(parsed)
                    if ci:
                        state.memory_manager.set_flag(chat_id, "checkin", ci)
                    if co:
                        state.memory_manager.set_flag(chat_id, "checkout", co)
                    if locator:
                        state.memory_manager.set_flag(chat_id, "reservation_locator", locator)
                    if folio_id and folio_from_params:
                        upsert_chat_reservation(
                            chat_id=chat_id,
                            folio_id=folio_id,
                            checkin=ci or checkin,
                            checkout=co or checkout,
                            property_id=property_id,
                            instance_id=instance_id,
                            original_chat_id=context_id or None,
                            reservation_locator=locator,
                            source="pms",
                        )
            except Exception as exc:
                log.warning("No se pudo enriquecer checkin/checkout via folio: %s", exc)

        rendered = None
        try:
            rendered = (payload.rendered_text or "").strip() or None
            if not rendered:
                rendered = template_def.render_content(payload.parameters) if template_def else None
            if not rendered and template_def:
                rendered = template_def.render_fallback_summary(payload.parameters)
            if not rendered and payload.parameters:
                rendered = "Parametros de plantilla:\n" + "\n".join(
                    f"{k}: {v}" for k, v in payload.parameters.items()
                    if v is not None and str(v).strip() != ""
                )
            if rendered and not reservation_locator:
                m = re.search(r"(localizador)\s*[:#]?\s*([A-Za-z0-9/\\-]{4,})", rendered, re.IGNORECASE)
                if m:
                    reservation_locator = m.group(2)
                    for mem_id in [context_id, chat_id]:
                        if mem_id:
                            state.memory_manager.set_flag(mem_id, "reservation_locator", reservation_locator)
            if rendered and not folio_id:
                try:
                    f_id, ci, co = _extract_from_text(rendered)
                    folio_id = folio_id or f_id
                    checkin = checkin or ci
                    checkout = checkout or co
                    if folio_id:
                        for mem_id in [context_id, chat_id]:
                            if mem_id:
                                state.memory_manager.set_flag(mem_id, "folio_id", folio_id)
                    if checkin:
                        for mem_id in [context_id, chat_id]:
                            if mem_id:
                                state.memory_manager.set_flag(mem_id, "checkin", checkin)
                    if checkout:
                        for mem_id in [context_id, chat_id]:
                            if mem_id:
                                state.memory_manager.set_flag(mem_id, "checkout", checkout)
                except Exception as exc:
                    log.warning("No se pudo extraer folio/checkin/checkout desde rendered: %s", exc)
            if reservation_locator and folio_id and folio_from_params:
                try:
                    upsert_chat_reservation(
                        chat_id=chat_id,
                        folio_id=folio_id,
                        checkin=checkin,
                        checkout=checkout,
                        property_id=property_id,
                        instance_id=instance_id,
                        original_chat_id=context_id or None,
                        reservation_locator=reservation_locator,
                        source="rendered",
                    )
                except Exception as exc:
                    log.warning("No se pudo persistir reservation_locator desde rendered: %s", exc)
            state.memory_manager.set_flag(chat_id, "default_channel", "whatsapp")
            if rendered:
                if property_id is not None:
                    state.memory_manager.set_flag(chat_id, "property_id", property_id)
                state.memory_manager.save(
                    chat_id,
                    role="bookai",
                    content=rendered,
                    channel="whatsapp",
                    original_chat_id=context_id or None,
                )
            if property_id is not None:
                state.memory_manager.set_flag(chat_id, "property_id", property_id)
            state.memory_manager.save(
                chat_id,
                role="system",
                content=f"[TEMPLATE_SENT] plantilla={template_name} lang={language}",
                channel="whatsapp",
                original_chat_id=context_id or None,
            )
        except Exception as exc:
            log.warning("No se pudo registrar plantilla en memoria: %s", exc)

        now_iso = datetime.now(timezone.utc).isoformat()
        rooms = _rooms(chat_id, property_id, "whatsapp")
        await _emit(
            "chat.message.created",
            {
                "rooms": rooms,
                "chat_id": chat_id,
                "property_id": property_id,
                "channel": "whatsapp",
                "sender": "bookai",
                "message": rendered or template_name,
                "created_at": now_iso,
                "template": template_name,
                "template_language": language,
            },
        )
        await _emit(
            "chat.updated",
            {
                "rooms": rooms,
                "chat_id": chat_id,
                "property_id": property_id,
                "channel": "whatsapp",
                "last_message": rendered or template_name,
                "last_message_at": now_iso,
            },
        )

        return {
            "status": "sent",
            "chat_id": chat_id,
            "template": template_name,
            "language": language,
            "instance_id": instance_id,
        }

    @router.get("/chats/{chat_id}/window")
    async def check_window(
        chat_id: str,
        property_id: Optional[str] = Query(default=None),
        _: None = Depends(_verify_bearer),
    ):
        clean_id = _clean_chat_id(chat_id) or chat_id
        property_id = _normalize_property_id(property_id)
        # Also consider compound conversation ids like "prefix:<chat_id>".
        like_pattern = f"%:{clean_id}"
        query = supabase.table("chat_history").select("created_at").in_("role", ["guest"])
        if property_id is not None:
            query = query.eq("conversation_id", clean_id).eq("property_id", property_id)
        else:
            query = query.or_(
                f"conversation_id.eq.{clean_id},conversation_id.like.{like_pattern}"
            )
        resp = query.order("created_at", desc=True).limit(1).execute()
        rows = resp.data or []
        last_ts = rows[0].get("created_at") if rows else None
        last_dt = _parse_ts(last_ts)

        if not last_dt:
            return {
                "chat_id": clean_id,
                "needs_template": True,
                "last_user_message_at": None,
                "hours_since_last_user_msg": None,
                "reason": "necesita_plantilla",
            }

        now = datetime.now(timezone.utc)
        delta_hours = (now - last_dt).total_seconds() / 3600.0
        needs_template = delta_hours >= 24

        return {
            "chat_id": clean_id,
            "needs_template": needs_template,
            "last_user_message_at": last_dt.isoformat(),
            "hours_since_last_user_msg": round(delta_hours, 2),
            "reason": "ventana_superada" if needs_template else "ventana_activa",
        }

    app.include_router(router)
