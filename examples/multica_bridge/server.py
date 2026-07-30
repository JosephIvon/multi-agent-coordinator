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
    MULTICA_OUTBOX_DIR (default .agent-context/outbox) as a structured
    JSON file with kind/path/body/issue_id/attempt metadata, and a
    warning is logged; the webhook response still returns 200 so
    Multica does not retry. The outbox can be drained manually via
    POST /outbox/replay, or you can replay entries from your own cron.

Audit-trail shipping (also MAC -> Multica, but distinct from the review
    packet):
    Set MULTICA_AUDIT_TRAIL=true to enable. After every handled webhook
    the bridge POSTs a short one-line audit comment ("[MAC audit]
    `<event_type>` @ <ts>") back to the same Multica issue. Failures
    are logged at WARNING but do NOT write to the outbox -- audit
    loss is non-critical because the MAC ledger already holds the
    authoritative record.

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
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
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
OUTBOX_DIR = os.environ.get("MULTICA_OUTBOX_DIR", ".agent-context/outbox")
OUTBOX_MAX_ATTEMPTS = int(os.environ.get("MULTICA_OUTBOX_MAX_ATTEMPTS", "3"))
OUTBOX_BACKOFF_SECONDS = float(os.environ.get("MULTICA_OUTBOX_BACKOFF_SECONDS", "0.5"))
DIGESTS_DIR = os.environ.get("MULTICA_DIGESTS_DIR", ".agent-context/digests")
GUARDED_PATTERNS = [
    p.strip()
    for p in os.environ.get("MULTICA_GUARDED_PATHS", "").split(",")
    if p.strip()
]
REFUSE_ON_BLOCKING = os.environ.get("MULTICA_REFUSE_ON_BLOCKING", "true").lower() not in ("0", "false", "no", "")
AUDIT_TRAIL = os.environ.get("MULTICA_AUDIT_TRAIL", "false").lower() not in ("0", "false", "no", "")

# Lightweight traffic counters for the /metrics endpoint. Resets on
# process restart -- the SQLite ledger is the durable source of truth.
# Guarded by a lock so concurrent webhook + replay bursts cannot lose
# increments.
_BRIDGE_LOCK = threading.Lock()
_WEBHOOK_TOTAL: Counter[str] = Counter()
_WEBHOOK_ERRORS: dict[str, Counter[str]] = {}
_REVIEW_POST: Counter[str] = Counter()
_AUDIT_POST: Counter[str] = Counter()
_OUTBOX_WRITES: Counter[str] = Counter()
_OUTBOX_DRAINS: Counter[str] = Counter()
_BRIDGE_STARTED_AT = datetime.datetime.now(datetime.timezone.utc).isoformat()


def _bump_webhook(event_type: str, *, error_code: str | None = None) -> None:
    """Increment the bridge traffic counter for a single webhook.

    Safe to call from concurrent webhook handlers; the lock is
    uncontended in the typical webhook flow and only held for a
    couple of dict ops. ``error_code`` (when present) increments a
    per-event-type sub-counter so dashboards can split failures by
    category without flattening them all into one bucket.
    """
    with _BRIDGE_LOCK:
        _WEBHOOK_TOTAL[event_type] += 1
        if error_code is not None:
            sub = _WEBHOOK_ERRORS.setdefault(event_type, Counter())
            sub[error_code] += 1


def _bump_review_post(outcome: str) -> None:
    with _BRIDGE_LOCK:
        _REVIEW_POST[outcome] += 1


def _bump_audit_post(outcome: str) -> None:
    with _BRIDGE_LOCK:
        _AUDIT_POST[outcome] += 1


def _bump_outbox(kind: str, outcome: str) -> None:
    """``kind`` is ``"write"`` or ``"drain"``; ``outcome`` is a free-form
    string the caller picks (e.g. ``"failed"`` for writes, ``"drained"``
    or ``"kept"`` for drains)."""
    with _BRIDGE_LOCK:
        if kind == "write":
            _OUTBOX_WRITES[outcome] += 1
        elif kind == "drain":
            _OUTBOX_DRAINS[outcome] += 1

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
    lines.append("- Status: completed")
    lines.append("- Capability: multica_issue")
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


def _post_json_to_multica(
    path: str,
    body: str,
    timeout: int = 10,
    *,
    max_attempts: int | None = None,
    backoff: float | None = None,
    idempotency_key: str | None = None,
) -> tuple[bool, int | None, BaseException | None]:
    """POST a JSON envelope to Multica with bounded exponential retry.

    Centralises the URL build, Bearer-token injection, timeout, and
    retry loop that both the review-packet reverse channel and the
    audit-trail ship-back need. When MULTICA_API_URL is empty the
    call is a no-op that returns ``(True, None, None)`` so dev
    environments do not regress.

    Retries on URLError/HTTPError/TimeoutError/OSError up to
    ``max_attempts`` times (default ``OUTBOX_MAX_ATTEMPTS``) with
    exponential backoff starting at ``backoff`` seconds (default
    ``OUTBOX_BACKOFF_SECONDS``). On a 2xx response the loop returns
    immediately with the status code.

    ``idempotency_key`` (when provided) is sent as the
    ``Idempotency-Key`` header so Multica can dedupe a successful
    retry that races with its own retries on the first attempt. The
    caller picks a stable key per logical action -- e.g.
    ``f"review:{issue_id}"`` for review packets,
    ``f"audit:{issue_id}:{event_type}"`` for audit-trail comments --
    so an in-process retry and an outbox replay end up sending the
    same key.

    Returns ``(ok, status_code, exception)`` where ``ok`` is True iff
    Multica returned 2xx. ``status_code`` is the HTTP status on a
    successful response, ``None`` on network failure. ``exception``
    carries the underlying URLError/HTTPError/TimeoutError/OSError on
    final failure, ``None`` otherwise.
    """
    if not MULTICA_API_URL:
        return True, None, None
    if max_attempts is None:
        max_attempts = OUTBOX_MAX_ATTEMPTS
    if backoff is None:
        backoff = OUTBOX_BACKOFF_SECONDS
    url = f"{MULTICA_API_URL}{path}"
    payload = json.dumps({"body": body}).encode("utf-8")
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        headers = {
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {MULTICA_API_TOKEN}"} if MULTICA_API_TOKEN else {}),
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return (200 <= resp.status < 300), resp.status, None
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last_exc = exc
            if attempt < max_attempts:
                time.sleep(backoff * (2 ** (attempt - 1)))
    return False, None, last_exc


def _post_review_to_multica(issue_id: str, body: str) -> bool:
    """POST the review packet markdown back to Multica as an issue comment.

    No-op (and returns True) when MULTICA_API_URL is empty, so dev
    environments without a Multica server keep working. Returns True if
    Multica accepted the comment (2xx), False otherwise. On any network
    failure the body is written to the outbox (MULTICA_OUTBOX_DIR) so a
    cron job or POST /outbox/replay can drain it later.
    """
    if not MULTICA_API_URL:
        logger.info("reverse channel skipped: MULTICA_API_URL not set")
        return True
    ok, status, exc = _post_json_to_multica(
        f"/api/issues/{issue_id}/comments",
        body,
        timeout=10,
        idempotency_key=f"review:{issue_id}",
    )
    if ok:
        _bump_review_post("ok")
        logger.info("reverse channel: posted review packet for %s (status=%d)", issue_id, status)
        return True
    _bump_review_post("failed")
    _bump_outbox("write", "failed")
    if exc is not None:
        logger.warning("reverse channel: failed to post %s (%s); writing outbox entry", issue_id, exc)
    else:
        logger.warning("reverse channel: non-2xx %d for %s; writing outbox entry", status, issue_id)
    _write_outbox_entry(
        issue_id=issue_id,
        kind="review",
        path=f"/api/issues/{issue_id}/comments",
        body=body,
    )
    return False


def _write_outbox_entry(
    *,
    issue_id: str,
    kind: str,
    path: str,
    body: str,
) -> str | None:
    """Persist a failed outbound Multica POST to the outbox for later replay.

    The file is JSON so a cron job (or the bridge itself via
    POST /outbox/replay) can re-POST it without losing context. The
    filename embeds a millisecond timestamp so multiple failures for the
    same issue_id are kept in arrival order; entries are sorted by
    timestamp at replay time.
    """
    try:
        os.makedirs(OUTBOX_DIR, exist_ok=True)
        ts_ms = int(time.time() * 1000)
        # Make the issue_id filename-safe (no / or \ in IDs).
        safe_issue = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in str(issue_id))
        filename = f"{ts_ms}-{safe_issue}-{kind}.json"
        full_path = os.path.join(OUTBOX_DIR, filename)
        entry = {
            "kind": kind,
            "issue_id": str(issue_id),
            "path": path,
            "body": body,
            "idempotency_key": f"{kind}:{issue_id}",
            "first_attempt": ts_ms,
            "last_attempt": ts_ms,
            "attempts": 1,
        }
        with open(full_path, "w", encoding="utf-8") as f:
            json.dump(entry, f, ensure_ascii=False)
        logger.warning(
            "outbox: wrote %s/%s for issue=%s after failed POST", OUTBOX_DIR, filename, issue_id,
        )
        return full_path
    except OSError as exc:
        logger.error("outbox: could not write entry for %s: %s", issue_id, exc)
        return None


def _drain_outbox() -> dict[str, Any]:
    """Replay every outbox entry against Multica and delete the file
    on success. Best-effort: failures are left in the outbox for the
    next attempt. The persisted ``idempotency_key`` (if any) is re-sent
    on the replay, so Multica sees the same key on every drain
    attempt for the same logical action -- identical dedup semantics
    to a fresh in-process POST.
    """
    if not MULTICA_API_URL:
        return {"replayed": 0, "succeeded": 0, "failed": 0, "skipped": True}
    if not os.path.isdir(OUTBOX_DIR):
        return {"replayed": 0, "succeeded": 0, "failed": 0, "skipped": True}
    replayed = succeeded = failed = 0
    for name in sorted(os.listdir(OUTBOX_DIR)):
        if not name.endswith(".json"):
            continue
        full_path = os.path.join(OUTBOX_DIR, name)
        try:
            with open(full_path, encoding="utf-8") as f:
                entry = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("outbox: skipping unreadable entry %s (%s)", name, exc)
            continue
        ok, status, exc = _post_json_to_multica(
            entry["path"], entry["body"], timeout=10,
            idempotency_key=entry.get("idempotency_key"),
        )
        replayed += 1
        if ok:
            try:
                os.remove(full_path)
                succeeded += 1
                _bump_outbox("drain", "drained")
                logger.info("outbox: drained %s for issue=%s", name, entry.get("issue_id"))
            except OSError as e:
                logger.warning("outbox: drained %s but could not remove file: %s", name, e)
        else:
            failed += 1
            _bump_outbox("drain", "kept")
            logger.warning(
                "outbox: %s for issue=%s still failing (status=%s exc=%s)",
                name, entry.get("issue_id"), status, exc,
            )
    return {"replayed": replayed, "succeeded": succeeded, "failed": failed, "skipped": False}


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
        f"/api/issues/{issue_id}/comments",
        body,
        timeout=5,
        idempotency_key=f"audit:{issue_id}:{event_type}",
    )
    if ok:
        _bump_audit_post("ok")
        logger.info("audit trail: posted %s for %s (status=%d)", event_type, issue_id, status)
    elif exc is not None:
        _bump_audit_post("failed")
        logger.warning("audit trail: failed to post %s/%s (%s)", issue_id, event_type, exc)
    else:
        _bump_audit_post("failed")
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
        except Exception as exc:  # -- sync is best-effort
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
        _bump_webhook("__auth__", error_code="bad_signature")
        raise HTTPException(401, "bad_signature")
    event = json.loads(body or b"{}")
    event_type = event.get("type") or "__no_type__"
    handler = HANDLERS.get(event_type)
    if handler is None:
        _bump_webhook(event_type, error_code="unknown_type")
        return {"ignored": event_type}
    _bump_webhook(event_type)
    data = event.get("data", {}) or {}
    try:
        result = handler(data)
    except Exception as exc:  # counter must still fire on handler error
        _bump_webhook(event_type, error_code=type(exc).__name__)
        raise
    # Fire-and-forget audit so reviewers see the full lifecycle on the
    # original Multica issue without having to round-trip via mac-agent.
    _audit_to_multica(
        data.get("issue_id", ""),
        event_type,
        _summarize_handler_result(result),
    )
    return result


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics")
def metrics() -> dict[str, Any]:
    """Bridge traffic counters + MAC ledger metrics in one JSON body.

    Response shape::

        {
          "bridge": {
            "started_at": "2026-...",
            "webhook_total": {"issue.created": 12, "agent.started": 9, ...},
            "webhook_errors": {"__auth__": {"bad_signature": 0}, ...},
            "review_post": {"ok": 8, "failed": 1},
            "audit_post": {"ok": 21, "failed": 0},
            "outbox_writes": {"failed": 1},
            "outbox_drains": {"drained": 1, "kept": 0},
            "outbox_pending": 0,
            "active_agents": 2,
          },
          "mac": {  # delegated to mac.metrics.compute_metrics
              ...six key indicators + samples...
          }
        }

    Cheap to query -- Counter ops are O(1), and the MAC side runs a
    handful of SQLite reads. Use this for ops dashboards; the durable
    truth still lives in the SQLite ledger and mac.metrics can be run
    any time via ``mac-agent metrics`` from the CLI.
    """
    with _BRIDGE_LOCK:
        webhook_total = dict(_WEBHOOK_TOTAL)
        webhook_errors = {
            event_type: dict(errors)
            for event_type, errors in _WEBHOOK_ERRORS.items()
        }
        review_post = dict(_REVIEW_POST)
        audit_post = dict(_AUDIT_POST)
        outbox_writes = dict(_OUTBOX_WRITES)
        outbox_drains = dict(_OUTBOX_DRAINS)

    outbox_pending = 0
    if os.path.isdir(OUTBOX_DIR):
        outbox_pending = sum(
            1 for name in os.listdir(OUTBOX_DIR) if name.endswith(".json")
        )

    active_agents = sum(
        1 for a in registry.ledger.list_agent_cards()
        if a.status == "online"
    )

    bridge = {
        "started_at": _BRIDGE_STARTED_AT,
        "webhook_total": webhook_total,
        "webhook_errors": webhook_errors,
        "review_post": review_post,
        "audit_post": audit_post,
        "outbox_writes": outbox_writes,
        "outbox_drains": outbox_drains,
        "outbox_pending": outbox_pending,
        "active_agents": active_agents,
    }

    try:
        from mac.metrics import compute_metrics
        mac_metrics = compute_metrics(registry.ledger)
    except Exception as exc:  # metrics should never break /metrics
        mac_metrics = {"error": type(exc).__name__, "message": str(exc)}

    return {"bridge": bridge, "mac": mac_metrics}


