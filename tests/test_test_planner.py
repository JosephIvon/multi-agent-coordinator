from mac.testing.planner import plan_test_contract


def assert_contract(contract, risk_level, required_command, required_evidence):
    assert contract.risk_level == risk_level
    assert required_command in contract.required_commands
    assert required_evidence in contract.required_evidence


def test_explicit_risk_hint_wins_over_file_signals():
    contract = plan_test_contract(["src/mac/security/auth.py"], risk_hint="low")

    assert_contract(contract, "low", "pytest related tests or smoke test", "test_output")


def test_docs_only_changes_are_low_risk():
    contract = plan_test_contract(["docs/SPEC.md", "README.md"])

    assert_contract(contract, "low", "pytest related tests or smoke test", "test_output")


def test_regular_code_changes_are_medium_risk():
    contract = plan_test_contract(["src/mac/registry.py"])

    assert_contract(contract, "medium", "python -m pytest tests", "changed_files")


def test_security_or_data_sensitive_paths_are_high_risk():
    for changed_file in [
        "src/mac/auth/session.py",
        "src/mac/security/policy.py",
        "src/mac/payment/checkout.py",
        "src/mac/storage/migrations/001_init.sql",
        "src/mac/schema/contracts.py",
    ]:
        contract = plan_test_contract([changed_file])

        assert_contract(contract, "high", "python -m pytest --cov", "coverage_report")
