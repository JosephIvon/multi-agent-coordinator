# MAC HTTP API Reference

> Version: v1.2.0 | Base URL: `http://localhost:8765`

MAC exposes 55+ REST endpoints for agent registration, task lifecycle, plans, reviews, sessions, callbacks, and remote dispatch. All endpoints return JSON unless noted otherwise.

## Authentication

When `MAC_HTTP_TOKEN` is set, all endpoints (except `/` and `/health`) require:

```
Authorization: Bearer <token>
```

Missing or invalid tokens return `401` with `WWW-Authenticate: Bearer`.

---

## Agents

### `POST /agents/register`
Register or update an agent card.

**Body**: `AgentCard` JSON
**Returns**: `AgentCard` (201)

### `GET /agents`
List agents. Query params: `capability`, `status` (default `online`), `max_load`, `project_context`.

### `GET /agents/{agent_id}`
Get agent by ID. Returns 404 if not found.

### `POST /agents/heartbeat`
Send heartbeat. **Body**: `{agent_id, status?, load?}` (204)

### `POST /agents/{agent_id}/claim`
Claim next ready task. **Body**: `{capability, project_context?, best_effort?}`
Returns `TaskTransfer` or 404.

### `POST /agents/{agent_id}/next`
Atomic: claim + start + worker packet. **Body**: same as claim.
Returns Markdown (`text/markdown`).

### `POST /agents/expire-stale`
Set offline agents past timeout. Query param: `timeout_seconds` (default 300).

---

## Tasks

### `POST /tasks`
Submit a new task. **Body**: `TaskTransfer` JSON → `TaskTransfer` (201).

### `GET /tasks`
List tasks. Query params: `status`, `capability`, `agent_id`, `project_context`.

### `GET /tasks/ready`
List dependency-ready proposed tasks. Query params: `agent_id?`, `capability?`, `project_context?`.

### `GET /tasks/{task_id}`
Get task by ID. Returns 404 if not found.

### `POST /tasks/{task_id}/accept`
Accept handoff. **Body**: `{agent_id}` → `TaskTransfer`.

### `POST /tasks/{task_id}/start`
Start task. **Body**: `{agent_id}` → `TaskTransfer`.

### `POST /tasks/{task_id}/done`
Finish a task in one step. **Body**: `DoneTaskRequest`
```json
{
  "agent_id": "a1",
  "quality_result": {"command": "pytest -q", "status": "passed", "evidence": ["test_output"]},
  "changed_files": ["src/auth.py"],
  "risks": ["manual browser test needed"]
}
```
Returns `{status, task_id, quality_gate, review?, reason?}`.

### `POST /tasks/{task_id}/complete`
Direct complete (legacy, prefer `/done`). **Body**: `{agent_id}`.

### `POST /tasks/{task_id}/fail`
Fail task. **Body**: `{agent_id, error_code, message?}`.

### `POST /tasks/{task_id}/cancel`
Cancel task. **Body**: `{agent_id, reason?}`.

### `POST /tasks/{task_id}/retry`
Retry failed task. **Body**: `{agent_id, fallback_agent_id?}`.

### `POST /tasks/{task_id}/blocked`
Block a running task. **Body**: `{agent_id, reason, handoff_to?, metadata?}`.

### `POST /tasks/{task_id}/resume`
Resume a blocked task. **Body**: `{agent_id, resolution?}`.

### `GET /tasks/{task_id}/blockers`
List blockers for a task. Query param: `status?`.

### `POST /tasks/{task_id}/checkpoint`
Record checkpoint on running task. **Body**: `{agent_id, checkpoint}`.

### `POST /tasks/{task_id}/quality-results`
Submit quality evidence. **Body**: `{command, status, evidence?, output?}` (204).

### `GET /tasks/{task_id}/evidence`
Get quality evidence bundle. Returns `TaskEvidenceBundle`.

### `GET /tasks/{task_id}/worker-packet`
Generate Markdown worker packet. Query param: `agent_id?`.

### `GET /tasks/{task_id}/review-packet`
Generate Markdown review packet.

### `GET /tasks/{task_id}/quality-preview`
Preview quality gate outcome. Returns `QualityGatePreview`.

### `GET /tasks/{task_id}/readiness`
Preview task readiness. Returns `TaskReadinessReport`.

### `POST /tasks/expire-stale`
Expire tasks past TTL. Query param: `auto_retry` (bool, default false).

