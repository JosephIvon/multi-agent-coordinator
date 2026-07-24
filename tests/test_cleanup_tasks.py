from mac.mcp_server import mac_cleanup_tasks
from mac.protocol.messages import AgentCapability, AgentCard, ContextBundle, TaskPayload, TaskTransfer
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger


def _registry_with_tasks(tmp_path, statuses):
    ledger = SQLiteTaskLedger(tmp_path / "mac.db")
    registry = Registry(ledger)
    for i, status in enumerate(statuses):
        task = TaskTransfer(
            task_id=f"t{i}",
            trace_id=f"t{i}",
            source_agent_id="a1",
            status=status,
            payload=TaskPayload(type="test", summary=f"task {i}"),
            context=ContextBundle(summary=f"task {i}"),
        )
        registry.ledger.save_task_transfer(task)
    return registry


def test_cleanup_tasks_deletes_terminal_only(tmp_path):
    registry = _registry_with_tasks(tmp_path, ["failed", "cancelled", "completed", "running"])
    deleted = registry.cleanup_tasks()
    assert len(deleted) == 2
    deleted_ids = {t.task_id for t in deleted}
    assert "t0" in deleted_ids
    assert "t1" in deleted_ids
    assert registry.get_task("t2") is not None
    assert registry.get_task("t3") is not None


def test_cleanup_tasks_with_status_filter(tmp_path):
    registry = _registry_with_tasks(tmp_path, ["failed", "cancelled"])
    deleted = registry.cleanup_tasks(statuses=["failed"])
    assert len(deleted) == 1
    assert deleted[0].task_id == "t0"


def test_cleanup_tasks_with_plan_filter(tmp_path):
    registry = _registry_with_tasks(tmp_path, ["failed", "failed"])
    task = registry.get_task("t0")
    from mac.protocol.messages import Plan
    plan = registry.create_plan(goal="test plan")
    task = task.model_copy(update={"plan_id": plan.plan_id})
    registry.ledger.save_task_transfer(task)
    deleted = registry.cleanup_tasks(plan_id=plan.plan_id)
    assert len(deleted) == 1
    assert deleted[0].task_id == "t0"


def test_cleanup_tasks_audit_trail(tmp_path):
    registry = _registry_with_tasks(tmp_path, ["failed"])
    registry.cleanup_tasks()
    trail = registry.get_audit_trail("t0")
    actions = [e.action for e in trail]
    assert "cleanup_task" in actions


def test_cleanup_cli(tmp_path):
    from mac.cli import main

    registry = _registry_with_tasks(tmp_path, ["failed", "cancelled"])
    db = str(tmp_path / "mac.db")
    rc = main(["cleanup", "--db", db])
    assert rc == 0
    assert registry.get_task("t0") is None
    assert registry.get_task("t1") is None


def test_cleanup_cli_with_status_filter(tmp_path):
    from mac.cli import main

    registry = _registry_with_tasks(tmp_path, ["failed", "cancelled"])
    db = str(tmp_path / "mac.db")
    rc = main(["cleanup", "--db", db, "--status", "failed"])
    assert rc == 0
    assert registry.get_task("t0") is None
    assert registry.get_task("t1") is not None


def test_cleanup_http_endpoint(tmp_path):
    from fastapi.testclient import TestClient
    from mac.transport.http_ws import create_app

    registry = _registry_with_tasks(tmp_path, ["failed", "cancelled", "completed"])
    client = TestClient(create_app(registry))

    response = client.post("/tasks/cleanup", json={})
    assert response.status_code == 200
    deleted = response.json()
    assert len(deleted) == 2

    assert registry.get_task("t0") is None
    assert registry.get_task("t1") is None
    assert registry.get_task("t2") is not None


def test_cleanup_http_with_status_filter(tmp_path):
    from fastapi.testclient import TestClient
    from mac.transport.http_ws import create_app

    registry = _registry_with_tasks(tmp_path, ["failed", "cancelled"])
    client = TestClient(create_app(registry))

    response = client.post("/tasks/cleanup", json={"statuses": ["cancelled"]})
    assert response.status_code == 200
    deleted = response.json()
    assert len(deleted) == 1
    assert deleted[0]["task_id"] == "t1"


def test_cleanup_mcp_tool(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    registry = _registry_with_tasks(tmp_path, ["failed", "cancelled"])
    monkeypatch.setattr("mac.mcp_server._DB_PATH", tmp_path / "mac.db")

    result = mac_cleanup_tasks()
    import json
    deleted = json.loads(result)
    assert len(deleted) == 2