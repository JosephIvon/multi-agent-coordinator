# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.8.0] — 2026-07-24

### Added

- **P0-1**: `TestContract.for_risk()` supports `custom_commands` and `custom_evidence` parameters to override hardcoded pytest commands and evidence names — enables quality gates for non-Python projects (e.g. `vue-tsc`, `biome check`)
- **P0-1**: `mac-agent contract --custom-command` and `--custom-evidence` CLI flags
- **P0-1**: `mac-agent submit --custom-command` and `--custom-evidence` CLI flags
- **P1-1**: `mac-agent submit --spec-json` CLI flag for structured task specs stored in `task.metadata.spec`
- **P1-1**: Worker packet renders `## Structured Spec` section from `task.metadata.spec`
- **P1-2**: `Registry.cleanup_tasks()` method to delete terminal tasks (failed/cancelled/rejected/superseded) with optional status, plan, and age filters
- **P1-2**: `mac-agent cleanup` CLI subcommand
- **P1-2**: `mac_cleanup_tasks` MCP tool (16th tool)
- **P1-2**: `POST /tasks/cleanup` HTTP endpoint
- Ruff linter configuration (`[tool.ruff]`) with E/W/F/UP/B/SIM/I/RUF100 rules
- MyPy type checker configuration (`[tool.mypy]`)
- Coverage configuration (`[tool.coverage]`) with 70% minimum threshold
- Pre-commit hooks configuration (`.pre-commit-config.yaml`) with ruff + trailing-whitespace + end-of-file-fixer
- `ruff`, `mypy`, `pytest-cov` added to `[dev]` extra dependencies

### Changed

- **P0-2**: Windows CLI `STATUS_STACK_BUFFER_OVERRUN` mitigation via `threading.stack_size(8MB)` in `__main__`
- CI lint job upgraded from `import mac` check to `ruff check` + `mypy`
- CI test job now collects coverage via `pytest-cov` and uploads `coverage.xml` artifact
- Publish workflow migrated from API token to PyPI Trusted Publishing (OIDC)
- SPEC.md updated: 15 → 16 MCP tools
- README.md updated: 15 → 16 MCP tools
- CLAUDE.md updated: K-002 status → ✅已修, 15 → 16 tools
- Test count: ~251 → ~261

## [0.7.0] — 2026-07-23

### Added

- **C-6**: `Registry.done()` single entry point for finishing a task: submit quality evidence → save handoff → complete (or mark review-ready, auto-branching on `require_review`)
- **C-6**: `mac-agent done` CLI command (only `--task-id` and `--agent-id` required)
- **C-6**: `mac_done` MCP tool (15 tools total; `mac_record_quality_and_complete` kept as legacy)
- **C-6**: `POST /tasks/{task_id}/done` HTTP endpoint
- Rewrote `docs/USER_GUIDE.md` with AI-tool-first approach: normal workflow is `mac_next_task` → `mac_done` (two steps, no state machine to remember)

### Changed

- SPEC.md updated to v2.5 (C-6 done feature, 15 MCP tools)
- Test count: ~240 → ~251

## [0.6.0] — 2026-07-23

### Added

- **C-1**: GitHub Actions publish workflow (tag-triggered PyPI upload via trusted publishing)
- **C-2**: `expire_stale_tasks(auto_retry=True)` resets tasks with retries remaining to `proposed` instead of `failed`
- **C-2**: `mac-agent expire-stale --auto-retry` CLI flag
- **C-2**: `mac_expire_stale_tasks(auto_retry)` MCP parameter
- **C-2**: `POST /tasks/expire-stale?auto_retry=true` HTTP parameter
- **C-3**: `expire_stale_agents()` sets offline agents with stale heartbeats
- **C-3**: `CoordinationPolicy.agent_timeout` field (default 300s) + `MAC_AGENT_TIMEOUT` env var
- **C-3**: `mac-agent expire-stale-agents` CLI command
- **C-3**: `mac_expire_stale_agents` MCP tool
- **C-3**: `POST /agents/expire-stale` HTTP endpoint
- **C-4**: CLI structured logging (`logging` module replaces `print()` for diagnostics)
- **C-4**: `--verbose` / `--quiet` global CLI flags
- **C-5**: `mac-agent dashboard` command: project overview (plans, tasks, agents, conflicts, metrics)
- MCP tools count: 13 → 14

