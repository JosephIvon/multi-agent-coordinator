from mac.protocol.messages import ContextBundle, TaskPayload, TaskTransfer
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger
from mac.testing.contracts import TestContract


def _high_risk_registry(tmp_path) -> Registry:
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(
        TaskTransfer(
            task_id="task-1",
            trace_id="trace-1",
            source_agent_id="planner",
            payload=TaskPayload(
                type="write_test",
                summary="Write quality preview tests",
                target_module="mac.registry",
                coverage_goal=85,
                risk_level="high",
            ),
            context=ContextBundle(summary="Quality preview task"),
            test_contract=TestContract.for_risk("high"),
        )
    )
    return registry


def test_quality_gate_preview_reports_missing_evidence_without_mutating_task(tmp_path):
    registry = _high_risk_registry(tmp_path)
    registry.submit_quality_result(
        "task-1",
        {
            "agent_id": "tester",
            "command": "python -m pytest --cov",
            "status": "passed",
            "evidence": ["test_output"],
        },
    )
    original = registry.get_task("task-1")
    assert original is not None
    before_audit = [entry.action for entry in registry.get_audit_trail("trace-1")]

    preview = registry.preview_quality_gate("task-1")
    after = registry.get_task("task-1")
    assert after is not None
    after_audit = [entry.action for entry in registry.get_audit_trail("trace-1")]

    assert preview is not None
    assert preview.task_id == "task-1"
    assert preview.trace_id == "trace-1"
    assert preview.has_contract is True
    assert preview.allowed is False
    assert preview.reason == "missing_evidence:coverage_report,review_notes"
    assert preview.required_commands == ["python -m pytest --cov"]
    assert preview.required_evidence == ["test_output", "coverage_report", "review_notes"]
    assert preview.passed_commands == ["python -m pytest --cov"]
    assert preview.present_evidence == ["test_output"]
    assert preview.missing_commands == []
    assert preview.missing_evidence == ["coverage_report", "review_notes"]
    assert preview.quality_results_count == 1
    assert after.updated_at == original.updated_at
    assert after_audit == before_audit


def test_quality_gate_preview_reports_allowed_when_contract_is_satisfied(tmp_path):
    registry = _high_risk_registry(tmp_path)
    registry.submit_quality_result(
        "task-1",
        {
            "agent_id": "tester",
            "command": "python -m pytest --cov",
            "status": "passed",
            "evidence": ["test_output", "coverage_report", "review_notes"],
        },
    )

    preview = registry.preview_quality_gate("task-1")

    assert preview is not None
    assert preview.allowed is True
    assert preview.reason is None
    assert preview.missing_commands == []
    assert preview.missing_evidence == []


def test_quality_gate_preview_manual_override_preserves_missing_items(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    contract = TestContract.for_risk("high")
    contract.allow_manual_override = True
    registry.submit_task(
        TaskTransfer(
            task_id="task-override",
            trace_id="trace-override",
            source_agent_id="planner",
            payload=TaskPayload(
                type="write_test",
                summary="Manual override preview",
                target_module="mac.registry",
                coverage_goal=85,
                risk_level="high",
            ),
            context=ContextBundle(summary="Manual override preview"),
            test_contract=contract,
        )
    )
    registry.submit_quality_result(
        "task-override",
        {
            "agent_id": "tester",
            "command": "manual approval",
            "status": "passed",
            "evidence": [],
            "manual_override": True,
        },
    )

    preview = registry.preview_quality_gate("task-override")

    assert preview is not None
    assert preview.allowed is True
    assert preview.reason is None
    assert preview.missing_commands == ["python -m pytest --cov"]
    assert preview.missing_evidence == ["test_output", "coverage_report", "review_notes"]


def test_quality_gate_preview_ignores_failed_results_for_passed_commands_and_evidence(tmp_path):
    registry = _high_risk_registry(tmp_path)
    registry.submit_quality_result(
        "task-1",
        {
            "agent_id": "tester",
            "command": "python -m pytest --cov",
            "status": "failed",
            "evidence": ["test_output", "coverage_report", "review_notes"],
        },
    )

    preview = registry.preview_quality_gate("task-1")

    assert preview is not None
    assert preview.allowed is False
    assert preview.reason == "no_passed_results"
    assert preview.passed_commands == []
    assert preview.present_evidence == []
    assert preview.missing_commands == ["python -m pytest --cov"]
    assert preview.missing_evidence == ["test_output", "coverage_report", "review_notes"]


def test_quality_gate_preview_supports_dict_backed_contract(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    contract = TestContract.for_risk("low").model_dump()
    registry.submit_task(
        TaskTransfer(
            task_id="task-dict-contract",
            trace_id="trace-dict-contract",
            source_agent_id="planner",
            payload=TaskPayload(
                type="write_test",
                summary="Dict contract",
                target_module="mac.registry",
                coverage_goal=80,
            ),
            context=ContextBundle(summary="Dict contract"),
            test_contract=contract,
        )
    )

    preview = registry.preview_quality_gate("task-dict-contract")

    assert preview is not None
    assert preview.has_contract is True
    assert preview.required_commands == ["pytest related tests or smoke test"]
    assert preview.missing_commands == ["pytest related tests or smoke test"]


def test_quality_gate_preview_without_contract_is_allowed(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(
        TaskTransfer(
            task_id="task-no-contract",
            trace_id="trace-no-contract",
            source_agent_id="planner",
            payload=TaskPayload(type="custom", summary="No contract"),
            context=ContextBundle(summary="No contract"),
        )
    )

    preview = registry.preview_quality_gate("task-no-contract")

    assert preview is not None
    assert preview.has_contract is False
    assert preview.allowed is True
    assert preview.reason is None
    assert preview.required_commands == []
    assert preview.required_evidence == []
    assert preview.missing_commands == []
    assert preview.missing_evidence == []


def test_quality_gate_preview_returns_none_for_missing_task(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))

    assert registry.preview_quality_gate("missing") is None