@app.get("/outbox")
def list_outbox() -> dict[str, Any]:
    """List pending outbox entries (failed Multica POSTs awaiting replay).

    Cheap directory listing; the body of each entry is NOT returned --
    callers should use POST /outbox/replay to actually attempt
    delivery.
    """
    if not os.path.isdir(OUTBOX_DIR):
        return {"count": 0, "entries": []}
    entries = []
    for name in sorted(os.listdir(OUTBOX_DIR)):
        if not name.endswith(".json"):
            continue
        full_path = os.path.join(OUTBOX_DIR, name)
        try:
            with open(full_path, encoding="utf-8") as f:
                entry = json.load(f)
            entries.append({
                "filename": name,
                "kind": entry.get("kind"),
                "issue_id": entry.get("issue_id"),
                "first_attempt": entry.get("first_attempt"),
                "last_attempt": entry.get("last_attempt"),
                "attempts": entry.get("attempts"),
            })
        except (OSError, json.JSONDecodeError):
            entries.append({"filename": name, "broken": True})
    return {"count": len(entries), "entries": entries}


@app.post("/outbox/replay")
def replay_outbox() -> dict[str, Any]:
    """Replay every outbox entry against Multica. Successful replays
    delete their file; failures are kept for the next call. Safe to
    call repeatedly -- it is idempotent against the current outbox
    snapshot.
    """
    return _drain_outbox()


