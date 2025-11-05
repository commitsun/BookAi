#dispo-precios-tool.py
"""
🏨 Disponibilidad y Precios Tool - Subagente como herramienta
==============================================================
Convierte el subagente de disponibilidad/precios en una tool que
el agente Main puede invocar cuando el usuario pregunta sobre
habitaciones, precios, o disponibilidad.
"""

import logging
from typing import Optional
from pydantic import BaseModel, Field
from langchain.tools import StructuredTool
from agents.dispo_precios_agent import DispoPreciosAgent

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
    Herramienta que delega consultas de disponibilidad/precios al subagente especializado.
    El subagente tiene acceso al PMS del hotel vía MCP server.
    """

    def __init__(self, memory_manager=None, chat_id: str = ""):
        """
        Args:
            memory_manager: Gestor de memoria para contexto conversacional
            chat_id: ID del chat (para tracking)
        """
        self.memory_manager = memory_manager
        self.chat_id = chat_id

        # ✅ CORREGIDO: propagar memory_manager al subagente
        self.agent = DispoPreciosAgent(
            model_name="gpt-4.1-mini",
            memory_manager=memory_manager
        )

        log.info(f"✅ DispoPreciosTool inicializado para chat {chat_id}")

    def _procesar_consulta(self, consulta: str) -> str:
        """
        Delega la consulta al subagente de disponibilidad y precios.
        """
        try:
            log.info(f"🏨 Procesando consulta de dispo/precios: {consulta[:80]}...")

            # ✅ Obtener historial correcto desde MemoryManager
            history = []
            if self.memory_manager and self.chat_id:
                try:
                    history = self.memory_manager.get_memory_as_messages(self.chat_id)
                except Exception as e:
                    log.warning(f"⚠️ No se pudo obtener memoria: {e}")

            # Invocar al subagente
            respuesta = self.agent.invoke(
                user_input=consulta,
                chat_history=history
            )

            log.info(f"✅ Respuesta generada ({len(respuesta)} caracteres)")
            return respuesta

        except Exception as e:
            log.error(f"❌ Error en subagente dispo/precios: {e}", exc_info=True)
            return (
                f"❌ Error al consultar disponibilidad y precios: {str(e)}. "
                "Por favor, reformula tu consulta o contacta directamente con el hotel."
            )

    
    def as_tool(self) -> StructuredTool:
        """
        Convierte esta clase en una herramienta compatible con LangChain.
        
        Returns:
            StructuredTool configurado para usar con agentes
        """
        return StructuredTool(
            name="availability_pricing",
            description=(
                "Usa esta herramienta para responder preguntas sobre:\n"
                "- Disponibilidad de habitaciones para fechas específicas\n"
                "- Precios y tarifas de habitaciones\n"
                "- Tipos de habitaciones disponibles\n"
                "- Consultas sobre reservas\n"
                "- Capacidad de huéspedes por habitación\n"
                "\n"
                "Esta herramienta tiene acceso directo al sistema de gestión del hotel (PMS). "
                "Pasa la consulta COMPLETA del usuario incluyendo TODOS los detalles: fechas, "
                "número de personas, preferencias, etc."
            ),
            func=self._procesar_consulta,
            args_schema=DispoPreciosInput,
        )


def create_dispo_precios_tool(memory_manager=None, chat_id: str = "") -> StructuredTool:
    """
    Factory function para crear la herramienta de disponibilidad y precios.
    
    Args:
        memory_manager: Gestor de memoria conversacional
        chat_id: ID del chat
        
    Returns:
        StructuredTool configurado
    """
    tool_instance = DispoPreciosTool(memory_manager=memory_manager, chat_id=chat_id)
    return tool_instance.as_tool()