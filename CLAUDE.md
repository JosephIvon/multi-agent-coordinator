# Multi-Agent Coordinator (MAC)

## Project Overview

MAC is a lightweight coordination layer for AI coding agents — a task ledger, context broker, quality gate, and handoff protocol for multi-agent Python development workflows.

**Version:** 0.1.0
**License:** MIT

## Architecture

```
src/mac/
├── protocol/          # Domain models: messages.py, errors.py, constants.py
├── storage/           # SQLite ledger: sqlite.py, models.py
├── registry.py        # Task lifecycle, agent discovery, claim, quality gate
├── quality/           # Quality gate evaluation: gate.py
├── runner/            # LocalAgentRunner and templates
│   ├── local.py       # LocalAgentRunner.run_once(), TaskRunResult, command_task_handler
│   └── templates.py   # LocalAgentTemplate, command_agent_template, pytest_agent_template
├── testing/           # TestContract, TestContract planner
│   ├── contracts.py   # Risk-based TestContract.for_risk()
│   └── planner.py     # Automatic TestContract generation
├── transport/
│   ├── inprocess.py   # InProcessMAC (same-process adapter)
│   └── http_ws.py     # FastAPI adapter (HTTP endpoints)
├── cli.py             # mac-agent CLI (20 subcommands)
└── __init__.py        # Package entry, version
```

## Key Design Decisions

### A2A/MCP/MAC Boundary
- **MCP**: resources, tools, context URIs. MAC does not replace it.
- **A2A-compatible**: TaskTransfer has `task_id`, `trace_id`, status, summary, execution agent. Profile is mappable to A2A spec.
- **MAC**: scheduling (capability/load/affinity), ledger (SQLite WAL + CAS), handoff (ContextBundle + TestContract), quality gate (evidence vs. contract).

### State Machine
`proposed` → `accepted` → `running` → `completed` / `failed`
Also: `rejected`, `cancelled`, `superseded`.

### Risk-Based TestContract
- **low**: `pytest related tests or smoke test`, evidence: `test_output`
- **medium**: `python -m pytest tests`, evidence: `test_output`, `changed_files`
- **high**: `python -m pytest --cov` (hard requirement), evidence: `test_output`, `coverage_report`, `review_notes`

### Read-Only Operations (no audit, no state mutation)
- `list_tasks()` / `GET /tasks`
- `get_task_evidence()` / `GET /tasks/{id}/evidence`
- `preview_quality_gate()` / `GET /tasks/{id}/quality-preview`
- `preview_task_readiness()` / `GET /tasks/{id}/readiness`

## CLI Commands

| Command | Description |
|---------|-------------|
| `contract --risk {low,medium,high}` | Generate a risk-based TestContract |
| `register --agent-id ... --name ... --capability ...` | Register an agent |
| `discover --capability ...` | Find agents by capability |
| `submit --task-id ... --type ... --summary ...` | Submit a task |
| `status --task-id ...` | Print task status |
| `tasks [--status] [--capability] [--agent-id] [--project-context]` | List tasks (read-only) |
| `task-evidence --task-id ...` | Print task evidence bundle (read-only) |
| `quality-preview --task-id ...` | Preview quality gate (read-only) |
| `task-readiness --task-id ...` | Preview next action (read-only) |
| `accept --task-id ... --agent-id ...` | Accept a handoff |
| `start --task-id ... --agent-id ...` | Mark task running |
| `quality --task-id ... --command ... --status passed|failed --evidence ...` | Record quality evidence |
| `complete --task-id ... --agent-id ...` | Complete after quality gate passes |
| `fail --task-id ... --agent-id ... --error-code ...` | Fail a task |
| `audit --trace-id ...` | Print audit trail |
| `claim --agent-id ... --capability ...` | Claim next proposed task |
| `run-once --agent-id ... --name ... --capability ... --command ...` | Run one adapter loop |
| `observe --agent-id ... --capability ... --task-type ... --status ... --duration ...` | Record observed outcome |
| `capability-score --agent-id ... --capability ...` | Print observed capability score |

## Testing

```bash
python -m pytest -q          # 99 tests
python examples/local_handoff.py
python examples/local_runner.py
python -m compileall -q src examples
```

## Phase Status

- **Phase 1.0–1.8**: Complete. Local MVP with SQLite ledger, CLI, HTTP, in-process adapter, task claiming, adapter templates, task visibility, evidence bundles, quality gate preview, and task readiness preview.
- **Phase 2** (deferred): gRPC, Redis Pub/Sub, PostgreSQL, Cloud Bridge, Hybrid Bridge, capability认证.

## What MAC Is Not

- Not an MCP replacement (MCP handles resources/tools)
- Not a LangGraph/CrewAI replacement (MAC is a lightweight handoff protocol, not an execution engine)
- Not a task queue (claim is a one-shot state transition, not a lease or long-poll)
- Not a test framework (TestContract specifies what evidence is required; actual tests run elsewhere)
- Not a streaming/log service (adapter loops are one-shot with captured stdout/stderr)

## Known Constraints

- SQLite WAL for single-instance; multi-instance strong consistency in Phase 2 PostgreSQL
- ContextBundle quality determines handoff quality; MAC enforces structure, not comprehension
- Quality Gate checks evidence vs. contract; it does not judge test quality
- Observed capability metrics are observations, not credentials or SLAs