def _safe_parse_iso(ts: str) -> datetime.datetime | None:
    """Parse a Multica-style ISO timestamp, tolerating trailing ``Z``.

    Returns ``None`` for unparsable strings so the digest endpoint can
    silently skip tasks with malformed timestamps instead of 500-ing.
    """
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _collect_digest_items(
    since_iso: str, until_iso: str, project: str | None,
) -> list[dict[str, Any]]:
    """Return ``[{task, handoff}]`` rows where ``handoff.timestamp``
    falls in ``[since_iso, until_iso]`` and ``task.project_context``
    matches ``project`` (when provided).

    Tasks without a handoff result or with an unparsable timestamp
    are skipped silently -- they may be in-progress or were never
    closed by MAC (e.g. failures rolled back to ``running``).
    """
    since_dt = _safe_parse_iso(since_iso)
    until_dt = _safe_parse_iso(until_iso)
    tasks = registry.list_tasks(status="completed", project_context=project)
    rows: list[dict[str, Any]] = []
    for task in tasks:
        handoff = registry.get_handoff_result(task.task_id)
        if handoff is None:
            continue
        ts_dt = _safe_parse_iso(handoff.timestamp)
        if ts_dt is None:
            continue
        if since_dt is not None and ts_dt < since_dt:
            continue
        if until_dt is not None and ts_dt > until_dt:
            continue
        rows.append({"task": task, "handoff": handoff})
    return rows


