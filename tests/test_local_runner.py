import sys

from mac.protocol.messages import AgentCapability, AgentCard, ContextBundle, TaskPayload, TaskTransfer
from mac.registry import Registry
from mac.runner.local import LocalAgentRunner, TaskRunResult, command_task_handler
from mac.storage import SQLiteTaskLedger
from mac.testing.contracts import TestContract


def _registry_with_task(tmp_path, *, risk: str | None = "low") -> Registry:
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    task = TaskTransfer(
        task_id="task-1",
        trace_id="trace-1",
        source_agent_id="planner",
        payload=TaskPayload(
            type="write_test",
            summary="Write focused tests",
            target_module="mac.runner.local",
            coverage_goal=80,
            risk_level=risk,
        ),
        context=ContextBundle(summary="Runner task"),
        test_contract=TestContract.for_risk(risk) if risk else None,
    )
    registry.submit_task(task)
    return registry


def test_local_runner_completes_claimed_task_and_records_outcome(tmp_path):
    registry = _registry_with_task(tmp_path)
    runner = LocalAgentRunner(
        registry=registry,
        agent=AgentCard(agent_id="runner", name="Runner", capabilities=[AgentCapability(name="write_test")]),
        capability="write_test",
        handler=lambda task: TaskRunResult.passed(
            command="pytest related tests or smoke test",
            evidence=["test_output"],
            output=f"handled {task.task_id}",
        ),
    )

    completed = runner.run_once()

    assert completed is not None
    assert completed.status == "completed"
    assert [entry.action for entry in registry.get_audit_trail("trace-1")] == [
        "submit_task",
        "claim_task",
        "start_task",
        "submit_quality_result",
        "complete_task",
    ]
    assert registry.get_capability_score("runner", "write_test")["success_rate"] == 1.0


def test_local_runner_fails_task_when_handler_raises(tmp_path):
    registry = _registry_with_task(tmp_path, risk=None)

    def broken_handler(task):
        raise RuntimeError("boom")

    runner = LocalAgentRunner(
        registry=registry,
        agent=AgentCard(agent_id="runner", name="Runner", capabilities=[AgentCapability(name="write_test")]),
        capability="write_test",
        handler=broken_handler,
    )

    failed = runner.run_once()

    assert failed is not None
    assert failed.status == "failed"
    assert failed.error_code == "HANDLER_ERROR"
    assert registry.get_capability_score("runner", "write_test")["failed"] == 1


def test_local_runner_preserves_quality_gate_failure_reason(tmp_path):
    registry = _registry_with_task(tmp_path, risk="high")
    runner = LocalAgentRunner(
        registry=registry,
        agent=AgentCard(agent_id="runner", name="Runner", capabilities=[AgentCapability(name="write_test")]),
        capability="write_test",
        handler=lambda task: TaskRunResult.passed(
            command="python -m pytest --cov",
            evidence=["test_output"],
            output="missing review notes",
        ),
    )

    failed = runner.run_once()

    assert failed is not None
    assert failed.status == "failed"
    assert failed.error_code == "QUALITY_GATE_FAILED"
    assert registry.get_capability_score("runner", "write_test")["last_error_code"] == "QUALITY_GATE_FAILED"


def test_command_task_handler_reports_success_and_failure(tmp_path):
    success = command_task_handler([sys.executable, "-c", "print('ok')"])
    failure = command_task_handler([sys.executable, "-c", "import sys; sys.exit(3)"])

    assert success(None).status == "passed"
    assert "test_output" in success(None).evidence
    assert failure(None).status == "failed"
    assert failure(None).error_code == "COMMAND_FAILED"
