# Multica -> MAC Bridge (PoC)

Receives Multica webhooks and mirrors them into the local MAC ledger so every
agent handoff that happens inside Multica leaves a structured record MAC can
query, audit, and replay.

## What it does

| Multica event       | MAC action                                                |
|---------------------|-----------------------------------------------------------|
| `issue.created`     | `submit_task` (status: proposed)                          |
| `agent.started`     | `accept_handoff` + `start_task` (status: running)         |
| `agent.commented`   | `record_checkpoint` (trace only, no state change)          |
| `agent.completed`   | `done` with `HandoffResult` (changed_files, risks, etc.)  |
| `agent.heartbeat`   | `heartbeat_agent` (refresh status/load/last_heartbeat)    |
| `agent.failed`      | `fail_task` (status: failed)                              |

Idempotent: duplicate `agent.started` events swallow `StateConflictError`.

## Quick start

```bash
pip install "mac-agent[http]"

# Self-test (no HTTP, synthetic events)
PYTHONPATH=src python examples/multica_bridge/server.py --demo

# Production run (listens on :8765)
PYTHONPATH=src python examples/multica_bridge/server.py
```

Configure Multica to POST to `http://your-host:8765/webhook/multica`. Set
`MULTICA_WEBHOOK_SECRET` to enable HMAC-SHA256 signature verification (header:
`X-Multica-Signature`).

## Smoke test

The PoC ships with a self-contained smoke test (server + 4 sample events + CLI
verification) that exercises the full path:

```bash
powershell -ExecutionPolicy Bypass -File examples/multica_bridge/_smoke2.ps1
```

It will:

1. Clean any prior `mac.db`.
2. Start the webhook receiver in the background.
3. POST 4 events (`issue.created`, `agent.started`, `agent.completed`, plus one
   unhandled type to verify it is ignored).
4. Run `mac-agent tasks` and `mac-agent status --task-id multica-TEST-1` to
   confirm the ledger.
5. Stop the server.

Sample event payloads live in `examples/multica_bridge/_events/` so you can
edit them and re-run.

### Sending events by hand

If you want to POST events yourself, **do not use `curl.exe` on Windows** --
the bundled curl adds a BOM to `@file` payloads and the server JSON parser
rejects them with `Expecting property name enclosed in double quotes`. Two
options that work everywhere:

**PowerShell** (any host):

```powershell
$body = Get-Content -Raw examples\multica_bridge\_events\01_issue_created.json
Invoke-RestMethod -Uri http://127.0.0.1:8765/webhook/multica `
    -Method Post -ContentType "application/json" -Body $body
```

**Python** (any host, easiest for automation):

```python
import json, pathlib, requests
body = pathlib.Path("examples/multica_bridge/_events/01_issue_created.json").read_text()
r = requests.post("http://127.0.0.1:8765/webhook/multica",
                  data=body, headers={"Content-Type": "application/json"})
print(r.status_code, r.json())
```

On Linux/macOS `curl` works fine; this caveat is Windows-only.

### Health check

The `/healthz` endpoint returns 200 OK with `{"status":"ok"}` -- safe to use
either tool against:

### Reverse channel (MAC -> Multica)

When `MULTICA_API_URL` is set, the bridge POSTs a markdown review packet
back to Multica as an issue comment after every successful `done()`
(only when the result is `completed` or `review_ready`, not `failed`).
The packet mirrors what `mac-agent review-packet` would print.

```
MULTICA_API_URL=http://10.47.102.70:8081
MULTICA_API_TOKEN=optional-bearer-token
REVIEW_FALLBACK_DIR=.agent-context/review-fallback   # (legacy, deprecated)
MULTICA_OUTBOX_DIR=.agent-context/outbox             # structured JSON for
                                                       # failed POSTs; drained
                                                       # via POST /outbox/replay