def _render_digest_markdown(
    items: list[dict[str, Any]],
    since_iso: str,
    until_iso: str,
    project: str | None,
) -> str:
    """Render the digest body as markdown.

    Layout: a header summarising the window, then one bullet per
    task with verification status + changed files + risks. Designed
    to paste straight into a Multica project comment or a standup
    channel.
    """
    project_label = project or "all"
    header = (
        f"# MAC review digest\n\n"
        f"- Window: `{since_iso}` -> `{until_iso}`\n"
        f"- Project: `{project_label}`\n"
        f"- Tasks completed: **{len(items)}**\n"
    )
    if not items:
        return header + "\n_No tasks completed in this window._\n"
    by_agent: dict[str, int] = {}
    for row in items:
        handoff = row["handoff"]
        by_agent[handoff.agent_id] = by_agent.get(handoff.agent_id, 0) + 1
    agent_line = ", ".join(
        f"`{name}` x {count}" for name, count in sorted(by_agent.items())
    )
    body_parts = [header, f"- Agents: {agent_line}\n"]
    for row in items:
        task = row["task"]
        handoff = row["handoff"]
        ver = ", ".join(
            f"{v.command}:{v.result}" for v in handoff.verification
        ) or "(none)"
        files = ", ".join(handoff.changed_files) or "(none)"
        risks = "; ".join(handoff.risks) or "(none)"
        body_parts.append(
            f"\n## `{task.task_id}`\n\n"
            f"- Issue: `{task.title or task.task_id}`\n"
            f"- Agent: `{handoff.agent_id}` @ "
            f"{handoff.timestamp}\n"
            f"- Verification: {ver}\n"
            f"- Changed files: {files}\n"
            f"- Risks: {risks}\n"
        )
    return "".join(body_parts)


