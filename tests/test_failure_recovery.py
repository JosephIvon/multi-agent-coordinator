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


def test_resume_blocked_task_starts_new_attempt(tmp_path):
    # Regression (M-2): resume_blocked_task used to keep retry_count
    # unchanged, so quality results from the blocked attempt stayed in the
    # "current attempt" bucket and could satisfy the gate on the retry.
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task(risk="low"))
    registry.accept_handoff("task-1", "tester")
    registry.start_task("task-1", "tester")

    blocked = registry.block_task("task-1", agent_id="tester", reason="waiting on spec")
    assert blocked.status == "blocked"

    resumed = registry.resume_blocked_task("task-1", agent_id="planner", resolution="spec clarified")
    assert resumed.status == "proposed"
    assert resumed.retry_count == 1

    # Re-run the task: the new attempt's quality result is stamped with
    # the bumped retry_count, keeping attempt buckets disjoint.
    registry.accept_handoff("task-1", "tester")
    registry.start_task("task-1", "tester")
    registry.submit_quality_result(
        "task-1",
        {
            "agent_id": "tester",
            "command": "pytest related tests or smoke test",
            "status": "passed",
            "evidence": ["test_output"],
        },
    )
    completed = registry.complete_task("task-1", "tester")
    assert completed.status == "completed"


def test_quality_gate_ignores_stale_passed_results_after_resume(tmp_path):
    # End-to-end variant (M-1/M-2): a passed result submitted before the
    # block must NOT let the post-resume attempt complete without fresh
    # evidence.
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task(risk="low"))
    registry.accept_handoff("task-1", "tester")
    registry.start_task("task-1", "tester")

    # Attempt 0: gate passes, but the task gets blocked (e.g. a blocking
    # conflict) before completing.
    registry.submit_quality_result(
        "task-1",
        {
            "agent_id": "tester",
            "command": "pytest related tests or smoke test",
            "status": "passed",
            "evidence": ["test_output"],
        },
    )
    registry.block_task("task-1", agent_id="tester", reason="blocking conflict")
    registry.resume_blocked_task("task-1", agent_id="planner", resolution="resolved")

    # New attempt reaches running with NO fresh evidence.
    registry.accept_handoff("task-1", "tester")
    registry.start_task("task-1", "tester")

    with pytest.raises(QualityGateError):
        registry.complete_task("task-1", "tester")


def test_fail_task_refuses_completed_task_without_audit_side_effect(tmp_path):
    # Regression (M-3): fail_task used to blind-write over any status, so a
    # late expire_stale_tasks could overwrite a just-completed task.
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task())
    registry.accept_handoff("task-1", "tester")
    registry.start_task("task-1", "tester")
    registry.complete_task("task-1", "tester")

    with pytest.raises(StateConflictError):
        registry.fail_task("task-1", "system", "TTL_EXPIRED")

    assert registry.get_task("task-1").status == "completed"


def test_cancel_task_loses_race_to_concurrent_transition(tmp_path):
    # Read-modify-write guard: cancelling from a stale snapshot must raise
    # instead of clobbering the newer status.
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task())
    task = registry.get_task("task-1")  # stale snapshot: still 'proposed'
    registry.cancel_task("task-1", agent_id="planner", reason="obsolete")

    # Simulate the stale writer finishing after the cancel landed.
    task.status = "cancelled"
    task.updated_at = "2026-01-01T00:00:00+00:00"
    from mac.storage.sqlite import StatusConflict

    with pytest.raises(StatusConflict):
        registry.ledger.save_task_transfer(task, expected_status="proposed")

    assert registry.get_task("task-1").status == "cancelled"


def test_expire_stale_tasks_skips_task_that_completed_concurrently(tmp_path):
    # The expiry loop must swallow the StateConflictError from fail_task's
    # CAS and keep the newer (completed) state.
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    stale = _task("task-old")
    stale.ttl_seconds = 1
    stale.updated_at = "2020-01-01T00:00:00+00:00"
    registry.submit_task(stale)
    registry.accept_handoff("task-old", "tester")
    registry.start_task("task-old", "tester")
    registry.complete_task("task-old", "tester")

    import time as _time

    expired = registry.expire_stale_tasks(now=_time.time() + 3600)
    assert expired == []
    assert registry.get_task("task-old").status == "completed"
