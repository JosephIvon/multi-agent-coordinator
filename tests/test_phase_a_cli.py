import json

from mac.cli import main


def test_cli_plan_ready_handoff_conflict_and_packets(tmp_path, capsys):
    db_path = tmp_path / "mac.db"

    assert main(["plan", "create", "--db", str(db_path), "--plan-id", "plan-1", "--goal", "Ship Phase A", "--created-by", "planner"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["plan_id"] == "plan-1"

    assert main(["plan", "activate", "--db", str(db_path), "--plan-id", "plan-1"]) == 0
    capsys.readouterr()

    assert main([
        "register",
        "--db",
        str(db_path),
        "--agent-id",
        "worker",
        "--name",
        "Worker",
        "--capability",
        "write_code",
        "--allowed-path",
        "src/**",
    ]) == 0
    capsys.readouterr()

    assert main([
        "submit",
        "--db",
        str(db_path),
        "--task-id",
        "task-1",
        "--source-agent-id",
        "planner",
        "--type",
        "write_code",
        "--summary",
        "Implement packet generation",
        "--plan-id",
        "plan-1",
    ]) == 0
    capsys.readouterr()

    assert main(["ready-tasks", "--db", str(db_path), "--capability", "write_code"]) == 0
    ready = json.loads(capsys.readouterr().out)
    assert [task["task_id"] for task in ready] == ["task-1"]

    assert main([
        "handoff",
        "--db",
        str(db_path),
        "--task-id",
        "task-1",
        "--agent-id",
        "worker",
        "--verification",
        "python -m pytest -q:pass:unit suite",
        "--changed-file",
        "src/mac/registry.py",
        "--risk",
        "needs project pilot",
    ]) == 0
    handoff = json.loads(capsys.readouterr().out)
    assert handoff["verification"][0]["result"] == "pass"

    assert main([
        "record-conflict",
        "--db",
        str(db_path),
        "--conflict-id",
        "conflict-1",
        "--plan-id",
        "plan-1",
        "--task-id",
        "task-1",
        "--source",
        "manual",
        "--description",
        "Review wording",
    ]) == 0
    capsys.readouterr()

    assert main(["conflicts", "--db", str(db_path), "--plan-id", "plan-1", "--unresolved"]) == 0
    conflicts = json.loads(capsys.readouterr().out)
    assert conflicts[0]["conflict_id"] == "conflict-1"

    assert main(["resolve-conflict", "--db", str(db_path), "--conflict-id", "conflict-1", "--resolution", "accepted"]) == 0
    resolved = json.loads(capsys.readouterr().out)
    assert resolved["resolved"] is True

    assert main(["worker-packet", "--db", str(db_path), "--task-id", "task-1", "--agent-id", "worker"]) == 0
    assert "Worker Task: task-1" in capsys.readouterr().out

    assert main(["review-packet", "--db", str(db_path), "--task-id", "task-1"]) == 0
    assert "Review Task: task-1" in capsys.readouterr().out
