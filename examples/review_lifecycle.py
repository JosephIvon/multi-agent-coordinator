from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mac.protocol.messages import (  # noqa: E402
    AgentCapability,
    AgentCard,
    CoordinationPolicy,
    HandoffResult,
    TaskPayload,
    TaskTransfer,
    VerificationEntry,
)
from mac.registry import Registry  # noqa: E402
from mac.storage import SQLiteTaskLedger  # noqa: E402


def main() -> None:
    with TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
        registry = Registry(
            SQLiteTaskLedger(Path(temp_dir) / "mac.db"),
            policy=CoordinationPolicy(require_review=True),
        )
        registry.register(
            AgentCard(
                agent_id="worker",
                name="Worker",
                capabilities=[AgentCapability(name="write_code")],
            )
        )
        registry.submit_task(
            TaskTransfer(
                task_id="review-demo",
                source_agent_id="planner",
                payload=TaskPayload(
                    type="write_code",
                    summary="Implement a change that requires review",
                ),
            )
        )

        claimed = registry.claim_next_task(agent_id="worker", capability="write_code")
        registry.start_task(claimed.task_id, "worker")
        review_ready = registry.mark_review_ready(
            claimed.task_id,
            agent_id="worker",
            handoff=HandoffResult(
                task_id=claimed.task_id,
                agent_id="worker",
                changed_files=["src/feature.py"],
                verification=[VerificationEntry(command="python -m pytest -q", result="pass")],
            ),
        )
        completed = registry.accept_review(review_ready.task_id, reviewer_id="reviewer")

        print(f"review-ready task: {review_ready.task_id}")
        print(f"final status: {completed.status}")
        print("audit actions:")
        for entry in registry.get_audit_trail(completed.trace_id):
            print(f"- {entry.action}")


if __name__ == "__main__":
    main()
