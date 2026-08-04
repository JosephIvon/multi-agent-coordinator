import json

from mac.cli import main


def test_contract_command_outputs_risk_based_evidence(capsys):
    exit_code = main(["contract", "--risk", "high"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["risk_level"] == "high"
    assert "pytest" in output["recommended_commands"]
    assert "coverage_report" in output["required_evidence"]
    assert "review_notes" in output["required_evidence"]


def test_register_and_discover_agent_with_sqlite_ledger(tmp_path, capsys):
    db_path = tmp_path / "mac.db"

    register_exit = main(
        [
            "register",
            "--db",
            str(db_path),
            "--agent-id",
            "pytest-runner",
            "--name",
            "Pytest Runner",
            "--capability",
            "python_unit_test",
            "--project-context",
            "demo",
            "--load",
            "10",
        ]
    )

    assert register_exit == 0
    capsys.readouterr()

    discover_exit = main(
        [
            "discover",
            "--db",
            str(db_path),
            "--capability",
            "python_unit_test",
            "--project-context",
            "demo",
        ]
    )

    assert discover_exit == 0
    discovered = json.loads(capsys.readouterr().out)
    assert discovered[0]["agent_id"] == "pytest-runner"
    assert discovered[0]["metadata"]["selection_reason"] == "capability_load_affinity"


def test_db_flag_accepted_before_subcommand(tmp_path, capsys):
    """Regression test: --db must work when placed BEFORE the subcommand."""
    """Previously argparse consumed the path as the subcommand name."""
    db_path = tmp_path / "flag-order.db"

    register_exit = main([
        "--db", str(db_path),
        "register",
        "--agent-id", "flag-order-agent",
        "--name", "Flag Order",
        "--capability", "order_check",
    ])
    assert register_exit == 0
    capsys.readouterr()

    discover = main([
        "--db", str(db_path),
        "discover",
        "--capability", "order_check",
    ])
    assert discover == 0, "--db before subcommand must work"
    found = json.loads(capsys.readouterr().out)
    assert any(a["agent_id"] == "flag-order-agent" for a in found)