MULTICA_OUTBOX_MAX_ATTEMPTS=3                         # in-process retry budget
MULTICA_OUTBOX_BACKOFF_SECONDS=0.5                    # initial backoff (doubles)
# Every outbound POST also carries an Idempotency-Key header:
#   review  -> "review:<issue_id>"
#   audit   -> "audit:<issue_id>:<event_type>"
# so Multica can collapse a successful double-dispatch (in-process
# retry, outbox replay, or webhook retry) into a single side-effect.
# The key is persisted in the outbox JSON entry so manual drains
# carry the same dedup semantics as the original POST.
```

When `MULTICA_API_URL` is empty the reverse channel is a no-op (dev mode).
On any 4xx/5xx or network failure the packet is persisted to the
structured outbox at `MULTICA_OUTBOX_DIR` (default
`.agent-context/outbox`) as JSON, the webhook response still returns
200 so Multica does not retry, and the operator can drain the
outbox with `POST /outbox/replay` (or your own cron). Each outbound
POST is retried in-process up to `MULTICA_OUTBOX_MAX_ATTEMPTS` times
with exponential backoff starting at `MULTICA_OUTBOX_BACKOFF_SECONDS`,
so transient network blips are absorbed without ever touching disk.

`REVIEW_FALLBACK_DIR` is still honoured as a deprecated alias for
`MULTICA_OUTBOX_DIR` for backward compatibility, but new
deployments should set `MULTICA_OUTBOX_DIR` explicitly.

#### Idempotency

Every outbound POST carries an `Idempotency-Key` header derived from
the logical action: `review:<issue_id>` for review packets,
`audit:<issue_id>:<event_type>` for audit-trail comments. Multica
should treat the second occurrence of a key it has already accepted
as a no-op, so a redundant retry (in-process after a transient 5xx,
or an outbox replay from cron) cannot double-post the same comment.
Older outbox entries written before this feature shipped do not carry
an `idempotency_key` field; drains on those entries omit the header
entirely, which keeps forward-compat with on-disk state from previous
deployments.

#### Cross-window digest

`GET /reviews/digest?since=<iso>&until=<iso>&project=<id>&ship=<bool>`
aggregates every task in `status="completed"` whose handoff timestamp
falls inside the window into a single markdown roll-up.

- `since` defaults to `now - 24h`; `until` to `now` (UTC). Both accept
  the `Z` suffix and full microsecond precision, so a cron replaying
  every minute will see the same wall-clock second land in the same
  dedup bucket.
- `project` filters by `task.project_context`; omit it for an
  org-wide roll-up.
- `ship=true` additionally writes the digest body to
  `MULTICA_DIGESTS_DIR` (default `.agent-context/digests`) as
  `digest-<project>-<since>_<until>.md` plus a sibling `.json`
  metadata sidecar. Filenames are second-precision stable so a
  re-ship for the same window overwrites in place rather than
  accumulating. `GET /digests` lists previously shipped digests
  with their metadata so an operator can review before manually
  uploading to Multica (the bridge does NOT auto-POST the digest
  file -- the upload channel can be wired separately).

Response (always) carries:

- `count` -- number of contributing tasks
- `task_ids` -- the Multica-style task ids (no `multica-` prefix)
- `digest` -- the rendered markdown body
- `since` / `until` / `project` echoed back so the consumer can
  confirm the window they queried
- `shipped_to` -- absolute path to the `.md` (when `ship=true`)


```powershell
Invoke-RestMethod http://127.0.0.1:8765/healthz
```

```python
requests.get("http://127.0.0.1:8765/healthz").json()
```

## Layout

```
examples/multica_bridge/
    server.py          # 200-line bridge: FastAPI app + handlers + demo
    README.md          # this file
    _smoke2.ps1        # canonical end-to-end smoke test (PowerShell)
    _events/           # JSON payloads the smoke test POSTs
        01_issue_created.json
        02_agent_started.json
        03_agent_completed.json
        04_unknown.json          # unhandled event type, expected to be ignored
```

## Mapping details

- `data.verification` (Multica sends a short string like `"pytest:pass"`) is
  promoted to `VerificationEntry(command, result)` before `HandoffResult`.
- `data.ci_command` defaults to `"pr-ci"` if not provided.
- `data.changed_files` is passed straight through (must be a list of paths).

## What is NOT in this PoC

- No retry queue / dead-letter handling. Multica at-least-once delivery is
  expected to cover retries; idempotent handlers absorb duplicates.
- No batch events. Multica emits one POST per event today; if it switches to
  batches, wrap the handler loop in `for ev in batch:`.
- No agent roster registration. Multica is the source of truth for agent
  identity; MAC records the `agent_id` string verbatim without enforcing
  capability/load/heartbeat.

## What this proves

- The end-to-end loop works: Multica event -> MAC handoff -> queryable ledger.
- Idempotency under at-least-once delivery.
- The MAC state machine (`proposed -> running -> completed`) lines up with
  Multica's lifecycle when `agent.started` does accept + start.
- `mac-agent` CLI (and therefore any non-Multica tool) can query Multica's
  activity through the shared ledger without touching the webhook endpoint.

## What this PoC does now

- **Conflict surfacing**: after every successful `done()` the bridge calls
  `Registry.detect_file_overlap_conflicts(task_id)`. Any overlap with another
  `completed` or `review_ready` task's `changed_files` is recorded as a
  `ConflictRecord` with `source='file_overlap'` and surfaced in the
  review packet under `## Open Conflicts`. Detection is idempotent via a
  deterministic `conflict_id` so re-runs do not duplicate rows.
