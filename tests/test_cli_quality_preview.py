import json

from mac.cli import main


def test_cli_quality_preview_prints_missing_evidence(tmp_path, capsys):
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
                "--type",
                "write_test",
                "--summary",
                "Write quality preview tests",
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

    assert main(["quality-preview", "--db", str(db_path), "--task-id", "task-1"]) == 0
    preview = json.loads(capsys.readouterr().out)

    assert preview["task_id"] == "task-1"
    assert preview["allowed"] is False
    assert preview["reason"] == "missing_evidence:coverage_report,review_notes"
    assert preview["missing_evidence"] == ["coverage_report", "review_notes"]


def test_cli_quality_preview_prints_null_for_missing_task(tmp_path, capsys):
    assert main(["quality-preview", "--db", str(tmp_path / "mac.db"), "--task-id", "missing"]) == 0

    assert json.loads(capsys.readouterr().out) is None
