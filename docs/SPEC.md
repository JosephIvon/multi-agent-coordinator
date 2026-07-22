# Multi-Agent Coordinator (MAC) Specification

> Version: 2.1
> Date: 2026-07-22
> Status: implemented for local Phase A collaboration

---

## 1. Purpose

MAC is a lightweight coordination ledger for AI coding agents. It provides shared task state, context handoff, quality evidence, plan grouping, dependency readiness, handoff records, conflict records, and packet generation.

MAC is intentionally not an execution engine. External agents still run in their own terminals or tools; MAC gives them a common protocol and durable local state.

---

## 2. Core Models

### AgentCard

Agents advertise capabilities and optional path boundaries.

```python
class AgentCard(BaseModel):
    agent_id: str
    name: str
    capabilities: list[AgentCapability]
    load: int = Field(default=0, ge=0, le=100)
    status: str = "online"
    last_heartbeat: float = 0
    project_context: str | None = None
    allowed_paths: list[str] = Field(default_factory=list)
    forbidden_paths: list[str] = Field(default_factory=list)
```

Empty `allowed_paths` and `forbidden_paths` means no agent-level path restriction.

### TaskTransfer

`TaskTransfer` is the durable task row.

```python
class TaskTransfer(BaseModel):
    task_id: str
    trace_id: str
    source_agent_id: str | None = None
    target_agent_id: str | None = None
    payload: TaskPayload | None = None
    context: ContextBundle | None = None
    test_contract: Any | None = None
    priority: int = Field(default=5, ge=1, le=10)
    status: str = "proposed"
    plan_id: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    retry_count: int = 0
    fallback_agent_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
```

`TaskTransfer` does not embed `HandoffResult`. Handoff records are stored separately so task rows stay small.

### Plan

`Plan` groups related tasks.

```python
class Plan(BaseModel):
    plan_id: str
    goal: str
    status: Literal["draft", "active", "completed", "cancelled"] = "draft"
    task_ids: list[str] = Field(default_factory=list)
    created_by: str = ""
    created_at: str
    closed_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
```

Phase A supports flat task lists plus `depends_on`. `parallel_groups` are deferred.

### HandoffResult

`HandoffResult` is the structured output a worker leaves for the next agent or reviewer.

```python
class HandoffResult(BaseModel):
    task_id: str
    plan_id: str | None = None
    agent_id: str
    verification: list[VerificationEntry] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    docs_touched: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    boundary_review: Literal["pass", "block", "not_required"] = "not_required"
    violated_guardrail: list[str] = Field(default_factory=list)
```

### ConflictRecord

`ConflictRecord` tracks coordination conflicts that need human or reviewer resolution.

```python
class ConflictRecord(BaseModel):
    conflict_id: str
    plan_id: str | None = None
    task_id: str | None = None
    source: str
    severity: Literal["blocking", "non_blocking"] = "non_blocking"
    description: str
    involved_agents: list[str] = Field(default_factory=list)
    involved_files: list[str] = Field(default_factory=list)
    resolved: bool = False
    resolution: str = ""
```

---

## 3. Task State Machine

### Default (`require_review=False`)

```text
proposed -> accepted -> running -> completed
    |          |           |
    v          v           v
 rejected   rejected     failed
                         cancelled
```

### With Review (`require_review=True`)

```text
proposed -> accepted -> running -> review_ready -> completed
    |          |           |           |
    v          v           v           v
 rejected   rejected     failed     rejected
                                     (reason → conflict)
                         cancelled
```

Rules:

- `proposed -> accepted`: explicit accept or `claim_next_task()`.
- `accepted -> running`: `start_task()`.
- `running -> completed`: `complete_task()` after the quality gate allows completion (only when `require_review=False`).
- `running -> review_ready`: `mark_review_ready()` (only when `require_review=True`). Optionally saves handoff.
- `review_ready -> completed`: `accept_review()`.
- `review_ready -> rejected`: `reject_review()`. Rejection reason is automatically recorded as a `ConflictRecord` with `source="reject_review"`.
- `running -> failed`: `fail_task()`.
- Any non-terminal task (including `review_ready`) can become `cancelled`.
- When `require_review=True`, calling `complete_task()` on a `running` task raises `StateConflictError`.

