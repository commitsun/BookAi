"""Tools para OnboardingAgent (reservas via MCP -> n8n)."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional, Tuple
import re

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


def _extract_folio_id(payload: Any) -> Optional[str]:
    """Intenta extraer un folio_id desde una respuesta de reserva."""
    if payload is None:
        return None

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            match = re.search(r"folio[_\s-]*id\"?\s*[:=]\s*\"?(\d+)", payload, re.IGNORECASE)
            return match.group(1) if match else None

    if isinstance(payload, list):
        for item in payload:
            found = _extract_folio_id(item)
            if found:
                return found
        return None

    if isinstance(payload, dict):
        for key in ("folio_id", "folioId", "folio"):
            if key in payload:
                val = payload.get(key)
                if isinstance(val, dict):
                    nested = _extract_folio_id(val)
                    if nested:
                        return nested
                if isinstance(val, (int, str)) and str(val).isdigit():
                    return str(val)
        return None

    return None


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

        # 🚧 Prevención de duplicados: si ya se creó una reserva con el mismo payload hace segundos, reutiliza respuesta.
        fingerprint = json.dumps(
            {
                "checkin": reservation_payload["reservations"][0]["checkin"],
                "checkout": reservation_payload["reservations"][0]["checkout"],
                "adults": reservation_payload["reservations"][0]["adults"],
                "children": reservation_payload["reservations"][0]["children"],
                "room_type_id": reservation_payload["reservations"][0]["roomTypeId"],
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
        pms_property_id: int = Field(default=38, description="Propiedad PMS (siempre 38).")

    def _normalize_phone(val: Optional[str]) -> str:
        return re.sub(r"\D+", "", val or "")

    async def _consulta_reserva_propia(
        folio_id: Optional[str] = None,
        fecha_inicio: Optional[str] = None,
        fecha_fin: Optional[str] = None,
        partner_name: Optional[str] = None,
        partner_email: Optional[str] = None,
        partner_phone: Optional[str] = None,
        pms_property_id: int = 38,
    ) -> str:
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

        if resolved_folio:
            tool = create_consulta_reserva_persona_tool()
            return await tool.ainvoke(
                {"folio_id": str(resolved_folio), "pms_property_id": pms_property_id}
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

        tool = create_consulta_reserva_general_tool()
        raw = await tool.ainvoke(
            {
                "fecha_inicio": fecha_inicio,
                "fecha_fin": fecha_fin,
                "pms_property_id": pms_property_id,
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
            "Consulta la reserva del propio huésped. Usa folio_id si lo hay; "
            "si no, necesita fechas y datos del huésped para filtrar."
        ),
        coroutine=_consulta_reserva_propia,
        args_schema=ConsultaReservaPropiaInput,
    )
