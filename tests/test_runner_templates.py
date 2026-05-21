import sys

from mac.protocol.messages import ContextBundle, TaskPayload, TaskTransfer
from mac.registry import Registry
from mac.runner import LocalAgentTemplate, TaskRunResult, command_agent_template, pytest_agent_template
from mac.storage import SQLiteTaskLedger


def _registry(tmp_path) -> Registry:
    return Registry(SQLiteTaskLedger(tmp_path / "mac.db"))


def _write_test_task(task_id: str, *, project_context: str | None = None) -> TaskTransfer:
    return TaskTransfer(
        task_id=task_id,
        trace_id=f"trace-{task_id}",
        source_agent_id="planner",
        payload=TaskPayload(
            type="write_test",
            summary="Write focused tests",
            target_module="mac.runner.templates",
            coverage_goal=80,
        ),
        context=ContextBundle(summary="Template runner task"),
        project_context=project_context,
    )


def test_local_agent_template_creates_agent_card_with_declared_capability():
    template = LocalAgentTemplate(
        agent_id="reviewer",
        name="Review Agent",
        capability="code_review",
        handler=lambda task: TaskRunResult.passed(command="review", evidence=["review_notes"]),
        project_context="project-a",
        metadata={"role": "review"},
    )

    card = template.agent_card(project_context="project-b")

    assert card.agent_id == "reviewer"
    assert card.name == "Review Agent"
    assert card.capabilities[0].name == "code_review"
    assert card.project_context == "project-b"
    assert card.metadata == {"role": "review"}

    card.metadata["role"] = "mutated"
    assert template.metadata == {"role": "review"}


def test_local_agent_template_create_runner_reuses_existing_runner_loop(tmp_path):
    registry = _registry(tmp_path)
    registry.submit_task(_write_test_task("task-1"))
    template = LocalAgentTemplate(
        agent_id="runner",
        name="Runner",
        capability="write_test",
        handler=lambda task: TaskRunResult.passed(
            command="pytest related tests or smoke test",
            evidence=["test_output"],
            output=f"handled {task.task_id}",
        ),
    )

    completed = template.create_runner(registry=registry).run_once()

    assert completed is not None
    assert completed.status == "completed"
    assert completed.target_agent_id == "runner"
    assert [entry.action for entry in registry.get_audit_trail("trace-task-1")] == [
        "submit_task",
        "claim_task",
        "start_task",
        "submit_quality_result",
        "complete_task",
    ]


def test_command_agent_template_runs_configured_command(tmp_path):
    registry = _registry(tmp_path)
    registry.submit_task(_write_test_task("task-1"))
    template = command_agent_template(
        agent_id="command-runner",
        name="Command Runner",
        capability="write_test",
        command=[sys.executable, "-c", "print('ok')"],
        evidence_on_success=["test_output"],
    )

    completed = template.create_runner(registry=registry).run_once()

    assert completed is not None
    assert completed.status == "completed"
    assert registry.get_capability_score("command-runner", "write_test")["succeeded"] == 1


def test_command_agent_template_does_not_use_task_payload_test_commands(tmp_path):
    registry = _registry(tmp_path)
    registry.submit_task(
        TaskTransfer(
            task_id="task-unsafe-payload",
            trace_id="trace-unsafe-payload",
            source_agent_id="planner",
            payload=TaskPayload(
                type="validate_tests",
                summary="Validate tests",
                test_commands=[sys.executable, "-c", "raise SystemExit(9)"],
            ),
            context=ContextBundle(summary="Payload commands are descriptive only"),
        )
    )
    template = command_agent_template(
        agent_id="validator",
        name="Validator",
        capability="validate_tests",
        command=[sys.executable, "-c", "print('configured command only')"],
    )

    completed = template.create_runner(registry=registry).run_once()

    assert completed is not None
    assert completed.status == "completed"
    results = registry.ledger.get_quality_results("task-unsafe-payload")
    assert len(results) == 1
    assert "configured command only" in results[0]["output"]
    assert "raise SystemExit(9)" not in results[0]["command"]


def test_template_project_context_can_be_overridden_at_runner_creation(tmp_path):
    registry = _registry(tmp_path)
    registry.submit_task(_write_test_task("task-template", project_context="template-project"))
    registry.submit_task(_write_test_task("task-override", project_context="override-project"))
    template = LocalAgentTemplate(
        agent_id="runner",
        name="Runner",
        capability="write_test",
        handler=lambda task: TaskRunResult.passed(
            command="pytest related tests or smoke test",
            evidence=["test_output"],
        ),
        project_context="template-project",
    )

    completed = template.create_runner(registry=registry, project_context="override-project").run_once()

    assert completed is not None
    assert completed.task_id == "task-override"


def test_pytest_agent_template_runs_pytest_command(tmp_path):
    test_file = tmp_path / "test_smoke.py"
    test_file.write_text("def test_smoke():\n    assert True\n", encoding="utf-8")
    registry = _registry(tmp_path)
    registry.submit_task(
        TaskTransfer(
            task_id="task-pytest",
            trace_id="trace-pytest",
            source_agent_id="planner",
            payload=TaskPayload(
                type="validate_tests",
                summary="Run pytest",
                target_test_suite="smoke",
                validation_framework="pytest",
            ),
            context=ContextBundle(summary="Pytest template task"),
        )
    )
    template = pytest_agent_template(
        agent_id="pytest-runner",
        name="Pytest Runner",
        pytest_args=[str(test_file), "-q"],
        cwd=tmp_path,
    )

    completed = template.create_runner(registry=registry).run_once()

    assert completed is not None
    assert completed.status == "completed"
    results = registry.ledger.get_quality_results("task-pytest")
    assert results[0]["command"].startswith(sys.executable)
    assert "-m pytest" in results[0]["command"]
