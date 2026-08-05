"""E2E multi-agent collaboration test.

Covers Scenario 1 (role routing pipeline), Scenario 2 (quality gate hard-fail/resume),
Scenario 3 (lease expiry/retry), Scenario 4 (facts cross-agent sharing),
Scenario 5 (kanban board completeness).

Run with: python scripts/e2e_multi_agent.py
"""

import sys
import time
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mac.registry import Registry
from mac.storage.sqlite import SQLiteTaskLedger
from mac.protocol.messages import (
    AgentCard,
    AgentCapability,
    TaskTransfer,
    TaskPayload,
    ContextBundle,
)
from mac.testing.contracts import TestContract

PASS = "[PASS]"
FAIL = "[FAIL]"
INFO = "[INFO]"


def _temp_db():
    """Create a temp file safe for Windows (no lingering handle)."""
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close()
    return db.name


# ---------------------------------------------------------------------------
# Scenario 1: Arch -> CRUD -> Test pipeline (role routing)
# ---------------------------------------------------------------------------

def test_scenario_1_arch_crud_test_pipeline():
    """S1: role-restricted pipeline with dependency chain and role routing."""
    db_path = _temp_db()
    try:
        reg = Registry(SQLiteTaskLedger(db_path))

        # Register 3 agents with distinct roles.
        reg.register_agent(AgentCard(
            agent_id="arch-agent",
            name="Codex",
            capabilities=[AgentCapability(name="arch")],
            roles=["arch"],
        ))
        reg.register_agent(AgentCard(
            agent_id="crud-agent",
            name="Trae",
            capabilities=[AgentCapability(name="crud")],
            roles=["crud"],
        ))
        reg.register_agent(AgentCard(
            agent_id="test-agent",
            name="TestBot",
            capabilities=[AgentCapability(name="test")],
            roles=["test"],
        ))

        # -- Submit arch task (no deps) --
        reg.submit_task(TaskTransfer(
            task_id="t-arch",
            payload=TaskPayload(type="arch", summary="Design architecture"),
            required_role="arch",
        ))

        # -- Submit crud task (depends on arch) --
        reg.submit_task(TaskTransfer(
            task_id="t-crud",
            payload=TaskPayload(type="crud", summary="Implement CRUD"),
            required_role="crud",
            depends_on=["t-arch"],
        ))

        # -- Submit test task (depends on crud) --
        reg.submit_task(TaskTransfer(
            task_id="t-test",
            payload=TaskPayload(type="test", summary="Write tests"),
            required_role="test",
            depends_on=["t-crud"],
        ))

        # --- Verify: crud agent cannot claim arch task ---
        claimed = reg.claim_next_task(
            agent_id="crud-agent", capability="crud",
        )
        assert claimed is None, (
            f"crud-agent should NOT claim arch task, got {claimed}"
        )
        print(f"  {PASS} S1 crud agent blocked from arch task (role gate)")

        # --- Verify: arch agent CAN claim arch task ---
        claimed = reg.claim_next_task(
            agent_id="arch-agent", capability="arch",
        )
        assert claimed is not None, "arch-agent should claim arch task"
        assert claimed.task_id == "t-arch"
        print(f"  {PASS} S1 arch agent claimed arch task")

        # Complete the arch task so the crud task becomes ready.
        # claim_next_task transitions proposed->accepted; need start_task to get running.
        reg.start_task("t-arch", "arch-agent")
        result = reg.done(
            "t-arch", "arch-agent",
            quality_result={"command": "pytest", "status": "passed", "evidence": ["test_output"]},
        )
        assert result["status"] == "completed"
        print(f"  {PASS} S1 arch task completed")

        # --- Arch done -> crud should now be claimable ---
        # crud-agent tries again (still blocked by role: test-agent should not claim it)
        claimed = reg.claim_next_task(
            agent_id="test-agent", capability="test",
        )
        assert claimed is None, (
            f"test-agent should NOT claim crud task, got {claimed}"
        )
        print(f"  {PASS} S1 test agent blocked from crud task (role gate)")

        # crud-agent claims crud task
        claimed = reg.claim_next_task(
            agent_id="crud-agent", capability="crud",
        )
        assert claimed is not None, "crud-agent should claim crud task"
        assert claimed.task_id == "t-crud"
        print(f"  {PASS} S1 crud agent claimed crud task (after arch completed)")

        # Complete crud task.
        # claim_next_task already did proposed->accepted; just start.
        reg.start_task("t-crud", "crud-agent")
        result = reg.done(
            "t-crud", "crud-agent",
            quality_result={"command": "pytest", "status": "passed", "evidence": ["test_output"]},
        )
        assert result["status"] == "completed"
        print(f"  {PASS} S1 crud task completed")

        # --- Crud done -> test task should be claimable ---
        claimed = reg.claim_next_task(
            agent_id="test-agent", capability="test",
        )
        assert claimed is not None, "test-agent should claim test task"
        assert claimed.task_id == "t-test"
        print(f"  {PASS} S1 test agent claimed test task (after crud completed)")

        # Complete the pipeline.
        # claim_next_task already did proposed->accepted; just start.
        reg.start_task("t-test", "test-agent")
        result = reg.done(
            "t-test", "test-agent",
            quality_result={"command": "pytest", "status": "passed", "evidence": ["test_output"]},
        )
        assert result["status"] == "completed"
        print(f"  {PASS} S1 test task completed -- pipeline finished")

    finally:
        try:
            Path(db_path).unlink(missing_ok=True)
        except OSError:
            pass  # Windows may hold a transient lock


