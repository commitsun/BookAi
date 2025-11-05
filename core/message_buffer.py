import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable, Awaitable, Dict, List, Optional

log = logging.getLogger("MessageBufferManager")


@dataclass
class ConversationState:
    messages: List[str] = field(default_factory=list)
    timer_task: Optional[asyncio.Task] = None
    processing_task: Optional[asyncio.Task] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    version: int = 0  # aumenta con cada mensaje para invalidar procesos antiguos


class MessageBufferManager:
    """
    Administra un buffer por conversación:
      ✅ Agrupa mensajes consecutivos del mismo usuario
      ✅ Reinicia temporizador con cada nuevo mensaje
      ✅ Combina mensajes como bloque coherente (con saltos de línea)
      ✅ Llama al callback tras inactividad de `idle_seconds`
    """

    def __init__(self, idle_seconds: float = 8.0):
        self.idle_seconds = float(idle_seconds)
        self._convs: Dict[str, ConversationState] = {}

    def _get_state(self, cid: str) -> ConversationState:
        if cid not in self._convs:
            self._convs[cid] = ConversationState()
        return self._convs[cid]

    async def add_message(
        self,
        conversation_id: str,
        text: str,
        process_callback: Callable[[str, str, int], Awaitable[None]],
    ):
        """
        Añade un mensaje al buffer y reinicia el temporizador.
        Si no hay más mensajes después de `idle_seconds`, se procesa el bloque combinado.
        """
        state = self._get_state(conversation_id)

        async with state.lock:
            state.messages.append(text.strip())
            state.version += 1
            current_version = state.version

            # Cancelar temporizador previo
            if state.timer_task and not state.timer_task.done():
                state.timer_task.cancel()

            # Cancelar procesamiento si aún no terminó
            if state.processing_task and not state.processing_task.done():
                state.processing_task.cancel()

            # Nuevo temporizador
            state.timer_task = asyncio.create_task(
                self._start_timer(conversation_id, current_version, process_callback)
            )

        log.info(f"🧩 Buffer actualizado ({len(state.messages)} msgs) para {conversation_id}")

    async def _start_timer(
        self,
        conversation_id: str,
        version: int,
        process_callback: Callable[[str, str, int], Awaitable[None]],
    ):
        """Espera `idle_seconds`. Si no llegan más mensajes, procesa el bloque."""
        try:
            await asyncio.sleep(self.idle_seconds)
            state = self._get_state(conversation_id)

            async with state.lock:
                if version != state.version:
                    return  # hubo nuevos mensajes → cancelar

                messages = list(state.messages)
                state.messages.clear()
                state.timer_task = None

                if not messages:
                    return

                # 🔹 Combinar mensajes con saltos de línea y limpieza
                combined = self._combine_messages(messages)

                # Lanzar procesamiento
                state.processing_task = asyncio.create_task(
                    process_callback(conversation_id, combined, version)
                )

        except asyncio.CancelledError:
            return  # Timer cancelado, llega mensaje nuevo

    def _combine_messages(self, messages: List[str]) -> str:
        """
        Une mensajes en un bloque estructurado y legible para el modelo.
        - Elimina duplicados triviales
        - Une con saltos de línea para claridad
        """
        cleaned = []
        last = ""
        for m in messages:
            m = m.strip()
            if m and m != last:
                cleaned.append(m)
                last = m

        combined = "\n".join(cleaned).strip()
        log.info(f"🧠 Combinando {len(cleaned)} mensajes:\n{combined}")
        return combined
