# Multi-Agent Coordinator (MAC)

**Version:** 0.2.0 | **License:** MIT

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
  cli.py             Console entry point
  events.py          In-process event bus
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
