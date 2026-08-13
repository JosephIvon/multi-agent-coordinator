# IDE and Remote Agent Integrations

MAC keeps exactly one factual store: the SQLite ledger. IDE files are connection
and context entry points only.

## MCP Surface (v1.2.0)

The MCP server exposes 31 tools + 4 resources. The authoritative
per-tool table is maintained in [README](../README.md#available-tools-31) and
[SPEC](SPEC.md#9-mcp-server); this integration page keeps only the grouping
needed to choose an IDE entry point.

| Category | Count | Tools |
|---|---|---|
| Task Coordination & Lifecycle | 23 | Task lifecycle, review, packets, stale expiry/cleanup, retry/block/lease, and agent discovery |
| Scoring | 3 | `mac_list_scorers`, `mac_set_scorer`, `mac_test_scorer` |
| Cross-IDE Knowledge | 5 | Vault search/save/promote plus fact remember/recall |

The category counts are navigational only; use the linked authoritative tables
for exact names and side effects.

MCP Resources:

| URI | Description |
|---|---|
| `mac://capabilities` | Agents grouped by capability name |
| `mac://health` | Health summary: `last_updated`, `open_tasks`, `inflight_agents` |
| `mac://kanban` | Four-color board: red/yellow/green/done |
| `mac://session-context` | Full project snapshot: kanban + facts + agents + conflicts + metrics |

## Shared MCP command

All MCP-capable tools launch:

```json
{"command": "mac-mcp-server", "args": [], "env": {"MAC_DB_PATH": "mac.db"}}
```

The MCP server reads `MAC_DB_PATH` from its environment (falling back to
`mac.db` in the process working directory).  The CLI honours the same
variable as a default for ``--db``.  When two tools must share a ledger,
point them at the same absolute path:

```json
{"command": "mac-mcp-server", "args": [], "env": {"MAC_DB_PATH": "C:\\shared\\mac.db"}}
```

| Tool | Generated entry | Purpose |
|---|---|---|
| Codex | `AGENTS.md` | Project instructions pointing to MAC |
| Claude Code | `CLAUDE.md`, `.mcp.json` | Rules entry and MCP server |
| Cursor | `.cursor/rules/mac.mdc`, `.cursor/mcp.json` | Always-applied entry and MCP |
| OpenCode CLI | `opencode.json`, `.opencode/rules/mac.md` | Local MCP and rule entry |
| Trae | `.trae/rules/mac.md`, `.trae/mcp.json` | Generic MCP-compatible entry |
| Qoder | `.qoder/rules/mac.md`, `.qoder/mcp.json` | Generic MCP-compatible entry |
| WorkBuddy | `.workbuddy/rules/mac.md`, `.workbuddy/mcp.json` | Generic MCP-compatible entry |

Vendor formats evolve; `mac-agent bootstrap` owns only its marked block and tiny
MCP connection files. If a vendor changes its config location, copy the same MCP
command into the vendor UI—never copy ledger contents into the rule file.

## Remote HTTP callback lifecycle

1. Register an `AgentCard`.
2. Submit a task.
3. Call `POST /tasks/{task_id}/dispatch`. MAC creates a durable session, starts
   the task, and sends the task plus `session_id` and `callback_url`.
4. The worker periodically calls `POST /sessions/{session_id}/heartbeat`.
5. The worker calls `POST /callbacks/{event_id}` once with a stable event ID.
6. MAC validates bearer auth and session/task/agent identity, atomically claims
   the event ID, writes handoff/quality/blocker state, and closes the session.
7. Replays with the same body return `duplicate: true`; reuse with a different
   body returns HTTP 409.

Completed callback body:

```json
{
  "session_id": "session-uuid",
  "task_id": "task-1",
  "agent_id": "worker-1",
  "result": {
    "status": "completed",
    "summary": "Implemented and tested",
    "changed_files": ["src/app.py"],
    "verification": ["pytest"]
  }
}
```

Blocked callback body:

```json
{
  "session_id": "session-uuid",
  "task_id": "task-1",
  "agent_id": "worker-1",
  "result": {
    "status": "blocked",
    "blocker": "Database schema decision required",
    "handoff_to": "architect"
  }
}
```

A blocked result creates a durable blocker, records the handoff target, and sets
the task to `blocked`. Resolve it with `POST /tasks/{task_id}/resume`.

## Cross-IDE Knowledge Tools (v1.2.0)

Four MCP tools provide IDE-independent knowledge management, so vault search and
session memory work identically from Claude Code, Codex, Trae, Cursor, or any
MCP-capable tool:

| Tool | Purpose |
|---|---|
| `mac_search_vault` | Full-text search Obsidian vault via Local REST API |
| `mac_save_to_vault` | Create or update a note in the Obsidian vault |
| `mac_remember` | Store a cross-session fact in the MAC SQLite ledger |
| `mac_recall` | Recall facts from the MAC ledger by query |

These share the MAC DB (not IDE-specific storage), so facts are visible to all
IDEs. The vault tools require the Obsidian Local REST API plugin to be running.

## Session Recovery: `mac://session-context` (v1.2.0)

`mac://session-context` is the primary cross-IDE session recovery entry point.
It returns a full project snapshot in one resource read:

- **Kanban board** — all tasks grouped by status (proposed/in-flight/review/blocked/done)
- **Facts** — recent `mac_remember` entries
- **Agents** — registered agents with roles, load, and status
- **Conflicts** — unresolved conflict records
- **Metrics** — completion rate, quality gate pass rate, active agent count

Session-start hooks inject this resource so every IDE lands on the same project
picture. See also the `mac://kanban` resource for a focused four-color board
(red = blocked/overdue, yellow = in-flight, green = proposed ready, done = completed).

## Role-Based Agent Routing (v1.2.0)

`AgentCard.roles` and `TaskTransfer.required_role` implement a tiered agent
model. Valid roles:

| Role | Scope |
|---|---|
| `arch` | Architecture design, cross-cutting decisions |
| `core` | Core logic implementation, complex features |
| `crud` | Boilerplate, CRUD operations, mechanical changes |
| `test` | Testing, verification, quality evidence |
| `review` | Code review, handoff evaluation |

An agent may hold multiple roles. When `required_role` is set on a task, only
agents whose `roles` includes that value can claim it via `claim_next_task()`.
Tasks with no `required_role` are open to all agents.

Register a role-gated agent:

```json
{
  "agent_id": "arch-1",
  "name": "Architect",
  "capabilities": ["design"],
  "roles": ["arch"]
}
```

## Timeboxed Tasks with `lease_seconds` (v1.2.0)

`TaskTransfer.lease_seconds` sets a per-attempt time limit. When an agent claims
a task with `lease_seconds > 0`:

1. `claimed_at` is recorded as the ISO timestamp of claim
2. The agent has `lease_seconds` to complete before the lease expires
3. If `(now - claimed_at) > lease_seconds`, the lease is expired

`mac_expire_task_leases` auto-releases expired leases back to `proposed` (or
`failed` if retries are exhausted). A task with `lease_seconds=0` has no time
limit — opt-in timeboxing.

Submit a timeboxed task:

```json
{
  "task_id": "task-1",
  "payload": {"description": "Critical hotfix"},
  "lease_seconds": 300
}
```

## Quality Gate C-2 Enforcement (v1.2.0)

`done()` is the single atomic entry point for finishing a task. It orchestrates:
submit quality evidence -> evaluate quality gate -> save handoff -> auto-branch
on `require_review` to either `complete_task()` or `mark_review_ready()`.

**Hard-fail on gate violation**: `done()` calls `block_task()` (transitioning to
`blocked` with `error_code="TASK_BLOCKED"`) instead of returning a soft status.
The task stays blocked until a human or architect resolves the blocker and calls
`POST /tasks/{task_id}/resume`.

`TestContract` enforces three risk-tiered checks:

| Risk | max_diff_lines | require_changelog | require_acceptance_criteria |
|------|---------------|-------------------|---------------------------|
| low | None (no limit) | False | False |
| medium | 500 | True | False |
| high | 300 | True | True |

Custom contracts can override all defaults:

```python
TestContract.for_risk("medium", max_diff_lines=200, require_acceptance_criteria=True)
```

`done()` signature (caller must explicitly pass C-2 flags):

```python
def done(
    self, task_id: str, agent_id: str, *,
    quality_result: dict[str, Any] | None = None,
    handoff: HandoffResult | None = None,
    has_changelog: bool | None = None,
    met_acceptance_criteria: list[str] | None = None,
) -> dict:
```

Returns:
- `{"status": "completed", "quality_gate": "passed", "review": False}` — no review required
- `{"status": "review_ready", "quality_gate": "passed", "review": True}` — review required
- `{"status": "blocked", "quality_gate": "failed", "reason": "..."}` — gate hard-failed

Legacy callers that pass `has_changelog=None` / `met_acceptance_criteria=None`
skip the new C-2 checks, preserving backward compatibility.

## Reliability notes

- Use a unique, unpredictable HTTP token and TLS at the reverse proxy.
- Callback event IDs must be stable across worker retries.
- Secret-shaped fields and bearer credentials are redacted at adapter boundaries.
- CLI adapters return 124 on timeout and 130 on cancellation.
- Run stale-session and stale-agent expiry from your scheduler when operating a
  long-lived coordinator.
