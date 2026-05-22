from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mac.protocol.messages import (  # noqa: E402
    AgentCapability,
    AgentCard,
    HandoffResult,
    TaskPayload,
    TaskTransfer,
    VerificationEntry,
)
from mac.registry import Registry  # noqa: E402
from mac.storage import SQLiteTaskLedger  # noqa: E402


def main() -> None:
    with TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
        registry = Registry(SQLiteTaskLedger(Path(temp_dir) / "mac.db"))

        registry.register(
            AgentCard(
                agent_id="coder",
                name="Coder",
                capabilities=[AgentCapability(name="write_code")],
                allowed_paths=["src/**"],
            )
        )
        registry.register(
            AgentCard(
                agent_id="tester",
                name="Tester",
                capabilities=[AgentCapability(name="write_test")],
                allowed_paths=["tests/**"],
            )
        )

        plan = registry.create_plan(plan_id="plan-demo", goal="Demonstrate collaboration plan", created_by="planner")
        registry.activate_plan(plan.plan_id)

        registry.submit_task(
            TaskTransfer(
                task_id="code-demo",
                plan_id=plan.plan_id,
                source_agent_id="planner",
                payload=TaskPayload(type="write_code", summary="Implement the demo feature"),
            )
        )
        registry.submit_task(
            TaskTransfer(
                task_id="test-demo",
                plan_id=plan.plan_id,
                source_agent_id="planner",
                payload=TaskPayload(
                    type="write_test",
                    summary="Test the demo feature",
                    target_files=["tests/test_demo.py"],
                    acceptance_criteria=["Demo behavior is covered"],
                ),
                depends_on=["code-demo"],
            )
        )

        ready_before = [task.task_id for task in registry.list_ready_tasks()]
        claimed = registry.claim_next_task(agent_id="coder", capability="write_code")
        registry.start_task(claimed.task_id, "coder")
        registry.complete_task(claimed.task_id, "coder")

        registry.save_handoff_result(
            HandoffResult(
                task_id="code-demo",
                plan_id=plan.plan_id,
                agent_id="coder",
                verification=[VerificationEntry(command="python -m pytest -q", result="pass")],
                changed_files=["src/demo.py"],
                risks=["manual integration check still recommended"],
            )
        )

        ready_after = [task.task_id for task in registry.list_ready_tasks()]
        worker_packet = registry.prepare_worker_packet("test-demo", agent_id="tester")
        review_packet = registry.prepare_review_packet("code-demo")

        print(f"plan: {plan.plan_id}")
        print(f"ready before completion: {ready_before}")
        print(f"ready after completion: {ready_after}")
        print("worker packet first line:", worker_packet.splitlines()[0])
        print("review packet first line:", review_packet.splitlines()[0])


if __name__ == "__main__":
    main()
