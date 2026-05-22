import pytest

from mac.events import TaskEventBus
from mac.protocol.errors import StateConflictError
from mac.protocol.messages import (
    AgentCapability,
    AgentCard,
    ConflictRecord,
    HandoffResult,
    PathRule,
    Plan,
    TaskPayload,
    TaskTransfer,
    VerificationEntry,
)
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger


def _task(task_id: str, *, capability: str = "write_code", status: str = "proposed", **updates) -> TaskTransfer:
    return TaskTransfer(
        task_id=task_id,
        payload=TaskPayload(type=capability, summary=f"{task_id} summary"),
        status=status,
        **updates,
    )


def test_registry_manages_plan_lifecycle_and_plan_task_membership(tmp_path):
    events = []
    bus = TaskEventBus()
    bus.subscribe(events.append)
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"), event_bus=bus)

    plan = registry.create_plan(goal="Ship Phase A", created_by="planner", plan_id="plan-1")
    registry.submit_task(_task("task-1", plan_id="plan-1"))
    activated = registry.activate_plan("plan-1")
    closed = registry.close_plan("plan-1")

    assert plan.status == "draft"
    assert activated.status == "active"
    assert closed.status == "completed"
    assert registry.get_plan("plan-1").task_ids == ["task-1"]
    assert [event.type for event in events] == [
        "plan_created",
        "task_submitted",
        "plan_activated",
        "plan_closed",
    ]


def test_create_plan_refuses_to_overwrite_existing_plan(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.create_plan(goal="Original", created_by="planner", plan_id="plan-1")

    with pytest.raises(StateConflictError):
        registry.create_plan(goal="Overwrite", created_by="planner", plan_id="plan-1")

    assert registry.get_plan("plan-1").goal == "Original"


def test_submit_task_requires_existing_plan_when_plan_id_is_set(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))

    with pytest.raises(KeyError):
        registry.submit_task(_task("task-1", plan_id="missing-plan"))

    assert registry.get_task("task-1") is None


def test_list_ready_tasks_requires_dependencies_to_be_completed_or_cancelled(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task("accepted-dependency", status="accepted"))
    registry.submit_task(_task("completed-dependency", status="completed"))
    registry.submit_task(_task("cancelled-dependency", status="cancelled"))
    registry.submit_task(_task("blocked-child", depends_on=["accepted-dependency"], priority=10))
    registry.submit_task(_task("ready-child", depends_on=["completed-dependency", "cancelled-dependency"], priority=8))
    registry.submit_task(_task("missing-child", depends_on=["missing-task"], priority=9))

    ready_ids = [task.task_id for task in registry.list_ready_tasks(capability="write_code")]

    assert ready_ids == ["ready-child"]


def test_claim_next_task_skips_dependency_blocked_tasks(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task("dependency", status="accepted"))
    registry.submit_task(_task("blocked-child", depends_on=["dependency"], priority=10))
    registry.submit_task(_task("unblocked", priority=1))

    claimed = registry.claim_next_task(agent_id="worker", capability="write_code")

    assert claimed.task_id == "unblocked"
    assert registry.get_task("blocked-child").status == "proposed"


def test_list_ready_tasks_is_read_only(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task("task-1"))
    before = [entry.action for entry in registry.ledger.list_audit_entries("task-1")]

    assert [task.task_id for task in registry.list_ready_tasks()] == ["task-1"]

    after = [entry.action for entry in registry.ledger.list_audit_entries("task-1")]
    assert after == before


def test_handoff_path_guardrail_blocks_and_records_conflict(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.register(
        AgentCard(
            agent_id="worker",
            name="Worker",
            capabilities=[AgentCapability(name="write_code")],
            allowed_paths=["src/**"],
            forbidden_paths=["src/secrets/**"],
        )
    )
    registry.create_plan(goal="Ship guardrails", created_by="planner", plan_id="plan-1")
    registry.submit_task(_task("task-1", plan_id="plan-1"))

    handoff = registry.save_handoff_result(
        HandoffResult(
            task_id="task-1",
            plan_id="plan-1",
            agent_id="worker",
            verification=[VerificationEntry(command="python -m pytest -q", result="pass")],
            changed_files=["src/secrets/key.py"],
        ),
        path_rule=PathRule(allow_all=False),
    )

    assert handoff.boundary_review == "block"
    assert handoff.violated_guardrail
    conflicts = registry.list_conflicts(plan_id="plan-1", resolved=False)
    assert conflicts[0].source == "path_violation"
    assert conflicts[0].task_id == "task-1"


def test_handoff_inherits_task_plan_id_when_not_supplied(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.create_plan(goal="Ship handoff", created_by="planner", plan_id="plan-1")
    registry.submit_task(_task("task-1", plan_id="plan-1"))

    handoff = registry.save_handoff_result(HandoffResult(task_id="task-1", agent_id="worker"))

    assert handoff.plan_id == "plan-1"
    assert registry.get_handoff_result("task-1").plan_id == "plan-1"


def test_conflict_lifecycle_and_packets(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.register(
        AgentCard(
            agent_id="worker",
            name="Worker",
            capabilities=[AgentCapability(name="write_code")],
            allowed_paths=["src/**"],
        )
    )
    registry.create_plan(goal="Ship packets", created_by="planner", plan_id="plan-1")
    registry.submit_task(_task("dependency", plan_id="plan-1", status="completed"))
    registry.submit_task(_task("task-1", plan_id="plan-1", depends_on=["dependency"]))
    registry.save_handoff_result(
        HandoffResult(
            task_id="task-1",
            plan_id="plan-1",
            agent_id="worker",
            verification=[VerificationEntry(command="python -m pytest -q", result="pass")],
            changed_files=["src/mac/registry.py"],
            risks=["needs integration smoke"],
        )
    )
    conflict = registry.record_conflict(
        ConflictRecord(
            conflict_id="conflict-1",
            plan_id="plan-1",
            task_id="task-1",
            source="manual",
            severity="non_blocking",
            description="Reviewer should inspect packet wording",
        )
    )

    worker_packet = registry.prepare_worker_packet("task-1", agent_id="worker")
    review_packet = registry.prepare_review_packet("task-1")
    resolved = registry.resolve_conflict(conflict.conflict_id, "Packet reviewed")

    assert "Worker Task: task-1" in worker_packet
    assert "Depends On" in worker_packet
    assert "Review Task: task-1" in review_packet
    assert "python -m pytest -q" in review_packet
    assert resolved.resolved is True
