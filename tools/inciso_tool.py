#inciso_tool.py
"""
🔔 Inciso Tool - Envía mensajes intermedios al usuario
=====================================================
Esta herramienta permite al agente Main enviar mensajes de cortesía
o actualizaciones de estado al usuario mientras procesa su solicitud
en segundo plano (por ejemplo, mientras consulta con el encargado).
"""

import logging
from typing import Optional
from pydantic import BaseModel, Field
from langchain.tools import StructuredTool

log = logging.getLogger("IncisoTool")


class IncisoInput(BaseModel):
    """Input schema para la herramienta Inciso."""
    mensaje: str = Field(
        description="Mensaje intermedio a enviar al usuario (ej: 'Un momento, estoy consultando con el encargado...')"
    )


class IncisoTool:
    """
    Herramienta que permite enviar mensajes intermedios al usuario.
    Se usa cuando el agente necesita tiempo para procesar (ej: consulta con encargado).
    """
    
    def __init__(self, send_callback=None):
        """
        Args:
            send_callback: Función que envía el mensaje al usuario.
                          Firma: send_callback(chat_id: str, message: str)
        """
        self.send_callback = send_callback
        log.info("✅ IncisoTool inicializado")
    
    def _send_inciso(self, mensaje: str) -> str:
        """
        Envía un mensaje intermedio al usuario.
        
        Args:
            mensaje: Texto del mensaje intermedio
            
        Returns:
            Confirmación de envío
        """
        try:
            if not self.send_callback:
                log.warning("⚠️ No hay callback configurado para enviar inciso")
                return "⚠️ Mensaje guardado pero no se pudo enviar (falta configuración de canal)"
            
            # Enviar mensaje a través del callback
            self.send_callback(mensaje)
            log.info(f"📤 Inciso enviado: {mensaje[:50]}...")
            
            return f"✅ Mensaje intermedio enviado al usuario: '{mensaje}'"
            
        except Exception as e:
            log.error(f"❌ Error al enviar inciso: {e}")
            return f"❌ Error al enviar mensaje intermedio: {str(e)}"
    
    def as_tool(self) -> StructuredTool:
        """
        Convierte esta clase en una herramienta compatible con LangChain.
        
        Returns:
            StructuredTool configurado para usar con agentes
        """
        return StructuredTool(
            name="Inciso",
            description=(
                "Envía un mensaje intermedio de cortesía al usuario mientras procesas su solicitud. "
                "Úsala cuando necesites tiempo para consultar información (ej: con el encargado) "
                "o cuando el proceso tarde más de lo esperado. "
                "Ejemplos: '🕓 Un momento por favor, estoy consultando...', "
                "'⏳ Dame un segundo mientras reviso esa información...'"
            ),
            func=self._send_inciso,
            args_schema=IncisoInput,
        )


def create_inciso_tool(send_callback=None) -> StructuredTool:
    """
    Factory function para crear la herramienta Inciso.
    
    Args:
        send_callback: Función para enviar mensajes al usuario
        
    Returns:
        StructuredTool configurado
    """
    tool_instance = IncisoTool(send_callback=send_callback)
    return tool_instance.as_tool()