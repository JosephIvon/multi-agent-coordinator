# MAC 1.0 Installation and Upgrade Guide

## Requirements

- Python 3.10–3.13
- SQLite 3 (bundled with normal Python distributions)
- A writable project directory

## Install

```bash
python -m pip install "mac-agent[http,mcp]==1.1.0"
mac-agent --help
mac-mcp-server --help
```

For local development:

```bash
git clone https://github.com/JosephIvon/multi-agent-coordinator.git
cd multi-agent-coordinator
python -m venv .venv
# Windows: .venv\Scripts\activate
# Unix: source .venv/bin/activate
python -m pip install -e ".[dev]"
pytest -q
```

## Initialize an IDE-neutral project

Run this once at the repository root, and rerun it after upgrading MAC:

```bash
mac-agent bootstrap --project-root .
```

The command creates thin entries for AGENTS/Codex, Claude Code, Cursor,
OpenCode, Trae, Qoder, and WorkBuddy. The generated rules point to MAC; they do
not contain task state. Existing content outside the managed marker is retained.

## Start services

MCP clients launch `mac-mcp-server` over stdio. For remote agents, use the HTTP
service with a strong token:

```bash
# PowerShell
$env:MAC_DB_PATH = "mac.db"
$env:MAC_HTTP_TOKEN = "replace-with-a-long-random-token"
mac-http-server
```

The server binds to `127.0.0.1:8765` by default. Set `MAC_HTTP_HOST` only when a
trusted network/reverse proxy is configured. All routes except `/` require
`Authorization: Bearer <token>` when `MAC_HTTP_TOKEN` is set.

## Upgrade and rollback

1. Back up `mac.db`.
2. Install the new wheel.
3. Run `mac-agent bootstrap --project-root .`.
4. Start MAC once; additive SQLite tables are created automatically.
5. Run the smoke checks below.

Rollback by reinstalling the prior wheel and restoring the database backup.
Version 1.0 uses additive schema changes and does not rewrite existing task rows.

## Release smoke checks

```bash
ruff check src tests
python -m mypy src/mac --no-error-summary
pytest -q
python -m build --no-isolation
python scripts/release_smoke.py --skip-build
```
