"""Rutas FastAPI para exponer herramientas del Superintendente."""

from __future__ import annotations

import json
import logging
import re
import secrets
import string
from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from core.config import Settings, ModelConfig, ModelTier
from core.constants import WA_CONFIRM_WORDS, WA_CANCEL_WORDS
from core.db import attach_structured_payload_to_latest_message, get_active_chat_reservation
from core.instance_context import ensure_instance_credentials
from core.language_manager import language_manager
from core.message_utils import sanitize_wa_message, looks_like_new_instruction, build_kb_preview

log = logging.getLogger("SuperintendenteRoutes")
_SUPER_STATE: Any | None = None
_INTERNAL_MARKERS = (
    "[WA_DRAFT]|",
    "[WA_SENT]|",
    "[KB_DRAFT]|",
    "[KB_REMOVE_DRAFT]|",
    "[BROADCAST_DRAFT]|",
    "[TPL_DRAFT]|",
    "[CHATTER_CTX]",
    "[SUPER_SESSION]|",
)


# ---------------------------------------------------------------------------
# Modelos de entrada
# ---------------------------------------------------------------------------
class SuperintendenteContext(BaseModel):
    owner_id: Optional[int | str] = Field(
        default=None,
        description="ID del encargado/owner (identidad interna)",
    )
    encargado_id: Optional[int | str] = Field(
        default=None,
        description="ID legado del encargado (compatibilidad temporal)",
    )
    hotel_name: str = Field(..., description="Nombre del hotel en contexto")
    session_id: Optional[str] = Field(default=None, description="ID de sesión del superintendente")
    property_id: Optional[int | str] = Field(
        default=None,
        description="ID de property (numérico o string, opcional)",
    )


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


def _persist_session_title_db(
    state: Any,
    *,
    conversation_id: str,
    owner_key: str,
    title: str,
) -> None:
    clean_convo = str(conversation_id or "").strip()
    clean_owner = str(owner_key or "").strip()
    clean_title = str(title or "").strip()
    if not clean_convo or not clean_owner or not clean_title:
        return
    try:
        (
            state.supabase_client.table(Settings.SUPERINTENDENTE_HISTORY_TABLE)
            .update({"session_title": clean_title})
            .eq("conversation_id", clean_convo)
            .eq("original_chat_id", clean_owner)
            .execute()
        )
    except Exception as exc:
        # Si la columna aún no existe en DB, no rompemos el flujo.
        log.debug("No se pudo persistir session_title en DB: %s", exc)


def _is_generic_session_title(title: Optional[str]) -> bool:
    text = re.sub(r"\s+", " ", str(title or "").strip().lower())
    if not text:
        return True
    if text in {"chat", "nueva conversación", "nueva conversacion"}:
        return True
    return text.startswith("chat ") or text.startswith("nueva conversación ") or text.startswith("nueva conversacion ")


def _is_internal_super_message(content: str) -> bool:
    text = str(content or "").strip()
    if not text:
        return True
    if not text.startswith("["):
        return False
    internal_prefixes = (
        "[SUPER_SESSION]|",
        "[CTX]",
        "[CHATTER_CTX]",
        "[WA_DRAFT]|",
        "[WA_SENT]|",
        "[KB_DRAFT]|",
        "[KB_REMOVE_DRAFT]|",
        "[BROADCAST_DRAFT]|",
        "[TPL_DRAFT]|",
    )
    return text.startswith(internal_prefixes)


def _render_internal_super_message(content: str) -> Optional[str]:
    text = str(content or "").strip()
    if not text:
        return None
    if text.startswith("[WA_DRAFT]|"):
        drafts = _parse_wa_drafts(text)
        if drafts:
            return _format_wa_preview(drafts)
        return None
    if text.startswith("[WA_SENT]|"):
        parts = text.split("|", 2)
        guest_id = _normalize_guest_id(parts[1] if len(parts) > 1 else "")
        if guest_id:
            return f"✅ Mensaje enviado al huésped: {guest_id}"
        return "✅ Mensaje enviado al huésped."
    return None


def _sanitize_generated_title(raw: str) -> Optional[str]:
    text = re.sub(r"\s+", " ", str(raw or "").strip())
    if not text:
        return None
    text = text.splitlines()[0].strip().strip("\"'`")
    text = re.sub(r"^[\-\d\.\)\s]+", "", text).strip()
    text = re.sub(r"[.!?]+$", "", text).strip()
    if not text:
        return None
    words = text.split()
    if len(words) > 8:
        text = " ".join(words[:8]).strip()
    if len(text) > 70:
        text = text[:70].rsplit(" ", 1)[0].strip() or text[:70].strip()
    if _is_generic_session_title(text):
        return None
    return text


def _fallback_title_from_seed(seed: str) -> str:
    text = re.sub(r"\s+", " ", str(seed or "").strip())
    text = text.strip("\"'`")
    if not text:
        return "Conversación"
    text = re.split(r"[.!?\n]", text, maxsplit=1)[0].strip() or text
    words = text.split()
    if len(words) > 6:
        text = " ".join(words[:6]).strip()
    if len(text) > 64:
        text = text[:64].rsplit(" ", 1)[0].strip() or text[:64].strip()
    return text or "Conversación"


async def _generate_session_title_with_ai(
    llm: Any,
    *,
    user_seed: str,
    assistant_seed: Optional[str] = None,
    hotel_name: Optional[str] = None,
) -> Optional[str]:
    if not llm:
        return None

    prompt = (
        "Genera un titulo corto para una conversación de gestión hotelera.\n"
        "Reglas:\n"
        "- 2 a 6 palabras.\n"
        "- En español.\n"
        "- Sin comillas, sin emojis, sin punto final.\n"
        "- Debe describir el tema principal.\n\n"
        f"Hotel: {hotel_name or 'N/A'}\n"
        f"Mensaje clave del encargado: {user_seed}\n"
        f"Respuesta/acción relevante: {assistant_seed or 'N/A'}\n\n"
        "Devuelve solo el titulo."
    )
    try:
        response = await llm.ainvoke(
            [
                {
                    "role": "system",
                    "content": "Eres un asistente que nombra conversaciones operativas con titulos concretos.",
                },
                {"role": "user", "content": prompt},
            ]
        )
        return _sanitize_generated_title(getattr(response, "content", None) or "")
    except Exception as exc:
        log.debug("No se pudo generar título de sesión con IA: %s", exc)
        return None


def _resolve_owner_id(payload: SuperintendenteContext) -> str:
    owner = str(payload.owner_id).strip() if payload.owner_id is not None else ""
    if owner:
        return owner
    legacy = str(payload.encargado_id).strip() if payload.encargado_id is not None else ""
    if legacy:
        return legacy
    raise HTTPException(status_code=422, detail="owner_id requerido")


