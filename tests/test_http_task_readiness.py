from fastapi.testclient import TestClient

from mac.protocol.messages import ContextBundle, TaskPayload, TaskTransfer
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger
from mac.testing.contracts import TestContract
from mac.transport.http_ws import create_app


def test_http_task_readiness_returns_next_action_and_quality_gaps(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    client = TestClient(create_app(registry))
    task = TaskTransfer(
        task_id="task-1",
        trace_id="trace-1",
        source_agent_id="planner",
        target_agent_id="tester",
        payload=TaskPayload(
            type="write_test",
            summary="Write HTTP readiness tests",
            target_module="mac.transport.http_ws",
            coverage_goal=85,
            risk_level="high",
        ),
        context=ContextBundle(summary="HTTP readiness task"),
        test_contract=TestContract.for_risk("high"),
    )
    assert client.post("/tasks", json=task.model_dump(mode="json")).status_code == 201
    assert client.post("/tasks/task-1/accept", json={"agent_id": "tester"}).status_code == 200
    assert client.post("/tasks/task-1/start", json={"agent_id": "tester"}).status_code == 200
    assert (
        client.post(
            "/tasks/task-1/quality-results",
            json={
                "agent_id": "tester",
                "command": "python -m pytest --cov",
                "status": "passed",
                "evidence": ["test_output"],
            },
        ).status_code
        == 204
    )

    response = client.get("/tasks/task-1/readiness")

    assert response.status_code == 200
    report = response.json()
    assert report["task_id"] == "task-1"
    assert report["next_action"] == "submit_quality_result"
    assert report["quality_allowed"] is False
    assert report["missing_evidence"] == ["coverage_report", "review_notes"]


def test_http_task_readiness_returns_404_for_missing_task(tmp_path):
    client = TestClient(create_app(Registry(SQLiteTaskLedger(tmp_path / "mac.db"))))

    assert client.get("/tasks/missing/readiness").status_code == 404
