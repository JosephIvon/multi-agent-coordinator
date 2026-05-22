from fastapi.testclient import TestClient

from mac.protocol.messages import AgentCapability, AgentCard, TaskPayload, TaskTransfer
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger
from mac.transport.http_ws import create_app


def test_http_phase_a_collaboration_endpoints(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    client = TestClient(create_app(registry))

    created = client.post("/plans", json={"plan_id": "plan-1", "goal": "Ship Phase A", "created_by": "planner"})
    assert created.status_code == 201
    assert created.json()["status"] == "draft"
    assert client.post("/plans/plan-1/activate").json()["status"] == "active"

    agent = AgentCard(
        agent_id="worker",
        name="Worker",
        capabilities=[AgentCapability(name="write_code")],
        allowed_paths=["src/**"],
    )
    assert client.post("/agents/register", json=agent.model_dump(mode="json")).status_code == 201

    task = TaskTransfer(
        task_id="task-1",
        plan_id="plan-1",
        payload=TaskPayload(type="write_code", summary="Implement collaboration endpoints"),
    )
    assert client.post("/tasks", json=task.model_dump(mode="json")).status_code == 201

    ready = client.get("/tasks/ready", params={"capability": "write_code"}).json()
    assert [item["task_id"] for item in ready] == ["task-1"]

    handoff_response = client.post(
        "/handoffs",
        json={
            "task_id": "task-1",
            "plan_id": "plan-1",
            "agent_id": "worker",
            "verification": [{"command": "python -m pytest -q", "result": "pass"}],
            "changed_files": ["src/mac/registry.py"],
            "risks": ["needs real project pilot"],
        },
    )
    assert handoff_response.status_code == 201
    assert client.get("/tasks/task-1/handoff").json()["agent_id"] == "worker"

    conflict_response = client.post(
        "/conflicts",
        json={
            "conflict_id": "conflict-1",
            "plan_id": "plan-1",
            "task_id": "task-1",
            "source": "manual",
            "description": "Review endpoint names",
        },
    )
    assert conflict_response.status_code == 201
    assert client.get("/conflicts", params={"plan_id": "plan-1", "resolved": False}).json()[0]["conflict_id"] == "conflict-1"
    assert client.post("/conflicts/conflict-1/resolve", json={"resolution": "accepted"}).json()["resolved"] is True

    assert "Worker Task: task-1" in client.get("/tasks/task-1/worker-packet", params={"agent_id": "worker"}).text
    assert "Review Task: task-1" in client.get("/tasks/task-1/review-packet").text
