import json
from pathlib import Path

import pytest

from mac import cli
from mac.protocol.messages import TaskPayload, TaskTransfer
from mac.registry import Registry
from mac.storage.sqlite import SQLiteTaskLedger


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    # Three proposed tasks with priorities 2, 5, 9 so the dry-run has data.
    db = tmp_path / "mac.db"
    registry = Registry(SQLiteTaskLedger(db))
    for i, priority in enumerate([2, 5, 9], start=1):
        registry.submit_task(
            TaskTransfer(
                task_id=f"c-{i}",
                source_agent_id="seed",
                payload=TaskPayload(type="noop", summary=f"c {i}"),
                priority=priority,
            )
        )
    return db


def test_scoring_list_reports_builtin_priority(capsys):
    rc = cli.main(['scoring', 'list'])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    names = [s["name"] for s in payload["sync"]]
    assert "priority" in names
    assert payload["async"] == []


def test_scoring_test_dry_runs_against_proposed_tasks(capsys, seeded_db):
    rc = cli.main(['scoring', 'test', '--name', 'priority', '--db', str(seeded_db), '--limit', '10'])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["scorer"] == "priority"
    by_id = {entry["task_id"]: entry for entry in payload["scored"]}
    assert by_id["c-1"]["score"] == 2.0
    assert by_id["c-2"]["score"] == 5.0
    assert by_id["c-3"]["score"] == 9.0


def test_scoring_test_limit_truncates_results(capsys, seeded_db):
    rc = cli.main(['scoring', 'test', '--name', 'priority', '--db', str(seeded_db), '--limit', '1'])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert len(payload["scored"]) == 1
    assert payload["scored"][0]["task_id"] == "c-1"


def test_scoring_test_unknown_scorer_returns_zeros(capsys, seeded_db):
    # CLI subprocess builds a fresh Registry, so unknown scorers degrade to 0
    # rather than crash — the operator still sees the proposed tasks.
    rc = cli.main(['scoring', 'test', '--name', 'definitely_not_registered', '--db', str(seeded_db)])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["scorer"] == "definitely_not_registered"
    assert all(entry["score"] == 0.0 for entry in payload["scored"])


def test_scoring_test_with_empty_db_returns_empty_list(capsys, tmp_path: Path):
    db = tmp_path / "empty.db"
    rc = cli.main(['scoring', 'test', '--name', 'priority', '--db', str(db)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scored"] == []

