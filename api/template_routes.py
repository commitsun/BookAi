"""Rutas REST para recibir envíos de plantillas desde Roomdoo/Odoo."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from core.config import Settings
from core.template_registry import TemplateRegistry
from core.instance_context import ensure_instance_credentials
from tools.superintendente_tool import create_consulta_reserva_persona_tool
from core.db import upsert_chat_reservation

log = logging.getLogger("TemplateRoutes")


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


def _normalize_phone(phone: str) -> str:
    """Solo dígitos, para Meta Cloud API."""
    digits = re.sub(r"\D", "", phone or "")
    return digits


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


def _extract_hotel_code(params: Dict[str, Any]) -> Optional[str]:
    if not params:
        return None
    for key in ("hotel_code", "hotel", "hotel_name", "property_name", "property"):
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


def _resolve_whatsapp_context_id(state, chat_id: str) -> Optional[str]:
    """Resuelve context_id (instancia:telefono) desde flags/memoria."""
    memory_manager = getattr(state, "memory_manager", None)
    if not memory_manager or not chat_id:
        return None

    clean = _normalize_phone(chat_id) or str(chat_id).strip()
    if clean:
        last_mem = memory_manager.get_flag(clean, "last_memory_id")
        if isinstance(last_mem, str) and last_mem.strip():
            return last_mem.strip()

    suffix = f":{clean}" if clean else ""
    if not suffix:
        return None

    for store_name in ("state_flags", "runtime_memory"):
        store = getattr(memory_manager, store_name, None)
        if isinstance(store, dict):
            for key in list(store.keys()):
                if isinstance(key, str) and key.endswith(suffix):
                    memory_manager.set_flag(clean, "last_memory_id", key.strip())
                    return key.strip()

    return None


# ---------------------------------------------------------------------------
# Registro de rutas
# ---------------------------------------------------------------------------
def register_template_routes(app, state) -> None:
    router = APIRouter(prefix="/api/v1/whatsapp", tags=["whatsapp-templates"])
    registry: TemplateRegistry = getattr(state, "template_registry", None)

    @router.post("/send-template")
    async def send_template(payload: SendTemplateRequest, _: None = Depends(_verify_bearer)):
        try:
            hotel_code = payload.source.hotel.external_code
            instance_id = (
                (payload.meta.instance_id if payload.meta else None)
                or payload.source.instance_id
                or payload.source.instance_url
            )
            language = (payload.template.language or "es").lower()
            template_code = payload.template.code
            idempotency_key = (payload.meta.idempotency_key if payload.meta else "") or ""

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
                hotel_code=hotel_code,
                template_code=template_code,
                language=language,
            ) if registry else None

            wa_template = template_def.whatsapp_name if template_def else template_code
            if template_def:
                parameters = template_def.build_meta_parameters(payload.template.parameters)
                language = template_def.language or language
            else:
                parameters = list((payload.template.parameters or {}).values())
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

            if payload.meta and payload.meta.property_id is not None:
                try:
                    state.memory_manager.set_flag(chat_id, "property_id", payload.meta.property_id)
                except Exception as exc:
                    log.warning("No se pudo guardar property_id en memoria: %s", exc)

            if hotel_code:
                try:
                    state.memory_manager.set_flag(chat_id, "property_name", hotel_code)
                    # property_name es nombre/label; instance_id se guarda aparte
                except Exception as exc:
                    log.warning("No se pudo guardar hotel_code en memoria: %s", exc)
            elif payload.template and payload.template.parameters:
                inferred_hotel = _extract_hotel_code(payload.template.parameters)
                if inferred_hotel:
                    hotel_code = inferred_hotel
                    try:
                        state.memory_manager.set_flag(chat_id, "property_name", hotel_code)
                    except Exception:
                        pass

            folio_id = None
            reservation_locator = None
            checkin = None
            checkout = None
            try:
                if payload.meta and payload.meta.folio_id is not None:
                    folio_id = str(payload.meta.folio_id)
                if payload.template and payload.template.parameters:
                    f_id, ci, co = _extract_reservation_fields(payload.template.parameters)
                    folio_id = folio_id or f_id
                    checkin = checkin or ci
                    checkout = checkout or co
                    reservation_locator = reservation_locator or _extract_reservation_locator(payload.template.parameters)
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
                    if folio.min_checkin:
                        checkin = checkin or folio.min_checkin
                    if folio.max_checkout:
                        checkout = checkout or folio.max_checkout
                except Exception as exc:
                    log.warning("No se pudo guardar origin_folio en memoria: %s", exc)

            context_id = _resolve_whatsapp_context_id(state, chat_id)
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

            if folio_id:
                try:
                    log.info(
                        "🧾 template upsert_chat_reservation chat_id=%s folio_id=%s checkin=%s checkout=%s property_id=%s instance_id=%s",
                        chat_id,
                        folio_id,
                        checkin,
                        checkout,
                        payload.meta.property_id if payload.meta else None,
                        instance_id,
                    )
                    upsert_chat_reservation(
                        chat_id=chat_id,
                        folio_id=folio_id,
                        checkin=checkin,
                        checkout=checkout,
                        property_id=payload.meta.property_id if payload.meta else None,
                        instance_id=instance_id,
                        original_chat_id=context_id or None,
                        reservation_locator=reservation_locator,
                        source="template",
                    )
                except Exception as exc:
                    log.warning("No se pudo persistir reserva en tabla: %s", exc)

            ensure_instance_credentials(state.memory_manager, context_id or chat_id)

            await state.channel_manager.send_template_message(
                chat_id,
                wa_template,
                parameters=parameters,
                language=language,
                channel="whatsapp",
                context_id=context_id,
            )

            # Si falta checkin/checkout y hay folio_id, intenta enriquecer desde PMS.
            if folio_id and (not checkin or not checkout):
                try:
                    consulta_tool = create_consulta_reserva_persona_tool(
                        memory_manager=state.memory_manager,
                        chat_id=chat_id,
                    )
                    raw = await consulta_tool.ainvoke(
                        {
                            "folio_id": folio_id,
                            "property_id": payload.meta.property_id if payload.meta else None,
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
                        if ci:
                            state.memory_manager.set_flag(chat_id, "checkin", ci)
                        if co:
                            state.memory_manager.set_flag(chat_id, "checkout", co)
                        if locator:
                            state.memory_manager.set_flag(chat_id, "reservation_locator", locator)
                        if folio_id:
                            upsert_chat_reservation(
                                chat_id=chat_id,
                                folio_id=folio_id,
                                checkin=ci or checkin,
                                checkout=co or checkout,
                                property_id=payload.meta.property_id if payload.meta else None,
                                instance_id=instance_id,
                                original_chat_id=context_id or None,
                                reservation_locator=locator,
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
                if reservation_locator and folio_id:
                    try:
                        upsert_chat_reservation(
                            chat_id=chat_id,
                            folio_id=folio_id,
                            checkin=checkin,
                            checkout=checkout,
                            property_id=payload.meta.property_id if payload.meta else None,
                            instance_id=instance_id,
                            original_chat_id=context_id or None,
                            reservation_locator=reservation_locator,
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
                if rendered:
                    state.memory_manager.set_flag(chat_id, "default_channel", "whatsapp")
                    state.memory_manager.save(
                        chat_id,
                        role="bookai",
                        content=rendered,
                        channel="whatsapp",
                        original_chat_id=context_id or None,
                    )
                meta_excerpt = f"trigger={payload.meta.trigger}" if payload.meta else ""
                source_tag = instance_id or payload.source.instance_url or hotel_code
                state.memory_manager.save(
                    chat_id,
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

            return {
                "status": "sent",
                "template": wa_template,
                "chat_id": chat_id,
                "hotel_code": hotel_code,
                "instance_id": instance_id,
                "language": language,
            }
        except HTTPException:
            raise
        except Exception as exc:
            log.error("Error en send-template: %s", exc, exc_info=True)
            raise HTTPException(status_code=500, detail=str(exc))

    app.include_router(router)
