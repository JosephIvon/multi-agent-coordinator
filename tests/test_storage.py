from dataclasses import dataclass, field

import pytest

from mac.storage import SQLiteTaskLedger, StatusConflict

try:
    from mac.protocol.messages import AgentCapability, AgentCard, AuditEntry, TaskTransfer
except ModuleNotFoundError:
    from mac.storage.models import AgentCapability, AgentCard, AuditEntry, TaskTransfer


def test_sqlite_ledger_persists_agent_task_and_audit_entries(tmp_path):
    ledger = SQLiteTaskLedger(tmp_path / "mac.db")
    agent = AgentCard(
        agent_id="agent-a",
        name="Agent A",
        capabilities=[AgentCapability(name="python_unit_test")],
        status="available",
        load=2,
        project_context="demo",
    )
    task = TaskTransfer(
        task_id="task-1",
        title="Run focused tests",
        description="Run storage tests",
        source_agent_id="lead",
        target_agent_id="agent-a",
        status="pending",
        project_context="demo",
    )
    audit = AuditEntry(
        entry_id="audit-1",
        task_id="task-1",
        actor="lead",
        action="transfer_created",
        details={"target": "agent-a"},
    )

    ledger.save_agent_card(agent)
    ledger.save_task_transfer(task)
    ledger.record_audit_entry(audit)

    assert ledger.get_agent_card("agent-a").agent_id == "agent-a"
    assert ledger.get_task_transfer("task-1").target_agent_id == "agent-a"
    assert ledger.list_audit_entries("task-1")[0].action == "transfer_created"


def test_update_task_status_uses_expected_status_compare_and_swap(tmp_path):
    ledger = SQLiteTaskLedger(tmp_path / "mac.db")
    task = TaskTransfer(
        task_id="task-1",
        title="Run focused tests",
        description="Run storage tests",
        source_agent_id="lead",
        target_agent_id="agent-a",
        status="pending",
        project_context="demo",
    )
    ledger.save_task_transfer(task)

    updated = ledger.update_task_status(
        "task-1",
        "accepted",
        expected_status="pending",
        actor="agent-a",
    )

    assert updated.status == "accepted"
    assert ledger.get_task_transfer("task-1").status == "accepted"
    audit_actions = [entry.action for entry in ledger.list_audit_entries("task-1")]
    assert "task_status_updated" in audit_actions

    with pytest.raises(StatusConflict):
        ledger.update_task_status(
            "task-1",
            "completed",
            expected_status="pending",
            actor="agent-a",
        )
