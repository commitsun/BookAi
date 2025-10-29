#interno_tool.py
"""
🔧 Interno Tool - Escalación al encargado del hotel vía Telegram
=================================================================
Herramienta que permite al agente Main escalar consultas al equipo
humano del hotel cuando no puede resolverlas con los subagentes.
"""

import logging
import json
from typing import Optional
from pydantic import BaseModel, Field
from langchain.tools import StructuredTool
from agents.interno_agent import InternoAgent

log = logging.getLogger("InternoTool")


class InternoInput(BaseModel):
    """Input schema para la herramienta Interno."""
    consulta_usuario: str = Field(
        description="La consulta original del usuario que necesita escalación al encargado"
    )
    contexto: str = Field(
        default="",
        description="Contexto adicional o razón por la que se escala (ej: 'No encontré información en la base de conocimientos')"
    )


class InternoTool:
    """
    Herramienta que escala consultas al encargado del hotel vía Telegram.
    Se invoca cuando los subagentes no pueden resolver la consulta del usuario.
    """
    
    def __init__(self, chat_id: str, hotel_name: str = "Hotel"):
        """
        Args:
            chat_id: ID del chat del usuario (para contexto)
            hotel_name: Nombre del hotel (para personalización)
        """
        self.chat_id = chat_id
        self.hotel_name = hotel_name
        self.interno_agent = InternoAgent()
        log.info(f"✅ InternoTool inicializado para chat {chat_id}")
    
    def _escalar_a_interno(self, consulta_usuario: str, contexto: str = "") -> str:
        """
        Escala la consulta al agente interno para que contacte al encargado.
        
        Args:
            consulta_usuario: La consulta que no se pudo resolver
            contexto: Información adicional sobre por qué se escala
            
        Returns:
            Mensaje confirmando la escalación
        """
        try:
            log.info(f"📞 Escalando consulta al encargado: {consulta_usuario[:80]}...")
            
            # Preparar el mensaje completo para el encargado
            mensaje_completo = f"""
🔔 NUEVA CONSULTA ESCALADA

📱 Chat ID: {self.chat_id}
🏨 Hotel: {self.hotel_name}

❓ Consulta del huésped:
{consulta_usuario}

📝 Contexto de escalación:
{contexto if contexto else 'Consulta no resuelta por los agentes automáticos'}

⏰ Esperando respuesta del encargado...
"""
            
            # Llamar al agente interno para que envíe por Telegram
            result = self.interno_agent.notify_staff(
                message=mensaje_completo,
                chat_id=self.chat_id
            )
            
            log.info(f"✅ Escalación completada: {result}")
            
            # Mensaje de confirmación para el agente Main
            return (
                "✅ Consulta escalada al encargado del hotel vía Telegram. "
                "El sistema quedará en espera de la respuesta del encargado. "
                "Cuando responda, recibirás su mensaje para enviarlo al huésped."
            )
            
        except Exception as e:
            log.error(f"❌ Error al escalar a interno: {e}")
            return (
                f"❌ Error al contactar con el encargado: {str(e)}. "
                "Por favor, informa al huésped que estamos experimentando problemas técnicos "
                "y que lo contactaremos pronto."
            )
    
    def as_tool(self) -> StructuredTool:
        """
        Convierte esta clase en una herramienta compatible con LangChain.
        
        Returns:
            StructuredTool configurado para usar con agentes
        """
        return StructuredTool(
            name="Interno",
            description=(
                "Escala la consulta del usuario al encargado del hotel vía Telegram cuando "
                "NO puedes resolverla con las otras herramientas disponibles (dispo/precios, "
                "información del hotel). Úsala SOLO como último recurso, después de intentar "
                "con las demás herramientas. Antes de llamar a esta tool, SIEMPRE usa la tool "
                "'Inciso' para informar al usuario que estás consultando con el encargado."
            ),
            func=self._escalar_a_interno,
            args_schema=InternoInput,
        )


def create_interno_tool(chat_id: str, hotel_name: str = "Hotel") -> StructuredTool:
    """
    Factory function para crear la herramienta Interno.
    
    Args:
        chat_id: ID del chat del usuario
        hotel_name: Nombre del hotel
        
    Returns:
        StructuredTool configurado
    """
    tool_instance = InternoTool(chat_id=chat_id, hotel_name=hotel_name)
    return tool_instance.as_tool()