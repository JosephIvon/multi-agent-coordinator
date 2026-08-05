# Multi-Agent Coordinator (MAC) Specification

> Version: 2.6
> Date: 2026-08-05
> Status: implemented for v1.2.0

---

## 1. Purpose

MAC is a lightweight coordination ledger for AI coding agents. It provides shared task state, context handoff, quality evidence, plan grouping, dependency readiness, handoff records, conflict records, and packet generation.

MAC is intentionally not an execution engine. External agents still run in their own terminals or tools; MAC gives them a common protocol and durable local state.

---

## 2. Core Models

### AgentCard

Agents advertise capabilities, optional roles, and optional path boundaries.

```python
class AgentCard(BaseModel):
    agent_id: str
    name: str
    capabilities: list[AgentCapability]
    roles: list[str] = Field(default_factory=list)
    load: int = Field(default=0, ge=0, le=100)
    status: str = "online"
    last_heartbeat: float = 0
    project_context: str | None = None
    allowed_paths: list[str] = Field(default_factory=list)
    forbidden_paths: list[str] = Field(default_factory=list)
```

`roles` enables tiered agent model: `arch` (architecture design), `core` (core logic), `crud` (boilerplate), `test` (testing), `review` (code review). An agent may hold multiple roles.

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
    required_role: str | None = None
    lease_seconds: int = Field(default=0, ge=0)
    claimed_at: str = Field(default="")
    error_code: str | None = None
```

`required_role` gates `claim_next_task()` — only agents whose `roles` includes the value can claim the task. `None` means no role restriction.

`lease_seconds` is the maximum time an agent may hold a task once claimed. `claimed_at` is the ISO timestamp of the last accept/start. See §6.2 Phase D.

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
                         blocked
```

### With Review (`require_review=True`)

```text
proposed -> accepted -> running -> review_ready -> completed
    |          |           |           |
    v          v           v           v
 rejected   rejected     failed     rejected
                                     (reason → conflict)
                         cancelled
                         blocked
```

Rules:

- `proposed -> accepted`: explicit accept or `claim_next_task()`.
- `accepted -> running`: `start_task()`.
- `running -> completed`: `done()` / `complete_task()` after the quality gate allows completion.
- `running -> blocked`: `done()` hard-fails on quality gate violation → `block_task()` (C-2).
- `running -> review_ready`: `mark_review_ready()` (only when `require_review=True`). Optionally saves handoff.
- `review_ready -> completed`: `accept_review()`.
- `review_ready -> rejected`: `reject_review()`. Rejection reason is automatically recorded as a `ConflictRecord` with `source="reject_review"`.
- `running -> failed`: `fail_task()`.
- Any non-terminal task (including `review_ready` and `blocked`) can become `cancelled`.
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
    reviewer_capability: str | None = None
    path_rule: PathRule = Field(default_factory=PathRule)
    max_retry_count: int = Field(default=3, ge=0)
    agent_timeout: int = Field(default=300, ge=0)
```

Environment variable mapping (`from_env()`):

| Variable | Effect |
|----------|--------|
| `MAC_REQUIRE_REVIEW` | Truthy → `require_review=True` |
| `MAC_REQUIRE_PATH_CHECK` | Truthy → `require_path_check=True` |
| `MAC_MAX_RETRY_COUNT` | Integer override for retry cap |
| `MAC_PATH_RULES` | `allowed1,allowed2\|forbidden1,forbidden2` format |
| `MAC_REVIEWER_CAPABILITY` | Capability name required for `accept_review`/`reject_review` |
| `MAC_AGENT_TIMEOUT` | Seconds before an online agent is considered stale (default 300) |

---

## 6. Registry API

Main operations:

- Agent: `register()`, `discover()`, `heartbeat_agent()`
- Task lifecycle: `submit_task()`, `claim_next_task()`, `accept_handoff()`, `start_task()`, `complete_task()`, `done()`, `fail_task()`, `cancel_task()`, `block_task()`
- Review: `mark_review_ready()`, `accept_review()`, `reject_review()`
- Quality: `submit_quality_result()`, `preview_quality_gate()`, `preview_task_readiness()`
- Plan: `create_plan()`, `activate_plan()`, `close_plan()`, `list_plans()`
- Dependency: `list_ready_tasks()`
- Handoff: `save_handoff_result()`, `get_handoff_result()`
- Conflict: `record_conflict()`, `list_conflicts()`, `resolve_conflict()`
- Packet: `prepare_worker_packet()`, `prepare_review_packet()`
- Kanban: `get_kanban()`
- Expiry: `expire_stale_tasks()`, `expire_stale_agents()`, `expire_task_leases()`
- Facts: `remember_fact()`, `recall_facts()`
- Audit: `get_audit_trail(trace_id)`
- Metrics: `get_metrics()`
- Scoring: `list_scorers()`, `set_scorer()`, `test_scorer()`

CLI and HTTP adapters are thin wrappers around this API.

---

## 6.1 Phase B Features

### B-1: Review Packet Quality Evidence

`prepare_review_packet()` now includes a **Quality Evidence** section listing all quality results for the task:

```text
## Quality Evidence
- `pytest -q`: passed
  evidence: 12 passed, 2 skipped
