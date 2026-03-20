from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import inspect
import logging
import os
import re
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from supabase import Client, create_client

from core.template_button_url import (
    build_folio_details_url,
    extract_folio_details_url,
    extract_url_button_indexes,
    resolve_button_base_url,
    strip_url_control_params,
    to_folio_dynamic_part,
)
from core.template_registry import TemplateDefinition
from core.template_structured import build_template_structured_payload

log = logging.getLogger("TemplateRoutes")

try:
    import phonenumbers
    from phonenumbers import NumberParseException
except Exception:  # pragma: no cover - dependencia opcional
    phonenumbers = None
    NumberParseException = Exception


class SourceHotel(BaseModel):
    id: Optional[int] = Field(default=None, description="ID interno Roomdoo/Odoo")
    external_code: str = Field(..., description="Código externo del hotel en Roomdoo")
    name: Optional[str] = Field(default=None, description="Nombre descriptivo del hotel")


class OriginFolio(BaseModel):
    id: Optional[int] = Field(default=None, description="ID del folio en Roomdoo")
    code: Optional[str] = Field(default=None, description="Código del folio")
    name: Optional[str] = Field(default=None, description="Nombre público del folio")
    min_checkin: Optional[str] = Field(default=None, description="Fecha de entrada ISO")
    max_checkout: Optional[str] = Field(default=None, description="Fecha de salida ISO")


class Source(BaseModel):
    instance_url: Optional[str] = Field(default=None, description="URL de la instancia Roomdoo")
    db: Optional[str] = Field(default=None, description="Base de datos origen")
    instance_id: Optional[str] = Field(default=None, description="Identificador lógico legado")
    hotel: SourceHotel
    origin_folio: Optional[OriginFolio] = Field(default=None, description="Resumen del folio")


class Recipient(BaseModel):
    phone: str = Field(..., description="Teléfono destino")
    country: Optional[str] = Field(default=None, description="País ISO")
    display_name: Optional[str] = Field(default=None, description="Nombre del huésped")

    @model_validator(mode="before")
    def _strip_phone(cls, data: dict[str, Any]) -> dict[str, Any]:
        phone = data.get("phone")
        if phone is not None:
            data["phone"] = str(phone).strip()
        return data


class TemplatePayload(BaseModel):
    code: str = Field(..., description="Código lógico de la plantilla")
    language: str = Field(default="es", description="Idioma")
    parameters: dict[str, Any] = Field(default_factory=dict, description="Parámetros nominales")
    rendered_text: Optional[str] = Field(default=None, description="Texto renderizado")


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
    button_base_url: Optional[str] = Field(default=None)


@dataclass(frozen=True)
class MessageContext:
    chat_id: str
    language: str
    idempotency_key: Optional[str]
    template_params_raw: dict[str, Any]
    template_params: dict[str, Any]
    button_base_url: Optional[str]
    folio_details_url_raw: Optional[str]
    folio_external_code: Optional[str]
    reservation_locator: Optional[str]
    checkin: Optional[str]
    checkout: Optional[str]
    guest_name: Optional[str]
    rendered_text: Optional[str]


@dataclass(frozen=True)
class TemplateResolution:
    template_id: int
    template: TemplateDefinition


@dataclass(frozen=True)
class ContactResolution:
    row: dict[str, Any]
    phone_code: str


@dataclass(frozen=True)
class ConversationResolution:
    row: dict[str, Any]
    created: bool


@dataclass(frozen=True)
class FolioResolution:
    folio_id: Optional[int]
    folio_external_code: Optional[str]


@dataclass(frozen=True)
class WhatsAppCredentials:
    phone_id: str
    token: str


@dataclass(frozen=True)
class SendResult:
    provider_message_id: Optional[str]
    raw_response: Any


def _create_database_client() -> Client:
    url = str(os.getenv("SUPABASE_URL") or "").strip()
    key = str(os.getenv("SUPABASE_KEY") or "").strip()
    if not url or not key:
        raise RuntimeError("Faltan SUPABASE_URL o SUPABASE_KEY.")
    return create_client(url, key)


def _single_row(rows: list[dict[str, Any]], *, not_found: Optional[HTTPException] = None) -> dict[str, Any]:
    if rows:
        return rows[0]
    if not_found is not None:
        raise not_found
    raise HTTPException(status_code=404, detail="Registro no encontrado")


def _fetch_single_row(
    client: Client,
    table: str,
    *,
    filters: dict[str, Any],
    columns: str = "*",
    order_by: Optional[str] = None,
    desc: bool = False,
    not_found: Optional[HTTPException] = None,
) -> dict[str, Any]:
    query = client.table(table).select(columns)
    for key, value in filters.items():
        query = query.eq(key, value)
    if order_by:
        query = query.order(order_by, desc=desc)
    rows = query.limit(1).execute().data or []
    return _single_row(rows, not_found=not_found)


