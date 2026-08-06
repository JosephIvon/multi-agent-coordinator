# Scoring Hooks

Scoring hooks are pluggable functions that reorder the result list from `Registry.list_ready_tasks()` and `Registry.alist_ready_tasks()`. Instead of returning tasks in SQL natural order (FIFO by `created_at`), a scorer assigns each task a numeric priority — higher scores float to the top and get claimed first.

---

## Quick Start

```python
from mac.scoring import register_scorer
from mac.registry import Registry

# 1. Define a scorer
def load_aware(task):
    """Deprioritize tasks that have been retried multiple times."""
    return task.priority - (task.retry_count * 0.1)

# 2. Register it
register_scorer("load_aware", load_aware)

# 3. Use it
registry = Registry(ledger, scoring_fn="load_aware")
tasks = registry.list_ready_tasks()  # sorted by load_aware, descending
```

---

## Sync vs Async

| Flavor | Signature | When to use | Registry method |
|--------|-----------|-------------|-----------------|
| **Sync** | `fn(task: TaskTransfer) -> float` | Cheap arithmetic, no I/O | `list_ready_tasks()` |
| **Async** | `async fn(task: TaskTransfer) -> float` | LLM calls, RPC, network | `alist_ready_tasks()` |

Sync is the hot path: `sorted(tasks, key=scorer, reverse=True)` with no asyncio overhead. If you call `list_ready_tasks()` while an async scorer is installed, it raises `TypeError` immediately — no silent fallback.

```python
from mac.scoring import register_async_scorer

async def llm_scorer(task):
    score = await some_llm(f"Rate urgency of: {task.description}")
    return float(score)

register_async_scorer("llm", llm_scorer)

registry = Registry(ledger, scoring_fn="llm")
tasks = await registry.alist_ready_tasks()  # awaits each score in parallel
```

Both registries are separate internally — sync paths never touch asyncio state.

---

## Built-in Scorer

`priority_scorer` is registered as `"priority"` at import time. It returns `task.priority` (default 5, range 1-10). It is always available with no setup:

```python
registry = Registry(ledger, scoring_fn="priority")
```

---

## Registration API

Available from `mac.scoring`:

```python
# Sync
register_scorer(name: str, fn: ScoringFn) -> None
unregister_scorer(name: str) -> bool
get_scorer(name: str) -> ScoringFn | None
list_scorers() -> dict[str, ScoringFn]
clear_scorers() -> None

# Async
register_async_scorer(name: str, fn: AsyncScoringFn) -> None
unregister_async_scorer(name: str) -> bool
get_async_scorer(name: str) -> AsyncScoringFn | None
list_async_scorers() -> dict[str, AsyncScoringFn]
clear_async_scorers() -> None

# Lookup across both registries (sync wins on conflict)
resolve_scorer(name: str) -> Callable | None
is_async_scorer(fn) -> bool
```

Rules:
- `name` must be non-empty. Raises `ValueError`.
- Async scorers must be coroutine functions (`async def`). Raises `ValueError` otherwise.
- Both registries are thread-safe (guarded by `RLock`).

---

## Using with Registry

```python
Registry(
    ledger,
    scoring_fn="my_scorer",           # str (looked up in registry) or callable or None
    scoring_cache_maxsize=1024,       # LRU cache capacity (0 disables)
    scoring_cache_ttl_seconds=300.0,  # TTL per entry in seconds
)
```

Set or clear at runtime:

```python
registry.set_scoring_fn("risk_aware")   # switch to risk-aware
registry.set_scoring_fn(None)           # back to natural order
registry.clear_scoring_cache()          # flush stale scores
info = registry.scoring_cache_info()    # -> {hits, misses, maxsize, currsize, ttl_seconds}
```

Calling `set_scoring_fn()` always clears the cache (stale scores from a different scorer are meaningless).

---

## MCP Tools

Three MCP tools expose scoring at runtime:

### `mac_list_scorers`
Lists all registered sync and async scorers with their qualified names. Use the returned names as input to `mac_set_scorer` or `mac_test_scorer`.

### `mac_set_scorer(name: str | None)`
Installs a named scorer on the long-lived MCP Registry. Pass `null` or empty string to clear. Returns the active scorer ID, whether async/sync is installed, and any error message.

### `mac_test_scorer(name: str, db: str, limit: int, project_context: str | None)`
Dry-runs a scorer against the current ledger's proposed tasks without mutating state. Returns scored tasks with their task IDs, descriptions, and computed scores. Uses a temporary Registry to avoid polluting the long-lived one.

---

## CLI Subcommands

```
mac-agent scoring list              # List all registered scorers (sync + async)
mac-agent scoring test --name NAME  # Dry-run a scorer against proposed tasks
```

`scoring test` options:

| Flag | Default | Description |
|------|---------|-------------|
| `--name` | (required) | Scorer name registered in `mac.scoring` |
| `--db` | `mac_tasks.db` | Path to SQLite ledger |
| `--limit` | 5 | Max tasks to score |
| `--project-context` | None | Extra context string passed to the scorer |

---

## Examples

### Load-Aware Scorer
```python
def load_aware(task):
    # Blockers are stored in the ledger, not on the task object directly.
    # Access them via registry.ledger.list_blockers(task_id) when needed.
    penalty = task.retry_count * 0.2
    return task.priority - penalty

register_scorer("load_aware", load_aware)
```

### Risk-Prioritized Scorer
```python
def risk_first(task):
    base = task.priority
    if hasattr(task, "risk_label"):
        weights = {"critical": 20, "high": 10, "medium": 5, "low": 0}
        base += weights.get(task.risk_label, 0)
    return base

register_scorer("risk_first", risk_first)
```

### LLM-Driven Scorer
```python
async def llm_triage(task):
    prompt = f"Task: {task.description}\nUrgency 1-100:"
    response = await openai_client.completions.create(model="gpt-4o-mini", prompt=prompt)
    return float(response.choices[0].text.strip())

register_async_scorer("llm_triage", llm_triage)
```

---

## LRU+TTL Cache

Async scorers (and sync scorers run through `alist_ready_tasks`) benefit from an LRU cache with TTL eviction. The cache key is `{scorer_id}::{task_id}`, so switching scorers automatically invalidates stale entries.

```python
registry = Registry(
    ledger,
    scoring_fn="llm_triage",
    scoring_cache_maxsize=512,       # store up to 512 entries
    scoring_cache_ttl_seconds=60.0,  # expire after 60 seconds
)

# Monitor cache
info = registry.scoring_cache_info()
# -> {"hits": 47, "misses": 12, "maxsize": 512, "currsize": 12, "ttl_seconds": 60.0}
```

Cache behavior:
- Hits: score is reused if `now - cached_at < ttl`. Entry is moved to end (LRU promotion).
- Misses: scorer runs, result is inserted. If cache exceeds `maxsize`, oldest entry is evicted.
- `maxsize=0` disables caching entirely (every call hits the scorer).
- `set_scoring_fn()` clears the cache because scorer identity changed.
- Sync `list_ready_tasks()` bypasses the cache (sub-ms sort key, caching would be overhead).

---

## Safety

All scorers are wrapped by `_safe_score()`, which catches exceptions, `None` returns, `NaN`, and non-numeric values — returning `0.0` in every failure case. This ensures `sorted()` always receives a total order and never raises.