- `ruff check`: passed
```

This gives reviewers a quick summary of what quality checks passed/failed without needing a separate API call.

### B-2: Worker Packet Upstream Handoff

`prepare_worker_packet()` now inlines upstream handoff summaries for completed dependencies:

```text
### Upstream Handoff: task-1
- Agent: coder
- Changed files: src/auth.py, src/models.py
- Risks: manual browser check still pending
```

Workers see what upstream agents changed and flagged as risky, enabling informed downstream work.

### B-3: Task TTL / Lease Expiry

`expire_stale_tasks()` scans non-terminal tasks (`running`, `review_ready`, `accepted`) whose `created_at + ttl_seconds` has passed and transitions them to `failed` with `error_code="TTL_EXPIRED"`:

```python
expired = registry.expire_stale_tasks()  # returns list of expired TaskTransfer
```

CLI: `mac-agent expire-stale --db mac.db`

### B-4: `mac-agent next` One-Shot Command

`mac-agent next` atomically claims, starts, and outputs a worker packet for the next ready task:

```bash
mac-agent next --agent-id coder --capability write_code --db mac.db
```

Output starts with `---MAC-TASK:` JSON header (machine-parseable) followed by the Markdown worker packet (human-readable).

### B-5: Reviewer Capability Validation

When `CoordinationPolicy.reviewer_capability` is set, `accept_review()` and `reject_review()` verify that the reviewer agent is registered and has a matching capability:

```python
policy = CoordinationPolicy(require_review=True, reviewer_capability="review_code")
registry = Registry(ledger, policy=policy)
# accept_review / reject_review will raise StateConflictError if the
# reviewer lacks the "review_code" capability
```

Environment variable: `MAC_REVIEWER_CAPABILITY=review_code`.

### B-6: Cross-IDE Bridge (`mac://session-context`)

`mac://session-context` MCP resource returns a complete project snapshot:

```json
{
  "kanban": { "red": {...}, "yellow": {...}, "green": {...}, "done": {...} },
  "recent_facts": [...],
  "active_agents": [...],
  "open_conflicts": [...],
  "metrics": {...}
}
```

This is the primary entry point for cross-IDE session recovery. See §9 for full MCP tool/resource listing.

---

## 6.2 Phase C Features

### C-1: Role-Based Agent Routing

`AgentCard.roles` (list of role strings) and `TaskTransfer.required_role` implement a tiered agent model:

```
arch -> core -> crud -> test
  \____________________/
         review (cross-cutting)
```

`claim_next_task()` role gates:
- If `required_role` is set on the task, only agents whose `roles` include it can claim
- If `required_role` is `None`, any agent can claim (open task)
- If the agent has no matching role, `claim_next_task()` returns `None` (skips the task silently)

```
mac-agent register --agent-id codex --name Codex --roles arch,review
mac-agent submit --task-id t1 --required-role arch ...
```

### C-2: Quality Gate Hardening

`TestContract` enforces three additional checks based on risk level:

| Risk | max_diff_lines | require_changelog | require_acceptance_criteria |
|------|---------------|-------------------|---------------------------|
| low | None (no limit) | False | False |
| medium | 500 | True | False |
| high | 300 | True | True |

Custom contracts can override all defaults:

```python
TestContract.for_risk("medium", max_diff_lines=200, require_acceptance_criteria=True)
```

`done()` **hard-fails** on gate violation: calls `block_task()` (transitioning to `blocked` status with `error_code="TASK_BLOCKED"`) instead of returning a soft status. Backward compatibility: caller must explicitly pass `has_changelog` / `met_acceptance_criteria` to trigger C-2 checks; legacy `complete_task()` callers pass `None` sentinels and skip the new checks.

`done()` signature:

```python
def done(
    self, task_id: str, agent_id: str, *,
    quality_result: dict[str, Any] | None = None,
    handoff: HandoffResult | None = None,
    has_changelog: bool | None = None,
    met_acceptance_criteria: list[str] | None = None,
) -> dict:
```

### C-3: Publish Workflow

Tag-triggered PyPI upload via GitHub Actions (`.github/workflows/publish.yml`). Uses PyPI trusted publishing — no API token needed after initial setup.

### C-3: Kanban Board

`Registry.get_kanban()` returns a four-color board:

