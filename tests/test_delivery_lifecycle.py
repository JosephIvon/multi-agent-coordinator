from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from mac.adapters.lifecycle import SessionState
from mac.protocol import AgentCard, TaskTransfer
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger
from mac.transport import create_app


def setup(tmp_path):
    ledger = SQLiteTaskLedger(tmp_path / "mac.db")
    registry = Registry(ledger)
    registry.register(AgentCard(agent_id="worker", name="Worker"))
    registry.submit_task(TaskTransfer(task_id="t1", target_agent_id="worker"))
    registry.accept_handoff("t1", "worker")
    registry.start_task("t1", "worker")
    return ledger, registry


def test_session_persists_queries_and_recovers(tmp_path):
    ledger, _ = setup(tmp_path)
    session = SessionState(agent_id="worker", session_id="s1", task_id="t1", status="offline")
    ledger.save_session(session)
    reopened = SQLiteTaskLedger(tmp_path / "mac.db")
    assert reopened.get_session("s1") == session
    assert reopened.list_sessions(agent_id="worker")[0].task_id == "t1"

    client = TestClient(create_app(Registry(reopened)))
    recovered = client.post("/sessions/s1/recover").json()
    assert recovered["status"] == "online"
    assert recovered["last_heartbeat"] > 0


def test_http_auth_callback_idempotency_and_identity(tmp_path):
    ledger, registry = setup(tmp_path)
    ledger.save_session(SessionState(agent_id="worker", session_id="s1", task_id="t1", status="online"))
    client = TestClient(create_app(registry, token="secret-token"))
    payload = {"session_id": "s1", "task_id": "t1", "agent_id": "worker",
               "result": {"status": "completed", "summary": "ok"}}
    assert client.get("/tasks/t1").status_code == 401
    headers = {"Authorization": "Bearer secret-token"}
    first = client.post("/callbacks/e1", json=payload, headers=headers)
    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    assert registry.get_task("t1").status == "completed"
    second = client.post("/callbacks/e1", json=payload, headers=headers)
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert client.post("/callbacks/e2", json=payload, headers=headers).status_code == 409
    changed = {**payload, "result": {"status": "failed"}}
    assert client.post("/callbacks/e1", json=changed, headers=headers).status_code == 409


def test_blocked_callback_records_blocker_and_handoff(tmp_path):
    ledger, registry = setup(tmp_path)
    ledger.save_session(SessionState(agent_id="worker", session_id="s1", task_id="t1", status="online"))
    client = TestClient(create_app(registry))
    response = client.post("/callbacks/block-1", json={
        "session_id": "s1", "task_id": "t1", "agent_id": "worker",
        "result": {"status": "blocked", "blocker": "Need schema", "handoff_to": "architect"},
    })
    assert response.status_code == 200
    task = registry.get_task("t1")
    assert task.status == "blocked"
    assert task.fallback_agent_id == "architect"
    blockers = ledger.list_blockers(task_id="t1", status="open")
    assert blockers[0].reason == "Need schema"
    handoff = ledger.get_handoff_result("t1")
    assert handoff.boundary_review == "block"
    assert handoff.handoff_to == "architect"
    assert client.post("/tasks/t1/resume", json={"agent_id": "lead", "resolution": "schema supplied"}).status_code == 200
    assert registry.get_task("t1").status == "proposed"


def test_stale_session_goes_offline(tmp_path):
    ledger, registry = setup(tmp_path)
    ledger.save_session(SessionState(agent_id="worker", session_id="old", status="online", last_heartbeat=time.time() - 20))
    client = TestClient(create_app(registry))
    expired = client.post("/sessions/expire-stale?timeout_seconds=1").json()
    assert expired[0]["status"] == "offline"


def test_concurrent_agent_sessions_are_durable(tmp_path):
    ledger, _ = setup(tmp_path)

    def persist(index: int) -> str:
        session = SessionState(agent_id=f"agent-{index}", session_id=f"session-{index}",
                               task_id=None, status="online", last_heartbeat=time.time())
        ledger.save_session(session)
        return ledger.get_session(session.session_id).session_id

    with ThreadPoolExecutor(max_workers=12) as pool:
        assert set(pool.map(persist, range(40))) == {f"session-{i}" for i in range(40)}
    assert len(ledger.list_sessions(status="online")) == 40
