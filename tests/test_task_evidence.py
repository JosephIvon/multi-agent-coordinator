from mac.protocol.messages import AgentCapability, AgentCard, ContextBundle, TaskPayload, TaskTransfer
from mac.registry import Registry
from mac.runner import LocalAgentRunner, TaskRunResult
from mac.storage import SQLiteTaskLedger
from mac.testing.contracts import TestContract


def _registry_with_task(tmp_path) -> Registry:
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(
        TaskTransfer(
            task_id="task-1",
            trace_id="trace-1",
            source_agent_id="planner",
            payload=TaskPayload(
                type="write_test",
                summary="Write focused evidence tests",
                target_module="mac.registry",
                coverage_goal=80,
                risk_level="low",
            ),
            context=ContextBundle(summary="Evidence bundle task"),
            test_contract=TestContract.for_risk("low"),
        )
    )
    return registry


def test_task_evidence_bundle_aggregates_task_quality_audit_and_capability_score(tmp_path):
    registry = _registry_with_task(tmp_path)
    runner = LocalAgentRunner(
        registry=registry,
        agent=AgentCard(agent_id="runner", name="Runner", capabilities=[AgentCapability(name="write_test")]),
        capability="write_test",
        handler=lambda task: TaskRunResult.passed(
            command="pytest related tests or smoke test",
            evidence=["test_output"],
            output="ok",
        ),
    )
    runner.run_once()

    bundle = registry.get_task_evidence("task-1")

    assert bundle is not None
    assert bundle.task_id == "task-1"
    assert bundle.trace_id == "trace-1"
    assert bundle.task.status == "completed"
    assert bundle.execution_agent_id == "runner"
    assert bundle.required_capability == "write_test"
    assert bundle.quality_results[0]["command"] == "pytest related tests or smoke test"
    assert [entry.action for entry in bundle.audit_trail] == [
        "submit_task",
        "claim_task",
        "start_task",
        "submit_quality_result",
        "complete_task",
    ]
    assert bundle.observed_capability_score is not None
    assert bundle.observed_capability_score["succeeded"] == 1


def test_task_evidence_returns_none_for_missing_task(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))

    assert registry.get_task_evidence("missing") is None


def test_task_evidence_is_read_only(tmp_path):
    registry = _registry_with_task(tmp_path)
    original = registry.get_task("task-1")
    assert original is not None

    before_audit = [entry.action for entry in registry.get_audit_trail("trace-1")]
    registry.get_task_evidence("task-1")
    after = registry.get_task("task-1")
    assert after is not None
    after_audit = [entry.action for entry in registry.get_audit_trail("trace-1")]

    assert after.updated_at == original.updated_at
    assert after_audit == before_audit


def test_task_evidence_for_unclaimed_task_has_empty_evidence_and_no_agent_score(tmp_path):
    registry = _registry_with_task(tmp_path)

    bundle = registry.get_task_evidence("task-1")

    assert bundle is not None
    assert bundle.task.status == "proposed"
    assert bundle.quality_results == []
    assert [entry.action for entry in bundle.audit_trail] == ["submit_task"]
    assert bundle.execution_agent_id is None
    assert bundle.required_capability == "write_test"
    assert bundle.observed_capability_score is None


def test_task_evidence_uses_target_agent_id_for_execution_agent_and_score(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    task = TaskTransfer(
        task_id="assigned-task",
        trace_id="trace-assigned",
        source_agent_id="planner",
        target_agent_id="assigned-runner",
        payload=TaskPayload(
            type="write_test",
            summary="Legacy target field",
            target_module="mac.registry",
            coverage_goal=80,
        ),
        context=ContextBundle(summary="Legacy target field"),
    )
    registry.submit_task(task)
    registry.record_task_outcome(
        agent_id="assigned-runner",
        capability="write_test",
        task_type="write_test",
        status="succeeded",
        duration_seconds=1.2,
    )

    bundle = registry.get_task_evidence("assigned-task")

    assert bundle is not None
    assert bundle.execution_agent_id == "assigned-runner"
    assert bundle.observed_capability_score is not None
    assert bundle.observed_capability_score["succeeded"] == 1


def test_task_evidence_uses_required_capability_override_for_score(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    task = TaskTransfer(
        task_id="custom-task",
        trace_id="trace-custom",
        source_agent_id="planner",
        target_agent_id="custom-runner",
        payload=TaskPayload(type="custom", summary="Custom task"),
        context=ContextBundle(summary="Custom task"),
    )
    task.payload.extra["required_capability"] = "write_test"
    registry.submit_task(task)
    registry.record_task_outcome(
        agent_id="custom-runner",
        capability="write_test",
        task_type="custom",
        status="succeeded",
        duration_seconds=0.5,
    )

    bundle = registry.get_task_evidence("custom-task")

    assert bundle is not None
    assert bundle.required_capability == "write_test"
    assert bundle.observed_capability_score is not None
    assert bundle.observed_capability_score["succeeded"] == 1


def test_task_evidence_preserves_quality_result_order(tmp_path):
    registry = _registry_with_task(tmp_path)
    registry.submit_quality_result(
        "task-1",
        {"agent_id": "tester", "command": "first", "status": "passed", "evidence": ["test_output"]},
    )
    registry.submit_quality_result(
        "task-1",
        {"agent_id": "tester", "command": "second", "status": "passed", "evidence": ["review_notes"]},
    )

    bundle = registry.get_task_evidence("task-1")

    assert bundle is not None
    assert [result["command"] for result in bundle.quality_results] == ["first", "second"]
