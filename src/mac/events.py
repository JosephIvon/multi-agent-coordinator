from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class TaskEvent:
    event_id: str = field(default_factory=lambda: str(uuid4()))
    type: str = ""
    task_id: str = ""
    trace_id: str = ""
    actor: str = ""
    from_status: str | None = None
    to_status: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class Subscription:
    def __init__(self, close_callback: Callable[[], None]) -> None:
        self._close_callback = close_callback
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._close_callback()


class QueueSubscription(Subscription):
    def __init__(self, queue: asyncio.Queue[TaskEvent], close_callback: Callable[[], None]) -> None:
        super().__init__(close_callback)
        self.queue = queue


class TaskEventBus:
    def __init__(self) -> None:
        self._subscribers: list[tuple[Callable[[TaskEvent], None], set[str] | None]] = []
        self._queues: list[tuple[asyncio.Queue[TaskEvent], asyncio.AbstractEventLoop | None, set[str] | None]] = []
        self._lock = RLock()

    def subscribe(
        self,
        handler: Callable[[TaskEvent], None],
        *,
        event_types: set[str] | None = None,
    ) -> Subscription:
        record = (handler, set(event_types) if event_types is not None else None)
        with self._lock:
            self._subscribers.append(record)

        def close() -> None:
            with self._lock:
                try:
                    self._subscribers.remove(record)
                except ValueError:
                    return

        return Subscription(close)

    def subscribe_queue(
        self,
        *,
        event_types: set[str] | None = None,
        maxsize: int = 0,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> QueueSubscription:
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

        queue: asyncio.Queue[TaskEvent] = asyncio.Queue(maxsize=maxsize)
        record = (queue, loop, set(event_types) if event_types is not None else None)
        with self._lock:
            self._queues.append(record)

        def close() -> None:
            with self._lock:
                try:
                    self._queues.remove(record)
                except ValueError:
                    return

        return QueueSubscription(queue, close)

    def publish(self, event: TaskEvent) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
            queues = list(self._queues)

        for handler, event_types in subscribers:
            if event_types is not None and event.type not in event_types:
                continue
            handler(event)

        for queue, loop, event_types in queues:
            if event_types is not None and event.type not in event_types:
                continue
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(queue.put_nowait, event)
            else:
                queue.put_nowait(event)
