from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel

from mac.protocol.errors import QualityGateError, StateConflictError
from mac.protocol.messages import (
    AgentCard,
    ConflictRecord,
    HandoffResult,
    Plan,
    QualityGatePreview,
    TaskEvidenceBundle,
    TaskReadinessReport,
    TaskTransfer,
)
from mac.registry import Registry


class AgentActionRequest(BaseModel):
    agent_id: str


class AgentHeartbeatRequest(BaseModel):
    agent_id: str
    status: str = "online"
    load: int | None = None


class ClaimTaskRequest(BaseModel):
    capability: str
    project_context: str | None = None
    best_effort: bool = False


class CheckpointTaskRequest(BaseModel):
    agent_id: str
    checkpoint: dict[str, Any]


class FailTaskRequest(BaseModel):
    agent_id: str
    error_code: str
    message: str = ""


class RetryTaskRequest(BaseModel):
    agent_id: str
    fallback_agent_id: str | None = None


class CancelTaskRequest(BaseModel):
    agent_id: str
    reason: str = ""


class CreatePlanRequest(BaseModel):
    plan_id: str | None = None
    goal: str
    created_by: str = ""
    metadata: dict[str, Any] | None = None


class ClosePlanRequest(BaseModel):
    status: str = "completed"


class ResolveConflictRequest(BaseModel):
    resolution: str


