"""Rutas FastAPI para exponer herramientas del Superintendente."""

from __future__ import annotations

import logging
import secrets
import string
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from core.config import Settings

log = logging.getLogger("SuperintendenteRoutes")


# ---------------------------------------------------------------------------
# Modelos de entrada
# ---------------------------------------------------------------------------
class SuperintendenteContext(BaseModel):
    owner_id: Optional[str] = Field(
        default=None,
        description="ID del encargado/owner (identidad interna)",
    )
    encargado_id: Optional[str] = Field(
        default=None,
        description="ID legado del encargado (compatibilidad temporal)",
    )
    hotel_name: str = Field(..., description="Nombre del hotel en contexto")
    session_id: Optional[str] = Field(default=None, description="ID de sesión del superintendente")


class AskSuperintendenteRequest(SuperintendenteContext):
    message: str = Field(..., description="Mensaje para el Superintendente")
    context_window: int = Field(default=50, ge=1, le=200)
    chat_history: Optional[list[Any]] = Field(default=None, description="Historial opcional en formato mensajes")


class CreateSessionRequest(SuperintendenteContext):
    title: Optional[str] = Field(default=None, description="Título visible del chat")


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def _verify_bearer(auth_header: Optional[str] = Header(None, alias="Authorization")) -> None:
    """Verifica Bearer Token contra el valor configurado."""
    expected = (Settings.ROOMDOO_BEARER_TOKEN or "").strip()
    if not expected:
        log.error("ROOMDOO_BEARER_TOKEN no configurado.")
        raise HTTPException(status_code=401, detail="Token de integración no configurado")

    if not auth_header or not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Autenticación Bearer requerida")

    token = auth_header.split(" ", 1)[1].strip()
    if token != expected:
        raise HTTPException(status_code=403, detail="Token inválido")


def _tracking_sessions(state) -> dict[str, dict[str, dict[str, Any]]]:
    sessions = state.tracking.setdefault("superintendente_sessions", {})
    if not isinstance(sessions, dict):
        state.tracking["superintendente_sessions"] = {}
        sessions = state.tracking["superintendente_sessions"]
    return sessions


def _generate_session_id(length: int = 12) -> str:
    alphabet = string.ascii_lowercase
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _parse_session_title(content: str) -> Optional[str]:
    if not content:
        return None
    if not content.startswith("[SUPER_SESSION]|"):
        return None
    parts = content.split("|")
    for part in parts:
        if part.startswith("title="):
            return part.replace("title=", "", 1).strip() or None
    return None


def _resolve_owner_id(payload: SuperintendenteContext) -> str:
    owner = (payload.owner_id or "").strip()
    if owner:
        return owner
    legacy = (payload.encargado_id or "").strip()
    if legacy:
        return legacy
    raise HTTPException(status_code=422, detail="owner_id requerido")


# ---------------------------------------------------------------------------
# Registro de rutas
# ---------------------------------------------------------------------------
def register_superintendente_routes(app, state) -> None:
    router = APIRouter(prefix="/api/v1/superintendente", tags=["superintendente"])

    @router.post("/ask")
    async def ask_superintendente(payload: AskSuperintendenteRequest, _: None = Depends(_verify_bearer)):
        agent = getattr(state, "superintendente_agent", None)
        if not agent:
            raise HTTPException(status_code=500, detail="Superintendente no disponible")

        owner_id = _resolve_owner_id(payload)
        result = await agent.ainvoke(
            user_input=payload.message,
            encargado_id=owner_id,
            hotel_name=payload.hotel_name,
            context_window=payload.context_window,
            chat_history=payload.chat_history,
            session_id=payload.session_id,
        )
        return {"result": result}

    @router.post("/sessions")
    async def create_session(payload: CreateSessionRequest, _: None = Depends(_verify_bearer)):
        owner_id = _resolve_owner_id(payload)
        session_id = _generate_session_id()
        title = (payload.title or "").strip()
        if not title:
            title = f"Chat {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}"

        sessions = _tracking_sessions(state)
        owner_sessions = sessions.setdefault(owner_id, {})
        owner_sessions[session_id] = {
            "title": title,
            "created_at": datetime.utcnow().isoformat(),
        }
        state.save_tracking()

        if state.memory_manager:
            try:
                state.memory_manager.set_flag(session_id, "history_table", Settings.SUPERINTENDENTE_HISTORY_TABLE)
                state.memory_manager.set_flag(session_id, "property_name", payload.hotel_name)
                state.memory_manager.set_flag(session_id, "superintendente_owner_id", owner_id)
                marker = f"[SUPER_SESSION]|title={title}"
                state.memory_manager.save(
                    conversation_id=session_id,
                    role="system",
                    content=marker,
                    channel="telegram",
                    original_chat_id=owner_id,
                )
            except Exception as exc:
                log.warning("No se pudo registrar sesión en historia: %s", exc)

        return {"session_id": session_id, "title": title}

    @router.get("/sessions")
    async def list_sessions(
        owner_id: str = Query(...),
        limit: int = Query(default=50, ge=1, le=200),
        _: None = Depends(_verify_bearer),
    ):
        table = Settings.SUPERINTENDENTE_HISTORY_TABLE
        sessions = _tracking_sessions(state).get(owner_id, {})
        titles = {sid: meta.get("title") for sid, meta in sessions.items()}

        items = []
        try:
            resp = (
                state.supabase_client.table(table)
                .select("conversation_id, content, created_at, original_chat_id")
                .eq("original_chat_id", owner_id)
                .order("created_at", desc=True)
                .limit(limit * 20)
                .execute()
            )
            rows = resp.data or []
        except Exception as exc:
            log.warning("No se pudo leer historial superintendente: %s", exc)
            rows = []

        seen = set()
        for row in rows:
            convo_id = str(row.get("conversation_id") or "").strip()
            if not convo_id or convo_id in seen:
                continue
            seen.add(convo_id)
            last_message = row.get("content")
            last_at = row.get("created_at")
            title = titles.get(convo_id) or _parse_session_title(str(last_message or "")) or "Chat"
            items.append(
                {
                    "session_id": convo_id,
                    "title": title,
                    "last_message": last_message,
                    "last_message_at": last_at,
                }
            )
            if len(items) >= limit:
                break

        if len(items) < limit:
            for session_id, meta in sessions.items():
                if session_id in seen:
                    continue
                items.append(
                    {
                        "session_id": session_id,
                        "title": meta.get("title") or "Chat",
                        "last_message": None,
                        "last_message_at": meta.get("created_at"),
                    }
                )
                if len(items) >= limit:
                    break

        return {"items": items}

    @router.get("/sessions/{session_id}/messages")
    async def list_session_messages(
        session_id: str,
        limit: int = Query(default=50, ge=1, le=200),
        _: None = Depends(_verify_bearer),
    ):
        from core.db import get_conversation_history

        rows = get_conversation_history(
            conversation_id=session_id,
            limit=limit,
            table=Settings.SUPERINTENDENTE_HISTORY_TABLE,
        )
        return {"session_id": session_id, "items": rows}

    app.include_router(router)
