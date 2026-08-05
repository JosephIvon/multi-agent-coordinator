"""Integration tests for extension lifecycle hooks in mac.registry.

Tests that the hook invocation points in Registry correctly call
mac.extensions hooks at the right times with the right arguments.
"""

import tempfile

import pytest

from mac.extensions import Extension, register, reset
from mac.protocol.messages import (
    AgentCapability,
    AgentCard,
    ContextBundle,
    TaskPayload,
    TaskTransfer,
)
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger
from mac.testing.contracts import TestContract


def _task(task_id: str, *, capability: str = "write_code", status: str = "proposed", **updates) -> TaskTransfer:
    """Create a minimal TaskTransfer for test purposes."""
    return TaskTransfer(
        task_id=task_id,
        payload=TaskPayload(type=capability, summary=f"{task_id} summary"),
        status=status,
        **updates,
    )


def _make_running(registry: Registry, task_id: str = "t1", agent_id: str = "a1") -> TaskTransfer:
    """Submit, accept, and start a task so it's 'running'."""
    registry.register_agent(AgentCard(
        agent_id=agent_id,
        name="Agent",
        capabilities=[AgentCapability(name="write_code")],
    ))
    registry.submit_task(_task(task_id))
    registry.accept_handoff(task_id, agent_id)
    return registry.start_task(task_id, agent_id)


def _make_running_high_risk(registry: Registry, task_id: str = "t1", agent_id: str = "a1") -> TaskTransfer:
    """Submit, accept, start a high-risk task with a strict TestContract."""
    registry.register_agent(AgentCard(
        agent_id=agent_id,
        name="Agent",
        capabilities=[AgentCapability(name="write_code")],
    ))
    task = TaskTransfer(
        task_id=task_id,
        payload=TaskPayload(type="write_code", summary=f"{task_id} summary"),
        test_contract=TestContract.for_risk("high"),
    )
    registry.submit_task(task)
    registry.accept_handoff(task_id, agent_id)
    return registry.start_task(task_id, agent_id)


# ---------------------------------------------------------------------------
# Test 1: on_agent_registered
# ---------------------------------------------------------------------------


def test_hook_on_agent_registered():
    """Register an extension hook that captures agents on registration."""
    reset()
    try:
        received: list = []
        ext = Extension(
            name="test-ext",
            version="1.0.0",
            hooks={
                "on_agent_registered": lambda **kwargs: received.append(kwargs),
            },
        )
        register(ext)

        db_path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        registry = Registry(SQLiteTaskLedger(db_path))

        agent = AgentCard(
            agent_id="worker-1",
            name="Worker One",
            capabilities=[AgentCapability(name="write_code")],
        )
        registry.register_agent(agent)

        assert len(received) == 1
        assert received[0]["agent"] is agent
        # The agent should be the exact same object passed to register_agent.
        assert received[0]["agent"].agent_id == "worker-1"
        assert received[0]["agent"].name == "Worker One"
    finally:
        reset()


# ---------------------------------------------------------------------------
# Test 2: on_task_done (completed path)
# ---------------------------------------------------------------------------


def test_hook_on_task_done():
    """Hook on_task_done fires with status='completed' when review not required."""
    reset()
    try:
        received: list = []
        ext = Extension(
            name="test-ext",
            version="1.0.0",
            hooks={
                "on_task_done": lambda **kwargs: received.append(kwargs),
            },
        )
        register(ext)

        db_path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        registry = Registry(SQLiteTaskLedger(db_path))
        _make_running(registry, "t1", "a1")

        result = registry.done(
            "t1", "a1",
            quality_result={"command": "pytest", "status": "passed"},
        )

        assert result["status"] == "completed"
        assert len(received) == 1
        hook_call = received[0]
        assert hook_call["task_id"] == "t1"
        assert hook_call["agent_id"] == "a1"
        assert hook_call["status"] == "completed"
        assert isinstance(hook_call["result"], dict)
        assert hook_call["result"]["task_id"] == "t1"
    finally:
        reset()


# ---------------------------------------------------------------------------
# Test 3: on_task_blocked
# ---------------------------------------------------------------------------


def test_hook_on_task_blocked():
    """Hook on_task_blocked fires when quality gate fails (C-2 hard-fail)."""
    reset()
    try:
        received: list = []
        ext = Extension(
            name="test-ext",
            version="1.0.0",
            hooks={
                "on_task_blocked": lambda **kwargs: received.append(kwargs),
            },
        )
        register(ext)

        db_path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        registry = Registry(SQLiteTaskLedger(db_path))
        _make_running_high_risk(registry, "t1", "a1")

        # done() with insufficient evidence + high risk contract with
        # require_changelog=True (default for high) → quality gate fails → blocked.
        result = registry.done(
            "t1", "a1",
            quality_result={"command": "lint", "status": "passed"},
        )

        assert result["status"] == "blocked"
        assert result["quality_gate"] == "failed"
        assert len(received) == 1
        hook_call = received[0]
        assert hook_call["task_id"] == "t1"
        assert hook_call["agent_id"] == "a1"
        assert hook_call["reason"] is not None
        assert isinstance(hook_call["reason"], str)
        assert hook_call["result"] is result  # same dict object
    finally:
        reset()


