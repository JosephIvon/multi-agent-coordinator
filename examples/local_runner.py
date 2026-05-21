from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mac.protocol.messages import ContextBundle, TaskPayload, TaskTransfer
from mac.registry import Registry
from mac.runner import command_agent_template
from mac.storage import SQLiteTaskLedger


def main() -> None:
    with TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
        registry = Registry(SQLiteTaskLedger(Path(temp_dir) / "mac.db"))

        task = registry.submit_task(
            TaskTransfer(
                task_id="task-runner-1",
                trace_id="trace-runner-1",
                source_agent_id="planner",
                payload=TaskPayload(
                    type="validate_tests",
                    summary="Run the configured local validation command",
                    target_test_suite="adapter smoke check",
                    validation_framework="python",
                ),
                context=ContextBundle(
                    summary="Demonstrate one local adapter loop",
                    artifact_refs=["file://examples/local_runner.py"],
                    acceptance_criteria=[
                        "The adapter command is configured by the caller",
                        "The task payload is not treated as an executable command",
                        "Quality evidence is recorded before completion",
                    ],
                ),
            )
        )

        template = command_agent_template(
            agent_id="local-validator",
            name="Local Validator",
            capability="validate_tests",
            command=[sys.executable, "-c", "print('adapter validation passed')"],
            cwd=ROOT,
            timeout_seconds=10,
            evidence_on_success=["test_output"],
        )
        runner = template.create_runner(registry=registry)

        result = runner.run_once()
        audit = registry.get_audit_trail(task.trace_id)
        score = registry.get_capability_score("local-validator", "validate_tests")

        print(f"task status: {result.status if result else 'none'}")
        print(f"task id: {result.task_id if result else 'none'}")
        print("audit events:")
        for entry in audit:
            print(f"- {entry.action}")
        print(f"observed total: {score.get('total', 0)}")
        print(f"observed succeeded: {score.get('succeeded', 0)}")


if __name__ == "__main__":
    main()
