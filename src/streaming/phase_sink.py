import asyncio

from streaming.events import ChatEvent, Phase


class PhaseSink:
    """In-memory channel for chat phase events.

    The chat page does not consume it yet; the sink only preserves emit order so a
    later streaming integration can read events asynchronously.
    """

    def __init__(self):
        self._events: list[ChatEvent] = []
        self._pending: asyncio.Queue[ChatEvent] = asyncio.Queue()

    def emit(self, phase: Phase, label: str) -> ChatEvent:
        chat_event = ChatEvent(seq=len(self._events) + 1, phase=phase, label=label)
        self._events.append(chat_event)
        self._pending.put_nowait(chat_event)
        return chat_event

    async def next_event(self) -> ChatEvent:
        return await self._pending.get()
