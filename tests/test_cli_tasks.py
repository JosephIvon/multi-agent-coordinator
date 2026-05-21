import json

from mac.cli import main


def _submit_write_test(capsys, db_path, task_id: str, *, target_agent_id: str | None = None) -> None:
    args = [
        "submit",
        "--db",
        str(db_path),
        "--task-id",
        task_id,
        "--trace-id",
        f"trace-{task_id}",
        "--source-agent-id",
        "planner",
        "--type",
        "write_test",
        "--summary",
        f"Write tests for {task_id}",
        "--target-module",
        "mac.registry",
        "--coverage-goal",
        "85",
    ]
    if target_agent_id is not None:
        args.extend(["--target-agent-id", target_agent_id])
    assert main(args) == 0
    capsys.readouterr()


def test_cli_tasks_lists_claimable_tasks_by_capability_and_agent(tmp_path, capsys):
    db_path = tmp_path / "mac.db"
    _submit_write_test(capsys, db_path, "task-open")
    _submit_write_test(capsys, db_path, "task-assigned", target_agent_id="tester")
    _submit_write_test(capsys, db_path, "task-other-agent", target_agent_id="other")

    assert (
        main(
            [
                "tasks",
                "--db",
                str(db_path),
                "--status",
                "proposed",
                "--capability",
                "write_test",
                "--agent-id",
                "tester",
            ]
        )
        == 0
    )
    tasks = json.loads(capsys.readouterr().out)

    assert [task["task_id"] for task in tasks] == ["task-open", "task-assigned"]


def test_cli_tasks_can_list_all_statuses_when_status_is_omitted(tmp_path, capsys):
    db_path = tmp_path / "mac.db"
    _submit_write_test(capsys, db_path, "task-open")
    _submit_write_test(capsys, db_path, "task-assigned", target_agent_id="tester")

    assert main(["claim", "--db", str(db_path), "--agent-id", "tester", "--capability", "write_test"]) == 0
    capsys.readouterr()

    assert main(["tasks", "--db", str(db_path), "--capability", "write_test"]) == 0
    tasks = json.loads(capsys.readouterr().out)

    assert [task["task_id"] for task in tasks] == ["task-assigned", "task-open"]
    assert {task["status"] for task in tasks} == {"accepted", "proposed"}
