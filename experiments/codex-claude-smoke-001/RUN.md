# Codex and Claude Collaboration Smoke Test -- Findings

Status: SUCCESSFUL after fixing one bug in mac-agent v1.1.0 MCP server.

## TL;DR

Codex and Claude CAN collaborate through MAC, but only after fixing the bug
where src/mac/mcp_server.py hardcoded _DB_PATH = Path(mac.db) and ignored
MAC_DB_PATH env var. After the fix, a single end-to-end handoff completed
with the upstream handoff inlined in Claude worker packet, depends_on chain
preserved, and 9 audit entries spanning two distinct actors.

## Chronology

### R1 FAILED: prompt never reached Claude
Codex prepared state, wrote HANDOFF_FOR_CLAUDE_CODE.md to disk, asked user
to feed to Claude Code. File existed but was never in Claude prompt context.
task-add-examples remained proposed. Lesson: on-disk files are not a handoff
unless explicitly in the receiving agent prompt.

### R2 FAILED: two databases, silent bug
Claude reported success but DB-side check showed depends_on empty and a
DIFFERENT database file. Root cause: mcp_server.py line 20 had _DB_PATH =
Path(mac.db) hardcoded, ignoring MAC_DB_PATH env var entirely. Codex wrote
to C:/tmp/collab-smoke/mac.db via --db; Claude Code MCP server resolved
Path(mac.db) relative to cwd and got D:/WorkSpace/multi-agent-coordinator/mac.db.
Two databases, no shared state.

### Source fix landed
Claude applied the fix per docs/superpowers/plans/2026-08-03-fix-mcp-db-path-env.md:
replaced _DB_PATH with lazy init, added _resolve_db_path that reads
os.environ.get MAC_DB_PATH default mac.db, updated all 3 usages, added
stderr log in main. 29 existing tests still pass. Manual verify: env var
honored. .pth file rewritten to point at canonical src. User deleted stale
.archive directory.

### R3b SUCCESS
Codex re-prepared on D:/WorkSpace/multi-agent-coordinator/mac.db. User drove
Claude chat via CLI path 1: claim proposed-to-accepted, start
accepted-to-running, appended 5 numbered examples to
docs/STATE_MACHINE_CHEATSHEET.md (9304 bytes), recorded quality with
pytest related tests or smoke test, ran done with running-to-completed and
gate passed, recorded handoff.

Final DB state verified by Codex:
- task-add-examples.status = completed
- target_agent_id = claude-test
- depends_on = [task-write-cheatsheet] (chain preserved)
- audit: 9 entries spanning codex-test (submit x2) and claude-test
  (claim, start, save_handoff_result x2, complete_task) plus 3 quality entries
- metrics: completed_tasks=5, handoffs=5, quality_results=23, task_transfers=13,
  quality_gate_pass_rate=1.0000, active_agents=2
- Physical file docs/STATE_MACHINE_CHEATSHEET.md modified 9304 bytes,
  contains Claude-added examples

## Findings

### What works
- Codex via CLI can register, plan, submit, complete, hand-off.
- Downstream worker packet auto-inlines upstream handoff section.
- depends_on chain preserved across agents in same DB.
- Audit trail captures both agents with actor and timestamp.
- Quality gate passes for risk=low when command and evidence match contract.
- Source fix is reversible via editable install plus .pth edit.

### What does not work or was not validated
- Cross-session context transfer without user relay: NOT WORKING. No
  auto watch-this-dir mechanism.
- Claude Code MCP stderr visibility to user: NOT EXPOSED by default UI.
- Path 2 (true Claude Code via real MCP): NOT RUN. R3b used CLI path 1.
- Editable install .pth refresh on pip install -e: BUG. .pth file does not
  auto-update when source path changes.
- handoff_success_rate showing 0.0000 despite 5 successful handoffs:
  METRIC FORMULA looks broken.

## Recommendations for mac-agent maintainers
1. Ship the env-var fix as a patch release.
2. Verify the plan-included unit tests for _resolve_db_path landed
   (env-set, env-unset, absolute path, never-None).
3. Document MAC_DB_PATH in docs/INTEGRATIONS.md and CLAUDE.md (clean-room
   three-sync rule).
4. Investigate why editable-install .pth does not refresh on pip install -e.
   Either fix pip or document an uninstall step.
5. Investigate handoff_success_rate metric formula.

## Recommendations for multi-tool coordination users
1. Verify which DB each tool sees before any cross-tool handoff. Fastest:
   launch MCP server manually with env var, grep stderr for DB path line.
