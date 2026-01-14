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

log = logging.getLogger("TemplateRoutes")


# ---------------------------------------------------------------------------
# Modelos de entrada
# ---------------------------------------------------------------------------
class SourceHotel(BaseModel):
    id: Optional[int] = Field(default=None, description="ID interno en Odoo/Roomdoo")
    external_code: str = Field(..., description="Código externo del hotel (ej: H_PORTONOVO)")
    name: Optional[str] = Field(default=None, description="Nombre descriptivo del hotel")


class Source(BaseModel):
    instance_url: str = Field(..., description="URL de la instancia en Roomdoo")
    db: Optional[str] = Field(default=None, description="Nombre de la base de datos")
    instance_id: Optional[str] = Field(default=None, description="Identificador lógico de la instancia")
    hotel: SourceHotel


class Recipient(BaseModel):
    phone: str = Field(..., description="Teléfono en formato E.164 (+34...)")
    country: Optional[str] = Field(default=None, description="Código de país ISO (opcional)")

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

            if payload.meta and payload.meta.property_id is not None:
                try:
                    state.memory_manager.set_flag(chat_id, "property_id", payload.meta.property_id)
                except Exception as exc:
                    log.warning("No se pudo guardar property_id en memoria: %s", exc)

            await state.channel_manager.send_template_message(
                chat_id,
                wa_template,
                parameters=parameters,
                language=language,
                channel="whatsapp",
            )

            # Registrar evento para contexto futuro
            try:
                rendered = template_def.render_content(payload.template.parameters) if template_def else None
                if rendered:
                    state.memory_manager.save(chat_id, role="assistant", content=rendered)
                meta_excerpt = f"trigger={payload.meta.trigger}" if payload.meta else ""
                source_tag = payload.source.instance_id or payload.source.instance_url
                state.memory_manager.save(
                    chat_id,
                    role="system",
                    content=(
                        f"[TEMPLATE_SENT] plantilla={wa_template} lang={language} hotel={hotel_code} "
                        f"origen={source_tag} {meta_excerpt}"
                    ).strip(),
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
