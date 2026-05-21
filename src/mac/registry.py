from __future__ import annotations

import time
from dataclasses import replace
from typing import Any
from uuid import uuid4

from mac.events import TaskEvent, TaskEventBus
from mac.protocol.errors import QualityGateError, StateConflictError
from mac.protocol.messages import (
    AuditEntry,
    QualityGatePreview,
    TaskEvidenceBundle,
    TaskReadinessReport,
    TaskTransfer,
)
from mac.quality.gate import evaluate_quality_gate
from mac.storage import SQLiteTaskLedger, StatusConflict
from mac.testing.contracts import TestContract


class Registry:
    def __init__(self, ledger: SQLiteTaskLedger, *, event_bus: TaskEventBus | None = None) -> None:
        self.ledger = ledger
        self.event_bus = event_bus

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
        if hasattr(agent, "model_copy"):
            refreshed = agent.model_copy(update=updates)
        else:
            refreshed = replace(agent, **updates)
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
        self.ledger.save_task_transfer(task)
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
        allowed, reason = evaluate_quality_gate(
            task.test_contract,
            _current_attempt_quality_results(task, self.ledger.get_quality_results(task_id)),
        )
        if not allowed:
            raise QualityGateError(reason or "quality_gate_failed")
        return self._transition(task_id, "completed", expected_status="running", agent_id=agent_id, action="complete_task")

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


def _now_id() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
