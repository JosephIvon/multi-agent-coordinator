from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Literal, cast

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from mac import scoring
from mac.protocol.errors import QualityGateError, StateConflictError
from mac.protocol.messages import TaskTransfer
from mac.quality.gate import evaluate_quality_gate
from mac.registry import Registry
from mac.storage.sqlite import SQLiteTaskLedger

logger = logging.getLogger("mac.mcp_server")
mcp = FastMCP("mac-coordinator")

_DB_PATH: Path | None = None


def _resolve_db_path() -> Path:
    """Resolve the SQLite DB path from ``MAC_DB_PATH`` env var, or default.

    The resolved absolute path is memoised so all tool calls within one
    process life use the same database file, even if the env var changes
    mid-run (which it shouldn't).
    """
    global _DB_PATH
    if _DB_PATH is None:
        raw = os.environ.get("MAC_DB_PATH", "mac.db")
        resolved = Path(raw).resolve()
        # Ensure the parent directory exists so SQLite doesn't fail silently.
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Read-only filesystem, network path, etc. — let SQLite surface
            # the error later with a clear message.
            pass
        _DB_PATH = resolved
    return _DB_PATH


def _registry() -> Registry:
    """Create a Registry backed by the default SQLite ledger."""
    return Registry(SQLiteTaskLedger(_DB_PATH))


def _serialize(result: Any) -> str:
    """Serialize a Pydantic model, list of models, dict, or primitive to JSON.

    Only handles success paths. ``None`` and error conditions are reported
    by ``_safe_call`` raising ``ToolError`` so the MCP transport can mark
    the response with ``isError=True``.
    """
    if isinstance(result, list):
        items = [r.model_dump() if hasattr(r, "model_dump") else r for r in result]
        return json.dumps(items)
    if isinstance(result, str):
        return result
    if hasattr(result, "model_dump"):
        return result.model_dump_json()
    return json.dumps(result)


def _safe_call(func: Any) -> str:
    """Execute *func*, raising :class:`ToolError` for any failure.

    MAC domain exceptions are translated into structured ``ToolError``
    messages so the SDK can mark the response with ``isError=True`` and
    LLM clients can distinguish success from failure.
    """
    from pydantic import ValidationError

    try:
        result = func()
    except ToolError:
        raise
    except KeyError as exc:
        raise ToolError(f"not_found: {exc}") from exc
    except ValidationError as exc:
        raise ToolError(f"validation_failed: {exc.errors()}") from exc
    except QualityGateError as exc:
        raise ToolError(f"quality_gate_failed: {exc}") from exc
    except StateConflictError as exc:
        raise ToolError(f"state_conflict: {exc}") from exc
    if result is None:
        raise ToolError("not_found")
    return _serialize(result)


# ---------------------------------------------------------------------------
# Long-lived registry for scoring hooks (Round 16)
# ---------------------------------------------------------------------------
#
# Unlike _registry() which is rebuilt per call, the scorer tools need a
# process-wide Registry so set_scoring_fn() sticks across MCP requests.
# Other tools continue to use the stateless _registry() so they remain
# side-effect free.

_LONG_REGISTRY: Registry | None = None


def _long_registry() -> Registry:
    # Memoised Registry used by mac_set_scorer / mac_list_scorers / etc.
    global _LONG_REGISTRY
    if _LONG_REGISTRY is None:
        _LONG_REGISTRY = Registry(SQLiteTaskLedger(_DB_PATH))
    return _LONG_REGISTRY


# ---------------------------------------------------------------------------
# Tools (30 across 6 categories: 10 task + 3 review + 6 maintenance + 4 knowledge + 3 lease + 3 scoring + 1 lifecycle)
# v1.2.0 final: added mac_get_task, mac_resume_blocked_task, mac_retry_task,
# mac_expire_task_leases, mac_list_agents, mac_block_task
# ---------------------------------------------------------------------------


@mcp.tool()
def mac_submit_task(task: dict) -> str:
    """Submit a new task to the MAC coordination ledger.

    :param task: Full TaskTransfer object as a dict. Schema follows
        mac.protocol.messages.TaskTransfer (Pydantic model).
    :returns: JSON of the created TaskTransfer.
    """

    def _do() -> Any:
        validated = TaskTransfer.model_validate(task)
        return _registry().submit_task(validated)

    return _safe_call(_do)


@mcp.tool()
def mac_claim_task(
    agent_id: str,
    capability: str,
    project_context: str | None = None,
    best_effort: bool = False,
    role: str | None = None,
) -> str:
    """Claim the next available proposed task and start it.

    Atomically: claim_next_task (claim + accept) → start_task.

    :param agent_id: ID of the claiming agent.
    :param capability: Required capability to match.
    :param project_context: Optional project filter.
    :param best_effort: If True, consider tasks with other capabilities.
    :param role: Optional role filter (arch/core/crud/test/review).
        When set, only tasks whose required_role matches (or is unset)
        are considered. When unset, the agent's registered roles are
        matched against task required_role fields.
    :returns: JSON of the claimed-and-started TaskTransfer, or not_found.
    """

    def _do() -> Any:
        reg = _registry()
        claimed = reg.claim_next_task(
            agent_id=agent_id,
            capability=capability,
            project_context=project_context,
            best_effort=best_effort,
            role=role,
        )
        if claimed is None:
            return None
        return reg.start_task(claimed.task_id, agent_id)

    return _safe_call(_do)


