from mac.protocol.messages import (
    AgentCapability,
    AgentCard,
    ConflictRecord,
    CoordinationPolicy,
    HandoffResult,
    PathRule,
    Plan,
    TaskPayload,
    TaskTransfer,
    VerificationEntry,
)


def test_phase_a_protocol_models_are_structured_and_generic():
    plan = Plan(plan_id="plan-1", goal="Ship collaboration layer", created_by="planner")
    handoff = HandoffResult(
        task_id="task-1",
        plan_id="plan-1",
        agent_id="worker",
        verification=[VerificationEntry(command="python -m pytest -q", result="pass", description="unit suite")],
        changed_files=["src/mac/registry.py"],
        docs_touched=["docs/SPEC.md"],
        risks=["needs real project pilot"],
    )
    conflict = ConflictRecord(
        conflict_id="conflict-1",
        plan_id="plan-1",
        task_id="task-1",
        source="manual",
        severity="blocking",
        description="Two agents edited the same file",
        involved_agents=["worker", "reviewer"],
        involved_files=["src/mac/registry.py"],
    )

    assert plan.status == "draft"
    assert plan.task_ids == []
    assert handoff.boundary_review == "not_required"
    assert handoff.risks == ["needs real project pilot"]
    assert conflict.source == "manual"
    assert conflict.resolved is False


def test_phase_a_path_fields_default_to_no_restriction():
    agent = AgentCard(agent_id="agent-1", name="Agent", capabilities=[AgentCapability(name="write_code")])
    rule = PathRule()

    assert agent.allowed_paths == []
    assert agent.forbidden_paths == []
    assert rule.allow_all is True
    assert rule.allowed_patterns == []
    assert rule.forbidden_patterns == []


def test_task_transfer_carries_plan_and_dependencies_without_embedded_handoff():
    task = TaskTransfer(
        task_id="task-1",
        plan_id="plan-1",
        depends_on=["task-0"],
        payload=TaskPayload(type="write_code", summary="Implement feature"),
    )

    assert task.plan_id == "plan-1"
    assert task.depends_on == ["task-0"]
    assert "handoff_result" not in TaskTransfer.model_fields


# ---------------------------------------------------------------------------
# CoordinationPolicy.from_env
# ---------------------------------------------------------------------------


def test_coordination_policy_from_env_returns_defaults_when_no_variables_set():
    policy = CoordinationPolicy.from_env(env={})

    assert policy.require_review is False
    assert policy.require_path_check is False
    assert policy.max_retry_count == 3
    assert policy.path_rule.allow_all is True
    assert policy.path_rule.allowed_patterns == []
    assert policy.path_rule.forbidden_patterns == []


def test_coordination_policy_from_env_reads_truthy_flags_case_insensitively():
    policy = CoordinationPolicy.from_env(
        env={
            "MAC_REQUIRE_REVIEW": "TRUE",
            "MAC_REQUIRE_PATH_CHECK": "on",
            "MAC_MAX_RETRY_COUNT": "7",
        }
    )

    assert policy.require_review is True
    assert policy.require_path_check is True
    assert policy.max_retry_count == 7


def test_coordination_policy_from_env_parses_path_rules_allowed_and_forbidden():
    policy = CoordinationPolicy.from_env(
        env={"MAC_PATH_RULES": "backend/**, tests/** | db/** , fixtures/gold/**"}
    )

    assert policy.path_rule.allow_all is False
    assert policy.path_rule.allowed_patterns == ["backend/**", "tests/**"]
    assert policy.path_rule.forbidden_patterns == ["db/**", "fixtures/gold/**"]


def test_coordination_policy_from_env_path_rules_only_allowed_side():
    policy = CoordinationPolicy.from_env(env={"MAC_PATH_RULES": "src/**|"})

    assert policy.path_rule.allow_all is False
    assert policy.path_rule.allowed_patterns == ["src/**"]
    assert policy.path_rule.forbidden_patterns == []


def test_coordination_policy_from_env_rejects_non_integer_retry_count():
    import pytest

    with pytest.raises(ValueError, match="must be an integer"):
        CoordinationPolicy.from_env(env={"MAC_MAX_RETRY_COUNT": "lots"})
