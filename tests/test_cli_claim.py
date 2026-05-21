import json

from mac.cli import main


def test_cli_claim_returns_matching_task_once(tmp_path, capsys):
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
                "Write tests",
                "--target-module",
                "mac.registry",
                "--coverage-goal",
                "85",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["claim", "--db", str(db_path), "--agent-id", "tester", "--capability", "write_test"]) == 0
    claimed = json.loads(capsys.readouterr().out)
    assert claimed["task_id"] == "task-1"
    assert claimed["status"] == "accepted"
    assert claimed["target_agent_id"] == "tester"
    assert claimed["target_agent_id"] == "tester"

    assert main(["claim", "--db", str(db_path), "--agent-id", "tester", "--capability", "write_test"]) == 0
    assert json.loads(capsys.readouterr().out) is None
