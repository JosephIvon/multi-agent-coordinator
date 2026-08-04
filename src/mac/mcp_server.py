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
# Tools (15 + 3 scoring = 18)
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
) -> str:
    """Claim the next available proposed task and start it.

    Atomically: claim_next_task (claim + accept) → start_task.

    :param agent_id: ID of the claiming agent.
    :param capability: Required capability to match.
    :param project_context: Optional project filter.
    :param best_effort: If True, consider tasks with other capabilities.
    :returns: JSON of the claimed-and-started TaskTransfer, or not_found.
    """

    def _do() -> Any:
        reg = _registry()
        claimed = reg.claim_next_task(
            agent_id=agent_id,
            capability=capability,
            project_context=project_context,
            best_effort=best_effort,
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
        return reg.done(
            task_id,
            agent_id,
            quality_result=quality_result,
            handoff=handoff,
        )

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
) -> str:
    """List dependency-unblocked proposed tasks ready for claiming.

    :param capability: Optional capability filter.
    :param project_context: Optional project filter.
    :returns: JSON array of ready TaskTransfer objects.
    """

    def _do() -> Any:
        return _registry().list_ready_tasks(capability=capability, project_context=project_context)

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
) -> str:
    """Atomically claim, start, and generate a worker packet for the next ready task.

    One-shot convenience: claim_next_task → start_task → prepare_worker_packet.

    :param agent_id: ID of the claiming agent.
    :param capability: Required capability to match.
    :param project_context: Optional project filter.
    :param best_effort: If True, consider tasks with other capabilities.
    :returns: Markdown worker packet string, or not_found if no task available.
    """

    def _do() -> Any:
        reg = _registry()
        claimed = reg.claim_next_task(
            agent_id=agent_id,
            capability=capability,
            project_context=project_context,
            best_effort=best_effort,
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


# ---------------------------------------------------------------------------
# Resources (2)
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
