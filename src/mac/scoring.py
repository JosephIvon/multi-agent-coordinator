"""Pluggable scoring for ``Registry.list_ready_tasks``.

This module lets user code (including LLM-assisted planners) override the
default ordering of ``proposed`` tasks returned by
:meth:`mac.registry.Registry.list_ready_tasks` without forking MAC.

A scorer is any callable ``fn(task: TaskTransfer) -> float`` where higher
values mean "claim this one first". The :data:`priority_scorer` builtin
just forwards ``task.priority`` (1-10) and is registered automatically as
the ``"priority"`` named scorer.

Usage:

    from mac.scoring import register_scorer, priority_scorer

    # override the default
    def load_aware_scorer(task):
        return task.priority - (task.retry_count * 0.1)

    register_scorer("load_aware", load_aware_scorer)

    from mac.registry import Registry
    registry = Registry(ledger, scoring_fn="load_aware")
"""

from __future__ import annotations

from collections.abc import Callable
from threading import RLock

from mac.protocol.messages import TaskTransfer

# Public type alias — a scoring function maps a task to a numeric score,
# higher means "more urgent / higher priority". Must be a total order;
# returning ``None`` is treated as 0.0 so partially-informative scorers
# never break ``sorted()``.
ScoringFn = Callable[[TaskTransfer], float]

_NAMED_SCORERS: dict[str, ScoringFn] = {}
_lock = RLock()


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
    """Register (or replace) a named scorer.

    Raises ``ValueError`` if ``name`` is empty or ``fn`` is not callable.
    """
    if not name:
        raise ValueError("scorer name must be a non-empty string")
    if not callable(fn):
        raise ValueError(f"scorer '{name}' must be callable")
    with _lock:
        _NAMED_SCORERS[name] = fn


def unregister_scorer(name: str) -> bool:
    """Remove a named scorer. Returns True if it existed, else False."""
    with _lock:
        return _NAMED_SCORERS.pop(name, None) is not None


def get_scorer(name: str) -> ScoringFn | None:
    """Return the scorer registered under ``name`` or ``None``."""
    with _lock:
        return _NAMED_SCORERS.get(name)


def list_scorers() -> dict[str, ScoringFn]:
    """Return a shallow copy of the currently registered scorers."""
    with _lock:
        return dict(_NAMED_SCORERS)


def clear_scorers() -> None:
    """Remove all registered scorers (including the built-in ``priority``)."""
    with _lock:
        _NAMED_SCORERS.clear()


# Auto-register the built-in priority scorer at import time. Guarded so
# concurrent module-import races don't double-register.
with _lock:
    _NAMED_SCORERS.setdefault("priority", priority_scorer)


__all__ = [
    "ScoringFn",
    "priority_scorer",
    "register_scorer",
    "unregister_scorer",
    "get_scorer",
    "list_scorers",
    "clear_scorers",
]