@mcp.tool()
def mac_record_quality_and_complete(
    task_id: str,
    agent_id: str,
    result: dict,
) -> str:
    """Submit quality evidence and, if the gate passes, complete the task.

    One-step atomic: submit_quality_result → evaluate_quality_gate →
    complete_task (only if gate passes).

    .. deprecated:: Prefer ``mac_done`` which also handles handoff and review
        lifecycle automatically.

    :param task_id: ID of the task.
    :param agent_id: ID of the agent submitting evidence.
    :param result: Quality result dict (must include 'command' and 'status').
    :returns: JSON with status='completed' if gate passes,
        or status='running' with reason if more evidence is needed.
    """

    def _do() -> Any:
        reg = _registry()
        reg.submit_quality_result(task_id, result)
        task = reg.ledger.get_task_transfer(task_id)
        if task is None:
            return None
        quality_results = reg.ledger.get_quality_results(task_id)
        allowed, reason = evaluate_quality_gate(task.test_contract, quality_results)
        if allowed:
            reg.complete_task(task_id, agent_id)
            return {"status": "completed", "task_id": task_id, "reason": reason}
        return {"status": "running", "task_id": task_id, "reason": reason}

    return _safe_call(_do)


@mcp.tool()
def mac_done(
    task_id: str,
    agent_id: str,
    quality_result: dict | None = None,
    changed_files: list[str] | None = None,
    risks: list[str] | None = None,
) -> str:
    """Finish a task in one step: submit quality evidence, save handoff, and complete (or mark review-ready).

    Automatically detects whether to complete or mark review-ready based on
    the CoordinationPolicy (``require_review``).  This is the primary way
    AI agents finish tasks — no need to know the state machine.

    :param task_id: ID of the running task.
    :param agent_id: ID of the agent finishing the task.
    :param quality_result: Optional quality evidence dict (must include
        ``command`` and ``status`` when provided).  If omitted, previously
        submitted quality results are used for gate evaluation.
    :param changed_files: List of files modified during work.
    :param risks: List of risk descriptions.
    :returns: JSON summary with ``status``, ``task_id``, ``quality_gate``,
        and optionally ``review`` and ``reason``.
    """

    def _do() -> Any:
        from mac.protocol.messages import HandoffResult, VerificationEntry

        reg = _registry()
        handoff = None
        if changed_files or risks:
            handoff = HandoffResult(
                task_id=task_id,
                agent_id=agent_id,
                changed_files=changed_files or [],
                risks=risks or [],
                verification=[VerificationEntry(command="done", result="pass")],
            )
        result = reg.done(
            task_id,
            agent_id,
            quality_result=quality_result,
            handoff=handoff,
        )

        # EOD hint: if 3+ tasks completed today, suggest running EOD
        try:
            kanban = reg.get_kanban()
            done_today = kanban.get("done", {}).get("total", 0)
            if done_today >= 3:
                result["eod_hint"] = (
                    f"{done_today} tasks completed today. "
                    "Consider running EOD: pwsh -NoProfile -File ~/.claude/hooks/eod.ps1"
                )
        except Exception:
            pass  # Non-fatal: EOD hint is optional

        return result

    return _safe_call(_do)


@mcp.tool()
def mac_fail_task(
    task_id: str,
    agent_id: str,
    error_code: str,
    message: str = "",
) -> str:
    """Mark a running task as failed.

    :param task_id: ID of the task to fail.
    :param agent_id: ID of the agent reporting failure.
    :param error_code: Error code from ERROR_CODES constant set.
    :param message: Optional human-readable error description.
    :returns: JSON of the failed TaskTransfer.
    """

    def _do() -> Any:
        return _registry().fail_task(task_id, agent_id, error_code, message)

    return _safe_call(_do)


@mcp.tool()
def mac_cancel_task(task_id: str, agent_id: str, reason: str = "") -> str:
    """Cancel a task.

    Cancels a task that is no longer needed. Canceled tasks are terminal
    and cannot be re-claimed.

    :param task_id: ID of the task to cancel.
    :param agent_id: ID of the agent requesting cancellation.
    :param reason: Optional reason for cancellation.
    :returns: JSON of the canceled TaskTransfer.
    """

    def _do() -> Any:
        return _registry().cancel_task(task_id, agent_id=agent_id, reason=reason)

    return _safe_call(_do)


