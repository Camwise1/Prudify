"""In-process event bus feeding the UI's Server-Sent Events stream.

The queue worker runs on a plain thread, so publishing has to hop back onto the
event loop. ``EventBus.publish`` is safe to call from any thread; subscribers
are asyncio queues, one per connected browser tab.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any

log = logging.getLogger(__name__)

MAX_BACKLOG = 200
SUBSCRIBER_QUEUE_SIZE = 500


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._backlog: deque[dict[str, Any]] = deque(maxlen=MAX_BACKLOG)
        self._counter = 0

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def backlog(self) -> list[dict[str, Any]]:
        return list(self._backlog)

    def publish(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """Fan an event out to every subscriber. Thread-safe."""
        self._counter += 1
        payload = {
            "id": self._counter,
            "type": event_type,
            "ts": time.time(),
            "data": data or {},
        }
        # Progress events are high-frequency; keeping them out of the replay
        # backlog stops a newly opened tab from replaying thousands of ticks.
        if event_type != "job.progress":
            self._backlog.append(payload)

        if self._loop is None or self._loop.is_closed():
            return
        try:
            self._loop.call_soon_threadsafe(self._dispatch, payload)
        except RuntimeError:  # loop shutting down
            pass

    def _dispatch(self, payload: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # A stalled client must never back-pressure the worker.
                self._subscribers.discard(queue)


def format_sse(payload: dict[str, Any]) -> str:
    return (
        f"id: {payload.get('id', 0)}\n"
        f"event: {payload.get('type', 'message')}\n"
        f"data: {json.dumps(payload.get('data', {}), default=str)}\n\n"
    )


bus = EventBus()
