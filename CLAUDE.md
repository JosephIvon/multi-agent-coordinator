# MAC — AI Agent Guide

**Version:** 0.2.0
**Language:** English (technical authority)
**Last updated:** 2026-05-22

---

## Project Overview

MAC is a lightweight coordination layer for AI coding agents — a task ledger, context broker, quality gate, and handoff protocol for multi-agent Python development.

- **Version:** 0.2.0 (see `src/mac/__init__.py` and `pyproject.toml`)
- **License:** MIT
- **Python:** >=3.10

---

## Architecture

```
src/mac/
├── __init__.py          # __version__ = "0.2.0"
├── cli.py               # Entry point: lifecycle + collaboration subcommands
├── events.py            # TaskEventBus (sync + asyncio queue)
├── protocol/
│   ├── __init__.py
│   ├── constants.py
│   ├── errors.py        # MACError hierarchy (StateConflictError, QualityGateError, etc.)
│   └── messages.py      # Pydantic models — AUTHORITATIVE (Plan, TaskTransfer, HandoffResult, ConflictRecord)
├── quality/
│   ├── __init__.py
│   └── gate.py          # evaluate_quality_gate()
├── registry.py          # Central coordinator — all business logic
├── runner/
│   ├── __init__.py
│   ├── local.py         # LocalAgentRunner, TaskRunResult, command_task_handler
│   └── templates.py     # LocalAgentTemplate, command_agent_template, pytest_agent_template
├── storage/
│   ├── __init__.py
│   ├── models.py        # DEPRECATED dataclass models — do not use for new code
│   └── sqlite.py        # SQLiteTaskLedger, StatusConflict (CAS error)
├── testing/
│   ├── __init__.py
│   ├── contracts.py     # TestContract.for_risk(), RiskLevel
│   └── planner.py       # plan_test_contract()
└── transport/
    ├── __init__.py
    └── http_ws.py       # FastAPI app factory, create_app()
```

**Key principle:** `protocol/messages.py` is the authoritative model layer. `storage/models.py` is deprecated.

---

## Coding Conventions

### Type Hints
- **Full explicit** annotations on all public functions/methods
- Python 3.10+ native union syntax: `str | None`, `int | None`, `list[str]`
- **No** `Optional[X]` — use `X | None` instead
- Return types required

```python
# Correct
def get_agent(self, agent_id: str) -> AgentCard | None: ...

# Incorrect
def get_agent(self, agent_id: str): ...           # missing return type
def get_agent(self, agent_id: str) -> Optional: ...  # bad style
```

### Error Handling
- Custom exception hierarchy in `protocol/errors.py`
- Base: `MACError(RuntimeError)` → `StateConflictError`, `QualityGateError`, `TaskExpiredError`, `MaxHopsExceededError`
- `StatusConflict` (in `storage/sqlite.py`) is the SQLite CAS error — different from domain errors
- No bare `except:` — catch specific types
- No silent `pass` in exception handlers

```python
# Correct
except StatusConflict as exc:
    raise StateConflictError(str(exc)) from exc

# Incorrect
except Exception:
    pass
```

### Docstrings
- **Required** on all public classes and functions in `src/mac/`
- Google style (`:param:`, `:returns:`, `:raises:`)
- Private methods (`_method`): optional, simple one-line summary fine

```python
def accept_handoff(self, task_id: str, agent_id: str) -> TaskTransfer:
    """Accept a proposed task handoff.

    :param task_id: ID of the task to accept
    :param agent_id: ID of the agent accepting the handoff
    :returns: Updated TaskTransfer with status='accepted'
    :raises StateConflictError: if task is not in 'proposed' status
    """
```

### Module Structure
- `from __future__ import annotations` at top of every file
- Maximum **300 lines** per `.py` file; split when exceeded
- Imports ordered: stdlib → third-party → local (`mac.*`)
- Private helpers (`_func`) placed after public API
- No `__all__` required

### Naming

| Element | Convention | Example |
|---------|------------|---------|
| Modules | lowercase, no separator | `sqlite.py`, `gate.py` |
| Packages | lowercase, single word | `protocol/`, `runner/` |
| Classes | PascalCase | `TaskTransfer`, `AgentCard` |
| Functions/methods | snake_case | `claim_next_task`, `_audit` |
| Constants | UPPER_SNAKE | `_HIGH_RISK_SIGNALS` |
| Test functions | `test_<what>` | `test_registry_discovers_agents` |
| Test files | `test_<subject>.py` | `test_registry.py` |

### Prohibited Patterns
- `TODO`, `FIXME`, `COMPLETE ME` — finish work before committing
- `[...]` placeholder — implement or remove
- `Optional[X]` without `None` default — use `X | None`
- Bare `except:` — catch specific types
- `"""<missing>"""` docstring template — complete or remove
- Magic numbers — use named constants

---

## State Machine

```
proposed → accepted → running → completed
    ↓          ↓           ↓
  rejected   rejected    failed
```

Also: `cancelled`, `superseded`.

---

## CLI Commands (25 subcommands)

`mac-agent <command> [options]`

