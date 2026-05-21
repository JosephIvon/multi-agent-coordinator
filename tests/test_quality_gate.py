from mac.quality.gate import evaluate_quality_gate
from mac.testing.contracts import TestContract


def test_no_contract_allows_completion():
    allowed, reason = evaluate_quality_gate(None, [])

    assert allowed is True
    assert reason is None


def test_high_risk_requires_passed_result_with_required_evidence():
    contract = TestContract.for_risk("high")

    allowed, reason = evaluate_quality_gate(
        contract,
        [{"command": "python -m pytest --cov", "status": "passed", "evidence": ["test_output"]}],
    )

    assert allowed is False
    assert reason == "missing_evidence:coverage_report,review_notes"

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
