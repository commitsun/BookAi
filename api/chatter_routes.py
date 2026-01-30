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

log = logging.getLogger("ChatterRoutes")


# ---------------------------------------------------------------------------
# Modelos de entrada
# ---------------------------------------------------------------------------
class SendMessageRequest(BaseModel):
    user_id: str = Field(..., description="ID del usuario en Roomdoo")
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
    hotel_code: Optional[str] = Field(default=None, description="Codigo del hotel (opcional)")
    language: Optional[str] = Field(default="es", description="Idioma de la plantilla")
    parameters: Dict[str, Any] = Field(default_factory=dict, description="Parametros para placeholders")
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


def _pending_actions(limit: int = 200) -> Dict[str, str]:
    """Devuelve un mapa guest_chat_id -> texto pendiente."""
    pending = list_pending_escalations(limit=limit) or []
    result: Dict[str, str] = {}
    for esc in pending:
        guest_id = _normalize_pending_key(esc.get("guest_chat_id"))
        if not guest_id:
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
        _: None = Depends(_verify_bearer),
    ):
        channel = (channel or "whatsapp").strip().lower()
        if channel not in {"whatsapp", "telegram"}:
            raise HTTPException(status_code=422, detail="Canal no soportado")

        target = page * page_size
        batch_size = max(200, page_size * 10)
        offset = 0
        ordered_keys: List[str] = []
        summaries: Dict[str, Dict[str, Any]] = {}

        while len(ordered_keys) < target:
            resp = (
                supabase.table("chat_history")
                .select("conversation_id, property_id, content, created_at, client_name, channel")
                .eq("channel", channel)
                .order("created_at", desc=True)
                .range(offset, offset + batch_size - 1)
                .execute()
            )
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
                    resp_names = (
                        supabase.table("chat_history")
                        .select("conversation_id, client_name, created_at")
                        .in_("conversation_id", conv_ids)
                        .eq("channel", channel)
                        .in_("role", ["guest"])
                        .order("created_at", desc=True)
                        .limit(500)
                        .execute()
                    )
                    for row in resp_names.data or []:
                        cid = str(row.get("conversation_id") or "").strip()
                        name = row.get("client_name")
                        if cid and name and cid not in client_names:
                            client_names[cid] = name
                except Exception as exc:
                    log.warning("No se pudo cargar client_name: %s", exc)

        items = []
        for key in page_keys:
            last = summaries.get(key, {})
            cid = str(last.get("conversation_id") or "").strip()
            prop_id = last.get("property_id")
            phone = _clean_chat_id(cid)
            items.append(
                {
                    "chat_id": cid,
                    "property_id": prop_id,
                    "reservation_id": None,
                    "reservation_status": None,
                    "room_number": None,
                    "checkin": None,
                    "checkout": None,
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
                    "escalation_messages": pending_messages_map.get(cid),
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

        query = supabase.table("chat_history").select(
            "role, content, created_at, read_status, original_chat_id, property_id"
        )
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
                    "chat_id": clean_id,
                    "created_at": row.get("created_at"),
                    "read_status": row.get("read_status"),
                    "message": row.get("content"),
                    "sender": _map_sender(row.get("role")),
                    "original_chat_id": row.get("original_chat_id"),
                    "property_id": row.get("property_id"),
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

        await state.channel_manager.send_message(chat_id, payload.message, channel="whatsapp")
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
            if property_id is None:
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
                channel=payload.channel.lower(),
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
                )
        except Exception as exc:
            log.warning("No se pudo guardar el mensaje en memoria: %s", exc)

        try:
            resolved_id = resolve_latest_pending_escalation(chat_id, final_response=payload.message)
            if resolved_id:
                log.info("Escalación %s resuelta automáticamente tras enviar mensaje.", resolved_id)
        except Exception as exc:
            log.warning("No se pudo auto-resolver escalación para %s: %s", chat_id, exc)

        now_iso = datetime.now(timezone.utc).isoformat()
        rooms = _rooms(chat_id, property_id, payload.channel.lower())
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
            },
        )

        return {
            "chat_id": clean_id,
            "escalation_id": escalation_id,
            "proposed_response": refined,
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

        return {
            "chat_id": clean_id,
            "escalation_id": escalation_id,
            "ai_message": ai_message,
            "messages": messages,
            "proposed_response": draft_response or None,
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

        return {
            "chat_id": clean_id,
            "read_status": True,
        }

    @router.get("/templates")
    async def list_templates(
        hotel_code: Optional[str] = Query(default=None),
        language: Optional[str] = Query(default=None),
        _: None = Depends(_verify_bearer),
    ):
        registry = _template_registry(state)
        if not registry:
            raise HTTPException(status_code=500, detail="Template registry no disponible")

        items: List[TemplateDefinition] = registry.list_templates()
        results = []
        for tpl in items:
            if hotel_code and (tpl.hotel_code or "").upper() != hotel_code.upper():
                continue
            if language and (tpl.language or "").lower() != language.lower():
                continue
            results.append(
                {
                    "code": tpl.code,
                    "whatsapp_name": tpl.whatsapp_name,
                    "language": tpl.language,
                    "hotel_code": tpl.hotel_code,
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

        template_def = None
        if registry:
            template_def = registry.resolve(
                hotel_code=payload.hotel_code,
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

        try:
            await state.channel_manager.send_template_message(
                chat_id,
                template_name,
                parameters=parameters,
                language=language,
                channel="whatsapp",
            )
        except Exception as exc:
            log.error("Error enviando plantilla: %s", exc, exc_info=True)
            raise HTTPException(status_code=500, detail="Error enviando plantilla")

        rendered = None
        try:
            rendered = template_def.render_content(payload.parameters) if template_def else None
            state.memory_manager.set_flag(chat_id, "default_channel", "whatsapp")
            if rendered:
                if property_id is not None:
                    state.memory_manager.set_flag(chat_id, "property_id", property_id)
                state.memory_manager.save(chat_id, role="bookai", content=rendered, channel="whatsapp")
            if property_id is not None:
                state.memory_manager.set_flag(chat_id, "property_id", property_id)
            state.memory_manager.save(
                chat_id,
                role="system",
                content=f"[TEMPLATE_SENT] plantilla={template_name} lang={language}",
                channel="whatsapp",
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
