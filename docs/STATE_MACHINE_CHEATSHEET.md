# MAC State Machine Cheatsheet

## Status Lifecycle (Default: `require_review=False`)

```
proposed → accepted → running → completed
    ↓         ↓          ↓
 rejected  rejected    failed
                      cancelled
```

| Transition | Trigger | Atomic? |
|---|---|---|
| `proposed → accepted` | `accept()` or `claim_next_task()` | `claim_next_task` is atomic |
| `accepted → running` | `start_task()` | No |
| `running → completed` | `complete_task()` after quality gate | No |
| `running → failed` | `fail_task()` | No |
| Any → `cancelled` | `cancel_task()` | No |
| Any → `rejected` | explicit reject | No |

## Status Lifecycle (With Review: `require_review=True`)

```
proposed → accepted → running → review_ready → completed
    ↓         ↓          ↓           ↓
 rejected  rejected    failed     rejected
                                  (reason → conflict)
                      cancelled
```

| Transition | Trigger | Notes |
|---|---|---|
| `running → review_ready` | `mark_review_ready()` | Replaces direct `complete` |
| `review_ready → completed` | `accept_review()` | Reviewer accepts |
| `review_ready → rejected` | `reject_review()` | Auto-creates `ConflictRecord` |

## Non-Terminal vs Terminal Statuses

| Non-Terminal (active) | Terminal (done) |
|---|---|
| `proposed` | `completed` |
| `accepted` | `failed` |
| `running` | `cancelled` |
| `review_ready` | `rejected` |

## Dependency & Readiness

- `depends_on` = list of upstream task IDs
- A `proposed` task is **ready** only when ALL dependencies are `completed` or `cancelled`
- `list_ready_tasks()` returns only unblocked proposed tasks
- `claim_next_task()` atomically: claim + accept + start

## Quality Gate

- `test_contract.required_commands` — must pass before `complete_task()`
- `test_contract.required_evidence` — must be submitted
- `allow_manual_override: false` — gate is mandatory
- `record_quality_and_complete()` — submit evidence + gate check + complete in one step

## CLI Quick Reference

```bash
# Submit a task
mac-agent submit --agent-id <agent> --task '{"task_id":"...","trace_id":"...",...}'

# Claim + start next ready task (atomic)
mac-agent next --agent-id <agent> --capability <cap>

# Complete with quality evidence
mac-agent done --task-id <id> --agent-id <agent> \
  --quality-command "pytest -q" --quality-status passed \
  --evidence test_output --changed-file path/to/file

# Save handoff
mac-agent handoff --task-id <id> --agent-id <agent> \
  --changed-file path/to/file --verification "manual-review:pass"

# View status
mac-agent status --task-id <id>
mac-agent tasks  # list all

# Dashboard
mac-agent dashboard
```

## Real-World Usage Examples

### Example 1: Submit → Claim → Start → Done (Default Flow, No Review)

Two agents collaborate: `scout` submits a task, `worker` claims and completes it.

```bash
# Agent "scout" submits a documentation task
mac-agent submit --agent-id scout --task '{
  "task_id": "task-doc-readme",
  "trace_id": "task-doc-readme",
  "source_agent_id": "scout",
  "target_agent_id": "worker",
  "payload": {
    "schema_version": "1.0",
    "type": "write_doc",
    "summary": "Update README with install instructions",
    "risk_level": "low"
  },
  "depends_on": [],
  "test_contract": {
    "required_commands": ["pytest tests/ -q"],
    "required_evidence": ["test_output"],
    "risk_level": "low"
  }
}'
# → status: proposed

# Agent "worker" claims and starts the task atomically
mac-agent next --agent-id worker --capability write_doc
# → status: accepted → running (atomic)

# Worker completes with quality evidence
mac-agent done --task-id task-doc-readme --agent-id worker \
  --quality-command "pytest tests/ -q" --quality-status passed \
  --evidence test_output --changed-file README.md
# → status: completed
```

**State path**: `proposed → accepted → running → completed`

---

### Example 2: Failure and Retry

A coding task fails, the agent retries with a corrected approach.