@mcp.tool()
def mac_save_handoff(
    task_id: str,
    agent_id: str,
    changed_files: list[str] | None = None,
    verification_passed: bool = True,
    boundary_review: str = "not_required",
    risks: list[str] | None = None,
) -> str:
    """Save a structured handoff result for a completed task.

    :param task_id: ID of the task.
    :param agent_id: ID of the agent performing the handoff.
    :param changed_files: List of files modified during work.
    :param verification_passed: Whether verification commands passed.
    :param boundary_review: Path guardrail result (pass/block/not_required).
    :param risks: List of risk descriptions.
    :returns: JSON of the saved HandoffResult.
    """

    def _do() -> Any:
        from mac.protocol.messages import HandoffResult, VerificationEntry

        handoff = HandoffResult(
            task_id=task_id,
            agent_id=agent_id,
            changed_files=changed_files or [],
            boundary_review=cast(Literal["pass", "block", "not_required"], boundary_review),
            risks=risks or [],
            verification=[
                VerificationEntry(
                    command="handoff",
                    result="pass" if verification_passed else "fail",
                )
            ],
        )
        return _registry().save_handoff_result(handoff)

    return _safe_call(_do)


@mcp.tool()
def mac_list_ready_tasks(
    capability: str | None = None,
    project_context: str | None = None,
    role: str | None = None,
) -> str:
    """List dependency-unblocked proposed tasks ready for claiming.

    :param capability: Optional capability filter.
    :param project_context: Optional project filter.
    :param role: Optional role filter (arch/core/crud/test/review).
        When set, only tasks whose required_role matches (or is unset)
        are listed.
    :returns: JSON array of ready TaskTransfer objects.
    """

    def _do() -> Any:
        return _registry().list_ready_tasks(capability=capability, project_context=project_context, role=role)

    return _safe_call(_do)


@mcp.tool()
def mac_review_packet(task_id: str) -> str:
    """Generate a Markdown review packet for a task.

    :param task_id: ID of the task.
    :returns: Markdown string with task context, evidence, and handoff.
    """

    def _do() -> Any:
        return _registry().prepare_review_packet(task_id)

    return _safe_call(_do)


@mcp.tool()
def mac_worker_packet(task_id: str, agent_id: str | None = None) -> str:
    """Generate a Markdown worker packet for a task.

    Mirrors mac_review_packet on the worker side: provides goal, dependency
    context, acceptance criteria, and (when agent_id is given) the agent's
    boundary guardrails so the worker knows what it can touch.

    :param task_id: ID of the task.
    :param agent_id: Optional agent ID; when supplied the packet includes
        the agent's allowed_paths and forbidden_paths.
    :returns: Markdown string with worker-facing task instructions.
    """

    def _do() -> Any:
        return _registry().prepare_worker_packet(task_id, agent_id=agent_id)

    return _safe_call(_do)


@mcp.tool()
def mac_mark_review_ready(task_id: str, agent_id: str) -> str:
    """Move a running task to ``review_ready``.

    Only valid when ``CoordinationPolicy.require_review=True``; otherwise
    the registry rejects the transition with ``state_conflict``.

    :param task_id: ID of the running task.
    :param agent_id: ID of the agent performing the handoff.
    :returns: JSON of the ``review_ready`` TaskTransfer, or state_conflict error.
    """

    def _do() -> Any:
        return _registry().mark_review_ready(task_id, agent_id=agent_id)

    return _safe_call(_do)


@mcp.tool()
def mac_accept_review(task_id: str, reviewer_id: str) -> str:
    """Accept a task in ``review_ready`` status, completing it.

    :param task_id: ID of the task to accept.
    :param reviewer_id: ID of the reviewer accepting the task.
    :returns: JSON of the completed TaskTransfer.
    """

    def _do() -> Any:
        return _registry().accept_review(task_id, reviewer_id=reviewer_id)

    return _safe_call(_do)


@mcp.tool()
def mac_reject_review(task_id: str, reviewer_id: str, reason: str = "") -> str:
    """Reject a task in ``review_ready`` status.

    The rejection reason is automatically recorded as a blocking conflict.

    :param task_id: ID of the task to reject.
    :param reviewer_id: ID of the reviewer rejecting the task.
    :param reason: Human-readable rejection reason; recorded in the conflict.
    :returns: JSON of the rejected TaskTransfer.
    """

    def _do() -> Any:
        return _registry().reject_review(task_id, reviewer_id=reviewer_id, reason=reason)

    return _safe_call(_do)


@mcp.tool()
def mac_expire_stale_tasks(auto_retry: bool = False) -> str:
    """Expire non-terminal tasks past their TTL.

    Scans for tasks in proposed, accepted, running, or review_ready status
    whose TTL has elapsed. When auto_retry=True and the task has retries
    remaining, it is reset to ``proposed`` instead of being failed.

    :param auto_retry: If True, auto-retry tasks with retries remaining.
    :returns: JSON array of expired/retried TaskTransfer objects.
    """

    def _do() -> Any:
        return _registry().expire_stale_tasks(auto_retry=auto_retry)

    return _safe_call(_do)


