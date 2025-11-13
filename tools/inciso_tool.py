"""
🔔 Inciso Tool - Envía mensajes intermedios al usuario
=====================================================
Esta herramienta permite al agente Main enviar mensajes de cortesía
o actualizaciones de estado al usuario mientras procesa su solicitud
en segundo plano (por ejemplo, mientras consulta con el encargado).
"""

import asyncio
import inspect
import logging
from typing import Any, Callable, Optional

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

    def __init__(self, send_callback: Optional[Callable[[str], Any]] = None):
        """
        Args:
            send_callback: Función que envía el mensaje al usuario.
                          Firma: send_callback(message: str) -> None | Awaitable
        """
        self.send_callback = send_callback
        log.info("✅ IncisoTool inicializado")

    async def _send_inciso_async(self, mensaje: str) -> str:
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

            resultado = self.send_callback(mensaje)
            if inspect.isawaitable(resultado):
                await resultado

            log.info(f"📤 Inciso enviado: {mensaje[:50]}...")
            return f"✅ Mensaje intermedio enviado al usuario: '{mensaje}'"

        except Exception as exc:  # pragma: no cover - logging defensivo
            log.error(f"❌ Error al enviar inciso: {exc}")
            return f"❌ Error al enviar mensaje intermedio: {str(exc)}"

    def _send_inciso_sync(self, mensaje: str) -> str:
        """Wrapper síncrono para compatibilidad con LangChain."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        if loop.is_running():
            loop.create_task(self._send_inciso_async(mensaje))
            return f"✅ Mensaje intermedio enviado al usuario: '{mensaje}'"

        return loop.run_until_complete(self._send_inciso_async(mensaje))

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
            func=self._send_inciso_sync,
            coroutine=self._send_inciso_async,
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
