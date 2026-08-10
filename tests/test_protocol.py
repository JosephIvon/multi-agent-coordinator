import pytest
from pydantic import ValidationError

from mac.protocol.constants import ERROR_CODES, RISK_LEVELS, TASK_STATUSES
from mac.protocol.messages import (
    AgentCapability,
    AgentCard,
    ContextBundle,
    TaskPayload,
    TaskTransfer,
)


def test_model_defaults_do_not_share_mutable_values():
    first = AgentCard(agent_id="a", name="A", capabilities=[AgentCapability(name="write_code")])
    second = AgentCard(agent_id="b", name="B", capabilities=[AgentCapability(name="write_code")])

    first.metadata["vendor"] = "anthropic"
    first.capabilities[0].frameworks.append("pytest")

    assert second.metadata == {}
    assert second.capabilities[0].frameworks == []


def test_task_transfer_validates_priority_range():
    payload = TaskPayload(type="custom", summary="coordinate handoff")

    with pytest.raises(ValidationError):
        TaskTransfer(task_id="task-1", trace_id="trace-1", source_agent_id="a", payload=payload, priority=0)

    with pytest.raises(ValidationError):
        TaskTransfer(task_id="task-1", trace_id="trace-1", source_agent_id="a", payload=payload, priority=11)


def test_context_bundle_carries_handoff_ready_context():
    bundle = ContextBundle(
        summary="Refactor payment tests",
        artifact_refs=["file://src/payment.py"],
        changed_files=["src/payment.py"],
        open_questions=["Does refund need idempotency?"],
        acceptance_criteria=["Existing payment tests pass"],
    )

    assert bundle.summary == "Refactor payment tests"
    assert bundle.artifact_refs == ["file://src/payment.py"]
    assert bundle.acceptance_criteria == ["Existing payment tests pass"]


def test_write_test_payload_requires_target_module_and_coverage_goal():
    with pytest.raises(ValidationError):
        TaskPayload(type="write_test", summary="Add tests")


def test_validate_tests_payload_requires_suite_and_framework():
    with pytest.raises(ValidationError):
        TaskPayload(type="validate_tests", summary="Validate tests")


def test_task_transfer_defaults_include_deadline_and_hop_guardrails():
    task = TaskTransfer(
        task_id="task-1",
        trace_id="trace-1",
        source_agent_id="planner",
        payload=TaskPayload(type="custom", summary="handoff"),
    )

    assert task.max_hops == 5
    assert task.current_hops == 0
    assert task.ttl_seconds == 3600
    assert task.status == "proposed"
    assert "hop_count" not in TaskTransfer.model_fields
    assert "hop_count" not in task.model_dump(mode="json")


def test_task_transfer_uses_canonical_agent_fields_only():
    assert "source_agent_id" in TaskTransfer.model_fields
    assert "target_agent_id" in TaskTransfer.model_fields
    assert "from_agent" not in TaskTransfer.model_fields
    assert "to_agent" not in TaskTransfer.model_fields

    task = TaskTransfer(
        task_id="task-1",
        trace_id="trace-1",
        source_agent_id="planner",
        target_agent_id="tester",
        payload=TaskPayload(
            type="write_test",
            summary="Canonical agent fields",
            target_module="mac.protocol",
            coverage_goal=80,
        ),
    )

    dumped = task.model_dump(mode="json")
    assert dumped["source_agent_id"] == "planner"
    assert dumped["target_agent_id"] == "tester"
    assert "from_agent" not in dumped
    assert "to_agent" not in dumped


def test_protocol_constants_include_mvp_state_and_error_vocabulary():
    assert "completed" in TASK_STATUSES
    assert "high" in RISK_LEVELS
    assert "PAYLOAD_TOO_LARGE" in ERROR_CODES


def test_task_transfer_agent_context_routing_fields_defaults():
    """Backward-compat guard for the 2026-08-09 agent-context-ops extension.

    The network / capability / data-classification / lease fields were added
    with defaults so pre-existing callers (e.g. older TaskTransfer payloads
    without them) keep constructing. New fields must not be required.
    """
    task = TaskTransfer(
        task_id="task-1",
        trace_id="trace-1",
        source_agent_id="planner",
        payload=TaskPayload(type="custom", summary="handoff"),
    )

    assert task.required_network == "local"
    assert task.required_capabilities == []
    assert task.data_classification == "local-only"
    assert task.lease_owner is None
    assert task.lease_expire_at is None
    assert task.fallback_policy is None

    # Defaults must not leak a shared mutable list across instances.
    other = TaskTransfer(
        task_id="task-2",
        trace_id="trace-2",
        source_agent_id="planner",
        payload=TaskPayload(type="custom", summary="handoff"),
    )
    assert other.required_capabilities == []
    other.required_capabilities.append("ssh")
    assert task.required_capabilities == []


def test_task_transfer_agent_context_routing_fields_roundtrip():
    """Explicit values for the new routing/lease fields survive a dump/load
    cycle and the Literal constraints reject out-of-vocabulary values.
    """
    task = TaskTransfer(
        task_id="task-1",
        trace_id="trace-1",
        source_agent_id="planner",
        payload=TaskPayload(type="custom", summary="handoff"),
        required_network="cloud",
        required_capabilities=["ssh", "mcp_browser"],
        data_classification="cloud-safe",
        lease_owner="worker-7",
        lease_expire_at="2026-08-10T12:00:00+00:00",
        fallback_policy="escalate",
    )

    dumped = task.model_dump(mode="json")
    assert dumped["required_network"] == "cloud"
    assert dumped["required_capabilities"] == ["ssh", "mcp_browser"]
    assert dumped["data_classification"] == "cloud-safe"
    assert dumped["lease_owner"] == "worker-7"
    assert dumped["lease_expire_at"] == "2026-08-10T12:00:00+00:00"
    assert dumped["fallback_policy"] == "escalate"

    reloaded = TaskTransfer.model_validate(dumped)
    assert reloaded.required_network == "cloud"
    assert reloaded.lease_owner == "worker-7"

    # Literal vocabularies must reject unknown values.
    with pytest.raises(ValidationError):
        TaskTransfer(
            task_id="task-1",
            trace_id="trace-1",
            source_agent_id="planner",
            payload=TaskPayload(type="custom", summary="handoff"),
            required_network="galactic",
        )
    with pytest.raises(ValidationError):
        TaskTransfer(
            task_id="task-1",
            trace_id="trace-1",
            source_agent_id="planner",
            payload=TaskPayload(type="custom", summary="handoff"),
            data_classification="public",
        )

