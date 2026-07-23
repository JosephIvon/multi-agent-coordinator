"""Real multi-agent E2E validation for MAC v0.5.0.

Simulates two real AI agents (claude-code + qoder) collaborating on the
same project through a shared MAC ledger. Exercises the full path:

1. Plan creation + activation
2. Claude Code submits + completes task A (foundation)
3. Qoder submits task B depending on A, blocked until A completes
4. Dependency unlock verification
5. Handoff passing (Claude's handoff read by Qoder's review packet)
6. Review lifecycle: mark_review_ready -> accept_review
7. Metrics aggregation reflects the collaboration
8. [B-1] Review packet includes quality evidence
9. [B-2] Worker packet inlines upstream handoff summary
10. [B-3] TTL expiry recovers stuck tasks
11. [B-5] Reviewer capability validation gates accept/reject

Run: python examples/e2e_multi_agent.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mac.metrics import compute_metrics, format_table  # noqa: E402
from mac.protocol.messages import (  # noqa: E402
    AgentCapability,
    AgentCard,
    ConflictRecord,
    CoordinationPolicy,
    HandoffResult,
    TaskPayload,
    TaskTransfer,
    VerificationEntry,
)
from mac.registry import Registry  # noqa: E402
from mac.storage import SQLiteTaskLedger  # noqa: E402


def banner(title: str) -> None:
    print(f"\n{'=' * 70}\n  {title}\n{'=' * 70}")


def check(condition: bool, message: str) -> None:
    mark = "[OK]" if condition else "[FAIL]"
    print(f"  {mark} {message}")
    if not condition:
        raise AssertionError(f"CHECK FAILED: {message}")


def main() -> None:
    db_path = ROOT / "mac_e2e.db"
    for suffix in ("", "-wal", "-shm"):
        p = Path(f"{db_path}{suffix}")
        if p.exists():
            p.unlink()

    registry = Registry(
        SQLiteTaskLedger(db_path),
        policy=CoordinationPolicy(require_review=True),
    )

    # ------------------------------------------------------------------
    banner("Step 1: Register two agents with distinct capabilities")
    # ------------------------------------------------------------------
    registry.register(
        AgentCard(
            agent_id="claude-code",
            name="Claude Code",
            capabilities=[AgentCapability(name="write_code")],
            allowed_paths=["src/**"],
        )
    )
    registry.register(
        AgentCard(
            agent_id="qoder",
            name="Qoder",
            capabilities=[AgentCapability(name="write_test")],
            allowed_paths=["tests/**"],
        )
    )
    agents = registry.discover()
    check(len(agents) == 2, "Both agents registered")
    check({a.agent_id for a in agents} == {"claude-code", "qoder"}, "Agent IDs correct")

    # ------------------------------------------------------------------
    banner("Step 2: Create + activate a collaboration plan")
    # ------------------------------------------------------------------
    plan = registry.create_plan(
        goal="Add a string-utility module with full test coverage",
        created_by="planner",
        plan_id="plan-e2e",
    )
    check(plan.status == "draft", "Plan starts as draft")
    activated = registry.activate_plan("plan-e2e")
    check(activated.status == "active", "Plan activated")

    # ------------------------------------------------------------------
    banner("Step 3: Claude Code submits task A (foundation, no deps)")
    # ------------------------------------------------------------------
    task_a = TaskTransfer(
        task_id="task-write-utility",
        plan_id="plan-e2e",
        source_agent_id="planner",
        payload=TaskPayload(
            type="write_code",
            summary="Implement src/stringutil.py with snake/camel converters",
        ),
        priority=8,
    )
    registry.submit_task(task_a)

    # ------------------------------------------------------------------
    banner("Step 4: Qoder submits task B (depends on A — must stay blocked)")
    # ------------------------------------------------------------------
    task_b = TaskTransfer(
        task_id="task-test-utility",
        plan_id="plan-e2e",
        source_agent_id="planner",
        depends_on=["task-write-utility"],
        payload=TaskPayload(
            type="write_test",
            summary="Write tests for src/stringutil.py, target 90% coverage",
            target_module="src/stringutil.py",
            coverage_goal=90,
        ),
        priority=7,
    )
    registry.submit_task(task_b)

    ready_before = registry.list_ready_tasks()
    ready_ids = {t.task_id for t in ready_before}
    check("task-test-utility" not in ready_ids, "Task B blocked while A incomplete")
    check("task-write-utility" in ready_ids, "Task A ready (no deps)")

    # ------------------------------------------------------------------
    banner("Step 5: Claude Code claims + starts task A")
    # ------------------------------------------------------------------
    claimed = registry.claim_next_task(
        agent_id="claude-code", capability="write_code"
    )
    check(claimed is not None, "Claude Code claimed task A")
    check(claimed.task_id == "task-write-utility", "Correct task claimed")
    check(claimed.target_agent_id == "claude-code", "Pinned to claude-code")
    registry.start_task(claimed.task_id, "claude-code")

    # ------------------------------------------------------------------
    banner("Step 6: Claude Code marks review-ready with structured handoff")
    # ------------------------------------------------------------------
    handoff_a = HandoffResult(
        task_id="task-write-utility",
        plan_id="plan-e2e",
        agent_id="claude-code",
        changed_files=["src/stringutil.py"],
        verification=[
            VerificationEntry(command="python -c 'import stringutil'", result="pass"),
        ],
        risks=["No input validation for None yet"],
        boundary_review="pass",
    )
    review_ready = registry.mark_review_ready(
        "task-write-utility", agent_id="claude-code", handoff=handoff_a
    )
    check(review_ready.status == "review_ready", "Task A in review_ready")

    # Review packet should surface Claude's handoff to the reviewer
    packet = registry.prepare_review_packet("task-write-utility")
    check("claude-code" in packet, "Review packet shows Claude's agent_id")
    check("src/stringutil.py" in packet, "Review packet shows changed file")
    check("No input validation" in packet, "Review packet shows risks")

    # ------------------------------------------------------------------
    banner("Step 7: Planner accepts the review -> task A completed")
    # ------------------------------------------------------------------
    completed_a = registry.accept_review("task-write-utility", reviewer_id="planner")
    check(completed_a.status == "completed", "Task A accepted/completed")

    # ------------------------------------------------------------------
    banner("Step 8: Dependency unlock — task B should now be ready")
    # ------------------------------------------------------------------
    ready_after = registry.list_ready_tasks()
    ready_ids_after = {t.task_id for t in ready_after}
    check("task-test-utility" in ready_ids_after, "Task B unblocked after A completes")

    # ------------------------------------------------------------------
    banner("Step 9: Qoder claims task B and reads Claude's handoff context")
    # ------------------------------------------------------------------
    claimed_b = registry.claim_next_task(agent_id="qoder", capability="write_test")
    check(claimed_b is not None, "Qoder claimed task B")
    check(claimed_b.task_id == "task-test-utility", "Correct task claimed by Qoder")
    registry.start_task("task-test-utility", "qoder")

    # Worker packet gives Qoder the context it needs
    worker_packet = registry.prepare_worker_packet("task-test-utility", agent_id="qoder")
    check("task-test-utility" in worker_packet, "Worker packet names the task")
    check("write_test" in worker_packet, "Worker packet shows required capability")
    check("tests/**" in worker_packet, "Worker packet shows Qoder's allowed paths")
    # Dependency context so Qoder knows A is done
    check("task-write-utility" in worker_packet, "Worker packet shows upstream dependency")

    # Qoder can fetch the actual handoff left by Claude
    upstream_handoff = registry.get_handoff_result("task-write-utility")
    check(upstream_handoff is not None, "Qoder can read Claude's handoff")
    check("src/stringutil.py" in upstream_handoff.changed_files, "Handoff changed_files accessible")

    # ------------------------------------------------------------------
    banner("Step 10: Qoder completes task B (review flow with quality gate)")
    # ------------------------------------------------------------------
    # Qoder submits quality evidence first
    registry.submit_quality_result(
        "task-test-utility",
        {
            "agent_id": "qoder",
            "command": "python -m pytest tests/test_stringutil.py --cov=src/stringutil",
            "status": "passed",
            "evidence": ["coverage_report", "test_output"],
        },
    )
    review_ready_b = registry.mark_review_ready("task-test-utility", agent_id="qoder")
    check(review_ready_b.status == "review_ready", "Task B in review_ready")
    completed_b = registry.accept_review("task-test-utility", reviewer_id="planner")
    check(completed_b.status == "completed", "Task B accepted/completed")

    # ------------------------------------------------------------------
    banner("Step 11: Reject path — simulate a failed review")
    # ------------------------------------------------------------------
    task_c = TaskTransfer(
        task_id="task-bad-docs",
        plan_id="plan-e2e",
        source_agent_id="planner",
        payload=TaskPayload(type="write_code", summary="Write docs"),
    )
    registry.submit_task(task_c)
    registry.claim_next_task(agent_id="claude-code", capability="write_code")
    registry.start_task("task-bad-docs", "claude-code")
    registry.mark_review_ready("task-bad-docs", agent_id="claude-code")

    rejected = registry.reject_review(
        "task-bad-docs", reviewer_id="planner", reason="Docs missing examples"
    )
    check(rejected.status == "rejected", "Task C rejected")
    conflicts = registry.list_conflicts(plan_id="plan-e2e", resolved=False)
    reject_conflicts = [c for c in conflicts if c.source == "reject_review"]
    check(len(reject_conflicts) >= 1, "Rejection auto-recorded a conflict")
    check(
        "Docs missing examples" in reject_conflicts[-1].description,
        "Conflict carries rejection reason",
    )

    # ------------------------------------------------------------------
    banner("Step 12: Metrics reflect the collaboration")
    # ------------------------------------------------------------------
    metrics = compute_metrics(registry.ledger)
    print(format_table(metrics))
    check(metrics["samples"]["task_transfers"] == 3, "Metrics: 3 tasks tracked")
    check(metrics["samples"]["handoffs"] >= 1, "Metrics: handoffs counted")
    check(metrics["samples"]["quality_results"] == 1, "Metrics: quality evidence counted")
    check(metrics["active_agents"] == 2, "Metrics: 2 active agents")
    check(metrics["conflict_rate"] > 0, "Metrics: conflict detected from rejection")

    # ------------------------------------------------------------------
    banner("Step 13: Full audit trail per trace")
    # ------------------------------------------------------------------
    trail_a = registry.get_audit_trail(completed_a.trace_id)
    actions_a = [e.action for e in trail_a]
    print(f"  Task A audit: {actions_a}")
    check("submit_task" in actions_a, "Audit: submit recorded")
    check("mark_review_ready" in actions_a, "Audit: review_ready recorded")
    check("accept_review" in actions_a, "Audit: accept_review recorded")

    # ==================================================================
    # Phase B validation (v0.5.0)
    # ==================================================================

    # ------------------------------------------------------------------
    banner("Step 14 [B-1]: Review packet includes quality evidence")
    # ------------------------------------------------------------------
    # Submit a task with quality evidence, then verify the review packet
    task_d = TaskTransfer(
        task_id="task-with-quality",
        plan_id="plan-e2e",
        source_agent_id="planner",
        payload=TaskPayload(type="write_code", summary="Task with quality evidence"),
    )
    registry.submit_task(task_d)
    registry.claim_next_task(agent_id="claude-code", capability="write_code")
    registry.start_task("task-with-quality", "claude-code")

    # Submit quality evidence before marking review-ready
    registry.submit_quality_result(
        "task-with-quality",
        {
            "agent_id": "claude-code",
            "command": "ruff check src/feature.py",
            "status": "passed",
            "evidence": ["no issues found"],
        },
    )
    registry.submit_quality_result(
        "task-with-quality",
        {
            "agent_id": "claude-code",
            "command": "pytest -q",
            "status": "passed",
            "evidence": ["12 passed", "2 skipped"],
        },
    )
    registry.mark_review_ready("task-with-quality", agent_id="claude-code")

    review_pkt = registry.prepare_review_packet("task-with-quality")
    check("Quality Evidence" in review_pkt, "B-1: Review packet has Quality Evidence section")
    check("ruff check" in review_pkt, "B-1: Review packet shows ruff command")
    check("pytest -q" in review_pkt, "B-1: Review packet shows pytest command")
    check("passed" in review_pkt, "B-1: Review packet shows pass status")

    registry.accept_review("task-with-quality", reviewer_id="planner")

    # ------------------------------------------------------------------
    banner("Step 15 [B-2]: Worker packet inlines upstream handoff summary")
    # ------------------------------------------------------------------
    # Task E depends on task D (which is now completed with handoff)
    task_e = TaskTransfer(
        task_id="task-downstream",
        plan_id="plan-e2e",
        source_agent_id="planner",
        depends_on=["task-with-quality"],
        payload=TaskPayload(
            type="write_test",
            summary="Test the feature",
            target_module="src/feature.py",
            coverage_goal=80,
        ),
    )
    registry.submit_task(task_e)

    # Save handoff for task D so the worker packet can inline it
    registry.save_handoff_result(HandoffResult(
        task_id="task-with-quality",
        plan_id="plan-e2e",
        agent_id="claude-code",
        changed_files=["src/feature.py", "src/models.py"],
        risks=["manual browser check still pending"],
    ))

    # Claim and start task E
    registry.claim_next_task(agent_id="qoder", capability="write_test")
    registry.start_task("task-downstream", "qoder")

    worker_pkt_e = registry.prepare_worker_packet("task-downstream", agent_id="qoder")
    check(
        "Upstream Handoff: task-with-quality" in worker_pkt_e,
        "B-2: Worker packet inlines upstream handoff header",
    )
    check(
        "src/feature.py" in worker_pkt_e,
        "B-2: Worker packet shows upstream changed files",
    )
    check(
        "manual browser check" in worker_pkt_e,
        "B-2: Worker packet shows upstream risks",
    )

    # ------------------------------------------------------------------
    banner("Step 16 [B-3]: TTL expiry recovers stuck tasks")
    # ------------------------------------------------------------------
    # Submit a task with very short TTL, then simulate it being stuck
    task_stuck = TaskTransfer(
        task_id="task-stuck",
        plan_id="plan-e2e",
        source_agent_id="planner",
        payload=TaskPayload(type="write_code", summary="Task that will expire"),
        ttl_seconds=1,  # 1 second TTL
    )
    registry.submit_task(task_stuck)
    registry.claim_next_task(agent_id="claude-code", capability="write_code")
    registry.start_task("task-stuck", "claude-code")

    # Wait for TTL to pass
    time.sleep(2)

    expired = registry.expire_stale_tasks()
    expired_ids = {t.task_id for t in expired}
    check("task-stuck" in expired_ids, "B-3: Stuck task expired via TTL")
    # Verify the expired task is now failed
    stuck_task = registry.ledger.get_task_transfer("task-stuck")
    check(stuck_task is not None, "B-3: Expired task still exists in ledger")
    check(stuck_task.status == "failed", "B-3: Expired task status is failed")
    check(stuck_task.error_code == "TTL_EXPIRED", "B-3: Error code is TTL_EXPIRED")

    # ------------------------------------------------------------------
    banner("Step 17 [B-5]: Reviewer capability validation")
    # ------------------------------------------------------------------
    # Create a separate registry with reviewer_capability set
    db_cap = ROOT / "mac_e2e_cap.db"
    for suffix in ("", "-wal", "-shm"):
        p = Path(f"{db_cap}{suffix}")
        if p.exists():
            p.unlink()

    cap_registry = Registry(
        SQLiteTaskLedger(db_cap),
        policy=CoordinationPolicy(require_review=True, reviewer_capability="review_code"),
    )

    # Register agents — one with review_code, one without
    cap_registry.register(AgentCard(
        agent_id="good-reviewer",
        name="Good Reviewer",
        capabilities=[AgentCapability(name="review_code")],
    ))
    cap_registry.register(AgentCard(
        agent_id="bad-reviewer",
        name="Bad Reviewer",
        capabilities=[AgentCapability(name="write_code")],
    ))
    cap_registry.register(AgentCard(
        agent_id="worker",
        name="Worker",
        capabilities=[AgentCapability(name="write_code")],
    ))

    # Submit + claim + start + mark_review_ready
    cap_registry.submit_task(TaskTransfer(
        task_id="cap-task",
        payload=TaskPayload(type="write_code", summary="Capability test"),
    ))
    cap_registry.claim_next_task(agent_id="worker", capability="write_code")
    cap_registry.start_task("cap-task", "worker")
    cap_registry.mark_review_ready("cap-task", "worker")

    # Bad reviewer should be blocked
    try:
        cap_registry.accept_review("cap-task", reviewer_id="bad-reviewer")
        check(False, "B-5: Bad reviewer should have been blocked")
    except Exception as exc:
        check("lacks capability" in str(exc), "B-5: Bad reviewer blocked with capability error")

    # Good reviewer should succeed
    result = cap_registry.accept_review("cap-task", reviewer_id="good-reviewer")
    check(result.status == "completed", "B-5: Good reviewer accepted successfully")

    # Clean up capability test DB
    for suffix in ("", "-wal", "-shm"):
        p = Path(f"{db_cap}{suffix}")
        if p.exists():
            p.unlink()

    # ------------------------------------------------------------------
    banner("ALL E2E CHECKS PASSED")
    # ------------------------------------------------------------------
    print("""
  Validated with simulated agents:
    - claude-code (write_code, src/**)
    - qoder       (write_test, tests/**)

  Confirmed MAC v0.5.0 capabilities:
    [OK] Multi-agent registration with distinct capabilities + path boundaries
    [OK] Plan lifecycle (draft -> active)
    [OK] Dependency blocking + unlock on upstream completion
    [OK] Atomic claim + start
    [OK] Structured handoff with path guardrails
    [OK] Review lifecycle: mark_review_ready -> accept_review / reject_review
    [OK] Reject auto-records conflict with reason
    [OK] Worker/review packet generation with dependency + boundary context
    [OK] Cross-agent handoff reading (Qoder reads Claude's handoff)
    [OK] Trace metrics aggregation (6 indicators)
    [OK] Per-trace audit trail
    [OK] B-1: Review packet includes quality evidence summary
    [OK] B-2: Worker packet inlines upstream handoff for completed deps
    [OK] B-3: TTL expiry recovers stuck tasks (failed + TTL_EXPIRED)
    [OK] B-5: Reviewer capability validation gates accept/reject
""")


if __name__ == "__main__":
    main()
