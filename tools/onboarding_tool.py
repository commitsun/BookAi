"""Tools para OnboardingAgent (reservas via MCP -> n8n)."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional, Tuple
import re

from langchain.tools import StructuredTool
from pydantic import BaseModel, Field

from core.mcp_client import get_tools
from core.db import upsert_chat_reservation

log = logging.getLogger("OnboardingTools")


def _find_tool(tools: list[Any], candidates: list[str]) -> Optional[Any]:
    """Localiza una tool MCP por coincidencia parcial de nombre."""
    for tool in tools or []:
        name = (tool.name or "").replace(" ", "_").lower()
        for candidate in candidates:
            if candidate in name:
                return tool
    return None


def _resolve_property_id(memory_manager, chat_id: str, fallback: int) -> int:
    if not memory_manager or not chat_id:
        return fallback
    try:
        raw = memory_manager.get_flag(chat_id, "property_id") or memory_manager.get_flag(chat_id, "pms_property_id")
        if raw is None:
            return fallback
        value = int(raw)
        return value if value > 0 else fallback
    except Exception:
        return fallback


def _safe_parse_json(raw: Any, context: str) -> Optional[Any]:
    """
    Intenta parsear JSON de forma tolerante:
    - Devuelve None si viene vacío o no parseable.
    - Loguea el contexto para facilitar debugging sin romper el flujo.
    """
    if raw is None:
        return None
    if isinstance(raw, str) and not raw.strip():
        log.info("Respuesta vacía en %s", context)
        return []
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception as exc:
        log.warning("No se pudo parsear respuesta en %s: %s", context, exc)
        return []


def _extract_folio_id(payload: Any) -> Optional[str]:
    """Intenta extraer un folio_id desde una respuesta de reserva."""
    if payload is None:
        return None

    def _as_digits(val: Any) -> Optional[str]:
        if isinstance(val, (int, float)) and int(val) == val:
            return str(int(val))
        if isinstance(val, str):
            m = re.search(r"\b(\d{4,})\b", val)
            return m.group(1) if m else None
        return None

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            match = re.search(r"\[(\d{4,})\]", payload)
            if match:
                return match.group(1)
            match = re.search(
                r"(folio[_\s-]*id|folio|reservation[_\s-]*id)\"?\s*[:=]\s*\"?(\d+)",
                payload,
                re.IGNORECASE,
            )
            return match.group(2) if match else None

    if isinstance(payload, list):
        for item in payload:
            found = _extract_folio_id(item)
            if found:
                return found
        return None

    if isinstance(payload, dict):
        # 1) Busca por claves conocidas
        for key in (
            "folio_id",
            "folioId",
            "folio",
            "reservation_id",
            "reservationId",
        ):
            if key in payload:
                val = payload.get(key)
                if isinstance(val, dict):
                    nested = _extract_folio_id(val)
                    if nested:
                        return nested
                digits = _as_digits(val)
                if digits:
                    return digits

        # 2) Recorrido profundo por valores
        for val in payload.values():
            if isinstance(val, (dict, list)):
                nested = _extract_folio_id(val)
                if nested:
                    return nested
            else:
                digits = _as_digits(val)
                if digits:
                    return digits
        return None

    return None

def _extract_reservation_locator(payload: Any) -> Optional[str]:
    if payload is None:
        return None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            match = re.search(
                r"(localizador|reservation[_\s-]*locator|name|code)\"?\s*[:=]\s*\"?([A-Za-z0-9/\\-]{4,})",
                payload,
                re.IGNORECASE,
            )
            return match.group(2) if match else None
    if isinstance(payload, list):
        for item in payload:
            found = _extract_reservation_locator(item)
            if found:
                return found
        return None
    if isinstance(payload, dict):
        for key in ("reservation_locator", "locator", "name", "code"):
            if key in payload:
                val = payload.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        if isinstance(payload.get("response"), list) and payload["response"]:
            item = payload["response"][0]
            if isinstance(item, dict):
                for key in ("name", "code", "reservation_locator", "locator"):
                    val = item.get(key)
                    if isinstance(val, str) and val.strip():
                        return val.strip()
        for val in payload.values():
            if isinstance(val, (dict, list)):
                nested = _extract_reservation_locator(val)
                if nested:
                    return nested
            elif isinstance(val, str) and val.strip():
                m = re.search(r"\b([A-Za-z0-9/\\-]{4,})\b", val)
                if m:
                    return m.group(1)
    return None


async def _get_mcp_tools(server_name: str = "OnboardingAgent") -> Tuple[list[Any], Optional[str]]:
    try:
        tools = await get_tools(server_name=server_name)
        return tools or [], None
    except Exception as exc:  # pragma: no cover - fallbacks de red
        log.error("No se pudieron obtener tools MCP (%s): %s", server_name, exc, exc_info=True)
        return [], f"❌ No se pudo acceder al servidor MCP ({server_name})."


async def _obtener_token(
    tools: list[Any],
    *,
    instance_url: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Reutiliza la tool 'buscar_token' expuesta por MCP."""
    try:
        token_tool = _find_tool(tools, ["buscar_token"])
        if not token_tool:
            return None, "No se encontro la tool 'buscar_token' en MCP."

        payload: dict[str, Any] = {}
        if instance_url:
            payload["instance_url"] = instance_url

        token_raw = await token_tool.ainvoke(payload)
        token_data = json.loads(token_raw) if isinstance(token_raw, str) else token_raw
        token = (
            token_data[0].get("key") if isinstance(token_data, list) else token_data.get("key")
        )

        if not token:
            return None, "No se pudo obtener el token de acceso."

        return str(token).strip(), None
    except Exception as exc:  # pragma: no cover - fallbacks de red
        log.error("Error obteniendo token desde MCP: %s", exc, exc_info=True)
        return None, f"Error obteniendo token desde MCP: {exc}"


