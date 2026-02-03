"""Rutas FastAPI para exponer herramientas del Superintendente."""

from __future__ import annotations

import logging
import re
import secrets
import string
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from core.config import Settings
from core.constants import WA_CONFIRM_WORDS, WA_CANCEL_WORDS
from core.instance_context import ensure_instance_credentials
from core.message_utils import sanitize_wa_message

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


def _normalize_guest_id(guest_id: str | None) -> str:
    return str(guest_id or "").replace("+", "").strip()


def _is_short_wa_confirmation(text: str) -> bool:
    clean = re.sub(r"[¡!¿?.]", "", (text or "").lower()).strip()
    tokens = [t for t in re.findall(r"[a-záéíóúñ]+", clean) if t]

    confirm_words = set(WA_CONFIRM_WORDS) | {"vale", "listo"}
    if clean in confirm_words:
        return True

    return 0 < len(tokens) <= 2 and all(tok in confirm_words for tok in tokens)


def _is_short_wa_cancel(text: str) -> bool:
    clean = re.sub(r"[¡!¿?.]", "", (text or "").lower()).strip()
    tokens = [t for t in re.findall(r"[a-záéíóúñ]+", clean) if t]

    cancel_words = set(WA_CANCEL_WORDS) | {"cancelado", "cancelo", "cancela"}
    if clean in cancel_words:
        return True

    return 0 < len(tokens) <= 2 and all(tok in cancel_words for tok in tokens)


def _looks_like_new_instruction(text: str) -> bool:
    if not text:
        return False
    action_terms = {
        "mandale",
        "mándale",
        "enviale",
        "envíale",
        "manda",
        "mensaje",
        "whatsapp",
        "historial",
        "convers",
        "broadcast",
        "plantilla",
        "resumen",
        "agrega",
        "añade",
        "anade",
        "elimina",
        "borra",
    }
    lowered = text.lower()
    return any(term in lowered for term in action_terms)


def _clean_wa_payload(msg: str) -> str:
    base = sanitize_wa_message(msg or "")
    if not base:
        return base
    base = re.sub(r"\[\s*superintendente\s*\]", "", base, flags=re.IGNORECASE).strip()
    cut_markers = [
        "borrador",
        "confirma",
        "confirmar",
        "por favor",
        "ok para",
        "ok p",
        "plantilla",
    ]
    lower = base.lower()
    cuts = [lower.find(m) for m in cut_markers if lower.find(m) > 0]
    if cuts:
        base = base[: min(cuts)].strip()
    return base.strip()


def _ensure_guest_language(msg: str, guest_id: str) -> str:
    return msg


def _parse_wa_drafts(raw_text: str) -> list[dict]:
    if "[WA_DRAFT]|" not in (raw_text or ""):
        return []
    drafts: list[dict] = []
    parts = (raw_text or "").split("[WA_DRAFT]|")
    for chunk in parts[1:]:
        if not chunk:
            continue
        subparts = chunk.split("|", 1)
        if len(subparts) < 2:
            continue
        guest_id = subparts[0].strip()
        msg = subparts[1].strip()
        if not guest_id or not msg:
            continue
        msg_clean = _clean_wa_payload(msg)
        msg_clean = _ensure_guest_language(msg_clean, guest_id)
        drafts.append({"guest_id": guest_id, "message": msg_clean})
    return drafts


def _recover_wa_drafts_from_memory(state, *conversation_ids: str) -> list[dict]:
    if not state or not state.memory_manager:
        return []
    marker = "[WA_DRAFT]|"
    for conv_id in [cid for cid in conversation_ids if cid]:
        try:
            recent = state.memory_manager.get_memory(conv_id, limit=20)
        except Exception:
            continue
        for msg in reversed(recent or []):
            content = msg.get("content", "") or ""
            if marker in content:
                chunk = content[content.index(marker):]
                parts = chunk.split("|", 2)
                if len(parts) == 3:
                    return [{"guest_id": parts[1], "message": parts[2]}]
    return []