def create_app(registry: Registry) -> FastAPI:
    app = FastAPI(title="Multi-Agent Coordinator")

    @app.get("/")
    def root() -> dict[str, str]:
        return {"status": "ok", "service": "mac"}

    @app.post("/agents/register", status_code=201)
    def register_agent(agent: AgentCard) -> AgentCard:
        registry.register(agent)
        return agent

    @app.get("/agents/{agent_id}")
    def get_agent(agent_id: str) -> AgentCard:
        card = registry.get_agent(agent_id)
        if card is None:
            raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")
        return card

    @app.post("/agents/heartbeat", status_code=204)
    def heartbeat_agent(request: AgentHeartbeatRequest) -> Response:
        _call_task(lambda: registry.heartbeat_agent(request.agent_id, status=request.status, load=request.load))
        return Response(status_code=204)

    @app.get("/agents")
    def list_agents(
        capability: str | None = None,
        status: str | None = "online",
        max_load: int | None = None,
        project_context: str | None = None,
    ) -> list[Any]:
        return registry.discover(
            capability=capability,
            status=status,
            max_load=max_load,
            project_context=project_context,
        )

    @app.post("/tasks", status_code=201)
    def submit_task(task: TaskTransfer) -> TaskTransfer:
        return registry.submit_task(task)

    @app.get("/tasks/ready")
    def list_ready_tasks(
        agent_id: str | None = None,
        capability: str | None = None,
        project_context: str | None = None,
    ) -> list[TaskTransfer]:
        return registry.list_ready_tasks(
            agent_id=agent_id,
            capability=capability,
            project_context=project_context,
        )

    @app.get("/tasks")
    def list_tasks(
        status: str | None = None,
        capability: str | None = None,
        agent_id: str | None = None,
        project_context: str | None = None,
    ) -> list[TaskTransfer]:
        return registry.list_tasks(
            status=status,
            capability=capability,
            agent_id=agent_id,
            project_context=project_context,
        )

    @app.get("/tasks/{task_id}")
    def get_task(task_id: str) -> TaskTransfer:
        task = registry.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=task_id)
        return task

    @app.post("/plans", status_code=201)
    def create_plan(request: CreatePlanRequest) -> Plan:
        return registry.create_plan(
            goal=request.goal,
            created_by=request.created_by,
            plan_id=request.plan_id,
            metadata=request.metadata,
        )

    @app.get("/plans")
    def list_plans(status: str | None = None) -> list[Plan]:
        return registry.list_plans(status=status)

    @app.get("/plans/{plan_id}")
    def get_plan(plan_id: str) -> Plan:
        plan = registry.get_plan(plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail=plan_id)
        return plan

    @app.post("/plans/{plan_id}/activate")
    def activate_plan(plan_id: str) -> Plan:
        return _call_task(lambda: registry.activate_plan(plan_id))

    @app.post("/plans/{plan_id}/close")
    def close_plan(plan_id: str, request: ClosePlanRequest | None = None) -> Plan:
        status = request.status if request is not None else "completed"
        return _call_task(lambda: registry.close_plan(plan_id, status=status))

    @app.get("/tasks/{task_id}/evidence")
    def get_task_evidence(task_id: str) -> TaskEvidenceBundle:
        bundle = registry.get_task_evidence(task_id)
        if bundle is None:
            raise HTTPException(status_code=404, detail=task_id)
        return bundle

    @app.post("/handoffs", status_code=201)
    def save_handoff_result(handoff: HandoffResult) -> HandoffResult:
        return _call_task(lambda: registry.save_handoff_result(handoff))

    @app.get("/tasks/{task_id}/handoff")
    def get_handoff_result(task_id: str) -> HandoffResult:
        handoff = registry.get_handoff_result(task_id)
        if handoff is None:
            raise HTTPException(status_code=404, detail=task_id)
        return handoff

    @app.post("/conflicts", status_code=201)
    def record_conflict(conflict: ConflictRecord) -> ConflictRecord:
        return registry.record_conflict(conflict)

    @app.get("/conflicts")
    def list_conflicts(
        plan_id: str | None = None,
        resolved: bool | None = None,
    ) -> list[ConflictRecord]:
        return registry.list_conflicts(plan_id=plan_id, resolved=resolved)

    @app.post("/conflicts/{conflict_id}/resolve")
    def resolve_conflict(conflict_id: str, request: ResolveConflictRequest) -> ConflictRecord:
        return _call_task(lambda: registry.resolve_conflict(conflict_id, request.resolution))

    @app.get("/tasks/{task_id}/worker-packet")
    def worker_packet(task_id: str, agent_id: str | None = None) -> Response:
        packet = _call_task(lambda: registry.prepare_worker_packet(task_id, agent_id=agent_id))
        return Response(content=packet, media_type="text/markdown")

    @app.get("/tasks/{task_id}/review-packet")
    def review_packet(task_id: str) -> Response:
        packet = _call_task(lambda: registry.prepare_review_packet(task_id))
        return Response(content=packet, media_type="text/markdown")

    @app.get("/tasks/{task_id}/quality-preview")
    def preview_quality_gate(task_id: str) -> QualityGatePreview:
        preview = registry.preview_quality_gate(task_id)
        if preview is None:
            raise HTTPException(status_code=404, detail=task_id)
        return preview

    @app.get("/tasks/{task_id}/readiness")
    def preview_task_readiness(task_id: str) -> TaskReadinessReport:
        report = registry.preview_task_readiness(task_id)
        if report is None:
            raise HTTPException(status_code=404, detail=task_id)
        return report

    @app.post("/agents/{agent_id}/claim")
    def claim_task(agent_id: str, request: ClaimTaskRequest) -> TaskTransfer:
        task = registry.claim_next_task(
            agent_id=agent_id,
            capability=request.capability,
            project_context=request.project_context,
            best_effort=request.best_effort,
        )
        if task is None:
            raise HTTPException(status_code=404, detail="No claimable task found")
        return task

    @app.post("/tasks/{task_id}/accept")
    def accept_task(task_id: str, request: AgentActionRequest) -> TaskTransfer:
        return _call_task(lambda: registry.accept_handoff(task_id, request.agent_id))

    @app.post("/tasks/{task_id}/start")
    def start_task(task_id: str, request: AgentActionRequest) -> TaskTransfer:
        return _call_task(lambda: registry.start_task(task_id, request.agent_id))

    @app.post("/tasks/{task_id}/quality-results", status_code=204)
    def submit_quality_result(task_id: str, result: dict[str, Any]) -> Response:
        _call_task(lambda: registry.submit_quality_result(task_id, result))
        return Response(status_code=204)

    @app.post("/tasks/{task_id}/complete")
    def complete_task(task_id: str, request: AgentActionRequest) -> TaskTransfer:
        return _call_task(lambda: registry.complete_task(task_id, request.agent_id))

    @app.post("/tasks/{task_id}/fail")
    def fail_task(task_id: str, request: FailTaskRequest) -> TaskTransfer:
        return _call_task(lambda: registry.fail_task(task_id, request.agent_id, request.error_code, request.message))

    @app.post("/tasks/{task_id}/checkpoint")
    def checkpoint_task(task_id: str, request: CheckpointTaskRequest) -> TaskTransfer:
        return _call_task(lambda: registry.record_checkpoint(task_id, agent_id=request.agent_id, checkpoint=request.checkpoint))

    @app.post("/tasks/{task_id}/retry")
    def retry_task(task_id: str, request: RetryTaskRequest) -> TaskTransfer:
        return _call_task(lambda: registry.retry_task(task_id, agent_id=request.agent_id, fallback_agent_id=request.fallback_agent_id))

    @app.post("/tasks/{task_id}/cancel")
    def cancel_task(task_id: str, request: CancelTaskRequest) -> TaskTransfer:
        return _call_task(lambda: registry.cancel_task(task_id, agent_id=request.agent_id, reason=request.reason))

    @app.get("/ledger/{trace_id}")
    def get_ledger(trace_id: str) -> list[Any]:
        return registry.get_audit_trail(trace_id)

    return app


def _call_task(operation: Any) -> Any:
    try:
        return operation()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (QualityGateError, StateConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
