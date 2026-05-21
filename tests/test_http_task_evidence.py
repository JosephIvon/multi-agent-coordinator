from fastapi.testclient import TestClient

from mac.protocol.messages import ContextBundle, TaskPayload, TaskTransfer
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger
from mac.testing.contracts import TestContract
from mac.transport.http_ws import create_app


def test_http_task_evidence_returns_task_quality_and_audit(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    client = TestClient(create_app(registry))
    task = TaskTransfer(
        task_id="task-1",
        trace_id="trace-1",
        source_agent_id="planner",
        target_agent_id="tester",
        payload=TaskPayload(
            type="write_test",
            summary="Write HTTP evidence tests",
            target_module="mac.transport.http_ws",
            coverage_goal=80,
            risk_level="low",
        ),
        context=ContextBundle(summary="HTTP evidence bundle task"),
        test_contract=TestContract.for_risk("low"),
    )
    assert client.post("/tasks", json=task.model_dump(mode="json")).status_code == 201
    assert client.post("/tasks/task-1/accept", json={"agent_id": "tester"}).status_code == 200
    assert client.post("/tasks/task-1/start", json={"agent_id": "tester"}).status_code == 200
    assert (
        client.post(
            "/tasks/task-1/quality-results",
            json={
                "agent_id": "tester",
                "command": "pytest related tests or smoke test",
                "status": "passed",
                "evidence": ["test_output"],
            },
        ).status_code
        == 204
    )
    assert client.post("/tasks/task-1/complete", json={"agent_id": "tester"}).status_code == 200

    response = client.get("/tasks/task-1/evidence")

    assert response.status_code == 200
    bundle = response.json()
    assert bundle["task_id"] == "task-1"
    assert bundle["task"]["status"] == "completed"
    assert bundle["execution_agent_id"] == "tester"
    assert bundle["required_capability"] == "write_test"
    assert bundle["observed_capability_score"]["agent_id"] == "tester"
    assert bundle["observed_capability_score"]["total"] == 0
    assert bundle["quality_results"][0]["command"] == "pytest related tests or smoke test"
    assert [entry["action"] for entry in bundle["audit_trail"]] == [
        "submit_task",
        "accept_handoff",
        "start_task",
        "submit_quality_result",
        "complete_task",
    ]


def test_http_task_evidence_returns_404_for_missing_task(tmp_path):
    client = TestClient(create_app(Registry(SQLiteTaskLedger(tmp_path / "mac.db"))))

    assert client.get("/tasks/missing/evidence").status_code == 404