# ---------------------------------------------------------------------------
# Scenario 2: Quality gate hard-fail and resume
# ---------------------------------------------------------------------------

def test_scenario_2_quality_gate_hard_fail_and_resume():
    """S2: done() on high-risk task without evidence -> blocked; resubmit with evidence -> completed."""
    db_path = _temp_db()
    try:
        reg = Registry(SQLiteTaskLedger(db_path))
        reg.register_agent(AgentCard(
            agent_id="a-worker",
            name="Worker",
            capabilities=[AgentCapability(name="write_code")],
        ))

        task = TaskTransfer(
            task_id="t-high-risk",
            payload=TaskPayload(type="write_code", summary="Critical security patch"),
            context=ContextBundle(
                summary="High risk patch",
                acceptance_criteria=["tests pass", "security review done", "docs updated"],
            ),
            test_contract=TestContract.for_risk("high"),
        )
        reg.submit_task(task)
        reg.accept_handoff("t-high-risk", "a-worker")
        reg.start_task("t-high-risk", "a-worker")

        # Attempt 1: done() WITHOUT changelog and without met_acceptance_criteria
        result = reg.done(
            "t-high-risk", "a-worker",
            quality_result={
                "command": "python -m pytest --cov",
                "status": "passed",
                "evidence": ["test_output", "coverage_report", "review_notes"],
            },
            # has_changelog defaults to False -> BLOCKED
            # met_acceptance_criteria defaults to None -> BLOCKED
        )

        assert result["status"] == "blocked", (
            f"Expected blocked, got {result['status']}"
        )
        assert result["quality_gate"] == "failed"
        print(f"  {PASS} S2 done() without changelog/acceptance -> blocked")

        task_check = reg.get_task("t-high-risk")
        assert task_check.status == "blocked"
        assert task_check.error_code == "TASK_BLOCKED"
        print(f"  {PASS} S2 task status is blocked with error_code TASK_BLOCKED")

        # Resume the blocked task
        reg.resume_blocked_task("t-high-risk", agent_id="a-worker")
        task_check = reg.get_task("t-high-risk")
        assert task_check.status == "proposed"
        print(f"  {PASS} S2 task resumed to proposed")

        # Attempt 2: done() WITH all evidence
        reg.accept_handoff("t-high-risk", "a-worker")
        reg.start_task("t-high-risk", "a-worker")
        result = reg.done(
            "t-high-risk", "a-worker",
            quality_result={
                "command": "python -m pytest --cov",
                "status": "passed",
                "evidence": ["test_output", "coverage_report", "review_notes"],
            },
            has_changelog=True,
            met_acceptance_criteria=["tests pass", "security review done", "docs updated"],
        )

        assert result["status"] == "completed", (
            f"Expected completed, got {result['status']}"
        )
        assert result["quality_gate"] == "passed"
        print(f"  {PASS} S2 resubmitted with full evidence -> completed")

    finally:
        try:
            Path(db_path).unlink(missing_ok=True)
        except OSError:
            pass  # Windows may hold a transient lock


