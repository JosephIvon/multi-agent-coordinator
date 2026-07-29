"""Regression tests for the user-facing CLI fixes shipped in this change set.

Three small UX gaps closed:
1. `audit` now accepts `--task-id` and auto-resolves the trace id, in
   addition to `--trace-id`. The two flags are mutually exclusive.
2. `record-conflict` accepts `--reason` as a friendlier alias for
   `--description`. The two flags are mutually exclusive.
3. `plan` (bare) defaults to `plan list` so the most common operation
   does not require a subcommand.
"""
from __future__ import annotations

import json

import pytest

from mac.cli import main


# ---------------------------------------------------------------------------
# audit --task-id
# ---------------------------------------------------------------------------


def _bootstrap_task_with_audit_trail(tmp_path, capsys):
    """Submit a task so audit trail has at least one entry to find."""
    db = tmp_path / "mac.db"

    # submit -> audit_trail will record submit_task
    assert main([
        "submit",
        "--db", str(db),
        "--task-id", "T-AUDIT-1",
        "--source-agent-id", "test",
        "--type", "smoke",
        "--summary", "for audit test",
    ]) == 0
    # Drain stdout so it doesn't leak into the next command's capture.
    capsys.readouterr()
    return db


def test_audit_accepts_task_id_and_resolves_trace_id(tmp_path, capsys):
    db = _bootstrap_task_with_audit_trail(tmp_path, capsys)

    exit_code = main(["audit", "--db", str(db), "--task-id", "T-AUDIT-1"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert len(payload) >= 1
    # Every entry should be from the same trace as the task.
    assert {entry["trace_id"] for entry in payload} == {payload[0]["trace_id"]}
    assert payload[0]["action"] == "submit_task"


def test_audit_still_accepts_trace_id(tmp_path, capsys):
    db = _bootstrap_task_with_audit_trail(tmp_path, capsys)

    # Find the trace id via status, then audit by trace id.
    assert main(["status", "--db", str(db), "--task-id", "T-AUDIT-1"]) == 0
    status = json.loads(capsys.readouterr().out)
    trace_id = status["trace_id"]

    exit_code = main(["audit", "--db", str(db), "--trace-id", trace_id])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["trace_id"] == trace_id


def test_audit_rejects_missing_task_id(tmp_path, capsys):
    db = _bootstrap_task_with_audit_trail(tmp_path, capsys)

    exit_code = main(["audit", "--db", str(db), "--task-id", "NOPE"])

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"error": "task_not_found", "task_id": "NOPE"}


def test_audit_requires_either_trace_id_or_task_id(tmp_path, capsys):
    db = _bootstrap_task_with_audit_trail(tmp_path, capsys)

    # argparse exits with code 2 on usage errors; main() forwards it.
    with pytest.raises(SystemExit) as exc_info:
        main(["audit", "--db", str(db)])
    assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# record-conflict --reason
# ---------------------------------------------------------------------------


def _registered_agent(db, capsys):
    assert main([
        "register",
        "--db", str(db),
        "--agent-id", "agent-x",
        "--name", "Agent X",
        "--capability", "smoke",
    ]) == 0
    capsys.readouterr()


def test_record_conflict_accepts_reason_alias(tmp_path, capsys):
    db = tmp_path / "mac.db"
    _registered_agent(db, capsys)

    exit_code = main([
        "record-conflict",
        "--db", str(db),
        "--task-id", "T-CONFLICT-1",
        "--source", "fix-verification",
        "--reason", "smoke-test --reason alias",
        "--agent", "agent-x",
    ])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["description"] == "smoke-test --reason alias"
    assert payload["task_id"] == "T-CONFLICT-1"
    assert payload["involved_agents"] == ["agent-x"]


def test_record_conflict_accepts_description_canonical(tmp_path, capsys):
    db = tmp_path / "mac.db"
    _registered_agent(db, capsys)

    exit_code = main([
        "record-conflict",
        "--db", str(db),
        "--task-id", "T-CONFLICT-2",
        "--source", "fix-verification",
        "--description", "canonical flag still works",
    ])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["description"] == "canonical flag still works"


# ---------------------------------------------------------------------------
# plan (bare)
# ---------------------------------------------------------------------------


def test_bare_plan_defaults_to_list(tmp_path, capsys):
    db = tmp_path / "mac.db"

    # bare `plan` should work, equivalent to `plan list`
    assert main(["plan", "--db", str(db)]) == 0
    bare = json.loads(capsys.readouterr().out)
    assert bare == []


def test_plan_list_still_works_explicitly(tmp_path, capsys):
    db = tmp_path / "mac.db"

    assert main(["plan", "list", "--db", str(db)]) == 0
    explicit = json.loads(capsys.readouterr().out)
    assert explicit == []


def test_plan_list_filter_by_status(tmp_path, capsys):
    db = tmp_path / "mac.db"

    assert main(["plan", "list", "--db", str(db), "--status", "active"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == []
