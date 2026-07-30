import json

import pytest

import mac.mcp_server as mcp_server
from mac.protocol.messages import TaskPayload, TaskTransfer
from mac.registry import Registry
from mac.storage.sqlite import SQLiteTaskLedger


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    # Redirect MCP server to tmp DB and reset memoised registry.
    db = tmp_path / "mac.db"
    monkeypatch.setattr(mcp_server, "_DB_PATH", db)
    monkeypatch.setattr(mcp_server, "_LONG_REGISTRY", None)
    return db


@pytest.fixture
def seed_proposed(tmp_db):
    # Write three proposed tasks so dry-runs have something to score.
    registry = Registry(SQLiteTaskLedger(tmp_db))
    seeds = [
        TaskTransfer(
            task_id=f"t-{i}",
            source_agent_id="seed",
            payload=TaskPayload(type="noop", summary=f"task {i}"),
            priority=priority,
        )
        for i, priority in enumerate([2, 5, 9], start=1)
    ]
    for task in seeds:
        registry.submit_task(task)
    return seeds


def test_list_scorers_reports_builtin_priority(tmp_db):
    payload = json.loads(mcp_server.mac_list_scorers())
    assert any(s["name"] == "priority" for s in payload["sync"])
    assert payload["async"] == []


def test_set_scorer_priority_activates_hook_on_long_registry(tmp_db):
    result = json.loads(mcp_server.mac_set_scorer("priority"))
    assert result["name"] == "priority"
    assert result["sync_installed"] is True
    assert result["async_installed"] is False
    assert result["active_scorer_id"].endswith("@sync")


def test_set_scorer_clears_hook_with_empty_name(tmp_db):
    mcp_server.mac_set_scorer("priority")
    result = json.loads(mcp_server.mac_set_scorer(""))
    assert result["sync_installed"] is False
    assert result["async_installed"] is False
    assert result["active_scorer_id"] is None


def test_set_scorer_unknown_name_surfaces_value_error(tmp_db):
    with pytest.raises(ValueError, match="unknown scoring_fn name"):
        mcp_server.mac_set_scorer("definitely_not_registered")


def test_test_scorer_dry_runs_against_proposed_tasks(tmp_db, seed_proposed):
    payload = json.loads(mcp_server.mac_test_scorer("priority", limit=10))
    assert payload["scorer"] == "priority"
    by_id = {entry["task_id"]: entry for entry in payload["scored"]}
    assert by_id["t-1"]["score"] == 2.0
    assert by_id["t-2"]["score"] == 5.0
    assert by_id["t-3"]["score"] == 9.0


def test_test_scorer_limit_truncates_results(tmp_db, seed_proposed):
    payload = json.loads(mcp_server.mac_test_scorer("priority", limit=1))
    assert len(payload["scored"]) == 1
    assert payload["scored"][0]["task_id"] == "t-1"


def test_test_scorer_does_not_pollute_long_registry(tmp_db, seed_proposed):
    # Activate a hook on the long-lived registry so any cache pollution
    # would surface as the hook being missing or as a stale ranking.
    long_registry = mcp_server._long_registry()
    long_registry.set_scoring_fn("priority")
    # mac_test_scorer must not touch the long-lived registry at all.
    mcp_server.mac_test_scorer("priority", limit=5)
    assert long_registry._scoring_fn_id is not None
    assert long_registry._scoring_fn_id.endswith("@sync")
    # Now add a fresh high-priority task and confirm the live registry
    # still applies the active hook to it (would not be true if the
    # test path had cleared the hook as a side effect).
    fresh = TaskTransfer(
        task_id="t-fresh",
        source_agent_id="seed",
        payload=TaskPayload(type="noop", summary="fresh"),
        priority=10,
    )
    Registry(SQLiteTaskLedger(tmp_db)).submit_task(fresh)
    long_registry = mcp_server._long_registry()
    ranked = [t.task_id for t in long_registry.list_ready_tasks()]
    assert ranked[0] == "t-fresh"