# ---------------------------------------------------------------------------
# Scenario 3: Lease expiry and retry
# ---------------------------------------------------------------------------

def test_scenario_3_lease_expiry_and_retry():
    """S3: task with short lease expires, auto-retries, completes on second claim."""
    db_path = _temp_db()
    try:
        reg = Registry(SQLiteTaskLedger(db_path))
        reg.register_agent(AgentCard(
            agent_id="agent-3",
            name="LeaseAgent",
            capabilities=[AgentCapability(name="write_code")],
        ))

        task = TaskTransfer(
            task_id="t-lease-3",
            payload=TaskPayload(type="write_code", summary="Lease expiry test"),
            lease_seconds=3,
        )
        reg.submit_task(task)
        reg.accept_handoff("t-lease-3", "agent-3")
        reg.start_task("t-lease-3", "agent-3")

        t = reg.get_task("t-lease-3")
        assert t.claimed_at, "claimed_at should be set after accept+start"
        print(f"  {INFO} S3 claimed_at = {t.claimed_at}")

        # Wait for lease to expire
        print(f"  {INFO} S3 waiting 4s for lease to expire...")
        time.sleep(4)

        expired = reg.expire_task_leases(auto_retry=True)
        assert len(expired) == 1, f"Expected 1 expired, got {len(expired)}"
        assert expired[0].task_id == "t-lease-3"
        print(f"  {PASS} S3 lease expired, task auto-retried")

        t = reg.get_task("t-lease-3")
        assert t.status == "proposed", (
            f"Expected proposed after expiry, got {t.status}"
        )
        assert t.claimed_at == "", "claimed_at should be cleared after release"
        assert t.retry_count == 1, (
            f"Expected retry_count=1, got {t.retry_count}"
        )
        print(f"  {PASS} S3 task back to proposed with retry_count=1")

        # Re-claim and complete successfully
        claimed = reg.claim_next_task(
            agent_id="agent-3", capability="write_code",
        )
        assert claimed is not None, "Should be able to re-claim after lease expiry"
        assert claimed.task_id == "t-lease-3"
        # claim_next_task already did proposed->accepted; just start.
        reg.start_task("t-lease-3", "agent-3")
        result = reg.done(
            "t-lease-3", "agent-3",
            quality_result={"command": "pytest", "status": "passed", "evidence": ["test_output"]},
        )
        assert result["status"] == "completed"
        print(f"  {PASS} S3 task completed on retry")

    finally:
        try:
            Path(db_path).unlink(missing_ok=True)
        except OSError:
            pass  # Windows may hold a transient lock


# ---------------------------------------------------------------------------
# Scenario 4: Facts cross-agent sharing
# ---------------------------------------------------------------------------

def test_scenario_4_facts_cross_agent_sharing():
    """S4: Agent A remembers facts, Agent B recalls them by query."""
    db_path = _temp_db()
    try:
        reg = Registry(SQLiteTaskLedger(db_path))

        # Agent A remembers 3 facts
        reg.remember_fact("arch-pattern", "Use hexagonal architecture for new services", "architecture")
        reg.remember_fact("db-migration", "Always use timestamp-prefixed migration files", "database")
        reg.remember_fact("ci-secret", "GitHub secrets must be repo-scoped, not org-scoped", "security")

        print(f"  {PASS} S4 Agent A remembered 3 facts")

        # Agent B recalls all facts
        all_facts = reg.recall_facts("", 10)
        assert len(all_facts) >= 3, f"Expected >= 3 facts, got {len(all_facts)}"
        print(f"  {PASS} S4 Agent B recalled all facts (found {len(all_facts)})")

        # Agent B queries by keyword
        found = reg.recall_facts("hexagonal architecture")
        assert len(found) >= 1, f"Expected >= 1 result for architecture query, got {len(found)}"
        assert found[0]["key"] == "arch-pattern"
        print(f"  {PASS} S4 Agent B found architecture fact by query")

        # Agent B queries by another keyword
        found = reg.recall_facts("migration")
        assert len(found) >= 1, f"Expected >= 1 result for migration query, got {len(found)}"
        assert found[0]["key"] == "db-migration"
        print(f"  {PASS} S4 Agent B found database fact by query")

        # Agent B queries by yet another keyword
        found = reg.recall_facts("secret")
        assert len(found) >= 1, f"Expected >= 1 result for secret query, got {len(found)}"
        assert found[0]["key"] == "ci-secret"
        print(f"  {PASS} S4 Agent B found security fact by query")

    finally:
        try:
            Path(db_path).unlink(missing_ok=True)
        except OSError:
            pass  # Windows may hold a transient lock


