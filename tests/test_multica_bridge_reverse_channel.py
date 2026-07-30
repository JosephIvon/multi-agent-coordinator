"""Tests for the Multica -> MAC -> Multica reverse channel.

The reverse channel is invoked from ``_on_agent_completed`` after a
successful MAC ``done()`` call. It POSTs the structured review packet
back to Multica as an issue comment. These tests cover the four code
paths:

* reverse channel skipped when ``MULTICA_API_URL`` is empty
* reverse channel POSTs the review packet on success
* reverse channel falls back to disk on API failure
* reverse channel never breaks the webhook response (always 200)

The test uses ``monkeypatch`` to substitute ``urllib.request.urlopen`` so
no real network call is made.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from urllib import error as urllib_error

import pytest
from fastapi.testclient import TestClient

EXAMPLE_DIR = (
    Path(__file__).resolve().parent.parent / "examples" / "multica_bridge"
)
sys.path.insert(0, str(EXAMPLE_DIR))
from mac.protocol.messages import AgentCard  # noqa: E402  (after sys.path insert is fine)


@pytest.fixture
def bridge():
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.WEBHOOK_SECRET = ""
    server.MULTICA_API_URL = ""
    server.MULTICA_API_TOKEN = ""
    server.REVIEW_FALLBACK_DIR = ""
    yield server


@pytest.fixture
def isolated_client(isolated_bridge):
    return TestClient(isolated_bridge.app)


@pytest.fixture
def isolated_bridge(tmp_path, monkeypatch):
    """Bridge backed by an isolated mac.db under tmp_path.

    The shared `bridge` fixture in this file uses the default mac.db
    path, which leaves stale tasks and conflict records between runs.
    Tests that exercise file-overlap conflict detection need a clean
    ledger so that `detect_file_overlap_conflicts` actually creates
    new ConflictRecords and the bridge sees them in the response.
    """
    db_path = tmp_path / "mac.db"
    monkeypatch.setenv("MAC_DB_PATH", str(db_path))
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    server.WEBHOOK_SECRET = ""
    server.MULTICA_API_URL = ""
    server.MULTICA_API_TOKEN = ""
    server.REVIEW_FALLBACK_DIR = ""
    yield server


@pytest.fixture
def client(bridge):
    return TestClient(bridge.app)


class _FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False
def _idem_of(req):
    """Return the ``Idempotency-Key`` value on a urllib Request.

    urllib normalizes header names to title-case-then-lowercase-rest
    (``Idempotency-key``), so a literal lookup is brittle. Match by
    lowering all keys -- wire traffic to Multica is case-insensitive
    per RFC 7230 either way.
    """
    return next(
        (v for k, v in req.headers.items() if k.lower() == "idempotency-key"),
        None,
    )


def _completed_payload(issue_id="R-1", agent_id="claude-frontend"):
    return {
        "issue_id": issue_id,
        "agent_id": agent_id,
        "changed_files": ["src/r.py"],
        "verification": "pytest:pass",
        "risks": ["manual browser test pending"],
    }


def _seed_submitted_task(client, issue_id):
    r = client.post(
        "/webhook/multica",
        json={"type": "issue.created", "data": {"issue_id": issue_id, "title": "demo", "url": "x"}},
    )
    assert r.status_code == 200
    r = client.post(
        "/webhook/multica",
        json={"type": "agent.started", "data": {"issue_id": issue_id, "agent_id": "claude-frontend"}},
    )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# behaviour
# ---------------------------------------------------------------------------


def test_reverse_channel_skipped_when_multica_url_empty(client, bridge, monkeypatch):
    _seed_submitted_task(client, "R-1")
    captured = []
    monkeypatch.setattr(bridge.urllib.request, "urlopen", lambda *a, **kw: captured.append((a, kw)) or _FakeResponse(200))
    monkeypatch.setattr(bridge, "MULTICA_API_URL", "")

    r = client.post("/webhook/multica", json={"type": "agent.completed", "data": _completed_payload()})
    assert r.status_code == 200
    # No POST attempted when MULTICA_API_URL is empty (dev mode).
    assert captured == []


def test_reverse_channel_posts_review_packet_on_success(client, bridge, monkeypatch):
    _seed_submitted_task(client, "R-2")
    monkeypatch.setattr(bridge, "MULTICA_API_URL", "http://multica.local:8080")
    monkeypatch.setattr(bridge, "MULTICA_API_TOKEN", "secret-token")

    captured = []

    def fake_urlopen(req, timeout=10):
        captured.append({
            "url": req.full_url,
            "method": req.get_method(),
            "headers": dict(req.headers),
            "body": req.data.decode("utf-8"),
        })
        return _FakeResponse(201)

    monkeypatch.setattr(bridge.urllib.request, "urlopen", fake_urlopen)

    r = client.post(
        "/webhook/multica",
        json={"type": "agent.completed", "data": _completed_payload("R-2")},
    )
    # The webhook always returns 200 -- reverse channel failure is non-fatal.
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"

    # Verify the outbound POST
    assert len(captured) == 1
    call = captured[0]
    assert call["url"] == "http://multica.local:8080/api/issues/R-2/comments"
    assert call["method"] == "POST"
    assert call["headers"]["Authorization"] == "Bearer secret-token"
    assert call["headers"]["Content-type"] == "application/json"
    payload = json.loads(call["body"])
    assert "body" in payload
    assert "multica-R-2" in payload["body"]
    assert "claude-frontend" in payload["body"]
    assert "`pytest`: pass" in payload["body"]  # formatter splits command:result
    assert "src/r.py" in payload["body"]


def test_reverse_channel_falls_back_to_disk_on_api_failure(
    isolated_client, isolated_bridge, monkeypatch, tmp_path,
):
    """Round 9: the old markdown-fallback (REVIEW_FALLBACK_DIR) was
    replaced by the structured outbox (MULTICA_OUTBOX_DIR). On API
    failure the review packet must be persisted as JSON so
    /outbox/replay can drain it later."""
    outbox = tmp_path / "outbox"
    monkeypatch.setattr(isolated_bridge, "OUTBOX_DIR", str(outbox))
    monkeypatch.setattr(isolated_bridge, "MULTICA_API_URL", "http://multica.local:8080")
    # 1 attempt so the test is fast
    monkeypatch.setattr(isolated_bridge, "OUTBOX_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(isolated_bridge, "OUTBOX_BACKOFF_SECONDS", 0.0)

    def fake_urlopen(req, timeout=10):
        raise urllib_error.URLError("connection refused")

    monkeypatch.setattr(isolated_bridge.urllib.request, "urlopen", fake_urlopen)

    _seed_submitted_task(isolated_client, "R-3")
    r = isolated_client.post(
        "/webhook/multica",
        json={"type": "agent.completed", "data": _completed_payload("R-3")},
    )
    # Webhook still 200 even when reverse channel failed.
    assert r.status_code == 200

    # The packet landed in the structured outbox for replay.
    files = list(outbox.glob("*R-3*.json"))
    assert files, f"outbox should have one entry for R-3, found: {list(outbox.iterdir())}"
    entry = json.loads(files[0].read_text(encoding="utf-8"))
    assert entry["kind"] == "review"
    assert entry["issue_id"] == "R-3"
    assert "multica-R-3" in entry["body"]
    assert "claude-frontend" in entry["body"]


def test_reverse_channel_does_not_run_when_status_not_done(client, bridge, monkeypatch):
    """If done() returns something other than completed/review_ready, do not post."""
    # Set up a fresh task and fail it instead of completing.
    r = client.post(
        "/webhook/multica",
        json={"type": "issue.created", "data": {"issue_id": "R-4", "title": "demo", "url": "x"}},
    )
    assert r.status_code == 200
    r = client.post(
        "/webhook/multica",
        json={"type": "agent.started", "data": {"issue_id": "R-4", "agent_id": "a"}},
    )
    assert r.status_code == 200

    captured = []
    monkeypatch.setattr(bridge, "MULTICA_API_URL", "http://multica.local:8080")
    monkeypatch.setattr(bridge.urllib.request, "urlopen", lambda *a, **kw: captured.append(1) or _FakeResponse(200))

    r = client.post(
        "/webhook/multica",
        json={"type": "agent.failed", "data": {
            "issue_id": "R-4", "agent_id": "a",
            "error_code": "build_broken", "message": "x",
        }},
    )
    assert r.status_code == 200
    # agent.failed does NOT trigger reverse channel -- only completed/review_ready do.
    assert captured == []

def test_boundary_violation_returns_structured_200(isolated_client, isolated_bridge, monkeypatch):
    """When an agent changes files outside its forbidden paths, the bridge
    refuses to record the handoff, returns a structured 200 with the
    violations, and does NOT trigger the reverse channel.
    """
    _seed_submitted_task(isolated_client, "BV-1")
    # Register claude-frontend with a strict forbidden_paths so the bridge
    # picks up the boundary on the next done() call. The bridge does not
    # auto-register agents, so we have to do it explicitly here.
    from mac.protocol.messages import AgentCapability, AgentCard
    isolated_bridge.registry.register(
        AgentCard(
            agent_id="claude-frontend",
            name="claude-frontend",
            capabilities=[AgentCapability(name="frontend")]
        ).model_copy(update={"forbidden_paths": ["secrets/*"]})
    )
    monkeypatch.setattr(isolated_bridge, "MULTICA_API_URL", "http://multica.local:8080")
    captured = []
    monkeypatch.setattr(
        isolated_bridge.urllib.request, "urlopen",
        lambda *a, **kw: captured.append(1) or _FakeResponse(200),
    )
    payload = {
        "issue_id": "BV-1",
        "agent_id": "claude-frontend",
        "changed_files": ["secrets/key.pem"],
        "verification": "pytest:pass",
        "risks": [],
    }
    r = isolated_client.post("/webhook/multica", json={"type": "agent.completed", "data": payload})
    # Webhook stays 200 so Multica gets the structured violations.
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "boundary_violation"
    assert body["task_id"] == "multica-BV-1"
    assert isinstance(body.get("violations"), list)
    assert any("forbidden:secrets/key.pem" in v for v in body["violations"])
    # Reverse channel must NOT fire for a refused handoff.
    assert captured == []
    # And the handoff was not persisted.
    assert isolated_bridge.registry.get_handoff_result("multica-BV-1") is None


def test_blocking_conflicts_segregated_in_review_packet(
    isolated_client, isolated_bridge, monkeypatch
):
    """Guarded overlap is recorded with severity=blocking and rendered
    under a separate "## Blocking Conflicts" header in the review packet.
    This test forces ``refuse_on_blocking=False`` so it exercises the
    informational-only path (refusal is covered separately in
    test_blocking_conflict_refuses_done_transition).
    """
    # Force informational mode: no refusal even when a blocking conflict fires.
    monkeypatch.setattr(isolated_bridge, "GUARDED_PATTERNS", ["secrets/*"])
    monkeypatch.setattr(isolated_bridge, "REFUSE_ON_BLOCKING", False)
    monkeypatch.setattr(isolated_bridge, "MULTICA_API_URL", "")
    _seed_submitted_task(isolated_client, "GD-1")
    _seed_submitted_task(isolated_client, "GD-2")
    # Complete GD-1 first (no reverse channel).
    r = isolated_client.post(
        "/webhook/multica",
        json={
            "type": "agent.completed",
            "data": {
                "issue_id": "GD-1",
                "agent_id": "claude-shared",
                "changed_files": ["secrets/key.pem"],
                "verification": "pytest:pass",
                "risks": [],
            },
        },
    )
    assert r.status_code == 200
    # Wire the reverse channel and complete GD-2 with the SAME guarded file.
    # Turn the guard back ON so GD-2 actually generates blocking conflicts.
    monkeypatch.setattr(isolated_bridge, "GUARDED_PATTERNS", ["secrets/*"])
    monkeypatch.setattr(isolated_bridge, "MULTICA_API_URL", "http://multica.local:8080")
    captured = []
    monkeypatch.setattr(
        isolated_bridge.urllib.request, "urlopen",
        lambda *a, **kw: (captured.append(a[0].data.decode("utf-8")) or _FakeResponse(201)),
    )
    r = isolated_client.post(
        "/webhook/multica",
        json={
            "type": "agent.completed",
            "data": {
                "issue_id": "GD-2",
                "agent_id": "claude-shared",
                "changed_files": ["secrets/key.pem"],
                "verification": "pytest:pass",
                "risks": [],
            },
        },
    )
    body = r.json()
    # With refuse_on_blocking=False, the task completes even with blocking conflicts.
    assert body["status"] == "completed"
    assert any(c["severity"] == "blocking" for c in body["conflicts"])
    assert captured
    packet = json.loads(captured[0])["body"]
    assert "## Blocking Conflicts" in packet
    assert "## Open Conflicts (non-blocking)" in packet
    assert "secrets/key.pem" in packet
    assert "BLOCKING" in packet


def test_blocking_conflict_refuses_done_transition(
    isolated_client, isolated_bridge, monkeypatch
):
    """When a guard fires AND refuse_on_blocking is on (i.e. the bridge
    has a non-empty GUARDED_PATTERNS), the bridge rolls the task back to
    running and returns a structured blocking_conflict result. The reverse
    channel must NOT fire (the task is not completed). The task remains
    running in the ledger so the agent can retry after addressing the
    overlap.
    """
    monkeypatch.setattr(isolated_bridge, "GUARDED_PATTERNS", ["secrets/*"])
    assert bool(isolated_bridge.GUARDED_PATTERNS) is True
    monkeypatch.setattr(isolated_bridge, "MULTICA_API_URL", "")
    _seed_submitted_task(isolated_client, "RF-1")
    _seed_submitted_task(isolated_client, "RF-2")
    # Complete RF-1 first (no refusal yet because there is no overlap).
    r = isolated_client.post(
        "/webhook/multica",
        json={
            "type": "agent.completed",
            "data": {
                "issue_id": "RF-1",
                "agent_id": "claude-shared",
                "changed_files": ["secrets/key.pem"],
                "verification": "pytest:pass",
                "risks": [],
            },
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "completed"
    # Now complete RF-2 with the SAME guarded file. With refuse_on_blocking
    # on (because GUARDED_PATTERNS is truthy), the bridge rolls the task
    # back to running and returns blocking_conflict.
    monkeypatch.setattr(isolated_bridge, "MULTICA_API_URL", "http://multica.local:8080")
    captured = []
    monkeypatch.setattr(
        isolated_bridge.urllib.request, "urlopen",
        lambda *a, **kw: (captured.append(1) or _FakeResponse(201)),
    )
    r = isolated_client.post(
        "/webhook/multica",
        json={
            "type": "agent.completed",
            "data": {
                "issue_id": "RF-2",
                "agent_id": "claude-shared",
                "changed_files": ["secrets/key.pem"],
                "verification": "pytest:pass",
                "risks": [],
            },
        },
    )
    body = r.json()
    assert body["status"] == "blocking_conflict"
    assert body["task_id"] == "multica-RF-2"
    assert any(c["severity"] == "blocking" for c in body["conflicts"])
    # Reverse channel must NOT fire for a refused handoff.
    assert captured == []
    # The task is rolled back to running, NOT completed.
    task = isolated_bridge.registry.ledger.get_task_transfer("multica-RF-2")
    assert task.status == "running"


def test_review_packet_includes_file_overlap_conflicts(isolated_client, isolated_bridge, monkeypatch):
    _seed_submitted_task(isolated_client, "OVR-1")
    _seed_submitted_task(isolated_client, "OVR-2")
    # Complete OVR-1 first (no reverse channel) so OVR-2 has something to overlap with.
    monkeypatch.setattr(isolated_bridge, "MULTICA_API_URL", "")
    payload_1 = {
        "issue_id": "OVR-1",
        "agent_id": "claude-shared",
        "changed_files": ["src/shared.py"],
        "verification": "pytest:pass",
        "risks": [],
    }
    r = isolated_client.post("/webhook/multica", json={"type": "agent.completed", "data": payload_1})
    assert r.status_code == 200

    # Now wire the reverse channel and complete OVR-2.
    monkeypatch.setattr(isolated_bridge, "MULTICA_API_URL", "http://multica.local:8080")
    captured = []

    def fake_urlopen(req, timeout=10):
        captured.append(req.data.decode("utf-8"))
        return _FakeResponse(201)

    monkeypatch.setattr(isolated_bridge.urllib.request, "urlopen", fake_urlopen)

    payload_2 = {
        "issue_id": "OVR-2",
        "agent_id": "claude-shared",
        "changed_files": ["src/shared.py"],
        "verification": "pytest:pass",
        "risks": [],
    }
    r = isolated_client.post("/webhook/multica", json={"type": "agent.completed", "data": payload_2})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"
    assert "conflicts" in body and len(body["conflicts"]) == 1
    assert captured, "reverse channel should have fired"
    packet = json.loads(captured[0])["body"]
    assert "## Open Conflicts" in packet
    assert "src/shared.py" in packet
    assert "multica-OVR-1" in packet
    assert "multica-OVR-2" in packet



# ---------------------------------------------------------------------------
# Round 5: audit-trail shipping and agent-card sync
# ---------------------------------------------------------------------------


def test_audit_trail_skipped_by_default(isolated_client, isolated_bridge, monkeypatch):
    """MULTICA_AUDIT_TRAIL defaults to false; no audit POST should happen
    even when MULTICA_API_URL is set."""
    monkeypatch.setattr(isolated_bridge, "AUDIT_TRAIL", False)
    monkeypatch.setattr(isolated_bridge, "MULTICA_API_URL", "http://multica.local:8080")
    captured = []
    monkeypatch.setattr(
        isolated_bridge.urllib.request,
        "urlopen",
        lambda *a, **kw: (captured.append(a[0].full_url) or _FakeResponse(201)),
    )
    _seed_submitted_task(isolated_client, "AUDIT-1")
    assert captured == [], f"unexpected audit POST(s): {captured}"


def test_audit_trail_posts_when_enabled(isolated_client, isolated_bridge, monkeypatch):
    """With MULTICA_AUDIT_TRAIL=true and a real API URL, every handled
    event should fire a POST to /api/issues/<id>/comments with the
    audit-trail body prefix."""
    monkeypatch.setattr(isolated_bridge, "AUDIT_TRAIL", True)
    monkeypatch.setattr(isolated_bridge, "MULTICA_API_URL", "http://multica.local:8080")
    captured = []
    monkeypatch.setattr(
        isolated_bridge.urllib.request,
        "urlopen",
        lambda *a, **kw: (
            captured.append((a[0].full_url, json.loads(a[0].data.decode("utf-8"))))
            or _FakeResponse(201)
        ),
    )
    # issue.created should fire 1 audit POST (no Multica response here,
    # we just assert that the dispatch reached the audit hook).
    r = isolated_client.post(
        "/webhook/multica",
        json={"type": "issue.created", "data": {"issue_id": "AUDIT-2", "title": "demo"}},
    )
    assert r.status_code == 200
    assert len(captured) == 1
    url, body = captured[0]
    assert url.endswith("/api/issues/AUDIT-2/comments")
    assert "[MAC audit]" in body["body"]
    assert "`issue.created`" in body["body"]
    # handler-result summary should mention the task_id we just submitted
    assert "multica-AUDIT-2" in body["body"]


def test_audit_trail_swallows_network_errors(isolated_client, isolated_bridge, monkeypatch):
    """A network failure during the audit POST must not break the webhook
    (response stays 200, audit failure is silent)."""
    monkeypatch.setattr(isolated_bridge, "AUDIT_TRAIL", True)
    monkeypatch.setattr(isolated_bridge, "MULTICA_API_URL", "http://multica.local:8080")

    def boom(*a, **kw):
        raise urllib_error.URLError("connection refused")

    monkeypatch.setattr(isolated_bridge.urllib.request, "urlopen", boom)
    r = isolated_client.post(
        "/webhook/multica",
        json={"type": "issue.created", "data": {"issue_id": "AUDIT-3"}},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "submitted"


def test_agent_card_synced_from_multica_payload(isolated_client, isolated_bridge, monkeypatch):
    """When agent.started carries an agent_card payload, the bridge must
    register it via Registry.register so MAC path-boundary enforcement
    turns on automatically. The handler response also surfaces
    card_synced=True."""
    monkeypatch.setattr(isolated_bridge, "MULTICA_API_URL", "")
    # Seed an issue so agent.started has a target task.
    _seed_submitted_task(isolated_client, "CARD-1")
    r = isolated_client.post(
        "/webhook/multica",
        json={
            "type": "agent.started",
            "data": {
                "issue_id": "CARD-1",
                "agent_id": "claude-frontend",
                "agent_card": {
                    "name": "Claude Frontend",
                    "version": "2.3",
                    "allowed_paths": ["src/frontend/**"],
                    "forbidden_paths": ["secrets/**", "db/migrations/**"],
                    "metadata": {"owner": "team-frontend"},
                },
            },
        },
    )
    assert r.status_code == 200
    payload = r.json()
    assert payload["card_synced"] is True
    # The agent card must be readable from the ledger so a later
    # boundary-enforced done() can consult it.
    card = isolated_bridge.registry.get_agent("claude-frontend")
    assert card is not None
    assert card.agent_id == "claude-frontend"
    assert card.allowed_paths == ["src/frontend/**"]
    assert card.forbidden_paths == ["secrets/**", "db/migrations/**"]


def test_agent_card_absent_keeps_old_behavior(isolated_client, isolated_bridge, monkeypatch):
    """If agent.started has no agent_card payload (older Multica), the
    handler must still succeed and card_synced must be False. Nothing
    is registered against the ledger."""
    monkeypatch.setattr(isolated_bridge, "MULTICA_API_URL", "")
    _seed_submitted_task(isolated_client, "NOCARD-1")
    r = isolated_client.post(
        "/webhook/multica",
        json={
            "type": "agent.started",
            "data": {"issue_id": "NOCARD-1", "agent_id": "claude-frontend"},
        },
    )
    assert r.status_code == 200
    payload = r.json()
    assert payload["card_synced"] is False
    # No agent card in the ledger (the registry returns None for agents
    # it has never seen).
    assert isolated_bridge.registry.get_agent("claude-frontend") is None



# ---------------------------------------------------------------------------
# Round 7: agent heartbeat (event + HTTP endpoint) and GET /agents
# ---------------------------------------------------------------------------


def test_heartbeat_event_refreshes_card(isolated_client, isolated_bridge):
    """agent.heartbeat must call Registry.heartbeat_agent and surface
    the refreshed load / status / last_heartbeat in the response."""
    isolated_bridge.registry.register(
        AgentCard(agent_id="hb-1", name="HB-1"),
    )
    before = isolated_bridge.registry.get_agent("hb-1")
    assert before.load == 0  # AgentCard default
    r = isolated_client.post(
        "/webhook/multica",
        json={
            "type": "agent.heartbeat",
            "data": {"agent_id": "hb-1", "load": 73, "status": "busy"},
        },
    )
    assert r.status_code == 200
    payload = r.json()
    assert payload["status"] == "heartbeated"
    assert payload["load"] == 73
    assert payload["card_status"] == "busy"
    assert payload["last_heartbeat"] >= before.last_heartbeat
    after = isolated_bridge.registry.get_agent("hb-1")
    assert after.load == 73
    assert after.status == "busy"


def test_heartbeat_event_unknown_agent_returns_structured_error(
    isolated_client, isolated_bridge,
):
    """Heartbeat for an agent that has never been registered must NOT
    auto-create a card; it must surface a structured error so the
    caller can diagnose the misconfiguration."""
    r = isolated_client.post(
        "/webhook/multica",
        json={
            "type": "agent.heartbeat",
            "data": {"agent_id": "ghost", "load": 50},
        },
    )
    assert r.status_code == 200  # webhook stays 200; error is in body
    payload = r.json()
    assert payload["status"] == "error"
    assert payload["error"] == "unknown_agent"
    assert payload["agent_id"] == "ghost"
    # The ledger must not have auto-registered a zombie card.
    assert isolated_bridge.registry.get_agent("ghost") is None


def test_heartbeat_event_missing_agent_id(isolated_client):
    """If agent_id is absent from the heartbeat payload we must not
    blow up; return a structured error."""
    r = isolated_client.post(
        "/webhook/multica",
        json={"type": "agent.heartbeat", "data": {}},
    )
    assert r.status_code == 200
    assert r.json() == {"status": "error", "error": "missing_agent_id"}


def test_http_heartbeat_endpoint_refreshes_card(isolated_client, isolated_bridge):
    """POST /agents/<id>/heartbeat must work for already-registered
    agents and surface the refreshed fields."""
    isolated_bridge.registry.register(AgentCard(agent_id="hb-http", name="HB-HTTP"))
    r = isolated_client.post(
        "/agents/hb-http/heartbeat",
        json={"load": 42, "status": "online"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["agent_id"] == "hb-http"
    assert body["load"] == 42
    assert body["status"] == "online"
    assert body["last_heartbeat"] > 0


def test_http_heartbeat_endpoint_unknown_returns_404(isolated_client):
    """Unknown agent must yield a structured 404 (FastAPI HTTPException)."""
    r = isolated_client.post("/agents/no-such-agent/heartbeat", json={"load": 10})
    assert r.status_code == 404
    # FastAPI's HTTPException wraps detail in {"detail": ...}
    body = r.json()
    assert body["detail"]["error"] == "unknown_agent"
    assert body["detail"]["agent_id"] == "no-such-agent"


def test_http_heartbeat_endpoint_empty_body_uses_defaults(isolated_client, isolated_bridge):
    """POST with no body or empty body should default status='online'
    and leave load untouched (None in heartbeat_agent)."""
    isolated_bridge.registry.register(AgentCard(agent_id="hb-def", name="HB-DEF"))
    before = isolated_bridge.registry.get_agent("hb-def")
    r = isolated_client.post("/agents/hb-def/heartbeat")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "online"
    # load was not provided, so heartbeat_agent keeps the existing value
    after = isolated_bridge.registry.get_agent("hb-def")
    assert after.load == before.load


def test_list_agents_endpoint_returns_registered_cards(isolated_client, isolated_bridge):
    """GET /agents must list all registered cards with their heartbeat
    state. Empty when nothing is registered."""
    r = isolated_client.get("/agents")
    assert r.status_code == 200
    assert r.json() == {"count": 0, "agents": []}
    isolated_bridge.registry.register(AgentCard(agent_id="a1", name="A1"))
    isolated_bridge.registry.register(AgentCard(agent_id="a2", name="A2", load=33))
    r = isolated_client.get("/agents")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    ids = {a["agent_id"] for a in body["agents"]}
    assert ids == {"a1", "a2"}
    a2 = next(a for a in body["agents"] if a["agent_id"] == "a2")
    assert a2["load"] == 33



# ---------------------------------------------------------------------------
# Round 9: persistent outbox + retry + /outbox endpoints
# ---------------------------------------------------------------------------


def test_review_post_writes_outbox_on_network_failure(
    isolated_client, isolated_bridge, monkeypatch, tmp_path,
):
    """When the Multica API is unreachable after all retries, the
    review packet must be written to the outbox as a structured JSON
    file so a cron / replay endpoint can drain it later."""
    outbox = tmp_path / "outbox"
    monkeypatch.setattr(isolated_bridge, "OUTBOX_DIR", str(outbox))
    monkeypatch.setattr(isolated_bridge, "MULTICA_API_URL", "http://multica.local:8080")
    # 1 attempt, no backoff so the test is fast
    monkeypatch.setattr(isolated_bridge, "OUTBOX_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(isolated_bridge, "OUTBOX_BACKOFF_SECONDS", 0.0)

    def boom(*a, **kw):
        raise urllib_error.URLError("connection refused")

    monkeypatch.setattr(isolated_bridge.urllib.request, "urlopen", boom)
    _seed_submitted_task(isolated_client, "OUTBOX-1")

    r = isolated_client.post(
        "/webhook/multica",
        json={
            "type": "agent.completed",
            "data": {
                "issue_id": "OUTBOX-1",
                "agent_id": "claude-frontend",
                "changed_files": ["src/foo.py"],
                "verification": "pytest:pass",
            },
        },
    )
    # Webhook stays 200 so Multica does not retry the whole delivery.
    assert r.status_code == 200
    files = sorted(outbox.glob("*.json"))
    assert len(files) == 1
    entry = json.loads(files[0].read_text(encoding="utf-8"))
    assert entry["kind"] == "review"
    assert entry["issue_id"] == "OUTBOX-1"
    assert entry["path"] == "/api/issues/OUTBOX-1/comments"
    assert entry["attempts"] == 1
    assert "Review Task" in entry["body"]


def test_post_json_to_multica_retries_with_backoff(
    isolated_bridge, monkeypatch,
):
    """When the API returns a transient URLError the helper must retry
    up to max_attempts times with exponential backoff. We mock
    time.sleep so the test runs instantly."""
    calls = {"n": 0}
    sleeps = []

    def fake_urlopen(*a, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib_error.URLError("transient")
        return _FakeResponse(200)

    isolated_bridge.MULTICA_API_URL = "http://multica.local:8080"
    monkeypatch.setattr(isolated_bridge.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(isolated_bridge.urllib.request, "urlopen", fake_urlopen)
    ok, status, exc = isolated_bridge._post_json_to_multica(
        "/api/issues/X/comments",
        "body",
        timeout=2,
        max_attempts=3,
        backoff=0.5,
    )
    assert ok is True
    assert status == 200
    assert exc is None
    assert calls["n"] == 3  # 2 failures + 1 success
    # Exponential: 0.5, 1.0  (third attempt is the success, no sleep)
    assert sleeps == [0.5, 1.0]


def test_post_json_to_multica_gives_up_after_max_attempts(
    isolated_bridge, monkeypatch,
):
    """After max_attempts the helper must return (False, None, exc) so
    the caller can decide what to do (write to outbox, etc.)."""
    calls = {"n": 0}

    def always_fails(*a, **kw):
        calls["n"] += 1
        raise urllib_error.URLError("nope")

    isolated_bridge.MULTICA_API_URL = "http://multica.local:8080"
    monkeypatch.setattr(isolated_bridge.time, "sleep", lambda s: None)
    monkeypatch.setattr(isolated_bridge.urllib.request, "urlopen", always_fails)
    ok, status, exc = isolated_bridge._post_json_to_multica(
        "/api/issues/Y/comments", "body", timeout=1, max_attempts=3, backoff=0.0,
    )
    assert ok is False
    assert status is None
    assert isinstance(exc, urllib_error.URLError)
    assert calls["n"] == 3


def test_list_outbox_returns_empty_when_dir_missing(
    isolated_client, isolated_bridge, tmp_path, monkeypatch,
):
    """GET /outbox must return count=0 cleanly when the outbox
    directory does not exist (e.g. first run, before any failures)."""
    monkeypatch.setattr(isolated_bridge, "OUTBOX_DIR", str(tmp_path / "no-such-dir"))
    r = isolated_client.get("/outbox")
    assert r.status_code == 200
    assert r.json() == {"count": 0, "entries": []}


def test_replay_outbox_drains_entries_on_success(
    isolated_client, isolated_bridge, monkeypatch, tmp_path,
):
    """POST /outbox/replay must attempt delivery of every outbox entry
    and delete files that succeed. Bodies are NOT returned in the
    /outbox list (only metadata) -- that is the replay endpoint's job.
    """
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    # Plant two entries
    for i, (issue, body) in enumerate([
        ("R-1", "review packet 1"),
        ("R-2", "review packet 2"),
    ]):
        entry = {
            "kind": "review",
            "issue_id": issue,
            "path": f"/api/issues/{issue}/comments",
            "body": body,
            "first_attempt": 1_000_000 + i,
            "last_attempt": 1_000_000 + i,
            "attempts": 1,
        }
        (outbox / f"{1_000_000 + i}-{issue}-review.json").write_text(
            json.dumps(entry), encoding="utf-8",
        )

    monkeypatch.setattr(isolated_bridge, "OUTBOX_DIR", str(outbox))
    monkeypatch.setattr(isolated_bridge, "MULTICA_API_URL", "http://multica.local:8080")

    delivered = []

    def fake_urlopen(*a, **kw):
        delivered.append(a[0].full_url)
        return _FakeResponse(201)

    monkeypatch.setattr(isolated_bridge.urllib.request, "urlopen", fake_urlopen)

    r = isolated_client.post("/outbox/replay")
    assert r.status_code == 200
    summary = r.json()
    assert summary["replayed"] == 2
    assert summary["succeeded"] == 2
    assert summary["failed"] == 0
    # Files must be deleted on success
    assert list(outbox.glob("*.json")) == []
    # Both endpoints must have been called
    assert len(delivered) == 2


def test_replay_outbox_keeps_failures_in_place(
    isolated_client, isolated_bridge, monkeypatch, tmp_path,
):
    """If a replay attempt still fails, the outbox file must remain
    so the next replay can try again."""
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    entry = {
        "kind": "review",
        "issue_id": "R-still-bad",
        "path": "/api/issues/R-still-bad/comments",
        "body": "still bad",
        "first_attempt": 1,
        "last_attempt": 1,
        "attempts": 1,
    }
    path = outbox / "1-R-still-bad-review.json"
    path.write_text(json.dumps(entry), encoding="utf-8")

    monkeypatch.setattr(isolated_bridge, "OUTBOX_DIR", str(outbox))
    monkeypatch.setattr(isolated_bridge, "MULTICA_API_URL", "http://multica.local:8080")

    def boom(*a, **kw):
        raise urllib_error.URLError("still failing")

    monkeypatch.setattr(isolated_bridge.time, "sleep", lambda s: None)
    monkeypatch.setattr(isolated_bridge.urllib.request, "urlopen", boom)

    r = isolated_client.post("/outbox/replay")
    summary = r.json()
    assert summary["replayed"] == 1
    assert summary["succeeded"] == 0
    assert summary["failed"] == 1
    # File must remain
    assert path.exists()


def test_replay_outbox_skipped_when_multica_url_empty(
    isolated_client, isolated_bridge, monkeypatch, tmp_path,
):
    """In dev mode (MULTICA_API_URL empty) replay must be a safe
    no-op rather than raising."""
    monkeypatch.setattr(isolated_bridge, "MULTICA_API_URL", "")
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    monkeypatch.setattr(isolated_bridge, "OUTBOX_DIR", str(outbox))
    r = isolated_client.post("/outbox/replay")
    assert r.status_code == 200
    summary = r.json()
    assert summary["skipped"] is True
    assert summary["replayed"] == 0
def test_post_json_to_multica_sets_idempotency_key_header(
    isolated_bridge, monkeypatch,
):
    """When called with ``idempotency_key=...``, the helper must attach
    an ``Idempotency-Key`` header on every retry attempt so Multica
    can dedupe a logical action across retries and outbox replays.
    """
    captured = []

    def fake_urlopen(*a, **kw):
        captured.append(_idem_of(a[0]))
        return _FakeResponse(201)

    isolated_bridge.MULTICA_API_URL = "http://multica.local:8080"
    monkeypatch.setattr(isolated_bridge.time, "sleep", lambda s: None)
    monkeypatch.setattr(isolated_bridge.urllib.request, "urlopen", fake_urlopen)
    ok, status, _ = isolated_bridge._post_json_to_multica(
        "/api/issues/IDEMP-1/comments",
        "body",
        timeout=2,
        max_attempts=2,
        backoff=0.0,
        idempotency_key="review:IDEMP-1",
    )
    assert ok is True
    assert status == 201
    assert captured == ["review:IDEMP-1"]


def test_post_json_to_multica_omits_header_when_no_key(
    isolated_bridge, monkeypatch,
):
    """Without an ``idempotency_key`` the request must NOT carry the
    ``Idempotency-Key`` header -- otherwise unrelated callers would
    accidentally collide on Multica's dedup table.
    """
    captured = []

    def fake_urlopen(*a, **kw):
        captured.append(_idem_of(a[0]))
        return _FakeResponse(200)

    isolated_bridge.MULTICA_API_URL = "http://multica.local:8080"
    monkeypatch.setattr(isolated_bridge.urllib.request, "urlopen", fake_urlopen)
    isolated_bridge._post_json_to_multica(
        "/api/issues/X/comments", "body", timeout=1,
    )
    assert captured == [None]


def test_review_post_carries_review_idempotency_key(
    isolated_client, isolated_bridge, monkeypatch,
):
    """``_on_agent_completed`` -> ``_post_review_to_multica`` must set
    ``Idempotency-Key: review:<issue_id>`` so Multica can dedupe a
    successful double-dispatch (e.g. webhook retry vs. RPC).
    """
    monkeypatch.setattr(isolated_bridge, "MULTICA_API_URL", "http://multica.local:8080")
    monkeypatch.setattr(isolated_bridge, "OUTBOX_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(isolated_bridge, "OUTBOX_BACKOFF_SECONDS", 0.0)

    captured = []

    def fake_urlopen(*a, **kw):
        captured.append(_idem_of(a[0]))
        return _FakeResponse(201)

    monkeypatch.setattr(isolated_bridge.urllib.request, "urlopen", fake_urlopen)
    _seed_submitted_task(isolated_client, "IDEMP-R")
    r = isolated_client.post(
        "/webhook/multica",
        json={
            "type": "agent.completed",
            "data": {
                "issue_id": "IDEMP-R",
                "agent_id": "claude-frontend",
                "changed_files": ["src/x.py"],
                "verification": "pytest:pass",
            },
        },
    )
    assert r.status_code == 200
    review_keys = [k for k in captured if k and k.startswith("review:")]
    assert review_keys == ["review:IDEMP-R"], captured


def test_audit_post_carries_audit_idempotency_key(
    isolated_client, isolated_bridge, monkeypatch,
):
    """The audit-trail hook must set
    ``Idempotency-Key: audit:<issue_id>:<event_type>`` so an audit POST
    fired twice for the same logical event is collapsed server-side.
    """
    monkeypatch.setattr(isolated_bridge, "AUDIT_TRAIL", True)
    monkeypatch.setattr(isolated_bridge, "MULTICA_API_URL", "http://multica.local:8080")

    captured = []

    def fake_urlopen(*a, **kw):
        captured.append(_idem_of(a[0]))
        return _FakeResponse(201)

    monkeypatch.setattr(isolated_bridge.urllib.request, "urlopen", fake_urlopen)
    r = isolated_client.post(
        "/webhook/multica",
        json={"type": "issue.created", "data": {"issue_id": "IDEMP-A", "title": "x"}},
    )
    assert r.status_code == 200
    assert captured == ["audit:IDEMP-A:issue.created"]


def test_outbox_entry_persists_idempotency_key(
    isolated_client, isolated_bridge, monkeypatch, tmp_path,
):
    """When a review POST fails after all retries the outbox JSON entry
    must persist the same ``idempotency_key`` the original POST carried
    so a drain replays with identical dedup semantics.
    """
    outbox = tmp_path / "outbox"
    monkeypatch.setattr(isolated_bridge, "OUTBOX_DIR", str(outbox))
    monkeypatch.setattr(isolated_bridge, "MULTICA_API_URL", "http://multica.local:8080")
    monkeypatch.setattr(isolated_bridge, "OUTBOX_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(isolated_bridge, "OUTBOX_BACKOFF_SECONDS", 0.0)

    def boom(*a, **kw):
        raise urllib_error.URLError("nope")

    monkeypatch.setattr(isolated_bridge.urllib.request, "urlopen", boom)
    _seed_submitted_task(isolated_client, "IDEMP-OUT")
    isolated_client.post(
        "/webhook/multica",
        json={
            "type": "agent.completed",
            "data": {
                "issue_id": "IDEMP-OUT",
                "agent_id": "claude-frontend",
                "changed_files": ["src/x.py"],
                "verification": "pytest:pass",
            },
        },
    )

    files = list(outbox.glob("*.json"))
    assert len(files) == 1
    entry = json.loads(files[0].read_text(encoding="utf-8"))
    assert entry["idempotency_key"] == "review:IDEMP-OUT"


def test_outbox_replay_resends_persisted_idempotency_key(
    isolated_client, isolated_bridge, monkeypatch, tmp_path,
):
    """A drain must re-send the persisted ``idempotency_key`` so Multica
    sees the same key on every delivery attempt for the same logical
    action (in-process retry + outbox replay + manual drain).
    """
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    entry = {
        "kind": "review",
        "issue_id": "IDEMP-DRAIN",
        "path": "/api/issues/IDEMP-DRAIN/comments",
        "body": "review packet",
        "idempotency_key": "review:IDEMP-DRAIN",
        "first_attempt": 1,
        "last_attempt": 1,
        "attempts": 1,
    }
    (outbox / "1-IDEMP-DRAIN-review.json").write_text(
        json.dumps(entry), encoding="utf-8",
    )

    monkeypatch.setattr(isolated_bridge, "OUTBOX_DIR", str(outbox))
    monkeypatch.setattr(isolated_bridge, "MULTICA_API_URL", "http://multica.local:8080")

    captured = []

    def fake_urlopen(*a, **kw):
        captured.append(_idem_of(a[0]))
        return _FakeResponse(201)

    monkeypatch.setattr(isolated_bridge.urllib.request, "urlopen", fake_urlopen)
    r = isolated_client.post("/outbox/replay")
    assert r.status_code == 200
    assert r.json()["succeeded"] == 1
    assert captured == ["review:IDEMP-DRAIN"]


def test_outbox_replay_without_idempotency_key_omits_header(
    isolated_client, isolated_bridge, monkeypatch, tmp_path,
):
    """Older outbox entries written before Round 10 don't carry an
    ``idempotency_key`` field -- a drain must still succeed and simply
    omit the header. This keeps forward-compat with on-disk entries
    produced by previous deployments.
    """
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    entry = {
        "kind": "review",
        "issue_id": "LEGACY",
        "path": "/api/issues/LEGACY/comments",
        "body": "old review packet",
        # NB: no "idempotency_key" key here on purpose
        "first_attempt": 1,
        "last_attempt": 1,
        "attempts": 1,
    }
    (outbox / "1-LEGACY-review.json").write_text(
        json.dumps(entry), encoding="utf-8",
    )

    monkeypatch.setattr(isolated_bridge, "OUTBOX_DIR", str(outbox))
    monkeypatch.setattr(isolated_bridge, "MULTICA_API_URL", "http://multica.local:8080")

    captured = []

    def fake_urlopen(*a, **kw):
        captured.append(_idem_of(a[0]))
        return _FakeResponse(201)

    monkeypatch.setattr(isolated_bridge.urllib.request, "urlopen", fake_urlopen)
    r = isolated_client.post("/outbox/replay")
    assert r.status_code == 200
    assert r.json()["succeeded"] == 1
    assert captured == [None]
