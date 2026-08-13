# Multi-Agent Coordinator (MAC)

**Version:** 1.2.0 | **License:** MIT

MAC is a lightweight local coordination layer for AI coding agents. It gives multiple agents a shared ledger for tasks, plans, context handoff, quality evidence, conflict records, and review packets.

It is useful when you use several AI coding tools in the same project and need one place to answer:

- What tasks exist, and who claimed them?
- Which tasks are blocked by unfinished upstream work?
- What did the previous agent change, verify, and leave risky?
- What conflicts or path-boundary issues need human review?
- What prompt packet should I give to the next worker or reviewer agent?

MAC is coordination-first. Generic CLI/HTTP adapters can dispatch work while durable task, session, blocker, and handoff truth remains in one SQLite ledger exposed through CLI, MCP, and authenticated HTTP surfaces.

---

## Install

```bash
pip install mac-agent
pip install "mac-agent[http]"
pip install -e ".[dev]"
```

---

## Quick Start: Single Task

```bash
mac-agent register --agent-id claude --name Claude --capability write_code
mac-agent submit --task-id t1 --source-agent-id planner --type write_code --summary "Add auth handler"

# One command to claim + start + get work instructions
mac-agent next --agent-id claude --capability write_code

# ... agent does the work ...

# One command to finish: quality evidence + handoff + complete
mac-agent done --task-id t1 --agent-id claude \
  --quality-command "pytest -q" --quality-status passed \
  --changed-file src/auth.py --risk "manual browser test needed"
```

---

## Quick Start: Collaboration Plan

```bash
mac-agent plan create --plan-id plan-1 --goal "Ship login flow" --created-by planner
mac-agent plan activate --plan-id plan-1

mac-agent register --agent-id coder --name Coder --capability write_code --allowed-path "src/**"
mac-agent register --agent-id tester --name Tester --capability write_test --allowed-path "tests/**"

mac-agent submit --task-id code-login --source-agent-id planner --type write_code --summary "Implement login" --plan-id plan-1
mac-agent submit --task-id test-login --source-agent-id planner --type write_test --summary "Test login" --plan-id plan-1 --depends-on code-login --target-module src/login.py --coverage-goal 80

mac-agent ready-tasks --capability write_code
mac-agent worker-packet --task-id code-login --agent-id coder
```

After an agent finishes, save its handoff:

```bash
mac-agent handoff \
  --task-id code-login \
  --agent-id coder \
  --plan-id plan-1 \
  --verification "python -m pytest -q:pass:unit suite" \
  --changed-file src/login.py \
  --risk "manual browser check still pending"
```

Then the next agent or human reviewer can inspect:

```bash
mac-agent review-packet --task-id code-login
mac-agent conflicts --plan-id plan-1 --unresolved
```

Run the complete local example:

```bash
python examples/collaboration_plan.py
```

---

## Python API

```python
from mac.registry import Registry
from mac.storage.sqlite import SQLiteStorage
from mac.protocol.messages import AgentCard, AgentCapability, TaskPayload, TaskTransfer

registry = Registry(SQLiteStorage("mac.db"))

registry.register(
    AgentCard(
        agent_id="worker-1",
        name="Worker",
        capabilities=[AgentCapability(name="write_code")],
        allowed_paths=["src/**"],
    )
)

plan = registry.create_plan(goal="Ship collaboration layer", created_by="planner")

registry.submit_task(
    TaskTransfer(
        task_id="task-1",
        plan_id=plan.plan_id,
        payload=TaskPayload(type="write_code", summary="Implement feature"),
    )
)

ready = registry.list_ready_tasks(capability="write_code")
```

---

## HTTP Adapter

```python
from mac.transport.http_ws import create_app
from mac.registry import Registry
from mac.storage.sqlite import SQLiteStorage

app = create_app(Registry(SQLiteStorage("mac.db")))
# Run with: uvicorn app:app --port 8000
```

Core endpoints:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Health check |
| `GET/POST` | `/agents` / `/agents/register` | Discover or register agents |
| `POST` | `/agents/heartbeat` | Refresh agent status |
| `POST` | `/agents/{agent_id}/claim` | Claim a dependency-ready task |
| `GET/POST` | `/tasks` | List or submit tasks |
| `GET` | `/tasks/ready` | List dependency-ready proposed tasks |
| `GET` | `/tasks/{task_id}` | Get task by ID |
| `GET` | `/tasks/{task_id}/evidence` | Task evidence bundle |
| `POST` | `/plans` | Create plan |
| `GET` | `/plans` | List plans |
| `POST` | `/plans/{plan_id}/activate` | Activate plan |
| `POST` | `/plans/{plan_id}/close` | Close plan |
| `POST` | `/handoffs` | Save structured handoff |
| `GET` | `/tasks/{task_id}/handoff` | Get structured handoff |
| `POST/GET` | `/conflicts` | Record or list conflicts |
| `POST` | `/conflicts/{conflict_id}/resolve` | Resolve conflict |
| `GET` | `/tasks/{task_id}/worker-packet` | Generate worker packet |
| `GET` | `/tasks/{task_id}/review-packet` | Generate review packet |
| `POST` | `/tasks/expire-stale` | Expire tasks past their TTL |
| `POST` | `/agents/{agent_id}/next` | Claim + start + worker packet (atomic) |
| `POST` | `/tasks/{task_id}/done` | Finish task: quality + handoff + complete (or review-ready) |
| `POST` | `/agents/expire-stale` | Set offline agents with stale heartbeats |

