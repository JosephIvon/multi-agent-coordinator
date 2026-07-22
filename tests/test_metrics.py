"""Tests for mac.metrics: aggregate collaboration metrics from existing ledger."""
from __future__ import annotations

from pathlib import Path

import pytest

from mac.metrics import compute_metrics, format_table
from mac.protocol.messages import (
    AgentCapability,
    AgentCard,
    ConflictRecord,
    ContextBundle,
    HandoffResult,
    TaskPayload,
    TaskTransfer,
)
from mac.registry import Registry
from mac.storage.sqlite import SQLiteTaskLedger
from mac.testing.contracts import TestContract


@pytest.fixture
def ledger(tmp_path: Path) -> SQLiteTaskLedger:
    return SQLiteTaskLedger(tmp_path / "mac.db")


@pytest.fixture
def registry(ledger: SQLiteTaskLedger) -> Registry:
    return Registry(ledger)


def _agent(agent_id: str = "agent-1") -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        name=f"Agent {agent_id}",
        capabilities=[AgentCapability(name="python")],
    )


def _task(task_id: str, **overrides: object) -> TaskTransfer:
    defaults: dict[str, object] = dict(
        task_id=task_id,
        trace_id=task_id,
        source_agent_id="lead",
        target_agent_id="agent-1",
        payload=TaskPayload(type="custom", summary="summary"),
        context=ContextBundle(summary="summary"),
        test_contract=TestContract.for_risk("low"),
    )
    defaults.update(overrides)
    return TaskTransfer(**defaults)  # type: ignore[arg-type]


def test_metrics_empty_ledger_returns_zeros(ledger: SQLiteTaskLedger) -> None:
    metrics = compute_metrics(ledger)
    expected_keys = {
        "metric_version",
        "task_cycle_time_seconds",
        "handoff_success_rate",
        "quality_gate_pass_rate",
        "retry_rate",
        "conflict_rate",
        "active_agents",
        "samples",
    }
    assert expected_keys.issubset(metrics.keys())
    assert metrics["task_cycle_time_seconds"] == 0.0
    assert metrics["handoff_success_rate"] == 0.0
    assert metrics["quality_gate_pass_rate"] == 0.0
    assert metrics["retry_rate"] == 0.0
    assert metrics["conflict_rate"] == 0.0
    assert metrics["active_agents"] == 0


def test_metrics_handoff_success_rate(registry: Registry, ledger: SQLiteTaskLedger) -> None:
    registry.register(_agent())
    for index in range(3):
        registry.submit_task(_task(f"task-{index}"))
        handoff = HandoffResult(
            task_id=f"task-{index}",
            agent_id="agent-1",
            boundary_review="pass" if index < 2 else "block",
        )
        # ledger.save_handoff_result bypasses Registry's _apply_path_guardrails
        # (which would otherwise coerce 'block' → 'pass' for unguarded agents).
        ledger.save_handoff_result(handoff)

    metrics = compute_metrics(ledger)
    assert metrics["samples"]["handoffs"] == 3
    assert metrics["handoff_success_rate"] == pytest.approx(2 / 3, abs=0.0001)


def test_metrics_quality_gate_pass_rate(registry: Registry, ledger: SQLiteTaskLedger) -> None:
    registry.register(_agent())
    for index in range(4):
        registry.submit_task(_task(f"task-{index}"))
        registry.submit_quality_result(
            f"task-{index}",
            {"command": "pytest", "status": "passed" if index < 3 else "failed"},
        )

    metrics = compute_metrics(ledger)
    assert metrics["samples"]["quality_results"] == 4
    assert metrics["quality_gate_pass_rate"] == pytest.approx(0.75, abs=0.0001)


def test_metrics_retry_rate(registry: Registry, ledger: SQLiteTaskLedger) -> None:
    registry.register(_agent())
    for index in range(5):
        task = _task(f"task-{index}", retry_count=2 if index < 2 else 0)
        registry.submit_task(task)

    metrics = compute_metrics(ledger)
    assert metrics["samples"]["task_transfers"] == 5
    assert metrics["retry_rate"] == pytest.approx(0.4, abs=0.0001)


def test_metrics_active_agents(registry: Registry, ledger: SQLiteTaskLedger) -> None:
    for index in range(3):
        registry.register(_agent(f"agent-{index}"))
        registry.heartbeat_agent(f"agent-{index}", status="online" if index < 2 else "busy")

    metrics = compute_metrics(ledger)
    assert metrics["active_agents"] == 2


def test_metrics_conflict_rate(registry: Registry, ledger: SQLiteTaskLedger) -> None:
    registry.register(_agent())
    for index in range(3):
        registry.submit_task(_task(f"task-{index}"))
    registry.record_conflict(
        ConflictRecord(source="test", description="conflict between agents")
    )

    metrics = compute_metrics(ledger)
    assert metrics["samples"]["conflicts"] == 1
    assert metrics["samples"]["task_transfers"] == 3
    assert metrics["conflict_rate"] == pytest.approx(1 / 3, abs=0.0001)


def test_metrics_task_cycle_time_completed_only(registry: Registry, ledger: SQLiteTaskLedger) -> None:
    """Only completed tasks contribute to cycle time; pending ones excluded."""
    registry.register(_agent())
    registry.submit_task(_task("task-1"))
    registry.submit_task(_task("task-2"))
    registry.claim_next_task(agent_id="agent-1", capability="custom")
    registry.start_task("task-1", agent_id="agent-1")
    # complete_task requires passing quality evidence satisfying the contract.
    # for_risk("low") requires command="pytest related tests or smoke test" and evidence=["test_output"].
    registry.submit_quality_result(
        "task-1",
        {
            "command": "pytest related tests or smoke test",
            "status": "passed",
            "evidence": ["test_output"],
        },
    )
    registry.complete_task("task-1", agent_id="agent-1")

    metrics = compute_metrics(ledger)
    assert metrics["samples"]["completed_tasks"] == 1
    assert metrics["task_cycle_time_seconds"] >= 0.0


def test_format_table_lists_all_six_indicators(ledger: SQLiteTaskLedger) -> None:
    metrics = compute_metrics(ledger)
    table = format_table(metrics)
    for indicator in (
        "task_cycle_time_seconds",
        "handoff_success_rate",
        "quality_gate_pass_rate",
        "retry_rate",
        "conflict_rate",
        "active_agents",
    ):
        assert indicator in table
    assert "Samples" in table