# ---------------------------------------------------------------------------
# Test 4: on_task_failed
# ---------------------------------------------------------------------------


def test_hook_on_task_failed():
    """Hook on_task_failed fires with error_code and message on fail_task()."""
    reset()
    try:
        received: list = []
        ext = Extension(
            name="test-ext",
            version="1.0.0",
            hooks={
                "on_task_failed": lambda **kwargs: received.append(kwargs),
            },
        )
        register(ext)

        db_path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        registry = Registry(SQLiteTaskLedger(db_path))
        registry.submit_task(_task("t1", status="running"))

        failed = registry.fail_task(
            task_id="t1",
            agent_id="worker",
            error_code="DEPS_TIMEOUT",
            message="missing import",
        )

        assert failed.status == "failed"
        assert failed.error_code == "DEPS_TIMEOUT"
        assert len(received) == 1
        hook_call = received[0]
        assert hook_call["task_id"] == "t1"
        assert hook_call["agent_id"] == "worker"
        assert hook_call["error_code"] == "DEPS_TIMEOUT"
        assert hook_call["message"] == "missing import"
    finally:
        reset()


# ---------------------------------------------------------------------------
# Test 5: No hooks registered — system works fine
# ---------------------------------------------------------------------------


def test_hook_not_called_when_not_registered():
    """When no extension registers hooks, all operations still complete normally."""
    reset()
    try:
        # No extensions registered at all — just ensure reset() left a clean slate.
        db_path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        registry = Registry(SQLiteTaskLedger(db_path))

        # register_agent should work
        agent = AgentCard(
            agent_id="worker-1",
            name="Worker One",
            capabilities=[AgentCapability(name="write_code")],
        )
        registry.register_agent(agent)
        assert registry.get_agent("worker-1") is not None

        # fail_task should work
        registry.submit_task(_task("t1", status="running"))
        failed = registry.fail_task("t1", "worker", "ERR", "test")
        assert failed.status == "failed"

        # done should work
        _make_running(registry, "t2", "a1")
        result = registry.done("t2", "a1", quality_result={"command": "pytest", "status": "passed"})
        assert result["status"] == "completed"
    finally:
        reset()


# ---------------------------------------------------------------------------
# Test 6: Hook error does not block the main operation
# ---------------------------------------------------------------------------


def test_hook_error_does_not_block_operation():
    """A hook that raises an exception must not prevent the operation from completing."""
    reset()
    try:
        ext = Extension(
            name="test-ext",
            version="1.0.0",
            hooks={
                "on_task_done": lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
                "on_task_failed": lambda **kwargs: (_ for _ in ()).throw(ValueError("crash")),
                "on_agent_registered": lambda **kwargs: (_ for _ in ()).throw(Exception("noop")),
            },
        )
        register(ext)

        db_path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        registry = Registry(SQLiteTaskLedger(db_path))

        # 1. register_agent with a crashing hook
        agent = AgentCard(
            agent_id="worker-1",
            name="Worker One",
            capabilities=[AgentCapability(name="write_code")],
        )
        registry.register_agent(agent)
        assert registry.get_agent("worker-1") is not None

        # 2. fail_task with a crashing hook
        registry.submit_task(_task("t1", status="running"))
        failed = registry.fail_task("t1", "worker", "ERR", "msg")
        assert failed.status == "failed"
        assert failed.error_code == "ERR"

        # 3. done with a crashing hook
        _make_running(registry, "t2", "a1")
        result = registry.done("t2", "a1", quality_result={"command": "pytest", "status": "passed"})
        assert result["status"] == "completed"

        # 4. done blocked path with a crashing hook — need a blocking hook too.
        # Re-register with an on_task_blocked hook that also crashes.
        reset()
        ext2 = Extension(
            name="test-ext-2",
            version="1.0.0",
            hooks={
                "on_task_blocked": lambda **kwargs: (_ for _ in ()).throw(RuntimeError("blocked boom")),
            },
        )
        register(ext2)

        db_path2 = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        registry2 = Registry(SQLiteTaskLedger(db_path2))
        _make_running_high_risk(registry2, "t3", "a3")

        result2 = registry2.done(
            "t3", "a3",
            quality_result={"command": "lint", "status": "passed"},
        )
        # The operation still completed — task is blocked despite the hook crash.
        assert result2["status"] == "blocked"
        assert result2["quality_gate"] == "failed"
    finally:
        reset()
