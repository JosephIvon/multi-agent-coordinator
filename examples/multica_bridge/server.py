"""Multica -> MAC bridge (PoC).

Receives Multica webhooks and mirrors them into the local MAC ledger, so every
agent handoff that happens inside Multica leaves a structured record MAC can
query, audit, and replay.

Run as a webhook receiver:

    pip install "mac-agent[http]"
    PYTHONPATH=src python examples/multica_bridge/server.py            # :8765
    PYTHONPATH=src python examples/multica_bridge/server.py --demo     # self-test

Configure Multica to POST to ``http://your-host:8765/webhook/multica``. Set
``MULTICA_WEBHOOK_SECRET`` to enable HMAC-SHA256 signature verification; the
header is ``X-Multica-Signature``.

This is a 200-line PoC. Things deliberately deferred:

    - No retry / dead-letter queue (Multica at-least-once delivery is enough).
    - No batch events -- Multica sends one POST per event.

Reverse channel (MAC -> Multica):
    After a successful `agent.completed` (or `review_ready`) handoff, the
    bridge POSTs a markdown review packet back to Multica as an issue
    comment. Configured via env vars:

        MULTICA_API_URL       e.g. http://10.47.102.70:8081  (no /api suffix)
        MULTICA_API_TOKEN     optional Bearer token for the Multica API
        REVIEW_FALLBACK_DIR  where to write the packet if the POST fails
                              (default ./.agent-context/review-fallback)

    When MULTICA_API_URL is empty the reverse channel is a no-op (dev
    mode). On any 4xx/5xx or network failure the packet is written to
    REVIEW_FALLBACK_DIR and a warning is logged; the webhook response
    still returns 200 so Multica does not retry.

Audit-trail shipping (also MAC -> Multica, but distinct from the review
packet):
    Set MULTICA_AUDIT_TRAIL=true to enable. After every handled webhook
    the bridge POSTs a short one-line audit comment ("[MAC audit]
    `<event_type>` @ <ts>") back to the same Multica issue. Failures
    are logged at WARNING but do NOT write a fallback file -- audit
    loss is non-critical because the MAC ledger already holds the
    authoritative record.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import hmac
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from mac.protocol.errors import StateConflictError
from mac.protocol.messages import (
    AgentCard,
    HandoffResult,
    TaskPayload,
    TaskTransfer,
    VerificationEntry,
)
from mac.registry import Registry
from mac.storage.sqlite import SQLiteTaskLedger

DB_PATH = os.environ.get("MAC_DB_PATH", "mac.db")
WEBHOOK_SECRET = os.environ.get("MULTICA_WEBHOOK_SECRET", "")
LISTEN_HOST = os.environ.get("BRIDGE_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("BRIDGE_PORT", "8765"))
MULTICA_API_URL = os.environ.get("MULTICA_API_URL", "").rstrip("/")
MULTICA_API_TOKEN = os.environ.get("MULTICA_API_TOKEN", "")
REVIEW_FALLBACK_DIR = os.environ.get("REVIEW_FALLBACK_DIR", ".agent-context/review-fallback")
GUARDED_PATTERNS = [
    p.strip()
    for p in os.environ.get("MULTICA_GUARDED_PATHS", "").split(",")
    if p.strip()
]
REFUSE_ON_BLOCKING = os.environ.get("MULTICA_REFUSE_ON_BLOCKING", "true").lower() not in ("0", "false", "no", "")
AUDIT_TRAIL = os.environ.get("MULTICA_AUDIT_TRAIL", "false").lower() not in ("0", "false", "no", "")

logger = logging.getLogger("multica_bridge")

registry = Registry(SQLiteTaskLedger(DB_PATH))
app = FastAPI(title="MAC-Multica Bridge", version="0.1.0")


# ----- helpers ----------------------------------------------------------------


def _task_id(issue_id: str) -> str:
    return "multica-" + issue_id


def _verify(body: bytes, signature: str) -> bool:
    if not WEBHOOK_SECRET:
        return True  # dev mode: skip verification
    expected = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def _format_review_packet(
    issue_id: str,
    task_id: str,
    handoff: HandoffResult,
    quality: dict[str, Any] | None = None,
    conflicts: list[dict[str, Any]] | None = None,
) -> str:
    """Render a MAC HandoffResult as a markdown review packet.

    Mirrors the layout of `mac-agent review-packet` so Multica agents see
    the same shape they would see if they ran the CLI directly. Kept
    in sync with `mac.cli._render_review_packet` (when that helper is
    extracted); for now it is a small focused re-implementation.
    """
    lines = [f"# Review Task: {task_id}", ""]
    lines.append("## Task")
    lines.append(f"- Status: completed")
    lines.append(f"- Capability: multica_issue")
    lines.append(f"- Multica issue: {issue_id}")
    lines.append("")
    lines.append("## HandoffResult")
    lines.append(f"- Agent: {handoff.agent_id}")
    boundary = handoff.boundary_review or "not_required"
    lines.append(f"- Boundary review: {boundary}")
    files = handoff.changed_files or []
    lines.append(f"- Changed files: {', '.join(files) if files else 'None'}")
    docs = handoff.docs_touched or []
    lines.append(f"- Docs touched: {', '.join(docs) if docs else 'None'}")
    risks = handoff.risks or []
    lines.append(f"- Risks: {chr(10).join(risks) if risks else 'None'}")
    lines.append("")
    lines.append("## Verification")
    if handoff.verification:
        for entry in handoff.verification:
            lines.append(f"- `{entry.command}`: {entry.result}")
    else:
        lines.append("- (none recorded)")
    lines.append("")
    lines.append("## Quality Evidence")
    if quality:
        cmd_q = quality.get("command", "?")
        st_q = quality.get("status", "?")
        lines.append(f"- `{cmd_q}`: {st_q}")
    else:
        lines.append("- (none recorded)")
    lines.append("")
    blocking = [c for c in (conflicts or []) if c.get("severity") == "blocking"]
    other = [c for c in (conflicts or []) if c.get("severity") != "blocking"]
    lines.append("## Blocking Conflicts")
    if blocking:
        for _c in blocking:
            _files = ", ".join(_c.get("involved_files") or [])
            _agents = ", ".join(sorted(_c.get("involved_agents") or []))
            _cid = _c.get("conflict_id", "?")
            _desc = _c.get("description", "")
            lines.append("- **" + _cid + "**: " + _desc + " | files: " + _files + " | agents: " + _agents)
    else:
        lines.append("- None")
    lines.append("")
    lines.append("## Open Conflicts (non-blocking)")
    if other:
        for _c in other:
            _files = ", ".join(_c.get("involved_files") or [])
            _agents = ", ".join(sorted(_c.get("involved_agents") or []))
            _cid = _c.get("conflict_id", "?")
            _src = _c.get("source", "?")
            _desc = _c.get("description", "")
            lines.append("- " + _cid + " (" + _src + "): " + _desc + " | files: " + _files + " | agents: " + _agents)
    else:
        lines.append("- None")
    return chr(10).join(lines) + chr(10)


def _post_json_to_multica(path: str, body: str, timeout: int = 10) -> tuple[bool, int | None, BaseException | None]:
    """POST a JSON envelope to Multica and report the outcome.

    Centralises the URL build, Bearer-token injection, and timeout that
    both the review-packet reverse channel and the audit-trail
    ship-back need. When MULTICA_API_URL is empty the call is a no-op
    that returns ``(True, None, None)`` so dev environments do not
    regress.

    Returns ``(ok, status_code, exception)`` where ``ok`` is True iff
    Multica returned 2xx. ``status_code`` is the HTTP status on a
    successful response, ``None`` on network failure. ``exception``
    carries the underlying URLError/HTTPError/TimeoutError/OSError on
    failure, ``None`` otherwise.
    """
    if not MULTICA_API_URL:
        return True, None, None
    url = f"{MULTICA_API_URL}{path}"
    payload = json.dumps({"body": body}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {MULTICA_API_TOKEN}"} if MULTICA_API_TOKEN else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (200 <= resp.status < 300), resp.status, None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        return False, None, exc


def _post_review_to_multica(issue_id: str, body: str) -> bool:
    """POST the review packet markdown back to Multica as an issue comment.

    No-op (and returns True) when MULTICA_API_URL is empty, so dev
    environments without a Multica server keep working. Returns True if
    Multica accepted the comment (2xx), False otherwise. On any network
    failure the body is also written to REVIEW_FALLBACK_DIR so a
    human or cron can replay it later.
    """
    if not MULTICA_API_URL:
        logger.info("reverse channel skipped: MULTICA_API_URL not set")
        return True
    ok, status, exc = _post_json_to_multica(
        f"/api/issues/{issue_id}/comments", body, timeout=10,
    )
    if ok:
        logger.info("reverse channel: posted review packet for %s (status=%d)", issue_id, status)
        return True
    if exc is not None:
        logger.warning("reverse channel: failed to post %s (%s); writing fallback", issue_id, exc)
    else:
        logger.warning("reverse channel: non-2xx %d for %s", status, issue_id)
    _write_review_fallback(issue_id, body)
    return False


def _write_review_fallback(issue_id: str, body: str) -> None:
    """Persist the review packet to disk so it is not lost on API failure."""
    try:
        os.makedirs(REVIEW_FALLBACK_DIR, exist_ok=True)
        # Stable filename so retries are idempotent at the FS level.
        path = os.path.join(REVIEW_FALLBACK_DIR, f"{issue_id}.md")
        # Refuse to overwrite an existing fallback so a previous attempt
        # is not silently lost; the operator can compare or rotate manually.
        if os.path.exists(path):
            logger.info("reverse channel: fallback already exists for %s, skipping", issue_id)
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        logger.info("reverse channel: wrote fallback for %s to %s", issue_id, path)
    except OSError as exc:
        logger.error("reverse channel: could not write fallback for %s: %s", issue_id, exc)


def _summarize_handler_result(result: dict[str, Any] | None) -> str:
    """Reduce a handler result dict to a one-or-two-line audit summary."""
    if not isinstance(result, dict):
        return "(no result)"
    bits = []
    status = result.get("status")
    if status:
        bits.append(f"status={status}")
    task_id = result.get("task_id")
    if task_id:
        bits.append(f"task_id={task_id}")
    conflicts = result.get("conflicts")
    if isinstance(conflicts, list) and conflicts:
        bits.append(f"conflicts={len(conflicts)}")
    violations = result.get("violations")
    if isinstance(violations, list) and violations:
        bits.append(f"violations={len(violations)}")
    error = result.get("error")
    if error:
        bits.append(f"error={error}")
    return ", ".join(bits) if bits else "ok"


def _audit_to_multica(issue_id: str, event_type: str, summary: str) -> None:
    """Best-effort audit-trail ship-back to Multica as an issue comment.

    Opt-in via MULTICA_AUDIT_TRAIL=true (default false) to avoid spamming
    Multica with every webhook event. Failures are logged but never
    raised -- the MAC ledger already holds the authoritative record, so
    losing an audit comment is acceptable. No fallback file is written
    on failure (unlike the review-packet reverse channel) because
    audit-trail loss is non-critical and we want to keep the working
    directory clean during long-running bridges.
    """
    if not AUDIT_TRAIL:
        return
    if not MULTICA_API_URL:
        logger.debug("audit trail: MULTICA_API_URL not set, skipping %s", event_type)
        return
    if not issue_id:
        # Some events (e.g. roster broadcasts) may not carry an issue_id;
        # without one we cannot attach a comment to a Multica issue.
        logger.debug("audit trail: no issue_id on %s, skipping", event_type)
        return
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = f"### [MAC audit] `{event_type}` @ {ts}\n\n{summary}\n"
    ok, status, exc = _post_json_to_multica(
        f"/api/issues/{issue_id}/comments", body, timeout=5,
    )
    if ok:
        logger.info("audit trail: posted %s for %s (status=%d)", event_type, issue_id, status)
    elif exc is not None:
        logger.warning("audit trail: failed to post %s/%s (%s)", issue_id, event_type, exc)
    else:
        logger.warning("audit trail: non-2xx %d for %s/%s", status, issue_id, event_type)


# ----- event handlers ---------------------------------------------------------


def _on_issue_created(data: dict[str, Any]) -> dict[str, Any]:
    issue_id = data["issue_id"]
    task = TaskTransfer(
        task_id=_task_id(issue_id),
        source_agent_id="multica",
        target_agent_id=data.get("assignee"),
        payload=TaskPayload(
            type="multica_issue",
            summary=data.get("title", ""),
            acceptance_criteria=data.get("acceptance_criteria", []),
            target_files=data.get("target_files", []),
            metadata={"multica_url": data.get("url", "")},
        ),
        metadata={"multica_issue_id": issue_id, "multica_url": data.get("url", "")},
    )
    saved = registry.submit_task(task)
    return {"status": "submitted", "task_id": saved.task_id}


def _on_agent_started(data: dict[str, Any]) -> dict[str, Any]:
    # Multica dispatches -> MAC transitions proposed -> accepted -> running so
    # the eventual `done()` lands cleanly. Accept/start are idempotent under
    # Multica at-least-once delivery: duplicate calls hit StateConflictError,
    # which we swallow.
    tid = _task_id(data["issue_id"])
    agent_id = data["agent_id"]
    # Sync the agent card from Multica roster metadata (if provided) so
    # MAC path-boundary enforcement turns on automatically without the
    # operator having to register the agent by hand. Older Multica
    # deployments that do not yet send `agent_card` simply skip this
    # step and keep behaving like today (no enforcement).
    card_payload = data.get("agent_card")
    card_synced = False
    if isinstance(card_payload, dict) and card_payload:
        try:
            registry.register(
                AgentCard(
                    agent_id=agent_id,
                    name=card_payload.get("name", agent_id),
                    version=str(card_payload.get("version", "1.0")),
                    allowed_paths=list(card_payload.get("allowed_paths", []) or []),
                    forbidden_paths=list(card_payload.get("forbidden_paths", []) or []),
                    metadata=dict(card_payload.get("metadata", {}) or {}),
                )
            )
            card_synced = True
        except Exception as exc:  # noqa: BLE001 -- sync is best-effort
            logger.warning("agent card sync failed for %s: %s", agent_id, exc)
    # Idempotent under Multica at-least-once delivery:
    # duplicate calls hit StateConflictError, which we suppress.
    for action in ("accept_handoff", "start_task"):
        with contextlib.suppress(StateConflictError):
            getattr(registry, action)(tid, agent_id)
    return {
        "status": "running",
        "task_id": tid,
        "agent_id": agent_id,
        "card_synced": card_synced,
    }


def _on_agent_commented(data: dict[str, Any]) -> dict[str, Any]:
    tid = _task_id(data["issue_id"])
    registry.record_checkpoint(
        tid,
        agent_id=data.get("agent_id", "multica"),
        checkpoint={"comment": data.get("body", ""), "ts": data.get("ts")},
    )
    return {"status": "checkpointed", "task_id": tid}


def _on_agent_completed(data: dict[str, Any]) -> dict[str, Any]:
    tid = _task_id(data["issue_id"])
    agent_id = data["agent_id"]
    verification_raw = data.get("verification", "")
    cmd, _, status = verification_raw.partition(":")
    verifications = []
    if verification_raw:
        verifications.append(
            VerificationEntry(
                command=cmd or verification_raw,
                result="pass" if status == "pass" else "fail",
            )
        )
    quality = {"command": data.get("ci_command", "pr-ci"), "status": "passed"}
    handoff = HandoffResult(
        task_id=tid,
        agent_id=agent_id,
        changed_files=data.get("changed_files", []),
        verification=verifications,
        risks=data.get("risks", []),
    )
    result = registry.done(
        tid,
        agent_id,
        quality_result=quality,
        handoff=handoff,
        detect_conflicts=True,
        enforce_boundaries=True,
        guarded_patterns=GUARDED_PATTERNS or None,
        refuse_on_blocking=bool(GUARDED_PATTERNS) and REFUSE_ON_BLOCKING,
    )
    # Reverse channel: post the review packet back to Multica so the
    # original issue shows the structured handoff. Failures are
    # non-fatal and persisted to disk; the webhook response still
    # returns 200.
    status = result.get("status")
    if status == "boundary_violation":
        # The handoff crossed the agent path boundary; skip the reverse
        # channel (no review packet to post) but keep the webhook 200 so
        # Multica gets the structured violations back.
        logger.warning(
            "path boundary violation for %s: %s",
            tid,
            result.get("violations"),
        )
        return result
    if status == "blocking_conflict":
        # A blocking overlap was detected under a guarded module;
        # the task has been rolled back to running so the agent can
        # address the conflict before retrying. Skip the reverse
        # channel (no completed-state review packet to post) but keep
        # the webhook 200 so Multica sees the structured conflicts.
        logger.warning(
            "blocking conflict for %s under guarded module: %d conflict(s)",
            tid,
            len(result.get("conflicts") or []),
        )
        return result
    if status in ("completed", "review_ready"):
        issue_id = data["issue_id"]
        try:
            packet = _format_review_packet(
                issue_id, tid, handoff, quality,
                conflicts=result.get("conflicts"),
            )
            _post_review_to_multica(issue_id, packet)
        except Exception as exc:  # last-resort safety net
            logger.exception("reverse channel: unexpected error for %s: %s", issue_id, exc)
    return result


def _on_agent_heartbeat(data: dict[str, Any]) -> dict[str, Any]:
    """Refresh an agent's status / load / last_heartbeat in the ledger.

    Multica sends these periodically so MAC can keep its scheduling
    ranking fresh (claim_next_task sorts by -load among candidates with
    the required capability). Heartbeat for an unknown agent returns a
    structured error rather than auto-creating a card -- heartbeat is
    meant to refresh an existing registration, not to register; the
    canonical registration event remains agent.started.
    """
    agent_id = data.get("agent_id", "")
    if not agent_id:
        return {"status": "error", "error": "missing_agent_id"}
    try:
        refreshed = registry.heartbeat_agent(
            agent_id,
            status=data.get("status", "online"),
            load=data.get("load"),
        )
        return {
            "status": "heartbeated",
            "agent_id": agent_id,
            "load": refreshed.load,
            "card_status": refreshed.status,
            "last_heartbeat": refreshed.last_heartbeat,
        }
    except KeyError:
        return {
            "status": "error",
            "error": "unknown_agent",
            "agent_id": agent_id,
        }


def _on_agent_failed(data: dict[str, Any]) -> dict[str, Any]:
    tid = _task_id(data["issue_id"])
    return {
        "status": "failed",
        "task_id": registry.fail_task(
            tid, data["agent_id"], data.get("error_code", "unknown"), data.get("message", "")
        ).task_id,
    }


HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "issue.created": _on_issue_created,
    "agent.started": _on_agent_started,
    "agent.commented": _on_agent_commented,
    "agent.completed": _on_agent_completed,
    "agent.heartbeat": _on_agent_heartbeat,
    "agent.failed": _on_agent_failed,
}


# ----- webhook entry point ----------------------------------------------------


@app.post("/webhook/multica")
async def multica_webhook(request: Request) -> dict[str, Any]:
    body = await request.body()
    if not _verify(body, request.headers.get("X-Multica-Signature", "")):
        raise HTTPException(401, "bad_signature")
    event = json.loads(body or b"{}")
    handler = HANDLERS.get(event.get("type"))
    if handler is None:
        return {"ignored": event.get("type")}
    data = event.get("data", {}) or {}
    result = handler(data)
    # Fire-and-forget audit so reviewers see the full lifecycle on the
    # original Multica issue without having to round-trip via mac-agent.
    _audit_to_multica(
        data.get("issue_id", ""),
        event.get("type", ""),
        _summarize_handler_result(result),
    )
    return result


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/agents")
def list_agents() -> dict[str, Any]:
    """List registered agents (with their last heartbeat and load).

    Operators use this to see which agents are currently known to MAC
    and decide whether to investigate stale ones. Cheap query -- reads
    directly from the ledger.
    """
    agents = registry.ledger.list_agent_cards()
    return {
        "count": len(agents),
        "agents": [
            {
                "agent_id": a.agent_id,
                "name": a.name,
                "status": a.status,
                "load": a.load,
                "last_heartbeat": a.last_heartbeat,
                "capabilities": [c.name for c in (a.capabilities or [])],
            }
            for a in agents
        ],
    }


@app.post("/agents/{agent_id}/heartbeat")
def heartbeat_agent(agent_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Synchronous heartbeat endpoint for agents that prefer HTTP over
    Multica webhooks. Body (optional): ``{"status": "online"|"busy"|"offline",
    "load": 0..100}``. Returns 404 with a structured body if the agent
    is not registered; 200 with the refreshed card otherwise.
    """
    body = payload or {}
    try:
        refreshed = registry.heartbeat_agent(
            agent_id,
            status=body.get("status", "online"),
            load=body.get("load"),
        )
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail={"error": "unknown_agent", "agent_id": agent_id},
        )
    return {
        "agent_id": refreshed.agent_id,
        "status": refreshed.status,
        "load": refreshed.load,
        "last_heartbeat": refreshed.last_heartbeat,
    }


