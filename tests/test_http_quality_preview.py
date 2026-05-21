from fastapi.testclient import TestClient

from mac.protocol.messages import ContextBundle, TaskPayload, TaskTransfer
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger
from mac.testing.contracts import TestContract
from mac.transport.http_ws import create_app


def test_http_quality_preview_returns_missing_evidence(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    client = TestClient(create_app(registry))
    task = TaskTransfer(
        task_id="task-1",
        trace_id="trace-1",
        source_agent_id="planner",
        payload=TaskPayload(
            type="write_test",
            summary="Write HTTP quality preview tests",
            target_module="mac.transport.http_ws",
            coverage_goal=85,
            risk_level="high",
        ),
        context=ContextBundle(summary="HTTP quality preview task"),
        test_contract=TestContract.for_risk("high"),
    )
    assert client.post("/tasks", json=task.model_dump(mode="json")).status_code == 201
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

    response = client.get("/tasks/task-1/quality-preview")

    assert response.status_code == 200
    preview = response.json()
    assert preview["task_id"] == "task-1"
    assert preview["allowed"] is False
    assert preview["reason"] == "missing_evidence:coverage_report,review_notes"
    assert preview["missing_evidence"] == ["coverage_report", "review_notes"]


def test_http_quality_preview_returns_404_for_missing_task(tmp_path):
    client = TestClient(create_app(Registry(SQLiteTaskLedger(tmp_path / "mac.db"))))

    assert client.get("/tasks/missing/quality-preview").status_code == 404
