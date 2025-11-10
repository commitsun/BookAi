"""
🏨 Disponibilidad y Precios Tool - Datos reales desde MCP Server
==============================================================
Obtiene la disponibilidad y precios directamente desde Roomdoo
a través del MCP Server HTTP (sin intervención del modelo LLM).
"""

import logging
import datetime
import asyncio
from typing import Optional
from pydantic import BaseModel, Field
from langchain.tools import StructuredTool
from core.mcp_client import call_availability_pricing  # ✅ llamada directa al MCP Server

log = logging.getLogger("DispoPreciosTool")


class DispoPreciosInput(BaseModel):
    """Input schema para la herramienta de disponibilidad y precios."""
    consulta: str = Field(
        description=(
            "La consulta del usuario sobre disponibilidad, precios, tipos de habitación o reservas. "
            "Incluye TODOS los detalles relevantes: fechas, número de huéspedes, preferencias, etc."
        )
    )


class DispoPreciosTool:
    """
    Herramienta que consulta disponibilidad y precios REALES desde Roomdoo
    a través del MCP Server HTTP local.
    """

    def __init__(self, memory_manager=None, chat_id: str = ""):
        self.memory_manager = memory_manager
        self.chat_id = chat_id
        log.info(f"✅ DispoPreciosTool factual inicializada para chat {chat_id}")

    # ======================================================
    # 🔧 MÉTODO PRINCIPAL
    # ======================================================
    def _procesar_consulta(self, consulta: str) -> str:
        """Obtiene disponibilidad y precios reales (sin modelo) con formato limpio."""
        try:
            log.info(f"🏨 [Factual] Procesando consulta: {consulta[:100]}...")

            # 📅 Fechas por defecto si el usuario no da ninguna
            today = datetime.date.today()
            checkin = today + datetime.timedelta(days=7)
            checkout = checkin + datetime.timedelta(days=2)

            # 🔗 Llamada al MCP Server (HTTP)
            result = asyncio.run(
                call_availability_pricing(
                    checkin=str(checkin),
                    checkout=str(checkout),
                    occupancy=2,
                    pms_property_id=38
                )
            )

            # ❌ Si hay error en la respuesta
            if not result or "error" in result:
                log.error(f"❌ Error desde MCP Server: {result}")
                return "No se pudo obtener la disponibilidad del PMS."

            rooms = result.get("data", [])
            if not rooms:
                return "No hay disponibilidad para esas fechas."

            # ✅ Construir texto de respuesta factual (sin .0)
            response_lines = []
            for r in rooms:
                price = r.get("price", "?")
                if isinstance(price, float) and price.is_integer():
                    price = int(price)  # elimina el .0
                response_lines.append(
                    f"- {r.get('roomTypeName', 'Habitación')} "
                    f"({r.get('avail', 0)} disp.) — {price} €"
                )

            response_text = (
                f"Disponibilidad actual para 2 personas:\n\n"
                + "\n".join(response_lines)
                + "\n\n¿Quieres que te ayude a reservar alguna?"
            )

            log.info("✅ [Factual] Respuesta enviada con datos reales del PMS.")
            return response_text

        except Exception as e:
            log.error(f"❌ Error general en DispoPreciosTool factual: {e}", exc_info=True)
            return "Ocurrió un problema al obtener la disponibilidad real del hotel."


    # ======================================================
    # 🧩 CONVERSIÓN A TOOL
    # ======================================================
    def as_tool(self) -> StructuredTool:
        """Convierte esta clase en una herramienta de LangChain."""
        return StructuredTool(
            name="availability_pricing",
            description=(
                "Obtiene disponibilidad y precios REALES de las habitaciones desde el PMS del hotel (Roomdoo), "
                "a través del MCP Server HTTP. No inventa ni resume información, muestra los datos exactos."
            ),
            func=self._procesar_consulta,
            args_schema=DispoPreciosInput,
        )


def create_dispo_precios_tool(memory_manager=None, chat_id: str = "") -> StructuredTool:
    """Factory function para crear la tool factual."""
    tool_instance = DispoPreciosTool(memory_manager=memory_manager, chat_id=chat_id)
    return tool_instance.as_tool()