2. Be skeptical of I-ran-it claims without DB-side verification. Audit
   trail is ground truth.
3. Provide handoff context IN-PROMPT, not via shared filesystem. Each AI
   tool starts fresh context.
4. On Windows, manually rewrite _editable_impl_pkg.pth after moving source.

## File inventory
- D:/WorkSpace/multi-agent-coordinator/mac.db (canonical, ~385KB after R3b)
- D:/WorkSpace/multi-agent-coordinator/mac.db.r2-history (Claude R2 orphan
  work, archived)
- D:/WorkSpace/multi-agent-coordinator/docs/STATE_MACHINE_CHEATSHEET.md
  (9304B, Claude 5 examples appended)
- D:/WorkSpace/multi-agent-coordinator/docs/superpowers/plans/2026-08-03-fix-mcp-db-path-env.md
  (source fix plan)
- D:/WorkSpace/multi-agent-coordinator/experiments/codex-claude-smoke-001/RUN.md
  (this file)
- C:/tmp/collab-smoke/mac.db.001-history (R1 Codex DB, archived)
- D:/WorkSpace/.archive/ (deleted, was stale source reference)


---

## R4 update (2026-08-04 ~12:00) --- CANONICAL Path 2 SUCCESS via real MCP

User restarted the terminal, launching Claude Code fresh. The mac-mcp-server process (PID 26536, started 11:36:29) and Claude Code (PID 4868) were both running. Initial confusion: Claude Code reported an empty task queue because R3b had already completed task-add-examples and there was no fresh work for claude-test.

### Fix: submit a fresh task via Codex CLI

Codex ran:
- mac-agent plan create -plan-id plan-smoke-003c
- mac-agent plan activate -plan-id plan-smoke-003c
- mac-agent submit -task-id task-verify-r4 -target-agent-id claude-test -depends-on task-write-cheatsheet -summary R4 append summary -risk low

This gave Claude Code a real task with depends_on pointing at Codex prior work.

### Tool set mismatch surfaced

Claude Code reported its MCP tool list did NOT match what the earlier prompt assumed. Real tool names from src/mac/mcp_server.py (19 total):
mac_submit_task, mac_claim_task, mac_done, mac_save_handoff, mac_list_ready_tasks, mac_worker_packet, mac_next_task, mac_review_packet, mac_mark_review_ready, mac_accept_review, mac_reject_review, mac_fail_task, mac_record_quality_and_complete, mac_expire_stale_tasks, mac_expire_stale_agents, mac_cleanup_tasks, mac_list_scorers, mac_set_scorer, mac_test_scorer

There is NO mac_discover, NO mac_ready_tasks, NO mac_start_task. The earlier prompt was written from CLAUDE.md docs which list 16 tools and include some that do not exist as MCP tools.

### mac_done semantics

mac_done is one-shot: it does start + complete + quality evidence in a single atomic action. The audit trail records each internal step separately (start_task, submit_quality_result, complete_task), which is why the audit log shows three claude-test actions for what looked like one tool call.

### Verification (R4 final state)

task-verify-r4.status = completed
task-verify-r4.target_agent_id = claude-test
task-verify-r4.depends_on = [task-write-cheatsheet] (chain preserved end-to-end)
task-verify-r4.updated_at = 2026-08-04T04:00:33

Audit trail for task-verify-r4 (chronological):
1. codex-test submit_task (03:56:57)
2. claude-test claim_task (04:00:33)
3. claude-test start_task (04:00:33, same second as claim -- inside mac_done)
4. quality submit_quality_result (04:00:57)
5. claude-test save_handoff_result (04:00:57)
6. claude-test complete_task (04:00:57, running to completed)
7. claude-test save_handoff_result (04:01:04, final handoff save)

Metrics updated: completed_tasks=6, handoffs=6, quality_results=24, task_transfers=14, quality_gate_pass_rate=1.0000.

### What R4 proves that R3b did not

The handoff was driven by the ACTUAL Claude Code process through the ACTUAL mac-mcp-server over stdio. R3b was driven by a separate AI session via the CLI, which exercises the same registry/storage code but bypasses the MCP transport.

The transport layer (MCP stdio, JSON-RPC over stdout, stderr for logs) works end-to-end. Codex never sees that transport; only Claude Code does.

The mac-mcp-server silently honored MAC_DB_PATH being absent (or matching cwd) and resolved to D:/WorkSpace/multi-agent-coordinator/mac.db. The fix shipped upstream is what made this reliable -- without it, this run would have failed with two databases just like R2.

