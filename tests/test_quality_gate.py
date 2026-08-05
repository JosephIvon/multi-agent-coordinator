from mac.quality.gate import evaluate_quality_gate
from mac.testing.contracts import TestContract


def test_no_contract_allows_completion():
    allowed, reason = evaluate_quality_gate(None, [])

    assert allowed is True
    assert reason is None


def test_high_risk_requires_passed_result_with_required_evidence():
    contract = TestContract.for_risk("high")

    # Missing evidence and acceptance criteria
    allowed, reason = evaluate_quality_gate(
        contract,
        [{"command": "python -m pytest --cov", "status": "passed", "evidence": ["test_output"]}],
    )

    assert allowed is False
    assert reason == "missing_evidence:coverage_report,review_notes"

    # Has evidence, no C-2 checks triggered → passes (backward-compatible default)
    allowed, reason = evaluate_quality_gate(
        contract,
        [
            {
                "command": "python -m pytest --cov",
                "status": "passed",
                "evidence": ["test_output", "coverage_report", "review_notes"],
            }
        ],
    )

    assert allowed is True
    assert reason is None

    # Full C-2 enforcement: evidence + changelog + acceptance criteria
    allowed, reason = evaluate_quality_gate(
        contract,
        [
            {
                "command": "python -m pytest --cov",
                "status": "passed",
                "evidence": ["test_output", "coverage_report", "review_notes"],
            }
        ],
        has_changelog=True,
        acceptance_criteria=["feature works", "tests pass"],
        met_acceptance_criteria=["feature works", "tests pass"],
    )

    assert allowed is True
    assert reason is None


def test_failed_result_blocks_completion():
    contract = TestContract.for_risk("low")

    allowed, reason = evaluate_quality_gate(
        contract,
        [{"command": "pytest related tests or smoke test", "status": "failed", "evidence": ["test_output"]}],
    )

    assert allowed is False
    assert reason == "no_passed_results"


def test_high_risk_requires_coverage_command():
    contract = TestContract.for_risk("high")

    allowed, reason = evaluate_quality_gate(
        contract,
        [
            {
                "command": "pytest",
                "status": "passed",
                "evidence": ["test_output", "coverage_report", "review_notes"],
            }
        ],
    )

    assert allowed is False
    assert reason == "missing_command:python -m pytest --cov"


# ── C-2: diff line limit tests ───────────────────────────────────

def test_medium_risk_rejects_diff_over_500_lines():
    contract = TestContract.for_risk("medium")

    allowed, reason = evaluate_quality_gate(
        contract,
        [
            {
                "command": "python -m pytest tests",
                "status": "passed",
                "evidence": ["test_output", "changed_files"],
            }
        ],
        diff_lines=520,
    )

    assert allowed is False
    assert reason == "diff_too_large:520>500"


def test_medium_risk_allows_diff_under_500_lines():
    contract = TestContract.for_risk("medium")

    allowed, reason = evaluate_quality_gate(
        contract,
        [
            {
                "command": "python -m pytest tests",
                "status": "passed",
                "evidence": ["test_output", "changed_files"],
            }
        ],
        diff_lines=200,
        has_changelog=True,
    )

    assert allowed is True
    assert reason is None


def test_low_risk_has_no_diff_limit():
    contract = TestContract.for_risk("low")

    allowed, reason = evaluate_quality_gate(
        contract,
        [
            {
                "command": "pytest related tests or smoke test",
                "status": "passed",
                "evidence": ["test_output"],
            }
        ],
        diff_lines=2000,
    )

    assert allowed is True
    assert reason is None


# ── C-2: changelog requirement tests ──────────────────────────────

def test_medium_risk_requires_changelog():
    contract = TestContract.for_risk("medium")

    allowed, reason = evaluate_quality_gate(
        contract,
        [
            {
                "command": "python -m pytest tests",
                "status": "passed",
                "evidence": ["test_output", "changed_files"],
            }
        ],
        has_changelog=False,
    )

    assert allowed is False
    assert reason == "missing_changelog"


def test_medium_risk_passes_with_changelog():
    contract = TestContract.for_risk("medium")

    allowed, reason = evaluate_quality_gate(
        contract,
        [
            {
                "command": "python -m pytest tests",
                "status": "passed",
                "evidence": ["test_output", "changed_files"],
            }
        ],
        has_changelog=True,
    )

    assert allowed is True
    assert reason is None


def test_low_risk_does_not_require_changelog():
    contract = TestContract.for_risk("low")

    allowed, reason = evaluate_quality_gate(
        contract,
        [
            {
                "command": "pytest related tests or smoke test",
                "status": "passed",
                "evidence": ["test_output"],
            }
        ],
    )

    assert allowed is True
    assert reason is None


# ── C-2: acceptance criteria tests ────────────────────────────────

def test_high_risk_unmet_acceptance_criteria():
    contract = TestContract.for_risk("high")

    allowed, reason = evaluate_quality_gate(
        contract,
        [
            {
                "command": "python -m pytest --cov",
                "status": "passed",
                "evidence": ["test_output", "coverage_report", "review_notes"],
            }
        ],
        has_changelog=True,
        acceptance_criteria=["feature works", "edge cases handled", "docs updated"],
        met_acceptance_criteria=["feature works"],
    )

    assert allowed is False
    assert reason == "unmet_acceptance_criteria:docs updated,edge cases handled"


def test_high_risk_requires_acceptance_criteria_met():
    contract = TestContract.for_risk("high")

    allowed, reason = evaluate_quality_gate(
        contract,
        [
            {
                "command": "python -m pytest --cov",
                "status": "passed",
                "evidence": ["test_output", "coverage_report", "review_notes"],
            }
        ],
        has_changelog=True,
        acceptance_criteria=[],
    )
    # acceptance_criteria is empty → no_acceptance_criteria_defined
    assert allowed is False
    assert reason == "no_acceptance_criteria_defined"


def test_custom_contract_overrides_defaults():
    """C-2: for_risk keyword args override the risk-level defaults."""
    contract = TestContract.for_risk(
        "high",
        require_acceptance_criteria=False,
        require_changelog=False,
        max_diff_lines=5000,  # explicitly raised, None means "use default"
    )

    # Verify the contract fields were actually overridden
    assert contract.require_changelog is False
    assert contract.require_acceptance_criteria is False
    assert contract.max_diff_lines == 5000

    allowed, reason = evaluate_quality_gate(
        contract,
        [
            {
                "command": "python -m pytest --cov",
                "status": "passed",
                "evidence": ["test_output", "coverage_report", "review_notes"],
            }
        ],
        diff_lines=2000,
    )

    assert allowed is True
    assert reason is None
