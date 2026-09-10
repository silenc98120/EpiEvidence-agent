"""In-process publish/subscribe delivery for task events."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from uuid import UUID

from backend.app.api.schemas import AGUIEvent, AGUIEventType


class EventBroker:
    """Fan out each task event to all connected local SSE clients."""

    def __init__(self) -> None:
        self._subscribers: dict[UUID, set[asyncio.Queue[AGUIEvent | None]]] = defaultdict(set)
        self._terminal_events: dict[UUID, AGUIEvent] = {}

    async def publish(self, event: AGUIEvent) -> None:
        """Publish an event without blocking on a slow subscriber."""

        if event.type in {
            AGUIEventType.RUN_ERROR,
            AGUIEventType.RUN_FINISHED,
        }:
            self._terminal_events[event.task_id] = event
        for queue in tuple(self._subscribers.get(event.task_id, ())):
            await queue.put(event)

    async def subscribe(self, task_id: UUID) -> AsyncIterator[AGUIEvent]:
        """Yield events until the subscriber is disconnected or closed."""

        queue: asyncio.Queue[AGUIEvent | None] = asyncio.Queue()
        self._subscribers[task_id].add(queue)
        try:
            terminal = self._terminal_events.get(task_id)
            if terminal is not None:
                yield terminal
                return
            while True:
                event = await queue.get()
                if event is None:
                    return
                yield event
        finally:
            subscribers = self._subscribers.get(task_id)
            if subscribers is not None:
                subscribers.discard(queue)
                if not subscribers:
                    self._subscribers.pop(task_id, None)

    async def close(self, task_id: UUID) -> None:
        """Close all current subscribers after a terminal task event."""

        for queue in tuple(self._subscribers.get(task_id, ())):
            await queue.put(None)
