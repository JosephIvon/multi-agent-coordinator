# MAC Project Standards — v1.0

> 版本：1.0
> 日期：2026-05-22
> 状态：已批准

---

## 1. 定位与目标

**Purpose**: Keep README, SPEC, and CLAUDE.md synchronized with actual code state. Establish soft-commitment coding conventions that any AI IDE/agent can follow without CI enforcement.

**Target users**: Human developers, AI coding agents (Claude Code, Codex, Cursor, Trae, etc.)

---

## 2. README + SPEC 同步规则

### 2.1 三份文档职责边界

| Document | Role | Language | Update Frequency |
|----------|------|----------|-----------------|
| `README.md` | 用户入口：install、快速上手、API 示例 | 中英对照（上英下中） | On every release |
| `docs/SPEC.md` | 架构规范：设计决策、状态机、端点、Phase 状态 | 中文 | On every release |
| `CLAUDE.md` | AI agent 指南：目录结构、约定、Phase 进度 | 英文（技术规范） | On every release |

**同步规则**: Any code change that modifies public API, CLI commands, HTTP endpoints, or architecture MUST update all three documents in the same commit. No document shall reference code that does not exist.

### 2.2 README 中英对照格式

Each section follows **top-English / bottom-Chinese** pattern. English is authoritative for technical accuracy; Chinese provides accessibility.

```markdown
## Install

```bash
pip install mac-agent
```

## 安装

```bash
pip install mac-agent
```
```

Rule: Never mix languages within a single section. Each section is language-pure.

---

## 3. 编程约定（软性指导）

These conventions live in `CLAUDE.md`. AI agents are expected to follow them; violations do not trigger CI failure.

### 3.1 Type Hints

- **Full explicit** annotations on all public functions and methods
- Python 3.10+ native union syntax: `str | None`, `int | None`, `list[str]`
- No `Optional[X]` — use `X | None` instead
- Return types required: `-> None`, `-> TaskTransfer`, etc.
- Generic types fully specified: `dict[str, Any]`, `list[AgentCard]`

```python
# Correct
def get_agent(self, agent_id: str) -> AgentCard | None: ...

# Incorrect
def get_agent(self, agent_id: str): ...          # missing return type
def get_agent(self, agent_id: str) -> Optional: ...  # bad Optional usage
```

### 3.2 Error Handling

- Custom exception hierarchy rooted at `MACError` (in `protocol/errors.py`)
- Domain errors: `StateConflictError`, `QualityGateError`, `TaskExpiredError`, `MaxHopsExceededError`
- Storage errors: `StatusConflict` (SQLite CAS failure, separate from domain errors)
- No bare `raise Exception` — use existing domain exceptions or create new ones in `errors.py`
- No silent `except:` — always catch specific exception types

```python
# Correct
except StatusConflict as exc:
    raise StateConflictError(str(exc)) from exc

# Incorrect
except Exception:
    pass
```

### 3.3 Docstrings

- **Required** on all public classes and functions in `src/mac/` (not tests)
- Format: Google style (`:param:`, `:returns:`, `:raises:`)
- One-line summary for simple methods; full documentation for complex ones
- Private methods (`_method`): optional, simple description only

```python
def accept_handoff(self, task_id: str, agent_id: str) -> TaskTransfer:
    """Accept a proposed task handoff.

    :param task_id: ID of the task to accept
    :param agent_id: ID of the agent accepting the handoff
    :returns: Updated TaskTransfer with status='accepted'
    :raises StateConflictError: if task is not in 'proposed' status
    """
```

### 3.4 Module Structure

- **Maximum 300 lines** per `.py` file; split when exceeded
- `from __future__ import annotations` at top of every file
- Imports grouped: stdlib → third-party → local (`mac.*`)
- Private helpers (`_func`) placed after public API
- No `__all__` required

```python
from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from mac.protocol.errors import MACError
from mac.registry import Registry
```

### 3.5 Naming Conventions

| Element | Convention | Example |
|---------|------------|---------|
| Modules | lowercase, no separator | `sqlite.py`, `gate.py` |
| Packages | lowercase, single word | `protocol/`, `runner/` |
| Classes | PascalCase | `TaskTransfer`, `AgentCard` |
| Functions/methods | snake_case | `claim_next_task`, `_audit` |
| Constants | UPPER_SNAKE | `_HIGH_RISK_SIGNALS` |
| Test functions | `test_<what>` | `test_registry_discovers_agents` |
| Test files | `test_<subject>.py` | `test_registry.py` |

