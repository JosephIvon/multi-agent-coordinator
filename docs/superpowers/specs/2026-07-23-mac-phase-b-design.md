# MAC Phase B — Design Document

> Version: 0.1 (draft)
> Date: 2026-07-23
> Status: draft — awaiting review before implementation
> Prerequisite: Phase A complete (v0.4.0, Batch 1 + Batch 2)

---

## 0. Motivation

Phase A (v0.4.0) delivers a complete local coordination ledger with task lifecycle,
dependency management, review workflow, metrics, and MCP/HTTP/CLI surfaces. The
E2E validation (`examples/e2e_multi_agent.py`) confirmed all core paths work but
surfaced four protocol gaps that reduce real-world usability:

1. Reviewers can't see quality evidence in review packets
2. Workers must make a separate call to read upstream handoffs
3. No reviewer identity verification
4. No mechanism to recover stuck tasks (agent crash → task stuck in `running`)

Phase B addresses these gaps and adds the highest-value Deferred items based on
real usage experience rather than speculative design.

---

## 1. Scope

### In Scope (Batch 3)

| ID | Feature | Priority | Rationale |
|----|---------|----------|-----------|
| B-1 | Review packet quality evidence | High | E2E blind spot: reviewer can't see coverage/test results |
| B-2 | Worker packet upstream handoff summary | High | E2E blind spot: worker needs 2 calls to get context |
| B-3 | Task TTL / lease expiry | High | Real ops need: crashed agent → stuck `running` task |
| B-4 | `mac-agent next` one-shot command | Medium | Reduces agent friction: claim+start+packet in one call |
| B-5 | Reviewer capability validation | Medium | Prevents unauthorised review acceptance |

### Deferred (Phase C+)

| Feature | Reason |
|---------|--------|
| Parallel groups / DAG visualization | No E2E evidence of need yet; flat `depends_on` sufficient |
| Redis / Postgres / gRPC | Single-workspace SQLite is adequate for local dev |
| Automatic conflict resolution | Conflicts should require human judgement |
| Project-specific role presets | Premature abstraction; capability model is sufficient |
| Daemon workers / auto-scheduling | MAC is a ledger, not an execution engine |

---

## 2. Feature Designs

### B-1: Review Packet Quality Evidence

**Problem:** `prepare_review_packet()` shows handoff but not quality results.
When an agent submits `pytest --cov` evidence before `mark_review_ready`, the
&shy;reviewer cannot see whether coverage goals were met.

**Design:** Add a `## Quality Evidence` section to the review packet between
`HandoffResult` and `Open Conflicts`.

```python
# In prepare_review_packet():
quality_results = self.ledger.get_quality_results(task_id)
if quality_results:
    lines.extend(["", "## Quality Evidence"])
    for qr in quality_results:
        status = qr.get("status", "unknown")
        command = qr.get("command", "unknown")
        evidence = qr.get("evidence", [])
        lines.append(f"- `{command}`: {status}")
        if evidence:
            lines.append(f"  evidence: {', '.join(str(e) for e in evidence)}")
```

**Impact:** ~10 lines in `registry.py`. No schema change. Review packet gets
longer but more useful.

---

### B-2: Worker Packet Upstream Handoff Summary

**Problem:** `prepare_worker_packet()` lists dependency IDs and statuses but
doesn't inline the upstream handoff. A worker claiming task B (depends on A)
must separately call `get_handoff_result("task-A")` to learn what files A
changed and what risks A flagged.

**Design:** For each completed dependency, inline a compact handoff summary.

```python
# In prepare_worker_packet(), after dependency lines:
for dep_id in task.depends_on:
    dep = self.ledger.get_task_transfer(dep_id)
    if dep and dep.status == "completed":
        dep_handoff = self.get_handoff_result(dep_id)
        if dep_handoff:
            lines.extend([
                "",
                f"### Upstream Handoff: {dep_id}",
                f"- Agent: {dep_handoff.agent_id}",
                f"- Changed files: {_format_list(dep_handoff.changed_files)}",
                f"- Risks: {_format_list(dep_handoff.risks)}",
            ])
```

**Impact:** ~15 lines in `registry.py`. No schema change. Worker packet gets
longer for tasks with completed dependencies — acceptable since this is
exactly the context the worker needs.

---

### B-3: Task TTL / Lease Expiry

**Problem:** If an agent crashes or loses connectivity while a task is
`running`, the task stays `running` indefinitely. No other agent can claim it.
This is the most common operational failure mode in multi-agent systems.

**Design:** Add a `ttl_seconds` check (field already exists on `TaskTransfer`,
default 3600). A new method `expire_stale_tasks()` scans for tasks that have
been in a non-terminal state beyond their TTL and transitions them to `failed`
with `error_code="TTL_EXPIRED"`.

```python
def expire_stale_tasks(self, *, now: float | None = None) -> list[TaskTransfer]:
    """Transition tasks past their TTL to failed.

    Scans for tasks in non-terminal states (proposed, accepted, running,
    review_ready) whose updated_at + ttl_seconds < now. Returns the
    list of expired tasks.
    """
    checkpoint = now or time.time()
    candidates = []
    for status in ("proposed", "accepted", "running", "review_ready"):
        candidates.extend(self.ledger.list_task_transfers(status=status))
    expired = []
    for task in candidates:
        updated = _parse_iso_timestamp(task.updated_at or task.created_at)
        if updated and (checkpoint - updated) > task.ttl_seconds:
            failed = self.fail_task(
                task.task_id,
                agent_id="system",
                error_code="TTL_EXPIRED",
                message=f"Task exceeded TTL of {task.ttl_seconds}s",
            )
            expired.append(failed)
    return expired
```

