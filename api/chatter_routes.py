"""Rutas FastAPI para el chatter de Roomdoo."""

from __future__ import annotations

import logging
import re
from urllib.parse import unquote
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from core.config import Settings
from core.db import supabase
from core.escalation_db import list_pending_escalations
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
    if role in {"user", "guest", "cliente", "usuario"}:
        return "guest"
    if role in {"assistant", "bookai", "ai"}:
        return "bookai"
    if role == "system":
        return "system"
    if role == "tool":
        return "tool"
    return "bookai"


def _pending_actions(limit: int = 200) -> Dict[str, str]:
    """Devuelve un mapa guest_chat_id -> texto pendiente."""
    pending = list_pending_escalations(limit=limit) or []
    result: Dict[str, str] = {}
    for esc in pending:
        guest_id = str(esc.get("guest_chat_id") or "").strip()
        if not guest_id:
            continue
        question = (esc.get("guest_message") or "").strip()
        if not question:
            continue
        result[guest_id] = question
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


# ---------------------------------------------------------------------------
# Registro de rutas
# ---------------------------------------------------------------------------
def register_chatter_routes(app, state) -> None:
    router = APIRouter(prefix="/api/v1/chatter", tags=["chatter"])

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
                key = f"{cid}::{prop_id}" if prop_id is not None else f"{cid}::"
                content = (row.get("content") or "").strip()
                if (
                    not cid
                    or key in summaries
                    or content.startswith("[Superintendente]")
                ):
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
                        .in_("role", ["guest", "user"])
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
            if sender in {"user", "guest", "bookai", "system", "tool"}:
                role = sender
            elif sender in {"assistant", "ai"}:
                role = "bookai"
            elif sender in {"cliente", "usuario"}:
                role = "guest"
            else:
                role = "bookai"
            if property_id is not None:
                state.memory_manager.set_flag(chat_id, "property_id", property_id)
            state.memory_manager.set_flag(chat_id, "default_channel", payload.channel.lower())
            state.memory_manager.save(chat_id, role, payload.message, channel=payload.channel.lower())
        except Exception as exc:
            log.warning("No se pudo guardar el mensaje en memoria: %s", exc)

        return {
            "status": "sent",
            "chat_id": chat_id,
            "user_id": payload.user_id,
            "sender": role,
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
        query = supabase.table("chat_history").select("created_at").in_("role", ["guest", "user"])
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
