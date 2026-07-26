# IDE and Remote Agent Integrations

MAC keeps exactly one factual store: the SQLite ledger. IDE files are connection
and context entry points only.

## Shared MCP command

All MCP-capable tools launch:

```json
{"command": "mac-mcp-server", "args": [], "env": {"MAC_DB_PATH": "mac.db"}}
```

| Tool | Generated entry | Purpose |
|---|---|---|
| Codex | `AGENTS.md` | Project instructions pointing to MAC |
| Claude Code | `CLAUDE.md`, `.mcp.json` | Rules entry and MCP server |
| Cursor | `.cursor/rules/mac.mdc`, `.cursor/mcp.json` | Always-applied entry and MCP |
| OpenCode CLI | `opencode.json`, `.opencode/rules/mac.md` | Local MCP and rule entry |
| Trae | `.trae/rules/mac.md`, `.trae/mcp.json` | Generic MCP-compatible entry |
| Qoder | `.qoder/rules/mac.md`, `.qoder/mcp.json` | Generic MCP-compatible entry |
| WorkBuddy | `.workbuddy/rules/mac.md`, `.workbuddy/mcp.json` | Generic MCP-compatible entry |

Vendor formats evolve; `mac-agent bootstrap` owns only its marked block and tiny
MCP connection files. If a vendor changes its config location, copy the same MCP
command into the vendor UI—never copy ledger contents into the rule file.

## Remote HTTP callback lifecycle

1. Register an `AgentCard`.
2. Submit a task.
3. Call `POST /tasks/{task_id}/dispatch`. MAC creates a durable session, starts
   the task, and sends the task plus `session_id` and `callback_url`.
4. The worker periodically calls `POST /sessions/{session_id}/heartbeat`.
5. The worker calls `POST /callbacks/{event_id}` once with a stable event ID.
6. MAC validates bearer auth and session/task/agent identity, atomically claims
   the event ID, writes handoff/quality/blocker state, and closes the session.
7. Replays with the same body return `duplicate: true`; reuse with a different
   body returns HTTP 409.

Completed callback body:

```json
{
  "session_id": "session-uuid",
  "task_id": "task-1",
  "agent_id": "worker-1",
  "result": {
    "status": "completed",
    "summary": "Implemented and tested",
    "changed_files": ["src/app.py"],
    "verification": ["pytest"]
  }
}
```

Blocked callback body:

```json
{
  "session_id": "session-uuid",
  "task_id": "task-1",
  "agent_id": "worker-1",
  "result": {
    "status": "blocked",
    "blocker": "Database schema decision required",
    "handoff_to": "architect"
  }
}
```

A blocked result creates a durable blocker, records the handoff target, and sets
the task to `blocked`. Resolve it with `POST /tasks/{task_id}/resume`.

## Reliability notes

- Use a unique, unpredictable HTTP token and TLS at the reverse proxy.
- Callback event IDs must be stable across worker retries.
- Secret-shaped fields and bearer credentials are redacted at adapter boundaries.
- CLI adapters return 124 on timeout and 130 on cancellation.
- Run stale-session and stale-agent expiry from your scheduler when operating a
  long-lived coordinator.