def _persist_pending_wa(state, key: str, payload: Any) -> None:
    if not state or not key:
        return
    try:
        store = state.tracking.setdefault("superintendente_pending_wa", {})
        if not isinstance(store, dict):
            state.tracking["superintendente_pending_wa"] = {}
            store = state.tracking["superintendente_pending_wa"]
        store[str(key)] = payload
        state.save_tracking()
    except Exception:
        pass


def _load_pending_wa(state, key: str) -> Any:
    if not state or not key:
        return None
    try:
        store = state.tracking.get("superintendente_pending_wa", {})
        if isinstance(store, dict):
            return store.get(str(key))
    except Exception:
        pass
    return None


def _format_wa_preview(drafts: list[dict]) -> str:
    if not drafts:
        return ""
    if len(drafts) == 1:
        guest_id = drafts[0].get("guest_id")
        msg = drafts[0].get("message", "")
        return (
            f"📝 Borrador WhatsApp para {guest_id}:\n"
            f"{msg}\n\n"
            "✏️ Escribe ajustes directamente si deseas modificarlo.\n"
            "✅ Responde 'sí' para enviar.\n"
            "❌ Responde 'no' para descartar."
        )

    lines = ["📝 Borradores de WhatsApp preparados:"]
    for draft in drafts:
        guest_id = draft.get("guest_id", "")
        msg = draft.get("message", "")
        lines.append(f"• {guest_id}: {msg}")
    lines.append("")
    lines.append("✏️ Escribe ajustes para aplicar a todos.")
    lines.append("✅ Responde 'sí' para enviar todos.")
    lines.append("❌ Responde 'no' para descartar.")
    return "\n".join(lines)


