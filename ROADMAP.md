# MAC-Agent Roadmap

> Version: 1.2.1
> Date: 2026-08-25
> Status: **maintenance mode (formalization track open)**

---

## Current State (v1.2.1)

MAC-Agent is a lightweight coordination ledger for AI coding agents. It provides
shared task state, context handoff, quality evidence, plan grouping, dependency
readiness, conflict records, and packet generation.

**Maturity**: Production/Stable (ledger semantics under formalization — see
"Formalization P0/P1" below; green CI covers build/test/lint only, not the
semantic guarantees listed there)
**License**: MIT (open source)
**Python**: 3.10+
**MCP Tools**: 31
**CLI Subcommands**: ~40
**Tests**: 583 collected (latest local validation, no reruns)

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
- [x] KNOWN_ISSUES.md tracking template and resolved-issue workflow
- [x] Third-party review follow-ups H-1/H-2, M-1..M-4 (scorer visibility,
  agent filter, per-attempt evidence gate, status CAS on terminal writers)
- [x] Version==tag release gate (`scripts/check_version_tag.py` in publish.yml)

---

## Formalization P0/P1 (semantic risks — CI green does NOT close these)

Identified 2026-08-25 during the 1.2.1 release audit. These are ledger
*semantics* gaps: each one can silently corrupt coordination truth under
concurrency or partial failure even with all tests passing. They are tracked
here so "maintenance mode" is not read as "production-proven semantics".

### P0 — correctness of the coordination truth

1. **SQLite multi-step write atomicity.** Lifecycle flows perform
   read-modify-write across multiple independent writes with no shared
   transaction boundary — e.g. `done()` alone runs precheck →
   `submit_quality_result` → gate evaluation → handoff save → status CAS
   → audit/conflict writes as separate committed steps (see
   `registry.py::done`). A crash between steps leaves the ledger
   internally inconsistent (quality evidence persisted for a task that
   never transitioned, handoff saved without completion, etc.). Fix
   direction: per-flow transaction scopes (or a write-ahead intent
   journal) so each lifecycle transition is all-or-nothing. Related
   landed work: status CAS on terminal writers (M-3) and the done()
   running-precheck narrow the window but do not close it.
2. **HTTP default authentication.** `create_app()` only enforces a bearer
   token when one is passed or `MAC_HTTP_TOKEN` is set — with neither, the
   full API is unauthenticated (loopback bind by default, but `MAC_HTTP_HOST`
   can expose it). P0 work: fail closed for non-loopback binds without a
   token, deprecate the unauthenticated default explicitly, and document
   that loopback trust is an opt-in assumption, not a security boundary.
3. **Receipt full binding (回执全绑定).** Durable callbacks (`claim_callback`)
   dedupe on event id but bind neither the executor attempt nor a content
   hash of the claimed payload; a late or replayed receipt can be applied
   to a newer attempt than the one that produced it. Fix direction:
   receipts must carry and verify attempt identity + content hash before
   mutating state (extends M-1's current-attempt bucketing from the gate
   layer to the transport layer).
4. **Attempt / fence identity.** `TaskTransfer` tracks `retry_count` and
   lease holder/expiry but carries no monotonically increasing
   `attempt_id` / fencing token. Two concurrent writers (lease-expiry
   takeover vs. a slow original writer) can interleave writes with no
   ordering arbiter — status CAS catches status-field races but not
   last-writer-wins on payload/evidence fields. Fix direction: per-task
   `attempt_id` + fencing token checked on every write path, including
   quality evidence and handoff saves.

### P1 — robustness of completion semantics

5. **Weak 2xx == completed judgment.** `adapters/http.py` maps any 2xx to
   `"completed"` by status code alone (`status = "completed" if 200 <=
   result.status_code < 300`); the body is never inspected for a success
   envelope. A 200 carrying an error body or truncated payload still
   completes the task. Fix direction: require a structured success envelope
   (status field + artifact refs) before the completed transition; anything
   else lands in `receipt_pending` / `correction_required`.

**Ordering**: P0-3 and P0-4 are prerequisites for trusting any multi-writer
takeover flow (including mac_coffee's lease/heartbeat model); P0-1 bounds
the blast radius of all of them; P1-5 is independently schedulable.

**Boundary**: these live upstream (mac-agent) because they are ledger
semantics, not commercial features. mac_coffee inherits each fix via the
version pin; no contract bump expected unless the receipt schema changes
(P0-3 may require contract version 2, coordinated with mac_coffee).

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
