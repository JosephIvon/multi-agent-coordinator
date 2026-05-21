import asyncio

from mac.events import TaskEventBus
from mac.protocol.messages import ContextBundle, TaskPayload, TaskTransfer
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger


def _task() -> TaskTransfer:
    return TaskTransfer(
        task_id="task-1",
        trace_id="trace-1",
        source_agent_id="planner",
        target_agent_id="tester",
        payload=TaskPayload(
            type="write_test",
            summary="Event bus",
            target_module="mac.registry",
            coverage_goal=80,
        ),
        context=ContextBundle(summary="Event bus"),
    )


def test_task_event_bus_receives_registry_write_events(tmp_path):
    bus = TaskEventBus()
    events = []
    subscription = bus.subscribe(events.append)
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"), event_bus=bus)

    registry.submit_task(_task())
    registry.accept_handoff("task-1", "tester")
    registry.start_task("task-1", "tester")
    registry.submit_quality_result(
        "task-1",
        {"agent_id": "tester", "command": "pytest", "status": "passed", "evidence": ["test_output"]},
    )
    registry.fail_task("task-1", "tester", "HANDLER_ERROR")
    subscription.close()

    assert [event.type for event in events] == [
        "task_submitted",
        "task_accepted",
        "task_started",
        "quality_result_submitted",
        "task_failed",
    ]
    assert all(event.task_id == "task-1" for event in events)
    assert events[1].from_status == "proposed"
    assert events[1].to_status == "accepted"


def test_task_event_bus_filters_and_unsubscribes(tmp_path):
    bus = TaskEventBus()
    events = []
    subscription = bus.subscribe(events.append, event_types={"task_started"})
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"), event_bus=bus)

    registry.submit_task(_task())
    registry.accept_handoff("task-1", "tester")
    registry.start_task("task-1", "tester")
    subscription.close()
    registry.submit_quality_result(
        "task-1",
        {"agent_id": "tester", "command": "pytest", "status": "passed", "evidence": ["test_output"]},
    )

    assert [event.type for event in events] == ["task_started"]


def test_claim_event_is_published_after_target_agent_is_persisted(tmp_path):
    bus = TaskEventBus()
    events = []
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"), event_bus=bus)
    bus.subscribe(events.append)
    task = _task()
    task.target_agent_id = None
    registry.submit_task(task)

    claimed = registry.claim_next_task(agent_id="tester", capability="write_test")

    assert claimed is not None
    claim_event = [event for event in events if event.type == "task_claimed"][0]
    assert claim_event.actor == "tester"
    assert claim_event.payload["target_agent_id"] == "tester"


def test_task_event_bus_supports_asyncio_broadcast_queue(tmp_path):
    async def run() -> None:
        bus = TaskEventBus()
        subscription = bus.subscribe_queue(event_types={"task_claimed"})
        registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"), event_bus=bus)
        task = _task()
        task.target_agent_id = None
        registry.submit_task(task)

        registry.claim_next_task(agent_id="tester", capability="write_test")

        event = await asyncio.wait_for(subscription.queue.get(), timeout=1)
        subscription.close()
        assert event.type == "task_claimed"
        assert event.task_id == "task-1"

    asyncio.run(run())
