"""Tests for mac.scoring + Registry scoring hook (Round 14)."""
from __future__ import annotations

import asyncio
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

# --- Round 15: async scorer + LRU+TTL cache ---------------------------------


def test_scoring_resolve_scorer_finds_async_registry(ledger):
    scoring.register_async_scorer("async_demo", _async_priority_factory())
    found = scoring.resolve_scorer("async_demo")
    assert found is not None
    assert scoring.is_async_scorer(found)


def test_scoring_register_async_rejects_sync_callable(ledger):
    with pytest.raises(ValueError, match="coroutine function"):
        scoring.register_async_scorer("bad_sync_as_async", lambda t: 1.0)


def test_scoring_register_async_rejects_empty_name(ledger):
    with pytest.raises(ValueError):
        scoring.register_async_scorer("", _async_priority_factory())


def test_scoring_list_async_scorers_returns_copy(ledger):
    scoring.register_async_scorer("tmp1", _async_priority_factory())
    snapshot = scoring.list_async_scorers()
    snapshot["leaked"] = _async_priority_factory()
    assert "leaked" not in scoring.list_async_scorers()


def test_scoring_clear_async_scorers_removes_only_async(ledger):
    scoring.register_async_scorer("x", _async_priority_factory())
    assert "priority" in scoring.list_scorers()
    scoring.clear_async_scorers()
    assert scoring.list_async_scorers() == {}
    assert "priority" in scoring.list_scorers()  # builtin survives


def test_scoring_clear_scorers_preserves_async_registry(ledger):
    scoring.register_async_scorer("x", _async_priority_factory())
    scoring.clear_scorers()
    assert scoring.list_scorers() == {}
    assert "x" in scoring.list_async_scorers()


def test_alist_ready_orders_by_async_scorer(ledger):
    _seed_two_tasks(ledger)
    registry = Registry(ledger, scoring_fn=_async_priority_factory())
    ready = asyncio.run(registry.alist_ready_tasks(project_context="demo"))
    assert [t.task_id for t in ready] == ["T-B", "T-A"]


def test_alist_ready_without_scorer_returns_natural_order(ledger):
    _seed_two_tasks(ledger)
    registry = Registry(ledger)
    ready = asyncio.run(registry.alist_ready_tasks(project_context="demo"))
    assert [t.task_id for t in ready] == ["T-A", "T-B"]


def test_alist_ready_via_string_name_resolves_async(ledger):
    _seed_two_tasks(ledger)
    scoring.register_async_scorer("async_priority", _async_priority_factory())
    registry = Registry(ledger, scoring_fn="async_priority")
    assert registry._async_scoring_fn is not None
    assert registry._scoring_fn is None
    ready = asyncio.run(registry.alist_ready_tasks(project_context="demo"))
    assert [t.task_id for t in ready] == ["T-B", "T-A"]


def test_list_ready_tasks_raises_typeerror_for_async_scorer(ledger):
    _seed_two_tasks(ledger)
    registry = Registry(ledger, scoring_fn=_async_priority_factory())
    with pytest.raises(TypeError, match="alist_ready_tasks"):
        registry.list_ready_tasks(project_context="demo")


def test_alist_ready_cache_hits_skip_scorer_invocations(ledger):
    _seed_two_tasks(ledger)
    counter = {"calls": 0}

    async def counting_scorer(task):
        counter["calls"] += 1
        return float(task.priority)

    registry = Registry(ledger, scoring_fn=counting_scorer)
    asyncio.run(registry.alist_ready_tasks(project_context="demo"))
    info1 = registry.scoring_cache_info()
    assert counter["calls"] == 2
    assert info1.misses == 2
    assert info1.hits == 0

    # Second call - everything should come from cache.
    asyncio.run(registry.alist_ready_tasks(project_context="demo"))
    info2 = registry.scoring_cache_info()
    assert counter["calls"] == 2
    assert info2.misses == 2
    assert info2.hits == 2
    assert info2.currsize == 2


def test_alist_ready_cache_respects_ttl(ledger, monkeypatch):
    _seed_two_tasks(ledger)
    counter = {"calls": 0}

    async def counting_scorer(task):
        counter["calls"] += 1
        return float(task.priority)

    fake_now = {"t": 1000.0}
    monkeypatch.setattr("mac.registry.time.time", lambda: fake_now["t"])
    registry = Registry(
        ledger, scoring_fn=counting_scorer, scoring_cache_ttl_seconds=5.0
    )
    asyncio.run(registry.alist_ready_tasks(project_context="demo"))
    assert counter["calls"] == 2
    fake_now["t"] += 10.0
    asyncio.run(registry.alist_ready_tasks(project_context="demo"))
    assert counter["calls"] == 4