def create_room_type_tool(memory_manager=None, chat_id: str = ""):
    class RoomTypeInput(BaseModel):
        property_id: Optional[int] = Field(
            default=None,
            description="ID de propiedad (property_id).",
        )
        pms_property_id: int = Field(
            default=38,
            description="ID de propiedad PMS (compatibilidad).",
        )
        room_type_name: Optional[str] = Field(
            default=None,
            description="Nombre a filtrar (opcional, ej. 'Individual').",
        )

    async def _room_type_lookup(
        property_id: Optional[int] = None,
        pms_property_id: int = 38,
        room_type_name: Optional[str] = None,
    ) -> str:
        tools, err = await _get_mcp_tools()
        if err:
            return err

        if property_id is not None:
            pms_property_id = property_id
        pms_property_id = _resolve_property_id(memory_manager, chat_id, pms_property_id)
        instance_url = None
        if memory_manager and chat_id:
            try:
                instance_url = memory_manager.get_flag(chat_id, "instance_url")
            except Exception:
                instance_url = None

        token, token_err = await _obtener_token(tools, instance_url=instance_url)
        if not token:
            return token_err or "No se pudo obtener el token de acceso."

        type_tool = _find_tool(tools, ["tipo_de_habitacion", "tipo"])
        if not type_tool:
            return "No se encontro la tool de 'tipo de habitacion' en MCP."

        payload = {
            "pmsPropertyIds[0]": pms_property_id,
            "pmsPropertyId": pms_property_id,
            "key": token,
        }
        if instance_url:
            payload["instance_url"] = instance_url
            payload["property_id"] = pms_property_id

        try:
            raw = await type_tool.ainvoke(payload)
            parsed = _safe_parse_json(raw, "tipos de habitacion")
            if parsed is None:
                return "⚠️ No pude leer la lista de tipos de habitación (respuesta vacía o inválida)."
        except Exception as exc:  # pragma: no cover - fallbacks de red
            log.error("Error consultando tipos de habitacion: %s", exc, exc_info=True)
            return f"❌ Error consultando tipos de habitacion: {exc}"

        items = parsed if isinstance(parsed, list) else []
        if room_type_name:
            target = room_type_name.strip().lower()
            matched = [
                item for item in items if target in str(item.get("name", "")).strip().lower()
            ]
            if matched:
                return json.dumps(matched, ensure_ascii=False)
            if not items:
                return (
                    f"⚠️ No pude obtener la lista de tipos de habitación ahora mismo. "
                    f"Intento de nuevo o lo consulto con el encargado."
                )
            return (
                f"⚠️ No encontré coincidencias para '{room_type_name}'. "
                f"Tipos disponibles: {json.dumps(items, ensure_ascii=False)}"
            )

        return json.dumps(items, ensure_ascii=False)

    return StructuredTool.from_function(
        name="listar_tipos_habitacion",
        description=(
            "Obtiene los tipos de habitacion disponibles (roomTypeId) para una propiedad. "
            "Usa token automatico via MCP."
        ),
        coroutine=_room_type_lookup,
        args_schema=RoomTypeInput,
    )


