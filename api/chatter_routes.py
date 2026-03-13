"""Rutas FastAPI para el chatter de Roomdoo."""

from __future__ import annotations

import json
import logging
import re
import time
from urllib.parse import unquote
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.config import Settings, ModelConfig, ModelTier
from core.db import supabase, is_chat_visible_in_list
from core.escalation_db import (
    get_escalation,
    get_latest_escalation_for_chat,
    get_latest_resolved_escalation_for_chat,
    is_escalation_resolved,
    list_pending_escalations,
    list_pending_escalations_for_chat,
    resolve_escalation_with_resolution,
    resolve_pending_escalations_for_chat,
)
from core.template_registry import TemplateRegistry, TemplateDefinition
from core.instance_context import ensure_instance_credentials, fetch_instance_by_code
from core.offer_semantics import sync_guest_offer_state_from_sent_wa
from core.language_manager import language_manager
from core.template_structured import (
    build_template_structured_payload,
    extract_structured_csv,
)
from core.template_button_url import (
    build_folio_details_url,
    extract_folio_details_url,
    extract_url_button_indexes,
    resolve_button_base_url,
    strip_url_control_params,
    to_folio_dynamic_part,
)
from tools.superintendente_tool import create_consulta_reserva_persona_tool
from core.db import upsert_chat_reservation, get_active_chat_reservation

log = logging.getLogger("ChatterRoutes")
_INSTANCE_CHAT_SETS_TTL_SECONDS = 5
_instance_chat_sets_cache: Dict[str, Tuple[float, set[str], set[str]]] = {}

try:
    import phonenumbers
    from phonenumbers import NumberParseException
except Exception:
    phonenumbers = None
    NumberParseException = Exception


# ---------------------------------------------------------------------------
# Modelos de entrada
# ---------------------------------------------------------------------------
class SendMessageRequest(BaseModel):
    user_id: Optional[int | str] = Field(
        default=None,
        description="ID del usuario en Roomdoo (numérico o string, opcional por compatibilidad).",
    )
    user_first_name: Optional[str] = Field(default=None, description="Nombre del usuario")
    user_last_name: Optional[str] = Field(default=None, description="Primer apellido del usuario")
    user_last_name2: Optional[str] = Field(default=None, description="Segundo apellido del usuario")
    chat_id: str = Field(..., description="ID del chat (telefono)")
    message: str = Field(..., description="Texto del mensaje a enviar")
    channel: str = Field(default="whatsapp", description="Canal de salida")
    sender: Optional[str] = Field(default="bookai", description="Emisor (guest/cliente, bookai)")
    property_id: Optional[int | str] = Field(
        default=None,
        description="ID de property (numérico o string, opcional).",
    )


class ToggleBookAiRequest(BaseModel):
    bookai_enabled: bool = Field(..., description="Activa o desactiva BookAI para el hilo")
    property_id: Optional[int | str] = Field(
        default=None,
        description="ID de property (numérico o string, opcional).",
    )


class SendTemplateRequest(BaseModel):
    chat_id: str = Field(..., description="ID del chat (telefono)")
    template_code: str = Field(..., description="Codigo interno de la plantilla")
    instance_id: Optional[str] = Field(default=None, description="ID de instancia (opcional)")
    language: Optional[str] = Field(default="es", description="Idioma de la plantilla")
    parameters: Dict[str, Any] = Field(default_factory=dict, description="Parametros para placeholders")
    button_base_url: Optional[str] = Field(
        default=None,
        description="URL base publica para botones URL dinamicos (ej: https://alda.roomdoo.com)",
    )
    rendered_text: Optional[str] = Field(
        default=None,
        description="Texto renderizado de la plantilla (opcional, para contexto)",
    )
    channel: str = Field(default="whatsapp", description="Canal de salida")
    property_id: Optional[int | str] = Field(
        default=None,
        description="ID de property (numérico o string, opcional).",
    )


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


class ResolveEscalationRequest(BaseModel):
    property_id: Optional[int | str] = Field(
        default=None,
        description="ID de property (numérico o string, opcional).",
    )
    resolution_medium: Optional[Literal["phone", "in_person", "other"]] = Field(
        default=None,
        description="Canal de resolución manual.",
    )
    resolution_notes: Optional[str] = Field(
        default="",
        description="Notas de resolución (puede ser vacío).",
    )
    resolved_by: Optional[int | str] = Field(
        default=None,
        description="ID del usuario que resuelve (opcional).",
    )
    resolved_by_name: Optional[str] = Field(
        default=None,
        description="Nombre visible del usuario que resuelve (opcional).",
    )
    resolved_by_email: Optional[str] = Field(
        default=None,
        description="Email del usuario que resuelve (opcional).",
    )


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def _parse_token_instance_map() -> Dict[str, str]:
    parsed: Dict[str, str] = {}
    token_test = str(Settings.ROOMDOO_BOOKAI_TOKEN_TEST or "").strip()
    token_alda = str(Settings.ROOMDOO_BOOKAI_TOKEN_ALDA or "").strip()
    instance_test = str(Settings.ROOMDOO_INSTANCE_ID_TEST or "").strip()
    instance_alda = str(Settings.ROOMDOO_INSTANCE_ID_ALDA or "").strip()
    if token_test:
        parsed[token_test] = instance_test or "bookai-test"
    if token_alda:
        parsed[token_alda] = instance_alda or "bookai-alda"

    raw = (Settings.ROOMDOO_TOKEN_INSTANCE_MAP or "").strip()
    if not raw:
        return parsed

    if raw.startswith("{"):
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                for token, instance in payload.items():
                    token_text = str(token or "").strip()
                    instance_text = str(instance or "").strip()
                    if token_text and instance_text:
                        parsed[token_text] = instance_text
        except Exception:
            log.warning("ROOMDOO_TOKEN_INSTANCE_MAP en formato JSON inválido; se ignora.")
        return parsed

    for part in raw.split(","):
        chunk = (part or "").strip()
        if not chunk or "=" not in chunk:
            continue
        instance_id, token = chunk.split("=", 1)
        instance_text = str(instance_id or "").strip()
        token_text = str(token or "").strip()
        if not instance_text or not token_text:
            continue
        parsed[token_text] = instance_text
    return parsed


def _verify_bearer(auth_header: Optional[str] = Header(None, alias="Authorization")) -> Dict[str, Optional[str]]:
    """Verifica Bearer Token y, si aplica, resuelve instance_id desde mapa token->instancia."""
    if not auth_header or not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Autenticacion Bearer requerida")

    token = auth_header.split(" ", 1)[1].strip()
    token_map = _parse_token_instance_map()
    if token_map:
        instance_id = token_map.get(token)
        if not instance_id:
            raise HTTPException(status_code=403, detail="Token invalido")
        return {"token": token, "instance_id": instance_id}

    expected = (Settings.ROOMDOO_BEARER_TOKEN or "").strip()
    if not expected:
        log.error("ROOMDOO_BEARER_TOKEN/ROOMDOO_TOKEN_INSTANCE_MAP no configurado.")
        raise HTTPException(status_code=401, detail="Token de integracion no configurado")
    if token != expected:
        raise HTTPException(status_code=403, detail="Token invalido")
    return {"token": token, "instance_id": None}


def _instance_chat_sets(
    instance_id: str,
    channel: str,
    property_id: Optional[str | int],
) -> Tuple[set[str], set[str]]:
    cache_key = f"{str(instance_id or '').strip()}|{str(channel or '').strip()}|{str(property_id)}"
    cached = _instance_chat_sets_cache.get(cache_key)
    now_ts = time.time()
    if cached:
        cached_at, cached_chats, cached_originals = cached
        if now_ts - cached_at <= _INSTANCE_CHAT_SETS_TTL_SECONDS:
            return set(cached_chats), set(cached_originals)
        _instance_chat_sets_cache.pop(cache_key, None)

    chat_ids: set[str] = set()
    original_chat_ids: set[str] = set()
    original_prefixes: set[str] = {str(instance_id or "").strip()}
    try:
        instance_payload = fetch_instance_by_code(str(instance_id).strip()) or {}
        instance_number = _resolve_instance_number(instance_payload)
        if instance_number:
            original_prefixes.add(str(instance_number).strip())
            clean_number = _clean_chat_id(instance_number)
            if clean_number:
                original_prefixes.add(clean_number)
        phone_id = str(instance_payload.get("whatsapp_phone_id") or "").strip()
        if phone_id:
            original_prefixes.add(phone_id)
    except Exception as exc:
        log.warning("No se pudo resolver payload de instancia %s: %s", instance_id, exc)

    try:
        query = (
            supabase.table(Settings.CHAT_RESERVATIONS_TABLE)
            .select("chat_id, original_chat_id")
            .eq("instance_id", instance_id)
        )
        if property_id is not None:
            query = query.eq("property_id", property_id)
        rows = (query.limit(2000).execute().data or [])
        for row in rows:
            chat = _clean_chat_id(str(row.get("chat_id") or ""))
            original = str(row.get("original_chat_id") or "").strip()
            if chat:
                chat_ids.add(chat)
            if original:
                original_chat_ids.add(original)
                tail = _clean_chat_id(original.split(":")[-1])
                if tail:
                    chat_ids.add(tail)
    except Exception as exc:
        log.warning("No se pudo cargar chat_reservations por instancia %s: %s", instance_id, exc)

    for prefix in [p for p in original_prefixes if p]:
        try:
            query = (
                supabase.table("chat_history")
                .select("conversation_id, original_chat_id")
                .eq("channel", channel)
                .like("original_chat_id", f"{prefix}:%")
            )
            if property_id is not None:
                query = query.eq("property_id", property_id)
            rows = (query.limit(3000).execute().data or [])
            for row in rows:
                cid = _clean_chat_id(str(row.get("conversation_id") or ""))
                original = str(row.get("original_chat_id") or "").strip()
                if cid:
                    chat_ids.add(cid)
                if original:
                    original_chat_ids.add(original)
        except Exception as exc:
            log.warning(
                "No se pudo cargar chat_history por instancia %s prefijo %s: %s",
                instance_id,
                prefix,
                exc,
            )

    _instance_chat_sets_cache[cache_key] = (time.time(), set(chat_ids), set(original_chat_ids))
    return chat_ids, original_chat_ids


def _clean_chat_id(chat_id: str) -> str:
    return re.sub(r"\D", "", str(chat_id or "")).strip()


def _is_plausible_whatsapp_chat_id(chat_id: str) -> bool:
    digits = _clean_chat_id(chat_id)
    if not digits:
        return False
    if len(digits) < 8 or len(digits) > 15:
        return False
    if not phonenumbers:
        return True

    raw = str(chat_id or "").strip()
    candidate = raw if raw.startswith("+") else f"+{digits}"
    try:
        parsed = phonenumbers.parse(candidate, None)
    except NumberParseException:
        return False

    if not phonenumbers.is_possible_number(parsed):
        return False
    if not phonenumbers.is_valid_number(parsed):
        return False
    try:
        if phonenumbers.number_type(parsed) == phonenumbers.PhoneNumberType.FIXED_LINE:
            return False
    except Exception:
        pass
    return True


def _extract_guest_phone(chat_id: str) -> str:
    raw = str(chat_id or "").strip()
    if ":" in raw:
        raw = raw.split(":")[-1]
    clean = _clean_chat_id(raw)
    return clean or raw


def _to_international_phone(phone: str) -> Optional[str]:
    raw = str(phone or "").strip()
    if not raw:
        return None
    if raw.startswith("+"):
        clean = _clean_chat_id(raw)
        return f"+{clean}" if clean else raw
    if raw.startswith("00"):
        clean = _clean_chat_id(raw[2:])
        return f"+{clean}" if clean else None
    clean = _clean_chat_id(raw)
    if not clean:
        return None
    return f"+{clean}"


def _normalize_property_id(value: Optional[str]) -> Optional[str | int]:
    if value is None:
        return None
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    return text or None


def _normalize_user_id(value: Optional[int | str]) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _escalation_status(escalation: Dict[str, Any]) -> str:
    explicit = str((escalation or {}).get("status") or "").strip().lower()
    if explicit in {"pending", "resolved"}:
        return explicit
    if is_escalation_resolved(escalation):
        return "resolved"
    return "pending"


def _build_escalation_resolution_payload(
    chat_id: str,
    escalation: Dict[str, Any],
    *,
    fallback_property_id: Optional[str | int] = None,
) -> Dict[str, Any]:
    row = escalation or {}
    notes_raw = row.get("resolution_notes")
    if notes_raw is None:
        notes = ""
    elif isinstance(notes_raw, str):
        notes = notes_raw
    else:
        notes = str(notes_raw)
    resolved_by = row.get("resolved_by")
    if resolved_by is not None and not str(resolved_by).strip():
        resolved_by = None
    resolved_by_name = row.get("resolved_by_name")
    if resolved_by_name is not None and not str(resolved_by_name).strip():
        resolved_by_name = None
    resolved_by_email = row.get("resolved_by_email")
    if resolved_by_email is not None and not str(resolved_by_email).strip():
        resolved_by_email = None
    resolved_at = row.get("resolved_at") or row.get("updated_at") or row.get("timestamp")
    property_id = row.get("property_id")
    if property_id is None:
        property_id = fallback_property_id
    return {
        "chat_id": chat_id,
        "escalation_id": str(row.get("escalation_id") or "").strip() or None,
        "property_id": property_id,
        "status": _escalation_status(row),
        "resolved_at": resolved_at,
        "resolution_medium": row.get("resolution_medium"),
        "resolution_notes": notes,
        "resolved_by": resolved_by,
        "resolved_by_name": resolved_by_name,
        "resolved_by_email": resolved_by_email,
    }


def _chat_exists_in_history(
    chat_id: str,
    *,
    property_id: Optional[str | int] = None,
    channel: str = "whatsapp",
) -> bool:
    decoded_id = str(chat_id or "").strip()
    clean_id = _clean_chat_id(decoded_id) or decoded_id
    id_candidates = {clean_id}
    if decoded_id and decoded_id != clean_id:
        id_candidates.add(decoded_id)
    if ":" in decoded_id:
        tail = decoded_id.split(":")[-1].strip()
        tail_clean = _clean_chat_id(tail) or tail
        if tail_clean:
            id_candidates.add(tail_clean)
    like_patterns = {f"%:{candidate}" for candidate in id_candidates if candidate}
    or_filters = [f"conversation_id.eq.{candidate}" for candidate in id_candidates if candidate]
    or_filters += [f"conversation_id.like.{pattern}" for pattern in like_patterns]
    try:
        query = supabase.table("chat_history").select("conversation_id").eq("channel", channel)
        if property_id is not None:
            query = query.eq("property_id", property_id)
        if or_filters:
            query = query.or_(",".join(or_filters))
        rows = query.limit(1).execute().data or []
        return bool(rows)
    except Exception:
        return False


def _map_sender(role: str) -> str:
    role = (role or "").lower()
    if role in {"guest", "bookai", "system", "tool"}:
        return role
    if role in {"user", "hotel", "staff"}:
        return "user"
    if role in {"assistant", "ai"}:
        return "bookai"
    return "bookai"


def _format_history_content(content: str) -> str:
    """Normaliza content del historial para render en frontend."""
    text = str(content or "")
    text = text.replace("\r\n", "\n")
    text = re.sub(r"\s*\|\s*", ";", text)
    text = re.sub(r"\s*;\s*", ";", text)
    return text


def _normalize_pending_key(guest_id: str) -> str:
    raw = str(guest_id or "").strip()
    if not raw:
        return ""
    if ":" in raw:
        left, right = raw.rsplit(":", 1)
        left_clean = _clean_chat_id(left) or left.strip()
        right_clean = _clean_chat_id(right) or right.strip()
        return f"{left_clean}:{right_clean}".strip(":")
    clean = _clean_chat_id(raw)
    if clean:
        return clean
    return raw


