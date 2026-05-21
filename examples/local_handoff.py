from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mac.protocol.messages import AgentCapability, AgentCard, ContextBundle, TaskPayload, TaskTransfer
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger
from mac.testing.contracts import TestContract


def main() -> None:
    with TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
        mac = Registry(SQLiteTaskLedger(Path(temp_dir) / "mac.db"))
        mac.register(AgentCard(agent_id="writer", name="Writer", capabilities=[AgentCapability(name="write_code")]))
        mac.register(AgentCard(agent_id="tester", name="Tester", capabilities=[AgentCapability(name="write_test")]))

        task = mac.submit_task(
            TaskTransfer(
                task_id="task-local-1",
                trace_id="trace-local-1",
                source_agent_id="writer",
                payload=TaskPayload(
                    type="write_test",
                    summary="Write focused tests for the registry lifecycle",
                    target_module="mac.registry",
                    coverage_goal=85,
                    risk_level="high",
                ),
                context=ContextBundle(
                    summary="Registry lifecycle work",
                    artifact_refs=["file://src/mac/registry.py"],
                    acceptance_criteria=["Task cannot complete until quality evidence is recorded"],
                ),
                test_contract=TestContract.for_risk("high"),
            )
        )

        mac.accept_handoff(task.task_id, "tester")
        mac.start_task(task.task_id, "tester")
        mac.submit_quality_result(
            task.task_id,
            {
                "command": "python -m pytest --cov",
                "status": "passed",
                "evidence": ["test_output", "coverage_report", "review_notes"],
            },
        )
        completed = mac.complete_task(task.task_id, "tester")
        audit = mac.get_audit_trail(completed.trace_id)

        print(f"completed task: {completed.task_id}")
        print(f"audit events: {len(audit)}")
        for entry in audit:
            print(f"- {entry.action}")


if __name__ == "__main__":
    main()