def _normalize_property_id(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _resolve_owner_key(payload: SuperintendenteContext) -> tuple[str, str, Optional[str]]:
    owner_id = _resolve_owner_id(payload)
    property_id = _normalize_property_id(payload.property_id)
    if not property_id:
        return owner_id, owner_id, None
    return f"{owner_id}:{property_id}", owner_id, property_id


def _normalize_guest_id(guest_id: str | None) -> str:
    return str(guest_id or "").replace("+", "").strip()


def _extract_candidate_chat_id_from_payload(chat_history: Optional[list[Any]]) -> Optional[str]:
    if not isinstance(chat_history, list):
        return None
    for item in chat_history:
        if not isinstance(item, dict):
            continue
        for key in ("chat_id", "conversation_id", "guest_chat_id", "client_phone", "phone"):
            val = item.get(key)
            if val in (None, ""):
                continue
            raw = str(val).strip()
            if not raw:
                continue
            if ":" in raw:
                raw = raw.split(":")[-1].strip()
            clean = _normalize_guest_id(raw)
            if clean:
                return clean
    return None


def _collect_dynamic_memory_flags(state: Any, *keys: str) -> dict[str, Any]:
    memory = getattr(state, "memory_manager", None)
    if not memory:
        return {}

    out: dict[str, Any] = {}

    def _is_context_value(value: Any) -> bool:
        if value in (None, "", [], {}):
            return False
        if isinstance(value, (bool, int, float)):
            return True
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return False
            return len(text) <= 120
        return False

    candidate_ids = {str(k).strip() for k in keys if k}
    state_flags = getattr(memory, "state_flags", {})
    if isinstance(state_flags, dict):
        for cid in list(candidate_ids):
            if not isinstance(cid, str):
                continue
            suffix = f":{cid}"
            for key in list(state_flags.keys()):
                if isinstance(key, str) and (key == cid or key.endswith(suffix)):
                    candidate_ids.add(key)

        for cid in candidate_ids:
            flags = state_flags.get(cid) if isinstance(cid, str) else None
            if not isinstance(flags, dict):
                continue
            for key, value in flags.items():
                if key in out:
                    continue
                if _is_context_value(value):
                    out[key] = value
    return out


def _build_chatter_context_block(
    state: Any,
    *,
    message: str,
    session_key: str,
    alt_key: Optional[str],
    owner_key: str,
    owner_id: str,
    property_id: Optional[str],
    hotel_name: str,
    chat_history: Optional[list[Any]],
) -> str:
    context: dict[str, Any] = {}

    def _is_context_value(value: Any) -> bool:
        if value in (None, "", [], {}):
            return False
        if isinstance(value, (bool, int, float)):
            return True
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return False
            return len(text) <= 120
        return False

    context["owner_id"] = owner_id
    context["session_id"] = session_key
    context["channel"] = "whatsapp"
    context["hotel_name"] = hotel_name
    if property_id:
        context["property_id"] = property_id

    candidate_chat_id = None
    for source in (
        _extract_candidate_chat_id_from_payload(chat_history),
        _normalize_guest_id(message.split()[-1]) if re.search(r"\d{7,}", message or "") else None,
    ):
        if source:
            candidate_chat_id = source
            break

    memory = getattr(state, "memory_manager", None)
    if memory and not candidate_chat_id:
        try:
            candidate_chat_id = (
                memory.get_flag(session_key, "guest_chat_id")
                or memory.get_flag(session_key, "last_guest_chat_id")
                or memory.get_flag(session_key, "client_phone")
                or memory.get_flag(owner_key, "guest_chat_id")
                or memory.get_flag(owner_id, "guest_chat_id")
            )
            candidate_chat_id = _normalize_guest_id(str(candidate_chat_id or ""))
        except Exception:
            candidate_chat_id = None

    if candidate_chat_id:
        context["chat_id"] = candidate_chat_id
        context["client_phone"] = candidate_chat_id

        try:
            active = get_active_chat_reservation(
                chat_id=candidate_chat_id,
                property_id=property_id,
            )
        except Exception:
            active = None
        if isinstance(active, dict):
            for key, value in active.items():
                if value not in (None, "", [], {}):
                    context[key] = value
            if active.get("folio_id") and "reservation_id" not in context:
                context["reservation_id"] = active.get("folio_id")

        try:
            bookai_flags = getattr(state, "tracking", {}).get("bookai_enabled", {}) or {}
            enabled = bookai_flags.get(
                f"{candidate_chat_id}:{property_id}" if property_id else "",
                bookai_flags.get(candidate_chat_id, True),
            )
            context["bookai_enabled"] = bool(enabled)
        except Exception:
            context["bookai_enabled"] = True
        context.setdefault("unread_count", 0)

    dynamic_flags = _collect_dynamic_memory_flags(
        state,
        session_key,
        alt_key or "",
        owner_key,
        owner_id,
        str(candidate_chat_id or ""),
    )
    for key, value in dynamic_flags.items():
        if key in context:
            continue
        context[key] = value

    if isinstance(chat_history, list) and chat_history:
        for item in reversed(chat_history):
            if not isinstance(item, dict):
                continue
            for key, value in item.items():
                if key in context:
                    continue
                if _is_context_value(value):
                    context[key] = value
            ts = item.get("last_message_at") or item.get("created_at") or item.get("timestamp")
            if ts:
                context.setdefault("last_message_at", ts)
                break

    if not context:
        return ""

    lines = [
        f"- {k}: {v}"
        for k, v in sorted(context.items(), key=lambda it: str(it[0]))
        if _is_context_value(v)
    ]
    return "\n".join(lines)


def _parse_chatter_context_block(block: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in str(block or "").splitlines():
        raw = line.strip()
        if not raw.startswith("- "):
            continue
        content = raw[2:]
        if ":" not in content:
            continue
        key, value = content.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key and value:
            out[key] = value
    return out


def _is_valid_folio_id(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text) and bool(re.fullmatch(r"(?=.*\d)[A-Za-z0-9]{4,}", text))


def _extract_folio_id_from_text(text: str) -> Optional[str]:
    raw = str(text or "").strip()
    if not raw:
        return None
    patterns = [
        r"\bfolio(?:_id)?\s*[:#]?\s*([A-Za-z0-9]{4,})\b",
        r"\breserva(?:\s*id)?\s*[:#]?\s*([A-Za-z0-9]{4,})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw, flags=re.IGNORECASE)
        if not match:
            continue
        candidate = str(match.group(1) or "").strip()
        if _is_valid_folio_id(candidate):
            return candidate
    return None


def _resolve_fastpath_folio_id(
    state: Any,
    *,
    message: str,
    chatter_context_block: str,
    session_key: str,
    alt_key: Optional[str],
    owner_key: str,
    owner_id: str,
    property_id: Optional[str],
) -> Optional[str]:
    ctx = _parse_chatter_context_block(chatter_context_block)
    for key in ("folio_id", "reservation_id"):
        val = ctx.get(key)
        if _is_valid_folio_id(val):
            return str(val).strip()

    inline_folio = _extract_folio_id_from_text(message)
    if inline_folio:
        return inline_folio

    memory = getattr(state, "memory_manager", None)
    if not memory:
        return None

    candidate_chat_id = _normalize_guest_id(
        str(
            memory.get_flag(session_key, "guest_chat_id")
            or memory.get_flag(session_key, "last_guest_chat_id")
            or memory.get_flag(session_key, "client_phone")
            or memory.get_flag(owner_key, "guest_chat_id")
            or memory.get_flag(owner_id, "guest_chat_id")
            or ""
        )
    )
    if candidate_chat_id:
        try:
            active = get_active_chat_reservation(chat_id=candidate_chat_id, property_id=property_id)
        except Exception:
            active = None
        if isinstance(active, dict):
            for key in ("folio_id", "reservation_id"):
                val = active.get(key)
                if _is_valid_folio_id(val):
                    return str(val).strip()

    flag_keys = [session_key, alt_key, owner_key, owner_id]
    for key in [k for k in flag_keys if k]:
        for flag in ("folio_id", "reservation_id", "origin_folio_id"):
            val = memory.get_flag(key, flag)
            if _is_valid_folio_id(val):
                return str(val).strip()

    for key in [k for k in flag_keys if k]:
        try:
            msgs = memory.get_memory_as_messages(key, limit=20) or []
        except Exception:
            msgs = []
        for msg in reversed(msgs):
            content = str(getattr(msg, "content", "") or "").strip()
            if not content:
                continue
            if content.startswith("[CHATTER_CTX]"):
                block = content.split("\n", 1)[1] if "\n" in content else ""
                parsed = _parse_chatter_context_block(block)
                for field in ("folio_id", "reservation_id"):
                    val = parsed.get(field)
                    if _is_valid_folio_id(val):
                        return str(val).strip()
            for key_name in ("folio_id", "reservation_id", "folio"):
                m = re.search(rf"\b{key_name}\s*[:=]\s*([A-Za-z0-9]{{4,}})\b", content, flags=re.IGNORECASE)
                if not m:
                    continue
                val = str(m.group(1) or "").strip()
                if _is_valid_folio_id(val):
                    return val

    return None


async def _should_use_reservation_fastpath_with_llm(
    text: str,
    *,
    chatter_context_block: str,
    folio_id: str,
) -> bool:
    """
    Decide semánticamente si conviene usar fast-path de detalle de reserva.
    No usa matching por keywords para ser escalable.
    """
    raw = str(text or "").strip()
    folio = str(folio_id or "").strip()
    if not raw or not _is_valid_folio_id(folio):
        return False

    llm = ModelConfig.get_llm(ModelTier.INTERNAL)
    prompt = (
        "Analiza el mensaje del encargado y decide si solicita información/acción sobre UNA reserva concreta.\n"
        "Responde SOLO JSON con este esquema exacto:\n"
        "{"
        "\"use_fastpath\":true|false,"
        "\"confidence\":0.0,"
        "\"reason\":\"string\""
        "}\n\n"
        "Reglas:\n"
        "- Hay folio_id ya resuelto: prioriza use_fastpath=true por defecto.\n"
        "- use_fastpath=true si el mensaje pide consultar, resumir o gestionar datos de la reserva concreta del huésped.\n"
        "- use_fastpath=false SOLO si el mensaje es claramente para enviar un WhatsApp al huésped, crear una reserva nueva o tarea no relacionada con consultar esa reserva.\n"
        "- Debes usar interpretación semántica, no coincidencia literal de palabras.\n\n"
        "Ejemplos:\n"
        "1) 'quiero más info de rafalillo' con folio resuelto -> use_fastpath=true\n"
        "2) 'dile a rafalillo que pague' -> use_fastpath=false\n\n"
        f"folio_id_resuelto:\n{folio}\n\n"
        f"contexto_chatter:\n{(chatter_context_block or '').strip()}\n\n"
        f"mensaje:\n{raw}"
    )
    try:
        response = await llm.ainvoke(
            [
                {"role": "system", "content": "Eres un clasificador semántico operativo hotelero."},
                {"role": "user", "content": prompt},
            ]
        )
        data = _extract_json_object((getattr(response, "content", None) or "").strip()) or {}
    except Exception:
        data = {}

    use_fastpath = bool(data.get("use_fastpath")) if isinstance(data, dict) else False
    confidence = _safe_float(data.get("confidence"), 0.0) if isinstance(data, dict) else 0.0
    return use_fastpath and confidence >= 0.45


def _format_reservation_detail_response(detail: dict[str, Any]) -> str:
    partner_name = detail.get("partner_name") or "Huésped"
    folio_id = detail.get("folio_id") or "-"
    folio_code = detail.get("folio_code") or "-"
    checkin = detail.get("checkin") or "-"
    checkout = detail.get("checkout") or "-"
    state = detail.get("state") or "-"
    total = detail.get("amount_total") if detail.get("amount_total") not in (None, "") else "-"
    pending = detail.get("pending_amount") if detail.get("pending_amount") not in (None, "") else "-"
    phone = detail.get("partner_phone") or "-"
    email = detail.get("partner_email") or "-"
    portal_url = detail.get("portal_url")

    line = (
        f"Nombre: {partner_name} | Folio ID: {folio_id} | Código: {folio_code} | "
        f"Check-in: {checkin} | Check-out: {checkout} | Estado: {state} | "
        f"Total: {total} | Pendiente: {pending} | Tel: {phone} | Email: {email}"
    )
    if portal_url:
        return f"Aquí tienes la información detallada de la reserva:\n\n{line}\n\nPortal: {portal_url}"
    return f"Aquí tienes la información detallada de la reserva:\n\n{line}"


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


async def _classify_offer_state_from_wa_message(llm: Any, message: str) -> dict[str, Any]:
    text = (message or "").strip()
    if not llm or not text:
        return {"action": "none", "confidence": 0.0}
    prompt = (
        "Analiza un mensaje saliente de WhatsApp enviado por un hotel al huésped.\n"
        "Devuelve JSON con este esquema exacto:\n"
        "{"
        "\"action\":\"set_pending|clear_pending|none\","
        "\"is_offer\":true|false,"
        "\"offer_type\":\"string\","
        "\"has_actionable_details\":true|false,"
        "\"missing_fields\":[\"schedule|location|booking_method|conditions\"],"
        "\"confidence\":0.0"
        "}\n\n"
        "Reglas:\n"
        "- set_pending: promete una cortesía/beneficio pero faltan datos operativos para ejecutarlo.\n"
        "- clear_pending: aporta datos operativos suficientes para una oferta pendiente previa.\n"
        "- none: no aplica a una oferta pendiente.\n"
        "- Usa semántica, no coincidencia literal de palabras.\n"
        "- Responde solo JSON válido.\n\n"
        f"Mensaje:\n{text}"
    )
    try:
        response = await llm.ainvoke(
            [
                {"role": "system", "content": "Eres un clasificador semántico de operaciones hoteleras."},
                {"role": "user", "content": prompt},
            ]
        )
        data = _extract_json_object((getattr(response, "content", None) or "").strip()) or {}
    except Exception as exc:
        log.debug("No se pudo clasificar estado de oferta WA: %s", exc)
        data = {}

    action = str(data.get("action") or "none").strip().lower()
    if action not in {"set_pending", "clear_pending", "none"}:
        action = "none"
    missing_fields = data.get("missing_fields")
    if not isinstance(missing_fields, list):
        missing_fields = []
    missing_fields = [str(x).strip() for x in missing_fields if str(x).strip()]
    return {
        "action": action,
        "is_offer": bool(data.get("is_offer")),
        "offer_type": str(data.get("offer_type") or "").strip() or "unspecified_offer",
        "has_actionable_details": bool(data.get("has_actionable_details")),
        "missing_fields": missing_fields,
        "confidence": _safe_float(data.get("confidence"), 0.0),
    }


async def _sync_guest_offer_state_from_sent_wa(
    state: Any,
    *,
    guest_id: str,
    sent_message: str,
    owner_id: str,
    session_id: str,
    property_id: Optional[str] = None,
) -> None:
    memory = getattr(state, "memory_manager", None)
    clean_guest = _normalize_guest_id(guest_id)
    if not memory or not clean_guest:
        return
    try:
        llm = getattr(getattr(state, "superintendente_agent", None), "llm", None) or ModelConfig.get_llm(ModelTier.INTERNAL)
        sem = await _classify_offer_state_from_wa_message(llm, sent_message)
    except Exception:
        return

    action = sem.get("action")
    confidence = _safe_float(sem.get("confidence"), 0.0)
    if action == "set_pending" and confidence >= 0.65:
        now = datetime.utcnow()
        payload = {
            "type": sem.get("offer_type") or "unspecified_offer",
            "source": "superintendente",
            "details_missing": True,
            "missing_fields": sem.get("missing_fields") or [],
            "original_text": (sent_message or "").strip(),
            "owner_id": str(owner_id or "").strip() or None,
            "session_id": str(session_id or "").strip() or None,
            "property_id": property_id,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=24)).isoformat(),
        }
        try:
            memory.set_flag(clean_guest, "super_offer_pending", payload)
            if guest_id != clean_guest:
                memory.set_flag(guest_id, "super_offer_pending", payload)
            memory.set_flag(clean_guest, "super_offer_pending_offer_type", payload["type"])
        except Exception:
            pass
    elif action == "clear_pending" and confidence >= 0.60:
        try:
            memory.clear_flag(clean_guest, "super_offer_pending")
            memory.clear_flag(clean_guest, "super_offer_pending_offer_type")
            if guest_id != clean_guest:
                memory.clear_flag(guest_id, "super_offer_pending")
                memory.clear_flag(guest_id, "super_offer_pending_offer_type")
        except Exception:
            pass


def _normalize_super_role(raw_role: Any) -> str:
    role = str(raw_role or "").strip().lower()
    if role in {"assistant", "bookai", "system", "tool"}:
        return "assistant"
    if role in {"user", "guest"}:
        return "user"
    return "assistant"


def _normalize_super_sender(raw_role: Any) -> str:
    role = str(raw_role or "").strip().lower()
    if role in {"assistant", "bookai", "system", "tool"}:
        return "bookai"
    if role in {"user", "guest"}:
        return "guest"
    return "bookai"


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
    return looks_like_new_instruction(text)


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


def _pull_recent_reservations(state, *keys: str, max_age_seconds: int = 180) -> Optional[dict]:
    if not getattr(state, "memory_manager", None):
        return None
    for key in [k for k in keys if k]:
        try:
            payload = state.memory_manager.get_flag(key, "superintendente_last_reservations")
        except Exception:
            payload = None
        if not isinstance(payload, dict):
            continue
        stored_at = payload.get("stored_at")
        if stored_at:
            try:
                ts = datetime.fromisoformat(str(stored_at).replace("Z", ""))
                if (datetime.utcnow() - ts).total_seconds() > max_age_seconds:
                    continue
            except Exception:
                continue
        return payload
    return None


def _csv_escape(value: Any) -> str:
    text = "" if value is None else str(value)
    if any(ch in text for ch in [';', '\n', '"']):
        return '"' + text.replace('"', '""') + '"'
    return text


def _build_reservations_csv(payload: dict) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return None
    header = [
        "nombre",
        "folio_id",
        "codigo",
        "checkin",
        "checkout",
        "estado",
        "total",
        "pendiente",
        "tel",
        "email",
    ]
    lines = [";".join(header)]
    for item in items:
        if not isinstance(item, dict):
            continue
        row = [
            item.get("partner_name"),
            item.get("folio_id"),
            item.get("folio_code"),
            item.get("checkin"),
            item.get("checkout"),
            item.get("state"),
            item.get("amount_total"),
            item.get("pending_amount"),
            item.get("partner_phone"),
            item.get("partner_email"),
        ]
        lines.append(";".join(_csv_escape(v) for v in row))
    return "\n".join(lines)


def _pull_recent_reservation_detail(state, *keys: str, max_age_seconds: int = 180) -> Optional[dict]:
    if not getattr(state, "memory_manager", None):
        return None
    for key in [k for k in keys if k]:
        try:
            payload = state.memory_manager.get_flag(key, "superintendente_last_reservation_detail")
        except Exception:
            payload = None
        if not isinstance(payload, dict):
            continue
        stored_at = payload.get("stored_at")
        if stored_at:
            try:
                ts = datetime.fromisoformat(str(stored_at).replace("Z", ""))
                if (datetime.utcnow() - ts).total_seconds() > max_age_seconds:
                    continue
            except Exception:
                continue
        return payload
    return None


def _normalize_reservation_detail(payload: dict) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    detail = payload.get("detail") if isinstance(payload.get("detail"), dict) else payload
    if not isinstance(detail, dict):
        return None
    reservations = detail.get("reservations") or detail.get("reservation") or []
    first_res = reservations[0] if isinstance(reservations, list) and reservations else {}
    checkin = (first_res.get("checkin") or detail.get("firstCheckin") or detail.get("checkin") or "")
    checkout = (first_res.get("checkout") or detail.get("lastCheckout") or detail.get("checkout") or "")
    return {
        "folio_id": detail.get("id") or detail.get("folio_id") or detail.get("folio"),
        "folio_code": detail.get("name") or detail.get("folio_code"),
        "partner_name": detail.get("partnerName") or detail.get("partner_name"),
        "partner_phone": detail.get("partnerPhone") or detail.get("partner_phone"),
        "partner_email": detail.get("partnerEmail") or detail.get("partner_email"),
        "state": detail.get("state") or detail.get("stateCode"),
        "amount_total": detail.get("amountTotal"),
        "pending_amount": detail.get("pendingAmount"),
        "payment_state": detail.get("paymentStateDescription") or detail.get("paymentStateCode"),
        "checkin": str(checkin).split("T")[0] if checkin else "",
        "checkout": str(checkout).split("T")[0] if checkout else "",
        "portal_url": detail.get("portalUrl") or detail.get("portal_url"),
    }


def _build_reservation_detail_csv(detail: dict) -> Optional[str]:
    if not isinstance(detail, dict):
        return None
    header = [
        "folio_id",
        "codigo",
        "nombre",
        "checkin",
        "checkout",
        "estado",
        "total",
        "pendiente",
        "tel",
        "email",
        "portal_url",
    ]
    row = [
        detail.get("folio_id"),
        detail.get("folio_code"),
        detail.get("partner_name"),
        detail.get("checkin"),
        detail.get("checkout"),
        detail.get("state"),
        detail.get("amount_total"),
        detail.get("pending_amount"),
        detail.get("partner_phone"),
        detail.get("partner_email"),
        detail.get("portal_url"),
    ]
    return ";".join(header) + "\n" + ";".join(_csv_escape(v) for v in row)


def _extract_detail_from_text(text: str) -> Optional[dict]:
    if not text:
        return None
    if "Folio ID:" not in text:
        return None

    def _clean(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        v = str(value).strip()
        if not v:
            return None
        v = re.sub(r"^\*+|\*+$", "", v).strip()
        return v or None

    def _m(pattern: str) -> Optional[str]:
        m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        return _clean(m.group(1)) if m else None

    # Preferir siempre el formato etiquetado (multilínea).
    name = _m(r"^\s*Nombre:\s*(.+)$")
    if not name:
        m_title = re.search(r"detalle de la reserva de\s+([^:\n]+)", text, re.IGNORECASE)
        if m_title:
            name = _clean(m_title.group(1))
    # Fallback para formato en una sola línea con pipes.
    if not name and "| Folio ID:" in text:
        try:
            name = _clean(text.split("| Folio ID:", 1)[0].strip())
        except Exception:
            name = None

    return {
        "folio_id": _m(r"Folio ID:\s*([A-Za-z0-9]+)"),
        "folio_code": _m(r"Código:\s*([A-Za-z0-9/\\-]+)"),
        "partner_name": name,
        "partner_phone": _m(r"Tel:\s*([^\n|]+)"),
        "partner_email": _m(r"Email:\s*([^\n|]+)"),
        "state": _m(r"Estado:\s*([^\n|]+)"),
        "amount_total": _m(r"Total:\s*([0-9]+(?:[.,][0-9]+)?)"),
        "pending_amount": _m(r"Pendiente:\s*([0-9]+(?:[.,][0-9]+)?)"),
        "checkin": _m(r"Check-in:\s*([^\n|]+)"),
        "checkout": _m(r"Check-out:\s*([^\n|]+)"),
        "portal_url": _m(r"(https?://\S+)"),
    }


def _extract_reservations_from_text(text: str) -> Optional[dict]:
    if not text or "Folio ID:" not in text or text.count("Folio ID:") < 2:
        return None

    def _pick(raw: str, label: str) -> Optional[str]:
        m = re.search(rf"{label}\s*:\s*([^|\n]+)", raw, re.IGNORECASE)
        return m.group(1).strip() if m else None

    items: list[dict[str, Any]] = []
    for line in str(text).splitlines():
        raw = line.strip()
        if "Folio ID:" not in raw or "|" not in raw:
            continue
        left = raw.split("|", 1)[0].strip()
        if not left or left.lower().startswith(("aquí tienes", "estas son")):
            continue
        item = {
            "partner_name": left,
            "folio_id": _pick(raw, "Folio ID"),
            "folio_code": _pick(raw, "Código"),
            "checkin": _pick(raw, "Check-in"),
            "checkout": _pick(raw, "Check-out"),
            "state": _pick(raw, "Estado"),
            "amount_total": _pick(raw, "Total"),
            "pending_amount": _pick(raw, "Pendiente"),
            "partner_phone": _pick(raw, "Tel"),
            "partner_email": _pick(raw, "Email"),
        }
        if item["folio_id"]:
            items.append(item)

    if not items:
        return None
    return {
        "kind": "reservations",
        "data": {"items": items},
        "csv": None,
        "csv_delimiter": ";",
    }


def _ensure_guest_language(msg: str, guest_id: str) -> str:
    return _ensure_guest_language_with_target(msg, guest_id, target_lang=None)


def _normalize_target_lang(target_lang: Optional[str]) -> Optional[str]:
    raw = (target_lang or "").strip().lower()
    if not raw:
        return None
    if raw == "guest":
        return "guest"
    if re.fullmatch(r"[a-z]{2}", raw):
        return raw
    return None


async def _detect_language_adjustment_with_llm(text: str) -> dict[str, Any]:
    """
    Interpreta semánticamente si el encargado está pidiendo un cambio de idioma
    del borrador WA. Devuelve:
      { "target_lang": "guest|en|es|..|None", "language_only": bool }
    """
    raw = (text or "").strip()
    if not raw:
        return {"target_lang": None, "language_only": False}
    llm = ModelConfig.get_llm(ModelTier.INTERNAL)
    prompt = (
        "Analiza si el mensaje del encargado contiene una instrucción de idioma para el borrador WA.\n"
        "Responde SOLO JSON con este esquema exacto:\n"
        "{"
        "\"target_lang\":\"guest|es|en|fr|de|it|pt|nl|none\","
        "\"language_only\":true|false"
        "}\n\n"
        "Reglas:\n"
        "- target_lang=guest cuando pida 'en su idioma' o equivalente.\n"
        "- target_lang=none si NO hay instrucción de idioma.\n"
        "- language_only=true si el mensaje solo pide cambio de idioma sin cambios de contenido.\n"
        "- Usa interpretación semántica multilingüe, no keywords literales.\n\n"
        f"Mensaje:\n{raw}"
    )
    try:
        response = await llm.ainvoke(
            [
                {"role": "system", "content": "Eres un clasificador semántico de instrucciones de edición."},
                {"role": "user", "content": prompt},
            ]
        )
        data = _extract_json_object((getattr(response, "content", None) or "").strip()) or {}
    except Exception:
        data = {}

    target = _normalize_target_lang(data.get("target_lang")) if data else None
    if target == "none":
        target = None
    language_only = bool(data.get("language_only", False)) if isinstance(data, dict) else False
    return {"target_lang": target, "language_only": language_only}


def _ensure_guest_language_with_target(msg: str, guest_id: str, target_lang: Optional[str] = None) -> str:
    if not msg:
        return msg
    state = _SUPER_STATE
    memory = getattr(state, "memory_manager", None) if state else None

    clean_guest = _normalize_guest_id(guest_id)
    keys = [k for k in (clean_guest, f"+{clean_guest}" if clean_guest else "", str(guest_id or "").strip()) if k]

    lang = None
    source_key = None
    for key in keys:
        try:
            value = memory.get_flag(key, "guest_lang")
        except Exception:
            value = None
        if value:
            lang = str(value).strip().lower()
            source_key = key
            break

    if memory and not lang:
        sample = None
        for key in keys:
            try:
                history = memory.get_memory(key, limit=20) or []
            except Exception:
                history = []
            for entry in reversed(history):
                role = str(entry.get("role") or "").lower()
                if role not in {"guest", "user"}:
                    continue
                content = str(entry.get("content") or "").strip()
                if content:
                    sample = content
                    source_key = key
                    break
            if sample:
                break
        if sample:
            try:
                lang = language_manager.detect_language(sample, prev_lang=None)
            except Exception:
                lang = None

    lang = (lang or "es").strip().lower() or "es"
    if memory and source_key:
        try:
            memory.set_flag(source_key, "guest_lang", lang)
            if clean_guest and clean_guest != source_key:
                memory.set_flag(clean_guest, "guest_lang", lang)
        except Exception:
            pass

    normalized_target = _normalize_target_lang(target_lang)
    if target_lang == "guest":
        normalized_target = lang
    if normalized_target:
        lang = normalized_target

    if lang == "es":
        return msg
    try:
        return language_manager.ensure_language(msg, lang)
    except Exception:
        return msg


def _ensure_owner_language(msg: str, owner_lang: Optional[str]) -> str:
    if not msg:
        return msg
    if any(marker in msg for marker in _INTERNAL_MARKERS):
        return msg
    lang = (owner_lang or "es").strip().lower() or "es"
    if lang == "es":
        return msg
    try:
        return language_manager.ensure_language(msg, lang)
    except Exception:
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
        if payload is None:
            store.pop(str(key), None)
        else:
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


def _persist_last_pending_wa(state, owner_id: str, payload: Any) -> None:
    if not state or not owner_id:
        return
    try:
        store = state.tracking.setdefault("superintendente_last_pending_wa", {})
        if not isinstance(store, dict):
            state.tracking["superintendente_last_pending_wa"] = {}
            store = state.tracking["superintendente_last_pending_wa"]
        if payload is None:
            store.pop(str(owner_id), None)
        else:
            store[str(owner_id)] = payload
        state.save_tracking()
    except Exception:
        pass


def _load_last_pending_wa(state, owner_id: str) -> Any:
    if not state or not owner_id:
        return None
    try:
        store = state.tracking.get("superintendente_last_pending_wa", {})
        if isinstance(store, dict):
            return store.get(str(owner_id))
    except Exception:
        pass
    return None


def _parse_kb_draft_marker(raw_text: str) -> dict[str, str] | None:
    if not raw_text or "[KB_DRAFT]|" not in raw_text:
        return None
    marker = raw_text[raw_text.index("[KB_DRAFT]|") :]
    parts = marker.split("|", 4)
    if len(parts) < 5:
        return None
    _, hotel_name, topic, category, content = parts[:5]
    return {
        "hotel_name": hotel_name.strip(),
        "topic": topic.strip(),
        "category": category.strip(),
        "content": content.strip(),
    }


def _parse_kb_remove_draft_marker(raw_text: str) -> dict[str, Any] | None:
    if not raw_text or "[KB_REMOVE_DRAFT]|" not in raw_text:
        return None
    marker = raw_text[raw_text.index("[KB_REMOVE_DRAFT]|") :]
    parts = marker.split("|", 2)
    if len(parts) < 3:
        return None
    _, hotel_name, payload_raw = parts[:3]
    try:
        payload = json.loads(payload_raw)
    except Exception:
        payload = None
    if not payload or not isinstance(payload, dict):
        return None
    return {"hotel_name": hotel_name.strip(), "payload": payload}


def _persist_pending_kb(state, key: str, payload: Any) -> None:
    if not state or not key:
        return
    try:
        store = state.tracking.setdefault("superintendente_pending_kb", {})
        if not isinstance(store, dict):
            state.tracking["superintendente_pending_kb"] = {}
            store = state.tracking["superintendente_pending_kb"]
        if payload is None:
            store.pop(str(key), None)
        else:
            store[str(key)] = payload
        state.save_tracking()
    except Exception:
        pass


def _load_pending_kb(state, key: str) -> Any:
    if not state or not key:
        return None
    try:
        store = state.tracking.get("superintendente_pending_kb", {})
        if isinstance(store, dict):
            return store.get(str(key))
    except Exception:
        pass
    return None


def _record_pending_action(state, owner_id: str, action_type: str, payload: Any, session_id: str | None) -> None:
    if not state or not owner_id or not action_type:
        return
    try:
        store = state.tracking.setdefault("superintendente_pending_stack", {})
        if not isinstance(store, dict):
            state.tracking["superintendente_pending_stack"] = {}
            store = state.tracking["superintendente_pending_stack"]
        stack = store.get(str(owner_id))
        if not isinstance(stack, list):
            stack = []
        stack.append(
            {
                "type": action_type,
                "payload": payload,
                "session_id": session_id,
                "created_at": datetime.utcnow().isoformat(),
            }
        )
        if len(stack) > 20:
            stack = stack[-20:]
        store[str(owner_id)] = stack
        state.save_tracking()
    except Exception:
        pass


def _get_last_pending_action(state, owner_id: str) -> dict[str, Any] | None:
    if not state or not owner_id:
        return None
    try:
        store = state.tracking.get("superintendente_pending_stack", {})
        if isinstance(store, dict):
            stack = store.get(str(owner_id)) or []
            if isinstance(stack, list) and stack:
                return stack[-1]
    except Exception:
        pass
    return None


def _update_last_pending_action(state, owner_id: str, payload: Any) -> None:
    if not state or not owner_id:
        return
    try:
        store = state.tracking.get("superintendente_pending_stack", {})
        if not isinstance(store, dict):
            return
        stack = store.get(str(owner_id)) or []
        if not isinstance(stack, list) or not stack:
            return
        stack[-1]["payload"] = payload
        store[str(owner_id)] = stack
        state.save_tracking()
    except Exception:
        pass


def _pop_last_pending_action(state, owner_id: str) -> None:
    if not state or not owner_id:
        return
    try:
        store = state.tracking.get("superintendente_pending_stack", {})
        if not isinstance(store, dict):
            return
        stack = store.get(str(owner_id)) or []
        if not isinstance(stack, list) or not stack:
            return
        stack.pop()
        store[str(owner_id)] = stack
        state.save_tracking()
    except Exception:
        pass


def _pop_trailing_pending_type(state, owner_id: str, action_type: str) -> None:
    if not state or not owner_id or not action_type:
        return
    try:
        store = state.tracking.get("superintendente_pending_stack", {})
        if not isinstance(store, dict):
            return
        stack = store.get(str(owner_id)) or []
        if not isinstance(stack, list) or not stack:
            return
        while stack and stack[-1].get("type") == action_type:
            stack.pop()
        store[str(owner_id)] = stack
        state.save_tracking()
    except Exception:
        pass


def _is_short_confirmation(text: str) -> bool:
    clean = re.sub(r"[¡!¿?.]", "", (text or "").lower()).strip()
    tokens = [t for t in re.findall(r"[a-záéíóúñ]+", clean) if t]
    yes_words = {"ok", "okay", "okey", "si", "sí", "vale", "confirmo", "confirmar"}
    if clean in yes_words:
        return True
    return 0 < len(tokens) <= 2 and all(tok in yes_words for tok in tokens)


def _is_short_rejection(text: str) -> bool:
    clean = re.sub(r"[¡!¿?.]", "", (text or "").lower()).strip()
    tokens = [t for t in re.findall(r"[a-záéíóúñ]+", clean) if t]
    no_words = {"no", "cancelar", "cancelado", "descartar", "rechazar"}
    if clean in no_words:
        return True
    return 0 < len(tokens) <= 2 and all(tok in no_words for tok in tokens)


def _looks_like_kb_confirmation(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    confirm_terms = {
        "ok",
        "vale",
        "de acuerdo",
        "confirmo",
        "confirma",
        "confirmar",
        "guardalo",
        "guárdalo",
        "guardala",
        "guárdala",
        "agrega",
        "agregar",
        "añade",
        "anade",
        "añádelo",
        "añadelo",
        "añádela",
        "añadela",
        "guardar",
    }
    return any(term in low for term in confirm_terms)


def _looks_like_adjustment(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    adjustment_terms = {"ajusta", "modifica", "cambia", "mejora", "reformula", "refrasea"}
    return any(term in low for term in adjustment_terms)


def _looks_like_send_confirmation(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    send_terms = {"envia", "envía", "enviale", "envíale", "manda", "mandale", "mándale", "enviar"}
    confirm_hints = {"este", "último", "ultimo", "mensaje", "envialo", "envíalo", "mandalo", "mándalo"}
    return any(t in low for t in send_terms) and any(t in low for t in confirm_hints)


async def _classify_pending_action(text: str, pending_type: str) -> str:
    """
    Clasifica el mensaje del encargado cuando hay un borrador pendiente.
    Devuelve: "confirm" | "cancel" | "adjust" | "new".
    """
    if _is_short_wa_confirmation(text):
        return "confirm"
    if _is_short_wa_cancel(text):
        return "cancel"
    if _is_short_confirmation(text):
        return "confirm"
    if _is_short_rejection(text):
        return "cancel"

    llm = ModelConfig.get_llm(ModelTier.INTERNAL)
    system = (
        "Eres un clasificador. Solo responde con una palabra: "
        "confirm, cancel, adjust o new.\n"
        "Reglas: confirm=aprueba envío/guardar; cancel=descarta; "
        "adjust=quiere cambiar el borrador; new=es una solicitud nueva."
    )
    user = (
        f"Tipo de borrador pendiente: {pending_type}\n"
        f"Mensaje del encargado: {text}\n"
        "Respuesta:"
    )
    try:
        resp = llm.invoke([{"role": "system", "content": system}, {"role": "user", "content": user}])
        raw = (getattr(resp, "content", None) or "").strip().lower()
        if raw in {"confirm", "cancel", "adjust", "new"}:
            return raw
    except Exception:
        pass
    return "new"


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


def _format_kb_remove_preview(pending: dict) -> str:
    total = int(pending.get("total_matches", 0) or 0)
    preview = pending.get("preview") or []
    criteria = pending.get("criteria") or ""
    date_from = pending.get("date_from") or ""
    date_to = pending.get("date_to") or ""

    def _sanitize_preview_snippet(text: str) -> str:
        if not text:
            return ""
        lines = []
        for ln in str(text).splitlines():
            low = ln.lower()
            if "borrador para agregar" in low or "[kb_" in low or "[kb-" in low:
                continue
            lines.append(ln.strip())
        cleaned = " ".join(l for l in lines if l).strip()
        return cleaned[:320] + ("..." if len(cleaned) > 320 else "")

    header = [f"🧹 Borrador para eliminar de la KB ({total} registro(s))."]
    if criteria:
        header.append(f"Criterio: {criteria}")
    if date_from or date_to:
        header.append(f"Rango: {date_from or 'n/a'} -> {date_to or 'n/a'}")

    body_lines = []
    for item in preview:
        topic = item.get("topic") or "Entrada"
        fecha = item.get("fecha") or ""
        snippet = _sanitize_preview_snippet(item.get("snippet") or "")
        body_lines.append(f"- {fecha} {topic}: {snippet}")

    footer = (
        "\n✅ Responde 'ok' para eliminar estos registros.\n"
        "📝 Di qué conservar o ajusta el criterio para refinar.\n"
        "❌ Responde 'no' para cancelar."
    )

    if body_lines and total <= 12:
        return "\n".join(header + body_lines) + footer
    return "\n".join(header) + footer


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
        "Genera un único mensaje corto de WhatsApp, tono cordial y directo. "
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


async def _expand_followup_with_context(
    llm: Any,
    message: str,
    recent_messages: list[Any],
) -> str:
    """Reformula follow-ups cortos usando el contexto reciente del chat."""
    raw = (message or "").strip()
    if not raw:
        return raw

    token_count = len(re.findall(r"\S+", raw))
    if token_count > 4:
        return raw

    compact: list[str] = []
    for msg in recent_messages[-8:]:
        role = ""
        content = ""
        if isinstance(msg, dict):
            role = str(msg.get("role") or "").strip().lower()
            content = str(msg.get("content") or "").strip()
        else:
            msg_type = str(getattr(msg, "type", "") or "").strip().lower()
            content = str(getattr(msg, "content", "") or "").strip()
            if msg_type in {"human"}:
                role = "user"
            elif msg_type in {"system"}:
                role = "system"
            else:
                role = "assistant"
        if not content:
            continue
        if role not in {"user", "assistant", "system"}:
            role = "assistant"
        compact.append(f"{role}: {content}")
    if not compact:
        return raw

    prompt = (
        "Dado el historial reciente, convierte el último mensaje del usuario en una instrucción completa "
        "solo si es un follow-up ambiguo (por ejemplo: 'resumen', 'original', 'sí', 'ese'). "
        "Si ya es claro por sí mismo, devuélvelo igual. "
        "No inventes nombres ni datos no presentes en historial.\n\n"
        "Historial:\n"
        f"{chr(10).join(compact)}\n\n"
        f"Último mensaje: {raw}\n\n"
        "Devuelve solo la instrucción final en una sola línea."
    )
    try:
        response = await llm.ainvoke(
            [
                {
                    "role": "system",
                    "content": (
                        "Eres un reescritor de intención para un chat operativo de hotel. "
                        "Tu salida debe ser solo una frase accionable."
                    ),
                },
                {"role": "user", "content": prompt},
            ]
        )
        rewritten = (getattr(response, "content", None) or "").strip()
        if not rewritten:
            return raw
        return rewritten
    except Exception as exc:
        log.debug("No se pudo expandir follow-up con contexto: %s", exc)
        return raw


# ---------------------------------------------------------------------------
# Registro de rutas
# ---------------------------------------------------------------------------
def register_superintendente_routes(app, state) -> None:
    global _SUPER_STATE
    _SUPER_STATE = state
    router = APIRouter(prefix="/api/v1/superintendente", tags=["superintendente"])

    @router.post("/ask")
    async def ask_superintendente(payload: AskSuperintendenteRequest, _: None = Depends(_verify_bearer)):
        agent = getattr(state, "superintendente_agent", None)
        if not agent:
            raise HTTPException(status_code=500, detail="Superintendente no disponible")

        owner_key, owner_id, property_id = _resolve_owner_key(payload)
        session_key = payload.session_id or owner_key
        alt_key = owner_key if payload.session_id else None
        message = (payload.message or "").strip()
        original_message = message
        chatter_context_block = _build_chatter_context_block(
            state,
            message=message,
            session_key=session_key,
            alt_key=alt_key,
            owner_key=owner_key,
            owner_id=owner_id,
            property_id=property_id,
            hotel_name=payload.hotel_name,
            chat_history=payload.chat_history,
        )

        def _persist_visible_super_response(reply_text: str, *, persist_user: bool = False) -> None:
            if not getattr(state, "memory_manager", None):
                return
            text = str(reply_text or "").strip()
            if not text:
                return
            try:
                if persist_user and original_message:
                    state.memory_manager.save(
                        conversation_id=session_key,
                        role="user",
                        content=original_message,
                        channel="telegram",
                        original_chat_id=owner_id,
                    )
                state.memory_manager.save(
                    conversation_id=session_key,
                    role="assistant",
                    content=text,
                    channel="telegram",
                    original_chat_id=owner_id,
                )
            except Exception:
                pass

        owner_lang = "es"
        if state.memory_manager:
            try:
                prev_owner_lang = state.memory_manager.get_flag(session_key, "owner_lang")
                owner_lang = language_manager.detect_language(message, prev_lang=prev_owner_lang)
                owner_lang = (owner_lang or prev_owner_lang or "es").strip().lower() or "es"
                state.memory_manager.set_flag(session_key, "owner_lang", owner_lang)
                if alt_key:
                    state.memory_manager.set_flag(alt_key, "owner_lang", owner_lang)
                state.memory_manager.set_flag(owner_id, "owner_lang", owner_lang)
            except Exception:
                pass
        if state.memory_manager and message and not _is_short_wa_confirmation(message) and not _is_short_wa_cancel(message):
            try:
                recent = state.memory_manager.get_memory_as_messages(session_key, limit=14) or []
            except Exception:
                recent = []
            try:
                llm_rewriter = getattr(agent, "llm", None) or ModelConfig.get_llm(ModelTier.INTERNAL)
                message = await _expand_followup_with_context(llm_rewriter, message, recent)
            except Exception:
                pass
        auto_send_wa = False  # En Chatter mantenemos borrador + confirmación.
        pending_last = _get_last_pending_action(state, owner_key)
        if state.memory_manager:
            try:
                state.memory_manager.set_flag(session_key, "history_table", Settings.SUPERINTENDENTE_HISTORY_TABLE)
                if alt_key:
                    state.memory_manager.set_flag(alt_key, "history_table", Settings.SUPERINTENDENTE_HISTORY_TABLE)
                if property_id:
                    state.memory_manager.set_flag(session_key, "property_id", property_id)
                    state.memory_manager.set_flag(owner_id, "property_id", property_id)
                    if alt_key:
                        state.memory_manager.set_flag(alt_key, "property_id", property_id)
                    # Deja un contexto explícito para que el LLM conozca el property_id.
                    state.memory_manager.save(
                        conversation_id=session_key,
                        role="system",
                        content=f"[CTX] property_id={property_id}",
                        channel="superintendente",
                    )
                    if alt_key:
                        state.memory_manager.save(
                            conversation_id=alt_key,
                            role="system",
                            content=f"[CTX] property_id={property_id}",
                            channel="superintendente",
                        )
                if chatter_context_block:
                    state.memory_manager.save(
                        conversation_id=session_key,
                        role="system",
                        content=f"[CHATTER_CTX]\n{chatter_context_block}",
                        channel="superintendente",
                    )
                    if alt_key:
                        state.memory_manager.save(
                            conversation_id=alt_key,
                            role="system",
                            content=f"[CHATTER_CTX]\n{chatter_context_block}",
                            channel="superintendente",
                        )
            except Exception:
                pass

        # --------------------------------------------------------
        # ✅ Confirmación WA directa si el pending se perdió
        # --------------------------------------------------------
        if _is_short_wa_confirmation(message) and (not pending_last or pending_last.get("type") == "wa"):
            recovered = _recover_wa_drafts_from_memory(state, session_key, alt_key)
            if not recovered:
                try:
                    sessions = _tracking_sessions(state).get(owner_key, {})
                    for sid in list(sessions.keys())[-5:]:
                        recovered = _recover_wa_drafts_from_memory(state, sid)
                        if recovered:
                            break
                except Exception:
                    recovered = []
            if recovered:
                if state.memory_manager:
                    try:
                        ensure_instance_credentials(state.memory_manager, session_key)
                    except Exception:
                        pass
                sent = 0
                for draft in recovered:
                    guest_id = draft.get("guest_id")
                    msg_raw = draft.get("message", "")
                    if not guest_id:
                        continue
                    msg_to_send = _clean_wa_payload(msg_raw)
                    msg_to_send = _ensure_guest_language_with_target(
                        msg_to_send,
                        guest_id,
                        target_lang=draft.get("target_lang") if isinstance(draft, dict) else None,
                    )
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
                    try:
                        await _sync_guest_offer_state_from_sent_wa(
                            state,
                            guest_id=guest_id,
                            sent_message=msg_to_send,
                            owner_id=owner_id,
                            session_id=session_key,
                            property_id=property_id,
                        )
                    except Exception:
                        pass
                    sent += 1

                guest_list = ", ".join([_normalize_guest_id(d.get("guest_id")) for d in recovered if d.get("guest_id")])
                response_text = f"✅ Mensaje enviado a {sent}/{len(recovered)} huésped(es): {guest_list}"
                _persist_visible_super_response(response_text, persist_user=True)
                return {"result": response_text}

        # --------------------------------------------------------
        # ✅ Resolver último pendiente (WA / KB / KB_REMOVE)
        # --------------------------------------------------------
        if pending_last:
            pending_type = pending_last.get("type")
            pending_payload = pending_last.get("payload")
            if not pending_payload:
                if pending_type == "wa":
                    state.superintendente_pending_wa.pop(session_key, None)
                    if alt_key:
                        state.superintendente_pending_wa.pop(alt_key, None)
                    _persist_pending_wa(state, session_key, None)
                    if alt_key:
                        _persist_pending_wa(state, alt_key, None)
                    _persist_last_pending_wa(state, owner_key, None)
                elif pending_type == "kb":
                    _persist_pending_kb(state, session_key, None)
                    if alt_key:
                        _persist_pending_kb(state, alt_key, None)
                _pop_last_pending_action(state, owner_key)
                pending_last = None
            if pending_last and looks_like_new_instruction(message) and not _looks_like_adjustment(message):
                if pending_type == "wa":
                    state.superintendente_pending_wa.pop(session_key, None)
                    if alt_key:
                        state.superintendente_pending_wa.pop(alt_key, None)
                    _persist_pending_wa(state, session_key, None)
                    if alt_key:
                        _persist_pending_wa(state, alt_key, None)
                    _persist_last_pending_wa(state, owner_key, None)
                elif pending_type == "kb":
                    _persist_pending_kb(state, session_key, None)
                    if alt_key:
                        _persist_pending_kb(state, alt_key, None)
                _pop_last_pending_action(state, owner_key)
                pending_last = None
        if pending_last:
            pending_type = pending_last.get("type")
            action = await _classify_pending_action(message, pending_type or "")
            if (
                pending_type == "wa"
                and action == "new"
                and message
                and not _is_short_wa_confirmation(message)
                and not _is_short_wa_cancel(message)
                and not looks_like_new_instruction(message)
            ):
                action = "adjust"
            if pending_type == "wa" and action == "new" and _looks_like_send_confirmation(message):
                action = "confirm"

            if action == "new":
                if pending_type == "wa":
                    state.superintendente_pending_wa.pop(session_key, None)
                    if alt_key:
                        state.superintendente_pending_wa.pop(alt_key, None)
                    _persist_pending_wa(state, session_key, None)
                    if alt_key:
                        _persist_pending_wa(state, alt_key, None)
                    _persist_last_pending_wa(state, owner_key, None)
                    _pop_trailing_pending_type(state, owner_key, "wa")
                elif pending_type == "kb":
                    _persist_pending_kb(state, session_key, None)
                    if alt_key:
                        _persist_pending_kb(state, alt_key, None)
                    _pop_trailing_pending_type(state, owner_key, "kb")
                elif pending_type == "kb_remove":
                    _pop_trailing_pending_type(state, owner_key, "kb_remove")
                else:
                    _pop_last_pending_action(state, owner_key)
            else:
                if pending_type == "kb":
                    pending_kb = pending_last.get("payload") or _load_pending_kb(state, session_key)
                    if not pending_kb and alt_key:
                        pending_kb = _load_pending_kb(state, alt_key)

                    if not pending_kb:
                        _pop_last_pending_action(state, owner_key)
                        pending_last = None
                        pending_type = None
                    else:
                        if action != "confirm" and _looks_like_kb_confirmation(message):
                            action = "confirm"
                        if action == "cancel" or _is_short_rejection(message):
                            _persist_pending_kb(state, session_key, None)
                            if alt_key:
                                _persist_pending_kb(state, alt_key, None)
                            _pop_trailing_pending_type(state, owner_key, "kb")
                            return {"result": "✓ Información descartada. No se agregó a la base de conocimientos."}

                        kb_response = await state.interno_agent.process_kb_response(
                            chat_id=session_key,
                            escalation_id="",
                            manager_response=message,
                            topic=pending_kb.get("topic", ""),
                            draft_content=pending_kb.get("content", ""),
                            hotel_name=pending_kb.get("hotel_name", payload.hotel_name),
                            superintendente_agent=state.superintendente_agent,
                            pending_state=pending_kb,
                            source=pending_kb.get("source", "superintendente"),
                        )

                        if isinstance(kb_response, (tuple, list)):
                            kb_response = " ".join(str(x) for x in kb_response)
                        elif not isinstance(kb_response, str):
                            kb_response = str(kb_response)

                        if action == "confirm" or "agregad" in kb_response.lower() or "✅" in kb_response:
                            _persist_pending_kb(state, session_key, None)
                            if alt_key:
                                _persist_pending_kb(state, alt_key, None)
                            _pop_trailing_pending_type(state, owner_key, "kb")
                        else:
                            _persist_pending_kb(state, session_key, pending_kb)
                            if alt_key:
                                _persist_pending_kb(state, alt_key, pending_kb)
                            _update_last_pending_action(state, owner_key, pending_kb)

                        return {"result": kb_response}

                if pending_type == "kb_remove":
                    pending_remove = pending_last.get("payload") or {}
                    hotel_name = pending_remove.get("hotel_name") or payload.hotel_name
                    remove_payload = pending_remove.get("payload") if isinstance(pending_remove, dict) else {}
                    if not remove_payload:
                        _pop_last_pending_action(state, owner_key)
                        pending_last = None
                        pending_type = None
                    else:
                        if action == "cancel" or _is_short_rejection(message):
                            _pop_trailing_pending_type(state, owner_key, "kb_remove")
                            return {"result": "✓ Eliminación cancelada."}
                        if action == "adjust":
                            return {
                                "result": (
                                    "Indica qué registros quieres conservar o ajusta el criterio para refinar la eliminación."
                                )
                            }
                        target_ids = remove_payload.get("target_ids") or []
                        criteria = remove_payload.get("criteria") or ""
                        note = remove_payload.get("note") or ""
                        result_obj = await state.superintendente_agent.handle_kb_removal(
                            target_ids=target_ids,
                            hotel_name=hotel_name,
                            encargado_id=owner_id,
                            note=note,
                            criteria=criteria,
                        )
                        _pop_trailing_pending_type(state, owner_key, "kb_remove")
                        msg = result_obj.get("message") if isinstance(result_obj, dict) else None
                        return {"result": msg or "✅ Eliminación completada."}

                if pending_type == "wa":
                    pending_wa = pending_last.get("payload")
                    if not pending_wa:
                        pending_wa = _load_pending_wa(state, session_key) or (alt_key and _load_pending_wa(state, alt_key))
                    if not pending_wa and _looks_like_adjustment(message):
                        recovered = _recover_wa_drafts_from_memory(state, session_key, alt_key)
                        if recovered:
                            pending_wa = recovered[0] if len(recovered) == 1 else {"drafts": recovered}

                    if not pending_wa:
                        _pop_last_pending_action(state, owner_key)
                        pending_last = None
                        pending_type = None
                    else:
                        if action == "cancel" or _is_short_wa_cancel(message):
                            state.superintendente_pending_wa.pop(session_key, None)
                            if alt_key:
                                state.superintendente_pending_wa.pop(alt_key, None)
                            _persist_pending_wa(state, session_key, None)
                            if alt_key:
                                _persist_pending_wa(state, alt_key, None)
                            _persist_last_pending_wa(state, owner_key, None)
                            _pop_trailing_pending_type(state, owner_key, "wa")
                            response_text = "❌ Envío cancelado. Si necesitas otro borrador, dímelo."
                            _persist_visible_super_response(response_text, persist_user=True)
                            return {"result": response_text}

                        if action == "confirm" or _is_short_wa_confirmation(message):
                            drafts = pending_wa.get("drafts") if isinstance(pending_wa, dict) else [pending_wa]
                            drafts = drafts or []
                            if not drafts:
                                _pop_last_pending_action(state, owner_key)
                                response_text = "⚠️ No hay borrador pendiente para enviar."
                                _persist_visible_super_response(response_text, persist_user=True)
                                return {"result": response_text}

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
                                msg_to_send = _ensure_guest_language_with_target(
                                    msg_to_send,
                                    guest_id,
                                    target_lang=draft.get("target_lang") if isinstance(draft, dict) else None,
                                )
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
                                try:
                                    await _sync_guest_offer_state_from_sent_wa(
                                        state,
                                        guest_id=guest_id,
                                        sent_message=msg_to_send,
                                        owner_id=owner_id,
                                        session_id=session_key,
                                        property_id=property_id,
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
                            _persist_last_pending_wa(state, owner_key, None)
                            _pop_trailing_pending_type(state, owner_key, "wa")
                            guest_list = ", ".join(
                                [_normalize_guest_id(d.get("guest_id")) for d in drafts if d.get("guest_id")]
                            )
                            response_text = f"✅ Mensaje enviado a {sent}/{len(drafts)} huésped(es): {guest_list}"
                            _persist_visible_super_response(response_text, persist_user=True)
                            return {"result": response_text}

                        drafts = pending_wa.get("drafts") if isinstance(pending_wa, dict) else [pending_wa]
                        drafts = drafts or []
                        llm = getattr(state.superintendente_agent, "llm", None)
                        lang_adjust = await _detect_language_adjustment_with_llm(message)
                        target_lang_override = lang_adjust.get("target_lang")
                        language_only_adjustment = bool(lang_adjust.get("language_only"))
                        updated: list[dict] = []
                        for draft in drafts:
                            guest_id = draft.get("guest_id")
                            base_msg = draft.get("message", "")
                            if language_only_adjustment:
                                rewritten = base_msg
                            else:
                                rewritten = await _rewrite_wa_draft(llm, base_msg, message)
                            effective_target = target_lang_override or draft.get("target_lang")
                            updated.append(
                                {
                                    **draft,
                                    "guest_id": guest_id,
                                    "message": _ensure_guest_language_with_target(
                                        rewritten,
                                        guest_id,
                                        target_lang=effective_target,
                                    ),
                                    "target_lang": effective_target,
                                }
                            )
                        if not updated:
                            recovered = _recover_wa_drafts_from_memory(state, session_key, alt_key)
                            if recovered:
                                drafts = recovered
                                updated = []
                                for draft in drafts:
                                    guest_id = draft.get("guest_id")
                                    base_msg = draft.get("message", "")
                                    if language_only_adjustment:
                                        rewritten = base_msg
                                    else:
                                        rewritten = await _rewrite_wa_draft(llm, base_msg, message)
                                    effective_target = target_lang_override or draft.get("target_lang")
                                    updated.append(
                                        {
                                            **draft,
                                            "guest_id": guest_id,
                                            "message": _ensure_guest_language_with_target(
                                                rewritten,
                                                guest_id,
                                                target_lang=effective_target,
                                            ),
                                            "target_lang": effective_target,
                                        }
                                    )
                            if not updated:
                                response_text = "⚠️ No pude recuperar el borrador anterior. ¿Quieres que lo genere de nuevo?"
                                _persist_visible_super_response(response_text, persist_user=True)
                                return {"result": response_text}
                        pending_payload: Any = {"drafts": updated} if len(updated) > 1 else updated[0]
                        state.superintendente_pending_wa[session_key] = pending_payload
                        if alt_key:
                            state.superintendente_pending_wa[alt_key] = pending_payload
                        _persist_pending_wa(state, session_key, pending_payload)
                        if alt_key:
                            _persist_pending_wa(state, alt_key, pending_payload)
                        _persist_last_pending_wa(state, owner_key, pending_payload)
                        _update_last_pending_action(state, owner_key, pending_payload)
                        _record_pending_action(state, owner_key, "wa", pending_payload, session_key)
                        try:
                            if state.memory_manager and updated:
                                draft = updated[0]
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
                        response_text = _format_wa_preview(updated)
                        _persist_visible_super_response(response_text, persist_user=True)
                        return {"result": response_text}

        if (
            not pending_last
            and message
            and not looks_like_new_instruction(message)
            and not _is_short_wa_confirmation(message)
            and not _is_short_wa_cancel(message)
            and len(message.split()) <= 8
        ):
            recovered = _recover_wa_drafts_from_memory(state, session_key, alt_key)
            if not recovered:
                try:
                    sessions = _tracking_sessions(state).get(owner_key, {})
                    for sid in list(sessions.keys())[-5:]:
                        recovered = _recover_wa_drafts_from_memory(state, sid)
                        if recovered:
                            break
                except Exception:
                    recovered = []
            if recovered:
                pending_payload: Any = recovered[0] if len(recovered) == 1 else {"drafts": recovered}
                state.superintendente_pending_wa[session_key] = pending_payload
                if alt_key:
                    state.superintendente_pending_wa[alt_key] = pending_payload
                _persist_pending_wa(state, session_key, pending_payload)
                if alt_key:
                    _persist_pending_wa(state, alt_key, pending_payload)
                _persist_last_pending_wa(state, owner_key, pending_payload)
                _record_pending_action(state, owner_key, "wa", pending_payload, session_key)

                drafts = pending_payload.get("drafts") if isinstance(pending_payload, dict) else [pending_payload]
                drafts = drafts or []
                llm = getattr(state.superintendente_agent, "llm", None)
                lang_adjust = await _detect_language_adjustment_with_llm(message)
                target_lang_override = lang_adjust.get("target_lang")
                language_only_adjustment = bool(lang_adjust.get("language_only"))
                updated: list[dict] = []
                for draft in drafts:
                    guest_id = draft.get("guest_id")
                    base_msg = draft.get("message", "")
                    if language_only_adjustment:
                        rewritten = base_msg
                    else:
                        rewritten = await _rewrite_wa_draft(llm, base_msg, message)
                    effective_target = target_lang_override or draft.get("target_lang")
                    updated.append(
                        {
                            **draft,
                            "guest_id": guest_id,
                            "message": _ensure_guest_language_with_target(
                                rewritten,
                                guest_id,
                                target_lang=effective_target,
                            ),
                            "target_lang": effective_target,
                        }
                    )
                if updated:
                    new_payload: Any = {"drafts": updated} if len(updated) > 1 else updated[0]
                    state.superintendente_pending_wa[session_key] = new_payload
                    if alt_key:
                        state.superintendente_pending_wa[alt_key] = new_payload
                    _persist_pending_wa(state, session_key, new_payload)
                    if alt_key:
                        _persist_pending_wa(state, alt_key, new_payload)
                    _persist_last_pending_wa(state, owner_key, new_payload)
                    _update_last_pending_action(state, owner_key, new_payload)
                    _record_pending_action(state, owner_key, "wa", new_payload, session_key)
                    try:
                        if state.memory_manager and updated:
                            draft = updated[0]
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
                    response_text = _format_wa_preview(updated)
                    _persist_visible_super_response(response_text, persist_user=True)
                    return {"result": response_text}

        # Fast-path semántico: si hay folio_id resoluble y el LLM detecta
        # intención de consulta de reserva, evita consulta_reserva_general.
        if not pending_last:
            folio_ctx = _resolve_fastpath_folio_id(
                state,
                message=message,
                chatter_context_block=chatter_context_block,
                session_key=session_key,
                alt_key=alt_key,
                owner_key=owner_key,
                owner_id=owner_id,
                property_id=property_id,
            ) or ""
            should_fastpath = False
            if folio_ctx and folio_ctx.lower() != "n/d":
                should_fastpath = await _should_use_reservation_fastpath_with_llm(
                    message,
                    chatter_context_block=chatter_context_block,
                    folio_id=folio_ctx,
                )
            if should_fastpath:
                try:
                    from tools.superintendente_tool import create_consulta_reserva_persona_tool

                    consulta_tool = create_consulta_reserva_persona_tool(
                        memory_manager=state.memory_manager,
                        chat_id=session_key,
                    )
                    tool_property_id: Any = property_id
                    if isinstance(tool_property_id, str) and tool_property_id.isdigit():
                        tool_property_id = int(tool_property_id)

                    raw = await consulta_tool.ainvoke(
                        {
                            "folio_id": str(folio_ctx),
                            "property_id": tool_property_id,
                        }
                    )
                    parsed = raw
                    if isinstance(raw, str):
                        try:
                            parsed = json.loads(raw)
                        except Exception:
                            parsed = raw
                    if isinstance(parsed, list) and parsed:
                        parsed = parsed[0]
                    normalized = _normalize_reservation_detail(parsed if isinstance(parsed, dict) else {})
                    if normalized:
                        response_text = _format_reservation_detail_response(normalized)
                        _persist_visible_super_response(response_text, persist_user=True)
                        return {"result": response_text}
                    if isinstance(raw, str) and raw.strip():
                        _persist_visible_super_response(raw.strip(), persist_user=True)
                        return {"result": raw.strip()}
                except Exception as exc:
                    log.debug("Fast-path folio_id no aplicado: %s", exc)

        result = await agent.ainvoke(
            user_input=message,
            encargado_id=owner_id,
            hotel_name=payload.hotel_name,
            context_window=payload.context_window,
            chat_history=None,
            session_id=payload.session_id or session_key,
        )
        result = _ensure_owner_language(str(result or ""), owner_lang)

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
                    msg_to_send = _ensure_guest_language_with_target(
                        msg_to_send,
                        guest_id,
                        target_lang=draft.get("target_lang") if isinstance(draft, dict) else None,
                    )
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
                    try:
                        await _sync_guest_offer_state_from_sent_wa(
                            state,
                            guest_id=guest_id,
                            sent_message=msg_to_send,
                            owner_id=owner_id,
                            session_id=session_key,
                            property_id=property_id,
                        )
                    except Exception:
                        pass
                    sent += 1
                guest_list = ", ".join([_normalize_guest_id(d.get("guest_id")) for d in wa_drafts if d.get("guest_id")])
                response_text = f"✅ Mensaje enviado a {sent}/{len(wa_drafts)} huésped(es): {guest_list}"
                _persist_visible_super_response(response_text, persist_user=False)
                return {"result": response_text}
            if state.memory_manager:
                try:
                    ctx_property_id = state.memory_manager.get_flag(session_key, "property_id")
                    ctx_instance_id = (
                        state.memory_manager.get_flag(session_key, "instance_id")
                        or state.memory_manager.get_flag(session_key, "instance_hotel_code")
                    )
                    for draft in wa_drafts:
                        if ctx_property_id is not None:
                            draft["property_id"] = ctx_property_id
                        if ctx_instance_id:
                            draft["instance_id"] = ctx_instance_id
                        guest_id = draft.get("guest_id")
                        if guest_id:
                            if ctx_property_id is not None:
                                state.memory_manager.set_flag(guest_id, "property_id", ctx_property_id)
                            if ctx_instance_id:
                                state.memory_manager.set_flag(guest_id, "instance_id", ctx_instance_id)
                                state.memory_manager.set_flag(guest_id, "instance_hotel_code", ctx_instance_id)
                except Exception:
                    pass
            pending_payload: Any = {"drafts": wa_drafts} if len(wa_drafts) > 1 else wa_drafts[0]
            state.superintendente_pending_wa[session_key] = pending_payload
            if alt_key:
                state.superintendente_pending_wa[alt_key] = pending_payload
            _persist_pending_wa(state, session_key, pending_payload)
            if alt_key:
                _persist_pending_wa(state, alt_key, pending_payload)
            _persist_last_pending_wa(state, owner_key, pending_payload)
            _record_pending_action(state, owner_key, "wa", pending_payload, session_key)
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
            response_text = _format_wa_preview(wa_drafts)
            _persist_visible_super_response(response_text, persist_user=False)
            return {"result": response_text}

        kb_remove_payload = _parse_kb_remove_draft_marker(result)
        if kb_remove_payload:
            _record_pending_action(state, owner_key, "kb_remove", kb_remove_payload, session_key)
            removal_payload = kb_remove_payload.get("payload") if isinstance(kb_remove_payload, dict) else None
            if isinstance(removal_payload, dict):
                return {"result": _format_kb_remove_preview(removal_payload)}
            return {"result": "🧹 Borrador de eliminación preparado. Responde 'ok' para confirmar o 'no' para cancelar."}

        kb_payload = _parse_kb_draft_marker(result)
        if kb_payload:
            pending_kb = {
                **kb_payload,
                "source": "superintendente",
                "category": kb_payload.get("category") or "general",
            }
            _persist_pending_kb(state, session_key, pending_kb)
            if alt_key:
                _persist_pending_kb(state, alt_key, pending_kb)
            _record_pending_action(state, owner_key, "kb", pending_kb, session_key)
            preview = build_kb_preview(
                pending_kb.get("topic") or "Información",
                pending_kb.get("category") or "general",
                pending_kb.get("content") or "",
            )
            return {"result": preview}

        detail_payload = _pull_recent_reservation_detail(state, session_key, alt_key, owner_id)
        if detail_payload:
            normalized = _normalize_reservation_detail(detail_payload)
            csv_payload = _build_reservation_detail_csv(normalized or {})
            response = {
                "structured": {
                    "kind": "reservation_detail",
                    "data": normalized or detail_payload,
                    "csv": csv_payload,
                    "csv_delimiter": ";",
                }
            }
            attach_structured_payload_to_latest_message(
                conversation_id=session_key,
                structured_payload=response["structured"],
                table=Settings.SUPERINTENDENTE_HISTORY_TABLE,
                role="bookai",
            )
            try:
                for key in [session_key, alt_key, owner_id]:
                    if key:
                        state.memory_manager.clear_flag(key, "superintendente_last_reservation_detail")
            except Exception:
                pass
            return response

        structured = _pull_recent_reservations(state, session_key, alt_key, owner_id)
        if structured:
            csv_payload = _build_reservations_csv(structured)
            response = {
                "structured": {
                    "kind": "reservations",
                    "data": structured,
                    "csv": csv_payload,
                    "csv_delimiter": ";",
                }
            }
            attach_structured_payload_to_latest_message(
                conversation_id=session_key,
                structured_payload=response["structured"],
                table=Settings.SUPERINTENDENTE_HISTORY_TABLE,
                role="bookai",
            )
            try:
                for key in [session_key, alt_key, owner_id]:
                    if key:
                        state.memory_manager.clear_flag(key, "superintendente_last_reservations")
            except Exception:
                pass
            return response
        fallback_detail = _extract_detail_from_text(result)
        if fallback_detail:
            response = {
                "structured": {
                    "kind": "reservation_detail",
                    "data": fallback_detail,
                    "csv": _build_reservation_detail_csv(fallback_detail),
                    "csv_delimiter": ";",
                }
            }
            attach_structured_payload_to_latest_message(
                conversation_id=session_key,
                structured_payload=response["structured"],
                table=Settings.SUPERINTENDENTE_HISTORY_TABLE,
                role="bookai",
            )
            return response
        return {"result": result}

    @router.post("/sessions")
    async def create_session(payload: CreateSessionRequest, _: None = Depends(_verify_bearer)):
        owner_key, owner_id, property_id = _resolve_owner_key(payload)
        session_id = _generate_session_id()
        title = (payload.title or "").strip()
        if not title:
            title = f"Chat {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}"

        sessions = _tracking_sessions(state)
        owner_sessions = sessions.setdefault(owner_key, {})
        owner_sessions[session_id] = {
            "title": title,
            "created_at": datetime.utcnow().isoformat(),
        }
        state.save_tracking()
        _persist_session_title_db(
            state,
            conversation_id=session_id,
            owner_key=owner_key,
            title=title,
        )

        if state.memory_manager:
            try:
                state.memory_manager.set_flag(session_id, "history_table", Settings.SUPERINTENDENTE_HISTORY_TABLE)
                state.memory_manager.set_flag(session_id, "property_name", payload.hotel_name)
                state.memory_manager.set_flag(session_id, "superintendente_owner_id", owner_id)
                if property_id:
                    state.memory_manager.set_flag(session_id, "property_id", property_id)
                marker = f"[SUPER_SESSION]|title={title}"
                state.memory_manager.save(
                    conversation_id=session_id,
                    role="system",
                    content=marker,
                    channel="telegram",
                    original_chat_id=owner_key,
                )
            except Exception as exc:
                log.warning("No se pudo registrar sesión en historia: %s", exc)

        return {
            "session_id": session_id,
            "title": title,
            "name": title,
            "session_title": title,
            "chat_title": title,
            "display_name": title,
            "label": title,
        }

    @router.get("/sessions")
    async def list_sessions(
        owner_id: Optional[int | str] = Query(default=None),
        property_id: Optional[str] = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
        _: None = Depends(_verify_bearer),
    ):
        table = Settings.SUPERINTENDENTE_HISTORY_TABLE
        owner_id = str(owner_id).strip() if owner_id is not None else ""
        if not owner_id:
            return {"items": []}
        owner_key = f"{owner_id}:{property_id.strip()}" if property_id else owner_id
        sessions = _tracking_sessions(state).get(owner_key, {})
        titles = {sid: meta.get("title") for sid, meta in sessions.items()}

        items = []
        rows: list[dict[str, Any]]
        try:
            query = (
                state.supabase_client.table(table)
                .select("conversation_id, content, created_at, original_chat_id, role, session_title")
            )
            if property_id:
                # Compat: mensajes en sesión pueden persistirse con original_chat_id=owner_id
                # (sin property) o owner_key (owner:property).
                query = query.in_("original_chat_id", [owner_key, owner_id])
            else:
                query = query.eq("original_chat_id", owner_key)
            resp = (
                query.order("created_at", desc=True)
                .limit(limit * 20)
                .execute()
            )
            rows = resp.data or []
        except Exception as exc:
            log.warning("No se pudo leer historial superintendente: %s", exc)
            rows = []

        rows_by_conversation: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            convo_id = str(row.get("conversation_id") or "").strip()
            if not convo_id:
                continue
            rows_by_conversation.setdefault(convo_id, []).append(row)

        tracking_dirty = False
        ai_titles_budget = 8
        seen = set()
        for row in rows:
            convo_id = str(row.get("conversation_id") or "").strip()
            if not convo_id or convo_id in seen:
                continue
            convo_rows = rows_by_conversation.get(convo_id) or []
            # Evita mostrar pseudo-sesiones de contexto (p.ej. conversation_id == property_id)
            # cuando solo contienen marcadores internos como [CTX]/[SUPER_SESSION].
            if property_id and convo_id == str(property_id).strip():
                has_real_content = any(
                    not _is_internal_super_message(str((sample or {}).get("content") or ""))
                    for sample in convo_rows
                )
                if not has_real_content:
                    continue
            seen.add(convo_id)
            last_message = row.get("content")
            last_at = row.get("created_at")
            db_title = str(row.get("session_title") or "").strip() or None
            title = db_title or titles.get(convo_id) or _parse_session_title(str(last_message or "")) or "Chat"
            if _is_generic_session_title(title):
                user_seed = ""
                assistant_seed = ""
                for sample in convo_rows:
                    role = str(sample.get("role") or "").strip().lower()
                    content = str(sample.get("content") or "").strip()
                    if not content or _is_internal_super_message(content):
                        continue
                    if role in {"user", "guest"} and not user_seed:
                        user_seed = content
                    elif role in {"assistant", "bookai"} and not assistant_seed:
                        assistant_seed = content
                    if user_seed and assistant_seed:
                        break

                fallback_seed = user_seed or assistant_seed or ("" if _is_internal_super_message(str(last_message or "")) else str(last_message or ""))
                candidate_title = None
                if fallback_seed and ai_titles_budget > 0:
                    llm = (
                        getattr(getattr(state, "superintendente_agent", None), "llm", None)
                        or ModelConfig.get_llm(ModelTier.INTERNAL)
                    )
                    candidate_title = await _generate_session_title_with_ai(
                        llm,
                        user_seed=user_seed or fallback_seed,
                        assistant_seed=assistant_seed or None,
                    )
                    ai_titles_budget -= 1
                if not candidate_title and fallback_seed:
                    candidate_title = _fallback_title_from_seed(fallback_seed)
                if candidate_title:
                    title = candidate_title
                    owner_sessions = _tracking_sessions(state).setdefault(owner_key, {})
                    current_meta = owner_sessions.get(convo_id) or {}
                    if current_meta.get("title") != candidate_title:
                        current_meta["title"] = candidate_title
                        current_meta.setdefault("created_at", datetime.utcnow().isoformat())
                        owner_sessions[convo_id] = current_meta
                        tracking_dirty = True
                    _persist_session_title_db(
                        state,
                        conversation_id=convo_id,
                        owner_key=owner_key,
                        title=candidate_title,
                    )
            items.append(
                {
                    "session_id": convo_id,
                    "title": title,
                    "name": title,
                    "session_title": title,
                    "chat_title": title,
                    "display_name": title,
                    "label": title,
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
                        "name": meta.get("title") or "Chat",
                        "session_title": meta.get("title") or "Chat",
                        "chat_title": meta.get("title") or "Chat",
                        "display_name": meta.get("title") or "Chat",
                        "label": meta.get("title") or "Chat",
                        "last_message": None,
                        "last_message_at": meta.get("created_at"),
                    }
                )
                if len(items) >= limit:
                    break

        if tracking_dirty:
            state.save_tracking()

        return {"items": items}

    @router.get("/sessions/{session_id}/messages")
    async def list_session_messages(
        session_id: str,
        limit: int = Query(default=50, ge=1, le=200),
        include_internal: bool = Query(default=False),
        _: None = Depends(_verify_bearer),
    ):
        from core.db import get_conversation_history

        rows = get_conversation_history(
            conversation_id=session_id,
            limit=limit,
            table=Settings.SUPERINTENDENTE_HISTORY_TABLE,
        )
        normalized_rows: list[dict[str, Any]] = []
        visible_assistant_keys: set[str] = set()
        for base_row in (rows or []):
            raw = str((base_row or {}).get("content") or "").strip()
            role = _normalize_super_role((base_row or {}).get("role"))
            if not raw or _is_internal_super_message(raw) or role != "assistant":
                continue
            key = re.sub(r"\s+", " ", raw).strip().lower()
            if key:
                visible_assistant_keys.add(key)
        for row in (rows or []):
            item = dict(row or {})
            raw_content = str(item.get("content") or "")
            rendered_internal = _render_internal_super_message(raw_content)
            content = rendered_internal or raw_content
            raw_is_internal = _is_internal_super_message(raw_content)
            raw_role = item.get("role")
            structured = item.get("structured")
            structured_payload = item.get("structured_payload")

            # Compat extra: si structured_payload llega serializado, intentamos parsearlo.
            if structured is None and isinstance(structured_payload, str):
                try:
                    structured_payload = json.loads(structured_payload)
                except Exception:
                    structured_payload = None

            # Compat: el frontend del chatter suele esperar `structured`.
            if structured is None and structured_payload is not None:
                structured = structured_payload
            if structured is None:
                detail = _extract_detail_from_text(content)
                if detail:
                    structured = {
                        "kind": "reservation_detail",
                        "data": detail,
                        "csv": _build_reservation_detail_csv(detail),
                        "csv_delimiter": ";",
                    }
                else:
                    recovered = _extract_reservations_from_text(raw_content)
                    if recovered:
                        structured = recovered

            item["role"] = _normalize_super_role(raw_role)
            item["sender"] = _normalize_super_sender(raw_role)
            item["message"] = content
            item["content"] = content
            item["structured"] = structured
            item["structured_payload"] = structured_payload or structured
            item["_rendered_internal"] = bool(rendered_internal)
            item["_raw_internal"] = raw_is_internal
            normalized_rows.append(item)
        if not include_internal:
            filtered_rows: list[dict[str, Any]] = []
            seen_internal_rendered: set[str] = set()
            for row in (normalized_rows or []):
                raw_internal = bool((row or {}).get("_raw_internal"))
                rendered = bool((row or {}).get("_rendered_internal"))
                content_text = str((row or {}).get("content") or "")
                role = str((row or {}).get("role") or "")
                if raw_internal and not rendered:
                    continue
                if raw_internal and rendered and role == "assistant":
                    dedupe_key = re.sub(r"\s+", " ", content_text).strip().lower()
                    if dedupe_key in visible_assistant_keys:
                        continue
                    if dedupe_key in seen_internal_rendered:
                        continue
                    seen_internal_rendered.add(dedupe_key)
                filtered_rows.append(row)
            normalized_rows = filtered_rows
        for row in normalized_rows:
            row.pop("_rendered_internal", None)
            row.pop("_raw_internal", None)
        return {"session_id": session_id, "items": normalized_rows}

    app.include_router(router)