| Command | Description |
|---------|-------------|
| `contract --risk {low,medium,high}` | Generate risk-based TestContract |
| `register --agent-id ... --name ... --capability ... [--allowed-path ...] [--forbidden-path ...]` | Register agent |
| `discover --capability ...` | Find agents by capability |
| `submit --task-id ... --type ... --summary ... [--plan-id ...] [--depends-on ...] [--risk ...]` | Submit task |
| `status --task-id ...` | Print task status |
| `tasks [--status] [--capability] [--agent-id] [--project-context]` | List tasks (read-only) |
| `plan create --plan-id ... --goal ... --created-by ...` | Create collaboration plan |
| `plan activate --plan-id ...` | Activate plan |
| `plan close --plan-id ... [--status completed\|cancelled]` | Close plan |
| `plan list` | List plans |
| `ready-tasks [--capability ...]` | List dependency-unblocked proposed tasks |
| `handoff --task-id ... --agent-id ... [--plan-id ...] [--verification ...] [--changed-file ...] [--risk ...]` | Save or print structured handoff |
| `record-conflict --source ... --description ... [--plan-id ...] [--task-id ...] [--severity ...]` | Record a conflict |
| `conflicts [--plan-id ...] [--resolved] [--unresolved]` | List conflicts |
| `resolve-conflict --conflict-id ... --resolution ...` | Resolve a conflict |
| `worker-packet --task-id ... --agent-id ...` | Print worker packet (Markdown) |
| `review-packet --task-id ...` | Print review packet (Markdown) |
| `task-evidence --task-id ...` | Print task evidence bundle (read-only) |
| `quality-preview --task-id ...` | Preview quality gate (read-only) |
| `task-readiness --task-id ...` | Preview next action (read-only) |
| `accept --task-id ... --agent-id ...` | Accept handoff |
| `start --task-id ... --agent-id ...` | Mark task running |
| `quality --task-id ... --command ... --status passed\|failed --evidence ...` | Record quality evidence |
| `complete --task-id ... --agent-id ...` | Complete after quality gate passes |
| `fail --task-id ... --agent-id ... --error-code ...` | Fail task |
| `audit --trace-id ...` | Print audit trail |
| `claim --agent-id ... --capability ... [--best-effort]` | Claim next proposed task |
| `run-once --agent-id ... --name ... --capability ... --command ...` | Run one adapter loop |
| `observe --agent-id ... --capability ... --task-type ... --status ... --duration ...` | Record observed outcome |
| `capability-score --agent-id ... --capability ...` | Print observed capability score |
| `checkpoint --task-id ... --agent-id ... --summary ...` | Record recovery checkpoint |
| `retry --task-id ... --agent-id ... [--fallback-agent-id ...]` | Retry failed task |
| `cancel --task-id ... --agent-id ... [--reason ...]` | Cancel task |

---

## Phase Status

- **Phase 1.0–1.8**: Complete. Local MVP with SQLite ledger, CLI, HTTP, in-process adapter, task claiming, adapter templates, task visibility, evidence bundles, quality gate preview, and task readiness preview.
- **Phase 1.9** (integrated): Failure recovery (`checkpoint`, `retry`, `cancel`), TaskEventBus.
- **Phase A** (v0.2.0, current): Collaboration layer — Plan management, `depends_on` dependency tracking, `list_ready_tasks()`, `HandoffResult`, `ConflictRecord`, `PathRule`/`CoordinationPolicy`, `prepare_worker_packet()`/`prepare_review_packet()`, path guardrails, CLI collaboration commands (`ready-tasks`, `handoff`, `conflicts`, `record-conflict`, `resolve-conflict`, `worker-packet`, `review-packet`).
- **Phase 2** (deferred): gRPC, Redis Pub/Sub, PostgreSQL, Cloud Bridge, Hybrid Bridge.

---

## Key Design Decisions

### A2A/MCP/MAC Boundary
- **MCP**: resources, tools, context URIs. MAC does not replace it.
- **A2A-compatible**: `TaskTransfer` has `task_id`, `trace_id`, status, summary, execution agent.
- **MAC**: scheduling (capability/load/affinity), ledger (SQLite WAL + CAS), handoff (ContextBundle + TestContract), quality gate.

### Read-Only Operations (no audit, no state mutation)
- `list_tasks()`
- `get_task_evidence()`
- `preview_quality_gate()`
- `preview_task_readiness()`

### Risk-Based TestContract
- **low**: `pytest related tests or smoke test`, evidence: `test_output`
- **medium**: `python -m pytest tests`, evidence: `test_output`, `changed_files`
- **high**: `python -m pytest --cov` (hard requirement), evidence: `test_output`, `coverage_report`, `review_notes`

---

## What MAC Is Not

- Not an MCP replacement (MCP handles resources/tools)
- Not a LangGraph/CrewAI replacement (MAC is a lightweight handoff protocol, not an execution engine)
- Not a task queue (claim is a one-shot state transition, not a lease or long-poll)
- Not a test framework (TestContract specifies what evidence is required; actual tests run elsewhere)
- Not a streaming/log service (adapter loops are one-shot with captured stdout/stderr)

---

## Known Constraints

- SQLite WAL for single-instance; multi-instance strong consistency in Phase 2 PostgreSQL
- ContextBundle quality determines handoff quality; MAC enforces structure, not comprehension
- Quality Gate checks evidence vs. contract; it does not judge test quality
- Observed capability metrics are observations, not credentials or SLAs

---

## Testing

```bash
python -m pytest -q          # 118 tests
python examples/local_handoff.py
python examples/local_runner.py
python -m compileall -q src examples
```

---

## Document Update Rule

Any code change that modifies public API, CLI commands, HTTP endpoints, or architecture **MUST** update all three documents in the same commit:

- `README.md` — install, quick start, API example (English section + Chinese section)
- `docs/SPEC.md` — architecture, endpoints, phase status
- `CLAUDE.md` — this file

No document shall reference code that does not exist.

Documents are organized by language: English content first, Chinese content after the `====` separator. Both sections must be kept in sync.