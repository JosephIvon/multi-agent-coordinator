from mac.testing.contracts import TestContract


def test_high_risk_contract_requires_review_and_coverage_evidence():
    contract = TestContract.for_risk("high")

    assert contract.risk_level == "high"
    assert "pytest" in contract.recommended_commands
    assert "coverage_report" in contract.required_evidence
    assert "review_notes" in contract.required_evidence


def test_low_risk_contract_keeps_test_budget_small():
    contract = TestContract.for_risk("low")

    assert contract.recommended_commands == ["pytest related tests or smoke test"]
    assert contract.required_evidence == ["test_output"]