def test_alist_ready_cache_invalidated_by_set_scoring_fn(ledger):
    _seed_two_tasks(ledger)
    counter1 = {"calls": 0}
    counter2 = {"calls": 0}

    async def scorer_a(task):
        counter1["calls"] += 1
        return 1.0

    async def scorer_b(task):
        counter2["calls"] += 1
        return 10.0

    registry = Registry(ledger, scoring_fn=scorer_a)
    asyncio.run(registry.alist_ready_tasks(project_context="demo"))
    assert counter1["calls"] == 2
    assert counter2["calls"] == 0

    registry.set_scoring_fn(scorer_b)
    info = registry.scoring_cache_info()
    assert info.currsize == 0
    assert info.hits == 0
    asyncio.run(registry.alist_ready_tasks(project_context="demo"))
    assert counter2["calls"] == 2


def test_alist_ready_handles_scorer_returning_nan(ledger):
    _seed_two_tasks(ledger)

    async def nan_scorer(task):
        return float("nan")

    registry = Registry(ledger, scoring_fn=nan_scorer)
    ready = asyncio.run(registry.alist_ready_tasks(project_context="demo"))
    assert [t.task_id for t in ready] == ["T-A", "T-B"]


def test_alist_ready_handles_scorer_raising(ledger):
    _seed_two_tasks(ledger)

    async def flaky(task):
        if task.task_id == "T-A":
            raise RuntimeError("intentional scorer explosion")
        return 5.0

    registry = Registry(ledger, scoring_fn=flaky)
    ready = asyncio.run(registry.alist_ready_tasks(project_context="demo"))
    assert [t.task_id for t in ready] == ["T-B", "T-A"]


def test_alist_ready_handles_scorer_returning_none(ledger):
    _seed_two_tasks(ledger)

    async def none_scorer(task):
        return None

    registry = Registry(ledger, scoring_fn=none_scorer)
    ready = asyncio.run(registry.alist_ready_tasks(project_context="demo"))
    assert [t.task_id for t in ready] == ["T-A", "T-B"]


def test_set_scoring_fn_with_string_resolves_across_registries(ledger):
    scoring.register_async_scorer("async_demo", _async_priority_factory())
    registry = Registry(ledger, scoring_fn="async_demo")
    assert registry._async_scoring_fn is not None
    assert registry._scoring_fn is None


def test_set_scoring_fn_clears_cache_on_swap(ledger):
    _seed_two_tasks(ledger)
    counter = {"calls": 0}

    async def scorer_a(task):
        counter["calls"] += 1
        return 1.0

    async def scorer_b(task):
        return 2.0

    registry = Registry(ledger, scoring_fn=scorer_a)
    import asyncio as _aio
    _aio.run(registry.alist_ready_tasks(project_context="demo"))
    info = registry.scoring_cache_info()
    assert info.currsize > 0
    registry.set_scoring_fn(scorer_b)
    assert registry.scoring_cache_info().currsize == 0


def test_clear_scoring_cache_resets_counters(ledger):
    registry = Registry(ledger)
    registry._scoring_cache["foo::bar"] = (1.0, 0.0)
    registry._scoring_cache_hits = 5
    registry._scoring_cache_misses = 3
    registry.clear_scoring_cache()
    info = registry.scoring_cache_info()
    assert info.hits == 0
    assert info.misses == 0
    assert info.currsize == 0


def test_scoring_cache_ttl_zero_disables_expiry(ledger, monkeypatch):
    _seed_two_tasks(ledger)
    counter = {"calls": 0}

    async def scorer(task):
        counter["calls"] += 1
        return float(task.priority)

    fake_now = {"t": 0.0}
    monkeypatch.setattr("mac.registry.time.time", lambda: fake_now["t"])

    registry = Registry(ledger, scoring_fn=scorer, scoring_cache_ttl_seconds=0.0)
    import asyncio as _aio
    _aio.run(registry.alist_ready_tasks(project_context="demo"))
    # Advance far past any reasonable ttl - 0 disables expiry entirely.
    fake_now["t"] += 3600.0
    _aio.run(registry.alist_ready_tasks(project_context="demo"))
    assert counter["calls"] == 2


def _async_priority_factory():
    async def _fn(task):
        return float(task.priority)
    return _fn


# ---------------------------------------------------------------------------
# Edge cases: NaN, non-float, async exceptions, cache eviction, zero config
# ---------------------------------------------------------------------------


