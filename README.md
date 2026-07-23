# Multi-Agent Coordinator (MAC)

**Version:** 0.6.0 | **License:** MIT

MAC is a lightweight local coordination layer for AI coding agents. It gives multiple agents a shared ledger for tasks, plans, context handoff, quality evidence, conflict records, and review packets.

It is useful when you use several AI coding tools in the same project and need one place to answer:

- What tasks exist, and who claimed them?
- Which tasks are blocked by unfinished upstream work?
- What did the previous agent change, verify, and leave risky?
- What conflicts or path-boundary issues need human review?
- What prompt packet should I give to the next worker or reviewer agent?

MAC is not an execution engine. It does not run Claude, Codex, Trae, GLM, or other agents for you. It coordinates their work through a local SQLite ledger and CLI/HTTP/API surfaces.

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
mac-agent claim --agent-id claude --capability write_code
mac-agent start --task-id t1 --agent-id claude
mac-agent complete --task-id t1 --agent-id claude
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
| `POST` | `/agents/expire-stale` | Set offline agents with stale heartbeats |

---

## MCP Server

MAC exposes its coordination API as an MCP (Model Context Protocol) server, so AI coding tools like Claude Code, Cursor, and Windsurf can call MAC natively.

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

### Available Tools (14)

| Tool | Purpose | Side Effect |
|------|---------|-------------|
| `mac_submit_task` | Submit a task (full TaskTransfer dict) | write |
| `mac_claim_task` | Claim + start a task (atomic) | write |
| `mac_record_quality_and_complete` | Submit evidence + auto-complete on gate pass | write |
| `mac_fail_task` | Mark task as failed | write |
| `mac_save_handoff` | Save structured handoff result | write |
| `mac_list_ready_tasks` | List claimable tasks | read-only |
| `mac_review_packet` | Generate reviewer prompt (Markdown) | read-only |
| `mac_worker_packet` | Generate worker prompt (Markdown) | read-only |
| `mac_mark_review_ready` | Move task to review_ready (requires `require_review=True`) | write |
| `mac_accept_review` | Accept reviewed task → completed | write |
| `mac_reject_review` | Reject reviewed task → rejected (auto-records conflict) | write |
| `mac_expire_stale_tasks` | Expire non-terminal tasks past TTL → failed | write |
| `mac_next_task` | Claim + start + output worker packet (atomic) | write |
| `mac_expire_stale_agents` | Set offline agents with stale heartbeats | write |

### Available Resources (2)

| URI | Description |
|-----|-------------|
| `mac://capabilities` | Agent capability registry |
| `mac://health` | Health summary (open tasks, inflight agents) |

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
- Auto-retry on TTL expiry: `expire_stale_tasks(auto_retry=True)` resets tasks with retries remaining.
- Agent heartbeat expiry: `expire_stale_agents()` auto-offlines stale agents.
- `mac-agent dashboard` command: one-command project overview.
- CLI structured logging with `--verbose` / `--quiet` flags.
- Expose 6 aggregate metrics for observability (cycle time, handoff/quality pass rates, retry/conflict rates, active agents).

## What It Cannot Do Yet

- It does not automatically launch external AI tools.
- It does not stream logs or terminal sessions.
- It does not provide leases, distributed locks, Redis, Postgres, or cloud sync.
- It does not implement full review lifecycle states by default.
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
  metrics.py         Observability aggregation (6 metrics)
  cli.py             Console entry point
  events.py          In-process event bus
  mcp_server.py      MCP Server (14 tools + 2 resources)
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