def _write_digest_file(
    items: list[dict[str, Any]],
    since_iso: str,
    until_iso: str,
    project: str | None,
    body: str,
) -> str:
    """Persist the digest to ``DIGESTS_DIR`` so an operator or cron
    can ship it to Multica later. Returns the absolute path of the
    written file.

    Filename uses a window stamp and project slug; reruns for the
    same window overwrite the same file (digest is naturally
    idempotent across the same since/until pair).
    """
    os.makedirs(DIGESTS_DIR, exist_ok=True)
    project_slug = "all" if not project else re.sub(r"[^A-Za-z0-9._-]", "_", project)
    # Truncate the stamp to second precision so two requests within
    # the same wall-clock second overwrite the same file. Cron jobs
    # running every minute are well above this granularity anyway,
    # and second-level dedup prevents the directory from filling up
    # under frequent retries.
    def _to_second_stamp(iso: str) -> str:
        ts = _safe_parse_iso(iso)
        if ts is None:
            return re.sub(r"[^0-9TZ-]", "", iso)
        return ts.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stamp = (
        f"{_to_second_stamp(since_iso)}_{_to_second_stamp(until_iso)}"
        .replace(":", "")  # Windows rejects ':' in filenames
    )
    fname = f"digest-{project_slug}-{stamp}.md"
    full = os.path.join(DIGESTS_DIR, fname)
    metadata = {
        "kind": "digest",
        "since": since_iso,
        "until": until_iso,
        "project": project,
        "count": len(items),
        "issue_ids": [
            row["task"].task_id.removeprefix("multica-") for row in items
        ],
        "body_file": os.path.basename(full),
    }
    with open(full, "wb") as f:
        f.write(body.encode("utf-8"))
        f.write(b"\n")
    sidecar = full.removesuffix(".md") + ".json"
    with open(sidecar, "wb") as f:
        f.write(json.dumps(metadata, indent=2).encode("utf-8"))
    return os.path.abspath(full)


