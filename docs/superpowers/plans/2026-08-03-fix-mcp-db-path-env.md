# Fix MAC MCP Server Not Respecting MAC_DB_PATH

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `mac-mcp-server` read `MAC_DB_PATH` from the environment, so that `.claude/settings.json` `env` blocks and shell exports both control which database the MCP server opens.

**Architecture:** Replace the module-level `_DB_PATH = Path("mac.db")` in `mcp_server.py` with a deferred read of `os.environ.get("MAC_DB_PATH", "mac.db")` inside `_registry()` and `_long_registry()`, matching the existing pattern in `schema_extensions.py:116` and `transport/http_ws.py:717`. Add a startup log line that prints the resolved DB path (sanitized — no secrets). Add a test that verifies the env var is honored.

**Tech Stack:** Python stdlib (os.environ, pathlib.Path), pytest, monkeypatch

## Global Constraints

- `requires-python = ">=3.10"`
- No new dependencies
- Must keep existing tests passing (`python -m pytest tests/ -q`)
- Follow existing patterns in `schema_extensions.py` and `http_ws.py` for reading `MAC_DB_PATH`

---

### Task 1: Fix the hardcoded DB path in mcp_server.py

**Files:**
- Modify: `src/mac/mcp_server.py:20-26`
- Modify: `src/mac/mcp_server.py:83-88`
- Modify: `src/mac/mcp_server.py:596-597` (test_scorer path)
- Modify: `src/mac/mcp_server.py:633-637` (main entry point)

**Interfaces:**
- Consumes: `os.environ.get("MAC_DB_PATH", "mac.db")`
- Produces: resolved db path logged on module load, all tool functions use env-aware path

- [ ] **Step 1: Add DB path resolution function and module-level resolve**

At the top of `mcp_server.py`, replace:

```python
from mac import scoring
from mac.protocol.errors import QualityGateError, StateConflictError
from mac.protocol.messages import TaskTransfer
from mac.quality.gate import evaluate_quality_gate
from mac.registry import Registry
from mac.storage.sqlite import SQLiteTaskLedger

mcp = FastMCP("mac-coordinator")

_DB_PATH = Path("mac.db")
```

With:

```python
import os
import logging

from mac import scoring
from mac.protocol.errors import QualityGateError, StateConflictError
from mac.protocol.messages import TaskTransfer
from mac.quality.gate import evaluate_quality_gate
from mac.registry import Registry
from mac.storage.sqlite import SQLiteTaskLedger

logger = logging.getLogger("mac.mcp_server")
mcp = FastMCP("mac-coordinator")


def _resolve_db_path() -> Path:
    """Resolve the SQLite DB path from MAC_DB_PATH env var, or default."""
    raw = os.environ.get("MAC_DB_PATH", "mac.db")
    return Path(raw)
```

- [ ] **Step 2: Replace hardcoded `_DB_PATH` usage in `_registry()` and `_long_registry()`**

Change:

```python
def _registry() -> Registry:
    """Create a Registry backed by the default SQLite ledger."""
    return Registry(SQLiteTaskLedger(_DB_PATH))
```

To:

```python
def _registry() -> Registry:
    """Create a Registry backed by the default SQLite ledger."""
    return Registry(SQLiteTaskLedger(_resolve_db_path()))
```

And change `_long_registry()`:

```python
def _long_registry() -> Registry:
    # Memoised Registry used by mac_set_scorer / mac_list_scorers / etc.
    global _LONG_REGISTRY
    if _LONG_REGISTRY is None:
        _LONG_REGISTRY = Registry(SQLiteTaskLedger(_resolve_db_path()))
    return _LONG_REGISTRY
```

- [ ] **Step 3: Fix mac_test_scorer's direct _DB_PATH reference**

Change `mac_test_scorer`'s `_do()` closure (line ~597):

From:
```python
test_registry = _Registry(_Ledger(_DB_PATH), scoring_fn=name)
```

To:
```python
test_registry = _Registry(_Ledger(_resolve_db_path()), scoring_fn=name)
```