@mcp.tool()
def mac_next_task(
    agent_id: str,
    capability: str,
    project_context: str | None = None,
    best_effort: bool = False,
    role: str | None = None,
) -> str:
    """Atomically claim, start, and generate a worker packet for the next ready task.

    One-shot convenience: claim_next_task → start_task → prepare_worker_packet.

    :param agent_id: ID of the claiming agent.
    :param capability: Required capability to match.
    :param project_context: Optional project filter.
    :param best_effort: If True, consider tasks with other capabilities.
    :param role: Optional role filter (arch/core/crud/test/review).
    :returns: Markdown worker packet string, or not_found if no task available.
    """

    def _do() -> Any:
        reg = _registry()
        claimed = reg.claim_next_task(
            agent_id=agent_id,
            capability=capability,
            project_context=project_context,
            best_effort=best_effort,
            role=role,
        )
        if claimed is None:
            return None
        started = reg.start_task(claimed.task_id, agent_id)
        return reg.prepare_worker_packet(started.task_id, agent_id=agent_id)

    return _safe_call(_do)


@mcp.tool()
def mac_expire_stale_agents(timeout_seconds: int | None = None) -> str:
    """Set offline agents whose last heartbeat is older than the timeout.

    :param timeout_seconds: Timeout in seconds. Defaults to policy.agent_timeout (300s).
    :returns: JSON array of expired AgentCard objects.
    """

    def _do() -> Any:
        return _registry().expire_stale_agents(timeout_seconds=timeout_seconds)

    return _safe_call(_do)


@mcp.tool()
def mac_cleanup_tasks(
    statuses: list[str] | None = None,
    plan_id: str | None = None,
    older_than_seconds: float | None = None,
) -> str:
    """Delete terminal tasks (failed/cancelled/rejected/superseded) from the ledger.

    Useful for removing completed-but-failed tasks that clutter the dashboard.
    Deleted tasks are recorded in the audit trail.

    :param statuses: Task statuses to clean up. Defaults to
        ``["failed", "cancelled", "rejected", "superseded"]``.
    :param plan_id: Only clean tasks belonging to this plan.
    :param older_than_seconds: Only clean tasks whose updated_at is older
        than this many seconds. None means no age filter.
    :returns: JSON array of deleted TaskTransfer objects.
    """

    def _do() -> Any:
        return _registry().cleanup_tasks(
            statuses=statuses,
            plan_id=plan_id,
            older_than_seconds=older_than_seconds,
        )

    return _safe_call(_do)


@mcp.tool()
def mac_list_agents(status: str = "online") -> str:
    """List all registered agents, optionally filtered by status.

    :param status: Filter by agent status — ``online``, ``offline``, or ``all`` (default: ``online``).
    :returns: JSON array of AgentCard dicts.
    """

    def _do() -> Any:
        agents = _registry().discover()
        if status != "all":
            agents = [a for a in agents if a.status == status]
        return agents

    return _safe_call(_do)


@mcp.tool()
def mac_block_task(task_id: str, agent_id: str, reason: str, handoff_to: str | None = None) -> str:
    """Block a running task with a reason.

    Used when an agent encounters a blocking issue it cannot resolve (e.g.,
    missing dependency, need external decision). The task moves to ``blocked``
    status and can be resumed later with ``mac_resume_blocked_task``.

    :param task_id: The task to block.
    :param agent_id: The agent requesting the block.
    :param reason: Why the task is being blocked (required).
    :param handoff_to: Optional agent ID to hand the blocked task to.
    :returns: JSON of the blocked TaskTransfer.
    """

    def _do() -> Any:
        return _registry().block_task(
            task_id, agent_id=agent_id, reason=reason, handoff_to=handoff_to
        )

    return _safe_call(_do)


@mcp.tool()
def mac_get_task(task_id: str) -> str:
    """Get a task by ID. Returns the full TaskTransfer as JSON.

    :param task_id: The task ID to look up.
    :returns: JSON of the TaskTransfer, or an error if not found.
    """

    def _do() -> Any:
        task = _registry().get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    return _safe_call(_do)


@mcp.tool()
def mac_resume_blocked_task(task_id: str, agent_id: str, resolution: str = "") -> str:
    """Resume a blocked task back to proposed so it can be re-claimed.

    After a quality gate hard-fail (C-2), the task is in ``blocked`` status with
    ``error_code="TASK_BLOCKED"``.  Call this tool to clear the blocker and reset
    the task to ``proposed``, then re-claim with ``mac_claim_task`` or
    ``mac_next_task``.

    :param task_id: The blocked task to resume.
    :param agent_id: The agent requesting the resume.
    :param resolution: Optional note describing how the blocker was resolved.
    :returns: JSON of the resumed TaskTransfer (status=proposed).
    """

    def _do() -> Any:
        return _registry().resume_blocked_task(task_id, agent_id=agent_id, resolution=resolution)

    return _safe_call(_do)


