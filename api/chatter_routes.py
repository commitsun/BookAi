"""Rutas FastAPI para el chatter de Roomdoo."""

from __future__ import annotations

import logging
import re
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
    sender: Optional[str] = Field(default="bookai", description="Emisor (cliente, bookai, user)")


class ToggleBookAiRequest(BaseModel):
    bookai_enabled: bool = Field(..., description="Activa o desactiva BookAI para el hilo")


class SendTemplateRequest(BaseModel):
    chat_id: str = Field(..., description="ID del chat (telefono)")
    template_code: str = Field(..., description="Codigo interno de la plantilla")
    hotel_code: Optional[str] = Field(default=None, description="Codigo del hotel (opcional)")
    language: Optional[str] = Field(default="es", description="Idioma de la plantilla")
    parameters: Dict[str, Any] = Field(default_factory=dict, description="Parametros para placeholders")
    channel: str = Field(default="whatsapp", description="Canal de salida")


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


def _map_sender(role: str) -> str:
    role = (role or "").lower()
    if role == "user":
        return "user"
    if role == "assistant":
        return "bookai"
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
        _: None = Depends(_verify_bearer),
    ):
        target = page * page_size
        batch_size = max(200, page_size * 10)
        offset = 0
        ordered_ids: List[str] = []
        summaries: Dict[str, Dict[str, Any]] = {}

        while len(ordered_ids) < target:
            resp = (
                supabase.table("chat_history")
                .select("conversation_id, content, created_at")
                .order("created_at", desc=True)
                .range(offset, offset + batch_size - 1)
                .execute()
            )
            rows = resp.data or []
            if not rows:
                break
            for row in rows:
                cid = str(row.get("conversation_id") or "").strip()
                if not cid or cid in summaries:
                    continue
                ordered_ids.append(cid)
                summaries[cid] = row
                if len(ordered_ids) >= target:
                    break
            if len(rows) < batch_size:
                break
            offset += batch_size

        page_ids = ordered_ids[(page - 1) * page_size:page * page_size]
        pending_map = _pending_actions()
        bookai_flags = _bookai_settings(state)

        items = []
        for cid in page_ids:
            last = summaries.get(cid, {})
            phone = _clean_chat_id(cid)
            items.append(
                {
                    "chat_id": cid,
                    "reservation_id": None,
                    "reservation_status": None,
                    "room_number": None,
                    "checkin": None,
                    "checkout": None,
                    "channel": "whatsapp",
                    "last_message": last.get("content"),
                    "last_message_at": last.get("created_at"),
                    "avatar": None,
                    "client_name": None,
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
        _: None = Depends(_verify_bearer),
    ):
        clean_id = _clean_chat_id(chat_id) or chat_id
        offset = (page - 1) * page_size

        resp = (
            supabase.table("chat_history")
            .select("role, content, created_at, read_status, original_chat_id")
            .eq("conversation_id", clean_id)
            .order("created_at", desc=True)
            .range(offset, offset + page_size - 1)
            .execute()
        )

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
        if payload.channel.lower() != "whatsapp":
            raise HTTPException(status_code=422, detail="Canal no soportado")
        if not payload.message.strip():
            raise HTTPException(status_code=422, detail="Mensaje vacio")

        await state.channel_manager.send_message(chat_id, payload.message, channel="whatsapp")
        try:
            sender = (payload.sender or "bookai").strip().lower()
            role = "assistant"
            if sender in {"cliente", "user", "usuario", "guest"}:
                role = "user"
            state.memory_manager.save(chat_id, role, payload.message)
        except Exception as exc:
            log.warning("No se pudo guardar el mensaje en memoria: %s", exc)

        return {
            "status": "sent",
            "chat_id": chat_id,
            "user_id": payload.user_id,
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
        _: None = Depends(_verify_bearer),
    ):
        clean_id = _clean_chat_id(chat_id) or chat_id
        supabase.table("chat_history").update(
            {"read_status": True}
        ).eq("conversation_id", clean_id).eq("read_status", False).execute()

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
            if rendered:
                state.memory_manager.save(chat_id, role="assistant", content=rendered)
            state.memory_manager.save(
                chat_id,
                role="system",
                content=f"[TEMPLATE_SENT] plantilla={template_name} lang={language}",
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
        _: None = Depends(_verify_bearer),
    ):
        clean_id = _clean_chat_id(chat_id) or chat_id
        resp = (
            supabase.table("chat_history")
            .select("created_at")
            .eq("conversation_id", clean_id)
            .eq("role", "user")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
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
