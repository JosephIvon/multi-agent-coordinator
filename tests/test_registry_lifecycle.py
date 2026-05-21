import pytest

from mac.protocol.messages import AgentCapability, AgentCard, ContextBundle, TaskPayload, TaskTransfer
from mac.protocol.errors import QualityGateError, StateConflictError
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger
from mac.testing.contracts import TestContract


def test_registry_task_lifecycle_requires_quality_evidence(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.register(AgentCard(agent_id="tester", name="Tester", capabilities=[AgentCapability(name="write_test")]))
    task = TaskTransfer(
        task_id="task-1",
        trace_id="trace-1",
        source_agent_id="planner",
        payload=TaskPayload(
            type="write_test",
            summary="Write focused tests",
            target_module="mac.registry",
            coverage_goal=85,
            risk_level="high",
        ),
        context=ContextBundle(summary="Registry lifecycle", artifact_refs=["file://src/mac/registry.py"]),
        test_contract=TestContract.for_risk("high"),
    )

    registry.submit_task(task)
    registry.accept_handoff("task-1", "tester")
    registry.start_task("task-1", "tester")

    with pytest.raises(QualityGateError):
        registry.complete_task("task-1", "tester")

    registry.submit_quality_result(
        "task-1",
        {
            "command": "python -m pytest --cov",
            "status": "passed",
            "evidence": ["test_output", "coverage_report", "review_notes"],
        },
    )
    completed = registry.complete_task("task-1", "tester")

    assert completed.status == "completed"
    assert [entry.action for entry in registry.get_audit_trail("trace-1")] == [
        "submit_task",
        "accept_handoff",
        "start_task",
        "submit_quality_result",
        "complete_task",
    ]


def test_registry_rejects_invalid_state_transition(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    task = TaskTransfer(
        task_id="task-1",
        trace_id="trace-1",
        source_agent_id="planner",
        payload=TaskPayload(type="custom", summary="Review docs"),
    )
    registry.submit_task(task)

    with pytest.raises(StateConflictError):
        registry.start_task("task-1", "tester")