---

## MCP Server

MAC exposes its coordination API as an MCP (Model Context Protocol) server, so AI coding tools like Claude Code, Cursor, and Windsurf can call MAC natively. MAC v1.2 currently supports the MCP 1.x FastMCP API; MCP 2.x requires a coordinated adapter migration and is not installed by this release.

### Setup

```bash
pip install "mac-agent[mcp]"
```

### Running

```bash
# Console script (stdio transport)
mac-mcp-server

# Or module form
python -m mac.mcp_server
```

### Connecting AI Tools

**Claude Code:**

```bash
claude mcp add mac -- mac-mcp-server
```

**Cursor / Windsurf** — add to `.cursor/mcp.json` or project settings:

```json
{
  "mcpServers": {
    "mac": {
      "command": "mac-mcp-server",
      "args": []
    }
  }
}
```

### Available Tools (31)

| Tool | Purpose | Side Effect |
|------|---------|-------------|
| `mac_next_task` | Claim + start + output worker packet (atomic) | write |
| `mac_done` | Finish task in one step: quality + handoff + complete (or review-ready) | write |
| `mac_submit_task` | Submit a task (full TaskTransfer dict) | write |
| `mac_claim_task` | Claim + start a task (atomic) | write |
| `mac_record_quality_and_complete` | Submit evidence + auto-complete on gate pass (legacy, prefer mac_done) | write |
| `mac_fail_task` | Mark task as failed | write |
| `mac_save_handoff` | Save structured handoff result | write |
| `mac_list_ready_tasks` | List claimable tasks | read-only |
| `mac_review_packet` | Generate reviewer prompt (Markdown) | read-only |
| `mac_worker_packet` | Generate worker prompt (Markdown) | read-only |
| `mac_mark_review_ready` | Move task to review_ready (requires `require_review=True`) | write |
| `mac_accept_review` | Accept reviewed task → completed | write |
| `mac_reject_review` | Reject reviewed task → rejected (auto-records conflict) | write |
| `mac_expire_stale_tasks` | Expire non-terminal tasks past TTL → failed | write |
| `mac_expire_stale_agents` | Set offline agents with stale heartbeats | write |
| `mac_cleanup_tasks` | Delete terminal tasks (failed/cancelled/rejected/superseded) | write |
| `mac_get_task` | Get full task details by ID | read-only |
| `mac_retry_task` | Retry a failed task → proposed | write |
| `mac_resume_blocked_task` | Resume a blocked task → proposed (quality gate recovery) | write |
| `mac_cancel_task` | Cancel a task (terminal) | write |
| `mac_expire_task_leases` | Release expired per-attempt timebox leases | write |
| `mac_list_agents` | List all registered agents (optionally filter by status) | read-only |
| `mac_block_task` | Block a running task with reason | write |
| `mac_list_scorers` | List registered scoring functions | read-only |
| `mac_set_scorer` | Set active scoring function by name | write |
| `mac_test_scorer` | Test a scorer against proposed tasks | read-only |
| `mac_search_vault` | Full-text search Obsidian vault | read-only |
| `mac_save_to_vault` | Create/update note in Obsidian vault | write |
| `mac_promote_to_knowledge` | Promote a reviewed vault draft to permanent knowledge | write |
| `mac_remember` | Store cross-session fact in MAC ledger | write |
| `mac_recall` | Search facts by query (empty = recent 10) | read-only |

### Available Resources (4)

| URI | Description |
|-----|-------------|
| `mac://capabilities` | Agent capability registry |
| `mac://health` | Health summary (open tasks, inflight agents) |
| `mac://kanban` | Four-color board: red/yellow/green/done |
| `mac://session-context` | Full project snapshot: kanban + facts + agents + conflicts |

---

## Observability

MAC exposes 6 aggregate metrics via the Python API and HTTP endpoint:

| Metric | Description |
|--------|-------------|
| `task_cycle_time_seconds` | Average time from submit to completed |
| `handoff_success_rate` | Fraction of handoffs with `boundary_review == 'pass'` |
| `quality_gate_pass_rate` | Fraction of quality results with `status == 'passed'` |
| `retry_rate` | Fraction of tasks with `retry_count > 0` |
| `conflict_rate` | Conflicts per task |
| `active_agents` | Agents currently online |

