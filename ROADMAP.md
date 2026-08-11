# MAC-Agent Roadmap

> Version: 1.2.0
> Date: 2026-08-12
> Status: **maintenance mode**

---

## Current State (v1.2.0)

MAC-Agent is a lightweight coordination ledger for AI coding agents. It provides
shared task state, context handoff, quality evidence, plan grouping, dependency
readiness, conflict records, and packet generation.

**Maturity**: Production/Stable
**License**: MIT (open source)
**Python**: 3.10+
**MCP Tools**: 31
**CLI Subcommands**: ~40
**Tests**: 563+

---

## Maintenance Mode

**mac-agent is in maintenance mode.** New features live in the downstream
commercial layer (`mac_coffee`); bug fixes and security patches happen
upstream via this repo.

### What this means

| Category | This repo (mac-agent) | Downstream (mac_coffee) |
|----------|----------------------|------------------------|
| Bug fixes | ✅ Accepted | N/A (inherits via dependency) |
| Security patches | ✅ Priority | N/A (inherits via dependency) |
| New features | ❌ Redirect to mac_coffee | ✅ Accepted |
| Breaking changes | ❌ Not planned | Governed by mac_coffee ROADMAP |
| MCP tool additions | Only if cross-IDE core need | ✅ Accepted |

### Upstream responsibilities

1. **SQLite ledger schema** — mac-agent owns the task/agent/conflict/evidence
   tables. Schema extensions are versioned via `schema_extensions.py`.
2. **MCP server** — 31 tools covering task lifecycle, handoff, quality gates,
   vault integration, and cross-IDE fact storage.
3. **CLI** — ~40 subcommands for task management, kanban, dashboard, scoring.
4. **Protocol** — `TaskTransfer`, `AgentCard`, `TaskPayload` Pydantic models.
5. **Quality gate** — pluggable quality evaluation + contract checks.

### Downstream boundary

`mac_coffee` (v0.1.0, Alpha) builds on mac-agent to add:
- Human identity & authentication (ADR-013)
- Cycle management (planning cycles, not just tasks)
- Task transfer integrity governance (malformed row quarantine)
- Deployment套件 (Docker, cloud deploy, systemd)
- Web UI (Next.js dashboard)
- Audit & compliance (clean room, code provenance)

These features are **not** planned for mac-agent. See
`mac_coffee/ROADMAP.md` for the commercial layer roadmap.

---

## Completed Phases

| Phase | Description | Status |
|-------|-------------|--------|
| Core | SQLite ledger, Registry, CLI, MCP server | ✅ v1.0.0 |
| A | Task lifecycle + dependency readiness | ✅ v1.0.0 |
| B | Cross-IDE session context + kanban + metrics | ✅ v1.1.0 |
| C | Quality gate hardening + handoff packets | ✅ v1.2.0 |
| D | Timebox auto-rollback + lease expiry | ✅ v1.2.0 |
| E | Extension lifecycle hooks + scoring | ✅ v1.2.0 |
| Phase 3 | Vault tools (search filters, promote, EOD, daily_notes) | ✅ v1.2.0 |

---

## Maintenance Backlog

Items accepted for upstream maintenance:

- [x] Phase 3 vault tools (search filters, draft lifecycle, promote, EOD hint, daily_notes)
- [x] TaskTransfer network/capability/data grading + lease routing fields
- [x] Cross-repo contract guard (mac_coffee <-> mac-agent)
- [x] 7 HIGH priority fixes (version, role param, expire-leases, metrics)
- [ ] KNOWN_ISSUES.md tracking (template to be established)

---

## Not Planned (Redirect to mac_coffee)

- Human authentication / identity overlay
- Cycle management (mac_coffee's Cycle model replaces Plan for commercial use)
- Task transfer integrity governance (quarantine, background scans)
- Docker / cloud deployment scripts
- Web UI / dashboard SPA
- Audit evidence collection & reporting
- Clean room / code provenance enforcement

---

## Versioning

- **SemVer** with minor bumps for new MCP tools / CLI commands.
- **No breaking changes** in maintenance mode without a major version bump
  and a deprecation cycle.
- `mac_coffee` pins `mac-agent>=1.1,<2`; breaking changes require coordinated
  releases.