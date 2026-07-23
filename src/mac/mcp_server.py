from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from mac.protocol.errors import QualityGateError, StateConflictError
from mac.protocol.messages import TaskTransfer
from mac.quality.gate import evaluate_quality_gate
from mac.registry import Registry
from mac.storage.sqlite import SQLiteTaskLedger

mcp = FastMCP("mac-coordinator")

_DB_PATH = Path("mac.db")


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
# Tools (15)
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
            completed = reg.complete_task(task_id, agent_id)
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
            boundary_review=boundary_review,
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the MCP server over stdio transport."""
    mcp.run()
