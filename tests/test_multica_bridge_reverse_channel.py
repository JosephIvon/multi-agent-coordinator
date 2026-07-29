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
import io
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
def client(bridge):
    return TestClient(bridge.app)


class _FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


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


def test_reverse_channel_falls_back_to_disk_on_api_failure(client, bridge, monkeypatch, tmp_path):
    _seed_submitted_task(client, "R-3")
    fallback = tmp_path / "review-fallback"
    monkeypatch.setattr(bridge, "MULTICA_API_URL", "http://multica.local:8080")
    monkeypatch.setattr(bridge, "REVIEW_FALLBACK_DIR", str(fallback))

    def fake_urlopen(req, timeout=10):
        raise urllib_error.URLError("connection refused")

    monkeypatch.setattr(bridge.urllib.request, "urlopen", fake_urlopen)

    r = client.post(
        "/webhook/multica",
        json={"type": "agent.completed", "data": _completed_payload("R-3")},
    )
    # Webhook still 200 even when reverse channel failed.
    assert r.status_code == 200

    # The packet landed on disk for replay.
    fallback_file = fallback / "R-3.md"
    assert fallback_file.exists()
    content = fallback_file.read_text(encoding="utf-8")
    assert "multica-R-3" in content
    assert "claude-frontend" in content


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

def test_review_packet_includes_file_overlap_conflicts(client, bridge, monkeypatch):
    _seed_submitted_task(client, "OVR-1")
    _seed_submitted_task(client, "OVR-2")
    # Complete OVR-1 first (no reverse channel) so OVR-2 has something to overlap with.
    monkeypatch.setattr(bridge, "MULTICA_API_URL", "")
    payload_1 = {
        "issue_id": "OVR-1",
        "agent_id": "claude-shared",
        "changed_files": ["src/shared.py"],
        "verification": "pytest:pass",
        "risks": [],
    }
    r = client.post("/webhook/multica", json={"type": "agent.completed", "data": payload_1})
    assert r.status_code == 200

    # Now wire the reverse channel and complete OVR-2.
    monkeypatch.setattr(bridge, "MULTICA_API_URL", "http://multica.local:8080")
    captured = []

    def fake_urlopen(req, timeout=10):
        captured.append(req.data.decode("utf-8"))
        return _FakeResponse(201)

    monkeypatch.setattr(bridge.urllib.request, "urlopen", fake_urlopen)

    payload_2 = {
        "issue_id": "OVR-2",
        "agent_id": "claude-shared",
        "changed_files": ["src/shared.py"],
        "verification": "pytest:pass",
        "risks": [],
    }
    r = client.post("/webhook/multica", json={"type": "agent.completed", "data": payload_2})
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