@app.get("/reviews/digest")
def reviews_digest(
    since: str | None = None,
    until: str | None = None,
    project: str | None = None,
    ship: bool = False,
) -> dict[str, Any]:
    """Aggregate completed-tasks handoffs into a markdown digest.

    Query params:

    - ``since``  ISO timestamp; default ``now - 24h`` (UTC).
    - ``until``  ISO timestamp; default ``now`` (UTC).
    - ``project``  filter by ``task.project_context``.
    - ``ship``  when true, additionally write the digest body to
      ``MULTICA_DIGESTS_DIR`` (default ``.agent-context/digests``)
      with a ``.md`` body + ``.json`` metadata sidecar. The ship
      itself does NOT POST to Multica -- it produces an
      operator-reviewable artifact; upload to Multica is left to a
      separate cron that POSTs the file.

    Response includes the markdown body in ``digest``, the list of
    contributing task ids in ``task_ids``, and (when ``ship=true``)
    the absolute path of the written file in ``shipped_to``.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    if until is None:
        # Default window end is "right now" -- use full ISO precision
        # so a handoff that just landed (microseconds ago) is in the
        # window; a user-supplied ?until=2026-07-30T00:24:18Z that
        # matches this ``now`` second will still match because both
        # sides parse to the same second when ``Z`` is replaced with
        # ``+00:00``.
        until = now.isoformat()
    if since is None:
        since = (now - datetime.timedelta(hours=24)).isoformat()
    items = _collect_digest_items(since, until, project)
    body = _render_digest_markdown(items, since, until, project)
    payload: dict[str, Any] = {
        "since": since,
        "until": until,
        "project": project,
        "count": len(items),
        "task_ids": [row["task"].task_id for row in items],
        "digest": body,
    }
    if ship:
        path = _write_digest_file(items, since, until, project, body)
        payload["shipped_to"] = path
    return payload


@app.get("/digests")
def list_digests() -> dict[str, Any]:
    """List digest files previously shipped via ``GET
    /reviews/digest?ship=true``.

    Each row points at the ``.md`` body; metadata lives in the
    sibling ``.json``. Sorted by filename (which embeds the
    ``since_until`` stamp, so chronological by construction).
    """
    if not os.path.isdir(DIGESTS_DIR):
        return {"count": 0, "entries": []}
    entries: list[dict[str, Any]] = []
    for name in sorted(os.listdir(DIGESTS_DIR)):
        if not name.endswith(".md"):
            continue
        full = os.path.join(DIGESTS_DIR, name)
        sidecar = full.removesuffix(".md") + ".json"
        meta: dict[str, Any] = {}
        if os.path.exists(sidecar):
            try:
                with open(sidecar, encoding="utf-8") as f:
                    meta = json.load(f)
            except (OSError, json.JSONDecodeError):
                meta = {"sidecar_broken": True}
        entries.append({"filename": name, "path": os.path.abspath(full), **meta})
    return {"count": len(entries), "entries": entries}


def _task_to_json(task: TaskTransfer) -> dict[str, Any]:
    """Render a TaskTransfer as a JSON-friendly dict.

    The pydantic model would serialise via ``model_dump_json``, but
    that includes payload/context/test_contract objects that are
    large for ops dashboards. Here we surface the headline fields
    an operator cares about plus a few quality-of-life additions
    (``multica_issue_id`` strips the ``multica-`` prefix so the
    same id Multica uses appears in the response).
    """
    return {
        "task_id": task.task_id,
        "multica_issue_id": task.task_id.removeprefix("multica-"),
        "status": task.status,
        "title": task.title,
        "description": task.description,
        "project_context": task.project_context,
        "source_agent_id": task.source_agent_id,
        "target_agent_id": task.target_agent_id,
        "priority": task.priority,
        "plan_id": task.plan_id,
        "depends_on": list(task.depends_on),
        "max_hops": task.max_hops,
        "current_hops": task.current_hops,
        "retry_count": task.retry_count,
        "fallback_agent_id": task.fallback_agent_id,
        "error_code": task.error_code,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def _task_row(task: TaskTransfer) -> dict[str, Any]:
    """Render a TaskTransfer as a one-line ops row."""
    return {
        "task_id": task.task_id,
        "multica_issue_id": task.task_id.removeprefix("multica-"),
        "status": task.status,
        "title": task.title,
        "project_context": task.project_context,
        "target_agent_id": task.target_agent_id,
        "priority": task.priority,
        "updated_at": task.updated_at,
    }


@app.get("/tasks")
def list_tasks(
    status: str | None = None,
    project_context: str | None = None,
    agent_id: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """JSON task view over the bridge. Filters mirror the
    ``mac.registry.Registry.list_tasks`` signature, plus a
    ``limit`` cap (default 100) to keep responses small for
    dashboards.

    Result rows are the compact ``_task_row`` shape -- use
    ``GET /tasks/<task_id>`` for the full breakdown.
    """
    cap = max(1, min(int(limit), 500))
    tasks = registry.list_tasks(
        status=status,
        agent_id=agent_id,
        project_context=project_context,
    )
    rows = [_task_row(t) for t in tasks[:cap]]
    return {
        "count": len(rows),
        "total": len(tasks),
        "truncated": len(tasks) > cap,
        "filters": {
            "status": status,
            "project_context": project_context,
            "agent_id": agent_id,
            "limit": cap,
        },
        "tasks": rows,
    }


@app.get("/tasks/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    """Return the full record for a single task, plus its
    handoff result and project context.

    404 with a structured body when the task is unknown so a
    UI can distinguish "wrong id" from "server error".
    """
    ledger = registry.ledger
    row = ledger.get_task_transfer(task_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "unknown_task", "task_id": task_id},
        )
    handoff = registry.get_handoff_result(task_id)
    out = _task_to_json(row)
    out["handoff"] = handoff.model_dump() if handoff is not None else None
    return {"task": out, "has_handoff": handoff is not None}


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
    except KeyError as err:
        raise HTTPException(
            status_code=404,
            detail={"error": "unknown_agent", "agent_id": agent_id},
        ) from err
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
