import json
import sys

from mac.cli import main


def test_cli_run_once_claims_executes_and_completes_task(tmp_path, capsys):
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
                "Run local command",
                "--target-module",
                "mac.runner.local",
                "--coverage-goal",
                "80",
            ]
        )
        == 0
    )
    capsys.readouterr()

    exit_code = main(
        [
            "run-once",
            "--db",
            str(db_path),
            "--agent-id",
            "runner",
            "--name",
            "Local Runner",
            "--capability",
            "write_test",
            "--command",
            sys.executable,
            "-c",
            "print('ok')",
        ]
    )

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["task_id"] == "task-1"
    assert output["status"] == "completed"

    assert main(["capability-score", "--db", str(db_path), "--agent-id", "runner", "--capability", "write_test"]) == 0
    score = json.loads(capsys.readouterr().out)
    assert score["succeeded"] == 1


def test_cli_run_once_outputs_null_when_no_task(tmp_path, capsys):
    db_path = tmp_path / "mac.db"

    assert (
        main(
            [
                "run-once",
                "--db",
                str(db_path),
                "--agent-id",
                "runner",
                "--name",
                "Local Runner",
                "--capability",
                "write_test",
                "--command",
                sys.executable,
                "-c",
                "print('ok')",
            ]
        )
        == 0
    )

    assert json.loads(capsys.readouterr().out) is None
