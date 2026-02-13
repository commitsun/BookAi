"""Rutas FastAPI para exponer herramientas del Superintendente."""

from __future__ import annotations

import json
import logging
import re
import secrets
import string
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from core.config import Settings, ModelConfig, ModelTier
from core.constants import WA_CONFIRM_WORDS, WA_CANCEL_WORDS
from core.instance_context import ensure_instance_credentials
from core.message_utils import sanitize_wa_message, looks_like_new_instruction

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
    property_id: Optional[str] = Field(default=None, description="ID de property (opcional)")


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
    if text.count("Folio ID:") != 1:
        return None
    name = ""
    try:
        name = text.split("| Folio ID:")[0].strip()
    except Exception:
        name = ""
    def _m(pattern: str) -> Optional[str]:
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(1).strip() if m else None

    return {
        "folio_id": _m(r"Folio ID:\s*([A-Za-z0-9]+)"),
        "folio_code": _m(r"Código:\s*([A-Za-z0-9/\\-]+)"),
        "partner_name": name or _m(r"Nombre:\s*([^|\\n]+)"),
        "partner_phone": _m(r"Tel:\s*([^|\\n]+)"),
        "partner_email": _m(r"Email:\s*([^|\\n]+)"),
        "state": _m(r"Estado:\s*([^|\\n]+)"),
        "amount_total": _m(r"Total:\s*([0-9]+(?:[.,][0-9]+)?)"),
        "pending_amount": _m(r"Pendiente:\s*([0-9]+(?:[.,][0-9]+)?)"),
        "checkin": _m(r"Check-in:\s*([^|\\n]+)"),
        "checkout": _m(r"Check-out:\s*([^|\\n]+)"),
        "portal_url": _m(r"(https?://\\S+)"),
    }


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

                guest_list = ", ".join([_normalize_guest_id(d.get("guest_id")) for d in recovered if d.get("guest_id")])
                return {"result": f"✅ Mensaje enviado a {sent}/{len(recovered)} huésped(es): {guest_list}"}

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
                            return {"result": "❌ Envío cancelado. Si necesitas otro borrador, dímelo."}

                        if action == "confirm" or _is_short_wa_confirmation(message):
                            drafts = pending_wa.get("drafts") if isinstance(pending_wa, dict) else [pending_wa]
                            drafts = drafts or []
                            if not drafts:
                                _pop_last_pending_action(state, owner_key)
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
                            _persist_last_pending_wa(state, owner_key, None)
                            _pop_trailing_pending_type(state, owner_key, "wa")
                            guest_list = ", ".join(
                                [_normalize_guest_id(d.get("guest_id")) for d in drafts if d.get("guest_id")]
                            )
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
                            recovered = _recover_wa_drafts_from_memory(state, session_key, alt_key)
                            if recovered:
                                drafts = recovered
                                updated = []
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
                                return {"result": "⚠️ No pude recuperar el borrador anterior. ¿Quieres que lo genere de nuevo?"}
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
                        return {"result": _format_wa_preview(updated)}

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
                    return {"result": _format_wa_preview(updated)}

        result = await agent.ainvoke(
            user_input=message,
            encargado_id=owner_id,
            hotel_name=payload.hotel_name,
            context_window=payload.context_window,
            chat_history=payload.chat_history,
            session_id=payload.session_id or session_key,
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
            return {"result": _format_wa_preview(wa_drafts)}

        kb_remove_payload = _parse_kb_remove_draft_marker(result)
        if kb_remove_payload:
            _record_pending_action(state, owner_key, "kb_remove", kb_remove_payload, session_key)
            return {"result": result}

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
            return {"result": result}

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
            try:
                for key in [session_key, alt_key, owner_id]:
                    if key:
                        state.memory_manager.clear_flag(key, "superintendente_last_reservations")
            except Exception:
                pass
            return response
        fallback_detail = _extract_detail_from_text(result)
        if fallback_detail:
            return {
                "structured": {
                    "kind": "reservation_detail",
                    "data": fallback_detail,
                    "csv": _build_reservation_detail_csv(fallback_detail),
                    "csv_delimiter": ";",
                }
            }
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

        return {"session_id": session_id, "title": title}

    @router.get("/sessions")
    async def list_sessions(
        owner_id: str = Query(...),
        property_id: Optional[str] = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
        _: None = Depends(_verify_bearer),
    ):
        table = Settings.SUPERINTENDENTE_HISTORY_TABLE
        owner_key = f"{owner_id}:{property_id.strip()}" if property_id else owner_id
        sessions = _tracking_sessions(state).get(owner_key, {})
        titles = {sid: meta.get("title") for sid, meta in sessions.items()}

        items = []
        try:
            resp = (
                state.supabase_client.table(table)
                .select("conversation_id, content, created_at, original_chat_id")
                .eq("original_chat_id", owner_key)
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