def create_reservation_tool(memory_manager=None, chat_id: str = ""):
    class ReservationInput(BaseModel):
        checkin: str = Field(..., description="Fecha check-in YYYY-MM-DD")
        checkout: str = Field(..., description="Fecha check-out YYYY-MM-DD")
        adults: int = Field(..., description="Numero de adultos")
        children: int = Field(default=0, description="Numero de ninos")
        room_type_id: Optional[int] = Field(default=None, description="roomTypeId si ya se conoce")
        room_type_name: Optional[str] = Field(
            default=None,
            description="Nombre de la habitacion para resolver roomTypeId si no se pasa el id.",
        )
        partner_name: str = Field(..., description="Nombre del huesped")
        partner_email: str = Field(..., description="Email del huesped")
        partner_phone: str = Field(..., description="Telefono del huesped (con prefijo)")
        partner_requests: Optional[str] = Field(default=None, description="Peticiones especiales")
        property_id: Optional[int] = Field(default=None, description="Propiedad (property_id)")
        pms_property_id: int = Field(default=38, description="Propiedad PMS (compatibilidad)")
        pricelist_id: int = Field(default=3, description="Lista de precios (siempre 3)")

    async def _resolve_room_type_id(
        tools: list[Any],
        token: str,
        pms_property_id: int,
        room_type_id: Optional[int],
        room_type_name: Optional[str],
        instance_url: Optional[str] = None,
    ) -> Tuple[Optional[int], Optional[str]]:
        if room_type_id:
            return room_type_id, None
        if not room_type_name:
            return None, "Falta el roomTypeId o el nombre de habitacion."

        type_tool = _find_tool(tools, ["tipo_de_habitacion", "tipo"])
        if not type_tool:
            return None, "No se encontro la tool de 'tipo de habitacion' en MCP."

        payload = {
            "pmsPropertyIds[0]": pms_property_id,
            "pmsPropertyId": pms_property_id,
            "key": token,
        }
        if instance_url:
            payload["instance_url"] = instance_url
            payload["property_id"] = pms_property_id
        raw = await type_tool.ainvoke(payload)
        parsed = _safe_parse_json(raw, "resolver roomTypeId")
        items = parsed if isinstance(parsed, list) else []

        if not items:
            return None, "No pude obtener la lista de tipos de habitación en este momento."

        target = room_type_name.strip().lower()
        for item in items:
            name = str(item.get("name", "")).strip().lower()
            if target in name:
                rid = item.get("id") or item.get("roomTypeId")
                if rid is not None:
                    return int(rid), None

        return None, f"No se encontro un roomTypeId para '{room_type_name}'."

    async def _create_reservation(
        checkin: str,
        checkout: str,
        adults: int,
        children: int = 0,
        room_type_id: Optional[int] = None,
        room_type_name: Optional[str] = None,
        partner_name: str = "",
        partner_email: str = "",
        partner_phone: str = "",
        partner_requests: Optional[str] = None,
        property_id: Optional[int] = None,
        pms_property_id: int = 38,
        pricelist_id: int = 3,
    ) -> str:
        tools, err = await _get_mcp_tools()
        if err:
            return err

        if property_id is not None:
            pms_property_id = property_id
        pms_property_id = _resolve_property_id(memory_manager, chat_id, pms_property_id)
        instance_url = None
        if memory_manager and chat_id:
            try:
                instance_url = memory_manager.get_flag(chat_id, "instance_url")
            except Exception:
                instance_url = None

        token, token_err = await _obtener_token(tools, instance_url=instance_url)
        if not token:
            return token_err or "No se pudo obtener el token de acceso."

        reserva_tool = _find_tool(tools, ["reserva"])
        if not reserva_tool:
            return "No se encontro la tool 'reserva' en MCP."

        resolved_room_type_id, rt_err = await _resolve_room_type_id(
            tools,
            token,
            pms_property_id,
            room_type_id,
            room_type_name,
            instance_url=instance_url,
        )
        if not resolved_room_type_id:
            return rt_err or "No se pudo determinar el roomTypeId."

        reservation_payload = {
            "pricelistId": pricelist_id,
            "property_id": pms_property_id,
            "pmsPropertyId": pms_property_id,
            "reservations": [
                {
                    "checkin": checkin.strip(),
                    "checkout": checkout.strip(),
                    "roomTypeId": resolved_room_type_id,
                    "children": max(children or 0, 0),
                    "adults": max(adults or 0, 0),
                }
            ],
            "partnerName": partner_name.strip(),
            "partnerEmail": partner_email.strip(),
            "partnerPhone": partner_phone.strip(),
        }
        if instance_url:
            reservation_payload["instance_url"] = instance_url
        if partner_requests:
            reservation_payload["reservations"][0]["partnerRequests"] = partner_requests.strip()

        # 🚧 Prevención de duplicados: si ya se creó una reserva con el mismo payload hace segundos, reutiliza respuesta.
        fingerprint = json.dumps(
            {
                "checkin": reservation_payload["reservations"][0]["checkin"],
                "checkout": reservation_payload["reservations"][0]["checkout"],
                "adults": reservation_payload["reservations"][0]["adults"],
                "children": reservation_payload["reservations"][0]["children"],
                "room_type_id": reservation_payload["reservations"][0]["roomTypeId"],
                "property_id": pms_property_id,
                "instance_url": instance_url or "",
                "partner_name": reservation_payload["partnerName"],
                "partner_email": reservation_payload["partnerEmail"],
                "partner_phone": reservation_payload["partnerPhone"],
                "partner_requests": reservation_payload["reservations"][0].get("partnerRequests", ""),
            },
            sort_keys=True,
            ensure_ascii=False,
        )

        if memory_manager and chat_id:
            try:
                last = memory_manager.get_flag(chat_id, "onboarding_last_reservation")
                if last:
                    last_fp = last.get("fingerprint")
                    ts = last.get("timestamp")
                    last_response = last.get("response")
                    if last_fp == fingerprint and ts:
                        from datetime import datetime, timedelta

                        # Ventana corta para evitar dos reservas iguales en segundos/minutos
                        ts_dt = datetime.fromisoformat(ts) if isinstance(ts, str) else None
                        if ts_dt and datetime.utcnow() - ts_dt < timedelta(minutes=3):
                            log.info("🛑 Reserva duplicada detectada para %s, devolviendo respuesta previa", chat_id)
                            return last_response or (
                                "⚠️ Ya generé una reserva con estos mismos datos hace un momento. "
                                "Si necesitas modificarla o cancelarla, indícalo."
                            )
            except Exception as exc:
                log.warning("No se pudo revisar duplicados de reserva: %s", exc)

        try:
            payload = {
                **reservation_payload,
                "key": token,
            }
            raw = await reserva_tool.ainvoke(payload)
            if raw is None:
                log.error("Reserva devolvio respuesta vacia")
                return "❌ No se obtuvo respuesta del PMS."

            parsed = raw
            if isinstance(raw, str):
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("Respuesta de reserva no es JSON, devolviendo texto.")
                    response_text = raw
                else:
                    response_text = json.dumps(parsed, ensure_ascii=False)
            else:
                response_text = json.dumps(parsed, ensure_ascii=False)

            if memory_manager and chat_id and "❌" not in response_text:
                try:
                    from datetime import datetime

                    folio_id = _extract_folio_id(parsed)
                    if not folio_id and isinstance(response_text, str):
                        m = re.search(r"^\[(\d{4,})\]", response_text)
                        if m:
                            folio_id = m.group(1)
                    reservation_locator = _extract_reservation_locator(parsed)
                    if folio_id and not re.fullmatch(r"(?=.*\d)[A-Za-z0-9]{4,}", str(folio_id)):
                        log.warning("Folio_id inválido en onboarding, se ignora: %s", folio_id)
                        folio_id = None
                    # Si el reservation_locator coincide con el folio_id, no lo consideramos válido.
                    if reservation_locator and folio_id and str(reservation_locator).strip() == str(folio_id).strip():
                        reservation_locator = None
                    # Si el reservation_locator es solo dígitos (típico folio), intenta enriquecer luego vía PMS.
                    locator_needs_pms = False
                    if reservation_locator:
                        locator_needs_pms = re.fullmatch(r"\d{4,}", str(reservation_locator)) is not None
                    targets = [chat_id]
                    if isinstance(chat_id, str) and ":" in chat_id:
                        tail = chat_id.split(":")[-1].strip()
                        if tail:
                            targets.append(tail)
                    if folio_id:
                        for target in targets:
                            memory_manager.set_flag(target, "folio_id", str(folio_id))
                    if reservation_locator:
                        for target in targets:
                            memory_manager.set_flag(target, "reservation_locator", reservation_locator)
                    for target in targets:
                        memory_manager.set_flag(target, "checkin", reservation_payload["reservations"][0]["checkin"])
                        memory_manager.set_flag(target, "checkout", reservation_payload["reservations"][0]["checkout"])
                    if folio_id:
                        log.info(
                            "🧾 onboarding upsert_chat_reservation chat_id=%s folio_id=%s checkin=%s checkout=%s property_id=%s",
                            chat_id,
                            folio_id,
                            reservation_payload["reservations"][0]["checkin"],
                            reservation_payload["reservations"][0]["checkout"],
                            pms_property_id,
                        )
                        upsert_chat_reservation(
                            chat_id=chat_id,
                            folio_id=str(folio_id),
                            checkin=reservation_payload["reservations"][0]["checkin"],
                            checkout=reservation_payload["reservations"][0]["checkout"],
                            property_id=pms_property_id,
                            hotel_code=memory_manager.get_flag(chat_id, "property_name") if memory_manager else None,
                            original_chat_id=chat_id if isinstance(chat_id, str) and ":" in chat_id else None,
                            reservation_locator=reservation_locator,
                            source="onboarding",
                        )
                    # Enriquecer reservation_locator desde PMS si no vino o parece folio interno
                    if folio_id and (not reservation_locator or locator_needs_pms):
                        try:
                            from tools.superintendente_tool import create_consulta_reserva_persona_tool

                            consulta_tool = create_consulta_reserva_persona_tool(
                                memory_manager=memory_manager,
                                chat_id=chat_id,
                            )
                            log.info(
                                "🧾 onboarding consulta_reserva_persona folio_id=%s property_id=%s",
                                folio_id,
                                pms_property_id,
                            )
                            raw = await consulta_tool.ainvoke(
                                {"folio_id": str(folio_id), "property_id": pms_property_id}
                            )
                            parsed = None
                            if isinstance(raw, str):
                                try:
                                    parsed = json.loads(raw)
                                except Exception:
                                    parsed = None
                            elif isinstance(raw, dict):
                                parsed = raw
                            if parsed:
                                reservation_locator = _extract_reservation_locator(parsed)
                                if reservation_locator:
                                    for target in targets:
                                        memory_manager.set_flag(target, "reservation_locator", reservation_locator)
                                    upsert_chat_reservation(
                                        chat_id=chat_id,
                                        folio_id=str(folio_id),
                                        checkin=reservation_payload["reservations"][0]["checkin"],
                                        checkout=reservation_payload["reservations"][0]["checkout"],
                                        property_id=pms_property_id,
                                        hotel_code=memory_manager.get_flag(chat_id, "property_name") if memory_manager else None,
                                        original_chat_id=chat_id if isinstance(chat_id, str) and ":" in chat_id else None,
                                        reservation_locator=reservation_locator,
                                        source="pms",
                                    )
                                    if isinstance(response_text, str):
                                        # Quita prefijo [folio] si viene del PMS
                                        response_text = re.sub(r"^\[\d{4,}\]\s*", "", response_text).strip()
                                        # Sustituye localizador numérico por el locator público
                                        response_text = re.sub(
                                            r"(Localizador:\s*)(\d{4,})",
                                            rf"\\1{reservation_locator}",
                                            response_text,
                                            flags=re.IGNORECASE,
                                        )
                                        if "Localizador:" not in response_text:
                                            response_text = f"{response_text}\nLocalizador: {reservation_locator}"
                        except Exception as exc:
                            log.warning("No se pudo enriquecer reservation_locator en onboarding: %s", exc)

                    # Asegura que el output exponga el reservation_locator si existe.
                    if reservation_locator and isinstance(response_text, str):
                        response_text = re.sub(r"^\[\d{4,}\]\s*", "", response_text).strip()
                        response_text = re.sub(
                            r"(Localizador:\s*)(\d{4,})",
                            rf"\\1{reservation_locator}",
                            response_text,
                            flags=re.IGNORECASE,
                        )
                        if "Localizador:" not in response_text:
                            response_text = f"{response_text}\nLocalizador: {reservation_locator}"
                    memory_manager.set_flag(
                        chat_id,
                        "onboarding_last_reservation",
                        {
                            "fingerprint": fingerprint,
                            "timestamp": datetime.utcnow().isoformat(),
                            "response": response_text,
                            "meta": {
                                "checkin": reservation_payload["reservations"][0]["checkin"],
                                "checkout": reservation_payload["reservations"][0]["checkout"],
                                "partner_name": reservation_payload["partnerName"],
                                "partner_email": reservation_payload["partnerEmail"],
                                "partner_phone": reservation_payload["partnerPhone"],
                                "folio_id": folio_id,
                            },
                        },
                    )
                except Exception as exc:
                    log.warning("No se pudo guardar flag de reserva para %s: %s", chat_id, exc)

            return response_text
        except Exception as exc:  # pragma: no cover - fallbacks de red
            log.error("Error creando reserva: %s", exc, exc_info=True)
            return f"❌ Error creando la reserva: {exc}"

    return StructuredTool.from_function(
        name="crear_reserva_onboarding",
        description=(
            "Crea una reserva en el PMS. Usa token y roomTypeId automaticamente (busca por nombre si no se pasa el id). "
            "Requiere checkin, checkout, adultos y datos del huesped."
        ),
        coroutine=_create_reservation,
        args_schema=ReservationInput,
    )


