"""Tests for strict path-boundary enforcement on Registry.done().

The opt-in ``enforce_boundaries=True`` flag on ``Registry.done()`` and
the standalone :meth:`Registry.enforce_path_boundaries` are the hard
counterpart to the soft ``boundary_review=block`` annotation stored on a
HandoffResult. They raise / short-circuit before the handoff is recorded,
so a misbehaving agent can never silently write outside its allowed paths.
"""
from __future__ import annotations

import pytest

from mac.protocol.errors import BoundaryViolationError
from mac.protocol.messages import (
    AgentCapability,
    AgentCard,
    ContextBundle,
    HandoffResult,
    TaskPayload,
    TaskTransfer,
)
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger
from mac.testing.contracts import TestContract


def _task(task_id):
    return TaskTransfer(
        task_id=task_id,
        trace_id="trace-" + task_id,
        source_agent_id="planner",
        payload=TaskPayload(type="custom", summary="work on " + task_id),
        context=ContextBundle(summary="context " + task_id),
        test_contract=TestContract.for_risk("low"),
    )


def _handoff(task_id, agent_id, files):
    return HandoffResult(
        task_id=task_id,
        agent_id=agent_id,
        changed_files=list(files or []),
    )


def _make_agent(agent_id):
    return AgentCard(
        agent_id=agent_id,
        name=agent_id,
        capabilities=[AgentCapability(name="write_test")]
    )


def _start_task(registry, task_id, agent_id):
    registry.submit_task(_task(task_id))
    registry.accept_handoff(task_id, agent_id)
    registry.start_task(task_id, agent_id)
    registry.submit_quality_result(
        task_id,
        {
            "command": "pytest related tests or smoke test",
            "status": "passed",
            "evidence": ["test_output"],
        },
    )


# ----- enforce_path_boundaries ---------------------------------------


def test_enforce_returns_changed_files_when_no_boundary(tmp_path):
    "An agent without allowed/forbidden lists is permissive."
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.register(_make_agent("a"))
    _start_task(registry, "t", "a")
    passing = registry.enforce_path_boundaries("t", _handoff("t", "a", ["src/a.py", "tests/x.py"]))
    assert passing == ["src/a.py", "tests/x.py"]


def test_enforce_passes_when_all_in_allowed(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.register(_make_agent("a"))
    _start_task(registry, "t", "a")
    # Patch the agent card post-hoc to add allowed_paths (the model has a
    # default empty list; this test exercises the positive path).
    card = registry.get_agent("a").model_copy(update={"allowed_paths": ["src/*"]})
    registry.register(card)
    passing = registry.enforce_path_boundaries("t", _handoff("t", "a", ["src/a.py", "src/b.py"]))
    assert passing == ["src/a.py", "src/b.py"]


def test_enforce_raises_when_file_outside_allowed(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.register(_make_agent("a"))
    _start_task(registry, "t", "a")
    card = registry.get_agent("a").model_copy(update={"allowed_paths": ["src/*"]})
    registry.register(card)
    with pytest.raises(BoundaryViolationError) as exc:
        registry.enforce_path_boundaries("t", _handoff("t", "a", ["src/a.py", "secrets/key.pem"]))
    assert any("not_allowed:secrets/key.pem" in v for v in exc.value.violations)


def test_enforce_raises_when_file_matches_forbidden(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.register(_make_agent("a"))
    _start_task(registry, "t", "a")
    card = registry.get_agent("a").model_copy(update={"forbidden_paths": ["secrets/*"]})
    registry.register(card)
    with pytest.raises(BoundaryViolationError) as exc:
        registry.enforce_path_boundaries("t", _handoff("t", "a", ["secrets/key.pem"]))
    assert any("forbidden:secrets/key.pem:secrets/*" in v for v in exc.value.violations)


# ----- done() with enforce_boundaries ---------------------------------


def test_done_enforce_boundaries_default_off(tmp_path):
    "Default behaviour must remain backward-compatible (no enforcement)."
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.register(_make_agent("a"))
    _start_task(registry, "t", "a")
    result = registry.done(
        "t",
        "a",
        handoff=_handoff("t", "a", ["anywhere/at/all.py"]),
    )
    assert result["status"] == "completed"


def test_done_enforce_boundaries_violation_returns_structured(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.register(_make_agent("a"))
    _start_task(registry, "t", "a")
    card = registry.get_agent("a").model_copy(update={"forbidden_paths": ["secrets/*"]})
    registry.register(card)
    result = registry.done(
        "t",
        "a",
        handoff=_handoff("t", "a", ["secrets/key.pem"]),
        enforce_boundaries=True,
    )
    assert result["status"] == "boundary_violation"
    assert result["task_id"] == "t"
    assert any("forbidden:" in v for v in result["violations"])


def test_done_enforce_boundaries_does_not_save_handoff(tmp_path):
    "When the handoff is refused, no HandoffResult should be persisted."
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.register(_make_agent("a"))
    _start_task(registry, "t", "a")
    card = registry.get_agent("a").model_copy(update={"forbidden_paths": ["secrets/*"]})
    registry.register(card)
    result = registry.done(
        "t",
        "a",
        handoff=_handoff("t", "a", ["secrets/key.pem"]),
        enforce_boundaries=True,
    )
    assert result["status"] == "boundary_violation"
    assert registry.get_handoff_result("t") is None


def test_done_enforce_boundaries_passes_clean_handoff(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.register(_make_agent("a"))
    _start_task(registry, "t", "a")
    card = registry.get_agent("a").model_copy(update={"allowed_paths": ["src/*"]})
    registry.register(card)
    result = registry.done(
        "t",
        "a",
        handoff=_handoff("t", "a", ["src/a.py"]),
        enforce_boundaries=True,
    )
    assert result["status"] == "completed"
    saved = registry.get_handoff_result("t")
    assert saved is not None and saved.changed_files == ["src/a.py"]


def test_done_enforce_boundaries_no_handoff_is_noop(tmp_path):
    "When no handoff is provided, enforce_boundaries has nothing to check."
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.register(_make_agent("a"))
    _start_task(registry, "t", "a")
    result = registry.done("t", "a", enforce_boundaries=True)
    assert result["status"] == "completed"
