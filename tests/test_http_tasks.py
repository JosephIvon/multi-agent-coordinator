from fastapi.testclient import TestClient

from mac.protocol.messages import ContextBundle, TaskPayload, TaskTransfer
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger
from mac.transport.http_ws import create_app


def _task(task_id: str, *, target_agent_id: str | None = None, task_type: str = "write_test") -> TaskTransfer:
    payload_kwargs = {"type": task_type, "summary": f"{task_type} task"}
    if task_type == "write_test":
        payload_kwargs.update({"target_module": "mac.transport.http_ws", "coverage_goal": 85})
    if task_type == "code_review":
        payload_kwargs.update({"file_path": "src/mac/transport/http_ws.py", "diff_hunk": "@@ -1 +1 @@"})
    return TaskTransfer(
        task_id=task_id,
        trace_id=f"trace-{task_id}",
        source_agent_id="planner",
        target_agent_id=target_agent_id,
        payload=TaskPayload(**payload_kwargs),
        context=ContextBundle(summary=f"{task_type} task"),
    )


def test_http_lists_tasks_by_capability_and_agent_assignment(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    client = TestClient(create_app(registry))
    for task in [
        _task("task-open"),
        _task("task-assigned", target_agent_id="tester"),
        _task("task-other-agent", target_agent_id="other"),
        _task("task-review", task_type="code_review"),
    ]:
        assert client.post("/tasks", json=task.model_dump(mode="json")).status_code == 201

    response = client.get(
        "/tasks",
        params={"status": "proposed", "capability": "write_test", "agent_id": "tester"},
    )

    assert response.status_code == 200
    assert [task["task_id"] for task in response.json()] == ["task-open", "task-assigned"]


def test_http_get_task_returns_task_or_404(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    client = TestClient(create_app(registry))
    task = _task("task-open")
    assert client.post("/tasks", json=task.model_dump(mode="json")).status_code == 201

    found = client.get("/tasks/task-open")
    missing = client.get("/tasks/missing")

    assert found.status_code == 200
    assert found.json()["task_id"] == "task-open"
    assert missing.status_code == 404