### `POST /tasks/expire-leases`
Release tasks whose per-attempt timebox lease has expired. Query param: `auto_retry` (bool, default false). With `auto_retry=true`: resets to `proposed` if retries remain; without: fails with `error_code="LEASE_EXPIRED"`.

### `POST /tasks/cleanup`
Delete terminal tasks. **Body**: `{statuses?, plan_id?, older_than_seconds?}`.

### `POST /tasks/{task_id}/dispatch`
Dispatch task to remote worker. **Body**: `{agent_id, url, callback_url, token?, timeout?}`.
Creates a durable session, starts the task, and POSTs the task payload to `url`.
Returns `{session, status_code, body}`.

---

## Review Lifecycle

### `POST /tasks/{task_id}/mark-review-ready`
running → review_ready. **Body**: `{agent_id}`.

### `POST /tasks/{task_id}/accept-review`
review_ready → completed. **Body**: `{agent_id}` (must be reviewer).

### `POST /tasks/{task_id}/reject-review`
review_ready → rejected. **Body**: `{reviewer_id, reason?}`.
Auto-creates a ConflictRecord.

---

## Plans

### `POST /plans`
Create plan. **Body**: `{goal, created_by, plan_id?, metadata?}` → `Plan` (201).

### `GET /plans`
List plans. Query param: `status?`.

### `GET /plans/{plan_id}`
Get plan by ID.

### `POST /plans/{plan_id}/activate`
Activate draft plan.

### `POST /plans/{plan_id}/close`
Close plan. **Body**: `{status?}` (default "completed").

---

## Handoffs

### `POST /handoffs`
Save handoff result. **Body**: `HandoffResult` JSON → `HandoffResult` (201).

### `GET /tasks/{task_id}/handoff`
Get handoff result for task.

---

## Conflicts

### `POST /conflicts`
Record conflict. **Body**: `ConflictRecord` JSON → `ConflictRecord` (201).

### `GET /conflicts`
List conflicts. Query params: `plan_id?`, `resolved?`.

### `POST /conflicts/{conflict_id}/resolve`
Resolve conflict. **Body**: `{resolution}`.

---

## Sessions (Remote Agent Lifecycle)

### `POST /sessions`
Create durable session. **Body**: `{agent_id, task_id?, session_id?, callback_url?, metadata?}` (201).

### `GET /sessions`
List sessions. Query params: `agent_id?`, `task_id?`, `status?`.

### `GET /sessions/{session_id}`
Get session by ID.

### `POST /sessions/{session_id}/heartbeat`
Session heartbeat. **Body**: `{status?}` (default "online").

### `POST /sessions/expire-stale`
Expire stale sessions. Query param: `timeout_seconds` (default 300).

### `POST /sessions/{session_id}/recover`
Recover offline session → online.

---

## Callbacks (Result Delivery)

### `POST /callbacks/{event_id}`
Deliver work result. **Body**: `{session_id, agent_id, task_id, result}`.
- Validates agent/task identity matches session
- Idempotent: same body → `{duplicate: true}`; different body → 409
- On success: applies result, marks session completed

### `GET /callbacks/{event_id}`
Get callback event status.

---

## Observability

### `GET /`
Health check. Returns `{status: "ok", service: "mac"}`.

### `GET /metrics`
Aggregate metrics: cycle_time, handoff_success, quality_gate_pass, retry_rate, conflict_rate, active_agents.

### `GET /ledger/{trace_id}`
Full audit trail for a trace.

### `GET /extensions`
List registered extensions and their WebSocket channels.

---

## WebSocket (`/ws/{channel}`)

Extension WebSocket channels use text frames:

- Server → Client: `{"type": "ready", "channel": "..."}`
- Server → Client: `{"type": "event", "channel": "...", "payload": {...}}`
- Server → Client: `{"type": "error", "detail": [...]}`
- Client → Server: `{"type": "ping"}` → Server replies `{"type": "pong"}`

Authentication: Bearer token via `Authorization` header or `?token=` query param.

---

## Error Responses

| Status | When |
|--------|------|
| 201 | Resource created |
| 204 | Action confirmed (no body) |
| 400 | Validation error |
| 401 | Missing/invalid bearer token |
| 403 | Callback identity mismatch |
| 404 | Resource not found |
| 409 | Callback event id conflict (replay with different body) |
| 422 | State conflict (wrong status for operation) |

All errors return `{"detail": "<message>"}`.