```python
from mac.metrics import compute_metrics
from mac.storage.sqlite import SQLiteTaskLedger

metrics = compute_metrics(SQLiteTaskLedger("mac.db"))
print(metrics["quality_gate_pass_rate"])
# 0.8571
```

HTTP: `GET /metrics` returns the same dict as JSON.

---

## Coordination Policy

Optional features are controlled by `CoordinationPolicy`, passed to `Registry` or loaded from environment variables:

```python
from mac.registry import Registry
from mac.protocol.messages import CoordinationPolicy
from mac.storage.sqlite import SQLiteTaskLedger

# Explicit policy
policy = CoordinationPolicy(require_review=True)
registry = Registry(SQLiteTaskLedger("mac.db"), policy=policy)

# Or from environment (MAC_REQUIRE_REVIEW=1, etc.)
registry = Registry(SQLiteTaskLedger("mac.db"))
```

| Variable | Effect |
|----------|--------|
| `MAC_REQUIRE_REVIEW` | Truthy → tasks go through `review_ready` before `completed` |
| `MAC_REQUIRE_PATH_CHECK` | Truthy → enforce path guardrails on handoff |
| `MAC_MAX_RETRY_COUNT` | Integer override for retry cap |
| `MAC_PATH_RULES` | `allowed1,allowed2\|forbidden1,forbidden2` format |
| `MAC_REVIEWER_CAPABILITY` | Capability name required for review actions |
| `MAC_AGENT_TIMEOUT` | Seconds before an online agent is considered stale (default 300) |

When `require_review=True`, `complete_task()` is blocked on `running` tasks. Use `mark_review_ready()` → `accept_review()`/`reject_review()` instead.

---

## `done()` — Advanced Parameters

`Registry.done()` is the single entry point for finishing a task: it submits quality evidence, evaluates the quality gate, saves handoff context, and transitions the task to `completed`, `review_ready`, or `blocked`. Beyond the basics, four optional parameters enable advanced safety and conflict detection.

```python
result = registry.done(
    task_id="task-1",
    agent_id="agent-a",
    quality_result={"command": "pytest -q", "status": "passed"},
    handoff=HandoffResult(
        changed_files=["src/auth.py", "src/login.py"],
        summary="Implement OAuth2 login flow",
    ),
    # Advanced (all optional, all default to False/None):
    enforce_boundaries=True,
    refuse_on_blocking=True,
    detect_conflicts=True,
    guarded_patterns=["src/auth/**", "src/secrets/**"],
    has_changelog=True,
    met_acceptance_criteria=["oauth2_flow", "token_refresh"],
)
```

### `enforce_boundaries` (bool, default `False`)

When `True`, **refuses to save the handoff** if any `changed_files` fall outside the agent's `allowed_paths`/`forbidden_paths` (configured on the `AgentCard`). Returns a `boundary_violation` result with the task still `running`, so the agent can fix the scope before retrying. Defaults to `False` (soft-block only — the handoff is saved with a warning).

### `refuse_on_blocking` (bool, default `False`)

When `True` and a conflict recorded during this call has `severity="blocking"` (i.e., the file matches a `guarded_patterns` entry), returns a `blocking_conflict` result **without transitioning the task**. The handoff is still saved for review, and the conflict is recorded so the next attempt can address it. Defaults to `False` (informational only — task transitions normally).

### `detect_conflicts` (bool, default `False`)

When `True`, after a successful transition to `completed` or `review_ready`, scans the ledger for other tasks whose `changed_files` overlap with this handoff and records `ConflictRecord` entries for each overlap. Defaults to `False` to keep the hot path cheap; opt in when callers want automatic conflict surfacing (e.g., CI webhook bridges).

### `guarded_patterns` (list[str] | None, default `None`)

A list of glob patterns marking high-risk files. When combined with `refuse_on_blocking=True`, any overlap between the handoff's `changed_files` and these patterns produces a `blocking_conflict` severity. Useful for protecting shared infrastructure code, authentication modules, or database schemas from accidental conflicts.

### C-2 Quality Gate: `has_changelog` and `met_acceptance_criteria`

These two parameters strengthen the quality gate for medium/high-risk `TestContract` tasks:

- **`has_changelog`** (bool, default `False`): Whether the agent provided a `CHANGELOG.md` entry. Required for medium-risk tasks; the quality gate fails (task → `blocked`) if missing.
- **`met_acceptance_criteria`** (list[str] | None, default `None`): Subset of the task's acceptance criteria the agent claims to have met. Required for high-risk tasks; the gate fails if the intersection is empty.

---

## What It Can Do