@mcp.tool()
def mac_retry_task(task_id: str, agent_id: str, fallback_agent_id: str | None = None) -> str:
    """Retry a failed task by resetting it to proposed.

    The task's ``retry_count`` is incremented and it can be re-claimed by any
    eligible agent.  Use ``fallback_agent_id`` to route the retry to a different
    agent than the original.

    :param task_id: The failed task to retry.
    :param agent_id: The agent requesting the retry.
    :param fallback_agent_id: Optional alternative agent to target.
    :returns: JSON of the reset TaskTransfer (status=proposed).
    """

    def _do() -> Any:
        return _registry().retry_task(task_id, agent_id=agent_id, fallback_agent_id=fallback_agent_id)

    return _safe_call(_do)


@mcp.tool()
def mac_expire_task_leases(auto_retry: bool = False) -> str:
    """Expire tasks whose per-attempt lease has elapsed.

    Scans ``accepted`` and ``running`` tasks where ``lease_seconds > 0`` and
    ``(now - claimed_at) > lease_seconds``.  If ``auto_retry`` is True and the
    task has remaining retries, it is reset to ``proposed``; otherwise it is
    transitioned to ``failed`` with ``error_code="LEASE_EXPIRED"``.

    :param auto_retry: If True, auto-retry tasks with remaining retries.
    :returns: JSON array of expired TaskTransfer objects.
    """

    def _do() -> Any:
        return _registry().expire_task_leases(auto_retry=auto_retry)

    return _safe_call(_do)


# ---------------------------------------------------------------------------
# Resources (4)
# ---------------------------------------------------------------------------


@mcp.resource("mac://capabilities")
def capabilities_resource() -> str:
    """Current capability registry: agents grouped by capability."""
    reg = _registry()
    agents = reg.ledger.list_agent_cards()
    cap_map: dict[str, list[str]] = {}
    for agent in agents:
        for cap in getattr(agent, "capabilities", []):
            name = cap.name if hasattr(cap, "name") else str(cap)
            cap_map.setdefault(name, []).append(agent.agent_id)
    return json.dumps(cap_map)


@mcp.resource("mac://health")
def health_resource() -> str:
    """Health summary: last_updated, open_tasks, inflight_agents."""
    reg = _registry()
    all_tasks = reg.ledger.list_task_transfers()
    all_agents = reg.ledger.list_agent_cards()
    open_tasks = [t for t in all_tasks if t.status in ("proposed", "accepted", "running")]
    inflight = [a for a in all_agents if getattr(a, "status", "") == "online" and a.load > 0]
    last_updated = max(
        (getattr(t, "updated_at", "") for t in all_tasks),
        default="",
    )
    return json.dumps({
        "last_updated": last_updated,
        "open_tasks": len(open_tasks),
        "inflight_agents": len(inflight),
    })


@mcp.resource("mac://kanban")
def kanban_resource() -> str:
    """Four-color kanban board: tasks grouped by stage.

    Returns JSON with:
    - ``red``: proposed tasks waiting to be written (status=proposed)
    - ``yellow``: tasks pending quality evidence (status=accepted or running)
    - ``green``: tasks awaiting review (status=review_ready)
    - ``done``: completed today (status=completed, with counts by agent)
    """
    reg = _registry()
    return json.dumps(reg.get_kanban())


@mcp.tool()
def mac_list_scorers() -> str:
    """List registered scorers from mac.scoring (sync + async).

    Returns the union of named scorers visible to MAC today, including
    the built-in ``priority`` scorer. Names registered in the async
    registry get ``kind="async"``; the rest are ``"sync"``. Use the
    returned names as input to ``mac_set_scorer`` or ``mac_test_scorer``.
    """
    sync_names = sorted(scoring.list_scorers().keys())
    async_names = sorted(scoring.list_async_scorers().keys())
    payload = {
        "sync": [
            {"name": name, "qualname": getattr(scoring.get_scorer(name), "__qualname__", repr(scoring.get_scorer(name)))}
            for name in sync_names
        ],
        "async": [
            {"name": name, "qualname": getattr(scoring.get_async_scorer(name), "__qualname__", repr(scoring.get_async_scorer(name)))}
            for name in async_names
        ],
    }
    return json.dumps(payload)


@mcp.tool()
def mac_set_scorer(name: str | None) -> str:
    """Install a named scoring hook on the long-lived MAC Registry.

    :param name: Name registered in ``mac.scoring``. Pass ``null``/empty
        to clear the hook (reverts to SQL natural ordering). Unknown
        names raise ``ToolError`` so config errors surface immediately.
    """
    def _do() -> Any:
        registry = _long_registry()
        if not name:
            registry.set_scoring_fn(None)
        else:
            registry.set_scoring_fn(name)
        # Reflect the hook back so the caller can verify what is now live.
        info = {
            "name": name,
            "active_scorer_id": registry._scoring_fn_id,
            "async_installed": registry._async_scoring_fn is not None,
            "sync_installed": registry._scoring_fn is not None,
        }
        return json.dumps(info)
    return _safe_call(_do)


# ═══════════════════════════════════════════════════════════════════
# Phase E: Cross-IDE knowledge tools (vault + memory)
# ═══════════════════════════════════════════════════════════════════

_OBSIDIAN_API = "https://127.0.0.1:27124"
_OBSIDIAN_TOKEN: str | None = None


