"""Tools para OnboardingAgent (reservas via MCP -> n8n)."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional, Tuple

from langchain.tools import StructuredTool
from pydantic import BaseModel, Field

from core.mcp_client import mcp_client

log = logging.getLogger("OnboardingTools")


def _find_tool(tools: list[Any], candidates: list[str]) -> Optional[Any]:
    """Localiza una tool MCP por coincidencia parcial de nombre."""
    for tool in tools or []:
        name = (tool.name or "").replace(" ", "_").lower()
        for candidate in candidates:
            if candidate in name:
                return tool
    return None


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


async def _get_mcp_tools(server_name: str = "OnboardingAgent") -> Tuple[list[Any], Optional[str]]:
    try:
        tools = await mcp_client.get_tools(server_name=server_name)
        return tools or [], None
    except Exception as exc:  # pragma: no cover - fallbacks de red
        log.error("No se pudieron obtener tools MCP (%s): %s", server_name, exc, exc_info=True)
        return [], f"❌ No se pudo acceder al servidor MCP ({server_name})."


async def _obtener_token(tools: list[Any]) -> Tuple[Optional[str], Optional[str]]:
    """Reutiliza la tool 'buscar_token' expuesta por MCP."""
    try:
        token_tool = _find_tool(tools, ["buscar_token"])
        if not token_tool:
            return None, "No se encontro la tool 'buscar_token' en MCP."

        token_raw = await token_tool.ainvoke({})
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


def create_room_type_tool():
    class RoomTypeInput(BaseModel):
        pms_property_id: int = Field(
            default=38,
            description="ID de propiedad PMS (ej. 38).",
        )
        room_type_name: Optional[str] = Field(
            default=None,
            description="Nombre a filtrar (opcional, ej. 'Individual').",
        )

    async def _room_type_lookup(
        pms_property_id: int = 38,
        room_type_name: Optional[str] = None,
    ) -> str:
        tools, err = await _get_mcp_tools()
        if err:
            return err

        token, token_err = await _obtener_token(tools)
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


def create_reservation_tool():
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
        pms_property_id: int = Field(default=38, description="Propiedad PMS (siempre 38)")
        pricelist_id: int = Field(default=3, description="Lista de precios (siempre 3)")

    async def _resolve_room_type_id(
        tools: list[Any],
        token: str,
        pms_property_id: int,
        room_type_id: Optional[int],
        room_type_name: Optional[str],
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
        pms_property_id: int = 38,
        pricelist_id: int = 3,
    ) -> str:
        tools, err = await _get_mcp_tools()
        if err:
            return err

        token, token_err = await _obtener_token(tools)
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
        )
        if not resolved_room_type_id:
            return rt_err or "No se pudo determinar el roomTypeId."

        reservation_payload = {
            "pricelistId": pricelist_id,
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
        if partner_requests:
            reservation_payload["reservations"][0]["partnerRequests"] = partner_requests.strip()

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
                    return raw

            return json.dumps(parsed, ensure_ascii=False)
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