```bash
# Task submitted for a feature implementation
mac-agent submit --agent-id lead --task '{
  "task_id": "task-add-feature",
  "trace_id": "task-add-feature",
  "source_agent_id": "lead",
  "payload": {
    "schema_version": "1.0",
    "type": "write_code",
    "summary": "Implement user avatar upload",
    "risk_level": "medium"
  },
  "depends_on": [],
  "retry_count": 3,
  "test_contract": {
    "required_commands": ["pytest tests/test_avatar.py -q"],
    "required_evidence": ["test_output"],
    "risk_level": "medium"
  }
}'

# Worker claims and starts
mac-agent next --agent-id dev --capability write_code

# Something goes wrong — dev reports failure
mac-agent fail --task-id task-add-feature --agent-id dev \
  --error-code build_broken --message "Missing Pillow dependency"
# → status: failed

# Retry after fixing dependencies
mac-agent retry --task-id task-add-feature --agent-id dev
# → status: proposed (retry_count incremented)

# Second attempt succeeds
mac-agent next --agent-id dev --capability write_code
mac-agent done --task-id task-add-feature --agent-id dev \
  --quality-command "pytest tests/test_avatar.py -q" --quality-status passed \
  --evidence test_output --changed-file src/avatar.py
# → status: completed
```

**State path**: `proposed → accepted → running → failed → (retry) proposed → accepted → running → completed`

---

### Example 3: Review Flow (`require_review=True`)

When the coordination policy requires review, `done` redirects to `review_ready`.

```bash
# Policy is configured with require_review=True
# Worker completes work — done() auto-detects review policy
mac-agent done --task-id task-security-audit --agent-id worker \
  --quality-command "pytest tests/test_security.py -q" --quality-status passed \
  --evidence test_output --changed-file src/auth.py \
  --verification "manual-review:pass:all-checks-passed" --risk low
# → status: review_ready (auto-detected require_review=True)

# Reviewer inspects the work
mac-agent review-packet --task-id task-security-audit
# → prints Markdown review packet with context, evidence, handoff

# Reviewer accepts
mac-agent review-lifecycle accept --task-id task-security-audit --reviewer-id reviewer
# → status: completed

# OR reviewer rejects with reason
mac-agent review-lifecycle reject --task-id task-security-audit \
  --reviewer-id reviewer --reason "Missing edge-case test for empty password"
# → status: rejected (auto-creates ConflictRecord with source="reject_review")
```

**State path (accept)**: `running → review_ready → completed`
**State path (reject)**: `running → review_ready → rejected`

---

### Example 4: Dependency Chain

Two tasks in series — the second cannot be claimed until the first completes.

```bash
# Task 1: write cheatsheet (no dependencies)
mac-agent submit --agent-id lead --task '{
  "task_id": "task-write-cheatsheet",
  "trace_id": "task-write-cheatsheet",
  "source_agent_id": "lead",
  "payload": {
    "schema_version": "1.0",
    "type": "write_doc",
    "summary": "Write state machine cheatsheet"
  },
  "depends_on": [],
  "test_contract": {
    "required_commands": ["pytest tests/ -q"],
    "required_evidence": ["test_output"]
  }
}'

# Task 2: append examples (depends on task-write-cheatsheet)
mac-agent submit --agent-id lead --task '{
  "task_id": "task-add-examples",
  "trace_id": "task-add-examples",
  "source_agent_id": "lead",
  "payload": {
    "schema_version": "1.0",
    "type": "write_doc",
    "summary": "Append examples to cheatsheet"
  },
  "depends_on": ["task-write-cheatsheet"],
  "test_contract": {
    "required_commands": ["pytest tests/ -q"],
    "required_evidence": ["test_output"]
  }
}'

# Check ready tasks — only task-write-cheatsheet appears
mac-agent ready-tasks --capability write_doc
# → [task-write-cheatsheet]   (task-add-examples is blocked)

# Complete task 1
mac-agent next --agent-id worker --capability write_doc
mac-agent done --task-id task-write-cheatsheet --agent-id worker \
  --quality-command "pytest tests/ -q" --quality-status passed \
  --evidence test_output --changed-file docs/STATE_MACHINE_CHEATSHEET.md

# Now task-add-examples is unblocked
mac-agent ready-tasks --capability write_doc
# → [task-add-examples]
```

**Key insight**: `list_ready_tasks()` / `mac-agent ready-tasks` filters out any `proposed` task whose `depends_on` contains a non-terminal task.

---

### Example 5: Cancellation and Cleanup

A task becomes unnecessary mid-flight; cancel and clean up.

```bash
# A task is running
mac-agent status --task-id task-experimental
# → status: running

# Decision: the feature is cancelled — cancel the task
mac-agent cancel --task-id task-experimental --agent-id manager
# → status: cancelled

# Later, cleanup terminal tasks older than 1 hour
mac-agent cleanup --statuses cancelled,failed,rejected
# → deletes terminal tasks from the ledger (recorded in audit trail)

# Verify the task is gone
mac-agent tasks
# → task-experimental no longer in the list

# But the audit trail preserves the history
mac-agent audit --task-id task-experimental
# → full audit log: submit → claim → start → cancel → cleanup
```

**State path**: `running → cancelled → (cleanup) deleted-from-ledger`

