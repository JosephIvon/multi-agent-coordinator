"""Generate thin IDE entry points that all defer to the MAC ledger.

v1.2.0 — each IDE gets a role-aware rule file and a common MCP connection config.
The rule file acts as a "quick-start card": what MCP tools to call first, what
the kanban means, how to claim/done/remember.
"""
from __future__ import annotations

import json
from pathlib import Path

BEGIN = "<!-- MAC:BEGIN managed context entry -->"
END = "<!-- MAC:END managed context entry -->"

# ── Common section: workflow & tool cheat-sheet (shared across all IDEs) ─────

_COMMON_WORKFLOW = """
## Session Start (every session)
1. Read `mac://session-context` for full project snapshot (kanban + facts + agents + conflicts)
2. Read `mac://kanban` for the four-color board (red=proposed, yellow=accepted/running, green=review_ready, done=completed)
3. If another agent left a handoff for you, it appears in the kanban notes

## Claim Work
- `mac_list_ready_tasks` — see what's available for your capability
- `mac_next_task` — atomic claim + start + worker packet (preferred)
- `mac_claim_task` — claim + start a specific task
- Role-gated tasks: if `required_role` is set, your agent card MUST include that role

## Finish Work
- `mac_done(task_id, agent_id, quality_result=..., has_changelog=..., met_acceptance_criteria=...)`
  - quality_result: {"command": "...", "status": "passed|failed", "evidence": [...]}
  - has_changelog: required for medium+ risk tasks
  - met_acceptance_criteria: required for high risk tasks
- Quality gate hard-fails → task becomes "blocked" with error_code TASK_BLOCKED
- On block: fix the issue, then `mac_claim_task` again to retry

## Cross-IDE Memory (persists across sessions & tools)
- `mac_remember(key, value, category)` — store a fact (visible to all IDEs)
- `mac_recall(query, limit)` — search facts (empty query = recent 10)
- `mac_search_vault(query)` — search Obsidian vault
- `mac_save_to_vault(content, path)` — write to Obsidian vault

## Timeboxed Tasks
- `lease_seconds` on task submission limits how long an agent may hold a task
- Expired leases auto-release back to `proposed` (or `failed` if retries exhausted)

## Core MCP Tools
| Category | Tools |
|----------|-------|
| Task | mac_next_task, mac_done, mac_submit_task, mac_claim_task, mac_fail_task, mac_save_handoff, mac_list_ready_tasks, mac_worker_packet, mac_review_packet |
| Review | mac_mark_review_ready, mac_accept_review, mac_reject_review |
| Maintenance | mac_expire_stale_tasks, mac_expire_stale_agents, mac_cleanup_tasks |
| Knowledge | mac_remember, mac_recall, mac_search_vault, mac_save_to_vault |
| Lease | mac_expire_task_leases, mac_list_agents, mac_get_task, mac_block_task |
| Scoring | mac_list_scorers, mac_set_scorer, mac_test_scorer |
| Resources | mac://kanban, mac://session-context, mac://capabilities, mac://health |

## CLI equivalents (if MCP is unavailable)
`mac-agent next --agent-id <id> --capability <cap>`
`mac-agent done --task-id <id> --agent-id <id> --quality-command "..." --quality-status passed`
`mac-agent kanban --json`
`mac-agent remember <key> <value>`

## Rules
- The MAC ledger (`mac.db`) is the single source of truth — do not mirror state in IDE files
- Project context cache (`.agent-context/`) is generated, never authoritative
- This block is managed by `mac-agent bootstrap`; vendor sections above/below are yours
"""


def _make_entry(ide_class: str) -> str:
    """Build the full ENTRY block for a given IDE class.

    ``ide_class`` is one of ``"skill"`` (Claude Code / Codex — tools with a
    skill system that does reasoning), ``"mcp-only"`` (Cursor, Trae, Qoder,
    WorkBuddy, OpenCode — tools that rely entirely on MCP tools).
    """
    if ide_class == "skill":
        prefix = (
            "## MAC — Multi-Agent Coordinator (v1.2.0)\n\n"
            "This project uses MAC as the coordination ledger. "
            "You have both MCP tools AND IDE skills. "
            "**Skills do reasoning; MCP tools do coordination.** "
            "Use MCP tools for task state, kanban, facts, and vault access. "
            "Use IDE skills for code generation, TDD, code review logic.\n"
        )
    else:
        prefix = (
            "## MAC — Multi-Agent Coordinator (v1.2.0)\n\n"
            "This project uses MAC as the coordination ledger. "
            "All task state, context handoff, quality evidence, and cross-session "
            "memory lives in the MAC SQLite database. "
            "Use the MCP tools listed below for ALL coordination operations.\n"
        )

    return f"{BEGIN}\n{prefix}\n{_COMMON_WORKFLOW.strip()}\n{END}"