def create_token_tool():
    async def _get_token() -> str:
        tools, err = await _get_mcp_tools()
        if err:
            return err
        token, token_err = await _obtener_token(tools)
        return token or token_err or "No se pudo obtener el token."

    return StructuredTool.from_function(
        name="obtener_token_reservas",
        description="Devuelve el token actual consultando la tool buscar_token via MCP.",
        coroutine=_get_token,
    )


def create_consulta_reserva_propia_tool(memory_manager=None, chat_id: str = ""):
    from tools.superintendente_tool import (
        create_consulta_reserva_general_tool,
        create_consulta_reserva_persona_tool,
    )

    class ConsultaReservaPropiaInput(BaseModel):
        folio_id: Optional[str] = Field(
            default=None,
            description="Folio_id si el huésped lo tiene (opcional).",
        )
        reservation_locator: Optional[str] = Field(
            default=None,
            description="Localizador público de la reserva (opcional).",
        )
        fecha_inicio: Optional[str] = Field(
            default=None,
            description="Fecha inicio (YYYY-MM-DD) si no hay folio_id.",
        )
        fecha_fin: Optional[str] = Field(
            default=None,
            description="Fecha fin (YYYY-MM-DD) si no hay folio_id.",
        )
        partner_name: Optional[str] = Field(
            default=None,
            description="Nombre del huésped para filtrar.",
        )
        partner_email: Optional[str] = Field(
            default=None,
            description="Email del huésped para filtrar.",
        )
        partner_phone: Optional[str] = Field(
            default=None,
            description="Teléfono del huésped para filtrar.",
        )
        property_id: Optional[int] = Field(default=None, description="Propiedad (property_id)")
        pms_property_id: int = Field(default=38, description="Propiedad PMS (compatibilidad)")

    def _normalize_phone(val: Optional[str]) -> str:
        return re.sub(r"\D+", "", val or "")

    async def _consulta_reserva_propia(
        folio_id: Optional[str] = None,
        reservation_locator: Optional[str] = None,
        fecha_inicio: Optional[str] = None,
        fecha_fin: Optional[str] = None,
        partner_name: Optional[str] = None,
        partner_email: Optional[str] = None,
        partner_phone: Optional[str] = None,
        property_id: Optional[int] = None,
        pms_property_id: int = 38,
    ) -> str:
        if property_id is not None:
            pms_property_id = property_id
        pms_property_id = _resolve_property_id(memory_manager, chat_id, pms_property_id)

        # Primero: intenta multireserva en chat_reservations (si existe tool MCP)
        try:
            multireserva_tool = create_multireserva_tool(memory_manager=memory_manager, chat_id=chat_id)
            raw_multi = await multireserva_tool.ainvoke(
                {
                    "chat_id": chat_id,
                    "property_id": pms_property_id,
                }
            )
            parsed_multi = _safe_parse_json(raw_multi, "multireserva")
            items = None
            if isinstance(parsed_multi, dict):
                items = parsed_multi.get("response") or parsed_multi.get("items")
            elif isinstance(parsed_multi, list):
                items = parsed_multi
            if isinstance(items, list) and items:
                # Si recibimos un localizador, mapear a folio_id
                locator_candidate = (reservation_locator or folio_id or "").strip()
                if locator_candidate:
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        if str(item.get("reservation_locator") or "").strip() == locator_candidate:
                            mapped_folio = item.get("folio_id")
                            if mapped_folio:
                                tool = create_consulta_reserva_persona_tool(
                                    memory_manager=memory_manager, chat_id=chat_id
                                )
                                return await tool.ainvoke(
                                    {"folio_id": str(mapped_folio), "property_id": pms_property_id}
                                )
                simplified = []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    simplified.append(
                        {
                            "reservation_locator": item.get("reservation_locator"),
                            "folio_id": item.get("folio_id"),
                            "checkin": item.get("checkin"),
                            "checkout": item.get("checkout"),
                            "hotel_code": item.get("hotel_code"),
                        }
                    )
                if simplified:
                    return json.dumps(simplified, ensure_ascii=False)
        except Exception:
            pass

        last_flag = None
        if memory_manager and chat_id:
            try:
                last_flag = memory_manager.get_flag(chat_id, "onboarding_last_reservation")
            except Exception as exc:
                log.warning("No se pudo leer flag de reserva para %s: %s", chat_id, exc)

        meta = (last_flag or {}).get("meta") or {}

        resolved_folio = folio_id or meta.get("folio_id")
        if not resolved_folio and last_flag:
            resolved_folio = _extract_folio_id(last_flag.get("response"))

        # Si lo que llega parece un localizador (contiene /), intentamos mapearlo a folio_id
        if resolved_folio and "/" in str(resolved_folio):
            resolved_folio = None

        if resolved_folio:
            tool = create_consulta_reserva_persona_tool(memory_manager=memory_manager, chat_id=chat_id)
            return await tool.ainvoke(
                {"folio_id": str(resolved_folio), "property_id": pms_property_id}
            )

        fecha_inicio = fecha_inicio or meta.get("checkin")
        fecha_fin = fecha_fin or meta.get("checkout")
        if not fecha_inicio or not fecha_fin:
            return (
                "Para consultar tu reserva necesito el folio_id o las fechas de entrada y salida."
            )

        partner_name = (partner_name or meta.get("partner_name") or "").strip()
        partner_email = (partner_email or meta.get("partner_email") or "").strip()
        partner_phone = (partner_phone or meta.get("partner_phone") or "").strip()

        if not any([partner_name, partner_email, partner_phone]):
            return (
                "Para filtrar tu reserva necesito tu nombre, email o teléfono."
            )

        tool = create_consulta_reserva_general_tool(memory_manager=memory_manager, chat_id=chat_id)
        raw = await tool.ainvoke(
            {
                "fecha_inicio": fecha_inicio,
                "fecha_fin": fecha_fin,
                "property_id": pms_property_id,
            }
        )

        if isinstance(raw, str) and raw.strip().startswith("❌"):
            return raw

        parsed = _safe_parse_json(raw, "consulta_reserva_propia")
        if not isinstance(parsed, list):
            return json.dumps(parsed, ensure_ascii=False) if parsed else "No se encontraron reservas."

        filtered = []
        phone_norm = _normalize_phone(partner_phone)
        for item in parsed:
            item_name = str(item.get("partner_name") or "").strip().lower()
            item_email = str(item.get("partner_email") or "").strip().lower()
            item_phone = _normalize_phone(item.get("partner_phone"))

            if partner_name and partner_name.lower() not in item_name:
                continue
            if partner_email and partner_email.lower() not in item_email:
                continue
            if phone_norm and phone_norm not in item_phone:
                continue
            filtered.append(item)

        if not filtered:
            return "No encontré reservas activas con esos datos."

        return json.dumps(filtered, ensure_ascii=False)

    return StructuredTool.from_function(
        name="consultar_reserva_propia",
        description=(
            "Consulta las reservas del propio huésped. Primero intenta multireserva por chat_id. "
            "Si no hay datos, usa folio_id si lo hay; si no, necesita fechas y datos del huésped para filtrar."
        ),
        coroutine=_consulta_reserva_propia,
        args_schema=ConsultaReservaPropiaInput,
    )


