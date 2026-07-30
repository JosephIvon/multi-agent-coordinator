"""Tests for mac.scoring + Registry scoring hook (Round 14)."""
from __future__ import annotations

import math

import pytest

import mac.scoring as scoring
from mac.protocol.messages import TaskTransfer
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger

# --- fixtures ----------------------------------------------------------------


def _make_task(task_id: str, *, priority: int = 5, project_context: str | None = "demo") -> TaskTransfer:
    return TaskTransfer(
        task_id=task_id,
        title=f"task {task_id}",
        description="scoring hook test",
        priority=priority,
        project_context=project_context,
    )


def _seed_two_tasks(ledger: SQLiteTaskLedger) -> tuple[str, str]:
    registry = Registry(ledger)
    registry.submit_task(_make_task("T-A", priority=1))
    registry.submit_task(_make_task("T-B", priority=10))
    return "T-A", "T-B"


@pytest.fixture
def ledger(tmp_path):
    return SQLiteTaskLedger(tmp_path / "mac.db")


@pytest.fixture(autouse=True)
def _restore_scorer_registry():
    """Snapshot the named-scoring registry and restore it after each test.

    Several tests call :func:`register_scorer` or :func:`clear_scorers`; we
    do not want leaks across cases (or across the wider pytest suite).
    """
    snapshot = scoring.list_scorers()
    try:
        yield
    finally:
        scoring.clear_scorers()
        for name, fn in snapshot.items():
            scoring.register_scorer(name, fn)


# --- scoring module lifecycle ------------------------------------------------


def test_scoring_builtin_priority_registered() -> None:
    assert scoring.list_scorers()["priority"] is scoring.priority_scorer


def test_scoring_register_unregister_lifecycle() -> None:
    scoring.register_scorer("custom", lambda t: 1.0)
    assert scoring.get_scorer("custom") is not None
    assert scoring.unregister_scorer("custom") is True
    assert scoring.get_scorer("custom") is None
    # Unregistering something absent returns False.
    assert scoring.unregister_scorer("missing") is False


def test_scoring_register_rejects_empty_name() -> None:
    with pytest.raises(ValueError):
        scoring.register_scorer("", lambda t: 1.0)


def test_scoring_register_rejects_non_callable() -> None:
    with pytest.raises(ValueError):
        scoring.register_scorer("bad", "not-callable")  # type: ignore[arg-type]


def test_scoring_list_scorers_returns_copy() -> None:
    snapshot = scoring.list_scorers()
    snapshot["leaked"] = lambda t: 0.0
    assert "leaked" not in scoring.list_scorers()


# --- Registry wiring ----------------------------------------------------------


def test_scoring_default_unchanged(ledger: SQLiteTaskLedger) -> None:
    """Without a scorer, list_ready_tasks preserves the SQL natural order."""
    _seed_two_tasks(ledger)
    registry = Registry(ledger)  # no scoring_fn
    ready = registry.list_ready_tasks(project_context="demo")
    # Default ORDER BY updated_at ASC, task_id ASC => ["T-A", "T-B"]
    assert [t.task_id for t in ready] == ["T-A", "T-B"]


def test_scoring_callable_orders_by_priority(ledger: SQLiteTaskLedger) -> None:
    """A callable scorer reorders ready tasks by score (descending)."""
    _seed_two_tasks(ledger)
    registry = Registry(ledger, scoring_fn=scoring.priority_scorer)
    ready = registry.list_ready_tasks(project_context="demo")
    assert [t.task_id for t in ready] == ["T-B", "T-A"]


def test_scoring_named_lookup_via_constructor(ledger: SQLiteTaskLedger) -> None:
    """scoring_fn='priority' resolves to the built-in scorer."""
    registry = Registry(ledger, scoring_fn="priority")
    assert registry._scoring_fn is scoring.priority_scorer


def test_scoring_unknown_name_raises_value_error(ledger: SQLiteTaskLedger) -> None:
    with pytest.raises(ValueError, match="unknown scoring_fn name 'bogus'"):
        Registry(ledger, scoring_fn="bogus")


def test_scoring_invalid_type_raises_value_error(ledger: SQLiteTaskLedger) -> None:
    with pytest.raises(ValueError, match="must be callable"):
        Registry(ledger, scoring_fn=123)  # type: ignore[arg-type]


