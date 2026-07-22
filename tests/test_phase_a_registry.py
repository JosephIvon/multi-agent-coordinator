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


def test_submit_task_rejects_self_loop(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))

    with pytest.raises(StateConflictError, match="circular_dependency"):
        registry.submit_task(_task("task-1", depends_on=["task-1"]))

    assert registry.get_task("task-1") is None


def test_submit_task_rejects_indirect_cycle(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task("task-a", depends_on=["task-b"]))
    assert registry.get_task("task-a").depends_on == ["task-b"]

    with pytest.raises(StateConflictError, match="circular_dependency"):
        registry.submit_task(_task("task-b", depends_on=["task-a"]))

    assert registry.get_task("task-b") is None


def test_submit_task_allows_diamond_dependency_without_cycle(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task("task-b"))
    registry.submit_task(_task("task-c"))
    registry.submit_task(_task("task-a", depends_on=["task-b", "task-c"]))

    ready = [task.task_id for task in registry.list_ready_tasks(capability="write_code")]
    assert set(ready) == {"task-b", "task-c"}


def test_submit_task_tolerates_missing_dependency_forward_reference(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))

    registry.submit_task(_task("task-a", depends_on=["future-task"]))

    assert registry.get_task("task-a").depends_on == ["future-task"]


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


# ---------------------------------------------------------------------------
# P4-5: registry.py test reinforcement — dependency edges, state machine,
# conflict lifecycle, handoff inheritance. Targets mutmut kill-rate ~65%.
# ---------------------------------------------------------------------------


def test_claim_next_task_best_effort_claims_other_capability_by_observed_score(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.register(
        AgentCard(
            agent_id="worker",
            name="Worker",
            capabilities=[AgentCapability(name="write_tests")],
        )
    )
    # Pre-seed an observed outcome so the agent's score for write_code is non-zero.
    registry.record_task_outcome(
        agent_id="worker",
        capability="write_code",
        task_type="write_code",
        status="completed",
        duration_seconds=1.0,
    )
    registry.submit_task(_task("only-write-code", capability="write_code"))

    claimed = registry.claim_next_task(
        agent_id="worker", capability="write_tests", best_effort=True
    )

    assert claimed is not None
    assert claimed.task_id == "only-write-code"
    assert claimed.target_agent_id == "worker"


def test_claim_next_task_skips_task_pinned_to_other_agent(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task("pinned", target_agent_id="other-agent"))
    registry.submit_task(_task("free", priority=1))

    claimed = registry.claim_next_task(agent_id="worker", capability="write_code")

    assert claimed.task_id == "free"
    assert registry.get_task("pinned").status == "proposed"


def test_dependencies_satisfied_rejects_failed_dependency_status(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task("failed-dep", status="failed"))
    registry.submit_task(_task("child", depends_on=["failed-dep"]))

    ready = [task.task_id for task in registry.list_ready_tasks(capability="write_code")]

    assert ready == []


def test_cancel_task_rejects_completed_or_cancelled_status(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task("finished", status="completed"))

    with pytest.raises(StateConflictError):
        registry.cancel_task(task_id="finished", agent_id="worker", reason="too late")

    assert registry.get_task("finished").status == "completed"


def test_retry_task_rejects_non_failed_status_and_increments_count(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task("running-task", status="running"))

    with pytest.raises(StateConflictError):
        registry.retry_task(task_id="running-task", agent_id="worker")

    # Now flip it to failed, retry, and verify retry_count + fallback_agent_id.
    registry.submit_task(_task("will-fail", status="running"))
    registry.fail_task(task_id="will-fail", agent_id="worker", error_code="QUALITY_GATE_FAILED")
    retried = registry.retry_task(task_id="will-fail", agent_id="worker", fallback_agent_id="fallback-1")

    assert retried.status == "proposed"
    assert retried.retry_count == 1
    assert retried.error_code is None
    assert retried.target_agent_id == "fallback-1"


def test_fail_task_records_error_code_and_publishes_task_failed_event(tmp_path):
    events = []
    bus = TaskEventBus()
    bus.subscribe(events.append)
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"), event_bus=bus)
    registry.submit_task(_task("task-1", status="running"))

    failed = registry.fail_task(
        task_id="task-1", agent_id="worker", error_code="DEPS_TIMEOUT", message="missing import"
    )

    assert failed.status == "failed"
    assert failed.error_code == "DEPS_TIMEOUT"
    assert [event.type for event in events[-1:]] == ["task_failed"]


def test_record_checkpoint_rejects_completed_or_cancelled_status(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task("done", status="completed"))

    with pytest.raises(StateConflictError):
        registry.record_checkpoint(task_id="done", agent_id="worker", checkpoint={"note": "x"})


def test_complete_task_raises_quality_gate_error_when_contract_fails(tmp_path):
    from mac.protocol.errors import QualityGateError
    from mac.testing.contracts import TestContract

    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task("task-1", status="running"))
    task = registry.get_task("task-1")
    task.test_contract = TestContract(
        risk_level="medium",
        required_commands=["python -m pytest tests"],
        required_evidence=["test_output"],
    )
    registry.ledger.save_task_transfer(task)

    with pytest.raises(QualityGateError):
        registry.complete_task(task_id="task-1", agent_id="worker")