def create_multireserva_tool(memory_manager=None, chat_id: str = ""):
    class MultiReservaInput(BaseModel):
        chat_id: Optional[str] = Field(default=None, description="Chat ID del huésped.")
        property_id: Optional[int] = Field(default=None, description="Propiedad (property_id).")
        hotel_code: Optional[str] = Field(default=None, description="Código de hotel (opcional).")

    def _normalize_chat(val: Optional[str]) -> Optional[str]:
        if not val:
            return None
        if isinstance(val, str) and ":" in val:
            val = val.split(":")[-1]
        return re.sub(r"\D+", "", val) or val

    async def _multireserva(
        chat_id: Optional[str] = None,
        property_id: Optional[int] = None,
        hotel_code: Optional[str] = None,
    ) -> str:
        tools, err = await _get_mcp_tools()
        if err:
            return err

        tool = _find_tool(tools, ["multireserva"])
        if not tool:
            return "No se encontró la tool 'multireserva' en MCP."

        resolved_chat = _normalize_chat(chat_id)
        if not resolved_chat and memory_manager and chat_id:
            try:
                resolved_chat = _normalize_chat(memory_manager.get_flag(chat_id, "last_memory_id"))
            except Exception:
                resolved_chat = None

        if not resolved_chat:
            return "Falta chat_id para consultar reservas."

        payload = {"chat_id": resolved_chat}
        raw = await tool.ainvoke(payload)
        parsed = _safe_parse_json(raw, "multireserva")
        items = None
        if isinstance(parsed, dict):
            items = parsed.get("response") or parsed.get("items")
        elif isinstance(parsed, list):
            items = parsed

        if not isinstance(items, list):
            return json.dumps(parsed, ensure_ascii=False) if parsed else "No se encontraron reservas."

        resolved_property = property_id
        if resolved_property is None and memory_manager and chat_id:
            try:
                resolved_property = memory_manager.get_flag(chat_id, "property_id")
            except Exception:
                resolved_property = None
        resolved_hotel = hotel_code
        if not resolved_hotel and memory_manager and chat_id:
            try:
                resolved_hotel = memory_manager.get_flag(chat_id, "property_name")
            except Exception:
                resolved_hotel = None

        filtered = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if resolved_property is not None and item.get("property_id") != resolved_property:
                continue
            if resolved_hotel and str(item.get("hotel_code") or "").upper() != str(resolved_hotel).upper():
                continue
            filtered.append(item)

        return json.dumps(filtered or items, ensure_ascii=False)

    return StructuredTool.from_function(
        name="multireserva",
        description=(
            "Obtiene todas las reservas asociadas a un chat_id desde chat_reservations. "
            "Filtra por property_id/hotel_code si se indica."
        ),
        coroutine=_multireserva,
        args_schema=MultiReservaInput,
    )
