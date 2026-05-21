from mac.protocol.messages import AgentCapability, AgentCard, ContextBundle, TaskPayload, TaskTransfer
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger


def _task(
    task_id: str,
    task_type: str,
    *,
    trace_id: str | None = None,
    target_agent_id: str | None = None,
) -> TaskTransfer:
    payload_kwargs = {"type": task_type, "summary": f"{task_type} task"}
    if task_type == "write_test":
        payload_kwargs.update({"target_module": "mac.registry", "coverage_goal": 85})
    if task_type == "code_review":
        payload_kwargs.update({"file_path": "src/mac/registry.py", "diff_hunk": "@@ -1 +1 @@"})
    return TaskTransfer(
        task_id=task_id,
        trace_id=trace_id or task_id,
        source_agent_id="planner",
        target_agent_id=target_agent_id,
        payload=TaskPayload(**payload_kwargs),
        context=ContextBundle(summary=f"{task_type} task"),
    )


def test_claim_next_task_matches_capability_and_marks_accepted(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task("task-review", "code_review", trace_id="trace-review"))
    registry.submit_task(_task("task-test", "write_test", trace_id="trace-test"))

    claimed = registry.claim_next_task(agent_id="tester", capability="write_test")

    assert claimed is not None
    assert claimed.task_id == "task-test"
    assert claimed.status == "accepted"
    assert claimed.target_agent_id == "tester"
    persisted = registry.ledger.get_task_transfer("task-test")
    assert persisted is not None
    assert persisted.target_agent_id == "tester"
    assert registry.claim_next_task(agent_id="tester", capability="write_test") is None
    assert [entry.action for entry in registry.get_audit_trail("trace-test")] == ["submit_task", "claim_task"]


def test_claim_next_task_respects_explicit_target_agent(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(
        _task("task-targeted", "write_test", trace_id="trace-targeted", target_agent_id="tester-a")
    )

    assert registry.claim_next_task(agent_id="tester-b", capability="write_test") is None

    claimed = registry.claim_next_task(agent_id="tester-a", capability="write_test")

    assert claimed is not None
    assert claimed.task_id == "task-targeted"
    assert claimed.target_agent_id == "tester-a"


def test_claim_next_task_uses_required_capability_override(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    task = _task("task-custom", "custom", trace_id="trace-custom")
    task.payload.extra["required_capability"] = "write_test"
    registry.submit_task(task)

    claimed = registry.claim_next_task(agent_id="tester", capability="write_test")

    assert claimed is not None
    assert claimed.task_id == "task-custom"
    assert claimed.target_agent_id == "tester"


def test_claim_next_task_best_effort_uses_observed_success_rate(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.register_agent(
        AgentCard(
            agent_id="polyglot",
            name="Polyglot",
            capabilities=[AgentCapability(name="write_code"), AgentCapability(name="write_test")],
        )
    )
    registry.submit_task(_task("task-code", "code_review", trace_id="trace-code"))
    registry.submit_task(_task("task-test", "write_test", trace_id="trace-test"))
    registry.record_task_outcome(
        agent_id="polyglot",
        capability="code_review",
        task_type="code_review",
        status="failed",
        duration_seconds=2,
    )
    registry.record_task_outcome(
        agent_id="polyglot",
        capability="write_test",
        task_type="write_test",
        status="succeeded",
        duration_seconds=1,
    )

    claimed = registry.claim_next_task(agent_id="polyglot", capability="code_review", best_effort=True)

    assert claimed is not None
    assert claimed.task_id == "task-test"
    assert claimed.target_agent_id == "polyglot"
