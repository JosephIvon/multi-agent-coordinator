"""Cross-repo contract guard (mac-agent side, mirror of mac_coffee).

mac_coffee drives this Registry via agent_link.drive_mac_agent_for_claim.
If any method mac_coffee depends on changes shape here, this test must go
RED until tests/contract_fixtures/mac_coffee_contract.json is updated AND
mac_coffee's MAC_AGENT_CONTRACT_VERSION is bumped in lockstep.

This is the symmetric half of the guard described in
mac_coffee/docs/alignment-mac-coffee-mac-agent.md. The two repos hold
independent mirrors of the same contract; CI in either repo catches a
one-sided change.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

from mac.registry import Registry

# Mirror of mac_coffee.agent_link.MAC_AGENT_CONTRACT_VERSION. Bump together
# with the mac_coffee side and the fixture below on any contract change.
MAC_COFFEE_CONTRACT_VERSION = "1"

FIXTURE = Path(__file__).parent / "contract_fixtures" / "mac_coffee_contract.json"


def _load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_contract_version_matches_fixture() -> None:
    fixture = _load_fixture()
    assert fixture["contract_version"] == MAC_COFFEE_CONTRACT_VERSION


def test_registry_methods_match_fixture() -> None:
    """mac_coffee depends on these exact method shapes; guard against drift."""
    fixture = _load_fixture()
    for name, spec in fixture["registry_methods"].items():
        assert hasattr(Registry, name), f"mac_coffee depends on Registry.{name}; it is missing"
        sig = inspect.signature(getattr(Registry, name))
        params = [p for p in sig.parameters if p != "self"]
        expected_args = [a.split(":")[0].split(" ")[0] for a in spec["args"]]
        for expected in expected_args:
            assert expected in params, (
                f"contract drift: Registry.{name} no longer has param '{expected}'. "
                f"Got {params}. Update the fixture and bump MAC_COFFEE_CONTRACT_VERSION."
            )


def test_task_transfer_status_field_present() -> None:
    from mac.protocol.messages import TaskTransfer

    assert "status" in TaskTransfer.model_fields
