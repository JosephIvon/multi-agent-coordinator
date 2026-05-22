from mac.protocol.messages import ConflictRecord, HandoffResult, Plan, VerificationEntry
from mac.storage import SQLiteTaskLedger


def test_sqlite_persists_plans_and_filters_by_status(tmp_path):
    ledger = SQLiteTaskLedger(tmp_path / "mac.db")
    draft = Plan(plan_id="plan-draft", goal="Draft plan", created_by="planner")
    active = Plan(plan_id="plan-active", goal="Active plan", created_by="planner", status="active")

    ledger.save_plan(draft)
    ledger.save_plan(active)

    assert ledger.get_plan("plan-draft").goal == "Draft plan"
    assert [plan.plan_id for plan in ledger.list_plans(status="active")] == ["plan-active"]


def test_sqlite_persists_handoff_result_separately_from_task(tmp_path):
    ledger = SQLiteTaskLedger(tmp_path / "mac.db")
    handoff = HandoffResult(
        task_id="task-1",
        plan_id="plan-1",
        agent_id="worker",
        verification=[VerificationEntry(command="python -m pytest -q", result="pass")],
        changed_files=["src/mac/registry.py"],
        risks=["manual pilot still needed"],
    )

    ledger.save_handoff_result(handoff)

    loaded = ledger.get_handoff_result("task-1")
    assert loaded.agent_id == "worker"
    assert loaded.verification[0].result == "pass"
    assert loaded.risks == ["manual pilot still needed"]


def test_sqlite_records_lists_and_resolves_conflicts(tmp_path):
    ledger = SQLiteTaskLedger(tmp_path / "mac.db")
    conflict = ConflictRecord(
        conflict_id="conflict-1",
        plan_id="plan-1",
        task_id="task-1",
        source="manual",
        severity="blocking",
        description="Changed file outside ownership",
        involved_files=["src/mac/registry.py"],
    )

    ledger.record_conflict(conflict)
    assert [item.conflict_id for item in ledger.list_conflicts(plan_id="plan-1", resolved=False)] == ["conflict-1"]

    resolved = ledger.resolve_conflict("conflict-1", "Reviewer accepted the boundary exception")
    assert resolved.resolved is True
    assert resolved.resolution == "Reviewer accepted the boundary exception"
    assert ledger.list_conflicts(resolved=False) == []
