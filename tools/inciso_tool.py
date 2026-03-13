"""
🔔 Inciso Tool - Envía mensajes intermedios al usuario
=====================================================
Esta herramienta permite al agente Main enviar mensajes de cortesía
o actualizaciones de estado al usuario mientras procesa su solicitud
en segundo plano (por ejemplo, mientras consulta con el encargado).
"""

import logging
import time
import asyncio
from typing import Optional
from pydantic import BaseModel, Field
from langchain.tools import StructuredTool

log = logging.getLogger("IncisoTool")


class IncisoInput(BaseModel):
    """Input schema para la herramienta Inciso."""
    mensaje: str = Field(
        description=(
            "Mensaje intermedio a enviar al usuario "
            "(ej: 'Un momento, estoy consultando con el encargado...')"
        )
    )


class IncisoTool:
    """
    Herramienta que permite enviar mensajes intermedios al usuario.
    Se usa cuando el agente necesita tiempo para procesar (ej: consulta con encargado).
    """

    def __init__(self, send_callback=None, cooldown_seconds: int = 15):
        """
        Args:
            send_callback: Función que envía el mensaje al usuario (puede ser async).
                           Firma: send_callback(mensaje: str)
            cooldown_seconds: Tiempo mínimo entre envíos reales de inciso
                              dentro de la misma conversación/ejecución.
        """
        self.send_callback = send_callback
        self.cooldown_seconds = cooldown_seconds

        # 🔒 Estado interno para evitar SPAM dentro del mismo chain
        self._last_sent_at: Optional[float] = None
        self._last_message: Optional[str] = None
        self._send_count = 0  # 🔢 Protección adicional contra loops

        log.info("✅ IncisoTool inicializado correctamente")

    # --------------------------------------------------
    def _can_send(self, mensaje: str) -> bool:
        """Decide si se puede enviar un nuevo inciso o se suprime por cooldown o límite."""
        # Máximo 2 incisos por sesión de agente
        if self._send_count >= 2:
            log.warning("🚫 Límite de incisos alcanzado (2). Se ignora nuevo intento.")
            return False

        if self._last_sent_at is None:
            return True

        elapsed = time.time() - self._last_sent_at

        # 1️⃣ Si ha pasado muy poco tiempo, no mandamos nada.
        if elapsed < self.cooldown_seconds:
            log.debug(
                f"⏱️ Inciso suprimido por cooldown "
                f"({elapsed:.1f}s < {self.cooldown_seconds}s)"
            )
            return False

        # 2️⃣ Si es el mismo mensaje literal, no lo repetimos.
        if self._last_message and self._last_message.strip() == mensaje.strip():
            log.debug("🔁 Inciso duplicado detectado, suprimido.")
            return False

        return True

    # --------------------------------------------------
    def _send_inciso(self, mensaje: str) -> str:
        """
        Envía un mensaje intermedio al usuario, manejando correctamente
        callbacks asíncronos o síncronos incluso si no hay un event loop activo.
        """
        try:
            if not mensaje:
                return "⚠️ Mensaje vacío, nada que enviar."

            # 🛑 Anti-spam: no enviar si está dentro del cooldown o es duplicado
            if not self._can_send(mensaje):
                log.debug(f"🧩 Inciso suprimido (duplicado o cooldown): {mensaje}")
                return "🟢 Inciso ya enviado — no repetir."

            if not self.send_callback:
                log.warning("⚠️ No hay callback configurado para enviar inciso")
                return (
                    "⚠️ Mensaje guardado pero no se pudo enviar "
                    "(falta configuración de canal)"
                )

            # ✅ Enviar realmente el mensaje (soporta async y sync)
            if asyncio.iscoroutinefunction(self.send_callback):
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self.send_callback(mensaje))
                except RuntimeError:
                    # No hay loop activo: ejecutamos sincrónicamente
                    asyncio.run(self.send_callback(mensaje))
            else:
                self.send_callback(mensaje)

            # 📦 Actualizar estado interno
            self._last_sent_at = time.time()
            self._last_message = mensaje
            self._send_count += 1

            log.info(f"📤 Inciso enviado al usuario: {mensaje[:80]}...")
            return f"✅ Mensaje intermedio enviado al usuario: '{mensaje}'"

        except Exception as e:
            log.error(f"❌ Error al enviar inciso: {e}", exc_info=True)
            return f"❌ Error al enviar mensaje intermedio: {str(e)}"

    # --------------------------------------------------
    def as_tool(self) -> StructuredTool:
        """
        Convierte esta clase en una herramienta compatible con LangChain.
        """
        return StructuredTool(
            name="Inciso",
            description=(
                "Envía un mensaje intermedio de cortesía al usuario mientras procesas su solicitud. "
                "Úsala con moderación (máx. 2 veces por interacción), cuando necesites tiempo para "
                "consultar información o coordinar con el encargado. "
                "Ejemplos: '🕓 Un momento por favor, estoy consultando...', "
                "'⏳ Dame un segundo mientras reviso esa información...'"
            ),
            func=self._send_inciso,
            args_schema=IncisoInput,
        )


def create_inciso_tool(send_callback=None) -> StructuredTool:
    """
    Factory function para crear la herramienta Inciso.
    """
    tool_instance = IncisoTool(send_callback=send_callback)
    return tool_instance.as_tool()
