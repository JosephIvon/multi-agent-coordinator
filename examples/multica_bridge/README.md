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
REVIEW_FALLBACK_DIR=.agent-context/review-fallback   # where to write
                                                        # on POST failure
```

When `MULTICA_API_URL` is empty the reverse channel is a no-op (dev mode).
On any 4xx/5xx or network failure the packet is written to
`REVIEW_FALLBACK_DIR` and a warning is logged; the webhook response
still returns 200 so Multica does not retry the whole webhook.

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

## Next steps if you keep building

2. **Conflict surfacing**: scan the ledger for two completed tasks whose
   `changed_files` overlap; raise `ConflictRecord` on each.
3. **Path boundary enforcement**: refuse to record a `done` whose
   `changed_files` cross the agent's `allowed_paths`.
4. **Unit tests**: cover each event handler with `pytest` + `httpx.AsyncClient`
   so future edits don't break the idempotency contract.