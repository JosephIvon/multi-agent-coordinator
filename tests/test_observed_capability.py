import json

from mac.cli import main
from mac.protocol.messages import AgentCapability, AgentCard
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger


def test_registry_records_and_summarizes_observed_capability(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))

    registry.record_task_outcome(
        agent_id="tester",
        capability="write_test",
        task_type="write_test",
        status="succeeded",
        duration_seconds=12.5,
    )
    registry.record_task_outcome(
        agent_id="tester",
        capability="write_test",
        task_type="write_test",
        status="failed",
        duration_seconds=20,
        error_code="QUALITY_GATE_FAILED",
    )

    score = registry.get_capability_score("tester", "write_test")

    assert score == {
        "agent_id": "tester",
        "capability": "write_test",
        "total": 2,
        "succeeded": 1,
        "failed": 1,
        "success_rate": 0.5,
        "average_duration_seconds": 16.25,
        "last_error_code": "QUALITY_GATE_FAILED",
    }


def test_cli_records_and_reads_capability_score(tmp_path, capsys):
    db_path = tmp_path / "mac.db"

    assert (
        main(
            [
                "observe",
                "--db",
                str(db_path),
                "--agent-id",
                "tester",
                "--capability",
                "write_test",
                "--task-type",
                "write_test",
                "--status",
                "succeeded",
                "--duration",
                "10",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["capability-score", "--db", str(db_path), "--agent-id", "tester", "--capability", "write_test"]) == 0
    output = json.loads(capsys.readouterr().out)

    assert output["success_rate"] == 1.0
    assert output["average_duration_seconds"] == 10.0


def test_discover_orders_candidates_by_observed_success_rate(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.register(
        AgentCard(
            agent_id="fast-but-flaky",
            name="Fast but flaky",
            capabilities=[AgentCapability(name="write_test")],
            load=1,
        )
    )
    registry.register(
        AgentCard(
            agent_id="slower-but-reliable",
            name="Slower but reliable",
            capabilities=[AgentCapability(name="write_test")],
            load=50,
        )
    )
    registry.record_task_outcome(
        agent_id="fast-but-flaky",
        capability="write_test",
        task_type="write_test",
        status="failed",
        duration_seconds=1,
        error_code="QUALITY_GATE_FAILED",
    )
    registry.record_task_outcome(
        agent_id="slower-but-reliable",
        capability="write_test",
        task_type="write_test",
        status="succeeded",
        duration_seconds=30,
    )

    agents = registry.discover("write_test")

    assert [agent.agent_id for agent in agents] == ["slower-but-reliable", "fast-but-flaky"]
    assert agents[0].metadata["observed_capability_score"]["success_rate"] == 1.0
