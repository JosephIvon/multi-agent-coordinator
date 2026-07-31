# mac-agent extension API

A *downstream* package (e.g. ``mac_coffee``) can register
extension tables, hooks, and WebSocket channels with mac-agent
at import time, without subclassing or forking mac-agent.

The extension registry is process-local and thread-safe. It is
NOT multiprocessing-safe by design: mac-agent runs in a single
process per host.

Three registration surfaces are supported.

## 1. Table DDL

Use ``Extension.table_ddl`` to ship extra tables into the
mac-agent SQLite file. mac-agent calls ``apply_ddl(connection)``
during startup; you provide the DDL as a list of idempotent
``CREATE TABLE IF NOT EXISTS`` statements.

Packages that only need schema registration can use the smaller
``mac.schema_extensions`` facade:

```python
from mac.schema_extensions import connection, register_table

register_table(
    "todo",
    "CREATE TABLE IF NOT EXISTS todo (id TEXT PRIMARY KEY)",
)

with connection() as db:
    db.execute("INSERT INTO todo (id) VALUES (?)", ("todo-1",))
```

``connection()`` uses ``MAC_DB_PATH`` when no explicit path is
provided. The caller owns the returned connection. Extension DDL
is also applied whenever ``SQLiteTaskLedger`` initializes.

## 2. Hooks

Use ``Extension.hooks`` to register named callables that
mac-agent invokes at defined points via ``call_hook(name, *args, **kwargs)``.
Results are collected in registration order; a hook that raises
is logged and skipped (best-effort contract).

## 3. WebSocket channels

Use ``Extension.ws_channels`` to declare ``ChannelDef`` entries.
Each channel becomes a ``/ws/{name}`` route on mac-http-server
and accepts clients that subscribe to a stream of validated
events. Downstream code publishes events via
``publish_to_channel(name, payload)``; mac-agent validates the
payload against the channel's ``payload_schema`` before fanning
out to all subscribed clients.

# Wire protocol (text frames only)

* Server -> Client: ``{"type": "ready", "channel": "..."}`` once connected
* Server -> Client: ``{"type": "event", "channel": "...", "payload": {...}}``
* Server -> Client: ``{"type": "error", "detail": [...]}`` on payload-validation failure
* Client -> Server: ``{"type": "ping"}`` -> Server replies
  ``{"type": "pong", "channel": "..."}``

The closing of the client socket removes the subscription.
Publishing or subscribing to an undeclared channel raises
``KeyError`` so misspelled channel names do not fail silently.

When ``MAC_HTTP_TOKEN`` is configured, extension WebSocket routes
require the same bearer token. Non-browser clients may use the
``Authorization`` header; browser clients may use ``?token=...``.

# Example: a downstream package adding a "todos" table

```python
# in package myapp/__init__.py
from pydantic import BaseModel
from mac.extensions import (
    ChannelDef,
    Extension,
    register,
    publish_to_channel,
)

class TodoEvent(BaseModel):
    todo_id: str
    title: str
    state: str  # "open" | "done"

register(
    Extension(
        name="myapp",
        version="0.1.0",
        table_ddl=[
            "CREATE TABLE IF NOT EXISTS todo ("
            "  id TEXT PRIMARY KEY,"
            "  title TEXT NOT NULL,"
            "  state TEXT NOT NULL,"
            "  created_at TEXT NOT NULL"
            ")"
        ],
        ws_channels=[
            ChannelDef(
                name="myapp.todo",
                payload_schema=TodoEvent,
                description="stream of todo state changes",
            )
        ],
    )
)

# elsewhere in the package, when a todo changes:
publish_to_channel("myapp.todo", {
    "todo_id": "t-1",
    "title": "ship mac-agent 1.0",
    "state": "done",
})
```

After mac-agent starts, the ``todo`` table exists in the SQLite
file and a WebSocket client connecting to
``ws://localhost:8765/ws/myapp.todo`` receives every published
event as it happens.

# Introspection

mac-http-server exposes a ``GET /extensions`` route that lists
every registered extension and its declared channels. This is
handy for ops dashboards and for downstream packages debugging
their own registration.