class TestSafeScoreEdgeCases:
    """Verify _safe_score in registry.py protects sorted() from pathological scorers."""

    def test_scorer_returning_nan_returns_zero(self, ledger: SQLiteTaskLedger) -> None:
        """NaNfrom a scorer must not break sorted()."""
        def nan_scorer(task):
            return float('nan')

        registry = Registry(ledger, scoring_fn=nan_scorer)
        registry.submit_task(_make_task("T1", priority=5))
        registry.submit_task(_make_task("T2", priority=3))

        tasks = registry.list_ready_tasks()
        assert len(tasks) == 2

    def test_scorer_returning_none_returns_zero(self, ledger: SQLiteTaskLedger) -> None:
        """None from a scorer is treated as 0.0."""
        def none_scorer(task):
            return None

        registry = Registry(ledger, scoring_fn=none_scorer)
        registry.submit_task(_make_task("T1"))
        tasks = registry.list_ready_tasks()
        assert len(tasks) == 1

    def test_scorer_returning_string_returns_zero(self, ledger: SQLiteTaskLedger) -> None:
        """Non-float type (string) from scorer → 0.0 (no crash)."""
        def str_scorer(task):
            return "high"

        registry = Registry(ledger, scoring_fn=str_scorer)
        registry.submit_task(_make_task("T1"))
        tasks = registry.list_ready_tasks()
        assert len(tasks) == 1

    def test_scorer_returning_list_returns_zero(self, ledger: SQLiteTaskLedger) -> None:
        """Non-numeric type (list) from scorer → 0.0."""
        def list_scorer(task):
            return [1, 2, 3]

        registry = Registry(ledger, scoring_fn=list_scorer)
        registry.submit_task(_make_task("T1"))
        tasks = registry.list_ready_tasks()
        assert len(tasks) == 1

    def test_scorer_raising_exception_returns_zero(self, ledger: SQLiteTaskLedger) -> None:
        """Scorer that raises → 0.0, sorted() still works."""
        def boom_scorer(task):
            raise RuntimeError("scorer crashed")

        registry = Registry(ledger, scoring_fn=boom_scorer)
        registry.submit_task(_make_task("T1", priority=5))
        registry.submit_task(_make_task("T2", priority=3))
        tasks = registry.list_ready_tasks()
        assert len(tasks) == 2


class TestAsyncScorerEdgeCases:
    """Edge cases for async scoring path."""

    def test_async_scorer_raising_exception_does_not_crash(self, ledger: SQLiteTaskLedger) -> None:
        """Async scorer that raises → scores fall back to 0.0 gracefully."""
        async def boom_async(task):
            raise RuntimeError("async scorer crash")

        registry = Registry(ledger, scoring_fn=boom_async)
        _seed_two_tasks(ledger)
        import asyncio
        tasks = asyncio.run(registry.alist_ready_tasks())
        assert len(tasks) == 2

    def test_async_scorer_returning_none(self, ledger: SQLiteTaskLedger) -> None:
        """Async scorer that returns None → 0.0."""
        async def none_async(task):
            return None

        registry = Registry(ledger, scoring_fn=none_async)
        _seed_two_tasks(ledger)
        import asyncio
        tasks = asyncio.run(registry.alist_ready_tasks())
        assert len(tasks) == 2


