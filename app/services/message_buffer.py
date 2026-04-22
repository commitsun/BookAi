"""
Message buffer — accumulates rapid consecutive messages before processing.

When a guest sends multiple messages quickly (e.g. "hola" + "quiero saber" +
"los precios" in 3 seconds), the buffer concatenates them and processes as
one. This avoids triggering the AI pipeline 3 times and getting 3 responses.

Usage in webhook_service:
    buffered = await message_buffer.add(conversation_id, content)
    if buffered is None:
        return  # timer still running, will be processed later
    # buffered = concatenated text, process it
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

log = logging.getLogger("message_buffer")

BUFFER_TIMEOUT = 3.0  # seconds to wait for more messages


@dataclass
class BufferEntry:
    messages: list[str] = field(default_factory=list)
    timer: asyncio.TimerHandle | None = None
    future: asyncio.Future | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)


class MessageBuffer:

    def __init__(self, timeout: float = BUFFER_TIMEOUT):
        self._buffers: dict[int, BufferEntry] = {}
        self._timeout = timeout

    async def add(self, conversation_id: int, content: str) -> str | None:
        """Add a message to the buffer. Returns concatenated text when
        the buffer expires, or None if still accumulating.

        The first caller for a conversation waits and gets the result.
        Subsequent callers within the timeout window return None immediately.
        """
        loop = asyncio.get_event_loop()

        if conversation_id in self._buffers:
            # Existing buffer — append and reset timer
            entry = self._buffers[conversation_id]
            entry.messages.append(content)
            if entry.timer:
                entry.timer.cancel()
            entry.timer = loop.call_later(
                self._timeout, self._flush, conversation_id,
            )
            return None  # Not the first caller, skip processing

        # New buffer — first message
        entry = BufferEntry(messages=[content])
        entry.future = loop.create_future()
        entry.timer = loop.call_later(
            self._timeout, self._flush, conversation_id,
        )
        self._buffers[conversation_id] = entry

        # Wait for the buffer to flush
        result = await entry.future
        return result

    def _flush(self, conversation_id: int) -> None:
        entry = self._buffers.pop(conversation_id, None)
        if entry and entry.future and not entry.future.done():
            combined = "\n".join(entry.messages)
            entry.future.set_result(combined)
            log.debug(
                "Buffer flushed conv=%d messages=%d chars=%d",
                conversation_id, len(entry.messages), len(combined),
            )
