import pytest

from mac.events import TaskEventBus
from mac.protocol.errors import QualityGateError, StateConflictError
from mac.protocol.messages import ContextBundle, TaskPayload, TaskTransfer
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger
from mac.testing.contracts import TestContract


def _task(task_id: str = "task-1", *, risk: str | None = None) -> TaskTransfer:
    return TaskTransfer(
        task_id=task_id,
        trace_id=f"trace-{task_id}",
        source_agent_id="planner",
        target_agent_id="tester",
        payload=TaskPayload(
            type="write_test",
            summary="Recovery task",
            target_module="mac.registry",
            coverage_goal=80,
            risk_level=risk,
        ),
        context=ContextBundle(summary="Recovery task"),
        test_contract=TestContract.for_risk(risk) if risk else None,
    )


def test_checkpoint_then_retry_failed_task_to_fallback_agent(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task())
    registry.accept_handoff("task-1", "tester")
    registry.start_task("task-1", "tester")

    checkpointed = registry.record_checkpoint(
        "task-1",
        agent_id="tester",
        checkpoint={"summary": "created failing test", "artifacts": ["file://tests/test_x.py"]},
    )
    assert checkpointed.metadata["checkpoints"][0]["summary"] == "created failing test"

    registry.fail_task("task-1", "tester", "HANDLER_ERROR", "handler crashed")
    retried = registry.retry_task("task-1", agent_id="planner", fallback_agent_id="fallback-tester")

    assert retried.status == "proposed"
    assert retried.retry_count == 1
    assert retried.error_code is None
    assert retried.target_agent_id == "fallback-tester"
    assert retried.metadata["checkpoints"][0]["agent_id"] == "tester"
    assert [entry.action for entry in registry.get_audit_trail("trace-task-1")] == [
        "submit_task",
        "accept_handoff",
        "start_task",
        "checkpoint_task",
        "fail_task",
        "retry_task",
    ]


def test_cancel_task_records_terminal_cancelled_state(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task("task-cancel"))

    cancelled = registry.cancel_task("task-cancel", agent_id="planner", reason="superseded")

    assert cancelled.status == "cancelled"
    assert cancelled.error_code == "TASK_CANCELLED"
    assert registry.preview_task_readiness("task-cancel").next_action == "none"
    assert registry.preview_task_readiness("task-cancel").blocking_reason == "task_cancelled"


def test_retry_does_not_reuse_quality_evidence_from_previous_attempt(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task(risk="high"))
    registry.accept_handoff("task-1", "tester")
    registry.start_task("task-1", "tester")
    registry.submit_quality_result(
        "task-1",
        {
            "agent_id": "tester",
            "command": "python -m pytest --cov",
            "status": "passed",
            "evidence": ["test_output", "coverage_report", "review_notes"],
        },
    )
    registry.fail_task("task-1", "tester", "HANDLER_ERROR")
    registry.retry_task("task-1", agent_id="planner", fallback_agent_id="fallback")
    registry.accept_handoff("task-1", "fallback")
    registry.start_task("task-1", "fallback")

    with pytest.raises(QualityGateError):
        registry.complete_task("task-1", "fallback")

    preview = registry.preview_quality_gate("task-1")
    assert preview is not None
    assert preview.quality_results_count == 0

    registry.submit_quality_result(
        "task-1",
        {
            "agent_id": "fallback",
            "command": "python -m pytest --cov",
            "status": "passed",
            "evidence": ["test_output", "coverage_report", "review_notes"],
        },
    )
    completed = registry.complete_task("task-1", "fallback")
    assert completed.status == "completed"
    assert [result["retry_count"] for result in registry.ledger.get_quality_results("task-1")] == [0, 1]


def test_retry_rejects_non_failed_tasks(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task())

    with pytest.raises(StateConflictError):
        registry.retry_task("task-1", agent_id="planner")


def test_terminal_tasks_reject_checkpoint_and_duplicate_cancel(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task("task-cancel"))
    registry.cancel_task("task-cancel", agent_id="planner", reason="obsolete")

    with pytest.raises(StateConflictError):
        registry.record_checkpoint("task-cancel", agent_id="tester", checkpoint={"summary": "late"})

    with pytest.raises(StateConflictError):
        registry.cancel_task("task-cancel", agent_id="planner", reason="again")


def test_recovery_operations_publish_events(tmp_path):
    bus = TaskEventBus()
    events = []
    bus.subscribe(events.append)
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"), event_bus=bus)
    registry.submit_task(_task())
    registry.accept_handoff("task-1", "tester")
    registry.start_task("task-1", "tester")

    registry.record_checkpoint("task-1", agent_id="tester", checkpoint={"summary": "halfway"})
    registry.fail_task("task-1", "tester", "HANDLER_ERROR")
    registry.retry_task("task-1", agent_id="planner")
    registry.cancel_task("task-1", agent_id="planner", reason="obsolete")

    assert "task_checkpointed" in [event.type for event in events]
    assert "task_retried" in [event.type for event in events]
    assert "task_cancelled" in [event.type for event in events]
