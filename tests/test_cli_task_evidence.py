import json

from mac.cli import main


def test_cli_task_evidence_prints_task_quality_and_audit(tmp_path, capsys):
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
                "Write evidence tests",
                "--target-module",
                "mac.registry",
                "--coverage-goal",
                "85",
                "--risk",
                "low",
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
                "pytest related tests or smoke test",
                "--status",
                "passed",
                "--evidence",
                "test_output",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["complete", "--db", str(db_path), "--task-id", "task-1", "--agent-id", "tester"]) == 0
    capsys.readouterr()

    assert main(["task-evidence", "--db", str(db_path), "--task-id", "task-1"]) == 0
    bundle = json.loads(capsys.readouterr().out)

    assert bundle["task_id"] == "task-1"
    assert bundle["task"]["status"] == "completed"
    assert bundle["execution_agent_id"] == "tester"
    assert bundle["quality_results"][0]["evidence"] == ["test_output"]
    assert [entry["action"] for entry in bundle["audit_trail"]] == [
        "submit_task",
        "accept_handoff",
        "start_task",
        "submit_quality_result",
        "complete_task",
    ]
    assert bundle["required_capability"] == "write_test"
    assert bundle["observed_capability_score"]["agent_id"] == "tester"
    assert bundle["observed_capability_score"]["total"] == 0


def test_cli_task_evidence_prints_null_for_missing_task(tmp_path, capsys):
    assert main(["task-evidence", "--db", str(tmp_path / "mac.db"), "--task-id", "missing"]) == 0

    assert json.loads(capsys.readouterr().out) is None