async def _rewrite_wa_draft(llm, base_message: str, adjustments: str) -> str:
    clean_base = sanitize_wa_message(base_message or "")
    clean_adj = sanitize_wa_message(adjustments or "")
    if not clean_adj:
        return clean_base
    if not llm:
        if clean_base and clean_adj:
            return _clean_wa_payload(f"{clean_base}. {clean_adj}")
        return _clean_wa_payload(clean_base or clean_adj)

    system = (
        "Eres el asistente del encargado de un hotel. "
        "Genera un único mensaje corto de WhatsApp en español neutro, tono cordial y directo. "
        "Incluye las ideas del mensaje base y los ajustes. "
        "No añadas instrucciones, confirmaciones ni comillas; entrega solo el texto listo para enviar."
    )
    user_msg = (
        "Mensaje base:\n"
        f"{clean_base or 'N/A'}\n\n"
        "Ajustes solicitados:\n"
        f"{clean_adj}\n\n"
        "Devuelve solo el mensaje final en una línea."
    )
    try:
        response = await llm.ainvoke(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ]
        )
        text = (getattr(response, "content", None) or "").strip()
        if not text:
            return _clean_wa_payload(clean_adj)
        return _clean_wa_payload(text)
    except Exception as exc:
        log.warning("No se pudo reformular borrador WA: %s", exc)
        return _clean_wa_payload(clean_adj or clean_base)


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
        session_key = payload.session_id or owner_id
        alt_key = owner_id if payload.session_id else None
        message = (payload.message or "").strip()
        auto_send_wa = False  # En Chatter mantenemos borrador + confirmación.
        if state.memory_manager:
            try:
                state.memory_manager.set_flag(session_key, "history_table", Settings.SUPERINTENDENTE_HISTORY_TABLE)
                if alt_key:
                    state.memory_manager.set_flag(alt_key, "history_table", Settings.SUPERINTENDENTE_HISTORY_TABLE)
            except Exception:
                pass

        if not auto_send_wa:
            pending_wa = state.superintendente_pending_wa.get(session_key)
            if not pending_wa and alt_key:
                pending_wa = state.superintendente_pending_wa.get(alt_key)
            if not pending_wa:
                pending_wa = _load_pending_wa(state, session_key) or (alt_key and _load_pending_wa(state, alt_key))
            if not pending_wa and message and not _looks_like_new_instruction(message):
                recovered = _recover_wa_drafts_from_memory(state, session_key, alt_key)
                if not recovered:
                    try:
                        sessions = _tracking_sessions(state).get(owner_id, {})
                        for sid in list(sessions.keys())[-5:]:
                            recovered = _recover_wa_drafts_from_memory(state, sid)
                            if recovered:
                                break
                    except Exception:
                        recovered = []
                if recovered:
                    pending_wa = recovered[0] if len(recovered) == 1 else {"drafts": recovered}
                    state.superintendente_pending_wa[session_key] = pending_wa
                    if alt_key:
                        state.superintendente_pending_wa[alt_key] = pending_wa
                    _persist_pending_wa(state, session_key, pending_wa)
                    if alt_key:
                        _persist_pending_wa(state, alt_key, pending_wa)
            if pending_wa and message and not _looks_like_new_instruction(message):
                if _is_short_wa_cancel(message):
                    state.superintendente_pending_wa.pop(session_key, None)
                    if alt_key:
                        state.superintendente_pending_wa.pop(alt_key, None)
                    _persist_pending_wa(state, session_key, None)
                    if alt_key:
                        _persist_pending_wa(state, alt_key, None)
                    return {"result": "❌ Envío cancelado. Si necesitas otro borrador, dímelo."}

                if _is_short_wa_confirmation(message):
                    drafts = pending_wa.get("drafts") if isinstance(pending_wa, dict) else [pending_wa]
                    drafts = drafts or []
                    if not drafts:
                        state.superintendente_pending_wa.pop(session_key, None)
                        if alt_key:
                            state.superintendente_pending_wa.pop(alt_key, None)
                        return {"result": "⚠️ No hay borrador pendiente para enviar."}

                    if state.memory_manager:
                        try:
                            ensure_instance_credentials(state.memory_manager, session_key)
                        except Exception:
                            pass

                    sent = 0
                    for draft in drafts:
                        guest_id = draft.get("guest_id")
                        msg_raw = draft.get("message", "")
                        if not guest_id:
                            continue
                        msg_to_send = _clean_wa_payload(msg_raw)
                        msg_to_send = _ensure_guest_language(msg_to_send, guest_id)
                        await state.channel_manager.send_message(
                            guest_id,
                            msg_to_send,
                            channel="whatsapp",
                            context_id=session_key,
                        )
                        try:
                            if state.memory_manager:
                                state.memory_manager.save(guest_id, "assistant", msg_to_send, channel="whatsapp")
                                state.memory_manager.save(
                                    session_key,
                                    "system",
                                    f"[WA_SENT]|{guest_id}|{msg_to_send}",
                                    channel="superintendente",
                                )
                        except Exception:
                            pass
                        sent += 1

                    state.superintendente_pending_wa.pop(session_key, None)
                    if alt_key:
                        state.superintendente_pending_wa.pop(alt_key, None)
                    _persist_pending_wa(state, session_key, None)
                    if alt_key:
                        _persist_pending_wa(state, alt_key, None)
                    guest_list = ", ".join([_normalize_guest_id(d.get("guest_id")) for d in drafts if d.get("guest_id")])
                    return {"result": f"✅ Mensaje enviado a {sent}/{len(drafts)} huésped(es): {guest_list}"}

                drafts = pending_wa.get("drafts") if isinstance(pending_wa, dict) else [pending_wa]
                drafts = drafts or []
                llm = getattr(state.superintendente_agent, "llm", None)
                updated: list[dict] = []
                for draft in drafts:
                    guest_id = draft.get("guest_id")
                    base_msg = draft.get("message", "")
                    rewritten = await _rewrite_wa_draft(llm, base_msg, message)
                    updated.append(
                        {
                            **draft,
                            "guest_id": guest_id,
                            "message": _ensure_guest_language(rewritten, guest_id),
                        }
                    )
                if not updated:
                    return {"result": "⚠️ No hay borrador pendiente para ajustar."}
                pending_payload: Any = {"drafts": updated} if len(updated) > 1 else updated[0]
                state.superintendente_pending_wa[session_key] = pending_payload
                if alt_key:
                    state.superintendente_pending_wa[alt_key] = pending_payload
                _persist_pending_wa(state, session_key, pending_payload)
                if alt_key:
                    _persist_pending_wa(state, alt_key, pending_payload)
                return {"result": _format_wa_preview(updated)}

        result = await agent.ainvoke(
            user_input=message,
            encargado_id=owner_id,
            hotel_name=payload.hotel_name,
            context_window=payload.context_window,
            chat_history=payload.chat_history,
            session_id=payload.session_id,
        )

        wa_drafts = _parse_wa_drafts(result)
        if wa_drafts:
            if auto_send_wa:
                if state.memory_manager:
                    try:
                        ensure_instance_credentials(state.memory_manager, session_key)
                    except Exception:
                        pass
                sent = 0
                for draft in wa_drafts:
                    guest_id = draft.get("guest_id")
                    msg_raw = draft.get("message", "")
                    if not guest_id:
                        continue
                    msg_to_send = _clean_wa_payload(msg_raw)
                    msg_to_send = _ensure_guest_language(msg_to_send, guest_id)
                    await state.channel_manager.send_message(
                        guest_id,
                        msg_to_send,
                        channel="whatsapp",
                        context_id=session_key,
                    )
                    try:
                        if state.memory_manager:
                            state.memory_manager.save(guest_id, "assistant", msg_to_send, channel="whatsapp")
                            state.memory_manager.save(
                                session_key,
                                "system",
                                f"[WA_SENT]|{guest_id}|{msg_to_send}",
                                channel="superintendente",
                            )
                    except Exception:
                        pass
                    sent += 1
                guest_list = ", ".join([_normalize_guest_id(d.get("guest_id")) for d in wa_drafts if d.get("guest_id")])
                return {"result": f"✅ Mensaje enviado a {sent}/{len(wa_drafts)} huésped(es): {guest_list}"}
            if state.memory_manager:
                try:
                    ctx_property_id = state.memory_manager.get_flag(session_key, "property_id")
                    ctx_hotel_code = state.memory_manager.get_flag(session_key, "property_name")
                    for draft in wa_drafts:
                        if ctx_property_id is not None:
                            draft["property_id"] = ctx_property_id
                        if ctx_hotel_code:
                            draft["hotel_code"] = ctx_hotel_code
                        guest_id = draft.get("guest_id")
                        if guest_id:
                            if ctx_property_id is not None:
                                state.memory_manager.set_flag(guest_id, "property_id", ctx_property_id)
                            if ctx_hotel_code:
                                state.memory_manager.set_flag(guest_id, "property_name", ctx_hotel_code)
                except Exception:
                    pass
            pending_payload: Any = {"drafts": wa_drafts} if len(wa_drafts) > 1 else wa_drafts[0]
            state.superintendente_pending_wa[session_key] = pending_payload
            if alt_key:
                state.superintendente_pending_wa[alt_key] = pending_payload
            _persist_pending_wa(state, session_key, pending_payload)
            if alt_key:
                _persist_pending_wa(state, alt_key, pending_payload)
            try:
                if state.memory_manager and wa_drafts:
                    draft = wa_drafts[0]
                    state.memory_manager.save(
                        conversation_id=session_key,
                        role="system",
                        content=f"[WA_DRAFT]|{draft.get('guest_id')}|{draft.get('message')}",
                        channel="superintendente",
                    )
                    if alt_key:
                        state.memory_manager.save(
                            conversation_id=alt_key,
                            role="system",
                            content=f"[WA_DRAFT]|{draft.get('guest_id')}|{draft.get('message')}",
                            channel="superintendente",
                        )
            except Exception:
                pass
            return {"result": _format_wa_preview(wa_drafts)}

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
