"""mac.extensions — downstream package extension API for mac-agent.

This module lets a downstream package (e.g. mac_coffee) register
extra capabilities with mac-agent at import time, without
subclassing or forking mac-agent itself. Three registration
surfaces are supported:

* **Table DDL** — ``Extension.table_ddl``: a list of
  ``CREATE TABLE IF NOT EXISTS`` statements. mac-agent applies
  them to the SQLite file at ``apply_ddl(connection)`` time.
* **Hooks** — ``Extension.hooks``: a dict of name -> callable.
  mac-agent invokes ``call_hook(name, *args, **kwargs)`` at
  defined points; results are collected across extensions.
* **WebSocket channels** — ``Extension.ws_channels``: a list of
  ``ChannelDef(name, payload_schema)``. mac-http-server mounts
  ``/ws/{name}`` for each channel, and downstream code can call
  ``publish_to_channel(name, payload)`` to fan events out to
  all connected clients.

The extension registry is process-local and thread-safe. It is
NOT multiprocessing-safe by design: mac-agent's mac-http-server
runs in a single process per host.

Public symbols
--------------

* :class:`Extension` — the registration record
* :class:`ChannelDef` — a WebSocket channel definition
* :class:`ExtensionAlreadyRegisteredError`
* :func:`register` / :func:`unregister` / :func:`get` /
  :func:`list_extensions`
* :func:`apply_ddl`
* :func:`call_hook`
* :func:`get_channels`
* :func:`subscribe_to_channel` / :func:`publish_to_channel` /
  :class:`ChannelSubscription`
* :func:`reset` — for tests only
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import RLock
from typing import Any

from pydantic import BaseModel

_log = logging.getLogger("mac.extensions")


@dataclass(frozen=True)
class ChannelDef:
    """A WebSocket channel an extension wants mac-http-server to mount.

    ``payload_schema`` is a Pydantic model class. ``publish_to_channel``
    validates payloads against it before fanning out. ``description``
    is shown by the ``/extensions`` introspection endpoint and is
    free-form text.
    """

    name: str
    payload_schema: type[BaseModel]
    description: str = ""

    def __post_init__(self) -> None:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", self.name) is None:
            raise ValueError(
                "channel name must contain only letters, digits, '.', '-', "
                "or '_' and must start with a letter or digit"
            )
        if not isinstance(self.payload_schema, type) or not issubclass(
            self.payload_schema,
            BaseModel,
        ):
            raise TypeError("payload_schema must be a Pydantic BaseModel class")


@dataclass
class Extension:
    """A downstream package's registration record.

    All fields except ``name`` and ``version`` default to empty.
    mac-agent stores this record in a process-local registry; the
    extension's own code is responsible for calling :func:`register`
    at package import time.
    """

    name: str
    version: str
    table_ddl: list[str] = field(default_factory=list)
    hooks: dict[str, Callable[..., Any]] = field(default_factory=dict)
    ws_channels: list[ChannelDef] = field(default_factory=list)
    public_symbols: dict[str, Any] = field(default_factory=dict)


class ExtensionAlreadyRegisteredError(ValueError):
    """Raised by :func:`register` when an extension name collides with
    a different version and ``strict=True`` (the default)."""


# ---------------------------------------------------------------------------
# Extension registry
# ---------------------------------------------------------------------------

_registry: dict[str, Extension] = {}
_lock = RLock()


def register(ext: Extension, *, strict: bool = True) -> None:
    """Register an extension. Re-registering the same name replaces.

    If ``strict=True`` (default) and a different version of the
    same-named extension is already registered, raise
    :class:`ExtensionAlreadyRegisteredError` instead of replacing
    it silently.
    """
    if not ext.name:
        raise ValueError("extension name must be a non-empty string")
    with _lock:
        if strict and ext.name in _registry:
            existing = _registry[ext.name]
            if existing.version != ext.version:
                raise ExtensionAlreadyRegisteredError(
                    f"extension {ext.name!r} already registered with version "
                    f"{existing.version!r}; refusing to replace with {ext.version!r}. "
                    "Unregister first or pass strict=False."
                )
        _registry[ext.name] = ext


def unregister(name: str) -> bool:
    """Remove an extension and close its WS subscribers. Returns
    True if it was present."""
    with _lock:
        ext = _registry.pop(name, None)
    if ext is None:
        return False
    _close_channels_for_extension(ext)
    return True


def get(name: str) -> Extension | None:
    with _lock:
        return _registry.get(name)


def list_extensions() -> list[Extension]:
    with _lock:
        return list(_registry.values())


def apply_ddl(connection: sqlite3.Connection) -> int:
    """Run every registered extension's ``table_ddl`` against the
    SQLite connection. DDL statements are expected to be idempotent
    (e.g. ``CREATE TABLE IF NOT EXISTS``) because ``apply_ddl`` may
    be called multiple times. Returns the number of statements
    executed."""
    with _lock:
        statements = [
            statement
            for extension in _registry.values()
            for statement in tuple(extension.table_ddl)
        ]
    count = 0
    for statement in statements:
        connection.execute(statement)
        count += 1
    return count


def call_hook(name: str, *args: Any, **kwargs: Any) -> list[Any]:
    """Invoke the named hook on every extension that declared it.

    Returns the list of results in registration order. A hook that
    raises is logged and skipped — mac-agent continues with the
    remaining extensions.
    """
    with _lock:
        snapshot = [(ext.name, ext.hooks.get(name)) for ext in _registry.values()]
    results: list[Any] = []
    for ext_name, hook in snapshot:
        if hook is None:
            continue
        try:
            results.append(hook(*args, **kwargs))
        except Exception:
            _log.exception("extension %r raised in hook %r; continuing", ext_name, name)
    return results


def get_channels() -> dict[str, ChannelDef]:
    """Return a flat name -> ChannelDef view across all extensions.

    If two extensions declare the same channel name, the first wins
    and a warning is logged.
    """
    with _lock:
        snapshot = list(_registry.values())
    channels: dict[str, ChannelDef] = {}
    for ext in snapshot:
        for ch in ext.ws_channels:
            if ch.name in channels:
                _log.warning(
                    "channel %r declared by both %r and %r; first wins",
                    ch.name,
                    channels[ch.name].payload_schema.__class__.__name__,
                    ext.name,
                )
                continue
            channels[ch.name] = ch
    return channels


# ---------------------------------------------------------------------------
# WebSocket channel pub-sub
# ---------------------------------------------------------------------------

# Map of channel name -> list of per-subscriber asyncio queues.
# A subscriber's queue is a ChannelSubscription. We keep them in a
# list (not a set) so two subscribers from the same connection get
# fan-out (an unusual but valid use case).
_subscribers: dict[str, list[ChannelSubscription]] = {}
_subs_lock = RLock()


class ChannelSubscription:
    """A single subscriber's handle on a channel.

    Use :meth:`unsubscribe` to detach. The underlying queue is closed
    on unsubscribe; pending ``await queue.get()`` calls will raise
    :class:`asyncio.QueueShutDown`.
    """

    def __init__(self, channel: str, maxsize: int = 0) -> None:
        if maxsize < 0:
            raise ValueError("maxsize must be non-negative")
        self.channel = channel
        self._maxsize = maxsize
        self._items: deque[dict[str, Any]] = deque()
        self._state_lock = RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wakeup: asyncio.Event | None = None
        self._closed = False

    @property
    def pending_count(self) -> int:
        with self._state_lock:
            return len(self._items)

    async def wait(self) -> dict[str, Any] | None:
        """Block until the next event. Returns None when the
        subscription is closed (callers should treat None as EOF)."""
        loop = asyncio.get_running_loop()
        with self._state_lock:
            if self._loop is None:
                self._loop = loop
                self._wakeup = asyncio.Event()
            elif self._loop is not loop:
                raise RuntimeError(
                    "a channel subscription may only be awaited from one event loop"
                )

        while True:
            with self._state_lock:
                if self._items:
                    return self._items.popleft()
                if self._closed:
                    return None
                assert self._wakeup is not None
                self._wakeup.clear()
                wakeup = self._wakeup
            await wakeup.wait()

    def try_recv(self) -> dict[str, Any] | None:
        """Non-blocking receive. Returns None if no event is queued
        (and None if the subscription is closed and the queue is
        empty)."""
        with self._state_lock:
            if self._items:
                return self._items.popleft()
            return None

    def unsubscribe(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            loop = self._loop
            wakeup = self._wakeup
        with _subs_lock:
            subscribers = _subscribers.get(self.channel)
            if subscribers is not None and self in subscribers:
                subscribers.remove(self)
                if not subscribers:
                    _subscribers.pop(self.channel, None)
        if loop is not None and wakeup is not None:
            loop.call_soon_threadsafe(wakeup.set)

    def _offer(self, event: dict[str, Any]) -> bool:
        with self._state_lock:
            if self._closed:
                return False
            if self._maxsize and len(self._items) >= self._maxsize:
                return False
            self._items.append(event)
            loop = self._loop
            wakeup = self._wakeup
        if loop is not None and wakeup is not None:
            loop.call_soon_threadsafe(wakeup.set)
        return True


def subscribe_to_channel(channel: str, *, maxsize: int = 0) -> ChannelSubscription:
    """Attach a new subscriber to ``channel`` and return its handle.

    The caller is responsible for calling :meth:`ChannelSubscription.unsubscribe`
    when done.
    """
    if channel not in get_channels():
        raise KeyError(f"channel {channel!r} is not registered")
    sub = ChannelSubscription(channel=channel, maxsize=maxsize)
    with _subs_lock:
        _subscribers.setdefault(channel, []).append(sub)
    return sub


def publish_to_channel(channel: str, payload: dict[str, Any]) -> int:
    """Validate ``payload`` against the channel's schema and fan it
    out to all current subscribers. Returns the number of subscribers
    that received the event.

    If no schema is registered for ``channel``, the payload is
    forwarded as-is. If the payload fails schema validation, the
    call is rejected with :class:`pydantic.ValidationError` and
    nothing is published.
    """
    channel_def = get_channels().get(channel)
    if channel_def is None:
        raise KeyError(f"channel {channel!r} is not registered")
    channel_def.payload_schema.model_validate(payload)
    with _subs_lock:
        subscribers = list(_subscribers.get(channel, ()))
    delivered = 0
    for sub in subscribers:
        if sub._offer({"channel": channel, "payload": payload}):
            delivered += 1
        else:
            _log.warning("subscriber queue full on channel %r; dropping event", channel)
    return delivered


def _close_channels_for_extension(ext: Extension) -> None:
    """Close every subscriber of every channel declared by ``ext``."""
    with _subs_lock:
        for ch in ext.ws_channels:
            for sub in list(_subscribers.get(ch.name, ())):
                sub.unsubscribe()


# ---------------------------------------------------------------------------
# Test helper
# ---------------------------------------------------------------------------


def reset() -> None:
    """Clear the extension registry and close all subscribers.
    Intended for tests; do NOT call in production code."""
    with _lock:
        exts = list(_registry.values())
        _registry.clear()
    with _subs_lock:
        subscriptions = [
            subscription
            for subscribers in list(_subscribers.values())
            for subscription in list(subscribers)
        ]
        _subscribers.clear()
    for subscription in subscriptions:
        subscription.unsubscribe()
    for ext in exts:
        _close_channels_for_extension(ext)


__all__ = [
    "ChannelDef",
    "ChannelSubscription",
    "Extension",
    "ExtensionAlreadyRegisteredError",
    "apply_ddl",
    "call_hook",
    "get",
    "get_channels",
    "list_extensions",
    "publish_to_channel",
    "register",
    "reset",
    "subscribe_to_channel",
    "unregister",
]
