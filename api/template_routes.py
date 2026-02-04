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


class MetaInfo(BaseModel):
    trigger: Optional[str] = None
    reservation_id: Optional[int] = None
    folio_id: Optional[int] = None
    property_id: Optional[int] = None
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
                    state.memory_manager.set_flag(chat_id, "instance_hotel_code", hotel_code)
                except Exception as exc:
                    log.warning("No se pudo guardar hotel_code en memoria: %s", exc)

            if payload.source.origin_folio:
                try:
                    folio = payload.source.origin_folio
                    if folio.id is not None:
                        state.memory_manager.set_flag(chat_id, "origin_folio_id", folio.id)
                    if folio.code:
                        state.memory_manager.set_flag(chat_id, "origin_folio_code", folio.code)
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
                except Exception as exc:
                    log.warning("No se pudo guardar origin_folio en memoria: %s", exc)

            context_id = _resolve_whatsapp_context_id(state, chat_id)
            ensure_instance_credentials(state.memory_manager, context_id or chat_id)

            await state.channel_manager.send_template_message(
                chat_id,
                wa_template,
                parameters=parameters,
                language=language,
                channel="whatsapp",
                context_id=context_id,
            )

            # Registrar evento para contexto futuro
            try:
                rendered = template_def.render_content(payload.template.parameters) if template_def else None
                if not rendered and template_def:
                    rendered = template_def.render_fallback_summary(payload.template.parameters)
                if not rendered and payload.template.parameters:
                    rendered = "Parametros de plantilla:\n" + "\n".join(
                        f"{k}: {v}" for k, v in payload.template.parameters.items()
                        if v is not None and str(v).strip() != ""
                    )
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
                source_tag = hotel_code or payload.source.instance_url
                state.memory_manager.save(
                    chat_id,
                    role="system",
                    content=(
                        f"[TEMPLATE_SENT] plantilla={wa_template} lang={language} hotel={hotel_code} "
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
                "language": language,
            }
        except HTTPException:
            raise
        except Exception as exc:
            log.error("Error en send-template: %s", exc, exc_info=True)
            raise HTTPException(status_code=500, detail=str(exc))

    app.include_router(router)
