"""Generate thin IDE entry points that all defer to the MAC ledger."""
from __future__ import annotations

import json
from pathlib import Path

BEGIN = "<!-- MAC:BEGIN managed context entry -->"
END = "<!-- MAC:END managed context entry -->"

ENTRY = f'''{BEGIN}
## Multi-Agent Coordinator (MAC)

This project uses **MAC as the single source of truth** for agent, task, session,
blocker, handoff, and quality state. Do not duplicate project state in IDE rule
files. Read the current worker packet with `mac-agent next` or `mac-agent context`,
and write results back with MAC CLI, MCP, or authenticated HTTP callbacks.

- Ledger: `mac.db` (override with `MAC_DB_PATH`)
- MCP server: `mac-mcp-server`
- Project context cache: `.agent-context/` (generated; never authoritative)
- Rules here are only an entry point; task facts belong in the MAC ledger.
{END}
'''

MCP = {"mcpServers": {"mac": {"command": "mac-mcp-server", "args": [], "env": {"MAC_DB_PATH": "mac.db"}}}}


def _managed(path: Path, content: str = ENTRY) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if BEGIN in existing and END in existing:
        prefix, rest = existing.split(BEGIN, 1)
        _, suffix = rest.split(END, 1)
        rendered = prefix.rstrip() + "\n\n" + content.rstrip() + suffix
    else:
        rendered = existing.rstrip() + ("\n\n" if existing.strip() else "") + content
    path.write_text(rendered.rstrip() + "\n", encoding="utf-8")


def bootstrap_project(root: str | Path) -> list[Path]:
    """Create/update vendor entry points without creating parallel truth stores."""
    root = Path(root).resolve()
    text_entries = [
        root / "AGENTS.md",
        root / "CLAUDE.md",
        root / ".cursor" / "rules" / "mac.mdc",
        root / ".trae" / "rules" / "mac.md",
        root / ".qoder" / "rules" / "mac.md",
        root / ".workbuddy" / "rules" / "mac.md",
        root / ".opencode" / "rules" / "mac.md",
    ]
    for path in text_entries:
        prefix = "---\ndescription: MAC coordination entry point\nalwaysApply: true\n---\n\n" if path.suffix == ".mdc" else ""
        _managed(path, prefix + ENTRY)
    configs = {
        root / ".mcp.json": MCP,                         # Claude Code
        root / ".cursor" / "mcp.json": MCP,           # Cursor
        root / ".trae" / "mcp.json": MCP,             # Trae-compatible
        root / ".qoder" / "mcp.json": MCP,            # Qoder-compatible
        root / ".workbuddy" / "mcp.json": MCP,        # WorkBuddy-compatible
    }
    for path, payload in configs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        # These files are intentionally tiny connection configs, not state stores.
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    opencode = root / "opencode.json"
    opencode.write_text(json.dumps({"$schema": "https://opencode.ai/config.json", "mcp": {
        "mac": {"type": "local", "command": ["mac-mcp-server"], "enabled": True,
                "environment": {"MAC_DB_PATH": "mac.db"}}}}, indent=2) + "\n", encoding="utf-8")
    return [*text_entries, *configs, opencode]
