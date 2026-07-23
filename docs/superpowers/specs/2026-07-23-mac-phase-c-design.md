# MAC Phase C — Design Document

> Version: 0.1 (draft)
> Date: 2026-07-23
> Status: draft — awaiting review before implementation
> Prerequisite: Phase B complete (v0.5.0), PyPI published, CI active

---

## 0. Motivation

Phase A (v0.4.0) built the core coordination ledger. Phase B (v0.5.0) filled
E2E-validated protocol gaps (quality evidence in packets, upstream handoff
inlining, TTL expiry, next command, reviewer capability). MAC is now published
on PyPI with CI running.

The next priority is **production readiness**: making MAC reliable and
observable enough for daily multi-agent work. Phase C focuses on resilience,
operational visibility, and developer ergonomics — not new protocol features.

---

## 1. Scope

### In Scope (Batch 4)

| ID | Feature | Priority | Rationale |
|----|---------|----------|-----------|
| C-1 | Publish workflow (tag-triggered PyPI upload) | High | Eliminate manual publish step; every tag → tested + published automatically |
| C-2 | Retry with TTL expiry | High | `expire_stale_tasks` + auto-retry if `retry_count < max_retry_count`; completes the stuck-task recovery loop |
| C-3 | Agent heartbeat expiry | Medium | Stale agent cards (agent crashed) pollute `discover()` and metrics; auto-offline after timeout |
| C-4 | Structured logging | Medium | Replace `print()` in CLI with `logging` module; add `--verbose` / `--quiet` flags |
| C-5 | `mac-agent status` dashboard command | Medium | One-command overview: active plans, in-flight tasks, stuck tasks, recent conflicts |

### Deferred (Phase D+)

| Feature | Reason |
|---------|--------|
| Parallel groups / DAG visualization | No E2E evidence of need; `depends_on` still sufficient |
| Redis / Postgres / gRPC | SQLite is adequate for local single-workspace usage |
| Automatic conflict resolution | Conflicts should require human judgement by design |
| Web dashboard | UI concern; can be a separate project consuming the HTTP API |
| Multi-instance coordination | Requires distributed consensus; massive scope increase |

---

## 2. Feature Designs

### C-1: Publish Workflow (tag-triggered PyPI upload)

**Problem:** Publishing to PyPI is a manual process (build, twine check, upload).
Human steps can be forgotten or done inconsistently.

**Design:** GitHub Actions workflow triggered by `v*` tag push:

```yaml
name: Publish
on:
  push:
    tags: ['v*']
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - run: pip install build twine
      - run: python -m build
      - run: twine check dist/*
      - run: twine upload dist/*
        env:
          TWINE_API_TOKEN: ${{ secrets.PYPI_API_TOKEN }}
```

**Prerequisite:** Add `PYPI_API_TOKEN` secret to the GitHub repository.

**Impact:** 1 file (`.github/workflows/publish.yml`). No code changes.

---

### C-2: Retry with TTL Expiry

**Problem:** `expire_stale_tasks()` transitions stuck tasks to `failed`, but
doesn't attempt to re-schedule them. If `retry_count < max_retry_count`, the
task could be automatically reset to `proposed` for re-claiming.

**Design:** Add an `auto_retry` parameter to `expire_stale_tasks()`:

```python
def expire_stale_tasks(
    self, *, now: float | None = None, auto_retry: bool = False
) -> list[TaskTransfer]:
    """Transition tasks past their TTL.

    When auto_retry=True and the task has retries remaining, the task is
    reset to ``proposed`` (with incremented retry_count) instead of
    transitioning to ``failed``.
    """
    ...
    for task in candidates:
        if is_stale(task):
            if auto_retry and task.retry_count < self.policy.max_retry_count:
                self._transition(
                    task.task_id, "proposed",
                    expected_status=task.status,
                    agent_id="system",
                    action="retry_task",
                    retry_count=task.retry_count + 1,
                )
            else:
                self.fail_task(
                    task.task_id, "system",
                    error_code="TTL_EXPIRED",
                    message=f"Task exceeded TTL of {task.ttl_seconds}s",
                )
```

**CLI:** `mac-agent expire-stale --db mac.db --auto-retry`

**MCP:** Add `auto_retry: bool = False` parameter to `mac_expire_stale_tasks`.

**HTTP:** Add `?auto_retry=true` query parameter to `POST /tasks/expire-stale`.

**Impact:** ~20 lines in `registry.py`, ~15 lines tests. Backward compatible
(`auto_retry=False` by default preserves current behavior).

---

### C-3: Agent Heartbeat Expiry

**Problem:** When an agent crashes, its `AgentCard` stays `status="online"`
indefinitely. `discover()` returns stale agents, and `active_agents` metric
is inflated.

**Design:** Add `expire_stale_agents()` method:

```python
def expire_stale_agents(
    self, *, timeout_seconds: int = 300, now: float | None = None
) -> list[AgentCard]:
    """Set agents offline if their last_heartbeat is older than timeout.

    Agents with status != 'online' are skipped.
    Returns the list of agents that were expired.
    """
    checkpoint = now or time.time()
    agents = self.ledger.list_agent_cards()
    expired = []
    for agent in agents:
        if agent.status != "online":
            continue
        if agent.last_heartbeat > 0 and (checkpoint - agent.last_heartbeat) > timeout_seconds:
            self.heartbeat_agent(agent.agent_id, status="offline")
            expired.append(agent)
    return expired
```

**CLI:** `mac-agent expire-stale-agents --db mac.db --timeout 300`

**MCP:** `mac_expire_stale_agents(timeout_seconds: int = 300)`

**HTTP:** `POST /agents/expire-stale?timeout_seconds=300`

**Default timeout:** 300 seconds (5 minutes). Configurable via
`MAC_AGENT_TIMEOUT` env var in `CoordinationPolicy`.

**Impact:** ~25 lines in `registry.py`, ~5 lines in `messages.py` (add
`agent_timeout` to `CoordinationPolicy`), ~20 lines tests.

---

### C-4: Structured Logging

**Problem:** CLI uses `print()` for output. There's no way to control
verbosity, and `print()` interleaves with structured output (like `next`
command's JSON header) making it hard for tooling to parse.

**Design:** Replace `print()` with Python `logging` module in `cli.py`:

```python
import logging
logger = logging.getLogger("mac")

# CLI setup
def _setup_logging(verbose: bool = False, quiet: bool = False):
    level = logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO)
    logging.basicConfig(level=level, format="%(message)s")
```

Add `--verbose` / `--quiet` global flags to `mac-agent`.

**Rules:**
- Machine-parseable output (JSON, Markdown packets) goes to **stdout** via `print()`
- Diagnostic messages go to **stderr** via `logger`
- `--verbose` shows debug-level registry operations
- `--quiet` suppresses everything except errors

**Impact:** ~30 lines in `cli.py`. No changes to Registry / storage layer.

---

### C-5: `mac-agent status` Dashboard Command

**Problem:** There's no single command to see the overall project state.
You must run multiple CLI commands (ready-tasks, conflicts, plans, metrics)
to understand what's happening.

**Design:** A `status` command that outputs a concise dashboard:

```bash
$ mac-agent status --db mac.db

MAC Status (mac.db)

Plans:
  plan-1  active  3 tasks (1 completed, 1 running, 1 proposed)

Tasks:
  1 ready to claim
  1 in-flight (running)
  1 stuck past TTL (use expire-stale)

Agents:
  2 online (claude-code, qoder)

Conflicts:
  1 unresolved (reject_review: Docs missing examples)

Metrics:
  cycle_time   0.23s  |  handoff_rate  100%  |  quality_rate  100%
  retry_rate   0%     |  conflict_rate 33%   |  active_agents 2
```

**Implementation:** Orchestrates existing Registry methods:

```python
def cmd_status(args):
    reg = _registry(args)
    plans = reg.list_plans(status="active")
    ready = reg.list_ready_tasks()
    metrics = compute_metrics(reg.ledger)
    conflicts = reg.list_conflicts(resolved=False)
    # Format and print
```

**Impact:** ~50 lines in `cli.py`, ~10 lines tests. No new Registry methods.

---

## 3. Implementation Order

```
C-1 (publish workflow) ─────────────────────┐
C-2 (retry with TTL) ──┐                    ├─► version bump (0.6.0)
C-3 (agent expiry)  ───┤                    │
C-4 (logging)       ───┼─► C-5 (status) ───┘
```

C-1 is independent (CI only). C-2, C-3, C-4 are independent of each other.
C-5 (status dashboard) depends on C-4 (logging) being in place so it can
control output cleanly.

Estimated total: **~140 lines product code + ~80 lines tests**.

---

## 4. Version Target

Phase C ships as **v0.6.0 Alpha**. No breaking changes — all additions are
backward compatible (new parameters default to current behavior, new commands
are additive).

---

## 5. Not in Phase C

Explicitly excluded with rationale:

| Feature | Why excluded |
|---------|-------------|
| Parallel groups | No E2E pain point; `depends_on` handles sequential fan-out |
| DAG visualization | UI concern, not ledger concern |
| Redis / Postgres | Local SQLite is sufficient; distributed storage is Phase D |
| Web dashboard | Separate project consuming the HTTP API |
| Multi-instance | Requires distributed consensus; massive scope increase |
| Auto conflict resolution | Conflicts need human judgement by design |

---

*Design document: MAC Phase C — Production Readiness*
*Informed by Phase B deployment experience and E2E validation findings*