```python
{
    "red":    {"count": N, "tasks": [...task_ids...]},  # proposed
    "yellow": {"count": N, "tasks": [...]},              # accepted + running
    "green":  {"count": N, "tasks": [...]},              # review_ready
    "done":   {"total": N, "completed": C, "failed": F, "cancelled": X}
}
```

MCP resource: `mac://kanban`
CLI: `mac-agent kanban --json`

Session-start hooks inject the kanban for cross-session awareness.

### C-4: Retry with TTL Expiry

`expire_stale_tasks(auto_retry=True)` resets tasks with remaining retries to `proposed` instead of `failed`. The retry count is incremented and an audit event with `trigger=ttl_expiry` is recorded.

CLI: `mac-agent expire-stale --auto-retry`
MCP: `mac_expire_stale_tasks(auto_retry=True)`
HTTP: `POST /tasks/expire-stale?auto_retry=true`

### C-5: Agent Heartbeat Expiry

`expire_stale_agents()` sets agents offline if their `last_heartbeat` is older than the timeout. Defaults to `policy.agent_timeout` (300s, configurable via `MAC_AGENT_TIMEOUT`).

CLI: `mac-agent expire-stale-agents --timeout 300`
MCP: `mac_expire_stale_agents(timeout_seconds=300)`
HTTP: `POST /agents/expire-stale?timeout_seconds=300`

### C-6: Structured Logging

CLI uses Python `logging` module instead of `print()` for diagnostic output. Machine-parseable output (JSON, Markdown packets) stays on stdout; diagnostics go to stderr. Global flags: `--verbose` (DEBUG), `--quiet` (WARNING).

### C-7: Dashboard Command

`mac-agent dashboard` shows a concise project overview: active plans with task counts, ready/in-flight/review-ready tasks, online agents, unresolved conflicts, and key metrics.

### C-8: Done Command

`done()` is the single entry point for finishing a task. It atomically orchestrates: submit quality evidence (if provided) → evaluate quality gate → save handoff (if provided) → auto-branch on `require_review` to either `complete_task()` or `mark_review_ready()`.

Registry: `registry.done(task_id, agent_id, *, quality_result=None, handoff=None, has_changelog=None, met_acceptance_criteria=None) → dict`

Returns:
- `{"status": "completed", "task_id": ..., "quality_gate": "passed", "review": False}` — no review required
- `{"status": "review_ready", "task_id": ..., "quality_gate": "passed", "review": True}` — review required
- `{"status": "blocked", "task_id": ..., "quality_gate": "failed", "reason": ...}` — gate hard-failed

CLI: `mac-agent done --task-id T --agent-id A [--quality-command CMD --quality-status passed|failed] [--changed-file FILE] [--risk RISK]`

MCP: `mac_done(task_id, agent_id, quality_result?, changed_files?, risks?)`

HTTP: `POST /tasks/{task_id}/done` with body `{agent_id, quality_result?, changed_files?, risks?}`

### C-9: Pluggable Scoring

`mac.scoring` module allows custom (sync or async) scoring functions to reorder `list_ready_tasks()` results. Built-in: `priority_scorer`. Register custom scorers by name:

```python
register_scorer("load_aware", lambda t: t.priority - t.retry_count * 0.1)
registry = Registry(ledger, scoring_fn="load_aware")
```

Async scorers (LLM-backed) use `alist_ready_tasks()`. MCP tools: `mac_list_scorers`, `mac_set_scorer`, `mac_test_scorer`.

---

## 6.3 Phase D: Timebox + Auto-Rollback

`TaskTransfer.lease_seconds` and `claimed_at` implement per-attempt task leases.

When an agent accepts and starts a task with `lease_seconds > 0`:
1. `claimed_at` is set to the current ISO timestamp
2. The agent has `lease_seconds` to complete the task before the lease expires

`Registry.expire_task_leases(auto_retry=False)` scans `accepted`/`running` tasks:
- If `(now - claimed_at) > lease_seconds` and `lease_seconds > 0`: the lease is expired
- With `auto_retry=True` and remaining retries: resets to `proposed` (clears `claimed_at`, increments `retry_count`)
- Without retries: transitions to `failed` with `error_code="LEASE_EXPIRED"`

A task with `lease_seconds=0` has no time limit.

```python
expired = reg.expire_task_leases(auto_retry=True)  # returns list of expired TaskTransfer
```

CLI: `mac-agent expire-task-leases --auto-retry`
MCP: `mac_expire_task_leases(auto_retry=True)`
HTTP: `POST /tasks/expire-leases?auto_retry=true`

---

## 6.4 Phase E: Cross-IDE Knowledge Management

### Facts Table

SQLite ledger stores cross-session facts:

```sql
CREATE TABLE IF NOT EXISTS facts (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
```

Registry API: `remember_fact(key, value, category)` / `recall_facts(query, limit=10)`.

