"""Pluggable scoring for ``Registry.list_ready_tasks`` / ``alist_ready_tasks``.

This module lets user code (including LLM-assisted planners) override the
default ordering of ``proposed`` tasks returned by
:meth:`mac.registry.Registry.list_ready_tasks` (sync) or
:meth:`mac.registry.Registry.alist_ready_tasks` (async) without forking MAC.

A scorer is any callable ``fn(task: TaskTransfer) -> float`` where higher
values mean "claim this one first". The :data:`priority_scorer` builtin
just forwards ``task.priority`` (1-10) and is registered automatically as
the named scorer ``"priority"``.

Two flavours:

* Sync  (Round 14, :data:`ScoringFn`) - ``def fn(task) -> float``.
  Cheap path; the registry uses ``sorted()`` directly.
* Async (Round 15, :data:`AsyncScoringFn`) - ``async def fn(task) -> float``
  with an awaitable return value. Required when the scorer talks to an
  LLM, RPC, etc. Use :meth:`mac.registry.Registry.alist_ready_tasks`;
  the sync ``list_ready_tasks`` raises :class:`TypeError` if it sees an
  async scorer so misuse surfaces immediately.

Sync usage::

    from mac.scoring import register_scorer

    def load_aware_scorer(task):
        return task.priority - (task.retry_count * 0.1)

    register_scorer("load_aware", load_aware_scorer)

    from mac.registry import Registry
    registry = Registry(ledger, scoring_fn="load_aware")
    registry.list_ready_tasks()  # sorted by load_aware_scorer

Async usage::

    from mac.scoring import register_async_scorer

    async def llm_scorer(task):
        return await openai_score(task.description)

    register_async_scorer("llm", llm_scorer)

    registry = Registry(ledger, scoring_fn="llm")
    tasks = await registry.alist_ready_tasks()

Lookup across both registries is available via :func:`resolve_scorer`.
The two registries stay separate so the sync hot path remains free of
any asyncio branching.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from inspect import iscoroutinefunction
from threading import RLock

from mac.protocol.messages import TaskTransfer

# Public type aliases - a scoring function maps a task to a numeric score,
# higher means "more urgent / higher priority". Must be a total order;
# returning ``None`` is treated as 0.0 so partially-informative scorers
# never break ``sorted()``.
#
# Async scorers are usable only through ``Registry.alist_ready_tasks``,
# which awaits each invocation. Keeping the two registries separate means
# the sync surface area stays small and fast (sorted() with no asyncio
# overhead) while still giving LLM-backed scorers a first-class path.
ScoringFn = Callable[[TaskTransfer], float]
AsyncScoringFn = Callable[[TaskTransfer], Awaitable[float]]

_NAMED_SCORERS: dict[str, ScoringFn] = {}
_NAMED_ASYNC_SCORERS: dict[str, AsyncScoringFn] = {}
_lock = RLock()


def is_async_scorer(fn) -> bool:
    """Return ``True`` if ``fn`` is a coroutine function.

    Used by :class:`mac.registry.Registry` to decide whether the sync
    ``list_ready_tasks`` should fall through to the natural SQL ordering
    (and surface a ``TypeError``) or whether ``alist_ready_tasks`` should
    drive the scorer under asyncio.
    """
    return callable(fn) and iscoroutinefunction(fn)


def priority_scorer(task: TaskTransfer) -> float:
    """Use ``task.priority`` directly as the score.

    Higher task priority (1-10, default 5) wins. Returned as ``float`` so the
    caller can blend with fractional weights. Defensive against missing or
    bogus values: any non-int ``priority`` attribute falls back to 5.
    """
    value = getattr(task, "priority", 5)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 5.0


def register_scorer(name: str, fn: ScoringFn) -> None:
    """Register (or replace) a named sync scorer.

    Raises ``ValueError`` if ``name`` is empty or ``fn`` is not callable.
    Use :func:`register_async_scorer` for coroutine functions instead.
    """
    if not name:
        raise ValueError("scorer name must be a non-empty string")
    if not callable(fn):
        raise ValueError(f"scorer '{name}' must be callable")
    with _lock:
        _NAMED_SCORERS[name] = fn


def unregister_scorer(name: str) -> bool:
    """Remove a named sync scorer. Returns True if it existed, else False."""
    with _lock:
        return _NAMED_SCORERS.pop(name, None) is not None


def get_scorer(name: str) -> ScoringFn | None:
    """Return the sync scorer registered under ``name`` or ``None``."""
    with _lock:
        return _NAMED_SCORERS.get(name)


def list_scorers() -> dict[str, ScoringFn]:
    """Return a shallow copy of the currently registered sync scorers."""
    with _lock:
        return dict(_NAMED_SCORERS)


def clear_scorers() -> None:
    """Remove all registered sync scorers (including the built-in ``priority``)."""
    with _lock:
        _NAMED_SCORERS.clear()


# --- Async scorer registry --------------------------------------------------
#
# Kept separate from the sync registry so that the lookup hotspots stay
# branch-free and the sync path never has to inspect asyncio state.
# Built-ins (e.g. ``priority_scorer``) are intentionally sync; LLM-driven
# policies register their async variants here.


def register_async_scorer(name: str, fn: AsyncScoringFn) -> None:
    """Register (or replace) a named async scorer.

    Raises ``ValueError`` if ``name`` is empty or ``fn`` is not a coroutine
    function (use :func:`register_scorer` for plain callables).
    """
    if not name:
        raise ValueError("scorer name must be a non-empty string")
    if not iscoroutinefunction(fn):
        raise ValueError(
            f"async scorer '{name}' must be a coroutine function (async def)"
        )
    with _lock:
        _NAMED_ASYNC_SCORERS[name] = fn


def unregister_async_scorer(name: str) -> bool:
    """Remove a named async scorer. Returns True if it existed, else False."""
    with _lock:
        return _NAMED_ASYNC_SCORERS.pop(name, None) is not None


def get_async_scorer(name: str) -> AsyncScoringFn | None:
    """Return the async scorer registered under ``name`` or ``None``."""
    with _lock:
        return _NAMED_ASYNC_SCORERS.get(name)


def list_async_scorers() -> dict[str, AsyncScoringFn]:
    """Return a shallow copy of the currently registered async scorers."""
    with _lock:
        return dict(_NAMED_ASYNC_SCORERS)


def clear_async_scorers() -> None:
    """Remove all registered async scorers (no built-ins to preserve)."""
    with _lock:
        _NAMED_ASYNC_SCORERS.clear()


def resolve_scorer(name: str) -> Callable[[TaskTransfer], object] | None:
    """Look ``name`` up in either registry; sync side wins on conflict.

    Returns the first match found, or ``None`` if the name is unknown.
    The return type is intentionally broad because callers may receive
    either a sync or an async callable; use :func:`is_async_scorer` to
    discriminate. Sync scorers take precedence in the (unlikely) case
    someone registers the same name in both places.
    """
    with _lock:
        sync = _NAMED_SCORERS.get(name)
        if sync is not None:
            return sync
        return _NAMED_ASYNC_SCORERS.get(name)


# Auto-register the built-in priority scorer at import time. Guarded so
# concurrent module-import races don't double-register.
with _lock:
    _NAMED_SCORERS.setdefault("priority", priority_scorer)


__all__ = [
    "AsyncScoringFn",
    "ScoringFn",
    "clear_async_scorers",
    "clear_scorers",
    "get_async_scorer",
    "get_scorer",
    "is_async_scorer",
    "list_async_scorers",
    "list_scorers",
    "priority_scorer",
    "register_async_scorer",
    "register_scorer",
    "resolve_scorer",
    "unregister_async_scorer",
    "unregister_scorer",
]
