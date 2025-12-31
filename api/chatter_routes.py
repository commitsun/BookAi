"""Rutas REST para el chatter de Roomdoo."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from core.config import Settings
from core.db import supabase
from core.escalation_db import list_pending_escalations

log = logging.getLogger("ChatterRoutes")


# ---------------------------------------------------------------------------
# Modelos de entrada
# ---------------------------------------------------------------------------
class SendMessageRequest(BaseModel):
    user_id: str = Field(..., description="ID del usuario en Roomdoo")
    chat_id: str = Field(..., description="ID del chat (telefono)")
    message: str = Field(..., description="Texto del mensaje a enviar")
    channel: str = Field(default="whatsapp", description="Canal de salida")


class ToggleBookAiRequest(BaseModel):
    bookai_enabled: bool = Field(..., description="Activa o desactiva BookAI para el hilo")


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
        return "cliente"
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
            state.memory_manager.save(chat_id, "assistant", payload.message)
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

    app.include_router(router)