class TestScoringCacheEdgeCases:
    """Cache eviction, zero-TTL, zero-maxsize scenarios."""

    def test_cache_eviction_when_over_maxsize(self, ledger: SQLiteTaskLedger, monkeypatch) -> None:
        """When maxsize is exceeded, oldest entries are evicted (FIFO)."""
        counter = {"calls": 0}

        def counting_scorer(task):
            counter["calls"] += 1
            return float(task.priority)

        registry = Registry(ledger, scoring_fn=counting_scorer, scoring_cache_maxsize=2, scoring_cache_ttl_seconds=99999.0)
        # Submit 3 distinct tasks with unique priorities so cache grows (priority >= 1)
        for i in range(1, 4):
            registry.submit_task(_make_task(f"T{i}", priority=i))

        import asyncio as _aio
        _aio.run(registry.alist_ready_tasks(project_context="demo"))
        first_pass = counter["calls"]
        # Second pass: oldest entry should be evicted if cache is full,
        # so at least that entry gets re-scored.
        _aio.run(registry.alist_ready_tasks(project_context="demo"))
        second_pass = counter["calls"]
        # With maxsize=2 and 3 tasks, at least one eviction happened
        assert second_pass > first_pass, f"Expected eviction: first={first_pass}, second={second_pass}"

    def test_cache_ttl_zero_disables_ttl_expiry(self, ledger: SQLiteTaskLedger, monkeypatch) -> None:
        """scoring_cache_ttl_seconds=0 → infinite TTL, entries never expire."""
        counter = {"calls": 0}

        def counting_scorer(task):
            counter["calls"] += 1
            return float(task.priority)

        fake_now = {"t": 0.0}
        monkeypatch.setattr("mac.registry.time.time", lambda: fake_now["t"])

        registry = Registry(ledger, scoring_fn=counting_scorer, scoring_cache_ttl_seconds=0.0)
        _seed_two_tasks(ledger)
        import asyncio as _aio
        _aio.run(registry.alist_ready_tasks(project_context="demo"))
        fake_now["t"] += 99999.0  # Advance far past any reasonable TTL
        _aio.run(registry.alist_ready_tasks(project_context="demo"))
        # With TTL=0, all entries persist; no re-scoring needed
        assert counter["calls"] == 2

    def test_cache_maxsize_zero_disables_cache(self, ledger: SQLiteTaskLedger) -> None:
        """scoring_cache_maxsize=0 → caching disabled, every call re-scores."""
        counter = {"calls": 0}

        def counting_scorer(task):
            counter["calls"] += 1
            return float(task.priority)

        registry = Registry(ledger, scoring_fn=counting_scorer, scoring_cache_maxsize=0)
        _seed_two_tasks(ledger)
        import asyncio as _aio
        _aio.run(registry.alist_ready_tasks(project_context="demo"))
        first = counter["calls"]
        _aio.run(registry.alist_ready_tasks(project_context="demo"))
        second = counter["calls"]
        # With maxsize=0, nothing is cached; all tasks re-scored every call
        assert second > first, f"Expected re-scoring with maxsize=0: first={first}, second={second}"

    def test_cache_info_reflects_usage(self, ledger: SQLiteTaskLedger) -> None:
        """scoring_cache_info() reports hits, misses, and configuration."""
        def prio_scorer(task):
            return float(task.priority)

        registry = Registry(ledger, scoring_fn=prio_scorer, scoring_cache_maxsize=64, scoring_cache_ttl_seconds=60.0)
        _seed_two_tasks(ledger)
        import asyncio as _aio
        _aio.run(registry.alist_ready_tasks(project_context="demo"))
        # Second pass: both should be cache hits
        _aio.run(registry.alist_ready_tasks(project_context="demo"))

        info = registry.scoring_cache_info()
        assert info.maxsize == 64
        assert info.ttl_seconds == 60.0
        assert info.misses == 2  # first pass: 2 misses
        assert info.hits == 2   # second pass: 2 hits
        assert info.currsize == 2

    def test_clear_scoring_cache_zeros_stats(self, ledger: SQLiteTaskLedger) -> None:
        """clear_scoring_cache() resets hits, misses, and evicts all entries."""
        registry = Registry(ledger)
        _seed_two_tasks(ledger)
        import asyncio as _aio
        _aio.run(registry.alist_ready_tasks(project_context="demo"))

        registry.clear_scoring_cache()
        info = registry.scoring_cache_info()
        assert info.hits == 0
        assert info.misses == 0
        assert info.currsize == 0


class TestRoleParamOnAlistReadyTasks:
    """alist_ready_tasks() role filtering (v1.2.0 fix)."""

    def test_role_filter_blocks_non_matching(self, ledger: SQLiteTaskLedger) -> None:
        """Tasks with required_role != filter role are excluded."""
        registry = Registry(ledger)
        import asyncio as _aio

        registry.submit_task(TaskTransfer(
            task_id="t-arch",
            title="arch task",
            required_role="arch",
        ))
        registry.submit_task(TaskTransfer(
            task_id="t-crud",
            title="crud task",
            required_role="crud",
        ))

        tasks = _aio.run(registry.alist_ready_tasks(role="arch"))
        assert len(tasks) == 1
        assert tasks[0].task_id == "t-arch"

    def test_role_filter_none_still_passes_roleless_tasks(self, ledger: SQLiteTaskLedger) -> None:
        """When role=None, tasks without required_role are included."""
        registry = Registry(ledger)
        import asyncio as _aio

        registry.submit_task(TaskTransfer(
            task_id="t-open",
            title="open task",
            # no required_role
        ))
        registry.submit_task(TaskTransfer(
            task_id="t-gated",
            title="gated task",
            required_role="arch",
        ))

        tasks = _aio.run(registry.alist_ready_tasks())
        # Both included: role=None means no filter applied
        assert len(tasks) == 2