---

## 4. Dependency Readiness

`depends_on` is a list of upstream task IDs.

A proposed task is ready only when every dependency exists and has status `completed` or `cancelled`.

Important: `accepted` does not unlock a dependency. It only means an agent claimed the upstream task. A cancelled dependency stops scheduler waiting, but worker/review packets show the cancelled dependency explicitly so humans and agents can decide whether downstream work is still valid.

`list_ready_tasks()` is read-only and does not write audit entries.

`claim_next_task()` skips dependency-blocked tasks.

### Cycle Detection

`submit_task()` rejects tasks whose `depends_on` creates a cycle. The check walks the existing dependency graph from each declared dependency; if any path leads back to the new task's `task_id`, `StateConflictError(circular_dependency)` is raised and the row is never persisted.

Self-loops (`task_id` in its own `depends_on`) are also rejected.

---

## 5. Path Guardrails

Path checking combines optional agent boundaries and optional project `PathRule`.

Defaults are allow-all:

```python
class PathRule(BaseModel):
    allow_all: bool = True
    forbidden_patterns: list[str] = Field(default_factory=list)
    allowed_patterns: list[str] = Field(default_factory=list)
```

If no allowed or forbidden patterns exist, no checking is performed. If any pattern exists, changed files in `HandoffResult.changed_files` are checked. Violations set `boundary_review="block"` and record a `path_violation` conflict.

---

## 5.1 Coordination Policy

`CoordinationPolicy` controls optional coordination features. It is passed to `Registry` at construction and can be loaded from environment variables.

```python
class CoordinationPolicy(BaseModel):
    require_review: bool = False
    require_path_check: bool = False
    path_rule: PathRule = Field(default_factory=PathRule)
    max_retry_count: int = Field(default=3, ge=0)
```

Environment variable mapping (`from_env()`):

| Variable | Effect |
|----------|--------|
| `MAC_REQUIRE_REVIEW` | Truthy → `require_review=True` |
| `MAC_REQUIRE_PATH_CHECK` | Truthy → `require_path_check=True` |
| `MAC_MAX_RETRY_COUNT` | Integer override for retry cap |
| `MAC_PATH_RULES` | `allowed1,allowed2\|forbidden1,forbidden2` format |

---

## 6. Registry API

Main operations:

- Agent: `register()`, `discover()`, `heartbeat_agent()`
- Task lifecycle: `submit_task()`, `claim_next_task()`, `accept_handoff()`, `start_task()`, `complete_task()`, `fail_task()`, `cancel_task()`
- Review: `mark_review_ready()`, `accept_review()`, `reject_review()`
- Quality: `submit_quality_result()`, `preview_quality_gate()`, `preview_task_readiness()`
- Plan: `create_plan()`, `activate_plan()`, `close_plan()`, `list_plans()`
- Dependency: `list_ready_tasks()`
- Handoff: `save_handoff_result()`, `get_handoff_result()`
- Conflict: `record_conflict()`, `list_conflicts()`, `resolve_conflict()`
- Packet: `prepare_worker_packet()`, `prepare_review_packet()`
- Audit: `get_audit_trail(trace_id)`
- Metrics: `get_metrics()`

CLI and HTTP adapters are thin wrappers around this API.

---

## 7. SQLite Ledger

Tables:

| Table | Purpose |
|-------|---------|
| `agent_cards` | Agent card JSON plus indexed status/load/capability metadata |
| `task_transfers` | Task JSON plus indexed status/project context |
| `audit_entries` | Append-only task audit events (indexed by `trace_id` + `created_at`) |
| `quality_results` | Quality evidence by task and retry attempt |
| `agent_outcomes` | Observed capability outcomes |
| `plans` | Plan JSON and plan status |
| `handoff_results` | Structured handoff JSON by task |
| `conflict_records` | Conflict JSON and resolved index |

SQLite WAL mode is enabled. Phase A is intended for a local single-workspace setup.

The `audit_entries` table has a `trace_id` column (default empty) with index `idx_audit_trace(trace_id, created_at)`. Pre-existing databases are auto-migrated: the column is added and `trace_id` is backfilled from the payload JSON for rows written before the column existed.

