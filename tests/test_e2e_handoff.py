from mac.protocol.messages import AgentCapability, AgentCard, ContextBundle, TaskPayload, TaskTransfer
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger
from mac.testing.contracts import TestContract


def test_registry_runs_two_agent_handoff(tmp_path):
    mac = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    mac.register(AgentCard(agent_id="writer", name="Writer", capabilities=[AgentCapability(name="write_code")]))
    mac.register(AgentCard(agent_id="tester", name="Tester", capabilities=[AgentCapability(name="write_test")]))

    task = mac.submit_task(
            TaskTransfer(
                task_id="task-1",
                trace_id="trace-1",
                source_agent_id="writer",
                payload=TaskPayload(
                type="write_test",
                summary="Write focused tests",
                target_module="mac.registry",
                coverage_goal=85,
                risk_level="high",
            ),
            context=ContextBundle(summary="Direct Registry API", artifact_refs=["file://src/mac/registry.py"]),
            test_contract=TestContract.for_risk("high"),
        )
    )

    mac.accept_handoff(task.task_id, "tester")
    mac.start_task(task.task_id, "tester")
    mac.submit_quality_result(
        task.task_id,
        {
            "command": "python -m pytest --cov",
            "status": "passed",
            "evidence": ["test_output", "coverage_report", "review_notes"],
        },
    )
    completed = mac.complete_task(task.task_id, "tester")

    assert completed.status == "completed"
    assert len(mac.get_audit_trail("trace-1")) == 5
