from mac.protocol.messages import AgentCapability, AgentCard, CoordinationPolicy
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger


def test_registry_discovers_agents_by_capability_status_load_and_project_context(tmp_path):
    ledger = SQLiteTaskLedger(tmp_path / "mac.db")
    registry = Registry(ledger)
    registry.register_agent(
        AgentCard(
            agent_id="busy",
            name="Busy",
            capabilities=[AgentCapability(name="python_unit_test")],
            status="available",
            load=80,
            project_context="demo",
        )
    )
    registry.register_agent(
        AgentCard(
            agent_id="best-fit",
            name="Best Fit",
            capabilities=[AgentCapability(name="python_unit_test")],
            status="available",
            load=10,
            project_context="demo",
        )
    )
    registry.register_agent(
        AgentCard(
            agent_id="wrong-project",
            name="Wrong Project",
            capabilities=[AgentCapability(name="python_unit_test")],
            status="available",
            load=1,
            project_context="other",
        )
    )
    registry.register_agent(
        AgentCard(
            agent_id="offline",
            name="Offline",
            capabilities=[AgentCapability(name="python_unit_test")],
            status="offline",
            load=0,
            project_context="demo",
        )
    )

    discovered = registry.discover(
        capability="python_unit_test",
        status="available",
        max_load=50,
        project_context="demo",
    )

    assert [agent.agent_id for agent in discovered] == ["best-fit"]
    assert discovered[0].metadata["selection_reason"] == "capability_load_affinity"


def test_registry_loads_policy_from_env_when_not_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_REQUIRE_REVIEW", "true")
    monkeypatch.setenv("MAC_MAX_RETRY_COUNT", "5")

    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))

    assert isinstance(registry.policy, CoordinationPolicy)
    assert registry.policy.require_review is True
    assert registry.policy.max_retry_count == 5


def test_registry_accepts_explicit_policy_and_skips_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_REQUIRE_REVIEW", "true")
    explicit = CoordinationPolicy(require_review=False, max_retry_count=1)

    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"), policy=explicit)

    assert registry.policy is explicit
    assert registry.policy.require_review is False
    assert registry.policy.max_retry_count == 1