def _find_single_row(
    client: Client,
    table: str,
    *,
    filters: dict[str, Any],
    columns: str = "*",
    order_by: Optional[str] = None,
    desc: bool = False,
) -> Optional[dict[str, Any]]:
    query = client.table(table).select(columns)
    for key, value in filters.items():
        query = query.eq(key, value)
    if order_by:
        query = query.order(order_by, desc=desc)
    rows = query.limit(1).execute().data or []
    return rows[0] if rows else None


def _insert_single_row(
    client: Client,
    table: str,
    payload: dict[str, Any],
    *,
    not_found: HTTPException,
) -> dict[str, Any]:
    rows = client.table(table).insert(payload).execute().data or []
    return _single_row(rows, not_found=not_found)


def _pick_first_text(payload: dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = payload.get(key)
        if value in (None, ""):
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _extract_bearer_token(auth_header: Optional[str]) -> str:
    if not auth_header or not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Autenticación Bearer requerida")

    token = auth_header.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Autenticación Bearer requerida")
    return token


def _get_instance_from_bearer_token(client: Client, auth_header: Optional[str]) -> dict[str, Any]:
    token = _extract_bearer_token(auth_header)
    return _fetch_single_row(
        client,
        "instances",
        filters={"bearer_token": token},
        not_found=HTTPException(status_code=403, detail="Token inválido"),
    )


def _validate_instance_access(instance_row: dict[str, Any]) -> dict[str, Any]:
    if not bool(instance_row.get("active")):
        raise HTTPException(status_code=403, detail="Instancia inactiva")
    if not bool(instance_row.get("bookai_enabled")):
        raise HTTPException(status_code=403, detail="Instancia sin acceso a BookAI")
    return instance_row


def _normalize_phone(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def _is_plausible_recipient_phone(phone: str, country: Optional[str] = None) -> bool:
    digits = _normalize_phone(phone)
    if not digits or len(digits) < 8 or len(digits) > 15:
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


def _canonical_template_code(raw_code: Optional[str], language: Optional[str]) -> str:
    code = str(raw_code or "").strip().lower()
    lang = str(language or "es").split("-")[0].strip().lower() or "es"
    for suffix in (f"__{lang}", f"_{lang}", f"-{lang}"):
        if code.endswith(suffix):
            return code[: -len(suffix)]
    return code


def _extract_reservation_fields(params: dict[str, Any]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    return (
        _pick_first_text(
            params,
            "folio_id",
            "folioId",
            "folio",
            "localizador",
            "reservation_id",
            "reserva_id",
            "id_reserva",
            "locator",
        ),
        _pick_first_text(
            params,
            "checkin",
            "check_in",
            "fecha_entrada",
            "entrada",
            "arrival",
            "checkin_date",
        ),
        _pick_first_text(
            params,
            "checkout",
            "check_out",
            "fecha_salida",
            "salida",
            "departure",
            "checkout_date",
        ),
    )


def _extract_reservation_locator(params: dict[str, Any]) -> Optional[str]:
    return _pick_first_text(params, "reservation_locator", "locator", "reservation_code", "code", "name")


def _extract_reservation_client_name(params: dict[str, Any]) -> Optional[str]:
    return _pick_first_text(
        params,
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
    )


def _extract_property_name(params: dict[str, Any]) -> Optional[str]:
    return _pick_first_text(params, "hotel", "hotel_name", "property_name", "property")


def _extract_from_text(text: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    if not text:
        return None, None, None

    folio_match = re.search(r"(folio(?:_id)?)\s*[:#]?\s*([A-Za-z0-9]{4,})", text, re.IGNORECASE)
    if not folio_match:
        folio_match = re.search(r"reserva\s*[:#]?\s*([A-Za-z0-9]{4,})", text, re.IGNORECASE)
    checkin_match = re.search(
        r"(entrada|check[- ]?in)\s*[:#]?\s*([0-9]{1,2}[-/][0-9]{1,2}[-/][0-9]{2,4})",
        text,
        re.IGNORECASE,
    )
    checkout_match = re.search(
        r"(salida|check[- ]?out)\s*[:#]?\s*([0-9]{1,2}[-/][0-9]{1,2}[-/][0-9]{2,4})",
        text,
        re.IGNORECASE,
    )

    folio_external_code = None
    if folio_match:
        group_index = 2 if folio_match.lastindex and folio_match.lastindex >= 2 else 1
        candidate = folio_match.group(group_index)
        if re.fullmatch(r"(?=.*\d)[A-Za-z0-9]{4,}", candidate or ""):
            folio_external_code = candidate

    return (
        folio_external_code,
        checkin_match.group(2) if checkin_match else None,
        checkout_match.group(2) if checkout_match else None,
    )


def _extract_provider_message_id(raw_response: Any) -> Optional[str]:
    if not isinstance(raw_response, dict):
        return None
    messages = raw_response.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    first_message = messages[0]
    if not isinstance(first_message, dict):
        return None
    provider_message_id = first_message.get("id")
    return str(provider_message_id) if provider_message_id is not None else None


def _prepare_message_context(payload: SendTemplateRequest) -> MessageContext:
    if not _is_plausible_recipient_phone(payload.recipient.phone, payload.recipient.country):
        raise HTTPException(
            status_code=422,
            detail="El número de teléfono indicado no tiene una cuenta de WhatsApp válida.",
        )

    chat_id = _normalize_phone(payload.recipient.phone)
    if not chat_id:
        raise HTTPException(status_code=422, detail="Teléfono de destino inválido")

    template_params_raw = dict(payload.template.parameters or {})
    folio_details_url_raw = extract_folio_details_url(template_params_raw)
    button_base_url = resolve_button_base_url(
        request_base_url=payload.button_base_url,
        params=template_params_raw,
    )
    template_params = strip_url_control_params(template_params_raw)

    folio_external_code = None
    if payload.meta and payload.meta.folio_id is not None:
        folio_external_code = str(payload.meta.folio_id).strip()
    elif payload.source.origin_folio and payload.source.origin_folio.id is not None:
        folio_external_code = str(payload.source.origin_folio.id).strip()

    raw_folio, raw_checkin, raw_checkout = _extract_reservation_fields(template_params_raw)
    reservation_locator = _extract_reservation_locator(template_params_raw)
    guest_name = (
        _extract_reservation_client_name(template_params_raw)
        or (payload.recipient.display_name or "").strip()
        or None
    )

    folio_external_code = folio_external_code or raw_folio
    checkin = raw_checkin
    checkout = raw_checkout

    if payload.source.origin_folio:
        folio_external_code = folio_external_code or (payload.source.origin_folio.code or "").strip() or None
        checkin = checkin or payload.source.origin_folio.min_checkin
        checkout = checkout or payload.source.origin_folio.max_checkout
        reservation_locator = (
            reservation_locator
            or (payload.source.origin_folio.code or "").strip()
            or (payload.source.origin_folio.name or "").strip()
            or None
        )

    rendered_text = (payload.template.rendered_text or "").strip() or None
    if rendered_text:
        rendered_folio, rendered_checkin, rendered_checkout = _extract_from_text(rendered_text)
        folio_external_code = folio_external_code or rendered_folio
        checkin = checkin or rendered_checkin
        checkout = checkout or rendered_checkout

    idempotency_key = payload.meta.idempotency_key if payload.meta else None
    if idempotency_key is not None:
        idempotency_key = str(idempotency_key).strip() or None

    return MessageContext(
        chat_id=chat_id,
        language=(payload.template.language or "es").strip().lower() or "es",
        idempotency_key=idempotency_key,
        template_params_raw=template_params_raw,
        template_params=template_params,
        button_base_url=button_base_url,
        folio_details_url_raw=folio_details_url_raw,
        folio_external_code=folio_external_code,
        reservation_locator=reservation_locator,
        checkin=checkin,
        checkout=checkout,
        guest_name=guest_name,
        rendered_text=rendered_text,
    )


def _reserve_idempotency_key(state, idempotency_key: Optional[str]) -> None:
    if idempotency_key:
        state.processed_template_keys.add(idempotency_key)


def _finalize_idempotency_key(state, idempotency_key: Optional[str], *, success: bool) -> None:
    if not idempotency_key:
        return
    if not success:
        state.processed_template_keys.discard(idempotency_key)
        return

    if idempotency_key not in state.processed_template_queue:
        if len(state.processed_template_queue) >= state.processed_template_queue.maxlen:
            old_key = state.processed_template_queue.popleft()
            state.processed_template_keys.discard(old_key)
        state.processed_template_queue.append(idempotency_key)
    state.processed_template_keys.add(idempotency_key)


def _check_duplicate_request(
    client: Client,
    state,
    idempotency_key: Optional[str],
) -> Optional[dict[str, Any]]:
    if not idempotency_key:
        return None

    key = str(idempotency_key).strip()
    if not key:
        return None

    if key in state.processed_template_keys:
        return {"status": "duplicate", "idempotency_key": key}

    try:
        rows = (
            client.table("external_messages")
            .select("id")
            .contains("template_payload", {"idempotency_key": key})
            .limit(1)
            .execute()
            .data
            or []
        )
    except Exception as exc:
        log.warning("No se pudo consultar duplicados por idempotency_key: %s", exc)
        rows = []

    if rows:
        return {
            "status": "duplicate",
            "idempotency_key": key,
            "message_id": rows[0].get("id"),
        }

    _reserve_idempotency_key(state, key)
    return None


def _resolve_property_by_instance(
    client: Client,
    payload: SendTemplateRequest,
    instance_row: dict[str, Any],
) -> dict[str, Any]:
    explicit_property_id = payload.meta.property_id if payload.meta else None
    if explicit_property_id is not None:
        return _fetch_single_row(
            client,
            "properties",
            filters={"id": explicit_property_id, "instance_id": instance_row["id"]},
            not_found=HTTPException(
                status_code=404,
                detail="Property no encontrada para la instancia autenticada",
            ),
        )

    property_external_code = str(payload.source.hotel.external_code or "").strip()
    if not property_external_code:
        raise HTTPException(status_code=422, detail="external_code del hotel requerido")

    return _fetch_single_row(
        client,
        "properties",
        filters={
            "instance_id": instance_row["id"],
            "roomdoo_external_code": property_external_code,
        },
        not_found=HTTPException(
            status_code=404,
            detail="Property no encontrada para la instancia autenticada",
        ),
    )


def _select_best_template_match(
    template_rows: list[dict[str, Any]],
    payload: SendTemplateRequest,
) -> Optional[TemplateResolution]:
    requested_code = str(payload.template.code or "").strip()
    requested_language = (payload.template.language or "es").strip().lower() or "es"
    requested_raw = requested_code.lower()
    requested_canonical = _canonical_template_code(requested_code, requested_language)

    best_match: Optional[TemplateResolution] = None
    best_score = -1
    for row in template_rows:
        definition = TemplateDefinition.from_dict(row)
        row_code = str(row.get("code") or "").strip().lower()
        row_name = str(row.get("whatsapp_name") or "").strip().lower()
        definition_code = str(definition.code or "").strip().lower()
        row_language = str(row.get("language") or definition.language or "es").split("-")[0].strip().lower() or "es"

        if row_code == requested_raw:
            score = 100
        elif definition_code == requested_canonical:
            score = 95
        elif row_name == requested_raw:
            score = 90
        elif row_name == requested_canonical:
            score = 85
        else:
            continue

        if row_language == requested_language:
            score += 10
        elif row_language == "es":
            score += 5

        if score > best_score:
            best_score = score
            best_match = TemplateResolution(
                template_id=int(row["id"]),
                template=definition,
            )

    return best_match


def _get_template(client: Client, payload: SendTemplateRequest) -> TemplateResolution:
    template_rows = (
        client.table("whatsapp_templates")
        .select("*")
        .eq("active", True)
        .execute()
        .data
        or []
    )
    if not template_rows:
        raise HTTPException(status_code=404, detail="No hay plantillas activas")

    template_resolution = _select_best_template_match(template_rows, payload)
    if template_resolution is None:
        raise HTTPException(status_code=404, detail="Plantilla no encontrada")
    return template_resolution


def _resolve_template_for_property(
    client: Client,
    payload: SendTemplateRequest,
    property_row: dict[str, Any],
) -> TemplateResolution:
    relation_rows = (
        client.table("rel_whatsapp_template_property")
        .select("whatsapp_template_id")
        .eq("property_id", property_row["id"])
        .execute()
        .data
        or []
    )
    template_ids = [
        row.get("whatsapp_template_id")
        for row in relation_rows
        if row.get("whatsapp_template_id") is not None
    ]
    if not template_ids:
        raise HTTPException(status_code=404, detail="La property no tiene plantillas asociadas")

    template_rows = (
        client.table("whatsapp_templates")
        .select("*")
        .in_("id", template_ids)
        .eq("active", True)
        .execute()
        .data
        or []
    )
    if not template_rows:
        raise HTTPException(status_code=404, detail="No hay plantillas activas para la property")

    template_resolution = _select_best_template_match(template_rows, payload)
    if template_resolution is None:
        raise HTTPException(status_code=404, detail="Plantilla no encontrada para la property")
    return template_resolution


def _ensure_template_for_property(
    client: Client,
    payload: SendTemplateRequest,
    property_row: dict[str, Any],
    template_resolution: TemplateResolution,
) -> TemplateResolution:
    relation_row = _find_single_row(
        client,
        "rel_whatsapp_template_property",
        filters={
            "property_id": property_row["id"],
            "whatsapp_template_id": template_resolution.template_id,
        },
        columns="property_id,whatsapp_template_id",
    )
    if relation_row:
        return template_resolution
    return _resolve_template_for_property(client, payload, property_row)


def _resolve_or_create_contact(
    client: Client,
    payload: SendTemplateRequest,
    chat_id: str,
) -> ContactResolution:
    display_name = (payload.recipient.display_name or "").strip() or None
    row = _find_single_row(
        client,
        "contact",
        filters={"whatsapp_external_code": chat_id},
    )
    if row:
        if display_name and not row.get("name"):
            updated_rows = (
                client.table("contact")
                .update({"name": display_name})
                .eq("id", row["id"])
                .execute()
                .data
                or [row]
            )
            row = updated_rows[0]
        return ContactResolution(row=row, phone_code=chat_id)

    insert_payload: dict[str, Any] = {"whatsapp_external_code": chat_id}
    if display_name:
        insert_payload["name"] = display_name

    row = _insert_single_row(
        client,
        "contact",
        insert_payload,
        not_found=HTTPException(status_code=500, detail="No se pudo crear el contacto"),
    )
    return ContactResolution(row=row, phone_code=chat_id)


def _get_or_create_conversation(
    client: Client,
    property_row: dict[str, Any],
    contact_row: dict[str, Any],
) -> ConversationResolution:
    row = _find_single_row(
        client,
        "external_conversations",
        filters={"property_id": property_row["id"], "contact_id": contact_row["id"]},
        order_by="created_at",
    )
    if row:
        return ConversationResolution(row=row, created=False)

    row = _insert_single_row(
        client,
        "external_conversations",
        {"property_id": property_row["id"], "contact_id": contact_row["id"]},
        not_found=HTTPException(status_code=500, detail="No se pudo crear la conversación pública"),
    )
    return ConversationResolution(row=row, created=True)


def _set_folio_for_conversation(
    client: Client,
    conversation_row: dict[str, Any],
    message_context: MessageContext,
) -> FolioResolution:
    folio_external_code = str(message_context.folio_external_code or "").strip() or None
    if not folio_external_code:
        return FolioResolution(folio_id=None, folio_external_code=None)

    folio_row = _find_single_row(
        client,
        "folios",
        filters={"odoo_external_code": folio_external_code},
        columns="id,odoo_external_code",
    )
    if not folio_row:
        folio_row = _insert_single_row(
            client,
            "folios",
            {"odoo_external_code": folio_external_code},
            not_found=HTTPException(status_code=500, detail="No se pudo crear el folio"),
        )

    folio_id = int(folio_row["id"])
    relation_row = _find_single_row(
        client,
        "rel_conversation_folio",
        filters={"conversation_id": conversation_row["id"], "folio_id": folio_id},
        columns="conversation_id,folio_id",
    )
    if not relation_row:
        client.table("rel_conversation_folio").insert(
            {"conversation_id": conversation_row["id"], "folio_id": folio_id}
        ).execute()

    return FolioResolution(folio_id=folio_id, folio_external_code=folio_external_code)


def _resolve_whatsapp_channel_id(client: Client) -> int:
    row = _fetch_single_row(
        client,
        "channels",
        filters={"name": "whatsapp"},
        columns="id,name",
        not_found=HTTPException(status_code=500, detail="Canal WhatsApp no configurado en channels"),
    )
    return int(row["id"])


def _resolve_whatsapp_credentials(client: Client, property_row: dict[str, Any]) -> WhatsAppCredentials:
    phone_row = _fetch_single_row(
        client,
        "whatsapp_phones",
        filters={"id": property_row["whatsapp_phone_id"]},
        not_found=HTTPException(status_code=500, detail="whatsapp_phone no configurado para la property"),
    )

    account_row = _fetch_single_row(
        client,
        "whatsapp_accounts",
        filters={"id": phone_row["whatsapp_account_id"]},
        not_found=HTTPException(status_code=500, detail="whatsapp_account no configurado para la property"),
    )

    phone_id = str(phone_row.get("whatsapp_external_code") or "").strip()
    token = str(account_row.get("whatsapp_token") or "").strip()
    if not phone_id or not token:
        raise HTTPException(status_code=500, detail="Credenciales de WhatsApp incompletas")

    return WhatsAppCredentials(phone_id=phone_id, token=token)


def _render_template_text(
    template: TemplateDefinition,
    payload: SendTemplateRequest,
    message_context: MessageContext,
) -> str:
    rendered_text = message_context.rendered_text
    if not rendered_text:
        rendered_text = template.render_content(message_context.template_params_raw)
    if not rendered_text:
        rendered_text = template.render_fallback_summary(message_context.template_params_raw)
    if not rendered_text:
        rendered_text = payload.template.code
    return rendered_text


def _build_outbound_parameters(
    template: TemplateDefinition,
    message_context: MessageContext,
) -> list[Any] | dict[str, Any]:
    parameters = template.build_meta_parameters(message_context.template_params)
    button_indexes = extract_url_button_indexes(template.components)
    if not button_indexes:
        return parameters

    button_url_value = None
    if message_context.folio_details_url_raw:
        button_url_value = to_folio_dynamic_part(
            message_context.folio_details_url_raw,
            message_context.button_base_url,
        )
    elif message_context.reservation_locator:
        button_url_value = message_context.reservation_locator

    if not button_url_value:
        return parameters

    return {
        "body": parameters,
        "buttons": [
            {
                "index": index,
                "sub_type": "url",
                "text": button_url_value,
            }
            for index in button_indexes
        ],
    }


def _build_template_payload(
    *,
    payload: SendTemplateRequest,
    instance_row: dict[str, Any],
    property_row: dict[str, Any],
    contact: ContactResolution,
    conversation: ConversationResolution,
    folio: FolioResolution,
    template_resolution: TemplateResolution,
    channel_id: int,
    message_context: MessageContext,
    rendered_text: str,
    language: str,
) -> dict[str, Any]:
    resolved_cta_url = build_folio_details_url(
        message_context.button_base_url,
        message_context.folio_details_url_raw,
    )
    hotel_name = (
        payload.source.hotel.name
        or _extract_property_name(message_context.template_params_raw)
        or property_row.get("name")
    )
    template_payload = build_template_structured_payload(
        template_code=template_resolution.template.code,
        template_name=template_resolution.template.whatsapp_name or template_resolution.template.code,
        language=language,
        parameters=message_context.template_params_raw,
        reservation_locator=message_context.reservation_locator,
        folio_id=folio.folio_external_code,
        guest_name=message_context.guest_name,
        hotel_name=hotel_name,
        checkin=message_context.checkin,
        checkout=message_context.checkout,
        cta_action="open_url" if resolved_cta_url else None,
        cta_url=resolved_cta_url,
        trigger=payload.meta.trigger if payload.meta else None,
    )
    if not isinstance(template_payload, dict):
        template_payload = {}

    template_payload.update(
        {
            "idempotency_key": message_context.idempotency_key,
            "instance_id": instance_row["id"],
            "instance_external_code": instance_row.get("roomdoo_external_code"),
            "property_id": property_row["id"],
            "property_external_code": property_row.get("roomdoo_external_code"),
            "contact_id": contact.row["id"],
            "contact_external_code": contact.phone_code,
            "conversation_id": conversation.row["id"],
            "channel_id": channel_id,
            "template_id": template_resolution.template_id,
            "template_code": template_resolution.template.code,
            "template_name": template_resolution.template.whatsapp_name or template_resolution.template.code,
            "language": language,
            "parameters": message_context.template_params_raw,
            "rendered_text": rendered_text,
            "folio_db_id": folio.folio_id,
            "folio_external_code": folio.folio_external_code,
        }
    )
    return template_payload


def _with_delivery_status(
    template_payload: dict[str, Any],
    *,
    status: str,
    error: Optional[str] = None,
    send_result: Optional[SendResult] = None,
) -> dict[str, Any]:
    updated_payload = dict(template_payload)
    updated_payload["delivery"] = {
        "status": status,
        "error": error,
        "provider_message_id": send_result.provider_message_id if send_result else None,
        "provider_response": send_result.raw_response if send_result else None,
        "sent_at": datetime.now(timezone.utc).isoformat() if status == "sent" else None,
    }
    return updated_payload


def _persist_message_status(
    client: Client,
    *,
    message_row: Optional[dict[str, Any]],
    template_payload: dict[str, Any],
    status: str,
    error: Optional[str] = None,
    send_result: Optional[SendResult] = None,
    conversation_row: Optional[dict[str, Any]] = None,
    channel_id: Optional[int] = None,
    rendered_text: Optional[str] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    updated_payload = _with_delivery_status(
        template_payload,
        status=status,
        error=error,
        send_result=send_result,
    )
    if message_row is None:
        inserted_rows = (
            client.table("external_messages")
            .insert(
                {
                    "conversation_id": conversation_row["id"],
                    "channel_id": channel_id,
                    "role": "bookai",
                    "content": rendered_text,
                    "read_status": False,
                    "ai_request_type": "template",
                    "template_payload": updated_payload,
                }
            )
            .execute()
            .data
            or []
        )
        return (
            _single_row(
                inserted_rows,
                not_found=HTTPException(status_code=500, detail="No se pudo registrar el mensaje público"),
            ),
            updated_payload,
        )

    updated_rows = (
        client.table("external_messages")
        .update({"template_payload": updated_payload})
        .eq("id", message_row["id"])
        .execute()
        .data
        or []
    )
    return (
        _single_row(
            updated_rows,
            not_found=HTTPException(status_code=500, detail="No se pudo actualizar el mensaje público"),
        ),
        updated_payload,
    )


async def _send_template_to_whatsapp(
    state,
    *,
    chat_id: str,
    template: TemplateDefinition,
    outbound_parameters: list[Any] | dict[str, Any],
    credentials: WhatsAppCredentials,
    language: str,
) -> SendResult:
    channel_manager = getattr(state, "channel_manager", None)
    if channel_manager is None:
        raise HTTPException(status_code=500, detail="ChannelManager no disponible")

    whatsapp_channel = getattr(channel_manager, "channels", {}).get("whatsapp")
    if whatsapp_channel is None:
        raise HTTPException(status_code=500, detail="Canal WhatsApp no disponible")

    phone_attr = "_dynamic_whatsapp_phone_id"
    token_attr = "_dynamic_whatsapp_token"
    marker = object()
    previous_phone = getattr(whatsapp_channel, phone_attr, marker)
    previous_token = getattr(whatsapp_channel, token_attr, marker)

    setattr(whatsapp_channel, phone_attr, credentials.phone_id)
    setattr(whatsapp_channel, token_attr, credentials.token)

    try:
        send_fn = getattr(whatsapp_channel, "send_template_message", None)
        if not send_fn:
            raise HTTPException(status_code=500, detail="El canal WhatsApp no implementa send_template_message")

        if inspect.iscoroutinefunction(send_fn):
            raw_response = await send_fn(
                chat_id,
                template.whatsapp_name or template.code,
                parameters=outbound_parameters,
                language=language,
            )
        else:
            raw_response = send_fn(
                chat_id,
                template.whatsapp_name or template.code,
                parameters=outbound_parameters,
                language=language,
            )
            if inspect.isawaitable(raw_response):
                raw_response = await raw_response

        ok = True if raw_response is None else bool(raw_response)
        if not ok:
            raise HTTPException(status_code=502, detail="WhatsApp no confirmó el envío de la plantilla")

        return SendResult(
            provider_message_id=_extract_provider_message_id(raw_response),
            raw_response=raw_response,
        )
    finally:
        if previous_phone is marker:
            try:
                delattr(whatsapp_channel, phone_attr)
            except AttributeError:
                pass
        else:
            setattr(whatsapp_channel, phone_attr, previous_phone)

        if previous_token is marker:
            try:
                delattr(whatsapp_channel, token_attr)
            except AttributeError:
                pass
        else:
            setattr(whatsapp_channel, token_attr, previous_token)


def _handle_send_error(
    client: Client,
    state,
    *,
    idempotency_key: Optional[str],
    provider_sent: bool,
    message_row: Optional[dict[str, Any]],
    template_payload: Optional[dict[str, Any]],
    detail: str,
) -> None:
    if provider_sent:
        _finalize_idempotency_key(state, idempotency_key, success=True)
        return

    if message_row is not None and template_payload is not None:
        try:
            _persist_message_status(
                client,
                message_row=message_row,
                template_payload=template_payload,
                status="failed",
                error=detail,
            )
        except Exception as exc:
            log.warning("No se pudo marcar el mensaje como fallido: %s", exc)

    _finalize_idempotency_key(state, idempotency_key, success=False)


async def _emit_socket_event(
    state,
    *,
    property_row: dict[str, Any],
    contact: ContactResolution,
    conversation: ConversationResolution,
    message_row: dict[str, Any],
    channel_id: int,
    template_resolution: TemplateResolution,
    template_payload: dict[str, Any],
    rendered_text: str,
    language: str,
) -> None:
    socket_manager = getattr(state, "socket_manager", None)
    if not socket_manager or not getattr(socket_manager, "enabled", False):
        return

    now_iso = datetime.now(timezone.utc).isoformat()
    rooms = [
        f"chat:{contact.phone_code}",
        f"property:{property_row['id']}",
        "channel:whatsapp",
    ]

    if conversation.created:
        await socket_manager.emit(
            "chat.list.updated",
            {
                "property_id": property_row["id"],
                "action": "created",
                "chat": {
                    "conversation_id": conversation.row["id"],
                    "chat_id": contact.phone_code,
                    "contact_id": contact.row["id"],
                    "property_id": property_row["id"],
                    "channel": "whatsapp",
                    "channel_id": channel_id,
                    "client_name": contact.row.get("name"),
                    "last_message": rendered_text,
                    "last_message_at": now_iso,
                },
            },
            rooms=f"property:{property_row['id']}",
        )

    await socket_manager.emit(
        "chat.message.created",
        {
            "rooms": rooms,
            "message_id": message_row["id"],
            "conversation_id": conversation.row["id"],
            "chat_id": contact.phone_code,
            "contact_id": contact.row["id"],
            "property_id": property_row["id"],
            "channel": "whatsapp",
            "channel_id": channel_id,
            "sender": "bookai",
            "message": rendered_text,
            "content": rendered_text,
            "created_at": now_iso,
            "template": template_resolution.template.whatsapp_name or template_resolution.template.code,
            "template_language": language,
            "template_payload": template_payload,
        },
        rooms=rooms,
    )
    await socket_manager.emit(
        "chat.updated",
        {
            "rooms": rooms,
            "conversation_id": conversation.row["id"],
            "chat_id": contact.phone_code,
            "contact_id": contact.row["id"],
            "property_id": property_row["id"],
            "channel": "whatsapp",
            "channel_id": channel_id,
            "last_message": rendered_text,
            "last_message_at": now_iso,
        },
        rooms=rooms,
    )


def register_template_routes(app, state) -> None:
    router = APIRouter(prefix="/api/v1/whatsapp", tags=["whatsapp-templates"])

    @router.post("/send-template")
    async def send_template(
        payload: SendTemplateRequest,
        authorization: Optional[str] = Header(None, alias="Authorization"),
    ):
        client = _create_database_client()

        instance_row = _validate_instance_access(_get_instance_from_bearer_token(client, authorization))
        message_context = _prepare_message_context(payload)

        duplicate = _check_duplicate_request(client, state, message_context.idempotency_key)
        if duplicate:
            return JSONResponse(duplicate, status_code=200)

        message_row: Optional[dict[str, Any]] = None
        template_payload: Optional[dict[str, Any]] = None
        provider_sent = False

        try:
            template_resolution = _get_template(client, payload)
            property_row = _resolve_property_by_instance(client, payload, instance_row)
            template_resolution = _ensure_template_for_property(
                client,
                payload,
                property_row,
                template_resolution,
            )
            contact = _resolve_or_create_contact(client, payload, message_context.chat_id)
            conversation = _get_or_create_conversation(client, property_row, contact.row)
            folio = _set_folio_for_conversation(client, conversation.row, message_context)
            channel_id = _resolve_whatsapp_channel_id(client)
            effective_language = template_resolution.template.language or message_context.language

            rendered_text = _render_template_text(template_resolution.template, payload, message_context)
            template_payload = _build_template_payload(
                payload=payload,
                instance_row=instance_row,
                property_row=property_row,
                contact=contact,
                conversation=conversation,
                folio=folio,
                template_resolution=template_resolution,
                channel_id=channel_id,
                message_context=message_context,
                rendered_text=rendered_text,
                language=effective_language,
            )
            message_row, template_payload = _persist_message_status(
                client,
                message_row=None,
                conversation_row=conversation.row,
                channel_id=channel_id,
                rendered_text=rendered_text,
                template_payload=template_payload,
                status="pending",
            )

            outbound_parameters = _build_outbound_parameters(template_resolution.template, message_context)
            credentials = _resolve_whatsapp_credentials(client, property_row)
            send_result = await _send_template_to_whatsapp(
                state,
                chat_id=contact.phone_code,
                template=template_resolution.template,
                outbound_parameters=outbound_parameters,
                credentials=credentials,
                language=effective_language,
            )
            provider_sent = True

            message_row, template_payload = _persist_message_status(
                client,
                message_row=message_row,
                template_payload=template_payload,
                status="sent",
                send_result=send_result,
            )

            await _emit_socket_event(
                state,
                property_row=property_row,
                contact=contact,
                conversation=conversation,
                message_row=message_row,
                channel_id=channel_id,
                template_resolution=template_resolution,
                template_payload=template_payload,
                rendered_text=rendered_text,
                language=effective_language,
            )

            _finalize_idempotency_key(state, message_context.idempotency_key, success=True)
            return {
                "status": "sent",
                "instance_id": instance_row["id"],
                "property_id": property_row["id"],
                "contact_id": contact.row["id"],
                "conversation_id": conversation.row["id"],
                "channel_id": channel_id,
                "message_id": message_row["id"],
                "template_id": template_resolution.template_id,
                "template": template_resolution.template.whatsapp_name or template_resolution.template.code,
                "language": effective_language,
                "template_payload": template_payload,
            }
        except Exception as exc:
            if not isinstance(exc, HTTPException):
                log.error("Error en send-template: %s", exc, exc_info=True)
            _handle_send_error(
                client,
                state,
                idempotency_key=message_context.idempotency_key,
                provider_sent=provider_sent,
                message_row=message_row,
                template_payload=template_payload,
                detail=exc.detail if isinstance(exc, HTTPException) and isinstance(exc.detail, str) else str(exc),
            )
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(status_code=500, detail="Error interno enviando plantilla")

    app.include_router(router)