def _obsidian_headers() -> dict[str, str]:
    """Build Obsidian Local REST API auth headers."""
    global _OBSIDIAN_TOKEN
    if _OBSIDIAN_TOKEN is None:
        _OBSIDIAN_TOKEN = os.environ.get(
            "OBSIDIAN_API_TOKEN",
            # Fallback: try reading from Claude config
            "",
        )
    if not _OBSIDIAN_TOKEN:
        raise ToolError("obsidian: OBSIDIAN_API_TOKEN not set — configure it in your environment")
    return {
        "Authorization": f"Bearer {_OBSIDIAN_TOKEN}",
        "Content-Type": "application/json",
    }


@mcp.tool()
def mac_search_vault(
    query: str,
    limit: int = 10,
    type: str | None = None,
    path_prefix: str | None = None,
) -> str:
    """Search the Obsidian vault via the Local REST API.

    Returns matching notes with titles, paths, and snippet previews.
    Use this to find project context, past decisions, and technical
    notes — the same source of truth across all IDEs.

    :param query: Search terms (Obsidian search syntax supported).
    :param limit: Maximum number of results.
    :param type: Filter by note type — ``decision``, ``pitfall``, ``daily``,
        ``project``, ``inbox``, or ``None`` for unfiltered.  Implemented as
        a ``tag:`` prefix in the Obsidian query (e.g. ``tag:decision``).
    :param path_prefix: Only return notes under this vault-relative path
        (e.g. ``10-projects/`` or ``daily/``).  Implemented as a ``path:``
        prefix in the Obsidian query.

    Requires the Obsidian Local REST API plugin running on port 27124
    and OBSIDIAN_API_TOKEN set in your environment.
    """
    # Map type to Obsidian tag
    _TYPE_TAGS: dict[str, str] = {
        "decision": "tag:decision",
        "pitfall": "tag:pitfall",
        "daily": "tag:daily",
        "project": "tag:project",
        "inbox": "tag:inbox",
    }

    def _do() -> Any:
        import urllib.error
        import urllib.request

        # Build composite query with type and path filters
        parts: list[str] = []
        if type and type in _TYPE_TAGS:
            parts.append(_TYPE_TAGS[type])
        if path_prefix:
            parts.append(f"path:{path_prefix}")
        parts.append(query)
        composite_query = " ".join(parts)

        url = f"{_OBSIDIAN_API}/search/?query={urllib.parse.quote(composite_query)}&limit={int(limit)}"
        req = urllib.request.Request(url, headers=_obsidian_headers())
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise ToolError(f"obsidian_search: HTTP {e.code} — is the Obsidian REST API plugin running?")
        except urllib.error.URLError as e:
            raise ToolError(f"obsidian_search: connection failed ({e.reason}) — is Obsidian running?")
        except json.JSONDecodeError:
            raise ToolError("obsidian_search: unexpected response format from vault API")

    return _safe_call(_do)


@mcp.tool()
def mac_save_to_vault(
    content: str,
    path: str | None = None,
    privacy: str = "private",
    status: str = "draft",
) -> str:
    """Save a note to the Obsidian vault via the Local REST API.

    By default writes to ``00-inbox/`` with ``status: draft`` — the note
    must be reviewed by a human before being promoted to permanent
    knowledge zones (``10-projects/``, ``20-areas/``).  Use
    ``mac_promote_to_knowledge`` to move a reviewed note.

    :param content: Markdown content of the note.
    :param path: Vault-relative path.  Defaults to ``00-inbox/<slug>.md``
        where *slug* is derived from the first heading or a timestamp.
        Explicit paths like ``10-projects/my-project/api-design.md`` are
        allowed but should only be used for confirmed knowledge.
    :param privacy: Privacy marker — ``public``, ``private``, or ``company``.
        Will be added to the note's frontmatter.
    :param status: Lifecycle status — ``draft`` (default), ``reviewed``,
        or ``promoted``.  Draft notes live in ``00-inbox/`` until reviewed.

    Requires the Obsidian Local REST API plugin running on port 27124
    and OBSIDIAN_API_TOKEN set in your environment.
    """
    def _do() -> Any:
        import re as _re
        import urllib.error
        import urllib.request

        nonlocal content

        # Default path: 00-inbox/<slug>.md
        if not path:
            # Derive slug from first heading or timestamp
            heading_match = _re.search(r'^#\s+(.+)$', content, _re.MULTILINE)
            if heading_match:
                slug = heading_match.group(1).strip()
                slug = _re.sub(r'[\\/:*?"<>|]', '-', slug)
                slug = _re.sub(r'\s+', '-', slug).lower()[:60]
                slug = _re.sub(r'-+$', '', slug)
            else:
                from datetime import datetime, timezone
                slug = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            vault_path = f"00-inbox/{slug}.md"
        else:
            vault_path = path

        # Add frontmatter wrapper if not already present
        if not content.strip().startswith("---"):
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            content = (
                f"---\n"
                f"created: {now}\n"
                f"privacy: {privacy}\n"
                f"status: {status}\n"
                f"source: mac-mcp\n"
                f"---\n\n"
                f"{content}"
            )
        else:
            # Ensure status field exists in existing frontmatter
            if "status:" not in content.split("---")[1]:
                content = content.replace(
                    "---\n", f"---\nstatus: {status}\n", 1
                )

        url = f"{_OBSIDIAN_API}/vault/{urllib.parse.quote(vault_path, safe='')}"
        data = json.dumps({"content": content}).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers=_obsidian_headers(), method="PUT",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.dumps({"saved": vault_path, "status": resp.status})
        except urllib.error.HTTPError as e:
            raise ToolError(f"obsidian_save: HTTP {e.code} — {e.reason}")
        except urllib.error.URLError as e:
            raise ToolError(f"obsidian_save: connection failed ({e.reason}) — is Obsidian running?")

    return _safe_call(_do)


