from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from mac.protocol.messages import ContextBundle, TaskPayload, TaskTransfer
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger


def _registry(db_path):
    return Registry(SQLiteTaskLedger(db_path))


def test_concurrent_claimants_only_one_agent_claims_task(tmp_path):
    db_path = tmp_path / "mac.db"
    registry = _registry(db_path)
    registry.submit_task(
        TaskTransfer(
            task_id="task-1",
            trace_id="trace-1",
            source_agent_id="planner",
            payload=TaskPayload(
                type="write_test",
                summary="Concurrent claim",
                target_module="mac.registry",
                coverage_goal=80,
            ),
            context=ContextBundle(summary="Concurrent claim"),
        )
    )
    barrier = Barrier(8)

    def claim(agent_id: str) -> str | None:
        local = _registry(db_path)
        barrier.wait()
        task = local.claim_next_task(agent_id=agent_id, capability="write_test")
        return task.target_agent_id if task is not None else None

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda index: claim(f"agent-{index}"), range(8)))

    winners = [result for result in results if result is not None]
    persisted = registry.get_task("task-1")
    assert persisted is not None
    assert len(winners) == 1
    assert persisted.status == "accepted"
    assert persisted.target_agent_id == winners[0]
    assert [entry.action for entry in registry.get_audit_trail("trace-1")].count("claim_task") == 1


def test_concurrent_quality_results_are_all_recorded(tmp_path):
    db_path = tmp_path / "mac.db"
    registry = _registry(db_path)
    registry.submit_task(
        TaskTransfer(
            task_id="task-1",
            trace_id="trace-1",
            source_agent_id="planner",
            target_agent_id="tester",
            payload=TaskPayload(
                type="write_test",
                summary="Concurrent quality",
                target_module="mac.registry",
                coverage_goal=80,
            ),
            context=ContextBundle(summary="Concurrent quality"),
        )
    )
    registry.accept_handoff("task-1", "tester")
    registry.start_task("task-1", "tester")
    barrier = Barrier(8)

    def submit(index: int) -> None:
        local = _registry(db_path)
        barrier.wait()
        local.submit_quality_result(
            "task-1",
            {
                "agent_id": f"tester-{index}",
                "command": f"pytest shard {index}",
                "status": "passed",
                "evidence": [f"evidence-{index}"],
            },
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(submit, range(8)))

    results = registry.ledger.get_quality_results("task-1")
    assert {result["command"] for result in results} == {f"pytest shard {index}" for index in range(8)}
    assert len(results) == 8
    assert [entry.action for entry in registry.get_audit_trail("trace-1")].count("submit_quality_result") == 8
