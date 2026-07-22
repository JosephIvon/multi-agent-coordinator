from mac.protocol.messages import AgentCapability, AgentCard
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