# ── IDE registry ────────────────────────────────────────────────────────────

# Each entry: (rule_file, ide_class, extra_headers)
# ide_class: "skill" | "mcp-only"
_IDE_ENTRIES: list[tuple[str, str, str]] = [
    # ── Skill-based IDEs (Claude Code / Codex) ──
    ("CLAUDE.md", "skill", ""),
    ("AGENTS.md", "skill", ""),
    # ── MCP-only IDEs ──
    (".cursor/rules/mac.mdc", "mcp-only", "---\ndescription: MAC coordination entry point\nalwaysApply: true\n---\n\n"),
    (".trae/rules/mac.md", "mcp-only", ""),
    (".qoder/rules/mac.md", "mcp-only", ""),
    (".workbuddy/rules/mac.md", "mcp-only", ""),
    (".opencode/rules/mac.md", "mcp-only", ""),
]

# ── MCP config payload ──────────────────────────────────────────────────────

_MCP_CONFIG = {
    "mcpServers": {
        "mac": {
            "command": "mac-mcp-server",
            "args": [],
            "env": {"MAC_DB_PATH": "mac.db"},
            "description": "MAC coordination ledger — 26 tools + 4 resources (v1.2.0)",
        }
    }
}


# ── Bootstrap logic ──────────────────────────────────────────────────────────


def _managed(path: Path, content: str, *, header: str = "") -> None:
    """Write or update a managed block inside a text file.

    If the file already has a ``MAC:BEGIN … MAC:END`` block, replace only that
    section, preserving everything above and below.  Otherwise, append the
    managed content to the end.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if BEGIN in existing and END in existing:
        prefix, rest = existing.split(BEGIN, 1)
        _, suffix = rest.split(END, 1)
        prefix = prefix.rstrip()
        rendered = (prefix + "\n\n" if prefix else "") + content.rstrip() + suffix
    else:
        rendered = existing.rstrip() + ("\n\n" if existing.strip() else "") + header + content
    path.write_text(rendered.rstrip() + "\n", encoding="utf-8")


def bootstrap_project(root: str | Path) -> list[Path]:
    """Create/update vendor entry points for all 7 supported IDEs.

    Returns the list of files that were written or updated.
    """
    root = Path(root).resolve()
    written: list[Path] = []

    # 1. Rule files (text entries with managed MAC block)
    for rel_path, ide_class, header in _IDE_ENTRIES:
        path = root / rel_path
        entry = _make_entry(ide_class)
        _managed(path, entry, header=header)
        written.append(path)

    # 2. MCP connection configs (small JSON files, not managed blocks)
    config_paths: list[tuple[Path, dict]] = [
        (root / ".mcp.json", _MCP_CONFIG),                # Claude Code
        (root / ".cursor" / "mcp.json", _MCP_CONFIG),     # Cursor
        (root / ".trae" / "mcp.json", _MCP_CONFIG),       # Trae-compatible
        (root / ".qoder" / "mcp.json", _MCP_CONFIG),      # Qoder-compatible
        (root / ".workbuddy" / "mcp.json", _MCP_CONFIG),  # WorkBuddy-compatible
    ]
    for path, payload in config_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write a small connection file — intentionally tiny, not a state store.
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        written.append(path)

    # 3. OpenCode CLI — its own config format
    opencode = root / "opencode.json"
    opencode.write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "mcp": {
                    "mac": {
                        "type": "local",
                        "command": ["mac-mcp-server"],
                        "enabled": True,
                        "environment": {"MAC_DB_PATH": "mac.db"},
                    }
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    written.append(opencode)

    return written
