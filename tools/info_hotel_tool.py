"""
🏨 InfoHotelTool — Información general del hotel
================================================
Convierte el subagente de información del hotel (InfoAgent)
en una herramienta LangChain compatible con el MainAgent.
Responde preguntas sobre servicios, instalaciones, horarios,
políticas, amenities y más.
"""

import logging
import asyncio
from pydantic import BaseModel, Field
from langchain.tools import StructuredTool
from agents.info_agent import InfoAgent

log = logging.getLogger("InfoHotelTool")


class InfoHotelInput(BaseModel):
    """Input schema para la herramienta de información del hotel."""
    consulta: str = Field(
        description=(
            "La consulta del usuario sobre servicios, instalaciones, políticas, "
            "horarios, amenidades o cualquier información general del hotel."
        )
    )


class InfoHotelTool:
    """
    Herramienta que delega consultas de información general al subagente especializado.
    """

    def __init__(self, memory_manager=None, chat_id: str = ""):
        self.memory_manager = memory_manager
        self.chat_id = chat_id

        self.agent = InfoAgent(memory_manager=memory_manager)

        log.info(f"✅ InfoHotelTool inicializado para chat {chat_id}")

    # ----------------------------------------------------------
    async def _procesar_consulta(self, consulta: str) -> str:
        """Delega la consulta al subagente de información del hotel."""
        try:
            log.info(f"📚 Procesando consulta de info hotel: {consulta[:80]}...")

            # Recuperar historial de conversación
            history = []
            if self.memory_manager and self.chat_id:
                try:
                    history = self.memory_manager.get_memory_as_messages(self.chat_id)
                except Exception as e:
                    log.warning(f"⚠️ No se pudo obtener memoria: {e}")

            # ⚠️ Invocar subagente de forma asíncrona
            respuesta = await self.agent.ainvoke(
                chat_history=history,
                chat_id=self.chat_id or "",
            )
            if respuesta == "ESCALATION_REQUIRED":
                log.warning("⚠️ InfoAgent sugirió escalación tras confirmar falta de información.")
                return "ESCALATION_REQUIRED"

            # Detectar si requiere escalación
            if (
                "ESCALAR_A_INTERNO" in respuesta
                or self._is_escalation_needed(respuesta)
            ):
                log.warning("⚠️ Subagente info no pudo resolver la consulta")
                return "ESCALATION_REQUIRED"

            log.info(f"✅ Respuesta generada ({len(respuesta)} caracteres)")
            return respuesta

        except Exception as e:
            log.error(f"❌ Error en subagente info: {e}", exc_info=True)
            return (
                f"❌ Error al consultar la información del hotel: {str(e)}. "
                "Por favor, reformula tu consulta o contacta directamente con el hotel."
            )

    # ----------------------------------------------------------
    def _is_escalation_needed(self, respuesta: str) -> bool:
        """Detecta si la respuesta requiere escalar al encargado."""
        keywords = [
            "no encuentro",
            "no tengo información",
            "no dispongo",
            "consultar con el encargado",
            "contacta con recepción",
            "no puedo confirmar",
        ]
        return any(k in respuesta.lower() for k in keywords)

    # ----------------------------------------------------------
    def as_tool(self) -> StructuredTool:
        """Convierte la clase en una tool compatible con LangChain."""
        return StructuredTool(
            name="knowledge_base",
            description=(
                "Responde preguntas sobre servicios, horarios, políticas, amenities o ubicación del hotel.\n"
                "Usa esta herramienta si el huésped pregunta por:\n"
                " - Servicios (spa, restaurante, gimnasio, etc.)\n"
                " - Políticas o condiciones\n"
                " - Horarios y ubicación\n"
                "Si la respuesta es 'ESCALATION_REQUIRED', usa la herramienta 'Interno' para escalar."
            ),
            func=self._sync_wrapper,  # 🧩 adaptador para entornos sync
            coroutine=self._procesar_consulta,  # 🧩 llamada asíncrona real
            args_schema=InfoHotelInput,
        )

    # ----------------------------------------------------------
    def _sync_wrapper(self, consulta: str) -> str:
        """Permite usar el tool desde entornos sin soporte async."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        if loop.is_running():
            import nest_asyncio
            nest_asyncio.apply()
        return loop.run_until_complete(self._procesar_consulta(consulta))


# ----------------------------------------------------------
def create_info_hotel_tool(memory_manager=None, chat_id: str = "") -> StructuredTool:
    """Factory para crear la herramienta de información del hotel."""
    return InfoHotelTool(memory_manager=memory_manager, chat_id=chat_id).as_tool()