- Coordinate local multi-agent task work through SQLite WAL.
- Register agents with capabilities and optional path boundaries.
- Submit tasks under a plan and express `depends_on` relationships.
- List only tasks whose dependencies are satisfied.
- Prevent claim from taking dependency-blocked work.
- Store structured handoff evidence separately from the task row.
- Record and resolve conflicts.
- Generate worker and review Markdown packets for human-mediated agent handoff.
- Enforce risk-based quality evidence before completing tasks with a `TestContract`.
- Optional review lifecycle: `mark_review_ready` → `accept_review`/`reject_review` (controlled by `CoordinationPolicy.require_review`).
- Reviewer capability validation: `accept_review`/`reject_review` enforce `CoordinationPolicy.reviewer_capability`.
- Review packets include quality evidence summary; worker packets inline upstream handoff context.
- Task TTL expiry: `expire_stale_tasks()` transitions stale tasks to `failed` with `TTL_EXPIRED`.
- One-shot `mac-agent next` command: claim + start + output worker packet atomically.
- One-shot `mac-agent done` command: quality + handoff + complete/review-ready atomically.
- `done()` advanced parameters: `enforce_boundaries` (hard-refuse path violations), `refuse_on_blocking` (block task on guarded-pattern conflicts), `detect_conflicts` (auto-scan for overlapping changed_files), `guarded_patterns` (glob-based high-risk file protection).
- C-2 quality gate: `has_changelog` (required for medium-risk tasks) and `met_acceptance_criteria` (required for high-risk tasks).
- Auto-retry on TTL expiry: `expire_stale_tasks(auto_retry=True)` resets tasks with retries remaining.
- Agent heartbeat expiry: `expire_stale_agents()` auto-offlines stale agents.
- `mac-agent dashboard` command: one-command project overview.
- CLI structured logging with `--verbose` / `--quiet` flags.
- Expose 6 aggregate metrics for observability (cycle time, handoff/quality pass rates, retry/conflict rates, active agents).

## What It Cannot Do Yet

- It does not automatically launch external AI tools.
- It does not stream logs or terminal sessions.
- It does not provide Redis, Postgres, or cloud sync.
- It does not solve conflicts automatically.
- It does not replace MCP, LangGraph, CrewAI, pytest, or CI.

---

## Architecture

```text
src/mac/
  protocol/          Domain models and constants
  storage/           SQLite ledger
  registry.py        Business API: lifecycle, plans, dependencies, handoff, conflicts
  quality/           Risk-based quality gate evaluation
  runner/            Local one-shot runner adapter and templates
  testing/           TestContract and planner
  transport/         FastAPI adapter
  adapters/          IDE adapter implementations (generic, HTTP, lifecycle)
  extensions.py      Plugin API: lifecycle hooks and WebSocket channels
  scoring.py         Pluggable task scoring (sync + async hooks)
  schema_extensions.py  Schema extension facade
  security.py        Security utilities
  metrics.py         Observability aggregation (6 metrics)
  cli.py             Console entry point
  events.py          In-process event bus
  mcp_server.py      MCP Server (31 tools + 4 resources)
```

---

## Testing

```bash
python -m pytest -q
python examples/local_handoff.py
python examples/local_runner.py
python examples/collaboration_plan.py
python -m compileall -q src examples scripts
```

## IDE-agnostic adapters

MAC core does not contain vendor-specific branches for Codex, Claude, Trae, Qoder, WorkBuddy, Cursor, OpenCode, or future tools. Coding tools are adapters selected by capability, not by brand. The built-in `generic-context` adapter works with any tool that can read a Markdown file; third-party adapters can be installed independently through the `mac.adapters` Python entry-point group.

An adapter should expose a manifest and implement `prepare_context(...)`. Optional dispatch, callbacks, MCP, and process-control capabilities can be added without changing the ledger or task protocol:

```toml
[project.entry-points."mac.adapters"]
my-ide = "my_mac_adapter:Adapter"
```

Use the portable context directory as the compatibility boundary:

```text
.agent-context/
  current-task.md
  project-state.json
  decisions.md
  handoffs/
```

This keeps one MAC source of truth while allowing new IDE integrations to be added as separate packages.


## Production delivery: HTTP, sessions, and IDE bootstrap

```bash
pip install "mac-agent[http,mcp]==1.2.0"
mac-agent bootstrap --project-root .
mac-http-server
```

Configure the HTTP authentication and database environment variables before
starting the server. Remote callbacks are bound to durable agent sessions and
stable event IDs: exact replays are idempotent and conflicting payloads return
HTTP 409. Results may be `completed`, `failed`, or `blocked`; blocked results
create a durable blocker and optional handoff target.

See [Installation](docs/INSTALL.md), [IDE and remote integrations](docs/INTEGRATIONS.md),
and the [remote callback example](examples/remote_callback.py).