# ----- CLI / demo -------------------------------------------------------------


def _demo() -> int:
    """Drive the handlers with synthetic events; no HTTP server needed."""
    sample = [
        {
            "type": "issue.created",
            "data": {
                "issue_id": "DEMO-1",
                "title": "Fix auth race",
                "url": "https://multica/issues/DEMO-1",
                "acceptance_criteria": ["No double-login"],
                "target_files": ["src/auth.py"],
            },
        },
        {
            "type": "agent.started",
            "data": {
                "issue_id": "DEMO-1",
                "agent_id": "claude-frontend",
            },
        },
        {
            "type": "agent.commented",
            "data": {
                "issue_id": "DEMO-1",
                "agent_id": "claude-frontend",
                "body": "Reprod locally",
            },
        },
        {
            "type": "agent.completed",
            "data": {
                "issue_id": "DEMO-1",
                "agent_id": "claude-frontend",
                "changed_files": ["src/auth.py"],
                "verification": "pytest:pass",
                "risks": ["manual browser check pending"],
            },
        },
        {
            "type": "issue.created",
            "data": {
                "issue_id": "DEMO-2",
                "title": "Refactor ledger schema",
                "url": "https://multica/issues/DEMO-2",
            },
        },
        {
            "type": "agent.failed",
            "data": {
                "issue_id": "DEMO-2",
                "agent_id": "codex-migrator",
                "error_code": "build_broken",
                "message": "compilation error in registry.py:42",
            },
        },
    ]
    for ev in sample:
        etype = ev["type"]
        result = HANDLERS[etype](ev["data"])
        print(f"[demo] {etype:18s} -> {result}")
    print("[demo] ledger at: " + os.path.abspath(DB_PATH))
    return 0


def _check_bind_safety() -> None:
    """Refuse non-loopback binds when HMAC secret is unset.

    The webhook falls open (skip signature verification) when
    MULTICA_WEBHOOK_SECRET is empty, so binding to anything
    other than localhost in that mode would expose the bridge
    to unsigned requests. Bail loudly instead.
    """
    if not WEBHOOK_SECRET and LISTEN_HOST not in ("127.0.0.1", "::1", "localhost"):
        sys.exit(
            "refusing to bind to non-loopback host without "
            "MULTICA_WEBHOOK_SECRET set. Either set the env var "
            "or run with BRIDGE_HOST=127.0.0.1."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multica -> MAC webhook bridge")
    parser.add_argument("--demo", action="store_true", help="run synthetic events without HTTP")
    args = parser.parse_args()
    if args.demo:
        sys.exit(_demo())
    _check_bind_safety()
    import uvicorn

    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT)
