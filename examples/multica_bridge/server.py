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

This is a 100-line PoC. Things deliberately deferred:

    - No retry / dead-letter queue (Multica at-least-once delivery is enough).
    - No "review_packet -> Multica comment" reverse channel yet (Phase 2).
    - No batch events -- Multica sends one POST per event.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request

from mac.protocol.errors import StateConflictError
from mac.protocol.messages import (
    HandoffResult,
    TaskPayload,
    TaskTransfer,
    VerificationEntry,
)
from mac.registry import Registry
from mac.storage.sqlite import SQLiteTaskLedger


DB_PATH = os.environ.get("MAC_DB", "mac.db")
WEBHOOK_SECRET = os.environ.get("MULTICA_WEBHOOK_SECRET", "")
LISTEN_HOST = os.environ.get("BRIDGE_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("BRIDGE_PORT", "8765"))

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
    for action in ("accept_handoff", "start_task"):
        try:
            getattr(registry, action)(tid, agent_id)
        except StateConflictError:
            pass  # already past that step
    return {"status": "running", "task_id": tid, "agent_id": agent_id}


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
    handoff = HandoffResult(
        task_id=tid,
        agent_id=agent_id,
        changed_files=data.get("changed_files", []),
        verification=verifications,
        risks=data.get("risks", []),
    )
    return registry.done(
        tid,
        agent_id,
        quality_result={"command": data.get("ci_command", "pr-ci"), "status": "passed"},
        handoff=handoff,
    )


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
    return handler(event.get("data", {}))


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


# ----- CLI / demo -------------------------------------------------------------

def _demo() -> int:
    """Drive the handlers with synthetic events; no HTTP server needed."""
    sample = [
        {"type": "issue.created", "data": {
            "issue_id": "DEMO-1", "title": "Fix auth race",
            "url": "https://multica/issues/DEMO-1",
            "acceptance_criteria": ["No double-login"],
            "target_files": ["src/auth.py"],
        }},
        {"type": "agent.started", "data": {
            "issue_id": "DEMO-1", "agent_id": "claude-frontend",
        }},
        {"type": "agent.commented", "data": {
            "issue_id": "DEMO-1", "agent_id": "claude-frontend",
            "body": "Reprod locally",
        }},
        {"type": "agent.completed", "data": {
            "issue_id": "DEMO-1", "agent_id": "claude-frontend",
            "changed_files": ["src/auth.py"],
            "verification": "pytest:pass",
            "risks": ["manual browser check pending"],
        }},
        {"type": "issue.created", "data": {
            "issue_id": "DEMO-2", "title": "Refactor ledger schema",
            "url": "https://multica/issues/DEMO-2",
        }},
        {"type": "agent.failed", "data": {
            "issue_id": "DEMO-2", "agent_id": "codex-migrator",
            "error_code": "build_broken",
            "message": "compilation error in registry.py:42",
        }},
    ]
    for ev in sample:
        etype = ev["type"]
        result = HANDLERS[etype](ev["data"])
        print("[demo] {:18s} -> {}".format(etype, result))
    print("[demo] ledger at: " + os.path.abspath(DB_PATH))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multica -> MAC webhook bridge")
    parser.add_argument("--demo", action="store_true", help="run synthetic events without HTTP")
    args = parser.parse_args()
    if args.demo:
        sys.exit(_demo())
    import uvicorn
    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT)