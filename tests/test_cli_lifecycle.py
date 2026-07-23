import json

from mac.cli import main


def test_cli_runs_local_handoff_lifecycle(tmp_path, capsys):
    db_path = tmp_path / "mac.db"

    assert main(["register", "--db", str(db_path), "--agent-id", "tester", "--name", "Tester", "--capability", "write_test"]) == 0
    capsys.readouterr()

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
                "Write registry lifecycle tests",
                "--target-module",
                "mac.registry",
                "--coverage-goal",
                "85",
                "--risk",
                "high",
                "--context-ref",
                "file://src/mac/registry.py",
            ]
        )
        == 0
    )
    submitted = json.loads(capsys.readouterr().out)
    assert submitted["status"] == "proposed"

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
                "--evidence",
                "coverage_report",
                "--evidence",
                "review_notes",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["complete", "--db", str(db_path), "--task-id", "task-1", "--agent-id", "tester"]) == 0
    completed = json.loads(capsys.readouterr().out)
    assert completed["status"] == "completed"

    assert main(["audit", "--db", str(db_path), "--trace-id", "trace-1"]) == 0
    audit = json.loads(capsys.readouterr().out)
    assert [entry["action"] for entry in audit] == [
        "submit_task",
        "accept_handoff",
        "start_task",
        "submit_quality_result",
        "complete_task",
    ]


def test_cli_runs_failure_recovery_commands(tmp_path, capsys):
    db_path = tmp_path / "mac.db"
    assert (
        main(
            [
                "submit",
                "--db",
                str(db_path),
                "--task-id",
                "task-recover",
                "--trace-id",
                "trace-recover",
                "--source-agent-id",
                "planner",
                "--target-agent-id",
                "tester",
                "--type",
                "write_test",
                "--summary",
                "Recover failed task",
                "--target-module",
                "mac.registry",
                "--coverage-goal",
                "80",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["accept", "--db", str(db_path), "--task-id", "task-recover", "--agent-id", "tester"]) == 0
    capsys.readouterr()
    assert main(["start", "--db", str(db_path), "--task-id", "task-recover", "--agent-id", "tester"]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "checkpoint",
                "--db",
                str(db_path),
                "--task-id",
                "task-recover",
                "--agent-id",
                "tester",
                "--summary",
                "halfway done",
                "--artifact-ref",
                "file://checkpoint.log",
            ]
        )
        == 0
    )
    checkpointed = json.loads(capsys.readouterr().out)
    assert checkpointed["metadata"]["checkpoints"][0]["summary"] == "halfway done"

    assert (
        main(
            [
                "fail",
                "--db",
                str(db_path),
                "--task-id",
                "task-recover",
                "--agent-id",
                "tester",
                "--error-code",
                "HANDLER_ERROR",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        main(
            [
                "retry",
                "--db",
                str(db_path),
                "--task-id",
                "task-recover",
                "--agent-id",
                "planner",
                "--fallback-agent-id",
                "fallback",
            ]
        )
        == 0
    )
    retried = json.loads(capsys.readouterr().out)
    assert retried["status"] == "proposed"
    assert retried["target_agent_id"] == "fallback"
    assert retried["retry_count"] == 1

    assert (
        main(
            [
                "cancel",
                "--db",
                str(db_path),
                "--task-id",
                "task-recover",
                "--agent-id",
                "planner",
                "--reason",
                "obsolete",
            ]
        )
        == 0
    )
    cancelled = json.loads(capsys.readouterr().out)
    assert cancelled["status"] == "cancelled"


def test_cli_done_completes_task_in_one_step(tmp_path, capsys):
    """mac-agent done: quality + handoff + complete in one command."""
    db_path = tmp_path / "mac.db"

    assert main(["register", "--db", str(db_path), "--agent-id", "dev", "--name", "Dev", "--capability", "write_code"]) == 0
    capsys.readouterr()

    assert main([
        "submit", "--db", str(db_path), "--task-id", "task-done",
        "--source-agent-id", "planner", "--type", "write_code",
        "--summary", "Done command test",
    ]) == 0
    capsys.readouterr()

    assert main(["accept", "--db", str(db_path), "--task-id", "task-done", "--agent-id", "dev"]) == 0
    capsys.readouterr()
    assert main(["start", "--db", str(db_path), "--task-id", "task-done", "--agent-id", "dev"]) == 0
    capsys.readouterr()

    # done with quality + handoff in one step
    assert main([
        "done", "--db", str(db_path), "--task-id", "task-done", "--agent-id", "dev",
        "--quality-command", "pytest -q", "--quality-status", "passed",
        "--changed-file", "src/main.py", "--risk", "manual test needed",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "completed"
    assert result["task_id"] == "task-done"
    assert result["quality_gate"] == "passed"
    assert result["review"] is False


def test_cli_done_minimal_no_quality_no_handoff(tmp_path, capsys):
    """mac-agent done with just task-id + agent-id (no quality, no handoff)."""
    db_path = tmp_path / "mac.db"

    assert main(["register", "--db", str(db_path), "--agent-id", "dev", "--name", "Dev", "--capability", "write_code"]) == 0
    capsys.readouterr()

    assert main([
        "submit", "--db", str(db_path), "--task-id", "task-min",
        "--source-agent-id", "planner", "--type", "write_code",
        "--summary", "Minimal done test",
    ]) == 0
    capsys.readouterr()

    assert main(["accept", "--db", str(db_path), "--task-id", "task-min", "--agent-id", "dev"]) == 0
    capsys.readouterr()
    assert main(["start", "--db", str(db_path), "--task-id", "task-min", "--agent-id", "dev"]) == 0
    capsys.readouterr()

    # done with minimal args — no test contract, so gate passes
    assert main(["done", "--db", str(db_path), "--task-id", "task-min", "--agent-id", "dev"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "completed"
    assert result["quality_gate"] == "passed"