### Changed

- SPEC.md updated to v2.4 (Phase C features, `agent_timeout` in CoordinationPolicy)
- Test count: ~232 → ~240

## [0.5.0] — 2026-07-23

### Added

- **B-1**: Review packet includes quality evidence summary (`## Quality Evidence` section in `prepare_review_packet()`)
- **B-2**: Worker packet inlines upstream handoff summary for completed dependencies (`### Upstream Handoff: <dep_id>`)
- **B-3**: `expire_stale_tasks()` transitions non-terminal tasks past their TTL to `failed` with `error_code="TTL_EXPIRED"`
- **B-3**: `mac-agent expire-stale` CLI command
- **B-4**: `mac-agent next` one-shot command: atomically claim + start + output worker packet
- **B-5**: `CoordinationPolicy.reviewer_capability` field + `MAC_REVIEWER_CAPABILITY` env var — gates `accept_review()`/`reject_review()` on reviewer capability
- E2E validation script (`examples/e2e_multi_agent.py`) expanded to 17 steps covering all Phase B features
- Phase B design document (`docs/superpowers/specs/2026-07-23-mac-phase-b-design.md`)
- PyPI publishing research (`docs/research/p3-pypi-publishing.md`)
- GitHub Actions CI test workflow (Python 3.10/3.11/3.12/3.13 matrix)

### Changed

- SPEC.md updated to v2.3 (Phase B features, `reviewer_capability` in CoordinationPolicy)
- `pyproject.toml` metadata: added `authors`, `license`, `project.urls`; fixed TOML structure

## [0.4.0] — 2026-07-23

### Added

- Review lifecycle: `mark_review_ready()`, `accept_review()`, `reject_review()` (controlled by `CoordinationPolicy.require_review`)
- `reject_review()` auto-records a `ConflictRecord` with `source="reject_review"`
- `CoordinationPolicy.from_env()` reads `MAC_REQUIRE_REVIEW`, `MAC_REQUIRE_PATH_CHECK`, `MAC_MAX_RETRY_COUNT`, `MAC_PATH_RULES`
- `GET /metrics` HTTP endpoint (6 aggregate indicators)
- Review lifecycle CLI subcommand (`mac-agent review-lifecycle`)
- Review lifecycle MCP tools: `mac_mark_review_ready`, `mac_accept_review`, `mac_reject_review`
- MCP tools count: 8 → 11
- Dual state machine diagram in SPEC.md (with/without review)

### Changed

- `complete_task()` raises `StateConflictError` when `require_review=True` and task is `running`
- SPEC.md updated to v2.2
- Test count: ~211 → ~218

## [0.3.0] — 2026-07-22

### Added

- `CoordinationPolicy` model for optional feature switches (`require_review`, `require_path_check`, `path_rule`, `max_retry_count`)
- `Registry.__init__` accepts `policy` parameter
- Cycle detection in `submit_task()` rejects circular `depends_on`
- Trace metrics: 6 aggregate indicators (`compute_metrics()`, `format_table()`)
- `TaskEventBus` for in-process event publishing
- MCP Server: 8 tools + 2 resources
- HTTP adapter: FastAPI app with full REST surface
- CLI: `mac-agent` console script with subcommands

### Changed

- SPEC.md updated to v2.1
- Three-doc sync rule established (SPEC.md + CLAUDE.md + README.md)

[0.6.0]: https://github.com/JosephIvon/multi-agent-coordinator/releases/tag/v0.6.0
[0.5.0]: https://github.com/JosephIvon/multi-agent-coordinator/releases/tag/v0.5.0
[0.4.0]: https://github.com/JosephIvon/multi-agent-coordinator/releases/tag/v0.4.0
[0.3.0]: https://github.com/JosephIvon/multi-agent-coordinator/releases/tag/v0.3.0