def test_scoring_set_scoring_fn_swaps_at_runtime(ledger: SQLiteTaskLedger) -> None:
    """set_scoring_fn() can install and clear the hook after construction."""
    _seed_two_tasks(ledger)
    registry = Registry(ledger)
    # Default order.
    assert [t.task_id for t in registry.list_ready_tasks(project_context="demo")] == ["T-A", "T-B"]
    # Swap to priority_scorer.
    registry.set_scoring_fn(scoring.priority_scorer)
    assert [t.task_id for t in registry.list_ready_tasks(project_context="demo")] == ["T-B", "T-A"]
    # Clear back to default.
    registry.set_scoring_fn(None)
    assert [t.task_id for t in registry.list_ready_tasks(project_context="demo")] == ["T-A", "T-B"]
    # set_scoring_fn also accepts a string name.
    registry.set_scoring_fn("priority")
    assert registry._scoring_fn is scoring.priority_scorer
    # And a string with no matching scorer raises.
    with pytest.raises(ValueError):
        registry.set_scoring_fn("nope")


# --- scorer safety / extensibility -------------------------------------------


def test_scoring_callable_returning_none_doesnt_crash(ledger: SQLiteTaskLedger) -> None:
    """A scorer that returns None must not break sorted() — Registry keeps working."""
    _seed_two_tasks(ledger)
    scoring.register_scorer("none_aware", lambda task: None)
    registry = Registry(ledger, scoring_fn="none_aware")
    # _safe_score collapses None to 0.0, so the natural order survives.
    ready = registry.list_ready_tasks(project_context="demo")
    assert [t.task_id for t in ready] == ["T-A", "T-B"]


def test_scoring_callable_returning_nan_doesnt_crash(ledger: SQLiteTaskLedger) -> None:
    """NaN is sanitised to 0.0 so CPython's sort invariant is preserved."""
    _seed_two_tasks(ledger)
    scoring.register_scorer("nan_aware", lambda task: float("nan"))
    registry = Registry(ledger, scoring_fn="nan_aware")
    ready = registry.list_ready_tasks(project_context="demo")
    assert [t.task_id for t in ready] == ["T-A", "T-B"]


def test_scoring_callable_returning_non_numeric_doesnt_crash(ledger: SQLiteTaskLedger) -> None:
    """A scorer that returns a non-numeric value falls back to 0.0."""
    _seed_two_tasks(ledger)
    scoring.register_scorer("string_aware", lambda task: "not-a-number")
    registry = Registry(ledger, scoring_fn="string_aware")
    # Should not raise; order is the SQL natural order (all scores = 0.0).
    ready = registry.list_ready_tasks(project_context="demo")
    assert [t.task_id for t in ready] == ["T-A", "T-B"]


def test_scoring_callable_raising_doesnt_break_list(ledger: SQLiteTaskLedger) -> None:
    """A scorer that raises on one task still yields a usable list."""

    def flaky(task: TaskTransfer) -> float:
        if task.task_id == "T-A":
            raise RuntimeError("intentional scorer explosion")
        return 1.0

    _seed_two_tasks(ledger)
    scoring.register_scorer("flaky", flaky)
    registry = Registry(ledger, scoring_fn="flaky")
    ready = registry.list_ready_tasks(project_context="demo")
    # T-A scored as 0.0 (exception), T-B as 1.0 => T-B first.
    assert [t.task_id for t in ready] == ["T-B", "T-A"]


def test_scoring_does_not_affect_claim_next_task(ledger: SQLiteTaskLedger) -> None:
    """claim_next_task queries the ledger directly; the hook must not reorder it.

    Even with ``scoring_fn=priority_scorer`` installed, the default
    non-best-effort claim path walks the ledger's natural
    ``updated_at ASC, task_id ASC`` ordering — so the lower priority
    task is claimed first, *opposite* of what ``list_ready_tasks`` would
    return with the same hook.
    """
    a_id, b_id = _seed_two_tasks(ledger)
    registry = Registry(ledger, scoring_fn=scoring.priority_scorer)
    claimed = registry.claim_next_task(agent_id="agent-1", capability="custom")
    assert claimed is not None
    assert claimed.task_id == a_id
    claimed2 = registry.claim_next_task(agent_id="agent-1", capability="custom")
    assert claimed2 is not None
    assert claimed2.task_id == b_id


def test_scoring_priority_scorer_is_defensive_on_bad_priority(ledger: SQLiteTaskLedger) -> None:
    """priority_scorer falls back to 5.0 for tasks missing/odd priority values."""
    task = _make_task("weird", priority=8)

    class _T:
        pass

    # priority_scorer only reads task.priority; this exercises the no-attribute branch.
    bad_task = _T()  # no priority attribute
    assert math.isfinite(scoring.priority_scorer(task))
    assert scoring.priority_scorer(bad_task) == 5.0