# ---------------------------------------------------------------------------
# Scenario 5: Kanban board reflects all states
# ---------------------------------------------------------------------------

def test_scenario_5_kanban_board_all_states():
    """S5: verify get_kanban() returns correct counts for tasks in all states."""
    db_path = _temp_db()
    try:
        reg = Registry(SQLiteTaskLedger(db_path))

        reg.register_agent(AgentCard(
            agent_id="kanban-agent",
            name="KanbanAgent",
            capabilities=[AgentCapability(name="custom")],
        ))

        today = "2026-08-05T12:00:00Z"

        # Submit tasks in various states
        for i, status in enumerate(["proposed", "proposed", "accepted", "running",
                                      "review_ready", "completed", "failed"]):
            t = TaskTransfer(
                task_id=f"t-kanban-{i}",
                payload=TaskPayload(type="custom", summary=f"Kanban task {i}"),
                status=status,
                updated_at=today,
                created_at=today,
                target_agent_id="kanban-agent" if status != "proposed" else None,
            )
            reg.ledger.save_task_transfer(t)

        board = reg.get_kanban()

        # Red: proposed tasks
        assert board["red"]["count"] == 2, (
            f"Expected 2 proposed, got {board['red']['count']}"
        )
        print(f"  {PASS} S5 kanban red (proposed): {board['red']['count']}")

        # Yellow: accepted + running
        assert board["yellow"]["count"] == 2, (
            f"Expected 2 in-progress, got {board['yellow']['count']}"
        )
        print(f"  {PASS} S5 kanban yellow (accepted+running): {board['yellow']['count']}")

        # Green: review_ready
        assert board["green"]["count"] == 1, (
            f"Expected 1 review_ready, got {board['green']['count']}"
        )
        print(f"  {PASS} S5 kanban green (review_ready): {board['green']['count']}")

        # Done: completed today
        assert board["done"]["total"] == 1, (
            f"Expected 1 completed, got {board['done']['total']}"
        )
        print(f"  {PASS} S5 kanban done (completed): {board['done']['total']}")

        # Failed tasks should not appear in any bucket
        # (kanban only covers proposed/accepted/running/review_ready/completed)
        total_visible = (
            board["red"]["count"]
            + board["yellow"]["count"]
            + board["green"]["count"]
            + board["done"]["total"]
        )
        # 2 proposed + 2 yellow + 1 green + 1 done = 6 visible, 1 failed invisible
        assert total_visible == 6, (
            f"Expected 6 visible tasks, got {total_visible}"
        )
        print(f"  {PASS} S5 kanban total visible: {total_visible} (failed excluded)")

    finally:
        try:
            Path(db_path).unlink(missing_ok=True)
        except OSError:
            pass  # Windows may hold a transient lock


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("MAC E2E Multi-Agent Collaboration Tests")
    print("=" * 60)

    all_pass = True

    scenarios = [
        ("Scenario 1: Arch->CRUD->Test pipeline (role routing)", test_scenario_1_arch_crud_test_pipeline),
        ("Scenario 2: Quality gate hard-fail and resume", test_scenario_2_quality_gate_hard_fail_and_resume),
        ("Scenario 3: Lease expiry and retry", test_scenario_3_lease_expiry_and_retry),
        ("Scenario 4: Facts cross-agent sharing", test_scenario_4_facts_cross_agent_sharing),
        ("Scenario 5: Kanban board reflects all states", test_scenario_5_kanban_board_all_states),
    ]

    for label, test_fn in scenarios:
        print(f"\n--- {label} ---")
        try:
            test_fn()
        except Exception as exc:
            print(f"  {FAIL} {label} raised: {exc}")
            all_pass = False

    print()
    print("=" * 60)
    if all_pass:
        print("All multi-agent E2E tests PASSED")
    else:
        print("Some multi-agent E2E tests FAILED")
        sys.exit(1)
