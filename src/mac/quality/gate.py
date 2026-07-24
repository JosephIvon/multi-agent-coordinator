from __future__ import annotations

from typing import Any

from mac.testing.contracts import TestContract


def evaluate_quality_gate(
    contract: TestContract | dict[str, Any] | None,
    results: list[dict[str, Any]],
) -> tuple[bool, str | None]:
    if contract is None:
        return True, None
    if isinstance(contract, dict):
        contract = TestContract.model_validate(contract)

    passed_results = [result for result in results if result.get("status") == "passed"]
    if not passed_results:
        return False, "no_passed_results"

    if getattr(contract, "allow_manual_override", False) and any(
        result.get("manual_override") is True for result in results
    ):
        return True, None

    commands = set(getattr(contract, "required_commands", []) or contract.recommended_commands)
    if commands:
        passed_commands = {str(result.get("command")) for result in passed_results}
        missing_commands = sorted(command for command in commands if command not in passed_commands)
        if missing_commands:
            return False, "missing_command:" + ",".join(missing_commands)

    evidence = set()
    for result in passed_results:
        evidence.update(str(item) for item in result.get("evidence", []))

    missing_evidence = sorted(item for item in contract.required_evidence if item not in evidence)
    if missing_evidence:
        return False, "missing_evidence:" + ",".join(missing_evidence)

    return True, None
