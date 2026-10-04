"""One run's event stream, fanned out to every browser tab watching it.

The loop already emits structured events (`run.started`, `step`, `recovery`,
`verify.finished`, ...). This is the thin layer that turns those into something a
browser can consume: a replayable buffer plus one queue per subscriber.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any


class EventBus:
    def __init__(self, *, history: int = 2000, queue_size: int = 1000) -> None:
        self._history: list[dict[str, Any]] = []
        self._subscribers: set[asyncio.Queue] = set()
        self._history_limit = history
        self._queue_size = queue_size
        self._seq = 0

    def publish(self, event: str, data: dict[str, Any] | None = None) -> None:
        self._seq += 1
        payload = {"seq": self._seq, "event": event, "data": data or {},
                   "at": time.time()}
        self._history.append(payload)
        if len(self._history) > self._history_limit:
            del self._history[: len(self._history) - self._history_limit]
        for queue in list(self._subscribers):
            if queue.full():
                # A tab that stopped reading must never apply backpressure to the
                # agent. Shed its oldest event instead; the client re-syncs from
                # REST, and the final state always arrives.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                continue

    def history(self) -> list[dict[str, Any]]:
        return list(self._history)

    @property
    def seq(self) -> int:
        """Highest sequence number published, for handing to a reconnecting client."""
        return self._seq

    def replay_from(self, seq: int) -> list[dict[str, Any]]:
        """Everything a reconnecting client missed."""
        return [e for e in self._history if e["seq"] > seq]

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)