### 3.6 Test Conventions

- Flat function structure, no class wrappers (`TestClass` pattern discouraged)
- Long descriptive names: `test_<module>_<scenario>_<expected_behavior>`
- Use built-in pytest fixtures (`tmp_path`, `capsys`)
- Helper setup functions at module level (prefix `_`)
- No external mock libraries required (standard pytest)

```python
def test_registry_claim_next_task_returns_none_when_no_proposed_tasks():
    registry = _registry_empty()
    result = registry.claim_next_task(agent_id="any", capability="any")
    assert result is None
```

### 3.7 Prohibited Patterns

| Pattern | Reason | Alternative |
|---------|--------|-------------|
| `TODO`, `FIXME`, `COMPLETE ME` | Indicates incomplete work | Finish before committing |
| `[...]` placeholder in code | Hallmark of AI skeleton code | Implement or remove |
| `Optional[X]` without `None` default | Python 3.10+ style | `X \| None` |
| Bare `except:` | Swallows all errors | `except SomeError:` |
| Docstring with `"""<缺的>"""` | Unfinished template | Complete or remove |
| Magic numbers without constant | Hard to maintain | Named constant at top |

---

## 4. Document Update Checklist

When any code change touches public API, run this checklist:

- [ ] `README.md` — install command, CLI example, or API example updated
- [ ] `docs/SPEC.md` — Phase status version bumped; new endpoints documented
- [ ] `CLAUDE.md` — architecture diagram, CLI command table, Phase status updated
- [ ] Tests pass: `python -m pytest -q`
- [ ] No stray `.pyc`, `*.db`, `dist/` files

---

## 5. Codex Implementation Review

**Finding: Codex implementation is architecturally sound.**

The codebase does not exhibit AI-generated code signatures:
- No `TODO`/`FIXME`/`[...]` placeholders
- No repetitive boilerplate blocks
- No malformed structures or hallucinated patterns
- Consistent architectural patterns throughout (`_audit`/`_publish` pair, `_transition` as single mutation primitive)

**Architectural quality markers**:
- `Registry` as single coordination layer — clean separation of concerns
- `storage/` isolates persistence; `protocol/` owns domain models
- CAS pattern for optimistic concurrency (`StatusConflict`)
- `TaskRunResult.passed()` / `.failed()` factory methods
- `LocalAgentTemplate` + `LocalAgentRunner` adapter pattern correctly implemented

**One structural issue noted**: `storage/models.py` (dataclass) coexists with `protocol/messages.py` (Pydantic). This was cleaned in v0.1.4 — `storage/models.py` is now deprecated but still imported by two test files (`test_storage.py`, `test_registry.py`). It should be removed in a future cleanup pass.

---

## 6. File Layout (Current)

```
src/mac/
├── __init__.py          # __version__ = "0.2.0"
├── cli.py               # Entry point, lifecycle and collaboration subcommands
├── events.py            # TaskEventBus
├── protocol/
│   ├── __init__.py
│   ├── constants.py
│   ├── errors.py        # MACError hierarchy
│   └── messages.py      # Pydantic models (authoritative)
├── quality/
│   ├── __init__.py
│   └── gate.py          # evaluate_quality_gate()
├── registry.py          # Central coordinator
├── runner/
│   ├── __init__.py
│   ├── local.py         # LocalAgentRunner, TaskRunResult, command_task_handler
│   └── templates.py     # LocalAgentTemplate, command_agent_template, pytest_agent_template
├── storage/
│   ├── __init__.py
│   ├── models.py        # DEPRECATED dataclass models (残留)
│   └── sqlite.py        # SQLiteTaskLedger
├── testing/
│   ├── __init__.py
│   ├── contracts.py     # TestContract, RiskLevel
│   └── planner.py       # plan_test_contract()
└── transport/
    ├── __init__.py
    └── http_ws.py       # FastAPI app factory

tests/                   # 54 test files, flat structure
examples/
scripts/
docs/
├── SPEC.md              # 架构规范（中文）
└── superpowers/specs/   # Design specs
CLAUDE.md                # AI agent guide (英文)
README.md                # 中英对照用户入口
pyproject.toml
```

---

*本规范为软性指导，违反不触发 CI 失败。AI agents 应将其作为编程标准参考。*
