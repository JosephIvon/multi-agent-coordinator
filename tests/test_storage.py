from dataclasses import dataclass, field
import json
import sqlite3
import time
from uuid import uuid4

import pytest

from mac.protocol.messages import AgentCapability, AgentCard, AuditEntry, TaskTransfer
from mac.storage import SQLiteTaskLedger, StatusConflict


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


def test_audit_entries_trace_id_column_indexes_lookups(tmp_path):
    ledger = SQLiteTaskLedger(tmp_path / "mac.db")
    task_a = TaskTransfer(task_id="task-a", title="a", description="a")
    task_b = TaskTransfer(task_id="task-b", title="b", description="b")
    ledger.save_task_transfer(task_a)
    ledger.save_task_transfer(task_b)

    for index in range(3):
        ledger.record_audit_entry(
            AuditEntry(
                entry_id=f"audit-a-{index}",
                task_id="task-a",
                trace_id=task_a.trace_id,
                actor="agent",
                action=f"submit_task_{index}",
            )
        )
    for index in range(2):
        ledger.record_audit_entry(
            AuditEntry(
                entry_id=f"audit-b-{index}",
                task_id="task-b",
                trace_id=task_b.trace_id,
                actor="agent",
                action=f"submit_task_{index}",
            )
        )

    trail_a = ledger.get_audit_trail(task_a.trace_id)
    trail_b = ledger.get_audit_trail(task_b.trace_id)

    assert [entry.entry_id for entry in trail_a] == ["audit-a-0", "audit-a-1", "audit-a-2"]
    assert [entry.entry_id for entry in trail_b] == ["audit-b-0", "audit-b-1"]
    assert all(entry.trace_id == task_a.trace_id for entry in trail_a)
    assert all(entry.trace_id == task_b.trace_id for entry in trail_b)


def test_audit_trail_skips_empty_trace_id_rows(tmp_path):
    ledger = SQLiteTaskLedger(tmp_path / "mac.db")
    task = TaskTransfer(task_id="task-1", title="t", description="d")
    ledger.save_task_transfer(task)

    ledger.record_audit_entry(
        AuditEntry(
            entry_id="audit-no-trace",
            task_id="task-1",
            trace_id="",
            actor="agent",
            action="legacy_event",
        )
    )
    ledger.record_audit_entry(
        AuditEntry(
            entry_id="audit-with-trace",
            task_id="task-1",
            trace_id=task.trace_id,
            actor="agent",
            action="submit_task",
        )
    )

    assert [entry.entry_id for entry in ledger.get_audit_trail(task.trace_id)] == ["audit-with-trace"]


def test_audit_trail_legacy_db_migrates_trace_id_column(tmp_path):
    """Pre-migration db (no trace_id column) should be ALTERed and backfilled."""
    db_path = tmp_path / "legacy.db"
    legacy_conn = sqlite3.connect(db_path)
    legacy_conn.execute(
        """
        CREATE TABLE audit_entries (
            entry_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            payload TEXT NOT NULL
        )
        """
    )
    legacy_trace = str(uuid4())
    legacy_payload = json.dumps(
        {
            "entry_id": "audit-legacy",
            "task_id": "task-x",
            "trace_id": legacy_trace,
            "actor": "agent",
            "action": "submit_task",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )
    legacy_conn.execute(
        "INSERT INTO audit_entries (entry_id, task_id, created_at, payload) VALUES (?, ?, ?, ?)",
        ("audit-legacy", "task-x", "2026-01-01T00:00:00+00:00", legacy_payload),
    )
    legacy_conn.commit()
    legacy_conn.close()

    ledger = SQLiteTaskLedger(db_path)
    columns = {
        row["name"]
        for row in ledger._fetch_all("PRAGMA table_info(audit_entries)")
    }
    assert "trace_id" in columns

    trail = ledger.get_audit_trail(legacy_trace)
    assert [entry.entry_id for entry in trail] == ["audit-legacy"]


def test_audit_trail_lookup_stays_fast_with_many_other_rows(tmp_path):
    """Index lookup should keep a single trace below 50ms even with 1000+ noise rows."""
    ledger = SQLiteTaskLedger(tmp_path / "mac.db")
    target = TaskTransfer(task_id="target", title="t", description="t")
    ledger.save_task_transfer(target)

    # Insert 1 entry for the target trace + 1000 noise rows on other traces.
    ledger.record_audit_entry(
        AuditEntry(
            entry_id="audit-target",
            task_id="target",
            trace_id=target.trace_id,
            actor="agent",
            action="submit_task",
        )
    )
    for index in range(1000):
        ledger.record_audit_entry(
            AuditEntry(
                entry_id=f"audit-noise-{index}",
                task_id=f"task-{index}",
                trace_id=f"trace-{index}",
                actor="agent",
                action="noise",
            )
        )

    start = time.perf_counter()
    trail = ledger.get_audit_trail(target.trace_id)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert [entry.entry_id for entry in trail] == ["audit-target"]
    assert elapsed_ms < 50, f"trace lookup took {elapsed_ms:.2f}ms, expected < 50ms"


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
