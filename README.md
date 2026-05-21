# Multi-Agent Coordinator (MAC)

**Version:** 0.1.3 | **License:** MIT

MAC is a lightweight coordination layer for AI coding agents with a task ledger, context broker, quality gate, and handoff protocol for multi-agent Python development.

## MVP Scope

Phase 1 keeps the system local and inspectable:

- Agent registration and capability discovery
- SQLite task ledger with audit trail
- ContextBundle references instead of large raw payloads
- Risk-based TestContract quality gates
- CLI bridge and direct in-process `Registry` API

Phase 1.1 extends the local MVP without changing its boundaries:

- Observed capability metrics recorded from task outcomes, quality evidence, and audit events
- Automatic TestContract planner that derives a minimal verification contract from task type, risk, and context
- Minimal FastAPI HTTP adapter for local service access to the existing registry operations
- HTTP includes health check, agent lookup, heartbeat refresh, claim 404 behavior, and recovery endpoints

Phase 1.2 adds capability-based task claiming while staying inside the same local ledger model:

- Online agents can actively claim proposed tasks that match their declared capabilities
- Claims respect explicit `target_agent_id` assignments, use CAS from `proposed` to `accepted`, and persist the claimer as `target_agent_id`
- `claim_next_task(best_effort=True)` can rank eligible candidates by the agent's observed capability success rate
- `discover()` ranks matching agents by observed capability success rate before falling back to load
- Successful claims append a `claim_task` audit event
- Claiming is not long polling, WebSocket streaming, Redis/PostgreSQL coordination, gRPC, or Cloud Bridge

Phase 1 also includes a minimal in-process task event bus:

- `TaskEventBus` publishes task write events from `Registry` operations such as submit, claim, start, quality, complete, fail, and reject
- The Phase 1 event bus supports synchronous callbacks and asyncio queue broadcast; Redis or distributed broadcast remains a Phase 2 concern

Phase 1.9 adds minimal failure recovery:

- `record_checkpoint()` stores structured checkpoints on the task metadata and audit trail
- `retry_task()` moves failed tasks back to `proposed`, increments `retry_count`, clears `error_code`, and can assign a fallback agent
- `cancel_task()` marks unfinished tasks as `cancelled` with `TASK_CANCELLED`
- Quality evidence is stamped with `retry_count`; previews and completion only evaluate evidence from the current attempt
- CLI and HTTP expose `checkpoint`, `retry`, `cancel`, and `fail` recovery operations

Phase 1.3 adds a local Agent Adapter Loop for one explicit unit of work:

- `LocalAgentRunner.run_once()` registers an agent, claims one matching task, starts it, runs a configured handler, writes quality evidence, then completes or fails the task
- `command_task_handler()` can run a controlled local command with timeout, cwd, captured output, and structured failure codes
- Task outcomes update observed capability metrics after completion or failure
- Commands are supplied by the adapter configuration, never by task payload content
- The loop is not a daemon, streaming log service, cloud bridge, Redis/Postgres queue, or gRPC worker

Phase 1.4 adds reusable local adapter templates on top of the one-shot runner:

- `LocalAgentTemplate` packages agent identity, one claimed capability, optional project context, metadata, and a configured handler
- `command_agent_template()` and `pytest_agent_template()` create reusable templates for shell-command and pytest validation adapters
- Templates create `LocalAgentRunner` instances; they do not poll, supervise, load plugins, or read executable commands from task payloads
- `mac-agent run-once --command ...` is wired through the command template while preserving the existing CLI behavior

Phase 1.5 adds read-only task visibility for coordination and debugging:

- `Registry.list_tasks()` filters ledger tasks by status, required capability, eligible agent assignment, and project context
- `mac-agent tasks` prints filtered task JSON without mutating task state or audit logs
- HTTP exposes `GET /tasks` and `GET /tasks/{task_id}` on the same local registry service
- Task visibility is not polling, leasing, scheduling, or queue ownership; `claim` remains the state-changing operation

Phase 1.6 adds a read-only task evidence bundle for handoff review:

- `TaskEvidenceBundle` aggregates the task snapshot, quality results, audit trail, execution agent, required capability, and observed capability score
- `Registry.get_task_evidence()`, `mac-agent task-evidence`, and `GET /tasks/{task_id}/evidence` expose the same view
- Evidence bundles are read-only snapshots; they do not mutate task state, write audit events, or replace quality gates

Phase 1.7 adds read-only quality gate previews:

- `QualityGatePreview` shows whether current quality results satisfy the task's TestContract
- `Registry.preview_quality_gate()`, `mac-agent quality-preview`, and `GET /tasks/{task_id}/quality-preview` expose the same view
- Preview output includes required commands/evidence, passed commands/present evidence, missing commands/evidence, and the current allow/block reason
- Previews do not complete, fail, mutate, or write audit events

Phase 1.8 adds read-only task readiness and next-action previews:

- `TaskReadinessReport` shows the current task status, execution agent, required capability, recommended next action, and any blocking reason
- `Registry.preview_task_readiness()`, `mac-agent task-readiness`, and `GET /tasks/{task_id}/readiness` expose the same view
- Running tasks include the current quality gate outcome and missing command/evidence gaps from the quality preview
- Readiness reports do not claim, accept, start, complete, fail, mutate task state, or write audit events

MCP remains the resource/tool/context layer. MAC owns scheduling, handoff, ledger state, and completion gates. gRPC, Redis, PostgreSQL, Cloud Bridge, and Hybrid Bridge are Phase 2 concerns.

## Install

```bash
pip install mac-agent
pip install "mac-agent[http]"
python -m pip install -e ".[dev,http]"
```

Use the base package for local ledger and CLI workflows. Install the `http` extra when using `mac.transport.http_ws` or the FastAPI adapter. The HTTP extra provides the ASGI app factory; run it with the ASGI server already used by your host project.

```bash
mac-agent --help
mac-agent contract --risk low
```

## Development

```bash
python -m pytest -q
python examples/local_handoff.py
python examples/local_runner.py
python -m compileall -q src examples scripts
```

## Release Check

```bash
python -m pip install -e ".[dev,http]"
python -m pytest -q
python examples/local_handoff.py
python examples/local_runner.py
python -m compileall -q src examples scripts
python -m build --no-isolation
python -m twine check dist/*
python scripts/release_smoke.py
```

`scripts/release_smoke.py` validates the built wheel in a temporary venv. By default it reuses already-installed dependencies to avoid package-index flakiness; run `python scripts/release_smoke.py --resolve-deps` in CI or a stable network to verify dependency resolution from package indexes.

## Local Example

```bash
python examples/local_handoff.py
python examples/local_runner.py
```

```python
from mac.runner import pytest_agent_template

template = pytest_agent_template(
    agent_id="pytest-runner",
    name="Pytest Runner",
    pytest_args=["tests", "-q"],
)
runner = template.create_runner(registry=registry)
runner.run_once()
```
