import json

from mac.cli import main


def test_cli_task_readiness_prints_next_action_and_quality_gaps(tmp_path, capsys):
    db_path = tmp_path / "mac.db"
    assert (
        main(
            [
                "submit",
                "--db",
                str(db_path),
                "--task-id",
                "task-1",
                "--trace-id",
                "trace-1",
                "--source-agent-id",
                "planner",
                "--target-agent-id",
                "tester",
                "--type",
                "write_test",
                "--summary",
                "Write readiness tests",
                "--target-module",
                "mac.registry",
                "--coverage-goal",
                "85",
                "--risk",
                "high",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["accept", "--db", str(db_path), "--task-id", "task-1", "--agent-id", "tester"]) == 0
    capsys.readouterr()
    assert main(["start", "--db", str(db_path), "--task-id", "task-1", "--agent-id", "tester"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "quality",
                "--db",
                str(db_path),
                "--task-id",
                "task-1",
                "--command",
                "python -m pytest --cov",
                "--status",
                "passed",
                "--evidence",
                "test_output",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["task-readiness", "--db", str(db_path), "--task-id", "task-1"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["task_id"] == "task-1"
    assert report["status"] == "running"
    assert report["next_action"] == "submit_quality_result"
    assert report["quality_allowed"] is False
    assert report["missing_evidence"] == ["coverage_report", "review_notes"]


def test_cli_task_readiness_prints_null_for_missing_task(tmp_path, capsys):
    assert main(["task-readiness", "--db", str(tmp_path / "mac.db"), "--task-id", "missing"]) == 0

    assert json.loads(capsys.readouterr().out) is None
