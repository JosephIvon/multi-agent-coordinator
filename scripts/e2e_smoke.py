"""E2E smoke test for C-2 (quality gate hard-fail) + C-1 (role routing) + D (lease expiry).

Run with: python scripts/e2e_smoke.py
Requires: MAC_DB_PATH set, or uses default mac.db
"""

import sys, time, tempfile, json
from pathlib import Path

# Ensure src is on path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mac.registry import Registry
from mac.storage.sqlite import SQLiteTaskLedger
from mac.protocol.messages import (
    AgentCard, AgentCapability, TaskTransfer, TaskPayload,
    ContextBundle,
)
from mac.testing.contracts import TestContract


def test_c2_quality_gate_hard_fail():
    """C-2: done() on high-risk task without changelog/acceptance → blocked."""
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close()
    db_path = db.name
    try:
        reg = Registry(SQLiteTaskLedger(db_path))
        reg.register_agent(AgentCard(
            agent_id="a1", name="Agent",
            capabilities=[AgentCapability(name="write_code")],
        ))

        task = TaskTransfer(
            task_id="t-c2",
            payload=TaskPayload(type="write_code", summary="C-2 smoke test"),
            test_contract=TestContract.for_risk("high"),
        )
        reg.submit_task(task)
        reg.accept_handoff("t-c2", "a1")
        reg.start_task("t-c2", "a1")

        # Submit quality evidence with correct commands but NO changelog/acceptance
        result = reg.done(
            "t-c2", "a1",
            quality_result={
                "command": "python -m pytest --cov",
                "status": "passed",
                "evidence": ["test_output", "coverage_report", "review_notes"],
            },
            # no has_changelog → False, no met_acceptance_criteria → None
        )

        task = reg.get_task("t-c2")
        assert result["status"] == "blocked", f"Expected blocked, got {result['status']}"
        assert result["quality_gate"] == "failed"
        assert task.status == "blocked", f"Task status should be blocked, got {task.status}"
        assert task.error_code == "TASK_BLOCKED"
        print("  [PASS] C-2 quality gate hard-fail")
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_c2_quality_gate_pass():
    """C-2: done() on high-risk task with all evidence → passes."""
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close()
    db_path = db.name
    try:
        reg = Registry(SQLiteTaskLedger(db_path))
        reg.register_agent(AgentCard(
            agent_id="a1", name="Agent",
            capabilities=[AgentCapability(name="write_code")],
        ))

        task = TaskTransfer(
            task_id="t-c2-pass",
            payload=TaskPayload(type="write_code", summary="C-2 pass test"),
            context=ContextBundle(
                summary="Test",
                acceptance_criteria=["tests pass", "docs updated"],
            ),
            test_contract=TestContract.for_risk("high"),
        )
        reg.submit_task(task)
        reg.accept_handoff("t-c2-pass", "a1")
        reg.start_task("t-c2-pass", "a1")

        result = reg.done(
            "t-c2-pass", "a1",
            quality_result={
                "command": "python -m pytest --cov",
                "status": "passed",
                "evidence": ["test_output", "coverage_report", "review_notes"],
            },
            has_changelog=True,
            met_acceptance_criteria=["tests pass", "docs updated"],
        )

        assert result["status"] == "completed"
        assert result["quality_gate"] == "passed"
        print("  [PASS] C-2 quality gate pass with full evidence")
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_c1_role_routing():
    """C-1: agent without matching role cannot claim role-restricted task."""
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close()
    db_path = db.name
    try:
        reg = Registry(SQLiteTaskLedger(db_path))

        # Register two agents: one arch, one crud
        reg.register_agent(AgentCard(
            agent_id="arch-agent", name="Codex",
            capabilities=[AgentCapability(name="arch")],
            roles=["arch", "review"],
        ))
        reg.register_agent(AgentCard(
            agent_id="crud-agent", name="Trae",
            capabilities=[AgentCapability(name="crud")],
            roles=["crud", "test"],
        ))

        # Submit an arch-role task (no target, open for claiming)
        reg.submit_task(TaskTransfer(
            task_id="t-arch-only",
            payload=TaskPayload(type="arch", summary="Design architecture"),
            required_role="arch",
        ))

        # crud agent tries to claim → should get None
        claimed = reg.claim_next_task(
            agent_id="crud-agent", capability="crud",
        )
        assert claimed is None, f"crud agent should not claim arch task, got {claimed}"
        print("  [PASS] C-1 crud agent blocked from arch task")

        # arch agent claims → should succeed
        claimed = reg.claim_next_task(
            agent_id="arch-agent", capability="arch",
        )
        assert claimed is not None, "arch agent should claim arch task"
        assert claimed.task_id == "t-arch-only"
        print("  [PASS] C-1 arch agent claimed arch task")

        # Submit a task without required_role → any agent can claim
        reg.submit_task(TaskTransfer(
            task_id="t-open",
            payload=TaskPayload(type="crud", summary="Add CRUD"),
        ))
        claimed = reg.claim_next_task(
            agent_id="crud-agent", capability="crud",
        )
        assert claimed is not None, "crud agent should claim open task"
        assert claimed.task_id == "t-open"
        print("  [PASS] C-1 open task claimable by any role")
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_d_lease_expiry():
    """D: task with lease_seconds auto-releases back to proposed."""
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close()
    db_path = db.name
    try:
        reg = Registry(SQLiteTaskLedger(db_path))
        reg.register_agent(AgentCard(
            agent_id="a1", name="Agent",
            capabilities=[AgentCapability(name="write_code")],
        ))

        # Submit with a short lease (2 seconds)
        task = TaskTransfer(
            task_id="t-lease",
            payload=TaskPayload(type="write_code", summary="Lease test"),
            lease_seconds=2,
        )
        reg.submit_task(task)
        reg.accept_handoff("t-lease", "a1")
        reg.start_task("t-lease", "a1")

        # Verify claimed_at was set
        t = reg.get_task("t-lease")
        assert t.claimed_at, "claimed_at should be set after accept+start"
        print(f"  [INFO] claimed_at = {t.claimed_at}")

        # Wait for lease to expire
        print("  [INFO] waiting 3s for lease to expire...")
        time.sleep(3)

        # Expire leases
        expired = reg.expire_task_leases(auto_retry=True)
        assert len(expired) == 1, f"Expected 1 expired, got {len(expired)}"
        assert expired[0].task_id == "t-lease"

        # Task should be back to proposed
        t = reg.get_task("t-lease")
        assert t.status == "proposed", f"Expected proposed, got {t.status}"
        assert t.claimed_at == "", "claimed_at should be cleared after release"
        assert t.retry_count == 1
        print("  [PASS] D lease expiry auto-release")
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_d_lease_no_limit():
    """D: task with lease_seconds=0 never expires."""
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close()
    db_path = db.name
    try:
        reg = Registry(SQLiteTaskLedger(db_path))
        reg.register_agent(AgentCard(
            agent_id="a1", name="Agent",
            capabilities=[AgentCapability(name="write_code")],
        ))

        task = TaskTransfer(
            task_id="t-no-lease",
            payload=TaskPayload(type="write_code", summary="No lease"),
            lease_seconds=0,
        )
        reg.submit_task(task)
        reg.accept_handoff("t-no-lease", "a1")

        expired = reg.expire_task_leases()
        assert len(expired) == 0
        print("  [PASS] D lease_seconds=0 never expires")
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_kanban_board():
    """C-3: get_kanban() returns correct kanban structure."""
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close()
    db_path = db.name
    try:
        reg = Registry(SQLiteTaskLedger(db_path))

        # Add tasks in different states
        for i, status in enumerate(["proposed", "accepted", "review_ready", "completed"]):
            t = TaskTransfer(
                task_id=f"t-{i}",
                payload=TaskPayload(type="custom", summary=f"Task {i}"),
                status=status,
                updated_at="2026-08-05T12:00:00Z",
                created_at="2026-08-05T12:00:00Z",
                target_agent_id="a1",
            )
            reg.ledger.save_task_transfer(t)

        board = reg.get_kanban()
        assert board["red"]["count"] == 1
        assert board["yellow"]["count"] == 1
        assert board["green"]["count"] == 1
        assert board["done"]["total"] == 1
        print("  [PASS] C-3 kanban board structure")
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_facts_remember_recall():
    """E: remember_fact + recall_facts round-trip."""
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close()
    db_path = db.name
    try:
        reg = Registry(SQLiteTaskLedger(db_path))

        reg.remember_fact("test-key", "test value content", "test")
        reg.remember_fact("bug-1", "Login timeout fix: increased to 30s", "bug")

        # Recall all
        all_facts = reg.recall_facts("", 10)
        assert len(all_facts) >= 2

        # Search by query
        found = reg.recall_facts("login timeout")
        assert len(found) == 1
        assert found[0]["key"] == "bug-1"
        print("  [PASS] E facts remember/recall round-trip")
    finally:
        Path(db_path).unlink(missing_ok=True)


if __name__ == "__main__":
    print("MAC v1.2.0 E2E Smoke Tests")
    print("=" * 60)

    test_c2_quality_gate_hard_fail()
    test_c2_quality_gate_pass()
    test_c1_role_routing()
    test_d_lease_expiry()
    test_d_lease_no_limit()
    test_kanban_board()
    test_facts_remember_recall()

    print()
    print("=" * 60)
    print("All smoke tests PASSED")
