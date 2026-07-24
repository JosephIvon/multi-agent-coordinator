from __future__ import annotations

import fnmatch
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from mac.events import TaskEvent, TaskEventBus
from mac.protocol.errors import QualityGateError, StateConflictError
from mac.protocol.messages import (
    AuditEntry,
    ConflictRecord,
    CoordinationPolicy,
    HandoffResult,
    PathRule,
    Plan,
    QualityGatePreview,
    TaskEvidenceBundle,
    TaskReadinessReport,
    TaskTransfer,
)
from mac.quality.gate import evaluate_quality_gate
from mac.storage import SQLiteTaskLedger, StatusConflict
from mac.testing.contracts import TestContract


class Registry:
    def __init__(
        self,
        ledger: SQLiteTaskLedger,
        *,
        event_bus: TaskEventBus | None = None,
        policy: CoordinationPolicy | None = None,
    ) -> None:
        self.ledger = ledger
        self.event_bus = event_bus
        # Default to environment-driven policy so existing call sites
        # pick up MAC_REQUIRE_REVIEW / MAC_PATH_RULES without code changes.
        self.policy: CoordinationPolicy = policy or CoordinationPolicy.from_env()

    def register_agent(self, agent: Any) -> None:
        self.ledger.save_agent_card(agent)

    def register(self, agent: Any) -> None:
        self.register_agent(agent)

    def get_agent(self, agent_id: str) -> Any | None:
        return self.ledger.get_agent_card(agent_id)

    def heartbeat_agent(self, agent_id: str, *, status: str = "online", load: int | None = None) -> Any:
        agent = self.get_agent(agent_id)
        if agent is None:
            raise KeyError(agent_id)
        updates: dict[str, Any] = {"status": status, "last_heartbeat": time.time()}
        if load is not None:
            updates["load"] = load
        refreshed = agent.model_copy(update=updates) if hasattr(agent, "model_copy") else replace(agent, **updates)
        self.ledger.save_agent_card(refreshed)
        return refreshed

    def discover(
        self,
        capability: str | None = None,
        *,
        status: str | None = "online",
        max_load: int | None = None,
        project_context: str | None = None,
    ) -> list[Any]:
        agents = self.ledger.list_agent_cards(
            capability=capability,
            status=status,
            max_load=max_load,
            project_context=project_context,
        )
        if capability is None:
            return [_with_selection_metadata(agent) for agent in agents]

        ranked = []
        for agent in agents:
            score = self.get_capability_score(agent.agent_id, capability)
            ranked.append((agent, score))
        ranked.sort(
            key=lambda item: (
                float(item[1].get("success_rate", 0.0)),
                int(item[1].get("total", 0)),
                -int(getattr(item[0], "load", 0)),
            ),
            reverse=True,
        )
        return [_with_selection_metadata(agent, capability_score=score) for agent, score in ranked]

    def submit_task(self, task: TaskTransfer) -> TaskTransfer:
        if task.plan_id and self.ledger.get_plan(task.plan_id) is None:
            raise KeyError(task.plan_id)
        self._assert_no_cycle(task)
        self.ledger.save_task_transfer(task)
        if task.plan_id:
            self._add_task_to_plan(task.plan_id, task.task_id)
        actor = task.source_agent_id or ""
        self._audit(task, "submit_task", actor)
        self._publish(task, "task_submitted", actor=actor, to_status=task.status)
        return task

    def get_task(self, task_id: str) -> TaskTransfer | None:
        return self.ledger.get_task_transfer(task_id)

    def list_tasks(
        self,
        *,
        status: str | None = None,
        capability: str | None = None,
        agent_id: str | None = None,
        project_context: str | None = None,
    ) -> list[TaskTransfer]:
        tasks = self.ledger.list_task_transfers(status=status, project_context=project_context)
        filtered = []
        for task in tasks:
            if capability is not None and _required_capability(task) != capability:
                continue
            if agent_id is not None and task.target_agent_id is not None and task.target_agent_id != agent_id:
                continue
            filtered.append(task)
        return filtered

    def get_task_evidence(self, task_id: str) -> TaskEvidenceBundle | None:
        task = self.get_task(task_id)
        if task is None:
            return None

        required_capability = _required_capability(task)
        execution_agent_id = task.target_agent_id
        observed_capability_score = (
            self.get_capability_score(execution_agent_id, required_capability)
            if execution_agent_id is not None and required_capability is not None
            else None
        )
        return TaskEvidenceBundle(
            task_id=task.task_id,
            trace_id=task.trace_id,
            task=task,
            quality_results=self.ledger.get_quality_results(task.task_id),
            audit_trail=self.get_audit_trail(task.trace_id),
            execution_agent_id=execution_agent_id,
            required_capability=required_capability,
            observed_capability_score=observed_capability_score,
            handoff_result=self.get_handoff_result(task.task_id),
        )

    def preview_quality_gate(self, task_id: str) -> QualityGatePreview | None:
        task = self.get_task(task_id)
        if task is None:
            return None
        return _preview_quality_gate(task, _current_attempt_quality_results(task, self.ledger.get_quality_results(task_id)))

    def preview_task_readiness(self, task_id: str) -> TaskReadinessReport | None:
        task = self.get_task(task_id)
        if task is None:
            return None

        audit_trail = self.get_audit_trail(task.trace_id)
        quality_preview = None
        if task.status == "running":
            quality_preview = _preview_quality_gate(task, _current_attempt_quality_results(task, self.ledger.get_quality_results(task_id)))
        next_action, blocking_reason = _readiness_decision(task, quality_preview)

        return TaskReadinessReport(
            task_id=task.task_id,
            trace_id=task.trace_id,
            status=task.status,
            execution_agent_id=task.target_agent_id,
            required_capability=_required_capability(task),
            next_action=next_action,
            blocking_reason=blocking_reason,
            quality_allowed=quality_preview.allowed if quality_preview is not None else None,
            missing_commands=quality_preview.missing_commands if quality_preview is not None else [],
            missing_evidence=quality_preview.missing_evidence if quality_preview is not None else [],
            quality_results_count=quality_preview.quality_results_count if quality_preview is not None else 0,
            audit_event_count=len(audit_trail),
        )

    def accept_handoff(self, task_id: str, agent_id: str) -> TaskTransfer:
        return self._transition(task_id, "accepted", expected_status="proposed", agent_id=agent_id, action="accept_handoff")

    def start_task(self, task_id: str, agent_id: str) -> TaskTransfer:
        return self._transition(task_id, "running", expected_status="accepted", agent_id=agent_id, action="start_task")

    def reject_handoff(self, task_id: str, agent_id: str, reason: str) -> TaskTransfer:
        return self._transition(
            task_id,
            "rejected",
            expected_status="proposed",
            agent_id=agent_id,
            action="reject_handoff",
            message=reason,
        )

    def fail_task(self, task_id: str, agent_id: str, error_code: str, message: str = "") -> TaskTransfer:
        task = self._get_task(task_id)
        task.error_code = error_code
        task.status = "failed"
        task.updated_at = _now_id()
        self.ledger.save_task_transfer(task)
        self._audit(task, "fail_task", agent_id, message=message, from_status=None, to_status="failed")
        self._publish(task, "task_failed", actor=agent_id, to_status="failed", payload={"error_code": error_code})
        return task

    def expire_stale_tasks(
        self, *, now: float | None = None, auto_retry: bool = False
    ) -> list[TaskTransfer]:
        """Transition non-terminal tasks past their TTL.

        Scans tasks in ``running`` / ``review_ready`` / ``accepted`` states
        whose ``updated_at`` (or ``created_at`` fallback) + ``ttl_seconds``
        precedes ``now``.

        When ``auto_retry=True`` and the task has retries remaining
        (``retry_count < policy.max_retry_count``), the task is reset to
        ``proposed`` with an incremented retry count instead of being failed.
        Otherwise the task is failed with ``error_code='TTL_EXPIRED'``.

        MAC does not run a background thread; callers (CLI, cron) invoke this
        periodically. Returns the list of expired/retried tasks.
        """
        checkpoint = now if now is not None else time.time()
        candidates: list[TaskTransfer] = []
        for status in ("running", "review_ready", "accepted"):
            candidates.extend(self.ledger.list_task_transfers(status=status))
        expired: list[TaskTransfer] = []
        for task in candidates:
            timestamp_str = task.updated_at or task.created_at
            updated = _parse_iso_to_epoch(timestamp_str)
            if updated is None:
                continue
            if (checkpoint - updated) > task.ttl_seconds:
                if auto_retry and task.retry_count < self.policy.max_retry_count:
                    # Reset to proposed with incremented retry count.
                    task.retry_count += 1
                    task.error_code = None
                    task.target_agent_id = task.fallback_agent_id
                    task.updated_at = _now_id()
                    previous_status = task.status
                    task.status = "proposed"
                    self.ledger.save_task_transfer(task)
                    self._audit(
                        task, "retry_task", "system",
                        from_status=previous_status, to_status="proposed",
                        message=f"Auto-retry: TTL expired ({task.ttl_seconds}s)",
                    )
                    self._publish(
                        task, "task_retried", actor="system",
                        from_status=previous_status, to_status="proposed",
                        payload={"retry_count": task.retry_count, "trigger": "ttl_expiry"},
                    )
                    expired.append(task)
                else:
                    failed = self.fail_task(
                        task.task_id,
                        agent_id="system",
                        error_code="TTL_EXPIRED",
                        message=f"Task exceeded TTL of {task.ttl_seconds}s in state {task.status!r}.",
                    )
                    expired.append(failed)
        return expired

    def expire_stale_agents(
        self, *, timeout_seconds: int | None = None, now: float | None = None
    ) -> list[Any]:
        """Set agents offline if their last heartbeat is older than the timeout.

        Agents with status != ``online`` are skipped. Returns the list of
        agents that were expired (set to ``offline``).

        The timeout defaults to ``policy.agent_timeout`` (300s).
        """
        timeout = timeout_seconds if timeout_seconds is not None else self.policy.agent_timeout
        checkpoint = now if now is not None else time.time()
        agents = self.ledger.list_agent_cards()
        expired: list[Any] = []
        for agent in agents:
            if agent.status != "online":
                continue
            # last_heartbeat == 0 means never heartbeated; skip (agent just registered)
            if agent.last_heartbeat == 0:
                continue
            if (checkpoint - agent.last_heartbeat) > timeout:
                self.heartbeat_agent(agent.agent_id, status="offline")
                expired.append(agent)
        return expired

    def record_checkpoint(self, task_id: str, *, agent_id: str, checkpoint: dict[str, Any]) -> TaskTransfer:
        task = self._get_task(task_id)
        if task.status in {"completed", "cancelled"}:
            raise StateConflictError(f"Task {task_id!r} status is {task.status!r}; cannot checkpoint.")
        checkpoint_data = dict(checkpoint)
        checkpoint_data.setdefault("agent_id", agent_id)
        checkpoint_data.setdefault("created_at", _now_id())
        metadata = dict(task.metadata or {})
        checkpoints = list(metadata.get("checkpoints", []))
        checkpoints.append(checkpoint_data)
        metadata["checkpoints"] = checkpoints
        task.metadata = metadata
        task.updated_at = _now_id()
        self.ledger.save_task_transfer(task)
        self._audit(task, "checkpoint_task", agent_id)
        self._publish(task, "task_checkpointed", actor=agent_id, payload={"checkpoint": checkpoint_data})
        return task

    def retry_task(self, task_id: str, *, agent_id: str, fallback_agent_id: str | None = None) -> TaskTransfer:
        task = self._get_task(task_id)
        if task.status != "failed":
            raise StateConflictError(f"Task {task_id!r} status is {task.status!r}, expected 'failed'.")
        previous_status = task.status
        selected_fallback = fallback_agent_id or task.fallback_agent_id
        task.status = "proposed"
        task.retry_count += 1
        task.error_code = None
        task.target_agent_id = selected_fallback
        task.fallback_agent_id = selected_fallback
        task.updated_at = _now_id()
        self.ledger.save_task_transfer(task)
        self._audit(task, "retry_task", agent_id, from_status=previous_status, to_status="proposed")
        self._publish(
            task,
            "task_retried",
            actor=agent_id,
            from_status=previous_status,
            to_status="proposed",
            payload={"fallback_agent_id": selected_fallback, "retry_count": task.retry_count},
        )
        return task

    def cancel_task(self, task_id: str, *, agent_id: str, reason: str = "") -> TaskTransfer:
        task = self._get_task(task_id)
        if task.status in {"completed", "cancelled"}:
            raise StateConflictError(f"Task {task_id!r} status is {task.status!r}; cannot cancel.")
        previous_status = task.status
        metadata = dict(task.metadata or {})
        if reason:
            metadata["cancel_reason"] = reason
        task.metadata = metadata
        task.status = "cancelled"
        task.error_code = "TASK_CANCELLED"
        task.updated_at = _now_id()
        self.ledger.save_task_transfer(task)
        self._audit(task, "cancel_task", agent_id, message=reason, from_status=previous_status, to_status="cancelled")
        self._publish(
            task,
            "task_cancelled",
            actor=agent_id,
            from_status=previous_status,
            to_status="cancelled",
            payload={"reason": reason},
        )
        return task

    def submit_quality_result(self, task_id: str, result: dict[str, Any]) -> None:
        task = self._get_task(task_id)
        stamped_result = dict(result)
        stamped_result["retry_count"] = task.retry_count
        self.ledger.record_quality_result(task_id, stamped_result)
        actor = str(stamped_result.get("agent_id") or stamped_result.get("actor") or "quality")
        self._audit(task, "submit_quality_result", actor)
        self._publish(task, "quality_result_submitted", actor=actor, payload={"result": stamped_result})

    def complete_task(self, task_id: str, agent_id: str) -> TaskTransfer:
        task = self._get_task(task_id)
        if self.policy.require_review and task.status == "running":
            raise StateConflictError(
                f"Task {task_id!r} requires review (require_review=True). "
                "Use mark_review_ready() instead of complete_task()."
            )
        allowed, reason = evaluate_quality_gate(
            task.test_contract,
            _current_attempt_quality_results(task, self.ledger.get_quality_results(task_id)),
        )
        if not allowed:
            raise QualityGateError(reason or "quality_gate_failed")
        return self._transition(task_id, "completed", expected_status="running", agent_id=agent_id, action="complete_task")

    def done(
        self,
        task_id: str,
        agent_id: str,
        *,
        quality_result: dict[str, Any] | None = None,
        handoff: HandoffResult | None = None,
    ) -> dict[str, Any]:
        """Finish a task in one step: submit quality evidence, save handoff, and complete (or mark review-ready).

        Automatically detects whether to complete or mark review-ready based on
        ``CoordinationPolicy.require_review``.  This is the single entry point
        for finishing a task — callers no longer need to know the state machine.

        :param task_id: ID of the running task.
        :param agent_id: ID of the agent finishing the task.
        :param quality_result: Optional quality evidence dict (must include
            ``command`` and ``status``).  If ``None``, any previously submitted
            quality results are used for gate evaluation.
        :param handoff: Optional :class:`HandoffResult` to save before
            completing / marking review-ready.
        :returns: A summary dict with keys ``status``, ``task_id``,
            ``quality_gate``, and optionally ``review`` and ``reason``.
        """
        # 1. Submit quality evidence if provided.
        if quality_result is not None:
            self.submit_quality_result(task_id, quality_result)

        # 2. Evaluate quality gate against current-attempt results.
        task = self._get_task(task_id)
        quality_results = _current_attempt_quality_results(
            task, self.ledger.get_quality_results(task_id),
        )
        allowed, reason = evaluate_quality_gate(task.test_contract, quality_results)

        if not allowed:
            return {
                "status": "running",
                "task_id": task_id,
                "quality_gate": "failed",
                "reason": reason,
            }

        # 3. Branch on require_review.
        if self.policy.require_review:
            # mark_review_ready() saves handoff internally when provided.
            self.mark_review_ready(task_id, agent_id, handoff=handoff)
            return {
                "status": "review_ready",
                "task_id": task_id,
                "quality_gate": "passed",
                "review": True,
            }

        # No review required: save handoff first, then complete.
        if handoff is not None:
            self.save_handoff_result(handoff)
        self.complete_task(task_id, agent_id)
        return {
            "status": "completed",
            "task_id": task_id,
            "quality_gate": "passed",
            "review": False,
        }

    # ------------------------------------------------------------------
    # Reviewer capability guard (B-5)
    # ------------------------------------------------------------------

    def _assert_reviewer_capability(self, reviewer_id: str) -> None:
        """Raise ``StateConflictError`` if the reviewer lacks the required capability.

        No-op when ``policy.reviewer_capability`` is not configured.
        """
        cap_name = self.policy.reviewer_capability
        if not cap_name:
            return
        agent = self.ledger.get_agent_card(reviewer_id)
        if agent is None or not any(
            c.name == cap_name for c in agent.capabilities
        ):
            raise StateConflictError(
                f"Agent {reviewer_id!r} lacks capability "
                f"{cap_name!r} required for review."
            )

    def mark_review_ready(
        self,
        task_id: str,
        agent_id: str,
        handoff: HandoffResult | None = None,
    ) -> TaskTransfer:
        """Move a running task to ``review_ready`` and optionally save handoff.

        Only valid when ``CoordinationPolicy.require_review=True``.
        When ``require_review=False``, use ``complete_task()`` directly.
        """
        if not self.policy.require_review:
            raise StateConflictError(
                f"Task {task_id!r} cannot enter review_ready: "
                "require_review is False. Use complete_task() instead."
            )
        task = self._get_task(task_id)
        if task.status != "running":
            raise StateConflictError(
                f"Task {task_id!r} status is {task.status!r}, expected 'running'."
            )
        if handoff is not None:
            self.save_handoff_result(handoff)
        return self._transition(
            task_id, "review_ready", expected_status="running",
            agent_id=agent_id, action="mark_review_ready",
        )

    def accept_review(self, task_id: str, reviewer_id: str) -> TaskTransfer:
        """Accept a ``review_ready`` task, transitioning to ``completed``."""
        self._assert_reviewer_capability(reviewer_id)
        task = self._get_task(task_id)
        if task.status != "review_ready":
            raise StateConflictError(
                f"Task {task_id!r} status is {task.status!r}, expected 'review_ready'."
            )
        return self._transition(
            task_id, "completed", expected_status="review_ready",
            agent_id=reviewer_id, action="accept_review",
        )

    def reject_review(
        self,
        task_id: str,
        reviewer_id: str,
        reason: str = "",
    ) -> TaskTransfer:
        """Reject a ``review_ready`` task, transitioning to ``rejected``.

        The rejection reason is automatically recorded as a conflict.
        """
        self._assert_reviewer_capability(reviewer_id)
        task = self._get_task(task_id)
        if task.status != "review_ready":
            raise StateConflictError(
                f"Task {task_id!r} status is {task.status!r}, expected 'review_ready'."
            )
        self.record_conflict(
            ConflictRecord(
                plan_id=task.plan_id,
                task_id=task_id,
                source="reject_review",
                severity="blocking",
                description=reason or f"Task {task_id!r} rejected by {reviewer_id}",
                involved_agents=[reviewer_id],
            )
        )
        return self._transition(
            task_id, "rejected", expected_status="review_ready",
            agent_id=reviewer_id, action="reject_review",
            message=reason,
        )

    def create_plan(
        self,
        *,
        goal: str,
        created_by: str = "",
        plan_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Plan:
        if plan_id is not None and self.ledger.get_plan(plan_id) is not None:
            raise StateConflictError(f"Plan {plan_id!r} already exists.")
        plan = Plan(plan_id=plan_id or str(uuid4()), goal=goal, created_by=created_by, metadata=metadata or {})
        self.ledger.save_plan(plan)
        self._publish_plan(plan, "plan_created", actor=created_by)
        return plan

    def get_plan(self, plan_id: str) -> Plan | None:
        return self.ledger.get_plan(plan_id)

    def list_plans(self, *, status: str | None = None) -> list[Plan]:
        return self.ledger.list_plans(status=status)

    def activate_plan(self, plan_id: str) -> Plan:
        plan = self._get_plan(plan_id)
        if plan.status != "draft":
            raise StateConflictError(f"Plan {plan_id!r} status is {plan.status!r}, expected 'draft'.")
        updated = plan.model_copy(update={"status": "active"})
        self.ledger.save_plan(updated)
        self._publish_plan(updated, "plan_activated")
        return updated

    def close_plan(self, plan_id: str, *, status: str = "completed") -> Plan:
        if status not in {"completed", "cancelled"}:
            raise ValueError("close_plan status must be 'completed' or 'cancelled'")
        plan = self._get_plan(plan_id)
        if plan.status not in {"draft", "active"}:
            raise StateConflictError(f"Plan {plan_id!r} status is {plan.status!r}; cannot close.")
        updated = plan.model_copy(
            update={
                "status": status,
                "closed_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self.ledger.save_plan(updated)
        self._publish_plan(updated, "plan_closed", to_status=status)
        return updated

    def list_ready_tasks(
        self,
        *,
        agent_id: str | None = None,
        capability: str | None = None,
        project_context: str | None = None,
    ) -> list[TaskTransfer]:
        tasks = self.ledger.list_task_transfers(status="proposed", project_context=project_context)
        ready = []
        for task in tasks:
            if agent_id is not None and task.target_agent_id is not None and task.target_agent_id != agent_id:
                continue
            if capability is not None and _required_capability(task) != capability:
                continue
            if not self._dependencies_satisfied(task):
                continue
            ready.append(task)
        return ready

    def save_handoff_result(
        self,
        handoff: HandoffResult,
        *,
        path_rule: PathRule | None = None,
    ) -> HandoffResult:
        task = self._get_task(handoff.task_id)
        if handoff.plan_id is None and task.plan_id is not None:
            handoff = handoff.model_copy(update={"plan_id": task.plan_id})
        normalized = self._apply_path_guardrails(handoff, path_rule or PathRule())
        self.ledger.save_handoff_result(normalized)
        self._audit(task, "save_handoff_result", normalized.agent_id)
        self._publish(task, "handoff_saved", actor=normalized.agent_id, payload={"boundary_review": normalized.boundary_review})
        return normalized

    def get_handoff_result(self, task_id: str) -> HandoffResult | None:
        return self.ledger.get_handoff_result(task_id)

    def record_conflict(self, conflict: ConflictRecord) -> ConflictRecord:
        recorded = self.ledger.record_conflict(conflict)
        self._publish_conflict(recorded, "conflict_recorded")
        return recorded

    def list_conflicts(
        self,
        *,
        plan_id: str | None = None,
        resolved: bool | None = None,
    ) -> list[ConflictRecord]:
        return self.ledger.list_conflicts(plan_id=plan_id, resolved=resolved)

    def cleanup_tasks(
        self,
        *,
        statuses: list[str] | None = None,
        plan_id: str | None = None,
        older_than_seconds: float | None = None,
    ) -> list[TaskTransfer]:
        """Delete terminal tasks (failed/cancelled/rejected/superseded) from the ledger.

        :param statuses: Task statuses to clean up. Defaults to
            ``["failed", "cancelled", "rejected", "superseded"]``.
        :param plan_id: Only clean tasks belonging to this plan.
        :param older_than_seconds: Only clean tasks whose ``updated_at``
            is older than this many seconds. ``None`` means no age filter.
        :returns: List of deleted TaskTransfer objects.
        """
        if statuses is None:
            statuses = ["failed", "cancelled", "rejected", "superseded"]
        candidates: list[TaskTransfer] = []
        for status in statuses:
            candidates.extend(self.ledger.list_task_transfers(status=status))
        now = time.time()
        deleted: list[TaskTransfer] = []
        for task in candidates:
            if plan_id is not None and task.plan_id != plan_id:
                continue
            if older_than_seconds is not None:
                updated = _parse_iso_to_epoch(task.updated_at or task.created_at)
                if updated is not None and (now - updated) < older_than_seconds:
                    continue
            self.ledger.delete_task_transfer(task.task_id)
            self._audit(task, "cleanup_task", "system", from_status=task.status, to_status="deleted")
            self._publish(task, "task_cleaned_up", actor="system", from_status=task.status, to_status="deleted")
            deleted.append(task)
        return deleted

    def resolve_conflict(self, conflict_id: str, resolution: str) -> ConflictRecord:
        resolved = self.ledger.resolve_conflict(conflict_id, resolution)
        self._publish_conflict(resolved, "conflict_resolved")
        return resolved

    def prepare_worker_packet(self, task_id: str, *, agent_id: str | None = None) -> str:
        task = self._get_task(task_id)
        agent = self.get_agent(agent_id) if agent_id else None
        dependencies = self._dependency_lines(task)
        lines = [
            f"# Worker Task: {task.task_id}",
            "",
            "## Goal",
            task.context.summary if task.context is not None else task.title or task.description,
            "",
            "## Task",
            f"- Status: {task.status}",
            f"- Capability: {_required_capability(task)}",
            f"- Priority: {task.priority}",
        ]
        if task.plan_id:
            lines.append(f"- Plan: {task.plan_id}")
        if agent is not None:
            lines.extend(
                [
                    "",
                    "## Agent Boundary",
                    f"- Agent: {agent.agent_id}",
                    f"- Allowed paths: {_format_list(agent.allowed_paths)}",
                    f"- Forbidden paths: {_format_list(agent.forbidden_paths)}",
                ]
            )
        lines.extend(["", "## Depends On"])
        lines.extend(dependencies or ["- None"])
        # Inline upstream handoff summaries for completed dependencies.
        for dep_id in task.depends_on:
            dep = self.ledger.get_task_transfer(dep_id)
            if dep is not None and dep.status == "completed":
                dep_handoff = self.get_handoff_result(dep_id)
                if dep_handoff is not None:
                    lines.extend([
                        "",
                        f"### Upstream Handoff: {dep_id}",
                        f"- Agent: {dep_handoff.agent_id}",
                        f"- Changed files: {_format_list(dep_handoff.changed_files)}",
                        f"- Risks: {_format_list(dep_handoff.risks)}",
                    ])
        lines.extend(
            [
                "",
                "## Acceptance Criteria",
            ]
        )
        criteria = list(getattr(task.context, "acceptance_criteria", []) or [])
        lines.extend([f"- {item}" for item in criteria] or ["- Complete the task and submit structured handoff evidence."])
        spec = (task.metadata or {}).get("spec")
        if spec is not None:
            lines.extend(
                [
                    "",
                    "## Structured Spec",
                ]
            )
            if isinstance(spec, dict):
                for key, value in spec.items():
                    lines.append(f"- **{key}**: {value}")
            else:
                lines.append(str(spec))
        lines.extend(
            [
                "",
                "## Handoff Format",
                "- verification: command/result/description",
                "- changed_files: files changed by this task",
                "- docs_touched: docs updated by this task",
                "- risks: residual risks or follow-up checks",
            ]
        )
        return "\n".join(lines).strip() + "\n"

    def prepare_review_packet(self, task_id: str) -> str:
        task = self._get_task(task_id)
        handoff = self.get_handoff_result(task_id)
        conflicts = self.list_conflicts(plan_id=task.plan_id, resolved=False) if task.plan_id else []
        task_conflicts = [conflict for conflict in conflicts if conflict.task_id in {None, task_id}]
        lines = [
            f"# Review Task: {task.task_id}",
            "",
            "## Task",
            f"- Status: {task.status}",
            f"- Capability: {_required_capability(task)}",
        ]
        if handoff is None:
            lines.extend(["", "## HandoffResult", "- None"])
        else:
            lines.extend(
                [
                    "",
                    "## HandoffResult",
                    f"- Agent: {handoff.agent_id}",
                    f"- Boundary review: {handoff.boundary_review}",
                    f"- Changed files: {_format_list(handoff.changed_files)}",
                    f"- Docs touched: {_format_list(handoff.docs_touched)}",
                    f"- Risks: {_format_list(handoff.risks)}",
                    "",
                    "## Verification",
                ]
            )
            lines.extend(
                [f"- `{entry.command}`: {entry.result} {entry.description}".rstrip() for entry in handoff.verification]
                or ["- None"]
            )
        quality_results = self.ledger.get_quality_results(task_id)
        if quality_results:
            lines.extend(["", "## Quality Evidence"])
            for qr in quality_results:
                status = qr.get("status", "unknown")
                command = qr.get("command", "unknown")
                evidence = qr.get("evidence", [])
                lines.append(f"- `{command}`: {status}")
                if evidence:
                    lines.append(f"  evidence: {', '.join(str(e) for e in evidence)}")
        lines.extend(["", "## Open Conflicts"])
        lines.extend([f"- {conflict.conflict_id}: {conflict.description}" for conflict in task_conflicts] or ["- None"])
        return "\n".join(lines).strip() + "\n"

    def claim_next_task(
        self,
        *,
        agent_id: str,
        capability: str,
        project_context: str | None = None,
        best_effort: bool = False,
    ) -> TaskTransfer | None:
        tasks = self.ledger.list_task_transfers(status="proposed", project_context=project_context)
        candidates = []
        for index, task in enumerate(tasks):
            if task.target_agent_id is not None and task.target_agent_id != agent_id:
                continue
            if not self._dependencies_satisfied(task):
                continue
            required_capability = _required_capability(task)
            if not best_effort and required_capability != capability:
                continue
            score = self.get_capability_score(agent_id, required_capability) if best_effort else None
            candidates.append((task, index, score or {}))

        if best_effort:
            candidates.sort(
                key=lambda item: (
                    float(item[2].get("success_rate", 0.0)),
                    int(getattr(item[0], "priority", 5)),
                    -item[1],
                ),
                reverse=True,
            )

        for task, _index, _score in candidates:
            try:
                claimed = self._transition(
                    task.task_id,
                    "accepted",
                    expected_status="proposed",
                    agent_id=agent_id,
                    action="claim_task",
                    emit_event=False,
                )
            except StateConflictError:
                continue
            claimed.target_agent_id = agent_id
            claimed.updated_at = _now_id()
            self.ledger.save_task_transfer(claimed)
            self._publish(
                claimed,
                "task_claimed",
                actor=agent_id,
                from_status="proposed",
                to_status="accepted",
                payload={"target_agent_id": agent_id},
            )
            return claimed
        return None

    def get_audit_trail(self, trace_id: str) -> list[AuditEntry]:
        return self.ledger.get_audit_trail(trace_id)

    def record_task_outcome(
        self,
        *,
        agent_id: str,
        capability: str,
        task_type: str,
        status: str,
        duration_seconds: float,
        error_code: str | None = None,
    ) -> None:
        self.ledger.record_task_outcome(
            agent_id=agent_id,
            capability=capability,
            task_type=task_type,
            status=status,
            duration_seconds=duration_seconds,
            error_code=error_code,
        )

    def get_capability_score(self, agent_id: str, capability: str) -> dict[str, Any]:
        return self.ledger.get_capability_score(agent_id, capability)

    def _transition(
        self,
        task_id: str,
        status: str,
        *,
        expected_status: str,
        agent_id: str,
        action: str,
        message: str = "",
        emit_event: bool = True,
    ) -> TaskTransfer:
        try:
            task = self.ledger.update_task_status(
                task_id,
                status,
                expected_status=expected_status,
                actor=agent_id,
            )
        except StatusConflict as exc:
            raise StateConflictError(str(exc)) from exc
        self._audit(task, action, agent_id, message=message, from_status=expected_status, to_status=status)
        if emit_event:
            self._publish(
                task,
                _event_type_for_action(action),
                actor=agent_id,
                from_status=expected_status,
                to_status=status,
            )
        return task

    def _get_task(self, task_id: str) -> TaskTransfer:
        task = self.ledger.get_task_transfer(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def _get_plan(self, plan_id: str) -> Plan:
        plan = self.ledger.get_plan(plan_id)
        if plan is None:
            raise KeyError(plan_id)
        return plan

    def _add_task_to_plan(self, plan_id: str, task_id: str) -> None:
        plan = self.ledger.get_plan(plan_id)
        if plan is None:
            return
        if task_id in plan.task_ids:
            return
        updated = plan.model_copy(update={"task_ids": [*plan.task_ids, task_id]})
        self.ledger.save_plan(updated)

    def _dependencies_satisfied(self, task: TaskTransfer) -> bool:
        for dependency_id in task.depends_on:
            dependency = self.ledger.get_task_transfer(dependency_id)
            if dependency is None or dependency.status not in {"completed", "cancelled"}:
                return False
        return True

    def _assert_no_cycle(self, task: TaskTransfer) -> None:
        """Reject submit_task when depends_on chains reach task.task_id.

        Walks the existing dependency graph from each declared dependency. If any
        path leads back to ``task.task_id`` (self-loop or indirect cycle),
        raises ``StateConflictError`` so the row is never persisted.
        """
        if task.task_id in task.depends_on:
            raise StateConflictError(
                f"circular_dependency: task {task.task_id!r} lists itself in depends_on"
            )

        target = task.task_id
        visited: set[str] = set()

        def walk(node: str) -> bool:
            if node == target:
                return True
            if node in visited:
                return False
            visited.add(node)
            dependency = self.ledger.get_task_transfer(node)
            if dependency is None:
                return False
            return any(walk(next_node) for next_node in dependency.depends_on)

        for dep_id in task.depends_on:
            if walk(dep_id):
                raise StateConflictError(
                    f"circular_dependency: dependency {dep_id!r} chain reaches {target!r}"
                )

    def _dependency_lines(self, task: TaskTransfer) -> list[str]:
        lines = []
        for dependency_id in task.depends_on:
            dependency = self.ledger.get_task_transfer(dependency_id)
            if dependency is None:
                lines.append(f"- {dependency_id}: missing")
            else:
                note = " (cancelled: verify downstream still makes sense)" if dependency.status == "cancelled" else ""
                lines.append(f"- {dependency_id}: {dependency.status}{note}")
        return lines

    def _apply_path_guardrails(self, handoff: HandoffResult, path_rule: PathRule) -> HandoffResult:
        agent = self.get_agent(handoff.agent_id)
        violations = _path_violations(handoff, agent=agent, path_rule=path_rule)
        if not violations:
            if handoff.boundary_review == "block":
                return handoff.model_copy(update={"boundary_review": "pass", "violated_guardrail": []})
            return handoff
        blocked = handoff.model_copy(update={"boundary_review": "block", "violated_guardrail": violations})
        self.record_conflict(
            ConflictRecord(
                plan_id=blocked.plan_id,
                task_id=blocked.task_id,
                source="path_violation",
                severity="blocking",
                description="Handoff changed files outside configured path guardrails.",
                involved_agents=[blocked.agent_id],
                involved_files=blocked.changed_files,
            )
        )
        return blocked

    def _audit(
        self,
        task: TaskTransfer,
        action: str,
        agent_id: str,
        *,
        message: str = "",
        from_status: str | None = None,
        to_status: str | None = None,
    ) -> None:
        self.ledger.record_audit_entry(
            AuditEntry(
                entry_id=str(uuid4()),
                trace_id=task.trace_id,
                task_id=task.task_id,
                agent_id=agent_id,
                action=action,
                from_status=from_status,
                to_status=to_status,
                message=message,
            )
        )

    def _publish(
        self,
        task: TaskTransfer,
        event_type: str,
        *,
        actor: str,
        from_status: str | None = None,
        to_status: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if self.event_bus is None:
            return
        self.event_bus.publish(
            TaskEvent(
                type=event_type,
                task_id=task.task_id,
                trace_id=task.trace_id,
                actor=actor,
                from_status=from_status,
                to_status=to_status,
                payload=payload or {},
            )
        )

    def _publish_plan(
        self,
        plan: Plan,
        event_type: str,
        *,
        actor: str = "",
        to_status: str | None = None,
    ) -> None:
        if self.event_bus is None:
            return
        self.event_bus.publish(
            TaskEvent(
                type=event_type,
                task_id="",
                trace_id=plan.plan_id,
                actor=actor or plan.created_by,
                to_status=to_status or plan.status,
                payload={"plan_id": plan.plan_id, "goal": plan.goal},
            )
        )

    def _publish_conflict(self, conflict: ConflictRecord, event_type: str) -> None:
        if self.event_bus is None:
            return
        self.event_bus.publish(
            TaskEvent(
                type=event_type,
                task_id=conflict.task_id or "",
                trace_id=conflict.plan_id or conflict.conflict_id,
                actor="",
                payload={
                    "conflict_id": conflict.conflict_id,
                    "plan_id": conflict.plan_id,
                    "resolved": conflict.resolved,
                },
            )
        )


def _with_selection_metadata(agent: Any, *, capability_score: dict[str, Any] | None = None) -> Any:
    metadata = dict(getattr(agent, "metadata", {}) or {})
    if capability_score is not None:
        metadata["observed_capability_score"] = capability_score
        reason = "observed_capability_success_rate" if int(capability_score.get("total", 0)) > 0 else "capability_load_affinity"
        metadata.setdefault("selection_reason", reason)
    else:
        metadata.setdefault("selection_reason", "capability_load_affinity")
    if hasattr(agent, "model_copy"):
        return agent.model_copy(update={"metadata": metadata})
    try:
        return replace(agent, metadata=metadata)
    except TypeError:
        agent.metadata = metadata
        return agent


def _required_capability(task: TaskTransfer) -> str:
    extra = getattr(task.payload, "extra", {}) if task.payload is not None else {}
    required = extra.get("required_capability") if isinstance(extra, dict) else None
    if required:
        return str(required)
    return str(getattr(task.payload, "type", "custom"))


def _event_type_for_action(action: str) -> str:
    return {
        "accept_handoff": "task_accepted",
        "start_task": "task_started",
        "reject_handoff": "task_rejected",
        "complete_task": "task_completed",
        "mark_review_ready": "task_review_ready",
        "accept_review": "task_review_accepted",
        "reject_review": "task_review_rejected",
    }.get(action, action)


def _preview_quality_gate(task: TaskTransfer, results: list[dict[str, Any]]) -> QualityGatePreview:
    contract = task.test_contract
    if isinstance(contract, dict):
        contract = TestContract.model_validate(contract)

    passed_results = [result for result in results if result.get("status") == "passed"]
    passed_commands = _unique(str(result.get("command")) for result in passed_results if result.get("command") is not None)
    present_evidence = _unique(
        str(item)
        for result in passed_results
        for item in result.get("evidence", [])
    )

    required_commands: list[str] = []
    required_evidence: list[str] = []
    if contract is not None:
        required_commands = list(getattr(contract, "required_commands", []) or contract.recommended_commands)
        required_evidence = list(contract.required_evidence)

    allowed, reason = evaluate_quality_gate(contract, results)
    missing_commands = [command for command in required_commands if command not in passed_commands]
    missing_evidence = [item for item in required_evidence if item not in present_evidence]

    return QualityGatePreview(
        task_id=task.task_id,
        trace_id=task.trace_id,
        has_contract=contract is not None,
        allowed=allowed,
        reason=reason,
        required_commands=required_commands,
        required_evidence=required_evidence,
        passed_commands=passed_commands,
        present_evidence=present_evidence,
        missing_commands=missing_commands,
        missing_evidence=missing_evidence,
        quality_results_count=len(results),
    )


def _current_attempt_quality_results(task: TaskTransfer, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    current_retry_count = int(task.retry_count)
    current_results = []
    for result in results:
        try:
            retry_count = int(result.get("retry_count", 0))
        except (TypeError, ValueError):
            retry_count = 0
        if retry_count == current_retry_count:
            current_results.append(result)
    return current_results


def _readiness_decision(
    task: TaskTransfer,
    quality_preview: QualityGatePreview | None,
) -> tuple[str, str | None]:
    if task.status == "proposed":
        if task.target_agent_id:
            return "accept_handoff", None
        return "claim_task", None
    if task.status == "accepted":
        return "start_task", None
    if task.status == "running":
        if quality_preview is not None and quality_preview.allowed:
            return "complete_task", None
        reason = quality_preview.reason if quality_preview is not None else "quality_gate_unavailable"
        return "submit_quality_result", f"quality_gate_failed:{reason or 'quality_gate_failed'}"
    if task.status == "review_ready":
        return "accept_or_reject_review", None
    if task.status == "completed":
        return "none", "task_completed"
    if task.status == "failed":
        reason = task.error_code or "unknown"
        return "inspect_failure", f"task_failed:{reason}"
    if task.status == "rejected":
        return "inspect_rejection", "task_rejected"
    if task.status == "cancelled":
        return "none", "task_cancelled"
    return "inspect_task", f"unknown_status:{task.status}"


def _unique(items: Any) -> list[str]:
    seen: set[str] = set()
    values: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        values.append(item)
    return values


def _path_violations(
    handoff: HandoffResult,
    *,
    agent: Any | None,
    path_rule: PathRule,
) -> list[str]:
    allowed_patterns: list[str] = []
    forbidden_patterns: list[str] = []
    if agent is not None:
        allowed_patterns.extend(getattr(agent, "allowed_paths", []) or [])
        forbidden_patterns.extend(getattr(agent, "forbidden_paths", []) or [])
    allowed_patterns.extend(path_rule.allowed_patterns)
    forbidden_patterns.extend(path_rule.forbidden_patterns)
    if path_rule.allow_all and not allowed_patterns and not forbidden_patterns:
        return []

    violations = []
    for changed_file in handoff.changed_files:
        normalized = changed_file.replace("\\", "/")
        for pattern in forbidden_patterns:
            if _glob_match(normalized, pattern):
                violations.append(f"forbidden:{changed_file}:{pattern}")
        if allowed_patterns and not any(_glob_match(normalized, pattern) for pattern in allowed_patterns):
            violations.append(f"not_allowed:{changed_file}")
    return violations


def _glob_match(path: str, pattern: str) -> bool:
    normalized_pattern = pattern.replace("\\", "/")
    return fnmatch.fnmatch(path, normalized_pattern)


def _format_list(values: list[str]) -> str:
    return ", ".join(values) if values else "None"


def _now_id() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _parse_iso_to_epoch(timestamp: str) -> float | None:
    """Convert an ISO-8601 timestamp to a UNIX epoch float. None on parse error."""
    try:
        dt = datetime.fromisoformat(timestamp)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return None
