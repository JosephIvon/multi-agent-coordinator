from __future__ import annotations

from typing import Any

from mac.testing.contracts import TestContract


def evaluate_quality_gate(
    contract: TestContract | dict[str, Any] | None,
    results: list[dict[str, Any]],
    *,
    diff_lines: int = 0,
    has_changelog: bool | None = None,
    acceptance_criteria: list[str] | None = None,
    met_acceptance_criteria: list[str] | None = None,
) -> tuple[bool, str | None]:
    """Evaluate a quality gate against submitted results and optional metadata.

    :param contract: The test contract to evaluate against.
    :param results: List of quality result dicts with keys ``command``, ``status``,
        ``evidence``, and optionally ``diff_lines``.
    :param diff_lines: Total lines changed in this attempt (used for max_diff_lines check).
        Only checked when > 0 (0 means not provided).
    :param has_changelog: Whether a CHANGELOG entry was provided.
        ``None`` (default) means the check is skipped entirely;
        ``True`` / ``False`` means enforce the contract requirement.
    :param acceptance_criteria: The task's required acceptance criteria (list of criteria IDs).
        ``None`` (default) means the check is skipped.
    :param met_acceptance_criteria: The subset of acceptance criteria the agent claims to have met.
        Only checked when ``acceptance_criteria`` is provided.
    :returns: ``(allowed, reason)`` where ``allowed`` is True if the gate passes,
        and ``reason`` is a machine-readable failure code when it doesn't.
    """
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

    evidence: set[str] = set()
    for result in passed_results:
        evidence.update(str(item) for item in result.get("evidence", []))

    missing_evidence = sorted(item for item in contract.required_evidence if item not in evidence)
    if missing_evidence:
        return False, "missing_evidence:" + ",".join(missing_evidence)

    # ── C-2: diff line limit check ──────────────────────────────────
    max_lines = getattr(contract, "max_diff_lines", None)
    if max_lines is not None and diff_lines > 0 and diff_lines > max_lines:
        return False, f"diff_too_large:{diff_lines}>{max_lines}"

    # ── C-2: changelog requirement check ────────────────────────────
    if getattr(contract, "require_changelog", False) and has_changelog is False:
        return False, "missing_changelog"

    # ── C-2: acceptance criteria verification ───────────────────────
    if getattr(contract, "require_acceptance_criteria", False) and acceptance_criteria is not None:
        if not acceptance_criteria:
            return False, "no_acceptance_criteria_defined"
        if met_acceptance_criteria is None:
            return False, "no_acceptance_criteria_met"
        unmet = sorted(item for item in acceptance_criteria if item not in met_acceptance_criteria)
        if unmet:
            return False, "unmet_acceptance_criteria:" + ",".join(unmet)

    return True, None
