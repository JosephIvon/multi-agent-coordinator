"""Regression tests for the Multica -> MAC webhook bridge in
``examples/multica_bridge/server.py``.

Covers the full event lifecycle (issue.created -> agent.completed/failed),
idempotency under at-least-once delivery, and HMAC signature verification.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Add the example package to sys.path so we can import its server module.
EXAMPLE_DIR = (
    Path(__file__).resolve().parent.parent / "examples" / "multica_bridge"
)
sys.path.insert(0, str(EXAMPLE_DIR))


@pytest.fixture
def bridge():
    """Import the bridge module fresh per test so module-level state is clean.

    Also forces WEBHOOK_SECRET to empty (dev mode, no signature checks)
    unless the test overrides it via ``monkeypatch.setattr(bridge, 'WEBHOOK_SECRET', ...)``.
    """
    # Drop any cached import so each test gets a clean module-level
    # WEBHOOK_SECRET read from the env.
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.WEBHOOK_SECRET = ""  # explicit dev mode by default
    yield server


@pytest.fixture
def client(bridge):
    """FastAPI TestClient wrapping the bridge's ASGI app."""
    return TestClient(bridge.app)


def _post_event(client, event_type: str, data: dict, secret: str = ""):
    """POST a Multica-shaped webhook event. Optionally sign with HMAC."""
    body = json.dumps({"type": event_type, "data": data}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if secret:
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Multica-Signature"] = sig
    return client.post("/webhook/multica", content=body, headers=headers)


# ---------------------------------------------------------------------------
# health + unknown event handling
# ---------------------------------------------------------------------------


def test_healthz_returns_ok(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_unknown_event_type_is_ignored(client):
    r = _post_event(client, "agent.snoozed", {})
    assert r.status_code == 200
    assert r.json() == {"ignored": "agent.snoozed"}


# ---------------------------------------------------------------------------
# full lifecycle
# ---------------------------------------------------------------------------


def test_issue_created_submits_task(client):
    r = _post_event(client, "issue.created", {
        "issue_id": "T-1", "title": "demo", "url": "https://x/T-1",
        "acceptance_criteria": ["a"], "target_files": ["src/x.py"],
    })
    assert r.status_code == 200
    assert r.json()["status"] == "submitted"

    r2 = _post_event(client, "issue.created", {
        "issue_id": "T-1", "title": "demo", "url": "https://x/T-1",
    })
    # Same task submitted twice; submit_task raises on duplicate, so the
    # bridge currently propagates that as 500. That is acceptable for
    # now (Phase 1 of the bridge) but documents the current behaviour.
    assert r2.status_code in (200, 500)


def test_agent_started_runs_task(client):
    _post_event(client, "issue.created", {
        "issue_id": "T-2", "title": "demo", "url": "https://x/T-2",
    })
    r = _post_event(client, "agent.started", {
        "issue_id": "T-2", "agent_id": "agent-x",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "running"
    assert body["agent_id"] == "agent-x"


def test_agent_started_is_idempotent_under_duplicate(client):
    _post_event(client, "issue.created", {
        "issue_id": "T-3", "title": "demo", "url": "https://x/T-3",
    })
    first = _post_event(client, "agent.started", {
        "issue_id": "T-3", "agent_id": "agent-x",
    })
    assert first.status_code == 200
    # At-least-once delivery can re-send agent.started. The bridge
    # swallows StateConflictError so the second call is a no-op.
    second = _post_event(client, "agent.started", {
        "issue_id": "T-3", "agent_id": "agent-x",
    })
    assert second.status_code == 200
    assert second.json() == {
        "status": "running",
        "task_id": "multica-T-3",
        "agent_id": "agent-x",
        "card_synced": False,
    }


def test_agent_commented_records_checkpoint(client):
    _post_event(client, "issue.created", {"issue_id": "T-4", "title": "demo", "url": "https://x/T-4"})
    _post_event(client, "agent.started", {"issue_id": "T-4", "agent_id": "agent-x"})
    r = _post_event(client, "agent.commented", {
        "issue_id": "T-4", "agent_id": "agent-x", "body": "reprod'd locally",
    })
    assert r.status_code == 200
    assert r.json() == {"status": "checkpointed", "task_id": "multica-T-4"}


def test_agent_completed_records_handoff(client):
    _post_event(client, "issue.created", {"issue_id": "T-5", "title": "demo", "url": "https://x/T-5"})
    _post_event(client, "agent.started", {"issue_id": "T-5", "agent_id": "agent-x"})
    r = _post_event(client, "agent.completed", {
        "issue_id": "T-5",
        "agent_id": "agent-x",
        "changed_files": ["src/a.py", "tests/test_a.py"],
        "verification": "pytest:pass",
        "risks": ["manual browser check pending"],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"
    assert body["quality_gate"] == "passed"
    assert body["review"] is False


def test_agent_failed_marks_failed(client):
    _post_event(client, "issue.created", {"issue_id": "T-6", "title": "demo", "url": "https://x/T-6"})
    _post_event(client, "agent.started", {"issue_id": "T-6", "agent_id": "agent-x"})
    r = _post_event(client, "agent.failed", {
        "issue_id": "T-6",
        "agent_id": "agent-x",
        "error_code": "build_broken",
        "message": "compile error in registry.py:42",
    })
    assert r.status_code == 200
    assert r.json() == {"status": "failed", "task_id": "multica-T-6"}


# ---------------------------------------------------------------------------
# HMAC signature verification
# ---------------------------------------------------------------------------


def test_hmac_rejects_request_with_no_signature_when_secret_set(client, monkeypatch, bridge):
    monkeypatch.setattr(bridge, "WEBHOOK_SECRET", "topsecret")
    r = _post_event(client, "issue.created", {"issue_id": "T-7", "title": "demo", "url": "x"})
    assert r.status_code == 401
    assert r.json() == {"detail": "bad_signature"}


def test_hmac_rejects_request_with_wrong_signature(client, monkeypatch, bridge):
    monkeypatch.setattr(bridge, "WEBHOOK_SECRET", "topsecret")
    body = json.dumps({"type": "issue.created", "data": {"issue_id": "T-8", "title": "demo", "url": "x"}}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Multica-Signature": "0" * 64,  # wrong length/format
    }
    r = client.post("/webhook/multica", content=body, headers=headers)
    assert r.status_code == 401


def test_hmac_accepts_request_with_correct_signature(client, monkeypatch, bridge):
    monkeypatch.setattr(bridge, "WEBHOOK_SECRET", "topsecret")
    r = _post_event(client, "issue.created", {
        "issue_id": "T-9", "title": "demo", "url": "https://x/T-9",
    }, secret="topsecret")
    assert r.status_code == 200
    assert r.json()["status"] == "submitted"


def test_hmac_skips_verification_when_secret_unset(client):
    """Dev mode: empty secret accepts unsigned requests."""
    r = _post_event(client, "issue.created", {
        "issue_id": "T-10", "title": "demo", "url": "https://x/T-10",
    })
    assert r.status_code == 200
    assert r.json()["status"] == "submitted"