**CLI integration:**

```bash
mac-agent expire-stale --db mac.db
```

**MCP tool:** `mac_expire_stale_tasks` (read-write).

**HTTP:** `POST /tasks/expire-stale`.

**Impact:** ~30 lines product code + ~20 lines tests. Uses existing `ttl_seconds`
field — no schema change. The caller (CLI, cron) is responsible for periodic
invocation; MAC does not start a background thread (ledger principle).

**Open question:** Should `expire_stale_tasks` also auto-retry if
`retry_count < max_retry_count`? Deferred — let TTL expiry stand alone first.

---

### B-4: `mac-agent next` One-Shot Command

**Problem:** An agent entering a project needs to:
1. `list_ready_tasks` to find work
2. `claim_next_task` to claim it
3. `start_task` to begin
4. `prepare_worker_packet` to get context

That's 4 API calls (or CLI invocations) before any real work starts.

**Design:** A single `next` command that atomically claims + starts + outputs
the worker packet:

```bash
mac-agent next --agent-id claude --capability write_code
```

Output: the worker packet Markdown to stdout (same as `worker-packet`), plus
a JSON header line with task_id and status for machine parsing:

```
---MAC-TASK: {"task_id": "task-1", "status": "running"}---
# Worker Task: task-1
...
```

**Implementation:** Thin wrapper in `cli.py`:

```python
claimed = registry.claim_next_task(agent_id=agent_id, capability=capability, ...)
if claimed is None:
    print("No claimable tasks found.", file=sys.stderr)
    return 1
started = registry.start_task(claimed.task_id, agent_id)
packet = registry.prepare_worker_packet(claimed.task_id, agent_id=agent_id)
print(f"---MAC-TASK: {json.dumps({'task_id': started.task_id, 'status': started.status})}---")
print(packet)
return 0
```

**Impact:** ~25 lines in `cli.py` + ~15 lines tests. No new Registry method
(orchestrates existing methods).

---

### B-5: Reviewer Capability Validation

**Problem:** `accept_review` and `reject_review` accept any `reviewer_id`
string with no validation. A `write_code` agent could accept its own review,
violating separation of concerns.

**Design:** Add an optional `reviewer_capability` to `CoordinationPolicy`:

```python
class CoordinationPolicy(BaseModel):
    require_review: bool = False
    require_path_check: bool = False
    reviewer_capability: str | None = None  # e.g. "review_code"
    path_rule: PathRule = Field(default_factory=PathRule)
    max_retry_count: int = Field(default=3, ge=0)
```

When `reviewer_capability` is set, `accept_review` and `reject_review` verify
that the reviewer has the specified capability. If not, raise `StateConflictError`.

```python
def accept_review(self, task_id: str, reviewer_id: str) -> TaskTransfer:
    if self.policy.reviewer_capability:
        reviewer = self.get_agent(reviewer_id)
        if reviewer is None or not any(
            cap.name == self.policy.reviewer_capability
            for cap in reviewer.capabilities
        ):
            raise StateConflictError(
                f"Agent {reviewer_id!r} lacks capability "
                f"{self.policy.reviewer_capability!r} required for review."
            )
    # ... existing logic
```

**Environment variable:** `MAC_REVIEWER_CAPABILITY`.

**Impact:** ~15 lines in `registry.py` + ~10 lines in `messages.py` + ~20 lines
tests. Backward compatible — `reviewer_capability=None` (default) skips validation.

---

## 3. Implementation Order

```
B-1 (review packet quality) ─┐
B-2 (worker packet handoff)  ─┼─► B-5 (reviewer validation) ─► version bump
B-3 (TTL expiry)             ─┤
B-4 (next command)           ─┘
```

B-1 and B-2 are independent packet enhancements. B-3 is standalone. B-4 is a
CLI convenience. B-5 depends on B-1/B-2 being stable (review flow must work
before we gate it).

Estimated total: **~130 lines product code + ~85 lines tests**.

---

## 4. Version Target

Phase B ships as **v0.5.0 Alpha**. No breaking changes — all additions are
backward compatible (new fields default to None/False, new methods are additive).

---

## 5. Not in Phase B

Explicitly excluded with rationale:

| Feature | Why excluded |
|---------|-------------|
| Parallel groups | No E2E pain point; `depends_on` handles sequential fan-out |
| DAG visualization | UI concern, not ledger concern |
| Redis / Postgres | Local SQLite is sufficient; distributed storage is Phase C |
| Auto conflict resolution | Conflicts need human judgement by design |
| Background expiry thread | MAC is a ledger, not a scheduler; caller invokes `expire_stale_tasks` |
| Web dashboard | Nice-to-have but not a protocol feature; can be a separate project |

---

*Design document: MAC Phase B — Packet Enhancement + Lease Expiry + Reviewer Validation*
*Informed by E2E validation findings from v0.4.0*