- [ ] **Step 4: Add startup log line to `main()`**

Change `main()` to:

```python
def main() -> None:
    """Run the MCP server over stdio transport."""
    resolved = _resolve_db_path()
    logger.info("mac-mcp-server starting with DB: %s (MAC_DB_PATH=%s)", 
                resolved, os.environ.get("MAC_DB_PATH", "<unset>"))
    # Print to stderr so it's visible in Claude Code's MCP server log
    import sys
    print(f"[mac-mcp-server] DB path: {resolved.absolute()}", file=sys.stderr)
    mcp.run()
```

- [ ] **Step 5: Verify existing tests still pass**

Run: `python -m pytest tests/ -q`
Expected: PASS (~251 tests, no regressions)

- [ ] **Step 6: Commit**

```bash
git add src/mac/mcp_server.py
git commit -m "fix(mcp): read MAC_DB_PATH from env, not hardcoded cwd default

mcp_server.py had _DB_PATH = Path('mac.db') at module load time,
ignoring the MAC_DB_PATH env var entirely.  schema_extensions.py and
http_ws.py already read this env var correctly; this commit brings the
MCP server in line.

Resolved DB path is logged to stderr on startup so operators can
verify which ledger is in use.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Add test for env var behavior

**Files:**
- Create: `tests/test_mcp_db_path.py`

**Interfaces:**
- Consumes: `mac.mcp_server._resolve_db_path`, `os.environ`
- Produces: test coverage verifying `MAC_DB_PATH` env var is honored

- [ ] **Step 1: Write the test file**

```python
"""Tests for MAC_DB_PATH env var resolution in mcp_server."""
from __future__ import annotations

import os
from pathlib import Path

from mac.mcp_server import _resolve_db_path


def test_resolve_db_path_env_var_set(monkeypatch):
    """_resolve_db_path should use MAC_DB_PATH env var when set."""
    monkeypatch.setenv("MAC_DB_PATH", "/custom/path/mac_custom.db")
    result = _resolve_db_path()
    assert result == Path("/custom/path/mac_custom.db")


def test_resolve_db_path_env_var_unset(monkeypatch):
    """_resolve_db_path should fall back to 'mac.db' when env var is absent."""
    monkeypatch.delenv("MAC_DB_PATH", raising=False)
    result = _resolve_db_path()
    assert result == Path("mac.db")


def test_resolve_db_path_absolute_path(monkeypatch):
    """_resolve_db_path should handle absolute paths correctly."""
    monkeypatch.setenv("MAC_DB_PATH", r"C:\tmp\collab-smoke\mac.db")
    result = _resolve_db_path()
    assert result == Path(r"C:\tmp\collab-smoke\mac.db")


def test_resolve_db_path_does_not_yield_none(monkeypatch):
    """_resolve_db_path must never return None regardless of env state."""
    monkeypatch.delenv("MAC_DB_PATH", raising=False)
    result = _resolve_db_path()
    assert result is not None
    assert isinstance(result, Path)
```

- [ ] **Step 2: Run the new tests**

Run: `python -m pytest tests/test_mcp_db_path.py -v`
Expected: 4 PASS

- [ ] **Step 3: Run full test suite**

Run: `python -m pytest tests/ -q`
Expected: all tests pass (~255)

- [ ] **Step 4: Commit**

```bash
git add tests/test_mcp_db_path.py
git commit -m "test(mcp): verify MAC_DB_PATH env var resolution

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: Add startup self-check that prints resolved DB path

**Files:**
- Modify: `src/mac/mcp_server.py:633-637`

**Note:** This is already done in Task 1 Step 4. If Task 1 was done, skip this task.

- [ ] **Step 1: Verify main() has the startup log line**

Check `src/mac/mcp_server.py:main()` contains:

```python
print(f"[mac-mcp-server] DB path: {resolved.absolute()}", file=sys.stderr)
```

- [ ] **Step 2: Verify existing tests still pass**

Run: `python -m pytest tests/ -q`
Expected: PASS

---
