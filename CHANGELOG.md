# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.5.0]: https://github.com/JosephIvon/multi-agent-coordinator/releases/tag/v0.5.0
[0.4.0]: https://github.com/JosephIvon/multi-agent-coordinator/releases/tag/v0.4.0
[0.3.0]: https://github.com/JosephIvon/multi-agent-coordinator/releases/tag/v0.3.0