- **Path boundary enforcement**: the bridge passes `enforce_boundaries=True`
  to `Registry.done()`. When the agent's `allowed_paths` / `forbidden_paths`
  reject the handoff (e.g. an agent touches `secrets/*` despite having that
  glob listed under `forbidden_paths`), MAC short-circuits before saving the
  `HandoffResult`. The webhook still returns 200 but with
  `{"status": "boundary_violation", "violations": [...]}` so Multica gets
  the structured refusal and the reverse channel is skipped. Agents without
  any declared path patterns stay permissive (backward compatible).
- **Severity upgrade for guarded modules**: when the bridge has a non-empty
  `MULTICA_GUARDED_PATHS` env var (e.g. `auth/*,secrets/*,db/migrations/*`),
  any file-overlap conflict whose `changed_files` match a guarded glob is
  recorded with `severity="blocking"` and rendered in a separate
  `## Blocking Conflicts` section of the review packet. Non-guarded overlaps
  remain `non_blocking` and stay under `## Open Conflicts (non-blocking)`.
  When `MULTICA_GUARDED_PATHS` is empty (default), every overlap stays
  non-blocking and the review packet keeps a single section, so existing
  deployments see no behaviour change.
- **Refuse `done()` on blocking conflicts**: when the bridge has at least one
  guarded pattern **and** `MULTICA_REFUSE_ON_BLOCKING` is not set to a falsy
  value (`0`, `false`, `no`, empty string), `Registry.done()` is called with
  `refuse_on_blocking=True`. If a blocking overlap is detected the bridge
  rolls the task back to `running` (via `Registry._transition("running",
  expected_status="completed"|"review_ready", action="rollback_blocking_conflict")`),
  preserves the handoff in the ledger so reviewers can see what was
  attempted, and returns
  `{"status": "blocking_conflict", "conflicts": [...], "quality_gate": "passed"}`.
  The webhook still returns 200 so Multica sees the structured refusal; the
  reverse channel is skipped because there is no completed-state review
  packet to post. Setting `MULTICA_REFUSE_ON_BLOCKING=false` (or leaving
  `MULTICA_GUARDED_PATHS` empty) keeps the old informational behaviour.
- **Agent-card auto-sync from Multica roster**: when `agent.started` carries
  an `agent_card` payload (allowed_paths / forbidden_paths / metadata), the
  bridge registers it via `Registry.register(AgentCard(...))` before
  transitioning to running. Path-boundary enforcement turns on
  automatically; older Multica deployments that do not yet send `agent_card`
  continue to work unchanged (the handler response surfaces `card_synced`
  so callers can verify wiring).
- **Audit-trail shipping (MAC -> Multica)**: with `MULTICA_AUDIT_TRAIL=true`
  and `MULTICA_API_URL` set, every handled webhook fires a short
  fire-and-forget POST to `POST /api/issues/<issue_id>/comments` with body
  `### [MAC audit] `<event_type>` @ <ts>\n<summary>`. Failures are logged
  at WARNING but never break the webhook (response stays 200). No fallback
  file is written on failure -- the MAC ledger is the authoritative
  record. Default OFF to avoid spamming Multica; flip it on when you want
  reviewers to see the full lifecycle on the original issue without
  needing to round-trip via mac-agent.

## Next steps if you keep building

1. **Agent capability heartbeat**: today the bridge syncs an agent card on
   `agent.started` but does not refresh `last_heartbeat`, `load`, or
   `status`. Adding a heartbeat thread that pings Multica every N seconds
   (or consumes a periodic event) would let MAC schedule against live
   capacity rather than static declarations.
2. **Cross-bridge review aggregation**: when many issues are in flight,
   the review packet is per-issue. A roll-up endpoint that fetches all
   completed tasks in a window and ships a single digest comment would
   help reviewers who watch many issues at once.
3. **WebSocket bridge option**: today the bridge is HTTP-only. Multica
   supports webhooks plus a long-poll channel; the latter would let the
   bridge push progress checkpoints without waiting for a Multica round-trip.
