from fastapi.testclient import TestClient

from mac.protocol.messages import CoordinationPolicy, TaskPayload, TaskTransfer
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger
from mac.transport.http_ws import create_app


def _running_task(task_id: str) -> TaskTransfer:
    return TaskTransfer(
        task_id=task_id,
        source_agent_id="planner",
        target_agent_id="worker",
        status="running",
        payload=TaskPayload(type="write_code", summary=f"Review {task_id}"),
    )


def test_http_review_lifecycle_accepts_and_rejects_tasks(tmp_path):
    registry = Registry(
        SQLiteTaskLedger(tmp_path / "mac.db"),
        policy=CoordinationPolicy(require_review=True),
    )
    client = TestClient(create_app(registry))
    registry.submit_task(_running_task("accept-task"))
    registry.submit_task(_running_task("reject-task"))

    for task_id in ("accept-task", "reject-task"):
        response = client.post(
            f"/tasks/{task_id}/mark-review-ready",
            json={"agent_id": "worker"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "review_ready"

    accepted = client.post(
        "/tasks/accept-task/accept-review",
        json={"agent_id": "reviewer"},
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "completed"

    rejected = client.post(
        "/tasks/reject-task/reject-review",
        json={"reviewer_id": "reviewer", "reason": "needs more tests"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    conflicts = registry.list_conflicts()
    assert len(conflicts) == 1
    assert conflicts[0].description == "needs more tests"


def test_http_mark_review_ready_returns_conflict_when_review_is_disabled(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_running_task("task-1"))
    client = TestClient(create_app(registry))

    response = client.post(
        "/tasks/task-1/mark-review-ready",
        json={"agent_id": "worker"},
    )

    assert response.status_code == 409
    assert "require_review is False" in response.json()["detail"]