def test_record_and_resolve_conflict_publishes_events_and_filters_listing(tmp_path):
    events = []
    bus = TaskEventBus()
    bus.subscribe(events.append)
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"), event_bus=bus)

    recorded = registry.record_conflict(
        ConflictRecord(
            conflict_id="c-1",
            plan_id="plan-1",
            task_id="task-1",
            source="manual",
            severity="non_blocking",
            description="needs review",
        )
    )
    unresolved_before = registry.list_conflicts(plan_id="plan-1", resolved=False)
    all_before = registry.list_conflicts(plan_id="plan-1")
    assert len(unresolved_before) == 1
    assert len(all_before) == 1

    resolved = registry.resolve_conflict("c-1", "Reviewed by planner")

    assert resolved.resolved is True
    assert resolved.resolution == "Reviewed by planner"
    assert registry.list_conflicts(plan_id="plan-1", resolved=False) == []
    assert len(registry.list_conflicts(plan_id="plan-1", resolved=True)) == 1
    assert [event.type for event in events] == ["conflict_recorded", "conflict_resolved"]
    assert recorded.conflict_id == "c-1"


def test_prepare_review_packet_includes_open_conflicts(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.create_plan(goal="Conflicts", created_by="planner", plan_id="plan-1")
    registry.submit_task(_task("task-1", plan_id="plan-1"))
    registry.record_conflict(
        ConflictRecord(
            conflict_id="c-open",
            plan_id="plan-1",
            task_id="task-1",
            source="manual",
            severity="blocking",
            description="Boundary breach detected",
        )
    )

    packet = registry.prepare_review_packet("task-1")

    assert "## Open Conflicts" in packet
    assert "c-open" in packet
    assert "Boundary breach detected" in packet


def test_save_handoff_result_preserves_explicit_plan_id_over_task_default(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.create_plan(goal="Override", created_by="planner", plan_id="plan-task")
    registry.create_plan(goal="Explicit", created_by="planner", plan_id="plan-explicit")
    registry.submit_task(_task("task-1", plan_id="plan-task"))

    handoff = registry.save_handoff_result(
        HandoffResult(
            task_id="task-1",
            plan_id="plan-explicit",  # Caller-supplied value must win.
            agent_id="worker",
        )
    )

    assert handoff.plan_id == "plan-explicit"
    assert registry.get_handoff_result("task-1").plan_id == "plan-explicit"


def test_apply_path_guardrails_resets_block_to_pass_when_no_violations_remain(tmp_path):
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
    registry.create_plan(goal="Reset boundary", created_by="planner", plan_id="plan-1")
    registry.submit_task(_task("task-1", plan_id="plan-1"))

    # First handoff with a forbidden file -> block.
    blocked = registry.save_handoff_result(
        HandoffResult(
            task_id="task-1",
            plan_id="plan-1",
            agent_id="worker",
            changed_files=["src/secrets/key.py"],
        ),
        path_rule=PathRule(allow_all=False),
    )
    assert blocked.boundary_review == "block"

    # Second handoff with only allowed files -> boundary resets to pass.
    cleared = registry.save_handoff_result(
        HandoffResult(
            task_id="task-1",
            plan_id="plan-1",
            agent_id="worker",
            boundary_review="block",  # pre-existing block; should be overridden
            changed_files=["src/mac/registry.py"],
        ),
        path_rule=PathRule(allow_all=False),
    )
    assert cleared.boundary_review == "pass"
    assert cleared.violated_guardrail == []
