import json

from mac.cli import main
from mac.protocol.messages import TaskPayload, TaskTransfer
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger


def _running_task(task_id: str) -> TaskTransfer:
    return TaskTransfer(
        task_id=task_id,
        source_agent_id="planner",
        target_agent_id="worker",
        status="running",
        payload=TaskPayload(type="write_code", summary=f"Review {task_id}"),
    )


def test_cli_review_lifecycle_accepts_and_rejects_tasks(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("MAC_REQUIRE_REVIEW", "true")
    db_path = tmp_path / "mac.db"
    registry = Registry(SQLiteTaskLedger(db_path))
    registry.submit_task(_running_task("accept-task"))
    registry.submit_task(_running_task("reject-task"))

    for task_id in ("accept-task", "reject-task"):
        assert (
            main(
                [
                    "review-lifecycle",
                    "--db",
                    str(db_path),
                    "--action",
                    "mark-ready",
                    "--task-id",
                    task_id,
                    "--agent-id",
                    "worker",
                ]
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out)["status"] == "review_ready"

    assert (
        main(
            [
                "review-lifecycle",
                "--db",
                str(db_path),
                "--action",
                "accept",
                "--task-id",
                "accept-task",
                "--reviewer-id",
                "reviewer",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "completed"

    assert (
        main(
            [
                "review-lifecycle",
                "--db",
                str(db_path),
                "--action",
                "reject",
                "--task-id",
                "reject-task",
                "--reviewer-id",
                "reviewer",
                "--reason",
                "needs more tests",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "rejected"

    conflicts = Registry(SQLiteTaskLedger(db_path)).list_conflicts()
    assert len(conflicts) == 1
    assert conflicts[0].description == "needs more tests"
