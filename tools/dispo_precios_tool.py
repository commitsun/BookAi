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
from core.config import ModelConfig, ModelTier  # ✅ Import centralizado

log = logging.getLogger("DispoPreciosTool")


# Input schema para la herramienta de disponibilidad y precios.
# Se usa en el flujo de tool de disponibilidad y precios como pieza de organización, contrato de datos o punto de extensión.
# Sus instancias reciben los campos declarados y validan payloads antes de entrar en endpoints, tools o agentes.
# No produce efectos por sí sola; sirve como estructura tipada para mover información entre capas.
class DispoPreciosInput(BaseModel):
    """Input schema para la herramienta de disponibilidad y precios."""
    consulta: str = Field(
        description=(
            "La consulta del usuario sobre disponibilidad, precios o tipos de habitación. "
            "Incluye TODOS los detalles relevantes: fechas, número de huéspedes, preferencias, etc."
        )
    )


# Herramienta que delega consultas de disponibilidad/precios al subagente especializado.
# Se usa en el flujo de tool de disponibilidad y precios como pieza de organización, contrato de datos o punto de extensión.
# Agrupa atributos y métodos de una responsabilidad concreta; la configuración real entra por su constructor o por sus campos.
# Los efectos reales ocurren cuando sus métodos se invocan; la definición de clase solo organiza estado y responsabilidades.
class DispoPreciosTool:
    """
    Herramienta que delega consultas de disponibilidad/precios al subagente especializado.
    El subagente tiene acceso al PMS del hotel vía MCP server.
    """

    # Args:.
    # Se usa dentro de `DispoPreciosTool` en el flujo de tool de disponibilidad y precios.
    # Recibe `memory_manager` como dependencias o servicios compartidos inyectados desde otras capas, y `chat_id` como datos de contexto o entrada de la operación.
    # No devuelve valor; deja la instancia preparada con sus dependencias y estado inicial. Puede activar tools o agentes.
    def __init__(self, memory_manager=None, chat_id: str = ""):
        """
        Args:
            memory_manager: Gestor de memoria para contexto conversacional
            chat_id: ID del chat (para tracking)
        """
        self.memory_manager = memory_manager
        self.chat_id = chat_id

        # ✅ Usa el modelo centralizado desde ModelConfig (SUBAGENT)
        model_name, temperature = ModelConfig.get_model(ModelTier.SUBAGENT)

        self.agent = DispoPreciosAgent(
            memory_manager=memory_manager,
            model_name=model_name,
            temperature=temperature,
        )

        log.info(f"✅ DispoPreciosTool inicializado para chat {chat_id} (modelo={model_name})")

    # Delega la consulta al subagente de disponibilidad y precios.
    # Se usa dentro de `DispoPreciosTool` en el flujo de tool de disponibilidad y precios.
    # Recibe `consulta` como entrada principal según la firma.
    # Devuelve un `str` con el resultado de esta operación. Puede realizar llamadas externas o a modelos, activar tools o agentes.
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

    # Convierte esta clase en una herramienta compatible con LangChain.
    # Se usa dentro de `DispoPreciosTool` en el flujo de tool de disponibilidad y precios.
    # No recibe parámetros externos; trabaja con estado capturado por el cierre o atributos de instancia.
    # Devuelve una tool configurada para que el agente la pueda invocar directamente. Puede activar tools o agentes.
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
                "- Capacidad de huéspedes por habitación\n"
                "\n"
                "Esta herramienta tiene acceso directo al sistema de gestión del hotel (PMS). "
                "Pasa la consulta COMPLETA del usuario incluyendo TODOS los detalles: fechas, "
                "número de personas, preferencias, etc."
            ),
            func=self._procesar_consulta,
            args_schema=DispoPreciosInput,
        )


# Factory function para crear la herramienta de disponibilidad y precios.
# Se usa en el flujo de tool de disponibilidad y precios para preparar datos, validaciones o decisiones previas.
# Recibe `memory_manager` como dependencias o servicios compartidos inyectados desde otras capas, y `chat_id` como datos de contexto o entrada de la operación.
# Devuelve una tool configurada para que el agente la pueda invocar directamente. Puede activar tools o agentes.
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
