"""Rutas REST para recibir envíos de plantillas desde Roomdoo/Odoo."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import re
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from api.chatter_routes import (
    _bookai_flag_resolution,
    _bookai_settings,
    _extract_guest_phone,
    _pending_snapshot_for_chat,
    _related_memory_ids,
    _to_international_phone,
)
from core.config import Settings
from core.db import is_chat_visible_in_list, supabase
from core.template_registry import TemplateRegistry
from core.instance_context import ensure_instance_credentials, fetch_instance_by_code
from core.offer_semantics import sync_guest_offer_state_from_sent_wa
from tools.superintendente_tool import create_consulta_reserva_persona_tool
from core.db import upsert_chat_reservation

log = logging.getLogger("TemplateRoutes")

try:
    import phonenumbers
    from phonenumbers import NumberParseException
except Exception:
    phonenumbers = None
    NumberParseException = Exception


# ---------------------------------------------------------------------------
# Modelos de entrada
# ---------------------------------------------------------------------------
class SourceHotel(BaseModel):
    id: Optional[int] = Field(default=None, description="ID interno en Odoo/Roomdoo")
    external_code: str = Field(..., description="Código externo del hotel (ej: H_PORTONOVO)")
    name: Optional[str] = Field(default=None, description="Nombre descriptivo del hotel")


class OriginFolio(BaseModel):
    id: Optional[int] = Field(default=None, description="ID del folio en Roomdoo")
    code: Optional[str] = Field(default=None, description="Código del folio (ej: F2600107)")
    name: Optional[str] = Field(default=None, description="Nombre público del folio (localizador)")
    min_checkin: Optional[str] = Field(
        default=None,
        description="Primera fecha de entrada dentro del folio (ISO 8601)",
    )
    max_checkout: Optional[str] = Field(
        default=None,
        description="Última fecha de salida dentro del folio (ISO 8601)",
    )


class Source(BaseModel):
    instance_url: str = Field(..., description="URL de la instancia en Roomdoo")
    db: Optional[str] = Field(default=None, description="Nombre de la base de datos")
    instance_id: Optional[str] = Field(default=None, description="Identificador lógico de la instancia")
    hotel: SourceHotel
    origin_folio: Optional[OriginFolio] = Field(
        default=None,
        description="Folio de origen (resumen de fechas y código)",
    )


class Recipient(BaseModel):
    phone: str = Field(..., description="Teléfono en formato E.164 (+34...)")
    country: Optional[str] = Field(default=None, description="Código de país ISO (opcional)")
    display_name: Optional[str] = Field(default=None, description="Nombre para mostrar del huésped")

    @model_validator(mode="before")
    def _strip_phone(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        phone = data.get("phone")
        if phone:
            data["phone"] = str(phone).strip()
        return data


class TemplatePayload(BaseModel):
    code: str = Field(..., description="Código interno de la plantilla (BookAi/Odoo)")
    language: str = Field(default="es", description="Código ISO del idioma")
    parameters: Dict[str, Any] = Field(default_factory=dict, description="Parámetros nominales de la plantilla")
    rendered_text: Optional[str] = Field(
        default=None,
        description="Texto renderizado de la plantilla (opcional, para contexto)",
    )


class MetaInfo(BaseModel):
    trigger: Optional[str] = None
    reservation_id: Optional[int] = None
    folio_id: Optional[int] = None
    property_id: Optional[int] = None
    instance_id: Optional[str] = None
    idempotency_key: Optional[str] = None


class SendTemplateRequest(BaseModel):
    source: Source
    recipient: Recipient
    template: TemplatePayload
    meta: Optional[MetaInfo] = None


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
        raise HTTPException(status_code=401, detail="Autenticación Bearer requerida")

    token = auth_header.split(" ", 1)[1].strip()
    token_map = _parse_token_instance_map()
    if token_map:
        instance_id = token_map.get(token)
        if not instance_id:
            raise HTTPException(status_code=403, detail="Token inválido")
        return {"token": token, "instance_id": instance_id}

    expected = (Settings.ROOMDOO_BEARER_TOKEN or "").strip()
    if not expected:
        log.error("ROOMDOO_BEARER_TOKEN/ROOMDOO_TOKEN_INSTANCE_MAP no configurado.")
        raise HTTPException(status_code=401, detail="Token de integración no configurado")
    if token != expected:
        raise HTTPException(status_code=403, detail="Token inválido")
    return {"token": token, "instance_id": None}


def _normalize_phone(phone: str) -> str:
    """Solo dígitos, para Meta Cloud API."""
    digits = re.sub(r"\D", "", phone or "")
    return digits


def _is_plausible_recipient_phone(phone: str, country: Optional[str] = None) -> bool:
    """
    Validación global de teléfono:
    - Longitud E.164 plausible.
    - Número posible/válido según plan nacional.
    - Bloquea líneas fijas (no WhatsApp objetivo).
    """
    digits = _normalize_phone(phone)
    if not digits:
        return False
    if len(digits) < 8 or len(digits) > 15:
        return False
    if not phonenumbers:
        return True

    raw = str(phone or "").strip()
    region = str(country or "").strip().upper() or None
    candidate = raw if raw.startswith("+") else f"+{digits}"

    try:
        parsed = phonenumbers.parse(candidate, region)
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


def _extract_property_name(params: Dict[str, Any]) -> Optional[str]:
    if not params:
        return None
    for key in ("hotel", "hotel_name", "property_name", "property"):
        val = params.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


_FOLIO_URL_PARAM_KEYS = ("folio_details_url", "folioDetailsUrl")
_FOLIO_BASE_URL_PARAM_KEYS = ("folio_base_url", "folioBaseUrl")


def _extract_folio_details_url(params: Dict[str, Any]) -> Optional[str]:
    if not params:
        return None
    for key in _FOLIO_URL_PARAM_KEYS:
        val = params.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _extract_folio_base_url(params: Dict[str, Any]) -> Optional[str]:
    if not params:
        return None
    for key in _FOLIO_BASE_URL_PARAM_KEYS:
        val = params.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _sanitize_base_url(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    if not re.match(r"^https?://", raw, re.IGNORECASE):
        return None
    if not raw.endswith("/"):
        raw += "/"
    return raw


def _build_folio_details_url(base_url: Optional[str], dynamic_part: Optional[str]) -> Optional[str]:
    dynamic = str(dynamic_part or "").strip()
    if not dynamic:
        return None
    if re.match(r"^https?://", dynamic, re.IGNORECASE):
        return dynamic
    if not base_url:
        return None
    return f"{base_url}{dynamic.lstrip('/')}"


def _to_folio_dynamic_part(raw_value: Optional[str], base_url: Optional[str]) -> Optional[str]:
    """
    Meta URL buttons con {{1}} esperan la parte dinámica, no la URL completa.
    Si llega una URL absoluta, intentamos recortar la base conocida o al menos host/scheme.
    """
    raw = str(raw_value or "").strip()
    if not raw:
        return None

    known_base = _sanitize_base_url(base_url)
    if known_base and raw.lower().startswith(known_base.lower()):
        tail = raw[len(known_base):].lstrip("/")
        return tail or None

    if re.match(r"^https?://", raw, re.IGNORECASE):
        parsed = urlsplit(raw)
        path = (parsed.path or "").lstrip("/")
        query = f"?{parsed.query}" if parsed.query else ""
        fragment = f"#{parsed.fragment}" if parsed.fragment else ""
        dynamic = f"{path}{query}{fragment}".strip()
        return dynamic or None

    return raw.lstrip("/") or None


def _extract_url_button_indexes(components: Any) -> list[int]:
    if not isinstance(components, list):
        return []
    indexes: list[int] = []
    for comp in components:
        if not isinstance(comp, dict):
            continue
        if str(comp.get("type") or "").strip().upper() != "BUTTONS":
            continue
        buttons = comp.get("buttons") or []
        if not isinstance(buttons, list):
            continue
        for idx, button in enumerate(buttons):
            if not isinstance(button, dict):
                continue
            if str(button.get("type") or "").strip().upper() == "URL":
                indexes.append(idx)
    return indexes


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


def _resolve_instance_number(instance_payload: Dict[str, Any]) -> Optional[str]:
    if not isinstance(instance_payload, dict):
        return None
    for key in ("display_phone_number", "whatsapp_number", "phone_number", "phone"):
        value = instance_payload.get(key)
        normalized = _normalize_phone(str(value or ""))
        if normalized:
            return normalized
    return None


def _build_context_id_from_instance(state, chat_id: str, instance_id: Optional[str] = None) -> Optional[str]:
    memory_manager = getattr(state, "memory_manager", None)
    clean = _normalize_phone(chat_id) or str(chat_id).strip()
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
    """Resuelve context_id (instancia:telefono) desde flags/memoria."""
    memory_manager = getattr(state, "memory_manager", None)
    if not memory_manager or not chat_id:
        return None

    clean = _normalize_phone(chat_id) or str(chat_id).strip()
    normalized_instance = str(instance_id or "").strip() or None
    if clean:
        last_mem = memory_manager.get_flag(clean, "last_memory_id")
        if isinstance(last_mem, str) and last_mem.strip():
            candidate = last_mem.strip()
            if not normalized_instance:
                return candidate
            try:
                candidate_instance = (
                    memory_manager.get_flag(candidate, "instance_id")
                    or memory_manager.get_flag(candidate, "instance_hotel_code")
                )
            except Exception:
                candidate_instance = None
            if str(candidate_instance or "").strip() == normalized_instance:
                return candidate

    suffix = f":{clean}" if clean else ""
    if not suffix:
        return None

    for store_name in ("state_flags", "runtime_memory"):
        store = getattr(memory_manager, store_name, None)
        if isinstance(store, dict):
            for key in list(store.keys()):
                if isinstance(key, str) and key.endswith(suffix):
                    candidate = key.strip()
                    if normalized_instance:
                        try:
                            candidate_instance = (
                                memory_manager.get_flag(candidate, "instance_id")
                                or memory_manager.get_flag(candidate, "instance_hotel_code")
                            )
                        except Exception:
                            candidate_instance = None
                        if str(candidate_instance or "").strip() != normalized_instance:
                            continue
                    memory_manager.set_flag(clean, "last_memory_id", candidate)
                    return candidate

    return _build_context_id_from_instance(state, chat_id, instance_id=instance_id)


def _validate_instance_id(instance_id: Optional[str]) -> str:
    normalized = str(instance_id or "").strip()
    if not normalized:
        raise HTTPException(status_code=422, detail="instance_id requerido")
    instance_payload = fetch_instance_by_code(normalized)
    if not instance_payload:
        raise HTTPException(status_code=400, detail=f"instance_id no registrado: {normalized}")
    return normalized


def _chat_room_aliases(state, chat_id: str, context_id: Optional[str] = None) -> list[str]:
    aliases: list[str] = []
    seen: set[str] = set()
    base_chat_id = _normalize_phone(chat_id) or str(chat_id or "").strip()
    candidates = list(_related_memory_ids(state, base_chat_id) or [])
    for extra in (context_id, chat_id, base_chat_id):
        if extra:
            candidates.append(str(extra).strip())

    for raw_value in candidates:
        raw = str(raw_value or "").strip()
        if not raw:
            continue
        variants = [raw]
        clean = _normalize_phone(raw)
        if clean:
            variants.append(clean)
        if ":" in raw:
            tail = raw.split(":")[-1].strip()
            if tail:
                variants.append(tail)
                tail_clean = _normalize_phone(tail)
                if tail_clean:
                    variants.append(tail_clean)
        for variant in variants:
            candidate = str(variant or "").strip()
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            aliases.append(candidate)
    return aliases


def _rooms(state, chat_id: str, property_id: Optional[str | int], channel: str, context_id: Optional[str] = None) -> list[str]:
    aliases = _chat_room_aliases(state, chat_id, context_id=context_id) or [chat_id]
    rooms = [f"chat:{alias}" for alias in aliases]
    if property_id is not None:
        rooms.append(f"property:{property_id}")
    if channel:
        rooms.append(f"channel:{channel}")
    return rooms


def _restore_chat_visibility(
    chat_id: str,
    *,
    property_id: Optional[str | int],
    channel: str,
    original_chat_id: Optional[str] = None,
) -> bool:
    clean_id = _normalize_phone(chat_id) or str(chat_id or "").strip()
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


# ---------------------------------------------------------------------------
# Registro de rutas
# ---------------------------------------------------------------------------
def register_template_routes(app, state) -> None:
    router = APIRouter(prefix="/api/v1/whatsapp", tags=["whatsapp-templates"])
    registry: TemplateRegistry = getattr(state, "template_registry", None)

    async def _emit(event: str, payload: dict) -> None:
        socket_mgr = getattr(state, "socket_manager", None)
        if not socket_mgr or not getattr(socket_mgr, "enabled", False):
            return
        try:
            await socket_mgr.emit(event, payload, rooms=payload.get("rooms"))
        except Exception as exc:
            log.debug("No se pudo emitir evento socket: %s", exc)

    @router.post("/send-template")
    async def send_template(
        payload: SendTemplateRequest,
        auth_ctx: Dict[str, Optional[str]] = Depends(_verify_bearer),
    ):
        try:
            property_code = payload.source.hotel.external_code
            payload_instance_id = (
                (payload.meta.instance_id if payload.meta else None)
                or payload.source.instance_id
                or payload.source.instance_url
            )
            token_instance_id = str((auth_ctx or {}).get("instance_id") or "").strip() or None
            if payload_instance_id and token_instance_id and payload_instance_id != token_instance_id:
                raise HTTPException(status_code=403, detail="instance_id no coincide con el token")
            instance_id = token_instance_id or payload_instance_id
            instance_id = _validate_instance_id(instance_id)
            language = (payload.template.language or "es").lower()
            template_code = payload.template.code
            idempotency_key = (payload.meta.idempotency_key if payload.meta else "") or ""
            property_id = (
                (payload.meta.property_id if payload.meta else None)
                or payload.source.hotel.id
            )
            template_params = dict(payload.template.parameters or {})
            folio_details_url_raw = _extract_folio_details_url(template_params)
            folio_base_url_raw = _extract_folio_base_url(template_params)
            for control_key in _FOLIO_URL_PARAM_KEYS:
                template_params.pop(control_key, None)
            for control_key in _FOLIO_BASE_URL_PARAM_KEYS:
                template_params.pop(control_key, None)

            if idempotency_key:
                if idempotency_key in state.processed_template_keys:
                    return JSONResponse(
                        {"status": "duplicate", "idempotency_key": idempotency_key},
                        status_code=200,
                    )
                if len(state.processed_template_queue) >= state.processed_template_queue.maxlen:
                    old = state.processed_template_queue.popleft()
                    state.processed_template_keys.discard(old)
                state.processed_template_queue.append(idempotency_key)
                state.processed_template_keys.add(idempotency_key)

            template_def = registry.resolve(
                instance_id=instance_id,
                template_code=template_code,
                language=language,
            ) if registry else None

            wa_template = template_def.whatsapp_name if template_def else template_code
            if not template_def:
                for suffix in (f"__{language}", f"_{language}", f"-{language}"):
                    if wa_template.endswith(suffix):
                        wa_template = wa_template[: -len(suffix)]
                        break
            if template_def:
                parameters = template_def.build_meta_parameters(template_params)
                language = template_def.language or language
            else:
                raw_params = template_params
                if isinstance(raw_params, dict):
                    # Fallback robusto: si no resolvemos la plantilla en registry,
                    # preservamos nombres para plantillas NAMED de Meta.
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
            if not _is_plausible_recipient_phone(
                payload.recipient.phone,
                payload.recipient.country,
            ):
                return JSONResponse(
                    status_code=422,
                    content={
                        "ok": False,
                        "code": "wa_invalid_phone",
                        "message": "El número de teléfono indicado no tiene una cuenta de WhatsApp.",
                    },
                )
            chat_id = _normalize_phone(payload.recipient.phone)
            if not chat_id:
                raise HTTPException(status_code=422, detail="Teléfono de destino inválido")

            if payload.source.instance_url:
                try:
                    state.memory_manager.set_flag(chat_id, "instance_url", payload.source.instance_url)
                except Exception as exc:
                    log.warning("No se pudo guardar instance_url en memoria: %s", exc)
            if instance_id:
                try:
                    state.memory_manager.set_flag(chat_id, "instance_id", instance_id)
                    state.memory_manager.set_flag(chat_id, "instance_hotel_code", instance_id)
                except Exception as exc:
                    log.warning("No se pudo guardar instance_id en memoria: %s", exc)

            if payload.recipient.display_name:
                try:
                    state.memory_manager.set_flag(chat_id, "client_name", payload.recipient.display_name)
                except Exception as exc:
                    log.warning("No se pudo guardar display_name en memoria: %s", exc)

            if property_id is not None:
                try:
                    state.memory_manager.set_flag(chat_id, "property_id", property_id)
                    state.memory_manager.set_flag(chat_id, "wa_context_property_id", property_id)
                except Exception as exc:
                    log.warning("No se pudo guardar property_id en memoria: %s", exc)

            if property_code:
                try:
                    state.memory_manager.set_flag(chat_id, "property_name", property_code)
                    # property_name es nombre/label; instance_id se guarda aparte
                except Exception as exc:
                    log.warning("No se pudo guardar property_code en memoria: %s", exc)
            elif payload.template and payload.template.parameters:
                inferred_name = _extract_property_name(payload.template.parameters)
                if inferred_name:
                    property_code = inferred_name
                    try:
                        state.memory_manager.set_flag(chat_id, "property_name", property_code)
                    except Exception:
                        pass

            folio_id = None
            reservation_locator = None
            checkin = None
            checkout = None
            reservation_client_name = (payload.recipient.display_name or "").strip() or None
            folio_from_meta = False
            try:
                if payload.meta and payload.meta.folio_id is not None:
                    folio_id = str(payload.meta.folio_id)
                    folio_from_meta = True
                if payload.template and payload.template.parameters:
                    f_id, ci, co = _extract_reservation_fields(payload.template.parameters)
                    folio_id = folio_id or f_id
                    checkin = checkin or ci
                    checkout = checkout or co
                    reservation_locator = reservation_locator or _extract_reservation_locator(payload.template.parameters)
                    reservation_client_name = _extract_reservation_client_name(payload.template.parameters) or reservation_client_name
                if payload.template and payload.template.rendered_text:
                    f_id, ci, co = _extract_from_text(payload.template.rendered_text)
                    folio_id = folio_id or f_id
                    checkin = checkin or ci
                    checkout = checkout or co
            except Exception as exc:
                log.warning("No se pudo extraer folio/checkin/checkout desde parametros: %s", exc)

            if payload.source.origin_folio:
                try:
                    folio = payload.source.origin_folio
                    if folio.id is not None:
                        state.memory_manager.set_flag(chat_id, "origin_folio_id", folio.id)
                    if folio.code:
                        state.memory_manager.set_flag(chat_id, "origin_folio_code", folio.code)
                        reservation_locator = reservation_locator or folio.code
                    if folio.name:
                        reservation_locator = reservation_locator or folio.name
                    if folio.min_checkin:
                        state.memory_manager.set_flag(
                            chat_id,
                            "origin_folio_min_checkin",
                            folio.min_checkin,
                        )
                    if folio.max_checkout:
                        state.memory_manager.set_flag(
                            chat_id,
                            "origin_folio_max_checkout",
                            folio.max_checkout,
                        )
                    if folio.id is not None:
                        folio_id = folio_id or str(folio.id)
                        folio_from_meta = True
                    if folio.min_checkin:
                        checkin = checkin or folio.min_checkin
                    if folio.max_checkout:
                        checkout = checkout or folio.max_checkout
                except Exception as exc:
                    log.warning("No se pudo guardar origin_folio en memoria: %s", exc)

            context_id = _resolve_whatsapp_context_id(state, chat_id, instance_id=instance_id)
            session_id = context_id or chat_id
            chat_visible_before = False
            rendered = None
            log.info(
                "[TEMPLATE_SEND] request chat_id=%s property_id=%s instance_id=%s context_id=%s session_id=%s template=%s",
                chat_id,
                property_id,
                instance_id,
                context_id,
                session_id,
                wa_template,
            )
            if context_id:
                try:
                    state.memory_manager.set_flag(chat_id, "last_memory_id", context_id)
                    state.memory_manager.set_flag(chat_id, "guest_number", chat_id)
                    state.memory_manager.set_flag(chat_id, "force_guest_role", True)
                    for key in (
                        "instance_url",
                        "instance_id",
                        "instance_hotel_code",
                        "client_name",
                        "property_id",
                        "property_name",
                    ):
                        val = state.memory_manager.get_flag(chat_id, key)
                        if val is not None:
                            state.memory_manager.set_flag(context_id, key, val)
                except Exception:
                    pass
            try:
                targets = [chat_id, context_id] if context_id else [chat_id]
                if folio_id:
                    for target in targets:
                        state.memory_manager.set_flag(target, "folio_id", folio_id)
                if reservation_locator:
                    for target in targets:
                        state.memory_manager.set_flag(target, "reservation_locator", reservation_locator)
                if checkin:
                    for target in targets:
                        state.memory_manager.set_flag(target, "checkin", checkin)
                if checkout:
                    for target in targets:
                        state.memory_manager.set_flag(target, "checkout", checkout)
            except Exception as exc:
                log.warning("No se pudo guardar folio/checkin/checkout en memoria: %s", exc)

            ensure_instance_credentials(state.memory_manager, session_id)
            # Refuerzo: fija credenciales WA de la instancia resuelta por token,
            # evitando arrastre de otra instancia para el mismo guest chat_id.
            try:
                inst_payload = fetch_instance_by_code(instance_id) if instance_id else {}
                if inst_payload:
                    for target in [session_id, context_id, chat_id]:
                        if not target:
                            continue
                        for key in ("whatsapp_phone_id", "whatsapp_token", "whatsapp_verify_token"):
                            val = inst_payload.get(key)
                            if val:
                                state.memory_manager.set_flag(target, key, val)
            except Exception as exc:
                log.warning("No se pudo reforzar credenciales WA por instance_id: %s", exc)

            precheck = await state.channel_manager.check_recipient_has_whatsapp_account(
                chat_id,
                channel="whatsapp",
                context_id=context_id,
                request_id=idempotency_key or f"template_api:{wa_template}:{chat_id}",
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

            if folio_id and folio_from_meta:
                try:
                    log.info(
                        "🧾 template upsert_chat_reservation chat_id=%s folio_id=%s checkin=%s checkout=%s property_id=%s instance_id=%s",
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

            outbound_parameters = parameters
            if template_def:
                url_button_indexes = _extract_url_button_indexes(template_def.components)
                # Fallback: algunas tablas legacy no guardan `components` en Supabase.
                # Para la plantilla de confirmación con botón URL dinámico,
                # asumimos índice 0 si no hay metadata pero sí localizador.
                if (
                    not url_button_indexes
                    and template_def.code in {"booking_confirmation_aldahotels_v1", "reserva_confirmation_aldahotels_v1"}
                    and reservation_locator
                ):
                    url_button_indexes = [0]
                button_url_value = None
                if folio_details_url_raw:
                    base_url = _sanitize_base_url(folio_base_url_raw)
                    button_url_value = _to_folio_dynamic_part(folio_details_url_raw, base_url)
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

            await state.channel_manager.send_template_message(
                chat_id,
                wa_template,
                parameters=outbound_parameters,
                language=language,
                channel="whatsapp",
                context_id=context_id,
            )

            # Si falta checkin/checkout y hay folio_id, intenta enriquecer desde PMS.
            if folio_id and folio_from_meta and (not checkin or not checkout):
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
                        if folio_id and folio_from_meta:
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

            # Registrar evento para contexto futuro
            try:
                rendered = (payload.template.rendered_text or "").strip() or None
                if not rendered:
                    rendered = template_def.render_content(payload.template.parameters) if template_def else None
                if not rendered and template_def:
                    rendered = template_def.render_fallback_summary(payload.template.parameters)
                if not rendered and payload.template.parameters:
                    rendered = "Parametros de plantilla:\n" + "\n".join(
                        f"{k}: {v}" for k, v in payload.template.parameters.items()
                        if v is not None and str(v).strip() != ""
                    )
                if rendered and not reservation_locator:
                    m = re.search(r"(localizador)\s*[:#]?\s*([A-Za-z0-9/\\-]{4,})", rendered, re.IGNORECASE)
                    if m:
                        reservation_locator = m.group(2)
                        for target in [chat_id, context_id] if context_id else [chat_id]:
                            state.memory_manager.set_flag(target, "reservation_locator", reservation_locator)
                if reservation_locator and folio_id and folio_from_meta:
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
                if rendered and not folio_id:
                    try:
                        f_id, ci, co = _extract_from_text(rendered)
                        folio_id = folio_id or f_id
                        checkin = checkin or ci
                        checkout = checkout or co
                        targets = [chat_id, context_id] if context_id else [chat_id]
                        if folio_id:
                            for target in targets:
                                state.memory_manager.set_flag(target, "folio_id", folio_id)
                        if checkin:
                            for target in targets:
                                state.memory_manager.set_flag(target, "checkin", checkin)
                        if checkout:
                            for target in targets:
                                state.memory_manager.set_flag(target, "checkout", checkout)
                    except Exception as exc:
                        log.warning("No se pudo extraer folio/checkin/checkout desde rendered: %s", exc)
                chat_visible_before = is_chat_visible_in_list(
                    chat_id,
                    property_id=property_id,
                    channel="whatsapp",
                    original_chat_id=context_id or None,
                )
                log.info(
                    "[TEMPLATE_SEND] visibility.before chat_id=%s property_id=%s channel=whatsapp original_chat_id=%s visible=%s",
                    chat_id,
                    property_id,
                    context_id or None,
                    chat_visible_before,
                )
                if rendered:
                    for target in [chat_id, context_id] if context_id else [chat_id]:
                        state.memory_manager.set_flag(target, "default_channel", "whatsapp")
                    state.memory_manager.save(
                        session_id,
                        role="bookai",
                        content=rendered,
                        channel="whatsapp",
                        original_chat_id=context_id or None,
                    )
                meta_excerpt = f"trigger={payload.meta.trigger}" if payload.meta else ""
                source_tag = instance_id or payload.source.instance_url or property_code
                state.memory_manager.save(
                    session_id,
                    role="system",
                    content=(
                        f"[TEMPLATE_SENT] plantilla={wa_template} lang={language} instance={instance_id or ''} "
                        f"origen={source_tag} {meta_excerpt}"
                    ).strip(),
                    channel="whatsapp",
                    original_chat_id=context_id or None,
                )
            except Exception as exc:
                log.warning("No se pudo registrar el envío en memoria: %s", exc)
            try:
                await sync_guest_offer_state_from_sent_wa(
                    state,
                    guest_id=chat_id,
                    sent_message=rendered or wa_template,
                    source="template_api",
                    session_id=context_id or chat_id,
                    property_id=property_id,
                )
            except Exception:
                pass

            now_iso = datetime.now(timezone.utc).isoformat()
            rooms = _rooms(state, chat_id, property_id, "whatsapp", context_id=context_id)
            visibility_restored = False
            if not chat_visible_before:
                visibility_restored = _restore_chat_visibility(
                    chat_id,
                    property_id=property_id,
                    channel="whatsapp",
                    original_chat_id=context_id or None,
                )
                log.info(
                    "[TEMPLATE_SEND] visibility.restore chat_id=%s property_id=%s channel=whatsapp attempted=%s",
                    chat_id,
                    property_id,
                    visibility_restored,
                )
            chat_visible_after = is_chat_visible_in_list(
                chat_id,
                property_id=property_id,
                channel="whatsapp",
                original_chat_id=context_id or None,
            )
            log.info(
                "[TEMPLATE_SEND] visibility.after chat_id=%s property_id=%s channel=whatsapp original_chat_id=%s visible=%s",
                chat_id,
                property_id,
                context_id or None,
                chat_visible_after,
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
                        instance_number = _normalize_phone(_resolve_instance_number(instance_payload) or "")
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
                            "last_message": rendered or wa_template,
                            "last_message_at": now_iso,
                            "avatar": None,
                            "client_name": reservation_client_name,
                            "client_phone": _extract_guest_phone(chat_id) or chat_id,
                            "whatsapp_phone_number": whatsapp_phone_number,
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
                    "[TEMPLATE_SEND] emit chat.list.updated action=created chat_id=%s property_id=%s room=property:%s",
                    chat_id,
                    property_id,
                    property_id,
                )
            else:
                log.info(
                    "[TEMPLATE_SEND] skip chat.list.updated chat_id=%s property_id=%s visible_before=%s visible_after=%s",
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
                    "channel": "whatsapp",
                    "sender": "bookai",
                    "message": rendered or wa_template,
                    "created_at": now_iso,
                    "template": wa_template,
                    "template_language": language,
                },
            )
            log.info(
                "[TEMPLATE_SEND] emit chat.message.created chat_id=%s property_id=%s rooms=%s",
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
                    "channel": "whatsapp",
                    "last_message": rendered or wa_template,
                    "last_message_at": now_iso,
                    **_pending_snapshot_for_chat(
                        chat_id,
                        property_id,
                        instance_id=instance_id,
                        memory_manager=getattr(state, "memory_manager", None),
                    ),
                },
            )
            log.info(
                "[TEMPLATE_SEND] emit chat.updated chat_id=%s property_id=%s rooms=%s",
                chat_id,
                property_id,
                rooms,
            )

            return {
                "status": "sent",
                "template": wa_template,
                "chat_id": chat_id,
                "instance_id": instance_id,
                "language": language,
            }
        except HTTPException:
            raise
        except Exception as exc:
            log.error("Error en send-template: %s", exc, exc_info=True)
            raise HTTPException(status_code=500, detail=str(exc))

    app.include_router(router)
