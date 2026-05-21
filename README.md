# Multi-Agent Coordinator (MAC)

**Version:** 0.1.4 | **License:** MIT

MAC is a lightweight coordination layer for AI coding agents — a task ledger, context broker, quality gate, and handoff protocol for multi-agent Python development.

## Install

```bash
pip install mac-agent                     # CLI + local ledger
pip install "mac-agent[http]"            # + FastAPI HTTP adapter
pip install -e ".[dev]"                   # development with tests
```

## Quick Start

```bash
mac-agent register --agent-id claude --name Claude --capability write_code
mac-agent submit --task-id t1 --source-agent-id alice --type write_code --summary "Add auth handler"
mac-agent tasks --status proposed
mac-agent claim --agent-id claude --capability write_code
mac-agent start --task-id t1 --agent-id claude
mac-agent complete --task-id t1 --agent-id claude
```

## Python API

```python
from mac.registry import Registry
from mac.storage.sqlite import SQLiteStorage
from mac.protocol.messages import AgentCard, AgentCapability, TaskTransfer, TaskPayload, ContextBundle

registry = Registry(SQLiteStorage("mac.db"))

# Register agent
agent = AgentCard(agent_id="worker-1", name="Worker", capabilities=[AgentCapability(name="write_code")])
registry.register(agent)

# Submit task
task = TaskTransfer(task_id="t1", source_agent_id="alice", payload=TaskPayload(type="write_code", summary="Write auth handler"))
registry.submit_task(task)

# Claim and complete
task = registry.claim_next_task(agent_id="worker-1", capability="write_code")
registry.start_task(task.task_id, "worker-1")
# ... run work, submit quality results ...
registry.complete_task(task.task_id, "worker-1")
```

## HTTP Adapter

```python
from mac.transport.http_ws import create_app
from mac.registry import Registry
from mac.storage.sqlite import SQLiteStorage

app = create_app(Registry(SQLiteStorage("mac.db")))
# Run with uvicorn: uvicorn app:app --port 8000
```

Endpoints: `GET /` (health), `GET/POST /agents`, `GET /agents/{id}`, `POST /agents/heartbeat`, `GET/POST /tasks`, `GET /tasks/{id}/evidence`, `GET /tasks/{id}/quality-preview`, `GET /tasks/{id}/readiness`, and state transition endpoints (`/claim`, `/accept`, `/start`, `/complete`, `/fail`, `/checkpoint`, `/retry`, `/cancel`).

## Examples

```bash
python examples/local_handoff.py   # two-agent handoff via Registry
python examples/local_runner.py    # LocalAgentRunner adapter loop
```