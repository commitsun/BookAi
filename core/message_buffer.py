# core/message_buffer.py
import asyncio
from dataclasses import dataclass, field
from typing import Callable, Awaitable, Dict, List, Optional


@dataclass
class ConversationState:
    messages: List[str] = field(default_factory=list)
    timer_task: Optional[asyncio.Task] = None
    processing_task: Optional[asyncio.Task] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    version: int = 0  # aumenta con cada mensaje para invalidar procesos antiguos


class MessageBufferManager:
    """
    Administra un buffer por conversación con:
      - Acumulación de mensajes durante `idle_seconds`
      - Reinicio del temporizador con cada nuevo mensaje
      - Al expirar, envía el lote al callback de proceso
      - Si llega un mensaje mientras se procesa, CANCELA el procesamiento previo
    """

    def __init__(self, idle_seconds: float = 10.0):
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
        Añade un mensaje al buffer y gestiona temporizador + cancelaciones.
        - process_callback(conversation_id, combined_text, version)
        """
        state = self._get_state(conversation_id)

        async with state.lock:
            # Guardar fragmento
            state.messages.append(text.strip())

            # Nueva "versión" de conversación → invalida procesos en curso
            state.version += 1
            current_version = state.version

            # Cancelar temporizador anterior si seguía vivo
            if state.timer_task and not state.timer_task.done():
                state.timer_task.cancel()
                state.timer_task = None

            # Si hay un procesamiento en curso, cancelarlo.
            if state.processing_task and not state.processing_task.done():
                state.processing_task.cancel()
                state.processing_task = None

            # Crear nuevo temporizador
            state.timer_task = asyncio.create_task(
                self._start_timer(conversation_id, current_version, process_callback)
            )

    async def _start_timer(
        self,
        conversation_id: str,
        version: int,
        process_callback: Callable[[str, str, int], Awaitable[None]],
    ):
        """Espera `idle_seconds`. Si nadie interrumpe, flush y procesa el lote."""
        try:
            await asyncio.sleep(self.idle_seconds)
            state = self._get_state(conversation_id)

            async with state.lock:
                # Si llegó otro mensaje después, esta versión ya no vale
                if version != state.version:
                    return

                # Combinar y limpiar buffer
                combined = " ".join(m for m in state.messages if m).strip()
                state.messages.clear()
                state.timer_task = None

                if not combined:
                    return

                # Lanzar tarea de procesamiento
                state.processing_task = asyncio.create_task(
                    process_callback(conversation_id, combined, version)
                )

        except asyncio.CancelledError:
            # Timer cancelado por llegada de nuevos mensajes → simplemente salir
            return