---

## 8. Trace Metrics

Six read-only aggregate indicators derived from existing SQLite tables (no new schema):

| Indicator | Description |
|-----------|-------------|
| `task_cycle_time_seconds` | Average time from first `submit_task` audit to `task_transfers.updated_at` (status=completed) |
| `handoff_success_rate` | `boundary_review == 'pass'` / total handoffs |
| `quality_gate_pass_rate` | `status == 'passed'` / total quality results |
| `retry_rate` | Tasks with `retry_count > 0` / total tasks |
| `conflict_rate` | Conflict records / total tasks |
| `active_agents` | Agent cards with `status == 'online'` |

Python API: `compute_metrics(ledger) → dict`. HTTP: `GET /metrics`.

Payload JSON is deserialized in Python and aggregated there (no `json_extract`, which requires SQLite 3.38+; this project supports Python 3.10+ whose stdlib ships SQLite 3.37).

---

## 9. MCP Server

MAC exposes its coordination API as an MCP (Model Context Protocol) server for AI coding tools. The server uses `FastMCP` with stdio transport.

### Error Signaling

Domain errors are raised as `ToolError` so the MCP SDK marks responses with `isError=True`:

| Domain Exception | ToolError Prefix |
|------------------|-----------------|
| `KeyError` | `not_found` |
| `ValidationError` | `validation_failed` |
| `QualityGateError` | `quality_gate_failed` |
| `StateConflictError` | `state_conflict` |
| `None` result | `not_found` |

LLM clients (Claude Code, Cursor, etc.) use `isError` to decide retry/strategy. Business errors are never returned as `isError=False`.

### Tools (11)

| Tool | Parameters | Returns | Side Effect |
|------|-----------|---------|-------------|
| `mac_submit_task` | `task: dict` (TaskTransfer) | JSON TaskTransfer | write |
| `mac_claim_task` | `agent_id`, `capability`, `project_context?`, `best_effort?` | JSON TaskTransfer | write |
| `mac_record_quality_and_complete` | `task_id`, `agent_id`, `result: dict` | JSON `{status, task_id, reason}` | write |
| `mac_fail_task` | `task_id`, `agent_id`, `error_code`, `message?` | JSON TaskTransfer | write |
| `mac_save_handoff` | `task_id`, `agent_id`, `changed_files?`, `verification_passed?`, `boundary_review?`, `risks?` | JSON HandoffResult | write |
| `mac_list_ready_tasks` | `capability?`, `project_context?` | JSON array of TaskTransfer | read-only |
| `mac_review_packet` | `task_id` | Markdown string | read-only |
| `mac_worker_packet` | `task_id`, `agent_id?` | Markdown string | read-only |
| `mac_mark_review_ready` | `task_id`, `agent_id`, `handoff?` | JSON TaskTransfer | write |
| `mac_accept_review` | `task_id`, `reviewer_id` | JSON TaskTransfer | write |
| `mac_reject_review` | `task_id`, `reviewer_id`, `reason?` | JSON TaskTransfer | write |

`mac_claim_task` is atomic: `claim_next_task` → `start_task` in one call.

`mac_record_quality_and_complete` is atomic: `submit_quality_result` → `evaluate_quality_gate` → `complete_task` (only if gate passes). Returns `status='completed'` or `status='running'` with reason.

`mac_worker_packet` includes the agent's `allowed_paths` and `forbidden_paths` when `agent_id` is provided.

Review tools are only effective when `CoordinationPolicy.require_review=True`. `mac_mark_review_ready` transitions `running → review_ready`. `mac_accept_review` transitions `review_ready → completed`. `mac_reject_review` transitions `review_ready → rejected` and auto-records a conflict with `source="reject_review"`.

### Resources (2)

| URI | Description |
|-----|-------------|
| `mac://capabilities` | Agents grouped by capability name |
| `mac://health` | Health summary: `last_updated`, `open_tasks`, `inflight_agents` |

---

## 10. Deferred Work

- Leases, daemon workers, and automatic external-agent execution.
- Parallel group planning and DAG visualization.
- Redis, Postgres, gRPC, and cloud synchronization.
- Automatic conflict resolution.
- Project-specific role presets.