### Obsidian Vault Integration

Two MCP tools for IDE-independent vault access via Obsidian Local REST API (https://127.0.0.1:27124):

- `mac_search_vault(query, limit=10)` — full-text search
- `mac_save_to_vault(content, path, privacy="private")` — create/update notes with frontmatter

Requires Bearer auth token (env var `OBSIDIAN_API_TOKEN`).

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
| `facts` | Cross-session key-value facts for IDE-independent recall |

SQLite WAL mode is enabled.

The `audit_entries` table has a `trace_id` column (default empty) with index `idx_audit_trace(trace_id, created_at)`. Pre-existing databases are auto-migrated: the column is added and `trace_id` is backfilled from the payload JSON for rows written before the column existed.

### 7.1 Downstream schema extensions

Downstream packages may register idempotent SQLite DDL through
`mac.extensions.Extension.table_ddl` or the function-style
`mac.schema_extensions.register_table()` facade. `SQLiteTaskLedger`
applies registered DDL in its initialization transaction after the
core tables. `mac.schema_extensions.connection()` opens the database
selected by `MAC_DB_PATH`, applies registered extension DDL, commits
that schema setup, and returns the live connection to its caller.

`mac.extensions.apply_ddl()` never commits the caller's transaction.

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

### Tools (30)

#### Task Coordination (10)
| Tool | Function |
|------|----------|
| `mac_next_task` | Atomic: claim_next → start → worker_packet |
| `mac_done` | Atomic: quality evidence → gate → complete or review_ready |
| `mac_submit_task` | Submit a new task |
| `mac_claim_task` | Atomic: claim_next → start |
| `mac_record_quality_and_complete` | Quality evidence → complete (legacy, prefer `mac_done`) |
| `mac_fail_task` | Mark task as failed |
| `mac_save_handoff` | Save handoff result |
| `mac_list_ready_tasks` | List dependency-ready proposed tasks |
| `mac_review_packet` | Generate review packet |
| `mac_worker_packet` | Generate worker packet |

#### Review Lifecycle (3)
| Tool | Function |
|------|----------|
| `mac_mark_review_ready` | running → review_ready |
| `mac_accept_review` | review_ready → completed |
| `mac_reject_review` | review_ready → rejected |

#### Maintenance & Lifecycle (7)
| Tool | Function |
|------|----------|
| `mac_expire_stale_tasks` | Expire tasks past TTL |
| `mac_expire_stale_agents` | Mark stale agents offline |
| `mac_cleanup_tasks` | Delete terminal tasks |
| `mac_get_task` | Get task details by ID |
| `mac_retry_task` | Retry a failed task → proposed |
| `mac_resume_blocked_task` | Resolve blocker → proposed |
| `mac_cancel_task` | Cancel a task (terminal) |

#### Scoring (3)
| Tool | Function |
|------|----------|
| `mac_list_scorers` | List registered scoring functions |
| `mac_set_scorer` | Set active scoring function by name |
| `mac_test_scorer` | Test a scorer against tasks |

#### Cross-IDE Knowledge (4)
| Tool | Function |
|------|----------|
| `mac_search_vault` | Full-text search Obsidian vault (REST API) |
| `mac_save_to_vault` | Create/update note in Obsidian vault |
| `mac_remember` | Store cross-session fact in MAC ledger |
| `mac_recall` | Recall facts by query |

#### Lease Management (3)
| Tool | Function |
|------|----------|
| `mac_expire_task_leases` | Auto-release or fail expired lease tasks |
| `mac_list_agents` | List registered agents |
| `mac_block_task` | Block a running task with reason |

### Resources (4)

| URI | Description |
|-----|-------------|
| `mac://capabilities` | Agents grouped by capability name |
| `mac://health` | Health summary: `last_updated`, `open_tasks`, `inflight_agents` |
| `mac://kanban` | Four-color board: red/yellow/green/done |
| `mac://session-context` | Full project snapshot: kanban + facts + agents + conflicts + metrics |

### Extension HTTP and WebSocket surface

`GET /extensions` lists registered extensions and their declared
channels. Each channel is mounted at `/ws/{channel}` when the HTTP
application is created. The server sends `ready` and `event` JSON
frames and answers client `ping` frames with `pong`.

Channel payloads are validated with the registered Pydantic model.
Unknown channels fail instead of falling back to an unvalidated
stream. When `MAC_HTTP_TOKEN` is configured, WebSocket clients must
provide the bearer token through the `Authorization` header or the
`token` query parameter.

---

## 10. Deferred Work

- Parallel group planning and DAG visualization.
- Redis, Postgres, gRPC, and cloud synchronization.
- Automatic conflict resolution.
- Project-specific role presets.
- Daemon workers and automatic external-agent execution.
