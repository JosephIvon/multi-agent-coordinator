from mac.protocol.messages import ContextBundle, TaskPayload, TaskTransfer
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger
from mac.testing.contracts import TestContract


def _task(
    task_id: str,
    *,
    status: str = "proposed",
    target_agent_id: str | None = None,
    risk: str | None = None,
) -> TaskTransfer:
    return TaskTransfer(
        task_id=task_id,
        trace_id=f"trace-{task_id}",
        source_agent_id="planner",
        target_agent_id=target_agent_id,
        payload=TaskPayload(
            type="write_test",
            summary=f"{task_id} summary",
            target_module="mac.registry",
            coverage_goal=85,
            risk_level=risk,
        ),
        context=ContextBundle(summary=f"{task_id} context"),
        test_contract=TestContract.for_risk(risk) if risk else None,
        status=status,
    )


def test_task_readiness_suggests_claim_for_unassigned_proposed_task(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task("task-open"))

    report = registry.preview_task_readiness("task-open")

    assert report is not None
    assert report.task_id == "task-open"
    assert report.trace_id == "trace-task-open"
    assert report.status == "proposed"
    assert report.execution_agent_id is None
    assert report.required_capability == "write_test"
    assert report.next_action == "claim_task"
    assert report.blocking_reason is None
    assert report.quality_allowed is None
    assert report.quality_results_count == 0
    assert report.audit_event_count == 1


def test_task_readiness_suggests_accept_for_assigned_proposed_task(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task("task-assigned", target_agent_id="tester"))

    report = registry.preview_task_readiness("task-assigned")

    assert report is not None
    assert report.execution_agent_id == "tester"
    assert report.next_action == "accept_handoff"
    assert report.blocking_reason is None


def test_task_readiness_respects_target_agent_and_required_capability_override(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    task = _task("task-assigned")
    task.target_agent_id = None
    task.target_agent_id = "tester"
    task.payload.extra["required_capability"] = "python-testing"
    registry.submit_task(task)

    report = registry.preview_task_readiness("task-assigned")

    assert report is not None
    assert report.execution_agent_id == "tester"
    assert report.required_capability == "python-testing"
    assert report.next_action == "accept_handoff"


def test_task_readiness_suggests_start_for_accepted_task(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task("task-accepted", target_agent_id="tester"))
    registry.accept_handoff("task-accepted", "tester")

    report = registry.preview_task_readiness("task-accepted")

    assert report is not None
    assert report.status == "accepted"
    assert report.next_action == "start_task"
    assert report.blocking_reason is None


def test_task_readiness_blocks_running_task_on_missing_quality_evidence(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task("task-running", target_agent_id="tester", risk="high"))
    registry.accept_handoff("task-running", "tester")
    registry.start_task("task-running", "tester")
    registry.submit_quality_result(
        "task-running",
        {
            "agent_id": "tester",
            "command": "python -m pytest --cov",
            "status": "passed",
            "evidence": ["test_output"],
        },
    )

    report = registry.preview_task_readiness("task-running")

    assert report is not None
    assert report.status == "running"
    assert report.next_action == "submit_quality_result"
    assert report.quality_allowed is False
    assert report.blocking_reason == "quality_gate_failed:missing_evidence:coverage_report,review_notes"
    assert report.missing_commands == []
    assert report.missing_evidence == ["coverage_report", "review_notes"]
    assert report.quality_results_count == 1


def test_task_readiness_suggests_complete_when_running_quality_gate_is_satisfied(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task("task-ready", target_agent_id="tester", risk="high"))
    registry.accept_handoff("task-ready", "tester")
    registry.start_task("task-ready", "tester")
    registry.submit_quality_result(
        "task-ready",
        {
            "agent_id": "tester",
            "command": "python -m pytest --cov",
            "status": "passed",
            "evidence": ["test_output", "coverage_report", "review_notes"],
        },
    )

    report = registry.preview_task_readiness("task-ready")

    assert report is not None
    assert report.next_action == "complete_task"
    assert report.quality_allowed is True
    assert report.blocking_reason is None
    assert report.missing_commands == []
    assert report.missing_evidence == []


def test_task_readiness_reports_terminal_completed_and_failed_tasks(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task("task-done", target_agent_id="tester"))
    registry.accept_handoff("task-done", "tester")
    registry.start_task("task-done", "tester")
    registry.complete_task("task-done", "tester")
    registry.submit_task(_task("task-failed", target_agent_id="tester"))
    registry.accept_handoff("task-failed", "tester")
    registry.start_task("task-failed", "tester")
    registry.fail_task("task-failed", "tester", "HANDLER_ERROR")

    completed = registry.preview_task_readiness("task-done")
    failed = registry.preview_task_readiness("task-failed")

    assert completed is not None
    assert completed.status == "completed"
    assert completed.next_action == "none"
    assert completed.blocking_reason == "task_completed"
    assert failed is not None
    assert failed.status == "failed"
    assert failed.next_action == "inspect_failure"
    assert failed.blocking_reason == "task_failed:HANDLER_ERROR"


def test_task_readiness_reports_rejected_task_for_inspection(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task("task-rejected", target_agent_id="tester"))
    registry.reject_handoff("task-rejected", "tester", "not suitable")

    report = registry.preview_task_readiness("task-rejected")

    assert report is not None
    assert report.status == "rejected"
    assert report.next_action == "inspect_rejection"
    assert report.blocking_reason == "task_rejected"


def test_task_readiness_is_read_only_and_returns_none_for_missing_task(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task("task-readonly", target_agent_id="tester", risk="high"))
    registry.accept_handoff("task-readonly", "tester")
    registry.start_task("task-readonly", "tester")
    original = registry.get_task("task-readonly")
    assert original is not None
    before_audit = [entry.action for entry in registry.get_audit_trail("trace-task-readonly")]

    report = registry.preview_task_readiness("task-readonly")
    after = registry.get_task("task-readonly")
    assert after is not None
    after_audit = [entry.action for entry in registry.get_audit_trail("trace-task-readonly")]

    assert report is not None
    assert after.updated_at == original.updated_at
    assert after_audit == before_audit
    assert registry.preview_task_readiness("missing") is None
