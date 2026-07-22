# P3-1: MCP Error Return Structuring Research

**Date:** 2026-07-22
**Status:** Complete (Implemented)
**Scope:** Determine if MAC's MCP tool error format should change

---

## Finding

MCP SDK 1.28.1 fully supports the `isError` field on `CallToolResult`. The original MAC `_safe_call` pattern **broke the MCP error signaling protocol** by returning business errors as normal `isError=False` results.

---

## Original Behavior (Broken)

`_safe_call` caught domain exceptions and returned JSON error strings:

```python
except KeyError as exc:
    return json.dumps({"error": "not_found", "detail": str(exc)})
```

The SDK wrapped this as:

```
CallToolResult(
    content=[TextContent(text='{"error": "not_found", "detail": "..."}')],
    isError=False   # ← WRONG: business error looks like success
)
```

**Impact:** LLM clients (Claude Code, Cursor, etc.) use `isError` to decide retry/strategy. With `isError=False`, they treat `{"error": "not_found"}` as a successful result and may misinterpret it.

---

## MCP SDK Error Mechanisms

### Mechanism A: Raise `ToolError` (Recommended — now implemented)

```python
from mcp.server.fastmcp.exceptions import ToolError

raise ToolError("Not found: task-1")
```

SDK automatically wraps as `CallToolResult(isError=True, content=[TextContent(text="Error executing tool mac_claim_task: Not found: task-1")])`.

### Mechanism B: Return `CallToolResult` directly

```python
from mcp.types import CallToolResult, TextContent

return CallToolResult(
    content=[TextContent(type="text", text="Not found")],
    isError=True,
)
```

More control but adds complexity with no practical benefit over `ToolError`.

---

## Recommendation (Implemented)

**Replace `_safe_call` with `raise ToolError(...)` pattern.** Minimal change, idiomatic MCP:

```python
from mcp.server.fastmcp.exceptions import ToolError

def _safe_call(func: Any) -> str:
    """Execute *func*, catching MAC domain errors and raising ToolError."""
    from pydantic import ValidationError

    try:
        result = func()
    except ToolError:
        raise
    except KeyError as exc:
        raise ToolError(f"not_found: {exc}") from exc
    except ValidationError as exc:
        raise ToolError(f"validation_failed: {exc.errors()}") from exc
    except QualityGateError as exc:
        raise ToolError(f"quality_gate_failed: {exc}") from exc
    except StateConflictError as exc:
        raise ToolError(f"state_conflict: {exc}") from exc
    if result is None:
        raise ToolError("not_found")
    return _serialize(result)
```

---

## Implementation Status

**Implemented by Qoder in commit `1be6457`.** The `_safe_call` now raises `ToolError` for all domain errors, and `_serialize` no longer has the `None → {"error": "not_found"}` branch. The SDK correctly marks these as `isError=True`.

A test fix was applied: `TestToolErrorIsErrorFlag` originally used `async def` + `@pytest.mark.asyncio` (violating the project's no-pytest-asyncio convention per CLAUDE.md §3). Rewritten to use `asyncio.run()` per project convention. All 3 tests pass.

Full suite: **177 passed, 1 skipped** (Windows stdio E2E).

---

## SDK Source References

| Component | File | Line |
|-----------|------|------|
| `CallToolResult.isError` | `mcp/types.py` | 1369 |
| `_make_error_result()` | `mcp/server/lowlevel/server.py` | 473-480 |
| `ToolError` class | `mcp/server/fastmcp/exceptions.py` | 16-17 |
| `Tool.run()` exception wrap | `mcp/server/fastmcp/tools/base.py` | 116-117 |
| `convert_result()` passthrough | `mcp/server/fastmcp/utilities/func_metadata.py` | 114-118 |