def _normalize_pending_property(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _pending_compound_key(guest_chat_id: str, property_id: Any) -> str:
    guest_key = _normalize_pending_key(guest_chat_id)
    prop_key = _normalize_pending_property(property_id)
    return f"{guest_key}|{prop_key or '*'}"


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


def _extract_reservation_client_name(params: Dict[str, Any]) -> Optional[str]:
    if not params:
        return None
    for key in (
        "client_name",
        "clientName",
        "guest_name",
        "guestName",
        "partner_name",
        "partnerName",
        "full_name",
        "fullName",
        "name_guest",
        "guest",
        "titular",
    ):
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


def _extract_client_name_from_reservation(payload: Dict[str, Any]) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in ("partner_name", "partnerName", "client_name", "clientName", "guest_name", "guestName"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    reservations = payload.get("reservations") or payload.get("reservation") or []
    if isinstance(reservations, dict):
        reservations = [reservations]
    if isinstance(reservations, list):
        for item in reservations:
            if not isinstance(item, dict):
                continue
            for key in ("partner_name", "partnerName", "client_name", "clientName", "guest_name", "guestName"):
                val = item.get(key)
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


def _sanitize_guest_outgoing_text(text: str) -> str:
    """Elimina marcadores internos (p.ej. [esc_xxx]) antes de enviar al huésped."""
    raw = (text or "").strip()
    if not raw:
        return ""
    clean_lines: List[str] = []
    for line in raw.splitlines():
        current = line.strip()
        # Quita prefijo tipo: "1. [esc_...]" o "[esc_...]"
        current = re.sub(r"^\s*\d+\.\s*\[esc_[^\]]+\]\s*", "", current, flags=re.IGNORECASE)
        current = re.sub(r"^\s*\[esc_[^\]]+\]\s*", "", current, flags=re.IGNORECASE)
        # Si no venía con índice, elimina marcadores internos residuales.
        current = re.sub(r"\s*\[esc_[^\]]+\]\s*", " ", current, flags=re.IGNORECASE)
        current = re.sub(r"\s{2,}", " ", current).strip()
        if current:
            clean_lines.append(current)
    return "\n".join(clean_lines).strip()


def _is_internal_hidden_message(text: str, *, hide_template_sent: bool = True) -> bool:
    content = (text or "").strip()
    if not content:
        return False
    lowered = content.lower()
    if lowered.startswith("[superintendente]"):
        return True
    if hide_template_sent and lowered.startswith("[template_sent]"):
        return True
    if lowered.startswith("contexto de propiedad actualizado"):
        return True
    # Oculta trazas internas/auditoría que no son mensajes para operador/huésped.
    if lowered.startswith("salida modelo:"):
        return True
    if "api debug" in lowered:
        return True
    if "sender (api):" in lowered and "chat id:" in lowered:
        return True
    return False


def _pending_by_chat(limit: int = 200, property_id: Optional[str | int] = None) -> Dict[str, List[Dict[str, Any]]]:
    """Agrupa pendientes por chat+property para evitar cruces entre hoteles."""
    pending = list_pending_escalations(limit=limit, property_id=property_id) or []
    if property_id is not None:
        # Compatibilidad: muchas escalaciones históricas quedaron con property_id=NULL.
        # En vistas filtradas por propiedad, las incluimos y luego filtramos por instancia/chat permitido.
        pending_null_prop = list_pending_escalations(limit=limit, property_id=None) or []
        if pending_null_prop:
            seen_ids = {
                str((esc or {}).get("escalation_id") or "").strip()
                for esc in pending
                if str((esc or {}).get("escalation_id") or "").strip()
            }
            for esc in pending_null_prop:
                esc_id = str((esc or {}).get("escalation_id") or "").strip()
                if esc_id and esc_id in seen_ids:
                    continue
                pending.append(esc)
                if esc_id:
                    seen_ids.add(esc_id)
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for esc in pending:
        guest_id = _normalize_pending_key(esc.get("guest_chat_id"))
        if not guest_id:
            continue
        key = _pending_compound_key(guest_id, esc.get("property_id"))
        grouped.setdefault(key, []).append(esc)
    for key, escs in grouped.items():
        grouped[key] = sorted(escs, key=lambda e: _parse_ts(e.get("timestamp")) or datetime.min)
    return grouped


def _instance_prefixes(instance_id: Optional[str]) -> set[str]:
    prefixes: set[str] = set()
    normalized = str(instance_id or "").strip()
    if not normalized:
        return prefixes
    prefixes.add(normalized)
    try:
        payload = fetch_instance_by_code(normalized) or {}
        instance_number = _resolve_instance_number(payload)
        if instance_number:
            prefixes.add(str(instance_number).strip())
            clean_number = _clean_chat_id(instance_number)
            if clean_number:
                prefixes.add(clean_number)
        phone_id = str(payload.get("whatsapp_phone_id") or "").strip()
        if phone_id:
            prefixes.add(phone_id)
    except Exception:
        pass
    return {p for p in prefixes if p}


def _filter_pending_by_instance(
    grouped: Dict[str, List[Dict[str, Any]]],
    instance_id: Optional[str],
    allowed_chat_ids: Optional[set[str]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    if not instance_id:
        return grouped
    prefixes = _instance_prefixes(instance_id)
    if not prefixes:
        return grouped

    filtered: Dict[str, List[Dict[str, Any]]] = {}
    for key, escs in grouped.items():
        kept: List[Dict[str, Any]] = []
        for esc in escs:
            guest_chat_id = str((esc or {}).get("guest_chat_id") or "").strip()
            if not guest_chat_id:
                continue
            if ":" not in guest_chat_id:
                # Compatibilidad: escalaciones legacy pueden venir sin prefijo de instancia.
                # Si el chat está dentro del conjunto permitido de la instancia, se conserva.
                guest_clean = _clean_chat_id(guest_chat_id)
                if allowed_chat_ids and guest_clean and guest_clean in allowed_chat_ids:
                    kept.append(esc)
                continue
            head = guest_chat_id.split(":", 1)[0].strip()
            head_clean = _clean_chat_id(head)
            if head in prefixes or (head_clean and head_clean in prefixes):
                kept.append(esc)
        if kept:
            filtered[key] = kept
    return filtered


def _join_pending_values(values: List[str]) -> Optional[str]:
    clean = [v.strip() for v in values if isinstance(v, str) and v.strip()]
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    return "\n".join(f"{idx}. {text}" for idx, text in enumerate(clean, start=1))


def _latest_pending(escs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not escs:
        return None
    return escs[-1]


def _pending_actions(grouped: Dict[str, List[Dict[str, Any]]], memory_manager: Any = None) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for guest_id, escs in grouped.items():
        latest = _latest_pending(escs) or {}
        action_text = (
            (latest.get("escalation_reason") or latest.get("reason") or "").strip()
            or (latest.get("guest_message") or "").strip()
        )
        if not action_text:
            continue
        guest_lang = _resolve_guest_lang(latest, memory_manager=memory_manager)
        question_es = action_text
        if guest_lang != "es":
            try:
                question_es = (
                    language_manager.translate_if_needed(action_text, guest_lang, "es").strip()
                    or action_text
                )
            except Exception:
                question_es = action_text
        result[guest_id] = f"El huésped solicita: {question_es}"
    return result


def _resolve_guest_lang(latest: Dict[str, Any], memory_manager: Any = None) -> str:
    guest_chat_id = str(
        latest.get("guest_chat_id")
        or latest.get("chat_id")
        or latest.get("conversation_id")
        or ""
    ).strip()
    candidate_keys = [guest_chat_id, _clean_chat_id(guest_chat_id)]
    for key in [k for k in candidate_keys if k]:
        try:
            if memory_manager:
                value = memory_manager.get_flag(key, "guest_lang")
                if value:
                    return str(value).strip().lower()
        except Exception:
            pass
    sample = (latest.get("guest_message") or "").strip()
    if sample:
        try:
            return (language_manager.detect_language(sample, prev_lang="es") or "es").strip().lower()
        except Exception:
            pass
    return "es"


def _pending_reasons(grouped: Dict[str, List[Dict[str, Any]]], memory_manager: Any = None) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for guest_id, escs in grouped.items():
        latest = _latest_pending(escs) or {}
        reason = (latest.get("escalation_reason") or latest.get("reason") or "").strip()
        if reason:
            result[guest_id] = reason
    return result


def _pending_types(grouped: Dict[str, List[Dict[str, Any]]]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for guest_id, escs in grouped.items():
        latest = _latest_pending(escs) or {}
        esc_type = (latest.get("escalation_type") or latest.get("type") or "").strip()
        if esc_type:
            result[guest_id] = esc_type
    return result


def _pending_responses(grouped: Dict[str, List[Dict[str, Any]]]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for guest_id, escs in grouped.items():
        latest = _latest_pending(escs) or {}
        proposed = (latest.get("draft_response") or "").strip()
        if proposed:
            result[guest_id] = proposed
    return result


def _pending_messages(grouped: Dict[str, List[Dict[str, Any]]]) -> Dict[str, list]:
    result: Dict[str, list] = {}
    for guest_id, escs in grouped.items():
        latest = _latest_pending(escs) or {}
        esc_id = str(latest.get("escalation_id") or "").strip()
        messages = latest.get("messages")
        if not isinstance(messages, list):
            continue
        enriched_messages: list = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            enriched = dict(msg)
            if esc_id:
                enriched["escalation_id"] = esc_id
            enriched_messages.append(enriched)
        result[guest_id] = sorted(
            enriched_messages,
            key=lambda m: _parse_ts(m.get("timestamp")) or datetime.min,
        )
    return result


def _pending_property_for_guest(
    grouped: Dict[str, List[Dict[str, Any]]],
    guest_chat_id: str,
) -> Optional[str | int]:
    """Si existe una única property en pendientes para el huésped, devuélvela."""
    guest_key = _normalize_pending_key(guest_chat_id)
    guest_tail = _clean_chat_id(str(guest_key).split(":")[-1]) if guest_key else ""
    if not guest_key and not guest_tail:
        return None
    matches: List[str] = []
    for key in grouped.keys():
        key_text = str(key or "")
        guest_part, _, _ = key_text.partition("|")
        guest_part = _normalize_pending_key(guest_part)
        guest_part_tail = _clean_chat_id(str(guest_part).split(":")[-1]) if guest_part else ""
        if guest_key and guest_part == guest_key:
            matches.append(key_text)
            continue
        if guest_tail and guest_part_tail and guest_part_tail == guest_tail:
            matches.append(key_text)
    if not matches:
        return None
    prop_values: set[str] = set()
    for key in matches:
        _, _, prop = str(key).partition("|")
        prop_clean = (prop or "").strip()
        if not prop_clean or prop_clean == "*":
            continue
        prop_values.add(prop_clean)
    if len(prop_values) != 1:
        return None
    value = next(iter(prop_values))
    if str(value).isdigit():
        return int(value)
    return value


def _pending_snapshot_for_chat(
    chat_id: str,
    property_id: Optional[str | int],
    instance_id: Optional[str] = None,
    memory_manager: Any = None,
) -> Dict[str, Any]:
    """Estado consolidado de la última escalación pendiente para un chat."""
    pending = list_pending_escalations_for_chat(
        chat_id,
        limit=100,
        property_id=property_id,
    ) or []
    if pending and instance_id:
        key = _pending_compound_key(chat_id, property_id)
        grouped = _filter_pending_by_instance(
            {key: pending},
            instance_id=instance_id,
            allowed_chat_ids={_clean_chat_id(chat_id)} if _clean_chat_id(chat_id) else None,
        )
        pending = grouped.get(key) or []
    if not pending:
        return {
            "needs_action": None,
            "needs_action_type": None,
            "needs_action_reason": None,
            "proposed_response": None,
            "is_final_response": False,
            "escalation_messages": None,
        }

    key = _pending_compound_key(chat_id, property_id)
    grouped = {key: pending}
    pending_map = _pending_actions(grouped, memory_manager=memory_manager)
    pending_reason_map = _pending_reasons(grouped, memory_manager=memory_manager)
    pending_type_map = _pending_types(grouped)
    proposed_map = _pending_responses(grouped)
    pending_messages_map = _pending_messages(grouped)
    proposed = proposed_map.get(key)
    return {
        "needs_action": pending_map.get(key),
        "needs_action_type": pending_type_map.get(key),
        "needs_action_reason": pending_reason_map.get(key),
        "proposed_response": proposed,
        "is_final_response": bool(proposed),
        "escalation_messages": pending_messages_map.get(key),
    }


def _pending_value_with_fallback(mapping: Dict[str, Any], chat_id: str, property_id: Any) -> Any:
    """Busca valor por key exacta; solo usa fallback legacy si el chat no está acotado a una property."""
    if not isinstance(mapping, dict):
        return None
    exact = _pending_compound_key(chat_id, property_id)
    if exact in mapping and mapping.get(exact) is not None:
        return mapping.get(exact)
    target_prop = _normalize_pending_property(property_id)
    if target_prop is None:
        legacy = _pending_compound_key(chat_id, None)
        if legacy in mapping and mapping.get(legacy) is not None:
            return mapping.get(legacy)

    # Compat: escalaciones pueden guardarse con guest_chat_id compuesto
    # (instancia:telefono) mientras el chatter lista por telefono limpio.
    chat_norm = _normalize_pending_key(chat_id)
    chat_tail = _clean_chat_id(str(chat_norm).split(":")[-1]) if chat_norm else ""

    candidate_any_prop = None
    for key, value in mapping.items():
        if value is None:
            continue
        guest_part, _, prop_part = str(key or "").partition("|")
        guest_norm = _normalize_pending_key(guest_part)
        guest_tail = _clean_chat_id(str(guest_norm).split(":")[-1]) if guest_norm else ""
        if chat_norm and guest_norm == chat_norm:
            if target_prop and prop_part == target_prop:
                return value
            if not target_prop and (not prop_part or prop_part == "*"):
                return value
            if not target_prop and candidate_any_prop is None:
                candidate_any_prop = value
            continue
        if chat_tail and guest_tail and guest_tail == chat_tail:
            if target_prop and prop_part == target_prop:
                return value
            if not target_prop and (not prop_part or prop_part == "*"):
                return value
            if not target_prop and candidate_any_prop is None:
                candidate_any_prop = value

    return candidate_any_prop


def _strip_draft_instruction_block(text: str) -> str:
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


def _compact_ai_draft(text: str, max_chars: int = 380, max_sentences: int = 3) -> str:
    """Compacta borradores para que la sugerencia sea breve y legible en Chatter."""
    raw = (text or "").strip()
    if not raw:
        return ""
    raw = re.sub(r"^\s*\d+\.\s*\[esc_[^\]]+\]\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"^\s*\[esc_[^\]]+\]\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*\[esc_[^\]]+\]\s*", " ", raw, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+", " ", raw).strip()
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", normalized) if s.strip()]
    if len(sentences) > max_sentences:
        normalized = " ".join(sentences[:max_sentences]).strip()
    if len(normalized) > max_chars:
        clipped = normalized[:max_chars].rsplit(" ", 1)[0].strip()
        normalized = (clipped or normalized[:max_chars].strip()).rstrip(".,;:") + "..."
    return normalized


def _pending_escalations_summary(escalations: List[Dict[str, Any]]) -> str:
    if not escalations:
        return "No hay escalaciones pendientes."
    lines = []
    for idx, esc in enumerate(escalations, start=1):
        guest_message = (esc.get("guest_message") or "").strip() or "No disponible"
        esc_type = (esc.get("escalation_type") or esc.get("type") or "").strip() or "No disponible"
        reason = (esc.get("escalation_reason") or esc.get("reason") or "").strip() or "No disponible"
        lines.append(
            f"{idx}. Tipo: {esc_type} | Motivo: {reason} | Mensaje huésped: {guest_message}"
        )
    return "\n".join(lines)


def _bookai_settings(state) -> Dict[str, bool]:
    load_tracking = getattr(state, "load_tracking", None)
    if callable(load_tracking):
        try:
            load_tracking()
        except Exception as exc:
            log.debug("No se pudo recargar tracking en chatter_routes: %s", exc)
    settings = state.tracking.setdefault("bookai_enabled", {})
    if not isinstance(settings, dict):
        state.tracking["bookai_enabled"] = {}
        settings = state.tracking["bookai_enabled"]
    return settings


def _bookai_flag_keys(chat_id: str, property_id: Any = None, instance_id: Optional[str] = None) -> list[str]:
    clean_id = _clean_chat_id(chat_id) or str(chat_id or "").strip()
    prop = _normalize_property_id(property_id)
    inst = str(instance_id or "").strip()
    keys: list[str] = []
    if inst and clean_id and prop is not None:
        keys.append(f"{inst}|{clean_id}:{prop}")
    if inst and clean_id:
        keys.append(f"{inst}|{clean_id}")
    if not inst and clean_id and prop is not None:
        keys.append(f"{clean_id}:{prop}")
    if not inst and clean_id:
        keys.append(clean_id)
    return keys


def _bookai_flag_value(
    state,
    *,
    chat_id: str,
    property_id: Any = None,
    instance_id: Optional[str] = None,
    default: bool = True,
) -> bool:
    settings = _bookai_settings(state)
    resolution = _bookai_flag_resolution(
        settings,
        aliases=_related_memory_ids(state, chat_id) or [],
        chat_id=chat_id,
        property_id=property_id,
        instance_id=instance_id,
        default=default,
    )
    return bool(resolution["value"])


def _parse_bookai_flag(raw: Any) -> Optional[bool]:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    return None


def _bookai_flag_resolution(
    settings: Dict[str, Any],
    *,
    aliases: list[str],
    chat_id: str,
    property_id: Any = None,
    instance_id: Optional[str] = None,
    default: bool = True,
) -> Dict[str, Any]:
    raw_chat_id = str(chat_id or "").strip()
    alias_ids = list(aliases or [])
    if raw_chat_id:
        alias_ids.append(raw_chat_id)
    clean_chat_id = _clean_chat_id(raw_chat_id)
    if clean_chat_id:
        alias_ids.append(clean_chat_id)
    prop = _normalize_property_id(property_id)
    inst = str(instance_id or "").strip()

    dedup_aliases: list[str] = []
    seen_aliases: set[str] = set()
    for alias in alias_ids:
        normalized = str(alias or "").strip()
        if not normalized or normalized in seen_aliases:
            continue
        seen_aliases.add(normalized)
        dedup_aliases.append(normalized)

    for alias in dedup_aliases:
        for key in _bookai_flag_keys(alias, property_id=property_id, instance_id=instance_id):
            if key in settings:
                parsed = _parse_bookai_flag(settings.get(key))
                if parsed is not None:
                    return {
                        "value": parsed,
                        "source": "exact",
                        "matched_key": key,
                        "aliases": dedup_aliases,
                    }

    false_found = False
    true_found = False
    matched_prefixes: list[str] = []
    for alias in dedup_aliases:
        clean_alias = _clean_chat_id(alias) or alias
        if not clean_alias:
            continue
        prefixes: list[str] = []
        if inst and prop is not None:
            prefixes.append(f"{inst}|{clean_alias}:{prop}")
        if inst:
            prefixes.append(f"{inst}|{clean_alias}")
        if not inst and prop is not None:
            prefixes.append(f"{clean_alias}:{prop}")
        if not inst:
            prefixes.append(clean_alias)
        for prefix in prefixes:
            for key, value in settings.items():
                if not str(key).startswith(prefix):
                    continue
                parsed = _parse_bookai_flag(value)
                if parsed is None:
                    continue
                matched_prefixes.append(str(key))
                if parsed is False:
                    false_found = True
                else:
                    true_found = True
    if false_found:
        return {
            "value": False,
            "source": "prefix_false",
            "matched_key": matched_prefixes[0] if matched_prefixes else None,
            "aliases": dedup_aliases,
            "matched_keys": matched_prefixes,
        }
    if true_found:
        return {
            "value": True,
            "source": "prefix_true",
            "matched_key": matched_prefixes[0] if matched_prefixes else None,
            "aliases": dedup_aliases,
            "matched_keys": matched_prefixes,
        }
    return {
        "value": default,
        "source": "default",
        "matched_key": None,
        "aliases": dedup_aliases,
    }


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


def _to_utc_z(value: datetime) -> str:
    dt = value.astimezone(timezone.utc).replace(microsecond=0)
    return dt.isoformat().replace("+00:00", "Z")


def _build_whatsapp_window(
    last_guest_message_at: Optional[str],
    last_template_sent_at: Optional[str] = None,
) -> Dict[str, Any]:
    last_dt = _parse_ts(last_guest_message_at)
    if last_dt and last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    last_template_dt = _parse_ts(last_template_sent_at)
    if last_template_dt and last_template_dt.tzinfo is None:
        last_template_dt = last_template_dt.replace(tzinfo=timezone.utc)
    if last_template_dt and (not last_dt or last_template_dt > last_dt):
        return {
            "status": "waiting_for_reply",
            "remaining_hours": 0.0,
            "expires_at": None,
        }
    if not last_dt:
        return {
            "status": "expired",
            "remaining_hours": 0.0,
            "expires_at": None,
        }
    now = datetime.now(timezone.utc)
    expires_at = last_dt + timedelta(hours=24)
    remaining_hours = (expires_at - now).total_seconds() / 3600.0
    if remaining_hours <= 0:
        return {
            "status": "expired",
            "remaining_hours": 0.0,
            "expires_at": _to_utc_z(expires_at),
        }
    return {
        "status": "active" if remaining_hours > 8 else "expiring",
        "remaining_hours": round(remaining_hours, 2),
        "expires_at": _to_utc_z(expires_at),
    }


def _chat_history_identity_filters(chat_id: str, original_chat_id: Optional[str] = None) -> List[str]:
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
    filters = [f"conversation_id.eq.{candidate}" for candidate in id_candidates if candidate]
    filters += [f"conversation_id.like.{pattern}" for pattern in like_patterns]
    original_clean = str(original_chat_id or "").strip()
    if original_clean:
        filters.append(f"original_chat_id.eq.{original_clean}")
    return filters


def _resolve_last_guest_message_at(
    chat_id: str,
    *,
    property_id: Optional[str | int] = None,
    channel: str = "whatsapp",
    original_chat_id: Optional[str] = None,
) -> Optional[str]:
    filters = _chat_history_identity_filters(chat_id, original_chat_id)
    if not filters:
        return None
    try:
        query = (
            supabase.table("chat_history")
            .select("created_at")
            .eq("channel", str(channel or "whatsapp").strip() or "whatsapp")
            .in_("role", ["guest"])
        )
        if property_id is not None:
            query = query.eq("property_id", property_id)
        rows = query.or_(",".join(filters)).order("created_at", desc=True).limit(1).execute().data or []
        return rows[0].get("created_at") if rows else None
    except Exception:
        return None


def _resolve_last_template_sent_at(
    chat_id: str,
    *,
    property_id: Optional[str | int] = None,
    channel: str = "whatsapp",
    original_chat_id: Optional[str] = None,
) -> Optional[str]:
    filters = _chat_history_identity_filters(chat_id, original_chat_id)
    if not filters:
        return None
    try:
        query = (
            supabase.table("chat_history")
            .select("created_at")
            .eq("channel", str(channel or "whatsapp").strip() or "whatsapp")
            .eq("role", "bookai")
            .like("content", "[TEMPLATE_SENT]%")
        )
        if property_id is not None:
            query = query.eq("property_id", property_id)
        rows = query.or_(",".join(filters)).order("created_at", desc=True).limit(1).execute().data or []
        return rows[0].get("created_at") if rows else None
    except Exception:
        return None


def _resolve_whatsapp_window_for_chat(
    chat_id: str,
    *,
    property_id: Optional[str | int] = None,
    channel: str = "whatsapp",
    original_chat_id: Optional[str] = None,
    last_guest_message_at: Optional[str] = None,
    last_template_sent_at: Optional[str] = None,
) -> Dict[str, Any]:
    guest_message_at = last_guest_message_at
    if not guest_message_at:
        guest_message_at = _resolve_last_guest_message_at(
            chat_id,
            property_id=property_id,
            channel=channel,
            original_chat_id=original_chat_id,
        )
    template_sent_at = last_template_sent_at
    if not template_sent_at:
        template_sent_at = _resolve_last_template_sent_at(
            chat_id,
            property_id=property_id,
            channel=channel,
            original_chat_id=original_chat_id,
        )
    return _build_whatsapp_window(guest_message_at, template_sent_at)


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


def _resolve_instance_number(instance_payload: Dict[str, Any]) -> Optional[str]:
    if not isinstance(instance_payload, dict):
        return None
    for key in ("display_phone_number", "whatsapp_number", "phone_number", "phone"):
        value = instance_payload.get(key)
        normalized = _clean_chat_id(str(value or ""))
        if normalized:
            return normalized
    return None


def _build_context_id_from_instance(state, chat_id: str, instance_id: Optional[str] = None) -> Optional[str]:
    memory_manager = getattr(state, "memory_manager", None)
    clean = _clean_chat_id(chat_id) or str(chat_id).strip()
    if not memory_manager or not clean or not instance_id:
        return None
    try:
        instance_payload = fetch_instance_by_code(str(instance_id).strip()) or {}
    except Exception:
        instance_payload = {}
    instance_number = _resolve_instance_number(instance_payload)
    if not instance_number:
        return None

    context_id = f"{instance_number}:{clean}"
    for target in (clean, str(chat_id).strip(), context_id):
        if not target:
            continue
        memory_manager.set_flag(target, "guest_number", clean)
        memory_manager.set_flag(target, "force_guest_role", True)
        memory_manager.set_flag(target, "last_memory_id", context_id)
        memory_manager.set_flag(target, "instance_number", instance_number)
        memory_manager.set_flag(target, "instance_id", str(instance_id).strip())
        memory_manager.set_flag(target, "instance_hotel_code", str(instance_id).strip())
        for key in ("whatsapp_phone_id", "whatsapp_token", "whatsapp_verify_token"):
            val = instance_payload.get(key)
            if val:
                memory_manager.set_flag(target, key, val)

    return context_id


def _resolve_whatsapp_context_id(
    state,
    chat_id: str,
    instance_id: Optional[str] = None,
) -> Optional[str]:
    """Resuelve el context_id (ej. instancia:telefono) para enrutar WhatsApp."""
    if not state or not chat_id:
        return None

    memory_manager = getattr(state, "memory_manager", None)
    clean = _clean_chat_id(chat_id) or str(chat_id).strip()

    def _matches_instance(mem_id: str) -> bool:
        if not mem_id:
            return False
        if not instance_id:
            return True
        if not memory_manager:
            return False
        try:
            mem_instance = (
                memory_manager.get_flag(mem_id, "instance_id")
                or memory_manager.get_flag(mem_id, "instance_hotel_code")
            )
        except Exception:
            mem_instance = None
        if mem_instance and str(mem_instance).strip() == str(instance_id).strip():
            return True
        if ":" in str(mem_id):
            prefix = str(mem_id).split(":", 1)[0].strip()
            built = _build_context_id_from_instance(state, chat_id, instance_id=instance_id)
            if built and prefix == str(built).split(":", 1)[0].strip():
                return True
        return False

    if memory_manager and clean:
        last_mem = memory_manager.get_flag(clean, "last_memory_id")
        if isinstance(last_mem, str) and last_mem.strip() and _matches_instance(last_mem.strip()):
            return last_mem.strip()

    related = _related_memory_ids(state, chat_id)
    for mem_id in related:
        if not isinstance(mem_id, str) or ":" not in mem_id:
            continue
        tail = mem_id.split(":")[-1]
        if (_clean_chat_id(tail) == clean or tail.strip() == clean) and _matches_instance(mem_id.strip()):
            if memory_manager and clean:
                memory_manager.set_flag(clean, "last_memory_id", mem_id.strip())
            return mem_id.strip()

    return _build_context_id_from_instance(state, chat_id, instance_id=instance_id)


def _normalize_language_confidence(value: Any, default: float = 0.0) -> float:
    try:
        confidence = float(value)
    except Exception:
        confidence = float(default)
    if confidence < 0.0:
        return 0.0
    if confidence > 1.0:
        return 1.0
    return confidence


def _resolve_guest_lang_meta_for_chat(
    state,
    chat_id: str,
    context_id: Optional[str] = None,
) -> Tuple[str, float]:
    """Resuelve idioma huésped + confianza priorizando flags de memoria y aliases."""
    memory_manager = getattr(state, "memory_manager", None)
    if not memory_manager:
        return "es", 0.0

    clean = _clean_chat_id(chat_id) or str(chat_id or "").strip()
    keys: list[str] = []
    if context_id:
        keys.append(str(context_id).strip())
    if chat_id:
        keys.append(str(chat_id).strip())
    if clean:
        keys.append(clean)
    keys.extend(_related_memory_ids(state, clean))

    seen = set()
    dedup_keys = []
    for key in keys:
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        dedup_keys.append(key)

    for key in dedup_keys:
        try:
            lang = memory_manager.get_flag(key, "guest_lang")
            if isinstance(lang, str) and lang.strip():
                confidence = _normalize_language_confidence(
                    memory_manager.get_flag(key, "guest_lang_confidence"),
                    default=1.0,
                )
                return lang.strip().lower(), confidence
        except Exception:
            continue

    for key in dedup_keys:
        try:
            history = memory_manager.get_memory(key, limit=40) or []
        except TypeError:
            try:
                history = memory_manager.get_memory(key) or []
            except Exception:
                history = []
        except Exception:
            history = []
        for msg in reversed(history):
            if not isinstance(msg, dict):
                continue
            if str(msg.get("role") or "").strip().lower() != "guest":
                continue
            sample = str(msg.get("content") or "").strip()
            if not sample:
                continue
            try:
                lang, confidence = language_manager.detect_language_with_confidence(
                    sample,
                    prev_lang=None,
                )
                resolved_lang = (lang or "es").strip().lower() or "es"
                resolved_conf = _normalize_language_confidence(confidence, default=0.0)
                for persist_key in dedup_keys:
                    try:
                        memory_manager.set_flag(persist_key, "guest_lang", resolved_lang)
                        memory_manager.set_flag(persist_key, "guest_lang_confidence", resolved_conf)
                    except Exception:
                        continue
                return resolved_lang, resolved_conf
            except Exception:
                return "es", 0.0

    return "es", 0.0


def _resolve_guest_lang_for_chat(state, chat_id: str, context_id: Optional[str] = None) -> str:
    """Resuelve idioma huésped priorizando flags de memoria y aliases del chat."""
    lang, _ = _resolve_guest_lang_meta_for_chat(state, chat_id, context_id=context_id)
    return lang


def _ensure_guest_language_for_outgoing(state, chat_id: str, text: str, context_id: Optional[str] = None) -> str:
    """Ajusta mensaje saliente al idioma del huésped para envíos manuales."""
    raw = (text or "").strip()
    if not raw:
        return ""
    guest_lang = _resolve_guest_lang_for_chat(state, chat_id, context_id=context_id)
    if guest_lang == "es":
        return raw
    try:
        return language_manager.ensure_language(raw, guest_lang).strip() or raw
    except Exception:
        return raw


# ---------------------------------------------------------------------------
# Registro de rutas
# ---------------------------------------------------------------------------
def register_chatter_routes(app, state) -> None:
    router = APIRouter(prefix="/api/v1/chatter", tags=["chatter"])

    def _chat_room_aliases(chat_id: str) -> list[str]:
        aliases: list[str] = []
        seen: set[str] = set()
        for candidate in _related_memory_ids(state, chat_id):
            raw = str(candidate or "").strip()
            if not raw:
                continue
            variants = [raw]
            clean = _clean_chat_id(raw)
            if clean:
                variants.append(clean)
            if ":" in raw:
                tail = raw.split(":")[-1].strip()
                if tail:
                    variants.append(tail)
                    tail_clean = _clean_chat_id(tail)
                    if tail_clean:
                        variants.append(tail_clean)
            for variant in variants:
                v = str(variant or "").strip()
                if not v or v in seen:
                    continue
                seen.add(v)
                aliases.append(v)
        return aliases

    def _rooms(chat_id: str, property_id: Optional[str | int], channel: str) -> list[str]:
        aliases = _chat_room_aliases(chat_id) or [chat_id]
        rooms = [f"chat:{alias}" for alias in aliases]
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

    def _restore_chat_visibility(
        chat_id: str,
        *,
        property_id: Optional[str | int],
        channel: str,
        original_chat_id: Optional[str] = None,
    ) -> bool:
        clean_id = _clean_chat_id(chat_id) or str(chat_id or "").strip()
        if not clean_id or property_id is None:
            return False

        restore_payload = {"archived_at": None, "hidden_at": None}
        current_channel = str(channel or "whatsapp").strip() or "whatsapp"
        original_clean = str(original_chat_id or "").replace("+", "").strip()

        try:
            if original_clean:
                (
                    supabase.table("chat_history")
                    .update(restore_payload)
                    .eq("original_chat_id", original_clean)
                    .eq("property_id", property_id)
                    .eq("channel", current_channel)
                    .execute()
                )
            (
                supabase.table("chat_history")
                .update(restore_payload)
                .eq("conversation_id", clean_id)
                .eq("property_id", property_id)
                .eq("channel", current_channel)
                .execute()
            )
            return True
        except Exception:
            return False

    @router.get("/chats")
    async def list_chats(
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        channel: str = Query(default="whatsapp"),
        property_id: Optional[str] = Query(default=None),
        auth_ctx: Dict[str, Optional[str]] = Depends(_verify_bearer),
    ):
        channel = (channel or "whatsapp").strip().lower()
        if channel not in {"whatsapp", "telegram"}:
            raise HTTPException(status_code=422, detail="Canal no soportado")
        property_id = _normalize_property_id(property_id)
        requested_page_size = 50
        instance_id = str((auth_ctx or {}).get("instance_id") or "").strip() or None
        allowed_chat_ids: Optional[set[str]] = None
        allowed_original_chat_ids: Optional[set[str]] = None
        if instance_id:
            chat_ids, original_chat_ids = _instance_chat_sets(instance_id, channel, property_id)
            allowed_chat_ids = chat_ids
            allowed_original_chat_ids = original_chat_ids
            if not allowed_chat_ids and not allowed_original_chat_ids:
                return {"page": page, "page_size": requested_page_size, "items": []}
        instance_whatsapp_phone_number: Optional[str] = None
        if instance_id:
            try:
                instance_payload = fetch_instance_by_code(instance_id) or {}
                instance_number = _resolve_instance_number(instance_payload)
                instance_whatsapp_phone_number = _to_international_phone(instance_number or "")
            except Exception:
                instance_whatsapp_phone_number = None

        target = page * requested_page_size
        batch_size = max(200, requested_page_size * 10)
        offset = 0
        ordered_keys: List[str] = []
        summaries: Dict[str, Dict[str, Any]] = {}


        while len(ordered_keys) < target:
            query = (
                supabase.table("chat_last_message")
                .select("conversation_id, original_chat_id, property_id, content, created_at, client_name, channel")
                .eq("channel", channel)
            )
            # En multi-instancia, algunos mensajes nuevos pueden no traer property_id aún.
            # Mantenemos aislamiento por instancia y dejamos pasar esos chats.
            if property_id is not None and not instance_id:
                query = query.eq("property_id", property_id)
            resp = query.order("created_at", desc=True).range(
                offset,
                offset + batch_size - 1,
            ).execute()
            rows = resp.data or []
            if not rows:
                break
            hidden_by_original: Dict[str, Dict[str, Any]] = {}
            hidden_by_chat_property: Dict[Tuple[str, str], Dict[str, Any]] = {}
            row_chat_ids = [
                str(row.get("conversation_id") or "").strip()
                for row in rows
                if str(row.get("conversation_id") or "").strip()
            ]
            if row_chat_ids:
                try:
                    hidden_query = (
                        supabase.table("chat_history")
                        .select("conversation_id, original_chat_id, property_id, archived_at, hidden_at")
                        .in_("conversation_id", list(dict.fromkeys(row_chat_ids)))
                        .eq("channel", channel)
                        .or_("archived_at.not.is.null,hidden_at.not.is.null")
                    )
                    if property_id is not None and not instance_id:
                        hidden_query = hidden_query.eq("property_id", property_id)
                    hidden_rows = hidden_query.order("created_at", desc=True).limit(5000).execute().data or []
                    for hidden_row in hidden_rows:
                        hidden_original_chat_id = str(hidden_row.get("original_chat_id") or "").strip()
                        hidden_conversation_id = str(hidden_row.get("conversation_id") or "").strip()
                        hidden_property_id = _normalize_property_id(hidden_row.get("property_id"))
                        if hidden_original_chat_id and hidden_original_chat_id not in hidden_by_original:
                            hidden_by_original[hidden_original_chat_id] = hidden_row
                        if hidden_conversation_id and hidden_property_id is not None:
                            hidden_key = (hidden_conversation_id, str(hidden_property_id).strip())
                            if hidden_key not in hidden_by_chat_property:
                                hidden_by_chat_property[hidden_key] = hidden_row
                except Exception:
                    pass
            for row in rows:
                cid = str(row.get("conversation_id") or "").strip()
                clean_cid = _clean_chat_id(cid)
                original_chat_id = str(row.get("original_chat_id") or "").strip()
                prop_id = _normalize_property_id(row.get("property_id"))
                if property_id is not None and not instance_id and prop_id is None:
                    continue
                if instance_id and allowed_chat_ids is not None and allowed_original_chat_ids is not None:
                    in_chat_set = bool(clean_cid and clean_cid in allowed_chat_ids)
                    in_original_set = bool(original_chat_id and original_chat_id in allowed_original_chat_ids)
                    # Evita mezcla entre instancias cuando el mismo teléfono existe en ambas:
                    # con original_chat_id presente, la validación fuerte debe ser por original.
                    if original_chat_id:
                        if not in_original_set:
                            continue
                    elif not in_chat_set and not in_original_set:
                        continue
                hidden_key = (cid, str(prop_id).strip()) if prop_id is not None else None
                if (
                    (original_chat_id and original_chat_id in hidden_by_original)
                    or (hidden_key and hidden_key in hidden_by_chat_property)
                ):
                    continue
                key = cid
                content = (row.get("content") or "").strip()
                if (
                    not cid
                    or key in summaries
                    or _is_internal_hidden_message(content, hide_template_sent=False)
                ):
                    continue
                ordered_keys.append(key)
                summaries[key] = row
                if len(ordered_keys) >= target:
                    break
            if len(rows) < batch_size:
                break
            offset += batch_size

        page_keys = ordered_keys[(page - 1) * requested_page_size:page * requested_page_size]
        pending_grouped = _pending_by_chat(property_id=property_id)
        pending_grouped = _filter_pending_by_instance(
            pending_grouped,
            instance_id=instance_id,
            allowed_chat_ids=allowed_chat_ids,
        )
        pending_map = _pending_actions(
            pending_grouped,
            memory_manager=getattr(state, "memory_manager", None),
        )
        pending_reason_map = _pending_reasons(
            pending_grouped,
            memory_manager=getattr(state, "memory_manager", None),
        )
        pending_type_map = _pending_types(pending_grouped)
        proposed_map = _pending_responses(pending_grouped)
        pending_messages_map = _pending_messages(pending_grouped)
        bookai_flags = _bookai_settings(state)
        client_names: Dict[str, str] = {}
        client_languages: Dict[str, Tuple[str, float]] = {}
        last_guest_message_at_by_cid: Dict[str, Optional[str]] = {}
        last_template_sent_at_by_cid: Dict[str, Optional[str]] = {}
        expected_original_by_cid: Dict[str, str] = {}
        if page_keys:
            conv_ids = [
                summaries[key].get("conversation_id")
                for key in page_keys
                if summaries.get(key) and summaries[key].get("conversation_id")
            ]
            for key in page_keys:
                summary_row = summaries.get(key) or {}
                cid = str(summary_row.get("conversation_id") or "").strip()
                if not cid:
                    continue
                expected_original_by_cid[cid] = str(summary_row.get("original_chat_id") or "").strip()
            if conv_ids:
                try:
                    base_query = (
                        supabase.table("chat_history")
                        .select("conversation_id, original_chat_id, client_name, content, created_at")
                        .in_("conversation_id", conv_ids)
                        .eq("channel", channel)
                        .in_("role", ["guest"])
                    )
                    resp_names_rows: List[Dict[str, Any]] = []
                    if property_id is not None and not instance_id:
                        resp_with_property = (
                            base_query
                            .eq("property_id", property_id)
                            .order("created_at", desc=True)
                            .limit(2000)
                            .execute()
                        )
                        resp_names_rows = resp_with_property.data or []
                        # Compatibilidad: algunos mensajes guest legacy no tienen property_id.
                        if not resp_names_rows:
                            resp_without_property = (
                                supabase.table("chat_history")
                                .select("conversation_id, original_chat_id, client_name, content, created_at")
                                .in_("conversation_id", conv_ids)
                                .eq("channel", channel)
                                .in_("role", ["guest"])
                                .order("created_at", desc=True)
                                .limit(2000)
                                .execute()
                            )
                            resp_names_rows = resp_without_property.data or []
                    else:
                        resp_without_property = (
                            base_query
                            .order("created_at", desc=True)
                            .limit(2000)
                            .execute()
                        )
                        resp_names_rows = resp_without_property.data or []
                    for row in resp_names_rows:
                        cid = str(row.get("conversation_id") or "").strip()
                        if not cid:
                            continue
                        row_original = str(row.get("original_chat_id") or "").strip()
                        expected_original = expected_original_by_cid.get(cid) or ""
                        if expected_original and row_original and row_original != expected_original:
                            continue
                        if cid not in last_guest_message_at_by_cid:
                            last_guest_message_at_by_cid[cid] = row.get("created_at")
                        name = row.get("client_name")
                        if cid and name and cid not in client_names:
                            client_names[cid] = name
                        if cid not in client_languages:
                            sample = str(row.get("content") or "").strip()
                            if not sample:
                                continue
                            try:
                                # Para el listado de chats priorizamos el idioma real del último mensaje guest,
                                # sin arrastre del idioma previo, para evitar falsos "es" en saludos tipo "hello".
                                lang, confidence = language_manager.detect_language_with_confidence(
                                    sample,
                                    prev_lang=None,
                                )
                                lang = (lang or "es").strip().lower() or "es"
                                confidence = _normalize_language_confidence(confidence, default=0.0)
                            except Exception:
                                lang = "es"
                                confidence = 0.0
                            client_languages[cid] = (lang, confidence)
                except Exception as exc:
                    log.warning("No se pudo cargar client_name/client_language: %s", exc)
                try:
                    template_query = (
                        supabase.table("chat_history")
                        .select("conversation_id, original_chat_id, created_at, content")
                        .in_("conversation_id", conv_ids)
                        .eq("channel", channel)
                        .eq("role", "bookai")
                        .like("content", "[TEMPLATE_SENT]%")
                    )
                    resp_template_rows: List[Dict[str, Any]] = []
                    if property_id is not None and not instance_id:
                        resp_with_property = (
                            template_query
                            .eq("property_id", property_id)
                            .order("created_at", desc=True)
                            .limit(2000)
                            .execute()
                        )
                        resp_template_rows = resp_with_property.data or []
                        if not resp_template_rows:
                            resp_without_property = (
                                supabase.table("chat_history")
                                .select("conversation_id, original_chat_id, created_at, content")
                                .in_("conversation_id", conv_ids)
                                .eq("channel", channel)
                                .eq("role", "bookai")
                                .like("content", "[TEMPLATE_SENT]%")
                                .order("created_at", desc=True)
                                .limit(2000)
                                .execute()
                            )
                            resp_template_rows = resp_without_property.data or []
                    else:
                        resp_without_property = (
                            template_query
                            .order("created_at", desc=True)
                            .limit(2000)
                            .execute()
                        )
                        resp_template_rows = resp_without_property.data or []
                    for row in resp_template_rows:
                        cid = str(row.get("conversation_id") or "").strip()
                        if not cid:
                            continue
                        row_original = str(row.get("original_chat_id") or "").strip()
                        expected_original = expected_original_by_cid.get(cid) or ""
                        if expected_original and row_original and row_original != expected_original:
                            continue
                        if cid not in last_template_sent_at_by_cid:
                            last_template_sent_at_by_cid[cid] = row.get("created_at")
                except Exception as exc:
                    log.warning("No se pudo cargar timestamps de plantilla whatsapp: %s", exc)

        items = []
        memory_manager = getattr(state, "memory_manager", None)
        for key in page_keys:
            last = summaries.get(key, {})
            cid = str(last.get("conversation_id") or "").strip()
            prop_id = last.get("property_id")
            if prop_id is None and cid:
                prop_id = _resolve_property_id_from_history(cid, channel)
            phone = _extract_guest_phone(cid)
            folio_id = None
            reservation_locator = None
            checkin = None
            checkout = None
            reservation_client_name = None
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
            # Siempre prioriza la reserva más próxima por checkin.
            try:
                # Si el endpoint viene filtrado por property_id, respeta ese filtro.
                # En listado global, usa todas las reservas del chat y prioriza la más próxima.
                reservation_property_filter = property_id if property_id is not None else None
                active = get_active_chat_reservation(chat_id=cid, property_id=reservation_property_filter)
                if active:
                    if prop_id is None and isinstance(active, dict):
                        prop_id = active.get("property_id") if active.get("property_id") is not None else prop_id
                    folio_id = active.get("folio_id") or folio_id
                    reservation_locator = active.get("reservation_locator") if isinstance(active, dict) else reservation_locator
                    checkin = active.get("checkin") or checkin
                    checkout = active.get("checkout") or checkout
                    reservation_client_name = active.get("client_name") if isinstance(active, dict) else None
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
            if prop_id is None and cid:
                pending_prop = _pending_property_for_guest(pending_grouped, cid)
                if pending_prop is not None:
                    prop_id = pending_prop
            bookai_resolution = _bookai_flag_resolution(
                _bookai_settings(state),
                aliases=_related_memory_ids(state, cid) or [],
                chat_id=cid,
                property_id=prop_id,
                instance_id=instance_id,
                default=True,
            )
            context_id = str(last.get("original_chat_id") or "").strip() or None
            client_language = "es"
            client_language_confidence = 0.0
            try:
                client_language, client_language_confidence = _resolve_guest_lang_meta_for_chat(
                    state,
                    cid,
                    context_id=context_id,
                )
            except Exception:
                client_language = "es"
                client_language_confidence = 0.0
            fallback_lang_meta = client_languages.get(cid)
            if (
                fallback_lang_meta
                and client_language == "es"
                and _normalize_language_confidence(client_language_confidence, default=0.0) <= 0.0
            ):
                client_language, client_language_confidence = fallback_lang_meta
            chat_channel = str(last.get("channel") or "whatsapp").strip() or "whatsapp"
            chat_channel_norm = chat_channel.lower()
            chat_payload = {
                "chat_id": cid,
                "property_id": prop_id,
                "reservation_id": folio_id,
                "reservation_locator": reservation_locator,
                "reservation_status": reservation_status,
                "room_number": room_number,
                "checkin": checkin,
                "checkout": checkout,
                "channel": chat_channel,
                "last_message": last.get("content"),
                "last_message_at": last.get("created_at"),
                "avatar": None,
                "client_name": reservation_client_name or client_names.get(cid) or last.get("client_name"),
                "client_language": client_language,
                "client_language_confidence": _normalize_language_confidence(
                    client_language_confidence,
                    default=0.0,
                ),
                "client_phone": phone or cid,
                "whatsapp_phone_number": instance_whatsapp_phone_number,
                "bookai_enabled": bool(bookai_resolution.get("value")),
                "unread_count": 0,
                "needs_action": _pending_value_with_fallback(pending_map, cid, prop_id),
                "needs_action_type": _pending_value_with_fallback(pending_type_map, cid, prop_id),
                "needs_action_reason": _pending_value_with_fallback(pending_reason_map, cid, prop_id),
                "proposed_response": _pending_value_with_fallback(proposed_map, cid, prop_id),
                "is_final_response": bool(_pending_value_with_fallback(proposed_map, cid, prop_id)),
                "escalation_messages": _pending_value_with_fallback(pending_messages_map, cid, prop_id),
                "folio_id": folio_id,
            }
            if chat_channel_norm == "whatsapp":
                last_guest_message_at = last_guest_message_at_by_cid.get(cid)
                if not last_guest_message_at:
                    last_guest_message_at = _resolve_last_guest_message_at(
                        cid,
                        property_id=prop_id,
                        channel=chat_channel,
                        original_chat_id=str(last.get("original_chat_id") or "").strip() or None,
                    )
                last_template_sent_at = last_template_sent_at_by_cid.get(cid)
                if not last_template_sent_at:
                    last_template_sent_at = _resolve_last_template_sent_at(
                        cid,
                        property_id=prop_id,
                        channel=chat_channel,
                        original_chat_id=str(last.get("original_chat_id") or "").strip() or None,
                    )
                chat_payload["whatsapp_window"] = _build_whatsapp_window(
                    last_guest_message_at,
                    last_template_sent_at,
                )
            items.append(chat_payload)

        return {
            "page": page,
            "page_size": requested_page_size,
            "items": items,
        }

    @router.get("/chats/{chat_id}/messages")
    async def list_messages(
        chat_id: str,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=100, ge=1, le=500),
        property_id: Optional[str] = Query(default=None),
        auth_ctx: Dict[str, Optional[str]] = Depends(_verify_bearer),
    ):
        decoded_id = unquote(chat_id or "").strip()
        clean_id = _clean_chat_id(decoded_id) or decoded_id
        property_id = _normalize_property_id(property_id)
        instance_id = str((auth_ctx or {}).get("instance_id") or "").strip() or None
        allowed_chat_ids: Optional[set[str]] = None
        allowed_original_chat_ids: Optional[set[str]] = None
        if instance_id:
            chat_ids, original_chat_ids = _instance_chat_sets(instance_id, "whatsapp", property_id)
            allowed_chat_ids = chat_ids
            allowed_original_chat_ids = original_chat_ids
            if not allowed_chat_ids and not allowed_original_chat_ids:
                return {
                    "chat_id": clean_id,
                    "whatsapp_phone_number": None,
                    "page": page,
                    "page_size": page_size,
                    "items": [],
                }
        whatsapp_phone_number: Optional[str] = None
        if instance_id:
            try:
                instance_payload = fetch_instance_by_code(instance_id) or {}
                instance_number = _resolve_instance_number(instance_payload)
                whatsapp_phone_number = _to_international_phone(instance_number or "")
            except Exception:
                whatsapp_phone_number = None
        id_candidates = {clean_id}
        if decoded_id and decoded_id != clean_id:
            id_candidates.add(decoded_id)
        tail = decoded_id.split(":")[-1] if ":" in decoded_id else ""
        tail_clean = _clean_chat_id(tail) or tail
        if tail_clean:
            id_candidates.add(tail_clean)
        offset = (page - 1) * page_size
        like_patterns = {f"%:{candidate}" for candidate in id_candidates}

        base_fields = "conversation_id, role, content, created_at, read_status, original_chat_id, property_id, structured_payload"
        extended_fields = (
            f"{base_fields}, ai_request_type, escalation_reason, "
            "user_id, user_first_name, user_last_name, user_last_name2, id"
        )
        extended_fields_no_escalation_meta = (
            f"{base_fields}, user_id, user_first_name, user_last_name, user_last_name2, id"
        )
        try:
            query = supabase.table("chat_history").select(extended_fields)
            if property_id is not None and not instance_id:
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
                query = supabase.table("chat_history").select(extended_fields_no_escalation_meta)
                if property_id is not None and not instance_id:
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
                fallback_base_fields = "conversation_id, role, content, created_at, read_status, original_chat_id, property_id"
                fallback_fields = f"{fallback_base_fields}, user_id, user_first_name, user_last_name, user_last_name2, message_id"
                try:
                    query = supabase.table("chat_history").select(fallback_fields)
                    if property_id is not None and not instance_id:
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
                    fallback_base_fields = "conversation_id, role, content, created_at, read_status, original_chat_id, property_id"
                    query = supabase.table("chat_history").select(fallback_base_fields)
                    if property_id is not None and not instance_id:
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
        if instance_id and allowed_chat_ids is not None and allowed_original_chat_ids is not None:
            filtered_rows = []
            for row in rows:
                cid = str((row or {}).get("conversation_id") or "").strip()
                cid_clean = _clean_chat_id(cid)
                original_chat_id = str((row or {}).get("original_chat_id") or "").strip()
                in_chat_set = bool(cid_clean and cid_clean in allowed_chat_ids)
                in_original_set = bool(original_chat_id and original_chat_id in allowed_original_chat_ids)
                if original_chat_id:
                    if not in_original_set:
                        continue
                elif not in_chat_set and not in_original_set:
                    continue
                filtered_rows.append(row)
            rows = filtered_rows
        if property_id is not None:
            filtered_rows = []
            for row in rows:
                row_prop = row.get("property_id")
                if row_prop is None and instance_id:
                    # Compatibilidad: mensajes legacy en multi-instancia sin property_id.
                    filtered_rows.append(row)
                    continue
                if str(row_prop).strip() == str(property_id).strip():
                    filtered_rows.append(row)
            rows = filtered_rows
        rows = [
            row
            for row in rows
            if not _is_internal_hidden_message(str((row or {}).get("content") or ""))
        ]
        rows.reverse()

        items = []
        for row in rows:
            structured_payload = row.get("structured_payload")
            if isinstance(structured_payload, str):
                try:
                    structured_payload = json.loads(structured_payload)
                except Exception:
                    structured_payload = None
            structured_csv = extract_structured_csv(structured_payload)
            items.append(
                {
                    "message_id": row.get("id") or row.get("message_id"),
                    "chat_id": clean_id,
                    "created_at": row.get("created_at"),
                    "read_status": row.get("read_status"),
                    "content": _format_history_content(row.get("content")),
                    "message": row.get("content"),
                    "sender": _map_sender(row.get("role")),
                    "original_chat_id": row.get("original_chat_id"),
                    "property_id": row.get("property_id"),
                    "user_id": row.get("user_id"),
                    "user_first_name": row.get("user_first_name"),
                    "user_last_name": row.get("user_last_name"),
                    "user_last_name2": row.get("user_last_name2"),
                    "structured_payload": structured_payload,
                    "structured_csv": structured_csv,
                    "ai_request_type": row.get("ai_request_type"),
                    "escalation_reason": row.get("escalation_reason"),
                }
            )

        return {
            "chat_id": clean_id,
            "whatsapp_phone_number": whatsapp_phone_number,
            "page": page,
            "page_size": page_size,
            "items": items,
        }

    @router.post("/messages")
    @router.post("/messages/send")
    async def send_message(
        payload: SendMessageRequest,
        auth_ctx: Dict[str, Optional[str]] = Depends(_verify_bearer),
    ):
        chat_id = _clean_chat_id(payload.chat_id) or payload.chat_id
        property_id = _normalize_property_id(payload.property_id)
        user_id = _normalize_user_id(payload.user_id)
        if payload.channel.lower() != "whatsapp":
            raise HTTPException(status_code=422, detail="Canal no soportado")
        if not payload.message.strip():
            raise HTTPException(status_code=422, detail="Mensaje vacio")
        outgoing_message = _sanitize_guest_outgoing_text(payload.message)
        if not outgoing_message:
            raise HTTPException(status_code=422, detail="Mensaje vacio tras limpieza")

        instance_id = None
        if state.memory_manager:
            try:
                instance_id = (
                    state.memory_manager.get_flag(chat_id, "instance_id")
                    or state.memory_manager.get_flag(chat_id, "instance_hotel_code")
                )
            except Exception:
                instance_id = None
        token_instance_id = str((auth_ctx or {}).get("instance_id") or "").strip() or None
        if token_instance_id:
            if instance_id and str(instance_id).strip() != token_instance_id:
                log.warning(
                    "instance_id en memoria (%s) no coincide con token (%s) para chat_id=%s; se prioriza token.",
                    instance_id,
                    token_instance_id,
                    chat_id,
                )
            instance_id = token_instance_id
            if state.memory_manager:
                try:
                    state.memory_manager.set_flag(chat_id, "instance_id", token_instance_id)
                    state.memory_manager.set_flag(chat_id, "instance_hotel_code", token_instance_id)
                except Exception:
                    pass
                try:
                    instance_payload = fetch_instance_by_code(token_instance_id) or {}
                    for key in ("whatsapp_phone_id", "whatsapp_token", "whatsapp_verify_token"):
                        val = instance_payload.get(key)
                        if val:
                            state.memory_manager.set_flag(chat_id, key, val)
                except Exception as exc:
                    log.warning("No se pudieron precargar credenciales WA para instance_id=%s: %s", token_instance_id, exc)
        if not instance_id:
            raise HTTPException(
                status_code=422,
                detail="instance_id requerido para enviar mensajes en WhatsApp multi-instancia",
            )

        if token_instance_id:
            context_id = _build_context_id_from_instance(state, chat_id, instance_id=instance_id)
        else:
            context_id = _resolve_whatsapp_context_id(state, chat_id, instance_id=instance_id)
        if not context_id:
            raise HTTPException(
                status_code=422,
                detail="No se pudo resolver el contexto de instancia para el envío de WhatsApp",
            )
        session_id = context_id or chat_id
        if context_id and state.memory_manager:
            try:
                state.memory_manager.set_flag(chat_id, "last_memory_id", context_id)
                state.memory_manager.set_flag(chat_id, "guest_number", chat_id)
                state.memory_manager.set_flag(chat_id, "force_guest_role", True)
            except Exception:
                pass
        if state.memory_manager and not instance_id:
            try:
                instance_id = (
                    state.memory_manager.get_flag(context_id or chat_id, "instance_id")
                    or state.memory_manager.get_flag(context_id or chat_id, "instance_hotel_code")
                )
            except Exception:
                instance_id = None
        # Si hay context_id compuesto y no viene property_id, es ambiguo en multi-instancia.
        if property_id is None and context_id and ":" in str(context_id):
            # Fallback para clientes que aún no envían property_id explícito.
            property_id = _resolve_property_id_from_history(chat_id, payload.channel.lower())
            if property_id is None and state.memory_manager:
                try:
                    property_id = state.memory_manager.get_flag(chat_id, "property_id")
                except Exception:
                    property_id = None
            # Si conocemos la instancia (por token o memoria), no bloquear envío manual.
            # En ese caso, el context_id ya enruta correctamente por instancia.
            if property_id is None and not instance_id:
                raise HTTPException(
                    status_code=422,
                    detail="property_id requerido para enviar mensajes en WhatsApp multi-instancia",
                )
        if state.memory_manager and property_id is not None:
            # Si conocemos la instancia del token/memoria, reconstruimos el contexto
            # compuesto correcto para no degradar el envío al chat_id plano.
            target_context_id = context_id
            if instance_id:
                rebuilt_context_id = _build_context_id_from_instance(state, chat_id, instance_id=instance_id)
                if rebuilt_context_id:
                    target_context_id = rebuilt_context_id

            for mem_id in [chat_id, context_id, target_context_id]:
                if not mem_id:
                    continue
                state.memory_manager.set_flag(mem_id, "property_id", property_id)

            ensure_instance_credentials(state.memory_manager, target_context_id or chat_id)
            context_id = target_context_id
        elif state.memory_manager:
            ensure_instance_credentials(state.memory_manager, context_id or chat_id)

        # IMPORTANTE: session_id debe resolverse después de cualquier ajuste de context_id.
        # Si no, puede persistirse en el contexto equivocado y no aparecer en chatter.
        session_id = context_id or chat_id
        chat_visible_before = False
        resolved_original_chat_id = context_id or (
            str(session_id).strip() if isinstance(session_id, str) and ":" in session_id else None
        )
        log.info(
            "[CHATTER_SEND] request chat_id=%s property_id=%s sender=%s instance_id=%s context_id=%s session_id=%s",
            chat_id,
            property_id,
            payload.sender,
            instance_id,
            context_id,
            session_id,
        )

        if token_instance_id and state.memory_manager:
            try:
                enforced_payload = fetch_instance_by_code(token_instance_id) or {}
                for mem_id in [chat_id, context_id, session_id]:
                    if not mem_id:
                        continue
                    state.memory_manager.set_flag(mem_id, "instance_id", token_instance_id)
                    state.memory_manager.set_flag(mem_id, "instance_hotel_code", token_instance_id)
                    for key in ("whatsapp_phone_id", "whatsapp_token", "whatsapp_verify_token"):
                        val = enforced_payload.get(key)
                        if val:
                            state.memory_manager.set_flag(mem_id, key, val)
            except Exception as exc:
                log.warning("No se pudieron imponer credenciales WA finales para instance_id=%s: %s", token_instance_id, exc)

        final_phone_id = None
        final_wa_instance = None
        if state.memory_manager:
            try:
                final_phone_id = state.memory_manager.get_flag(session_id, "whatsapp_phone_id") or state.memory_manager.get_flag(chat_id, "whatsapp_phone_id")
                final_wa_instance = (
                    state.memory_manager.get_flag(session_id, "instance_id")
                    or state.memory_manager.get_flag(session_id, "instance_hotel_code")
                    or state.memory_manager.get_flag(chat_id, "instance_id")
                    or state.memory_manager.get_flag(chat_id, "instance_hotel_code")
                )
            except Exception:
                final_phone_id = None
                final_wa_instance = None
        log.info(
            "[WA_ROUTE] chat_id=%s property_id=%s token_instance_id=%s resolved_instance_id=%s context_id=%s session_id=%s phone_id=%s",
            chat_id,
            property_id,
            token_instance_id,
            final_wa_instance or instance_id,
            context_id,
            session_id,
            final_phone_id or "missing",
        )

        outgoing_message = _ensure_guest_language_for_outgoing(
            state,
            chat_id,
            outgoing_message,
            context_id=context_id,
        )

        await state.channel_manager.send_message(
            chat_id,
            outgoing_message,
            channel="whatsapp",
            context_id=context_id,
        )
        try:
            await sync_guest_offer_state_from_sent_wa(
                state,
                guest_id=chat_id,
                sent_message=outgoing_message,
                source="chatter_manual",
                session_id=context_id or chat_id,
                property_id=property_id,
            )
        except Exception:
            pass
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
            for extra_id in (session_id, context_id):
                if extra_id and extra_id not in related_ids:
                    related_ids.append(extra_id)
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
                history_key = context_id or session_id or chat_id
                property_id = _resolve_property_id_from_history(history_key, payload.channel.lower())
            for mem_id in related_ids:
                if property_id is not None:
                    state.memory_manager.set_flag(mem_id, "property_id", property_id)
                state.memory_manager.set_flag(mem_id, "default_channel", payload.channel.lower())
                # Al responder manualmente, limpiamos posibles pendientes antiguos.
                state.memory_manager.clear_flag(mem_id, "escalation_in_progress")
                state.memory_manager.clear_flag(mem_id, "last_escalation_followup_message")
                state.memory_manager.clear_flag(mem_id, "escalation_confirmation_pending")
                state.memory_manager.clear_flag(mem_id, "consulta_base_realizada")
                state.memory_manager.clear_flag(mem_id, "inciso_enviado")
            if not resolved_original_chat_id and state.memory_manager:
                try:
                    last_mem = (
                        state.memory_manager.get_flag(chat_id, "last_memory_id")
                        or state.memory_manager.get_flag(session_id, "last_memory_id")
                    )
                    if isinstance(last_mem, str) and ":" in last_mem:
                        resolved_original_chat_id = last_mem.strip()
                except Exception:
                    pass
            if not resolved_original_chat_id and instance_id:
                try:
                    built_ctx = _build_context_id_from_instance(state, chat_id, instance_id=instance_id)
                    if isinstance(built_ctx, str) and ":" in built_ctx:
                        resolved_original_chat_id = built_ctx.strip()
                except Exception:
                    pass
            chat_visible_before = is_chat_visible_in_list(
                chat_id,
                property_id=property_id,
                channel=payload.channel.lower(),
                original_chat_id=resolved_original_chat_id,
            )
            log.info(
                "[CHATTER_SEND] visibility.before chat_id=%s property_id=%s channel=%s original_chat_id=%s visible=%s",
                chat_id,
                property_id,
                payload.channel.lower(),
                resolved_original_chat_id,
                chat_visible_before,
            )
            state.memory_manager.save(
                session_id,
                role,
                outgoing_message,
                user_id=user_id if role == "user" else None,
                user_first_name=payload.user_first_name if role == "user" else None,
                user_last_name=payload.user_last_name if role == "user" else None,
                user_last_name2=payload.user_last_name2 if role == "user" else None,
                channel=payload.channel.lower(),
                original_chat_id=resolved_original_chat_id,
                bypass_force_guest_role=role == "user",
            )
            for mem_id in related_ids:
                if mem_id == session_id:
                    continue
                state.memory_manager.add_runtime_message(
                    mem_id,
                    role,
                    outgoing_message,
                    channel=payload.channel.lower(),
                    original_chat_id=resolved_original_chat_id or chat_id,
                    bypass_force_guest_role=role == "user",
                    user_id=user_id if role == "user" else None,
                    user_first_name=payload.user_first_name if role == "user" else None,
                    user_last_name=payload.user_last_name if role == "user" else None,
                    user_last_name2=payload.user_last_name2 if role == "user" else None,
                )
        except Exception as exc:
            log.warning("No se pudo guardar el mensaje en memoria: %s", exc)

        emit_related_ids = _related_memory_ids(state, chat_id)
        for extra_id in (session_id, context_id):
            if extra_id and extra_id not in emit_related_ids:
                emit_related_ids.append(extra_id)
        if property_id is None and state.memory_manager:
            for mem_id in emit_related_ids:
                try:
                    candidate = state.memory_manager.get_flag(mem_id, "property_id")
                except Exception:
                    candidate = None
                if candidate is not None:
                    property_id = candidate
                    break
        if property_id is None:
            property_id = _resolve_property_id_from_history(
                session_id or chat_id,
                payload.channel.lower(),
            )
        if property_id is not None and state.memory_manager:
            for mem_id in emit_related_ids:
                state.memory_manager.set_flag(mem_id, "property_id", property_id)

        rooms = _rooms(chat_id, property_id, payload.channel.lower())
        visibility_restored = False
        if not chat_visible_before:
            visibility_restored = _restore_chat_visibility(
                chat_id,
                property_id=property_id,
                channel=payload.channel.lower(),
                original_chat_id=resolved_original_chat_id,
            )
            log.info(
                "[CHATTER_SEND] visibility.restore chat_id=%s property_id=%s channel=%s attempted=%s",
                chat_id,
                property_id,
                payload.channel.lower(),
                visibility_restored,
            )
        chat_visible_after = is_chat_visible_in_list(
            chat_id,
            property_id=property_id,
            channel=payload.channel.lower(),
            original_chat_id=resolved_original_chat_id,
        )
        log.info(
            "[CHATTER_SEND] visibility.after chat_id=%s property_id=%s channel=%s original_chat_id=%s visible=%s",
            chat_id,
            property_id,
            payload.channel.lower(),
            resolved_original_chat_id,
            chat_visible_after,
        )
        try:
            resolved_ids = resolve_pending_escalations_for_chat(
                chat_id,
                final_response=outgoing_message,
                property_id=property_id,
            )
            if resolved_ids:
                log.info(
                    "Escalaciones %s resueltas automáticamente tras enviar mensaje.",
                    ",".join(resolved_ids),
                )
                for resolved_id in resolved_ids:
                    await _emit(
                        "escalation.resolved",
                        {
                            "rooms": rooms,
                            "chat_id": chat_id,
                            "escalation_id": resolved_id,
                            "final_response": outgoing_message,
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
            log.warning("No se pudo auto-resolver escalaciones para %s: %s", chat_id, exc)

        now_iso = datetime.now(timezone.utc).isoformat()
        if role == "user":
            sender_for_ui = "user"
        elif role == "guest":
            sender_for_ui = "guest"
        else:
            sender_for_ui = "bookai"
        try:
            client_language, client_language_confidence = _resolve_guest_lang_meta_for_chat(
                state,
                chat_id,
                context_id=session_id,
            )
        except Exception:
            client_language, client_language_confidence = "es", 0.0
        whatsapp_window = _resolve_whatsapp_window_for_chat(
            chat_id,
            property_id=property_id,
            channel="whatsapp",
            original_chat_id=session_id,
        )
        socket_mgr = getattr(state, "socket_manager", None)
        if (
            socket_mgr
            and getattr(socket_mgr, "enabled", False)
            and property_id is not None
            and not chat_visible_before
            and chat_visible_after
        ):
            folio_id = None
            reservation_locator = None
            checkin = None
            checkout = None
            reservation_status = None
            room_number = None
            client_name = None
            if state.memory_manager:
                try:
                    folio_id = state.memory_manager.get_flag(session_id, "folio_id")
                    reservation_locator = state.memory_manager.get_flag(session_id, "reservation_locator")
                    checkin = state.memory_manager.get_flag(session_id, "checkin")
                    checkout = state.memory_manager.get_flag(session_id, "checkout")
                    reservation_status = state.memory_manager.get_flag(session_id, "reservation_status")
                    room_number = state.memory_manager.get_flag(session_id, "room_number")
                    client_name = state.memory_manager.get_flag(session_id, "client_name")
                except Exception:
                    pass
            whatsapp_phone_number = None
            if instance_id:
                try:
                    instance_payload = fetch_instance_by_code(instance_id) or {}
                    instance_number = _resolve_instance_number(instance_payload)
                    whatsapp_phone_number = _to_international_phone(instance_number or "")
                except Exception:
                    whatsapp_phone_number = None
            bookai_resolution = _bookai_flag_resolution(
                _bookai_settings(state),
                aliases=_related_memory_ids(state, chat_id) or [],
                chat_id=chat_id,
                property_id=property_id,
                instance_id=instance_id,
                default=True,
            )
            await socket_mgr.emit(
                "chat.list.updated",
                {
                    "property_id": property_id,
                    "action": "created",
                    "chat": {
                        "chat_id": chat_id,
                        "property_id": property_id,
                        "reservation_id": folio_id,
                        "reservation_locator": reservation_locator,
                        "reservation_status": reservation_status,
                        "room_number": room_number,
                        "checkin": checkin,
                        "checkout": checkout,
                        "channel": payload.channel.lower(),
                        "last_message": outgoing_message,
                        "last_message_at": now_iso,
                        "avatar": None,
                        "client_name": client_name,
                        "client_language": client_language,
                        "client_language_confidence": _normalize_language_confidence(
                            client_language_confidence,
                            default=0.0,
                        ),
                        "client_phone": _extract_guest_phone(chat_id) or chat_id,
                        "whatsapp_phone_number": whatsapp_phone_number,
                        "whatsapp_window": whatsapp_window,
                        "bookai_enabled": bool(bookai_resolution.get("value")),
                        "unread_count": 0,
                        **_pending_snapshot_for_chat(
                            chat_id,
                            property_id,
                            instance_id=instance_id,
                            memory_manager=getattr(state, "memory_manager", None),
                        ),
                        "folio_id": folio_id,
                    },
                },
                rooms=f"property:{property_id}",
                instance_id=instance_id,
            )
            log.info(
                "[CHATTER_SEND] emit chat.list.updated action=created chat_id=%s property_id=%s room=property:%s",
                chat_id,
                property_id,
                property_id,
            )
        else:
            log.info(
                "[CHATTER_SEND] skip chat.list.updated chat_id=%s property_id=%s visible_before=%s visible_after=%s",
                chat_id,
                property_id,
                chat_visible_before,
                chat_visible_after,
            )
        await _emit(
            "chat.message.created",
            {
                "rooms": rooms,
                "chat_id": chat_id,
                "property_id": property_id,
                "channel": payload.channel.lower(),
                "sender": sender_for_ui,
                "message": outgoing_message,
                "created_at": now_iso,
                "client_language": client_language,
                "client_language_confidence": _normalize_language_confidence(
                    client_language_confidence,
                    default=0.0,
                ),
                "whatsapp_window": whatsapp_window,
            },
        )
        log.info(
            "[CHATTER_SEND] emit chat.message.created chat_id=%s property_id=%s rooms=%s",
            chat_id,
            property_id,
            rooms,
        )
        await _emit(
            "chat.updated",
            {
                "rooms": rooms,
                "chat_id": chat_id,
                "property_id": property_id,
                "channel": payload.channel.lower(),
                "last_message": outgoing_message,
                "last_message_at": now_iso,
                "whatsapp_window": whatsapp_window,
                **_pending_snapshot_for_chat(
                    chat_id,
                    property_id,
                    instance_id=instance_id,
                    memory_manager=getattr(state, "memory_manager", None),
                ),
            },
        )
        log.info(
            "[CHATTER_SEND] emit chat.updated chat_id=%s property_id=%s rooms=%s",
            chat_id,
            property_id,
            rooms,
        )

        return {
            "status": "sent",
            "chat_id": chat_id,
            "user_id": payload.user_id,
            "sender": sender_for_ui,
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
        refined = _compact_ai_draft(refined)
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
        property_id: Optional[str] = Query(default=None),
        _: None = Depends(_verify_bearer),
    ):
        clean_id = _clean_chat_id(chat_id) or chat_id
        message = (payload.message or "").strip()
        if not message:
            raise HTTPException(status_code=422, detail="message requerida")
        property_id = _normalize_property_id(property_id)
        if property_id is None:
            property_id = _resolve_property_id_from_history(clean_id)

        from core.escalation_db import append_escalation_message

        pending_escalations = list_pending_escalations_for_chat(
            clean_id,
            limit=100,
            property_id=property_id,
        )
        if not pending_escalations:
            raise HTTPException(status_code=404, detail="No hay escalación pendiente")

        # Trabajamos solo sobre la pendiente más reciente para evitar
        # emitir múltiples borradores intermedios en cascada.
        esc = pending_escalations[-1]
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

        log.info(
            "Escalation-chat single-draft mode chat_id=%s escalation_id=%s pending_count=%s message=%s",
            clean_id,
            escalation_id,
            len(pending_escalations),
            message,
        )
        from tools.interno_tool import ESCALATIONS_STORE, Escalation, generar_borrador
        from core.message_utils import extract_clean_draft

        pending_guest_message = (esc.get("guest_message") or "").strip()
        pending_type = (esc.get("escalation_type") or esc.get("type") or "").strip()
        pending_reason = (esc.get("escalation_reason") or esc.get("reason") or "").strip()
        pending_context = (esc.get("context") or "").strip()
        pending_draft = (esc.get("draft_response") or "").strip()

        if escalation_id not in ESCALATIONS_STORE:
            ESCALATIONS_STORE[escalation_id] = Escalation(
                escalation_id=escalation_id,
                guest_chat_id=clean_id,
                guest_message=pending_guest_message,
                escalation_type=pending_type or "manual",
                escalation_reason=pending_reason,
                context=pending_context,
                timestamp=str(esc.get("timestamp") or ""),
                draft_response=pending_draft or None,
                manager_confirmed=bool(esc.get("manager_confirmed") or False),
                final_response=(esc.get("final_response") or None),
                sent_to_guest=bool(esc.get("sent_to_guest") or False),
                property_id=esc.get("property_id"),
            )

        base_response = pending_draft or pending_guest_message or ""
        if base_response:
            ESCALATIONS_STORE[escalation_id].draft_response = base_response

        raw_borrador = (
            generar_borrador(
                escalation_id=escalation_id,
                manager_response=base_response,
                adjustment=message,
            )
            or ""
        ).strip()
        draft_response = extract_clean_draft(raw_borrador or "").strip() or raw_borrador
        draft_response = _strip_draft_instruction_block(draft_response)
        draft_response = _compact_ai_draft(draft_response)
        if not draft_response:
            draft_response = "No tengo suficiente información para generar un borrador."

        update_escalation(escalation_id, {"draft_response": draft_response})
        ai_message = None

        await _emit(
            "escalation.chat.updated",
            {
                "rooms": _rooms(clean_id, None, "whatsapp"),
                "chat_id": clean_id,
                "escalation_id": escalation_id,
                "messages": messages,
                "ai_message": ai_message,
                "pending_escalations_count": len(pending_escalations),
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
                "pending_escalations_count": len(pending_escalations),
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

        proposed_response = draft_response or None
        is_final_response = bool(draft_response)

        return {
            "chat_id": clean_id,
            "escalation_id": escalation_id,
            "escalation_ids": [
                str(e.get("escalation_id") or "").strip()
                for e in pending_escalations
                if str(e.get("escalation_id") or "").strip()
            ],
            "pending_escalations_count": len(pending_escalations),
            "ai_message": ai_message,
            "messages": messages,
            "proposed_response": proposed_response,
            "is_final_response": is_final_response,
        }

    @router.get("/chats/{chat_id}/escalation-chat")
    async def get_escalation_chat(
        chat_id: str,
        escalation_id: Optional[str] = Query(default=None),
        property_id: Optional[str] = Query(default=None),
        _: None = Depends(_verify_bearer),
    ):
        clean_id = _clean_chat_id(chat_id) or chat_id
        property_id = _normalize_property_id(property_id)
        if property_id is None:
            property_id = _resolve_property_id_from_history(clean_id)

        pending_escalations = list_pending_escalations_for_chat(
            clean_id,
            limit=100,
            property_id=property_id,
        )
        if not pending_escalations:
            raise HTTPException(status_code=404, detail="No hay escalación pendiente")

        pending_ids = [
            str(e.get("escalation_id") or "").strip()
            for e in pending_escalations
            if str(e.get("escalation_id") or "").strip()
        ]
        active_escalation = pending_escalations[-1]
        active_id = str(active_escalation.get("escalation_id") or "").strip()

        if escalation_id and escalation_id.strip() and escalation_id.strip() not in pending_ids:
            return {
                "chat_id": clean_id,
                "escalation_id": active_id,
                "escalation_ids": pending_ids,
                "pending_escalations_count": len(pending_ids),
                "messages": [],
                "proposed_response": (active_escalation.get("draft_response") or "").strip() or None,
                "is_final_response": bool((active_escalation.get("draft_response") or "").strip()),
            }

        if escalation_id and escalation_id.strip():
            target_id = escalation_id.strip()
            target = next((e for e in pending_escalations if str(e.get("escalation_id") or "").strip() == target_id), None)
            if not target:
                raise HTTPException(status_code=404, detail="Escalación inválida")
            draft_response = (target.get("draft_response") or "").strip()
            messages = target.get("messages") if isinstance(target.get("messages"), list) else []
            return {
                "chat_id": clean_id,
                "escalation_id": target_id,
                "escalation_ids": pending_ids,
                "pending_escalations_count": len(pending_ids),
                "messages": messages,
                "proposed_response": draft_response or None,
                "is_final_response": bool(draft_response),
                "pending_summary": _pending_escalations_summary(pending_escalations),
            }

        merged_messages: List[Dict[str, Any]] = []
        for msg in active_escalation.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            enriched = dict(msg)
            if active_id:
                enriched["escalation_id"] = active_id
            merged_messages.append(enriched)
        merged_messages = sorted(merged_messages, key=lambda m: _parse_ts(m.get("timestamp")) or datetime.min)
        merged_draft = _compact_ai_draft((active_escalation.get("draft_response") or "").strip())

        return {
            "chat_id": clean_id,
            "escalation_id": active_id,
            "escalation_ids": pending_ids,
            "pending_escalations_count": len(pending_ids),
            "messages": merged_messages,
            "proposed_response": merged_draft or None,
            "is_final_response": bool(merged_draft),
            "pending_summary": _pending_escalations_summary(pending_escalations),
        }

    @router.post("/chats/{chat_id}/resolve-escalation")
    async def resolve_escalation(
        chat_id: str,
        payload: ResolveEscalationRequest,
        auth_ctx: Dict[str, Optional[str]] = Depends(_verify_bearer),
    ):
        clean_id = _clean_chat_id(chat_id) or chat_id
        instance_id = str((auth_ctx or {}).get("instance_id") or "").strip() or None
        requested_property_id = _normalize_property_id(payload.property_id)
        if requested_property_id is None:
            requested_property_id = _normalize_property_id(_resolve_property_id_from_history(clean_id))
        if requested_property_id is None and getattr(state, "memory_manager", None):
            for mem_id in _related_memory_ids(state, clean_id) or []:
                try:
                    candidate = state.memory_manager.get_flag(mem_id, "property_id")
                except Exception:
                    candidate = None
                if candidate is not None:
                    requested_property_id = _normalize_property_id(candidate)
                    break

        pending_for_property = []
        if requested_property_id is not None:
            pending_for_property = list_pending_escalations_for_chat(
                clean_id,
                limit=100,
                property_id=requested_property_id,
            ) or []
        pending_any = list_pending_escalations_for_chat(
            clean_id,
            limit=100,
            property_id=None,
        ) or []

        target_pending = pending_for_property[-1] if pending_for_property else None
        if target_pending is None and pending_any:
            if requested_property_id is None:
                target_pending = pending_any[-1]
            else:
                legacy_matches = [
                    esc
                    for esc in pending_any
                    if _normalize_property_id((esc or {}).get("property_id")) is None
                ]
                if legacy_matches:
                    target_pending = legacy_matches[-1]
                else:
                    raise HTTPException(status_code=422, detail="property_id no coincide con la escalación pendiente")

        if target_pending is None:
            latest_any = get_latest_escalation_for_chat(clean_id, property_id=requested_property_id)
            if latest_any and is_escalation_resolved(latest_any):
                raise HTTPException(status_code=409, detail="La escalación ya está resuelta")
            if _chat_exists_in_history(clean_id, property_id=requested_property_id, channel="whatsapp"):
                raise HTTPException(status_code=404, detail="No hay escalación pendiente")
            raise HTTPException(status_code=404, detail="Chat no encontrado")

        escalation_id = str((target_pending or {}).get("escalation_id") or "").strip()
        if not escalation_id:
            raise HTTPException(status_code=404, detail="Escalación inválida")

        current = get_escalation(escalation_id) or target_pending
        if is_escalation_resolved(current):
            raise HTTPException(status_code=409, detail="La escalación ya está resuelta")

        resolved_by = None
        if payload.resolved_by is not None and str(payload.resolved_by).strip():
            resolved_by = str(payload.resolved_by).strip()
        resolved_by_name = str(payload.resolved_by_name or "").strip() or None
        resolved_by_email = str(payload.resolved_by_email or "").strip() or None
        resolved_at = datetime.now(timezone.utc).isoformat()
        resolution_notes = payload.resolution_notes if payload.resolution_notes is not None else ""

        updated = resolve_escalation_with_resolution(
            escalation_id,
            property_id=requested_property_id if requested_property_id is not None else target_pending.get("property_id"),
            resolution_medium=payload.resolution_medium,
            resolution_notes=resolution_notes,
            resolved_at=resolved_at,
            resolved_by=resolved_by,
            resolved_by_name=resolved_by_name,
            resolved_by_email=resolved_by_email,
        )
        if not updated:
            raise HTTPException(status_code=500, detail="No se pudo resolver la escalación")
        if not is_escalation_resolved(updated):
            raise HTTPException(status_code=500, detail="No se pudo confirmar la resolución de la escalación")

        resolved_property_id = _normalize_property_id(updated.get("property_id"))
        if resolved_property_id is None:
            resolved_property_id = requested_property_id
        if resolved_property_id is None:
            resolved_property_id = _normalize_property_id((target_pending or {}).get("property_id"))

        if getattr(state, "memory_manager", None):
            related_ids = _related_memory_ids(state, clean_id) or []
            if clean_id not in related_ids:
                related_ids.append(clean_id)
            for mem_id in related_ids:
                try:
                    state.memory_manager.clear_flag(mem_id, "escalation_in_progress")
                    state.memory_manager.clear_flag(mem_id, "last_escalation_followup_message")
                    state.memory_manager.clear_flag(mem_id, "escalation_confirmation_pending")
                    if resolved_property_id is not None:
                        state.memory_manager.set_flag(mem_id, "property_id", resolved_property_id)
                except Exception:
                    continue

        rooms = _rooms(clean_id, resolved_property_id, "whatsapp")
        escalation_rooms = _rooms(clean_id, None, "whatsapp")
        resolution_payload = _build_escalation_resolution_payload(
            clean_id,
            updated,
            fallback_property_id=resolved_property_id,
        )
        pending_snapshot = _pending_snapshot_for_chat(
            clean_id,
            resolved_property_id,
            instance_id=instance_id,
            memory_manager=getattr(state, "memory_manager", None),
        )
        escalation_messages = updated.get("messages") if isinstance(updated.get("messages"), list) else []

        await _emit(
            "escalation.resolved",
            {
                "event": "escalation.resolved",
                "rooms": rooms,
                **resolution_payload,
            },
        )
        await _emit(
            "escalation.chat.updated",
            {
                "rooms": escalation_rooms,
                "chat_id": clean_id,
                "escalation_id": resolution_payload.get("escalation_id"),
                "messages": escalation_messages,
                "pending_escalations_count": 0,
                "resolution": resolution_payload,
            },
        )
        await _emit(
            "escalation.updated",
            {
                "rooms": escalation_rooms,
                "chat_id": clean_id,
                "escalation_id": resolution_payload.get("escalation_id"),
                "messages": escalation_messages,
                "draft_response": None,
                "pending_escalations_count": 0,
                "status": "resolved",
                "resolution": resolution_payload,
            },
        )
        await _emit(
            "chat.proposed_response.updated",
            {
                "rooms": escalation_rooms,
                "chat_id": clean_id,
                "proposed_response": None,
                "is_final_response": False,
            },
        )
        await _emit(
            "chat.updated",
            {
                "rooms": rooms,
                "chat_id": clean_id,
                "property_id": resolved_property_id,
                "channel": "whatsapp",
                **pending_snapshot,
                "escalation_resolution": resolution_payload,
            },
        )

        return resolution_payload

    @router.get("/chats/{chat_id}/escalation-resolution")
    async def get_escalation_resolution(
        chat_id: str,
        property_id: Optional[str] = Query(default=None),
        _: None = Depends(_verify_bearer),
    ):
        clean_id = _clean_chat_id(chat_id) or chat_id
        resolved_property_id = _normalize_property_id(property_id)
        if resolved_property_id is None:
            resolved_property_id = _normalize_property_id(_resolve_property_id_from_history(clean_id))
        if resolved_property_id is None and getattr(state, "memory_manager", None):
            for mem_id in _related_memory_ids(state, clean_id) or []:
                try:
                    candidate = state.memory_manager.get_flag(mem_id, "property_id")
                except Exception:
                    candidate = None
                if candidate is not None:
                    resolved_property_id = _normalize_property_id(candidate)
                    break

        latest = get_latest_resolved_escalation_for_chat(clean_id, property_id=resolved_property_id)
        if not latest and resolved_property_id is not None:
            # Compatibilidad para escalaciones legacy con property_id NULL.
            latest = get_latest_resolved_escalation_for_chat(clean_id, property_id=None)
        latest_any = get_latest_escalation_for_chat(clean_id, property_id=resolved_property_id)
        if not latest_any and resolved_property_id is not None:
            latest_any = get_latest_escalation_for_chat(clean_id, property_id=None)
        if not latest:
            if _chat_exists_in_history(clean_id, property_id=resolved_property_id, channel="whatsapp"):
                raise HTTPException(status_code=404, detail="No hay resolución de escalación")
            raise HTTPException(status_code=404, detail="Chat no encontrado")
        if latest_any and not is_escalation_resolved(latest_any):
            latest_any_id = str((latest_any or {}).get("escalation_id") or "").strip()
            latest_resolved_id = str((latest or {}).get("escalation_id") or "").strip()
            if latest_any_id and latest_any_id != latest_resolved_id:
                raise HTTPException(status_code=404, detail="No hay resolución de escalación")
        if not is_escalation_resolved(latest):
            raise HTTPException(status_code=404, detail="No hay resolución de escalación")

        return _build_escalation_resolution_payload(
            clean_id,
            latest,
            fallback_property_id=resolved_property_id,
        )

    @router.patch("/chats/{chat_id}/bookai")
    async def toggle_bookai(
        chat_id: str,
        payload: ToggleBookAiRequest,
        property_id: Optional[str] = Query(default=None),
        auth_ctx: Dict[str, Optional[str]] = Depends(_verify_bearer),
    ):
        clean_id = _clean_chat_id(chat_id) or chat_id
        property_id = _normalize_property_id(property_id) or _normalize_property_id(payload.property_id)
        instance_id = str((auth_ctx or {}).get("instance_id") or "").strip() or None
        if not instance_id:
            raise HTTPException(status_code=422, detail="instance_id requerido para toggle de WhatsApp multi-instancia")
        if property_id is None:
            raise HTTPException(status_code=422, detail="property_id requerido")
        bookai_flags = _bookai_settings(state)
        related_ids = _related_memory_ids(state, clean_id)
        if clean_id not in related_ids:
            related_ids.append(clean_id)
        legacy_keys_to_drop: set[str] = set()
        scoped_keys_to_set: set[str] = set()
        for alias in related_ids:
            alias_clean = _clean_chat_id(alias) or str(alias or "").strip()
            if not alias_clean:
                continue
            scoped_keys_to_set.update(
                _bookai_flag_keys(alias_clean, property_id=property_id, instance_id=instance_id)
            )
            legacy_keys_to_drop.add(alias_clean)
            legacy_keys_to_drop.add(f"{alias_clean}:{property_id}")
        for stale_key in legacy_keys_to_drop:
            bookai_flags.pop(stale_key, None)
        for key in scoped_keys_to_set:
            bookai_flags[key] = bool(payload.bookai_enabled)
        memory_mgr = getattr(state, "memory_manager", None)
        if memory_mgr:
            try:
                for mem_id in related_ids:
                    if not mem_id:
                        continue
                    try:
                        mem_instance = (
                            memory_mgr.get_flag(mem_id, "instance_id")
                            or memory_mgr.get_flag(mem_id, "instance_hotel_code")
                        )
                    except Exception:
                        mem_instance = None
                    if instance_id and mem_instance and str(mem_instance).strip() != str(instance_id).strip():
                        continue
                    try:
                        memory_mgr.clear_flag(mem_id, "bookai_enabled")
                    except Exception:
                        pass
            except Exception as exc:
                log.warning("No se pudo sincronizar flag bookai_enabled en memoria: %s", exc)
        log.info(
            "[BOOKAI_TOGGLE] chat_id=%s property_id=%s instance_id=%s value=%s scoped_keys=%s dropped_legacy=%s",
            clean_id,
            property_id,
            instance_id,
            bool(payload.bookai_enabled),
            ",".join(sorted(scoped_keys_to_set)),
            ",".join(sorted(legacy_keys_to_drop)),
        )
        state.save_tracking()

        if payload.bookai_enabled is False:
            # Evita que mensajes recibidos antes de desactivar queden pendientes y
            # se procesen luego al reactivar.
            try:
                buffer_mgr = getattr(state, "buffer_manager", None)
                convs = list(getattr(buffer_mgr, "_convs", {}).keys()) if buffer_mgr else []
                target_keys = []
                for cid in convs:
                    if cid != clean_id and not str(cid).endswith(f":{clean_id}"):
                        continue
                    if memory_mgr:
                        try:
                            cid_instance = (
                                memory_mgr.get_flag(cid, "instance_id")
                                or memory_mgr.get_flag(cid, "instance_hotel_code")
                            )
                        except Exception:
                            cid_instance = None
                        if instance_id and cid_instance and str(cid_instance).strip() != str(instance_id).strip():
                            continue
                        try:
                            cid_prop = memory_mgr.get_flag(cid, "property_id")
                        except Exception:
                            cid_prop = None
                        if cid_prop is not None and str(cid_prop).strip() != str(property_id).strip():
                            continue
                    target_keys.append(cid)
                for cid in target_keys:
                    await buffer_mgr.discard_conversation(cid, cancel_processing=True)
            except Exception as exc:
                log.warning("No se pudo purgar buffer al desactivar BookAI: %s", exc)

        await _emit(
            "chat.bookai.toggled",
            {
                "rooms": _rooms(clean_id, property_id, "whatsapp"),
                "chat_id": clean_id,
                "property_id": property_id,
                "bookai_enabled": payload.bookai_enabled,
            },
        )
        return {
            "chat_id": clean_id,
            "property_id": property_id,
            "bookai_enabled": payload.bookai_enabled,
        }

    @router.patch("/chats/{chat_id}/read")
    async def mark_chat_read(
        chat_id: str,
        property_id: Optional[str] = Query(default=None),
        auth_ctx: Dict[str, Optional[str]] = Depends(_verify_bearer),
    ):
        clean_id = _clean_chat_id(chat_id) or chat_id
        property_id = _normalize_property_id(property_id)
        instance_id = str((auth_ctx or {}).get("instance_id") or "").strip() or None
        if property_id is None:
            raise HTTPException(status_code=422, detail="property_id requerido")
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
                **_pending_snapshot_for_chat(
                    clean_id,
                    property_id,
                    instance_id=instance_id,
                    memory_manager=getattr(state, "memory_manager", None),
                ),
            },
        )

        return {
            "chat_id": clean_id,
            "read_status": True,
        }

    @router.post("/chats/{chat_id}/archive")
    async def archive_chat(
        chat_id: str,
        property_id: Optional[str] = Query(default=None),
        auth_ctx: Dict[str, Optional[str]] = Depends(_verify_bearer),
    ):
        clean_id = _clean_chat_id(chat_id) or chat_id
        property_id = _normalize_property_id(property_id)
        channel = "whatsapp"
        instance_id = str((auth_ctx or {}).get("instance_id") or "").strip() or None
        summary_row: Dict[str, Any] | None = None
        history_rows: List[Dict[str, Any]] = []
        try:
            summary_query = (
                supabase.table("chat_last_message")
                .select("conversation_id, original_chat_id, property_id, content, created_at, client_name, channel")
                .eq("conversation_id", clean_id)
                .eq("channel", channel)
            )
            if property_id is not None:
                summary_query = summary_query.eq("property_id", property_id)
            summary_rows = summary_query.order("created_at", desc=True).limit(20).execute().data or []
            if summary_rows:
                summary_row = summary_rows[0]
        except Exception:
            summary_row = None
        if property_id is None and summary_row is not None:
            property_id = _normalize_property_id(summary_row.get("property_id"))
        if property_id is None:
            property_id = _normalize_property_id(_resolve_property_id_from_history(clean_id, channel))
        if property_id is None and getattr(state, "memory_manager", None):
            try:
                property_id = _normalize_property_id(state.memory_manager.get_flag(clean_id, "property_id"))
            except Exception:
                property_id = None
        if property_id is None:
            raise HTTPException(status_code=422, detail="property_id requerido")
        if summary_row is None or _normalize_property_id(summary_row.get("property_id")) != property_id:
            try:
                summary_rows = (
                    supabase.table("chat_last_message")
                    .select("conversation_id, original_chat_id, property_id, content, created_at, client_name, channel")
                    .eq("conversation_id", clean_id)
                    .eq("channel", channel)
                    .eq("property_id", property_id)
                    .order("created_at", desc=True)
                    .limit(20)
                    .execute()
                    .data
                    or []
                )
                if summary_rows:
                    summary_row = summary_rows[0]
            except Exception:
                pass
        try:
            history_rows = (
                supabase.table("chat_history")
                .select("conversation_id, original_chat_id, property_id, content, created_at, client_name, channel")
                .eq("conversation_id", clean_id)
                .eq("channel", channel)
                .eq("property_id", property_id)
                .order("created_at", desc=True)
                .limit(100)
                .execute()
                .data
                or []
            )
        except Exception:
            history_rows = []
        if history_rows and (
            summary_row is None
            or _normalize_property_id(summary_row.get("property_id")) != property_id
        ):
            summary_row = history_rows[0]
        if summary_row is None or _normalize_property_id(summary_row.get("property_id")) != property_id:
            raise HTTPException(status_code=404, detail="Chat no encontrado")
        target_original_chat_id = str((summary_row or {}).get("original_chat_id") or "").strip()
        if not target_original_chat_id:
            for row in history_rows:
                candidate_original = str((row or {}).get("original_chat_id") or "").strip()
                if candidate_original:
                    target_original_chat_id = candidate_original
                    break
        now_iso = datetime.now(timezone.utc).isoformat()
        if target_original_chat_id:
            (
                supabase.table("chat_history")
                .update({"archived_at": now_iso})
                .eq("original_chat_id", target_original_chat_id)
                .eq("channel", channel)
                .execute()
            )
        (
            supabase.table("chat_history")
            .update({"archived_at": now_iso})
            .eq("conversation_id", clean_id)
            .eq("property_id", property_id)
            .eq("channel", channel)
            .execute()
        )

        last = summary_row or {}
        prop_id = property_id
        phone = _extract_guest_phone(clean_id)
        folio_id = None
        reservation_locator = None
        checkin = None
        checkout = None
        reservation_client_name = None
        reservation_status = None
        room_number = None
        memory_manager = getattr(state, "memory_manager", None)
        if memory_manager:
            try:
                folio_id = memory_manager.get_flag(clean_id, "folio_id") or memory_manager.get_flag(clean_id, "origin_folio_id")
                reservation_locator = memory_manager.get_flag(clean_id, "reservation_locator") or memory_manager.get_flag(clean_id, "origin_folio_code")
                checkin = memory_manager.get_flag(clean_id, "checkin") or memory_manager.get_flag(clean_id, "origin_folio_min_checkin")
                checkout = memory_manager.get_flag(clean_id, "checkout") or memory_manager.get_flag(clean_id, "origin_folio_max_checkout")
                reservation_status = memory_manager.get_flag(clean_id, "reservation_status")
                room_number = memory_manager.get_flag(clean_id, "room_number")
            except Exception:
                pass
        try:
            active = get_active_chat_reservation(chat_id=clean_id, property_id=prop_id)
            if active:
                folio_id = active.get("folio_id") or folio_id
                reservation_locator = active.get("reservation_locator") if isinstance(active, dict) else reservation_locator
                checkin = active.get("checkin") or checkin
                checkout = active.get("checkout") or checkout
                reservation_client_name = active.get("client_name") if isinstance(active, dict) else None
            if memory_manager and folio_id:
                memory_manager.set_flag(clean_id, "folio_id", folio_id)
            if memory_manager and reservation_locator:
                memory_manager.set_flag(clean_id, "reservation_locator", reservation_locator)
            if memory_manager and checkin:
                memory_manager.set_flag(clean_id, "checkin", checkin)
            if memory_manager and checkout:
                memory_manager.set_flag(clean_id, "checkout", checkout)
        except Exception:
            pass
        whatsapp_phone_number = None
        if instance_id:
            try:
                instance_payload = fetch_instance_by_code(instance_id) or {}
                instance_number = _resolve_instance_number(instance_payload)
                whatsapp_phone_number = _to_international_phone(instance_number or "")
            except Exception:
                whatsapp_phone_number = None
        bookai_resolution = _bookai_flag_resolution(
            _bookai_settings(state),
            aliases=_related_memory_ids(state, clean_id) or [],
            chat_id=clean_id,
            property_id=prop_id,
            instance_id=instance_id,
            default=True,
        )
        whatsapp_window = _resolve_whatsapp_window_for_chat(
            clean_id,
            property_id=prop_id,
            channel=channel,
            original_chat_id=str(last.get("original_chat_id") or "").strip() or None,
        )
        try:
            client_language, client_language_confidence = _resolve_guest_lang_meta_for_chat(
                state,
                clean_id,
                context_id=str(last.get("original_chat_id") or "").strip() or None,
            )
        except Exception:
            client_language, client_language_confidence = "es", 0.0
        socket_mgr = getattr(state, "socket_manager", None)
        if socket_mgr and getattr(socket_mgr, "enabled", False):
            await socket_mgr.emit(
                "chat.list.updated",
                {
                    "property_id": prop_id,
                    "action": "archived",
                    "chat": {
                        "chat_id": clean_id,
                        "property_id": prop_id,
                        "reservation_id": folio_id,
                        "reservation_locator": reservation_locator,
                        "reservation_status": reservation_status,
                        "room_number": room_number,
                        "checkin": checkin,
                        "checkout": checkout,
                        "channel": last.get("channel") or channel,
                        "last_message": last.get("content"),
                        "last_message_at": last.get("created_at"),
                        "avatar": None,
                        "client_name": reservation_client_name or last.get("client_name"),
                        "client_language": client_language,
                        "client_language_confidence": _normalize_language_confidence(
                            client_language_confidence,
                            default=0.0,
                        ),
                        "client_phone": phone or clean_id,
                        "whatsapp_phone_number": whatsapp_phone_number,
                        "whatsapp_window": whatsapp_window,
                        "bookai_enabled": bool(bookai_resolution.get("value")),
                        "unread_count": 0,
                        **_pending_snapshot_for_chat(
                            clean_id,
                            prop_id,
                            instance_id=instance_id,
                            memory_manager=memory_manager,
                        ),
                        "folio_id": folio_id,
                    },
                },
                rooms=f"property:{prop_id}",
                instance_id=instance_id,
            )

        return {
            "chat_id": clean_id,
            "property_id": prop_id,
            "archived": True,
        }

    @router.post("/chats/{chat_id}/hide")
    async def hide_chat(
        chat_id: str,
        property_id: Optional[str] = Query(default=None),
        auth_ctx: Dict[str, Optional[str]] = Depends(_verify_bearer),
    ):
        clean_id = _clean_chat_id(chat_id) or chat_id
        property_id = _normalize_property_id(property_id)
        channel = "whatsapp"
        instance_id = str((auth_ctx or {}).get("instance_id") or "").strip() or None
        summary_row: Dict[str, Any] | None = None
        history_rows: List[Dict[str, Any]] = []
        try:
            summary_query = (
                supabase.table("chat_last_message")
                .select("conversation_id, original_chat_id, property_id, content, created_at, client_name, channel")
                .eq("conversation_id", clean_id)
                .eq("channel", channel)
            )
            if property_id is not None:
                summary_query = summary_query.eq("property_id", property_id)
            summary_rows = summary_query.order("created_at", desc=True).limit(20).execute().data or []
            if summary_rows:
                summary_row = summary_rows[0]
        except Exception:
            summary_row = None
        if property_id is None and summary_row is not None:
            property_id = _normalize_property_id(summary_row.get("property_id"))
        if property_id is None:
            property_id = _normalize_property_id(_resolve_property_id_from_history(clean_id, channel))
        if property_id is None and getattr(state, "memory_manager", None):
            try:
                property_id = _normalize_property_id(state.memory_manager.get_flag(clean_id, "property_id"))
            except Exception:
                property_id = None
        if property_id is None:
            raise HTTPException(status_code=422, detail="property_id requerido")
        if summary_row is None or _normalize_property_id(summary_row.get("property_id")) != property_id:
            try:
                summary_rows = (
                    supabase.table("chat_last_message")
                    .select("conversation_id, original_chat_id, property_id, content, created_at, client_name, channel")
                    .eq("conversation_id", clean_id)
                    .eq("channel", channel)
                    .eq("property_id", property_id)
                    .order("created_at", desc=True)
                    .limit(20)
                    .execute()
                    .data
                    or []
                )
                if summary_rows:
                    summary_row = summary_rows[0]
            except Exception:
                pass
        try:
            history_rows = (
                supabase.table("chat_history")
                .select("conversation_id, original_chat_id, property_id, content, created_at, client_name, channel")
                .eq("conversation_id", clean_id)
                .eq("channel", channel)
                .eq("property_id", property_id)
                .order("created_at", desc=True)
                .limit(100)
                .execute()
                .data
                or []
            )
        except Exception:
            history_rows = []
        if history_rows and (
            summary_row is None
            or _normalize_property_id(summary_row.get("property_id")) != property_id
        ):
            summary_row = history_rows[0]
        if summary_row is None or _normalize_property_id(summary_row.get("property_id")) != property_id:
            raise HTTPException(status_code=404, detail="Chat no encontrado")
        target_original_chat_id = str((summary_row or {}).get("original_chat_id") or "").strip()
        if not target_original_chat_id:
            for row in history_rows:
                candidate_original = str((row or {}).get("original_chat_id") or "").strip()
                if candidate_original:
                    target_original_chat_id = candidate_original
                    break
        now_iso = datetime.now(timezone.utc).isoformat()
        if target_original_chat_id:
            (
                supabase.table("chat_history")
                .update({"hidden_at": now_iso})
                .eq("original_chat_id", target_original_chat_id)
                .eq("channel", channel)
                .execute()
            )
        (
            supabase.table("chat_history")
            .update({"hidden_at": now_iso})
            .eq("conversation_id", clean_id)
            .eq("property_id", property_id)
            .eq("channel", channel)
            .execute()
        )

        last = summary_row or {}
        prop_id = property_id
        phone = _extract_guest_phone(clean_id)
        folio_id = None
        reservation_locator = None
        checkin = None
        checkout = None
        reservation_client_name = None
        reservation_status = None
        room_number = None
        memory_manager = getattr(state, "memory_manager", None)
        if memory_manager:
            try:
                folio_id = memory_manager.get_flag(clean_id, "folio_id") or memory_manager.get_flag(clean_id, "origin_folio_id")
                reservation_locator = memory_manager.get_flag(clean_id, "reservation_locator") or memory_manager.get_flag(clean_id, "origin_folio_code")
                checkin = memory_manager.get_flag(clean_id, "checkin") or memory_manager.get_flag(clean_id, "origin_folio_min_checkin")
                checkout = memory_manager.get_flag(clean_id, "checkout") or memory_manager.get_flag(clean_id, "origin_folio_max_checkout")
                reservation_status = memory_manager.get_flag(clean_id, "reservation_status")
                room_number = memory_manager.get_flag(clean_id, "room_number")
            except Exception:
                pass
        try:
            active = get_active_chat_reservation(chat_id=clean_id, property_id=prop_id)
            if active:
                folio_id = active.get("folio_id") or folio_id
                reservation_locator = active.get("reservation_locator") if isinstance(active, dict) else reservation_locator
                checkin = active.get("checkin") or checkin
                checkout = active.get("checkout") or checkout
                reservation_client_name = active.get("client_name") if isinstance(active, dict) else None
            if memory_manager and folio_id:
                memory_manager.set_flag(clean_id, "folio_id", folio_id)
            if memory_manager and reservation_locator:
                memory_manager.set_flag(clean_id, "reservation_locator", reservation_locator)
            if memory_manager and checkin:
                memory_manager.set_flag(clean_id, "checkin", checkin)
            if memory_manager and checkout:
                memory_manager.set_flag(clean_id, "checkout", checkout)
        except Exception:
            pass
        whatsapp_phone_number = None
        if instance_id:
            try:
                instance_payload = fetch_instance_by_code(instance_id) or {}
                instance_number = _resolve_instance_number(instance_payload)
                whatsapp_phone_number = _to_international_phone(instance_number or "")
            except Exception:
                whatsapp_phone_number = None
        bookai_resolution = _bookai_flag_resolution(
            _bookai_settings(state),
            aliases=_related_memory_ids(state, clean_id) or [],
            chat_id=clean_id,
            property_id=prop_id,
            instance_id=instance_id,
            default=True,
        )
        whatsapp_window = _resolve_whatsapp_window_for_chat(
            clean_id,
            property_id=prop_id,
            channel=channel,
            original_chat_id=str(last.get("original_chat_id") or "").strip() or None,
        )
        try:
            client_language, client_language_confidence = _resolve_guest_lang_meta_for_chat(
                state,
                clean_id,
                context_id=str(last.get("original_chat_id") or "").strip() or None,
            )
        except Exception:
            client_language, client_language_confidence = "es", 0.0
        socket_mgr = getattr(state, "socket_manager", None)
        if socket_mgr and getattr(socket_mgr, "enabled", False):
            await socket_mgr.emit(
                "chat.list.updated",
                {
                    "property_id": prop_id,
                    "action": "deleted",
                    "chat": {
                        "chat_id": clean_id,
                        "property_id": prop_id,
                        "reservation_id": folio_id,
                        "reservation_locator": reservation_locator,
                        "reservation_status": reservation_status,
                        "room_number": room_number,
                        "checkin": checkin,
                        "checkout": checkout,
                        "channel": last.get("channel") or channel,
                        "last_message": last.get("content"),
                        "last_message_at": last.get("created_at"),
                        "avatar": None,
                        "client_name": reservation_client_name or last.get("client_name"),
                        "client_language": client_language,
                        "client_language_confidence": _normalize_language_confidence(
                            client_language_confidence,
                            default=0.0,
                        ),
                        "client_phone": phone or clean_id,
                        "whatsapp_phone_number": whatsapp_phone_number,
                        "whatsapp_window": whatsapp_window,
                        "bookai_enabled": bool(bookai_resolution.get("value")),
                        "unread_count": 0,
                        **_pending_snapshot_for_chat(
                            clean_id,
                            prop_id,
                            instance_id=instance_id,
                            memory_manager=memory_manager,
                        ),
                        "folio_id": folio_id,
                    },
                },
                rooms=f"property:{prop_id}",
                instance_id=instance_id,
            )

        return {
            "chat_id": clean_id,
            "property_id": prop_id,
            "hidden": True,
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
    async def send_template(
        payload: SendTemplateRequest,
        auth_ctx: Dict[str, Optional[str]] = Depends(_verify_bearer),
    ):
        chat_id = _clean_chat_id(payload.chat_id) or payload.chat_id
        if not _is_plausible_whatsapp_chat_id(chat_id):
            return JSONResponse(
                status_code=422,
                content={
                    "ok": False,
                    "code": "wa_invalid_phone",
                    "message": "El número de teléfono indicado no tiene una cuenta de WhatsApp.",
                },
            )
        property_id = _normalize_property_id(payload.property_id)
        if payload.channel.lower() != "whatsapp":
            raise HTTPException(status_code=422, detail="Canal no soportado")

        registry = _template_registry(state)
        template_code = payload.template_code
        language = (payload.language or "es").lower()
        payload_instance_id = (payload.instance_id or "").strip() or None
        token_instance_id = str((auth_ctx or {}).get("instance_id") or "").strip() or None
        if payload_instance_id and token_instance_id and payload_instance_id != token_instance_id:
            raise HTTPException(status_code=403, detail="instance_id no coincide con el token")
        instance_id = payload_instance_id or token_instance_id
        template_params_raw = dict(payload.parameters or {})
        folio_details_url_raw = extract_folio_details_url(template_params_raw)
        button_base_url = resolve_button_base_url(
            request_base_url=payload.button_base_url,
            params=template_params_raw,
        )
        template_params = strip_url_control_params(template_params_raw)

        template_def = None
        if registry:
            template_def = registry.resolve(
                instance_id=instance_id,
                template_code=template_code,
                language=language,
            )
        if not button_base_url and template_def:
            button_base_url = resolve_button_base_url(
                request_base_url=button_base_url,
                template_components=template_def.components,
            )

        if template_def:
            template_name = template_def.whatsapp_name or template_code
            parameters = template_def.build_meta_parameters(template_params)
            language = template_def.language or language
        else:
            template_name = template_code
            raw_params = template_params
            if isinstance(raw_params, dict):
                parameters = [
                    {
                        "type": "text",
                        "parameter_name": str(key),
                        "text": "" if val is None else str(val),
                    }
                    for key, val in raw_params.items()
                ]
            else:
                parameters = list(raw_params)

        context_id = _resolve_whatsapp_context_id(state, chat_id, instance_id=instance_id)
        session_id = context_id or chat_id
        chat_visible_before = False
        structured_payload = None
        structured_csv = None
        folio_id = None
        reservation_locator = None
        checkin = None
        checkout = None
        reservation_client_name = _extract_reservation_client_name(payload.parameters or {})
        folio_from_params = False
        try:
            if payload.parameters:
                f_id, ci, co = _extract_reservation_fields(payload.parameters)
                folio_id = f_id
                folio_from_params = bool(f_id)
                checkin = ci
                checkout = co
                reservation_locator = _extract_reservation_locator(payload.parameters)
                reservation_client_name = _extract_reservation_client_name(payload.parameters) or reservation_client_name
            if payload.rendered_text:
                f_id, ci, co = _extract_from_text(payload.rendered_text)
                folio_id = folio_id or f_id
                checkin = checkin or ci
                checkout = checkout or co
        except Exception as exc:
            log.warning("No se pudo extraer folio/checkin/checkout: %s", exc)

        if state.memory_manager:
            if property_id is not None:
                for mem_id in [session_id, context_id, chat_id]:
                    if mem_id:
                        state.memory_manager.set_flag(mem_id, "property_id", property_id)
            if instance_id:
                for mem_id in [session_id, context_id, chat_id]:
                    if mem_id:
                        state.memory_manager.set_flag(mem_id, "instance_id", instance_id)
                        state.memory_manager.set_flag(mem_id, "instance_hotel_code", instance_id)
            if payload.parameters:
                inferred_name = _extract_property_name(payload.parameters)
                if inferred_name:
                    for mem_id in [session_id, context_id, chat_id]:
                        if mem_id:
                            state.memory_manager.set_flag(mem_id, "property_name", inferred_name)
            if folio_id:
                for mem_id in [session_id, context_id, chat_id]:
                    if mem_id:
                        state.memory_manager.set_flag(mem_id, "folio_id", folio_id)
            if reservation_locator:
                for mem_id in [session_id, context_id, chat_id]:
                    if mem_id:
                        state.memory_manager.set_flag(mem_id, "reservation_locator", reservation_locator)
            if checkin:
                for mem_id in [session_id, context_id, chat_id]:
                    if mem_id:
                        state.memory_manager.set_flag(mem_id, "checkin", checkin)
            if checkout:
                for mem_id in [session_id, context_id, chat_id]:
                    if mem_id:
                        state.memory_manager.set_flag(mem_id, "checkout", checkout)
            if button_base_url:
                for mem_id in [session_id, context_id, chat_id]:
                    if mem_id:
                        state.memory_manager.set_flag(mem_id, "button_base_url", button_base_url)
                        state.memory_manager.set_flag(mem_id, "folio_base_url", button_base_url)
            ensure_instance_credentials(state.memory_manager, session_id)
            if not instance_id:
                try:
                    instance_id = (
                        state.memory_manager.get_flag(session_id, "instance_id")
                        or state.memory_manager.get_flag(session_id, "instance_hotel_code")
                    )
                except Exception:
                    instance_id = None
            if not button_base_url:
                try:
                    for mem_id in [session_id, context_id, chat_id]:
                        if not mem_id:
                            continue
                        candidate = (
                            state.memory_manager.get_flag(mem_id, "button_base_url")
                            or state.memory_manager.get_flag(mem_id, "folio_base_url")
                        )
                        button_base_url = resolve_button_base_url(request_base_url=candidate)
                        if button_base_url:
                            break
                except Exception:
                    button_base_url = None

        precheck = await state.channel_manager.check_recipient_has_whatsapp_account(
            chat_id,
            channel="whatsapp",
            context_id=context_id,
            request_id=f"chatter:{template_name}:{chat_id}",
        )
        if not bool(precheck.get("hasWhatsApp", True)):
            masked_phone = f"{chat_id[:2]}***{chat_id[-2:]}" if len(chat_id or "") > 4 else "***"
            log.warning(
                "[WA_PRECHECK_BLOCK] phone=%s chat_id=%s reservation_id=%s reason=%s",
                masked_phone,
                chat_id,
                folio_id or reservation_locator,
                precheck.get("reason") or "not_on_whatsapp",
            )
            return JSONResponse(
                status_code=422,
                content={
                    "ok": False,
                    "code": "wa_no_account",
                    "message": "El número de teléfono indicado no tiene una cuenta de WhatsApp.",
                },
            )

        outbound_parameters = parameters
        button_url_value = None
        if template_def:
            url_button_indexes = extract_url_button_indexes(template_def.components)
            # Compatibilidad con definiciones legacy de plantilla sin metadata de components.
            if (
                not url_button_indexes
                and template_def.code in {"booking_confirmation_aldahotels_v1", "reserva_confirmation_aldahotels_v1"}
                and reservation_locator
            ):
                url_button_indexes = [0]
            if folio_details_url_raw:
                button_url_value = to_folio_dynamic_part(folio_details_url_raw, button_base_url)
            elif reservation_locator:
                button_url_value = reservation_locator
            if url_button_indexes and button_url_value:
                outbound_parameters = {
                    "body": parameters,
                    "buttons": [
                        {
                            "index": idx,
                            "sub_type": "url",
                            "text": button_url_value,
                        }
                        for idx in url_button_indexes
                    ],
                }

        try:
            await state.channel_manager.send_template_message(
                chat_id,
                template_name,
                parameters=outbound_parameters,
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
                    client_name=reservation_client_name,
                    source="template",
                )
            except Exception as exc:
                log.warning("No se pudo persistir reserva en tabla: %s", exc)

        if folio_id and folio_from_params and (not checkin or not checkout):
            try:
                consulta_tool = create_consulta_reserva_persona_tool(
                    memory_manager=state.memory_manager,
                    chat_id=session_id,
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
                    reservation_client_name = _extract_client_name_from_reservation(parsed) or reservation_client_name
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
                            client_name=reservation_client_name,
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
                        client_name=reservation_client_name,
                        source="rendered",
                    )
                except Exception as exc:
                    log.warning("No se pudo persistir reservation_locator desde rendered: %s", exc)
            for mem_id in [session_id, chat_id]:
                if mem_id:
                    state.memory_manager.set_flag(mem_id, "default_channel", "whatsapp")
            chat_visible_before = is_chat_visible_in_list(
                chat_id,
                property_id=property_id,
                channel="whatsapp",
                original_chat_id=context_id or None,
            )
            resolved_cta_url = build_folio_details_url(button_base_url, folio_details_url_raw)
            cta_action = "open_url" if resolved_cta_url else None
            structured_payload = build_template_structured_payload(
                template_code=template_def.code if template_def else template_code,
                template_name=template_name,
                language=language,
                parameters=template_params_raw,
                reservation_locator=reservation_locator,
                folio_id=folio_id,
                guest_name=reservation_client_name,
                hotel_name=_extract_property_name(template_params_raw),
                checkin=checkin,
                checkout=checkout,
                cta_action=cta_action,
                cta_url=resolved_cta_url,
            )
            structured_csv = extract_structured_csv(structured_payload)
            if rendered:
                if property_id is not None:
                    state.memory_manager.set_flag(chat_id, "property_id", property_id)
                state.memory_manager.save(
                    session_id,
                    role="bookai",
                    content=rendered,
                    channel="whatsapp",
                    original_chat_id=context_id or None,
                    structured_payload=structured_payload,
                )
            if property_id is not None:
                state.memory_manager.set_flag(chat_id, "property_id", property_id)
            state.memory_manager.save(
                session_id,
                role="system",
                content=f"[TEMPLATE_SENT] plantilla={template_name} lang={language}",
                channel="whatsapp",
                original_chat_id=context_id or None,
            )
        except Exception as exc:
            log.warning("No se pudo registrar plantilla en memoria: %s", exc)
        try:
            await sync_guest_offer_state_from_sent_wa(
                state,
                guest_id=chat_id,
                sent_message=rendered or template_name,
                source="chatter_template",
                session_id=session_id,
                property_id=property_id,
            )
        except Exception:
            pass

        now_iso = datetime.now(timezone.utc).isoformat()
        rooms = _rooms(chat_id, property_id, "whatsapp")
        chat_visible_after = is_chat_visible_in_list(
            chat_id,
            property_id=property_id,
            channel="whatsapp",
            original_chat_id=context_id or None,
        )
        try:
            client_language, client_language_confidence = _resolve_guest_lang_meta_for_chat(
                state,
                chat_id,
                context_id=context_id or None,
            )
        except Exception:
            client_language, client_language_confidence = "es", 0.0
        whatsapp_window = _resolve_whatsapp_window_for_chat(
            chat_id,
            property_id=property_id,
            channel="whatsapp",
            original_chat_id=context_id or None,
        )
        socket_mgr = getattr(state, "socket_manager", None)
        if (
            socket_mgr
            and getattr(socket_mgr, "enabled", False)
            and property_id is not None
            and not chat_visible_before
            and chat_visible_after
        ):
            reservation_status = None
            room_number = None
            if state.memory_manager:
                try:
                    reservation_status = state.memory_manager.get_flag(session_id, "reservation_status")
                    room_number = state.memory_manager.get_flag(session_id, "room_number")
                except Exception:
                    pass
            whatsapp_phone_number = None
            if instance_id:
                try:
                    instance_payload = fetch_instance_by_code(instance_id) or {}
                    instance_number = _resolve_instance_number(instance_payload)
                    whatsapp_phone_number = _to_international_phone(instance_number or "")
                except Exception:
                    whatsapp_phone_number = None
            bookai_resolution = _bookai_flag_resolution(
                _bookai_settings(state),
                aliases=_related_memory_ids(state, chat_id) or [],
                chat_id=chat_id,
                property_id=property_id,
                instance_id=instance_id,
                default=True,
            )
            await socket_mgr.emit(
                "chat.list.updated",
                {
                    "property_id": property_id,
                    "action": "created",
                    "chat": {
                        "chat_id": chat_id,
                        "property_id": property_id,
                        "reservation_id": folio_id,
                        "reservation_locator": reservation_locator,
                        "reservation_status": reservation_status,
                        "room_number": room_number,
                        "checkin": checkin,
                        "checkout": checkout,
                        "channel": "whatsapp",
                        "last_message": rendered or template_name,
                        "last_message_at": now_iso,
                        "avatar": None,
                        "client_name": reservation_client_name,
                        "client_language": client_language,
                        "client_language_confidence": _normalize_language_confidence(
                            client_language_confidence,
                            default=0.0,
                        ),
                        "client_phone": _extract_guest_phone(chat_id) or chat_id,
                        "whatsapp_phone_number": whatsapp_phone_number,
                        "whatsapp_window": whatsapp_window,
                        "bookai_enabled": bool(bookai_resolution.get("value")),
                        "unread_count": 0,
                        **_pending_snapshot_for_chat(
                            chat_id,
                            property_id,
                            instance_id=instance_id,
                            memory_manager=getattr(state, "memory_manager", None),
                        ),
                        "folio_id": folio_id,
                    },
                },
                rooms=f"property:{property_id}",
                instance_id=instance_id,
            )
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
                "button_base_url": button_base_url,
                "structured_payload": structured_payload,
                "structured_csv": structured_csv,
                "client_language": client_language,
                "client_language_confidence": _normalize_language_confidence(
                    client_language_confidence,
                    default=0.0,
                ),
                "whatsapp_window": whatsapp_window,
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
                "whatsapp_window": whatsapp_window,
                **_pending_snapshot_for_chat(
                    chat_id,
                    property_id,
                    instance_id=instance_id,
                    memory_manager=getattr(state, "memory_manager", None),
                ),
            },
        )

        return {
            "status": "sent",
            "chat_id": chat_id,
            "template": template_name,
            "language": language,
            "instance_id": instance_id,
            "button_base_url": button_base_url,
            "structured_payload": structured_payload,
            "structured_csv": structured_csv,
        }

    @router.get("/chats/{chat_id}/window")
    async def check_window(
        chat_id: str,
        property_id: Optional[str] = Query(default=None),
        _: None = Depends(_verify_bearer),
    ):
        clean_id = _clean_chat_id(chat_id) or chat_id
        property_id = _normalize_property_id(property_id)
        last_guest_message_at = _resolve_last_guest_message_at(
            clean_id,
            property_id=property_id,
            channel="whatsapp",
        )
        last_template_sent_at = _resolve_last_template_sent_at(
            clean_id,
            property_id=property_id,
            channel="whatsapp",
        )
        whatsapp_window = _build_whatsapp_window(
            last_guest_message_at,
            last_template_sent_at,
        )
        last_guest_dt = _parse_ts(last_guest_message_at)
        if last_guest_dt and last_guest_dt.tzinfo is None:
            last_guest_dt = last_guest_dt.replace(tzinfo=timezone.utc)
        hours_since_last_guest_msg = None
        if last_guest_dt:
            now = datetime.now(timezone.utc)
            hours_since_last_guest_msg = round((now - last_guest_dt).total_seconds() / 3600.0, 2)

        status = str(whatsapp_window.get("status") or "").strip() or "expired"
        needs_template = status in {"expired", "waiting_for_reply"}
        if status == "waiting_for_reply":
            reason = "esperando_respuesta_huesped"
        elif status in {"active", "expiring"}:
            reason = "ventana_activa"
        else:
            reason = "ventana_superada"

        payload = {
            "chat_id": clean_id,
            "status": status,
            "needs_template": needs_template,
            "last_guest_message_at": _to_utc_z(last_guest_dt) if last_guest_dt else None,
            "hours_since_last_guest_msg": hours_since_last_guest_msg,
            "remaining_hours": whatsapp_window.get("remaining_hours"),
            "expires_at": whatsapp_window.get("expires_at"),
            "reason": reason,
        }
        payload["last_user_message_at"] = payload["last_guest_message_at"]
        payload["hours_since_last_user_msg"] = payload["hours_since_last_guest_msg"]
        return payload

    app.include_router(router)