@mcp.tool()
def mac_promote_to_knowledge(
    source_path: str,
    target_path: str,
) -> str:
    """Promote a reviewed draft note to a permanent knowledge zone.

    Reads a note from ``00-inbox/`` (or elsewhere), writes it to the
    target path (e.g. ``10-projects/my-project/api-design.md`` or
    ``20-areas/20-programming/python-async-gotchas.md``), and updates
    its ``status`` frontmatter to ``promoted``.  The source note is
    then deleted.

    Use this after a human has reviewed a draft and confirmed it has
    long-term value.  Do NOT call this automatically — promotion
    requires human approval.

    :param source_path: Vault-relative path of the draft note
        (e.g. ``00-inbox/2026-08-10-api-design.md``).
    :param target_path: Vault-relative path for the promoted note
        (e.g. ``10-projects/my-project/api-design.md``).
    """
    def _do() -> Any:
        import urllib.error
        import urllib.request

        # 1. Read source note
        read_url = f"{_OBSIDIAN_API}/vault/{urllib.parse.quote(source_path, safe='')}"
        req = urllib.request.Request(read_url, headers=_obsidian_headers())
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                content = data.get("content", "")
        except urllib.error.HTTPError as e:
            raise ToolError(f"promote: source not found (HTTP {e.code}) — does {source_path} exist?") from e
        except urllib.error.URLError as e:
            raise ToolError(f"promote: connection failed ({e.reason}) — is Obsidian running?") from e

        # 2. Update status in frontmatter
        if content.strip().startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                fm = parts[1]
                import re as _re
                fm = _re.sub(r'status:\s*\w+', 'status: promoted', fm)
                content = f"---{fm}---{parts[2]}"

        # 3. Write to target path
        write_url = f"{_OBSIDIAN_API}/vault/{urllib.parse.quote(target_path, safe='')}"
        put_data = json.dumps({"content": content}).encode("utf-8")
        req = urllib.request.Request(
            write_url, data=put_data, headers=_obsidian_headers(), method="PUT",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                pass
        except urllib.error.HTTPError as e:
            raise ToolError(f"promote: write failed (HTTP {e.code}) — {e.reason}") from e
        except urllib.error.URLError as e:
            raise ToolError(f"promote: connection failed ({e.reason})") from e

        # 4. Delete source note
        del_url = f"{_OBSIDIAN_API}/vault/{urllib.parse.quote(source_path, safe='')}"
        req = urllib.request.Request(
            del_url, headers=_obsidian_headers(), method="DELETE",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                pass
        except urllib.error.HTTPError:
            # Non-fatal: target was written, source just wasn't cleaned up
            pass

        return json.dumps({
            "promoted": True,
            "from": source_path,
            "to": target_path,
            "status": "promoted",
        })

    return _safe_call(_do)


@mcp.tool()
def mac_remember(
    key: str,
    value: str,
    category: str = "general",
) -> str:
    """Store a key-value fact in the MAC coordination ledger.

    Use this to persist decisions, bug fixes, gotchas, and context that
    should survive across sessions. Unlike IDE-specific memory systems,
    facts stored via mac_remember are visible to ALL agents regardless
    of which IDE they run in (Claude Code, Codex, Trae, etc.).

    :param key: Unique identifier for this fact (e.g. ``fix-login-timeout``).
    :param value: The fact content (free text, can be multi-line).
    :param category: Tag for grouping (``general``, ``bug``, ``decision``,
        ``gotcha``, ``pattern``).
    """
    def _do() -> Any:
        reg = _registry()
        return reg.remember_fact(key, value, category)

    return _safe_call(_do)


@mcp.tool()
def mac_recall(query: str, limit: int = 10) -> str:
    """Recall facts previously stored via ``mac_remember``.

    Searches across keys, values, and categories. Returns the most
    relevant facts ranked by recency. Use this at session start to
    restore context — especially in Codex or Trae where
    Claude Code's agentmemory is unavailable.

    :param query: Search query (matches against key, value, category).
    :param limit: Maximum number of facts to return.
    """
    def _do() -> Any:
        reg = _registry()
        return reg.recall_facts(query, int(limit))

    return _safe_call(_do)


# ═══════════════════════════════════════════════════════════════════
# Phase B: Cross-IDE session context resource
# ═══════════════════════════════════════════════════════════════════

@mcp.resource("mac://session-context")
def session_context_resource() -> str:
    """Snapshot of current project state for cross-IDE session restore.

    Returns JSON with:
    - ``kanban``: four-color task board (red/yellow/green/done)
    - ``recent_facts``: last 10 remembered facts from the ledger
    - ``active_agents``: agents currently online
    - ``open_conflicts``: unresolved conflict count
    - ``metrics_summary``: cycle time, handoff rate, quality rate
    - ``daily_notes``: last 3 daily notes from Obsidian vault (if available)
    """
    reg = _registry()
    from mac.metrics import compute_metrics

    kanban = reg.get_kanban()
    recent_facts = reg.recall_facts("", 10)
    agents = reg.discover()
    online = [a.agent_id for a in agents if getattr(a, "status", "") == "online"]
    conflicts = reg.list_conflicts(resolved=False)
    metrics = compute_metrics(reg.ledger)

    # Fetch last 3 daily notes from Obsidian vault (graceful fallback)
    daily_notes: list[dict[str, str]] = []
    try:
        import urllib.error as _ue
        import urllib.request
        from datetime import datetime, timedelta, timezone

        headers = _obsidian_headers()
        today = datetime.now(timezone.utc)
        for i in range(3):
            d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            path = f"daily/{d}.md"
            url = f"{_OBSIDIAN_API}/vault/{urllib.parse.quote(path, safe='')}"
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode())
                    content = data.get("content", "")
                    # Truncate to 500 chars for context injection
                    if len(content) > 500:
                        content = content[:497] + "..."
                    daily_notes.append({"date": d, "content": content})
            except (_ue.HTTPError, _ue.URLError):
                continue  # Note doesn't exist or Obsidian not running
    except ToolError:
        pass  # OBSIDIAN_API_TOKEN not set — skip daily notes

    return json.dumps({
        "kanban": kanban,
        "recent_facts": recent_facts,
        "active_agents": online,
        "open_conflicts": len(conflicts),
        "metrics": {
            "cycle_time_s": metrics.get("task_cycle_time_seconds", 0),
            "handoff_rate": metrics.get("handoff_success_rate", 0),
            "quality_rate": metrics.get("quality_gate_pass_rate", 0),
            "retry_rate": metrics.get("retry_rate", 0),
            "conflict_rate": metrics.get("conflict_rate", 0),
        },
        "daily_notes": daily_notes,
    })


@mcp.tool()
def mac_test_scorer(
    name: str,
    limit: int = 5,
    project_context: str | None = None,
) -> str:
    """Dry-run a named scorer against the first ``limit`` proposed tasks.

    Sync scorers are invoked inline; async scorers are awaited via
    ``asyncio.run`` (the server is single-threaded under stdio so this
    is safe and simpler than the in-process loop case). Returns the
    computed ``task_id -> score`` map plus the proposed tasks that were
    inspected. Use this to validate a new scorer before swapping it in
    via ``mac_set_scorer``.
    """
    def _do() -> Any:
        # The test path uses a fresh per-call Registry so we do not
        # pollute the long-lived one with a stale cache or scoring hook.
        from mac.registry import Registry as _Registry
        from mac.storage.sqlite import SQLiteTaskLedger as _Ledger
        test_registry = _Registry(_Ledger(_DB_PATH), scoring_fn=name)
        tasks = test_registry.ledger.list_task_transfers(
            status="proposed", project_context=project_context
        )
        tasks = tasks[: max(0, int(limit))]
        scores: dict[str, float] = {}
        if test_registry._async_scoring_fn is not None:
            async def _gather():
                coros = [test_registry._async_scoring_fn(t) for t in tasks]
                raw = await asyncio.gather(*coros, return_exceptions=True)
                return [test_registry._to_async_score(r) for r in raw]
            scores_list = asyncio.run(_gather())
        elif test_registry._scoring_fn is not None:
            scores_list = [
                test_registry._to_async_score(test_registry._scoring_fn(t))
                for t in tasks
            ]
        else:
            # Scorer missing; map everything to 0.0 so the UI is still informative.
            scores_list = [0.0 for _ in tasks]
        for task, score in zip(tasks, scores_list, strict=True):
            scores[task.task_id] = score
        payload = {
            "scorer": name,
            "scored": [
                {"task_id": t.task_id, "score": scores.get(t.task_id, 0.0), "priority": t.priority}
                for t in tasks
            ],
        }
        return json.dumps(payload)
    return _safe_call(_do)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the MCP server over stdio transport."""
    resolved = _resolve_db_path()
    env_val = os.environ.get("MAC_DB_PATH", "<unset>")
    logger.info("mac-mcp-server DB: %s (MAC_DB_PATH=%s)", resolved, env_val)
    # Also print to stderr so it's visible in Claude Code's MCP server log
    # even when logging isn't configured.
    print(f"[mac-mcp-server] DB path: {resolved}", file=sys.stderr)
    mcp.run()
