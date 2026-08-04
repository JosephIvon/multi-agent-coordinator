from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, cast

logger = logging.getLogger("mac")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mac-agent")
    parser.add_argument("--verbose", action="store_true", help="Show debug-level output")
    parser.add_argument("--quiet", action="store_true", help="Suppress non-error output")
    subcommands = parser.add_subparsers(dest="command", required=True)

    adapter = subcommands.add_parser("adapter", help="Discover and inspect IDE adapters")
    adapter_subcommands = adapter.add_subparsers(dest="adapter_command", required=True)
    adapter_subcommands.add_parser("list", help="List installed adapters")
    adapter_inspect = adapter_subcommands.add_parser("inspect", help="Inspect an adapter manifest")
    adapter_inspect.add_argument("adapter_id")
    adapter_run = adapter_subcommands.add_parser("run", help="Run a task through a CLI adapter and sync its result")
    adapter_run.add_argument("adapter_id", choices=["generic-cli"])
    adapter_run.add_argument("--db", default=_cli_db_arg())
    adapter_run.add_argument("--task-id", required=True)
    adapter_run.add_argument("--agent-id", required=True)
    adapter_run.add_argument("--command", dest="adapter_command_line", nargs=argparse.REMAINDER, required=True)
    adapter_run.add_argument("--output-dir", default=".agent-context")
    adapter_run.add_argument("--cwd")
    adapter_run.add_argument("--timeout", type=float, default=3600)
    adapter_run.add_argument("--quality-command")
    adapter_run.add_argument("--quality-evidence", action="append", default=[])

    context = subcommands.add_parser("context", help="Materialize a portable task context for an adapter")
    context.add_argument("--db", default=_cli_db_arg())
    context.add_argument("--task-id", required=True)
    context.add_argument("--agent-id")
    context.add_argument("--adapter", dest="adapter_id", default="generic-context")
    context.add_argument("--output-dir", default=".agent-context")

    bootstrap = subcommands.add_parser("bootstrap", help="Create tool-neutral project context files")
    bootstrap.add_argument("--db", default=_cli_db_arg())
    bootstrap.add_argument("--task-id")
    bootstrap.add_argument("--agent-id")
    bootstrap.add_argument("--adapter", dest="adapter_id", default="generic-context")
    bootstrap.add_argument("--output-dir", default=".agent-context")
    bootstrap.add_argument("--project-root", default=".")
    contract = subcommands.add_parser("contract", help="Generate a risk-based test contract")
    contract.add_argument("--risk", choices=["low", "medium", "high"], required=True)
    contract.add_argument("--custom-command", action="append", default=[], help="Override default commands (repeatable)")
    contract.add_argument("--custom-evidence", action="append", default=[], help="Override default evidence names (repeatable)")

    register = subcommands.add_parser("register", help="Register an agent in the local ledger")
    register.add_argument("--db", default=_cli_db_arg())
    register.add_argument("--agent-id", required=True)
    register.add_argument("--name", required=True)
    register.add_argument("--capability", action="append", required=True)
    register.add_argument("--project-context")
    register.add_argument("--load", type=int, default=0)
    register.add_argument("--allowed-path", action="append", default=[])
    register.add_argument("--forbidden-path", action="append", default=[])

    discover = subcommands.add_parser("discover", help="Discover agents by capability")
    discover.add_argument("--db", default=_cli_db_arg())
    discover.add_argument("--capability", required=True)
    discover.add_argument("--project-context")

    submit = subcommands.add_parser("submit", help="Submit a task to the local ledger")
    submit.add_argument("--db", default=_cli_db_arg())
    submit.add_argument("--task-id", required=True)
    submit.add_argument("--trace-id")
    submit.add_argument("--source-agent-id", required=True)
    submit.add_argument("--target-agent-id")
    submit.add_argument("--type", required=True)
    submit.add_argument("--summary", required=True)
    submit.add_argument("--target-module")
    submit.add_argument("--coverage-goal", type=int)
    submit.add_argument("--risk", choices=["low", "medium", "high"])
    submit.add_argument("--context-ref", action="append", default=[])
    submit.add_argument("--plan-id")
    submit.add_argument("--depends-on", action="append", default=[])
    submit.add_argument("--custom-command", action="append", default=[], help="Custom verification commands for test contract (repeatable)")
    submit.add_argument("--custom-evidence", action="append", default=[], help="Custom evidence names for test contract (repeatable)")
    submit.add_argument("--spec-json", help="Structured spec as JSON string (stored in task.metadata.spec)")

    status = subcommands.add_parser("status", help="Print task status")
    status.add_argument("--db", default=_cli_db_arg())
    status.add_argument("--task-id", required=True)

    tasks = subcommands.add_parser("tasks", help="List tasks from the local ledger")
    tasks.add_argument("--db", default=_cli_db_arg())
    tasks.add_argument("--status")
    tasks.add_argument("--capability")
    tasks.add_argument("--agent-id")
    tasks.add_argument("--project-context")

    plan = subcommands.add_parser("plan", help="Manage collaboration plans (bare = list)")
    plan.add_argument("--db", default=_cli_db_arg())
    plan.add_argument("--status", default=None)
    plan_subcommands = plan.add_subparsers(dest="plan_command")
    plan.set_defaults(plan_command="list", db="mac.db", status=None)
    plan_create = plan_subcommands.add_parser("create", help="Create a collaboration plan")
    plan_create.add_argument("--db", default=_cli_db_arg())
    plan_create.add_argument("--plan-id")
    plan_create.add_argument("--goal", required=True)
    plan_create.add_argument("--created-by", default="")
    plan_activate = plan_subcommands.add_parser("activate", help="Activate a collaboration plan")
    plan_activate.add_argument("--db", default=_cli_db_arg())
    plan_activate.add_argument("--plan-id", required=True)
    plan_close = plan_subcommands.add_parser("close", help="Close a collaboration plan")
    plan_close.add_argument("--db", default=_cli_db_arg())
    plan_close.add_argument("--plan-id", required=True)
    plan_close.add_argument("--status", choices=["completed", "cancelled"], default="completed")
    plan_list = plan_subcommands.add_parser("list", help="List collaboration plans")
    plan_list.add_argument("--db", default=_cli_db_arg())
    plan_list.add_argument("--status")

    ready_tasks = subcommands.add_parser("ready-tasks", help="List dependency-unblocked proposed tasks")
    ready_tasks.add_argument("--db", default=_cli_db_arg())
    ready_tasks.add_argument("--agent-id")
    ready_tasks.add_argument("--capability")
    ready_tasks.add_argument("--project-context")

    metrics = subcommands.add_parser("metrics", help="Show collaboration trace metrics")
    metrics.add_argument("--db", default=_cli_db_arg())
    metrics.add_argument("--json", action="store_true", help="Emit metrics as JSON")

    handoff = subcommands.add_parser("handoff", help="Save or print a structured task handoff")
    handoff.add_argument("--db", default=_cli_db_arg())
    handoff.add_argument("--task-id", required=True)
    handoff.add_argument("--agent-id")
    handoff.add_argument("--plan-id")
    handoff.add_argument("--verification", action="append", default=[])
    handoff.add_argument("--changed-file", action="append", default=[])
    handoff.add_argument("--doc", action="append", default=[])
    handoff.add_argument("--risk", action="append", default=[])

    record_conflict = subcommands.add_parser("record-conflict", help="Record a collaboration conflict")
    record_conflict.add_argument("--db", default=_cli_db_arg())
    record_conflict.add_argument("--conflict-id")
    record_conflict.add_argument("--plan-id")
    record_conflict.add_argument("--task-id")
    record_conflict.add_argument("--source", required=True)
    record_conflict.add_argument("--severity", choices=["blocking", "non_blocking"], default="non_blocking")
    desc_grp = record_conflict.add_mutually_exclusive_group(required=True)
    desc_grp.add_argument("--description", help="Conflict description (canonical flag, matches ConflictRecord schema)")
    desc_grp.add_argument("--reason", help="Alias for --description")
    record_conflict.add_argument("--agent", action="append", default=[])
    record_conflict.add_argument("--file", action="append", default=[])

    conflicts = subcommands.add_parser("conflicts", help="List collaboration conflicts")
    conflicts.add_argument("--db", default=_cli_db_arg())
    conflicts.add_argument("--plan-id")
    conflicts.add_argument("--resolved", action="store_true")
    conflicts.add_argument("--unresolved", action="store_true")

    resolve_conflict = subcommands.add_parser("resolve-conflict", help="Resolve a collaboration conflict")
    resolve_conflict.add_argument("--db", default=_cli_db_arg())
    resolve_conflict.add_argument("--conflict-id", required=True)
    resolve_conflict.add_argument("--resolution", required=True)

    worker_packet = subcommands.add_parser("worker-packet", help="Print a worker task packet")
    worker_packet.add_argument("--db", default=_cli_db_arg())
    worker_packet.add_argument("--task-id", required=True)
    worker_packet.add_argument("--agent-id")

    review_packet = subcommands.add_parser("review-packet", help="Print a review task packet")
    review_packet.add_argument("--db", default=_cli_db_arg())
    review_packet.add_argument("--task-id", required=True)

    task_evidence = subcommands.add_parser("task-evidence", help="Print a task evidence bundle")
    task_evidence.add_argument("--db", default=_cli_db_arg())
    task_evidence.add_argument("--task-id", required=True)

    quality_preview = subcommands.add_parser("quality-preview", help="Preview whether task quality evidence satisfies its contract")
    quality_preview.add_argument("--db", default=_cli_db_arg())
    quality_preview.add_argument("--task-id", required=True)

    task_readiness = subcommands.add_parser("task-readiness", help="Preview a task's recommended next action")
    task_readiness.add_argument("--db", default=_cli_db_arg())
    task_readiness.add_argument("--task-id", required=True)

    accept = subcommands.add_parser("accept", help="Accept a task handoff")
    accept.add_argument("--db", default=_cli_db_arg())
    accept.add_argument("--task-id", required=True)
    accept.add_argument("--agent-id", required=True)

    start = subcommands.add_parser("start", help="Mark a task running")
    start.add_argument("--db", default=_cli_db_arg())
    start.add_argument("--task-id", required=True)
    start.add_argument("--agent-id", required=True)

    quality = subcommands.add_parser("quality", help="Record quality evidence")
    quality.add_argument("--db", default=_cli_db_arg())
    quality.add_argument("--task-id", required=True)
    quality.add_argument("--command", dest="quality_command", required=True)
    quality.add_argument("--status", choices=["passed", "failed"], required=True)
    quality.add_argument("--evidence", action="append", default=[])

    complete = subcommands.add_parser("complete", help="Complete a task after quality gate")
    complete.add_argument("--db", default=_cli_db_arg())
    complete.add_argument("--task-id", required=True)
    complete.add_argument("--agent-id", required=True)

    done = subcommands.add_parser("done", help="Finish a task in one step: quality + handoff + complete (or review-ready)")
    done.add_argument("--db", default=_cli_db_arg())
    done.add_argument("--task-id", required=True)
    done.add_argument("--agent-id", required=True)
    done.add_argument("--quality-command", help="Quality check command (e.g. 'pytest -q')")
    done.add_argument("--quality-status", choices=["passed", "failed"], help="Quality check result")
    done.add_argument("--evidence", action="append", default=[], help="Quality evidence items")
    done.add_argument("--changed-file", action="append", default=[], help="Files changed by this task")
    done.add_argument("--risk", action="append", default=[], help="Residual risks")
    done.add_argument("--verification", action="append", default=[], help="Verification entries (command:result:description)")

    fail = subcommands.add_parser("fail", help="Mark a task failed")
    fail.add_argument("--db", default=_cli_db_arg())
    fail.add_argument("--task-id", required=True)
    fail.add_argument("--agent-id", required=True)
    fail.add_argument("--error-code", required=True)
    fail.add_argument("--message", default="")

    checkpoint = subcommands.add_parser("checkpoint", help="Record a task recovery checkpoint")
    checkpoint.add_argument("--db", default=_cli_db_arg())
    checkpoint.add_argument("--task-id", required=True)
    checkpoint.add_argument("--agent-id", required=True)
    checkpoint.add_argument("--summary", required=True)
    checkpoint.add_argument("--artifact-ref", action="append", default=[])

    retry = subcommands.add_parser("retry", help="Retry a failed task")
    retry.add_argument("--db", default=_cli_db_arg())
    retry.add_argument("--task-id", required=True)
    retry.add_argument("--agent-id", required=True)
    retry.add_argument("--fallback-agent-id")

    review_lifecycle = subcommands.add_parser(
        "review-lifecycle",
        help="Mark, accept, or reject a task review",
    )
    review_lifecycle.add_argument("--db", default=_cli_db_arg())
    review_lifecycle.add_argument("--action", choices=["mark-ready", "accept", "reject"], required=True)
    review_lifecycle.add_argument("--task-id", required=True)
    review_lifecycle.add_argument("--agent-id")
    review_lifecycle.add_argument("--reviewer-id")
    review_lifecycle.add_argument("--reason", default="")
    review_lifecycle.add_argument("--plan-id")

    cancel = subcommands.add_parser("cancel", help="Cancel a task")
    cancel.add_argument("--db", default=_cli_db_arg())
    cancel.add_argument("--task-id", required=True)
    cancel.add_argument("--agent-id", required=True)
    cancel.add_argument("--reason", default="")

    audit = subcommands.add_parser("audit", help="Print audit trail by trace id or task id")
    audit.add_argument("--db", default=_cli_db_arg())
    audit_group = audit.add_mutually_exclusive_group(required=True)
    audit_group.add_argument("--trace-id", help="Trace ID to look up")
    audit_group.add_argument("--task-id", help="Task ID; trace ID is resolved via Registry.get_task()")

    expire = subcommands.add_parser("expire-stale", help="Expire tasks past their TTL")
    expire.add_argument("--db", default=_cli_db_arg())
    expire.add_argument("--auto-retry", action="store_true", help="Auto-retry tasks with retries remaining")

    expire_agents = subcommands.add_parser("expire-stale-agents", help="Set offline agents with stale heartbeats")
    expire_agents.add_argument("--db", default=_cli_db_arg())
    expire_agents.add_argument("--timeout", type=int, default=None, help="Timeout in seconds (default: from policy)")

    cleanup = subcommands.add_parser("cleanup", help="Delete terminal tasks (failed/cancelled/rejected/superseded)")
    cleanup.add_argument("--db", default=_cli_db_arg())
    cleanup.add_argument("--status", action="append", default=[], help="Status to clean (repeatable, default: failed,cancelled,rejected,superseded)")
    cleanup.add_argument("--plan-id", help="Only clean tasks in this plan")
    cleanup.add_argument("--older-than", type=int, default=None, help="Only clean tasks older than N seconds")

    dashboard = subcommands.add_parser("dashboard", help="Show project overview: plans, tasks, agents, conflicts, metrics")
    dashboard.add_argument("--db", default=_cli_db_arg())

    next_cmd = subcommands.add_parser(
        "next", help="Claim + start the next ready task and print its worker packet"
    )
    next_cmd.add_argument("--db", default=_cli_db_arg())
    next_cmd.add_argument("--agent-id", required=True)
    next_cmd.add_argument("--capability", required=True)
    next_cmd.add_argument("--best-effort", action="store_true")

    observe = subcommands.add_parser("observe", help="Record an observed agent outcome")
    observe.add_argument("--db", default=_cli_db_arg())
    observe.add_argument("--agent-id", required=True)
    observe.add_argument("--capability", required=True)
    observe.add_argument("--task-type", required=True)
    observe.add_argument("--status", choices=["succeeded", "failed"], required=True)
    observe.add_argument("--duration", type=float, required=True)
    observe.add_argument("--error-code")

    score = subcommands.add_parser("capability-score", help="Print observed capability score")
    score.add_argument("--db", default=_cli_db_arg())
    score.add_argument("--agent-id", required=True)
    score.add_argument("--capability", required=True)

    scoring = subcommands.add_parser(
        "scoring",
        help="Inspect or dry-run scoring hooks for list_ready_tasks",
    )
    scoring_sub = scoring.add_subparsers(dest="scoring_command", required=True)
    scoring_sub.add_parser("list", help="List registered sync + async scorers")
    scoring_test = scoring_sub.add_parser(
        "test", help="Dry-run a named scorer against proposed tasks"
    )
    scoring_test.add_argument("--name", required=True, help="Scorer name registered in mac.scoring")
    scoring_test.add_argument("--db", default=_cli_db_arg())
    scoring_test.add_argument("--limit", type=int, default=5)
    scoring_test.add_argument("--project-context")

    claim = subcommands.add_parser("claim", help="Claim the next proposed task by capability")
    claim.add_argument("--db", default=_cli_db_arg())
    claim.add_argument("--agent-id", required=True)
    claim.add_argument("--capability", required=True)
    claim.add_argument("--project-context")
    claim.add_argument("--best-effort", action="store_true")

    run_once = subcommands.add_parser("run-once", help="Run one local agent adapter cycle")
    run_once.add_argument("--db", default=_cli_db_arg())
    run_once.add_argument("--agent-id", required=True)
    run_once.add_argument("--name", required=True)
    run_once.add_argument("--capability", required=True)
    run_once.add_argument("--project-context")
    run_once.add_argument("--timeout", type=float, default=60)
    run_once.add_argument("--command", dest="run_command", nargs=argparse.REMAINDER, required=True)

    return parser


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _parse_verification(value: str):
    from mac.protocol.messages import VerificationEntry

    command, result, description = (value.split(":", 2) + ["", ""])[:3]
    return VerificationEntry(command=command, result=cast(Literal["pass", "fail"], result), description=description)


def _cmd_scoring_list() -> int:
    from mac import scoring

    payload = {
        "sync": [
            {
                "name": name,
                "qualname": getattr(
                    scoring.get_scorer(name), "__qualname__", repr(scoring.get_scorer(name))
                ),
            }
            for name in sorted(scoring.list_scorers().keys())
        ],
        "async": [
            {
                "name": name,
                "qualname": getattr(
                    scoring.get_async_scorer(name),
                    "__qualname__",
                    repr(scoring.get_async_scorer(name)),
                ),
            }
            for name in sorted(scoring.list_async_scorers().keys())
        ],
    }
    _print_json(payload)
    return 0


def _cmd_scoring_test(args) -> int:
    import asyncio

    from mac.registry import Registry
    from mac.storage.sqlite import SQLiteTaskLedger

    # Always build a fresh Registry so the CLI is read-only and never
    # touches whatever scorer the MCP server has installed. If the
    # requested name is unknown we degrade to a no-hook registry so the
    # operator still gets a snapshot of the proposed tasks.
    try:
        registry = Registry(SQLiteTaskLedger(Path(args.db)), scoring_fn=args.name)
    except ValueError:
        registry = Registry(SQLiteTaskLedger(Path(args.db)))
    tasks = registry.ledger.list_task_transfers(
        status="proposed", project_context=args.project_context
    )
    tasks = tasks[: max(0, int(args.limit))]
    if registry._async_scoring_fn is not None:
        async def _gather():
            coros = [registry._async_scoring_fn(t) for t in tasks]
            raw = await asyncio.gather(*coros, return_exceptions=True)
            return [registry._to_async_score(r) for r in raw]
        scores_list = asyncio.run(_gather())
    elif registry._scoring_fn is not None:
        scores_list = [
            registry._to_async_score(registry._scoring_fn(t)) for t in tasks
        ]
    else:
        # Unknown scorer or no hook installed; surface zeros rather than
        # crashing so the operator still sees the proposed tasks.
        scores_list = [0.0 for _ in tasks]
    payload = {
        "scorer": args.name,
        "scored": [
            {"task_id": t.task_id, "score": s, "priority": t.priority}
            for t, s in zip(tasks, scores_list, strict=True)
        ],
    }
    _print_json(payload)
    return 0


def _cli_db_arg() -> str:
    """Return the default value for ``--db`` across CLI subcommands.

    Falls back from ``MAC_DB_PATH`` env var to ``mac.db``, matching the
    resolution logic in ``mcp_server._resolve_db_path()``.
    """
    return os.environ.get("MAC_DB_PATH", "mac.db")

def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # Configure logging based on global flags.
    if getattr(args, "verbose", False):
        level = logging.DEBUG
    elif getattr(args, "quiet", False):
        level = logging.WARNING
    else:
        level = logging.INFO
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stderr)

    if args.command == "adapter":
        from mac.adapters import discover_adapters

        adapters = discover_adapters()
        if args.adapter_command == "list":
            _print_json([a.manifest.to_dict() for a in adapters.values()])
            return 0
        adapter = adapters.get(args.adapter_id)
        if adapter is None:
            logger.error("Unknown adapter: %s", args.adapter_id)
            return 2
        if args.adapter_command == "run":
            from mac.adapters.generic import GenericCliAdapter
            from mac.protocol.messages import HandoffResult
            from mac.registry import Registry
            from mac.storage.sqlite import SQLiteStorage
            cli_adapter = cast(GenericCliAdapter, adapter)
            registry = Registry(SQLiteStorage(Path(args.db)))
            packet = registry.prepare_worker_packet(args.task_id, agent_id=args.agent_id)
            prepared = adapter.prepare_context(task_id=args.task_id, context=packet, output_dir=Path(args.output_dir))
            dispatch_result = cli_adapter.dispatch(prepared, args.adapter_command_line, cwd=args.cwd, timeout=args.timeout)
            normalized = cli_adapter.normalize_result(dispatch_result)
            if normalized.status == "completed":
                handoff = HandoffResult(task_id=args.task_id, agent_id=args.agent_id, risks=[], changed_files=[])
                synced = registry.done(
                    args.task_id,
                    args.agent_id,
                    quality_result={"command": args.quality_command, "status": "passed", "evidence": args.quality_evidence}
                    if args.quality_command else None,
                    handoff=handoff,
                )
                _print_json({"result": normalized.to_dict(), "sync": synced})
                return 0
            _print_json({"result": normalized.to_dict(), "sync": "not-completed"})
            return 1
        _print_json(adapter.manifest.to_dict())
        return 0

    if args.command == "bootstrap":
        from mac.bootstrap import bootstrap_project
        generated = bootstrap_project(args.project_root)
        if args.task_id is None:
            _print_json({"generated": [str(path) for path in generated], "source_of_truth": "mac.db"})
            return 0

    if args.command in {"context", "bootstrap"}:
        from mac.adapters import discover_adapters
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        adapter = discover_adapters().get(args.adapter_id)
        if adapter is None:
            logger.error("Unknown adapter: %s", args.adapter_id)
            return 2
        registry = Registry(SQLiteStorage(Path(args.db)))
        packet = registry.prepare_worker_packet(args.task_id, agent_id=args.agent_id)
        output_dir = Path(args.output_dir)
        prepared = adapter.prepare_context(task_id=args.task_id, context=packet, output_dir=output_dir)
        if args.command == "bootstrap":
            output_dir.mkdir(parents=True, exist_ok=True)
            tasks = registry.list_tasks()
            plans = registry.list_plans()
            conflicts = registry.list_conflicts(resolved=False)
            state = {
                "schema_version": "1.0",
                "task_id": args.task_id,
                "agent_id": args.agent_id,
                "tasks": [{"task_id": t.task_id, "status": t.status, "summary": t.payload.summary} for t in tasks],
                "plans": [{"plan_id": p.plan_id, "status": p.status, "goal": p.goal} for p in plans],
                "unresolved_conflicts": len(conflicts),
            }
            (output_dir / "current-task.md").write_text(packet, encoding="utf-8")
            (output_dir / "project-state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            (output_dir / "README.md").write_text(
                "# MAC Project Context\n\n"
                "Generated from the MAC ledger. Do not edit these files as a source of truth.\n"
                "Start work from `current-task.md`; inspect `project-state.json`; record completion with `mac-agent done`.\n",
                encoding="utf-8",
            )
        _print_json({"task_id": args.task_id, "adapter": args.adapter_id, "path": str(prepared.path)})
        return 0
    if args.command == "contract":
        from mac.testing.contracts import TestContract

        custom_commands = args.custom_command or None
        custom_evidence = args.custom_evidence or None
        _print_json(
            TestContract.for_risk(
                args.risk,
                custom_commands=custom_commands,
                custom_evidence=custom_evidence,
            ).model_dump()
        )
        return 0

    if args.command == "register":
        from mac.protocol.messages import AgentCapability, AgentCard
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        storage = SQLiteStorage(Path(args.db))
        registry = Registry(storage)
        card = AgentCard(
            agent_id=args.agent_id,
            name=args.name,
            capabilities=[AgentCapability(name=name) for name in args.capability],
            load=args.load,
            project_context=args.project_context,
            allowed_paths=args.allowed_path,
            forbidden_paths=args.forbidden_path,
        )
        registry.register(card)
        _print_json({"agent_id": card.agent_id, "status": "registered"})
        return 0

    if args.command == "discover":
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        storage = SQLiteStorage(Path(args.db))
        registry = Registry(storage)
        cards = registry.discover(args.capability, project_context=args.project_context)
        _print_json([card.model_dump() for card in cards])
        return 0

    if args.command == "submit":
        from typing import Any

        from mac.protocol.messages import ContextBundle, TaskPayload, TaskTransfer
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage
        from mac.testing.contracts import TestContract

        payload = TaskPayload(
            type=args.type,
            summary=args.summary,
            target_module=args.target_module,
            coverage_goal=args.coverage_goal,
            risk_level=args.risk,
        )
        custom_commands = args.custom_command or None
        custom_evidence = args.custom_evidence or None
        metadata: dict[str, Any] = {}
        if args.spec_json:
            try:
                metadata["spec"] = json.loads(args.spec_json)
            except (json.JSONDecodeError, TypeError) as exc:
                logger.error("Invalid --spec-json: %s", exc)
                return 1
        task = TaskTransfer(
            task_id=args.task_id,
            trace_id=args.trace_id or args.task_id,
            source_agent_id=args.source_agent_id,
            target_agent_id=args.target_agent_id,
            payload=payload,
            context=ContextBundle(summary=args.summary, artifact_refs=args.context_ref),
            test_contract=TestContract.for_risk(
                args.risk,
                custom_commands=custom_commands,
                custom_evidence=custom_evidence,
            ) if args.risk else None,
            plan_id=args.plan_id,
            depends_on=args.depends_on,
            metadata=metadata,
        )
        registry = Registry(SQLiteStorage(Path(args.db)))
        _print_json(registry.submit_task(task).model_dump(mode="json"))
        return 0

    if args.command == "status":
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        task = Registry(SQLiteStorage(Path(args.db))).get_task(args.task_id)
        _print_json(task.model_dump(mode="json") if task is not None else None)
        return 0

    if args.command == "tasks":
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        tasks = Registry(SQLiteStorage(Path(args.db))).list_tasks(
            status=args.status,
            capability=args.capability,
            agent_id=args.agent_id,
            project_context=args.project_context,
        )
        _print_json([task.model_dump(mode="json") for task in tasks])
        return 0

    if args.command == "plan":
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        registry = Registry(SQLiteStorage(Path(args.db)))
        if args.plan_command == "create":
            plan = registry.create_plan(goal=args.goal, created_by=args.created_by, plan_id=args.plan_id)
            _print_json(plan.model_dump(mode="json"))
            return 0
        if args.plan_command == "activate":
            plan = registry.activate_plan(args.plan_id)
            _print_json(plan.model_dump(mode="json"))
            return 0
        if args.plan_command == "close":
            plan = registry.close_plan(args.plan_id, status=args.status)
            _print_json(plan.model_dump(mode="json"))
            return 0
        if args.plan_command == "list":
            plans = registry.list_plans(status=args.status)
            _print_json([plan.model_dump(mode="json") for plan in plans])
            return 0

    if args.command == "ready-tasks":
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        tasks = Registry(SQLiteStorage(Path(args.db))).list_ready_tasks(
            agent_id=args.agent_id,
            capability=args.capability,
            project_context=args.project_context,
        )
        _print_json([task.model_dump(mode="json") for task in tasks])
        return 0

    if args.command == "metrics":
        from mac.metrics import compute_metrics, format_table
        from mac.storage.sqlite import SQLiteStorage

        computed = compute_metrics(SQLiteStorage(Path(args.db)))
        if args.json:
            print(json.dumps(computed, ensure_ascii=False, indent=2))
        else:
            print(format_table(computed))
        return 0

    if args.command == "handoff":
        from mac.protocol.messages import HandoffResult
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        registry = Registry(SQLiteStorage(Path(args.db)))
        if args.agent_id is None:
            handoff_result = registry.get_handoff_result(args.task_id)
            _print_json(handoff_result.model_dump(mode="json") if handoff_result is not None else None)
            return 0
        handoff_result = HandoffResult(
            task_id=args.task_id,
            plan_id=args.plan_id,
            agent_id=args.agent_id,
            verification=[_parse_verification(value) for value in args.verification],
            changed_files=args.changed_file,
            docs_touched=args.doc,
            risks=args.risk,
        )
        saved = registry.save_handoff_result(handoff_result)
        _print_json(saved.model_dump(mode="json"))
        return 0

    if args.command == "record-conflict":
        from mac.protocol.messages import ConflictRecord
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        conflict_data = {
            "plan_id": args.plan_id,
            "task_id": args.task_id,
            "source": args.source,
            "severity": args.severity,
            "description": args.description or args.reason,
            "involved_agents": args.agent,
            "involved_files": args.file,
        }
        if args.conflict_id:
            conflict_data["conflict_id"] = args.conflict_id
        conflict = ConflictRecord(**conflict_data)
        recorded = Registry(SQLiteStorage(Path(args.db))).record_conflict(conflict)
        _print_json(recorded.model_dump(mode="json"))
        return 0

    if args.command == "conflicts":
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        resolved = None
        if args.resolved:
            resolved = True
        if args.unresolved:
            resolved = False
        conflicts = Registry(SQLiteStorage(Path(args.db))).list_conflicts(plan_id=args.plan_id, resolved=resolved)
        _print_json([conflict.model_dump(mode="json") for conflict in conflicts])
        return 0

    if args.command == "resolve-conflict":
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        conflict = Registry(SQLiteStorage(Path(args.db))).resolve_conflict(args.conflict_id, args.resolution)
        _print_json(conflict.model_dump(mode="json"))
        return 0

    if args.command == "worker-packet":
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        print(Registry(SQLiteStorage(Path(args.db))).prepare_worker_packet(args.task_id, agent_id=args.agent_id), end="")
        return 0

    if args.command == "review-packet":
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        print(Registry(SQLiteStorage(Path(args.db))).prepare_review_packet(args.task_id), end="")
        return 0

    if args.command == "task-evidence":
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        bundle = Registry(SQLiteStorage(Path(args.db))).get_task_evidence(args.task_id)
        _print_json(bundle.model_dump(mode="json") if bundle is not None else None)
        return 0

    if args.command == "quality-preview":
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        preview = Registry(SQLiteStorage(Path(args.db))).preview_quality_gate(args.task_id)
        _print_json(preview.model_dump(mode="json") if preview is not None else None)
        return 0

    if args.command == "task-readiness":
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        report = Registry(SQLiteStorage(Path(args.db))).preview_task_readiness(args.task_id)
        _print_json(report.model_dump(mode="json") if report is not None else None)
        return 0

    if args.command == "accept":
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        task = Registry(SQLiteStorage(Path(args.db))).accept_handoff(args.task_id, args.agent_id)
        _print_json(task.model_dump(mode="json"))
        return 0

    if args.command == "start":
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        task = Registry(SQLiteStorage(Path(args.db))).start_task(args.task_id, args.agent_id)
        _print_json(task.model_dump(mode="json"))
        return 0

    if args.command == "quality":
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        Registry(SQLiteStorage(Path(args.db))).submit_quality_result(
            args.task_id,
            {"command": args.quality_command, "status": args.status, "evidence": args.evidence},
        )
        _print_json({"task_id": args.task_id, "status": "recorded"})
        return 0

    if args.command == "complete":
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        task = Registry(SQLiteStorage(Path(args.db))).complete_task(args.task_id, args.agent_id)
        _print_json(task.model_dump(mode="json"))
        return 0

    if args.command == "done":
        from mac.protocol.messages import HandoffResult
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        registry = Registry(SQLiteStorage(Path(args.db)))

        # Build quality_result if quality-command was provided.
        quality_result = None
        if args.quality_command:
            quality_result = {
                "command": args.quality_command,
                "status": args.quality_status or "passed",
                "evidence": args.evidence,
            }

        # Build handoff if any handoff fields were provided.
        handoff = None
        if args.changed_file or args.risk or args.verification:
            handoff = HandoffResult(
                task_id=args.task_id,
                agent_id=args.agent_id,
                verification=[_parse_verification(v) for v in args.verification],
                changed_files=args.changed_file,
                risks=args.risk,
            )

        result = registry.done(
            args.task_id,
            args.agent_id,
            quality_result=quality_result,
            handoff=handoff,
        )
        _print_json(result)
        return 0

    if args.command == "fail":
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        task = Registry(SQLiteStorage(Path(args.db))).fail_task(
            args.task_id,
            args.agent_id,
            args.error_code,
            message=args.message,
        )
        _print_json(task.model_dump(mode="json"))
        return 0

    if args.command == "checkpoint":
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        task = Registry(SQLiteStorage(Path(args.db))).record_checkpoint(
            args.task_id,
            agent_id=args.agent_id,
            checkpoint={"summary": args.summary, "artifact_refs": args.artifact_ref},
        )
        _print_json(task.model_dump(mode="json"))
        return 0

    if args.command == "retry":
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        task = Registry(SQLiteStorage(Path(args.db))).retry_task(
            args.task_id,
            agent_id=args.agent_id,
            fallback_agent_id=args.fallback_agent_id,
        )
        _print_json(task.model_dump(mode="json"))
        return 0

    if args.command == "cancel":
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        task = Registry(SQLiteStorage(Path(args.db))).cancel_task(
            args.task_id,
            agent_id=args.agent_id,
            reason=args.reason,
        )
        _print_json(task.model_dump(mode="json"))
        return 0

    if args.command == "review-lifecycle":
        from mac.protocol.messages import HandoffResult, VerificationEntry
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        registry = Registry(SQLiteStorage(Path(args.db)))
        if args.action == "mark-ready":
            handoff = (
                HandoffResult(
                    task_id=args.task_id,
                    plan_id=args.plan_id or "",
                    agent_id=args.agent_id or "",
                    verification=[
                        VerificationEntry(
                            command="cli mark-review-ready",
                            result="pass",
                        )
                    ],
                )
                if args.agent_id
                else None
            )
            task = registry.mark_review_ready(
                args.task_id,
                agent_id=args.agent_id,
                handoff=handoff,
            )
        elif args.action == "accept":
            task = registry.accept_review(args.task_id, reviewer_id=args.reviewer_id or "")
        else:
            task = registry.reject_review(
                args.task_id,
                reviewer_id=args.reviewer_id or "",
                reason=args.reason,
            )
        _print_json(task.model_dump(mode="json"))
        return 0

    if args.command == "audit":
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        registry = Registry(SQLiteStorage(Path(args.db)))
        trace_id = args.trace_id
        if trace_id is None:
            task = registry.get_task(args.task_id)
            if task is None:
                _print_json({"error": "task_not_found", "task_id": args.task_id})
                return 1
            trace_id = task.trace_id
        entries = registry.get_audit_trail(trace_id)
        _print_json([entry.model_dump(mode="json") for entry in entries])
        return 0

    if args.command == "expire-stale":
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        auto_retry = getattr(args, "auto_retry", False)
        expired = Registry(SQLiteStorage(Path(args.db))).expire_stale_tasks(auto_retry=auto_retry)
        if expired:
            for task in expired:
                action = "Retried" if task.status == "proposed" else "Expired"
                logger.info("%s: %s (%s)", action, task.task_id, task.status)
        else:
            logger.info("No stale tasks found.")
        return 0

    if args.command == "expire-stale-agents":
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        timeout = getattr(args, "timeout", None)
        expired_agents = Registry(SQLiteStorage(Path(args.db))).expire_stale_agents(timeout_seconds=timeout)
        if expired_agents:
            for agent in expired_agents:
                logger.info("Expired: %s (offline)", agent.agent_id)
        else:
            logger.info("No stale agents found.")
        return 0

    if args.command == "cleanup":
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        statuses = args.status or None
        older_than = args.older_than
        deleted = Registry(SQLiteStorage(Path(args.db))).cleanup_tasks(
            statuses=statuses,
            plan_id=args.plan_id,
            older_than_seconds=float(older_than) if older_than is not None else None,
        )
        if deleted:
            for task in deleted:
                logger.info("Deleted: %s (%s)", task.task_id, task.status)
        else:
            logger.info("No terminal tasks to clean up.")
        _print_json({"deleted_count": len(deleted)})
        return 0

    if args.command == "dashboard":
        from mac.metrics import compute_metrics, format_table
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        reg = Registry(SQLiteStorage(Path(args.db)))

        # Plans
        plans = reg.list_plans(status="active")
        print("MAC Dashboard")
        print("=" * 50)
        if plans:
            print(f"\nPlans ({len(plans)} active):")
            for p in plans:
                tasks_in_plan = [t for t in reg.list_tasks() if t.plan_id == p.plan_id]
                by_status: dict[str, int] = {}
                for t in tasks_in_plan:
                    by_status[t.status] = by_status.get(t.status, 0) + 1
                status_str = ", ".join(f"{v} {k}" for k, v in sorted(by_status.items()))
                print(f"  {p.plan_id}  {status_str or 'no tasks'}")
        else:
            print("\nPlans: none active")

        # Tasks
        all_tasks = reg.list_tasks()
        ready = reg.list_ready_tasks()
        running = [t for t in all_tasks if t.status == "running"]
        review_ready = [t for t in all_tasks if t.status == "review_ready"]
        print("\nTasks:")
        print(f"  {len(ready)} ready to claim")
        print(f"  {len(running)} in-flight (running)")
        if review_ready:
            print(f"  {len(review_ready)} awaiting review")

        # Agents
        agents = reg.discover()
        online = [a for a in agents if a.status == "online"]
        print("\nAgents:")
        print(f"  {len(online)} online" + (f" ({', '.join(a.agent_id for a in online)})" if online else ""))

        # Conflicts
        conflicts = reg.list_conflicts(resolved=False)
        if conflicts:
            print(f"\nConflicts ({len(conflicts)} unresolved):")
            for c in conflicts[:5]:
                desc = c.description[:60] + ("..." if len(c.description) > 60 else "")
                print(f"  {c.source}: {desc}")
        else:
            print("\nConflicts: none unresolved")

        # Metrics
        metrics = compute_metrics(reg.ledger)
        m = metrics
        print("\nMetrics:")
        print(f"  cycle_time   {m.get('task_cycle_time_seconds', 0):.2f}s  |  "
              f"handoff_rate  {m.get('handoff_success_rate', 0):.0%}  |  "
              f"quality_rate  {m.get('quality_gate_pass_rate', 0):.0%}")
        print(f"  retry_rate   {m.get('retry_rate', 0):.0%}  |  "
              f"conflict_rate  {m.get('conflict_rate', 0):.0%}  |  "
              f"active_agents  {m.get('active_agents', 0)}")

        return 0

    if args.command == "next":
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        registry = Registry(SQLiteStorage(Path(args.db)))
        claimed = registry.claim_next_task(
            agent_id=args.agent_id,
            capability=args.capability,
            best_effort=args.best_effort,
        )
        if claimed is None:
            logger.warning("No claimable tasks found.")
            return 1
        started = registry.start_task(claimed.task_id, args.agent_id)
        packet = registry.prepare_worker_packet(claimed.task_id, agent_id=args.agent_id)
        header = json.dumps({"task_id": started.task_id, "status": started.status})
        print(f"---MAC-TASK: {header}---")
        print(packet, end="")
        return 0

    if args.command == "observe":
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        Registry(SQLiteStorage(Path(args.db))).record_task_outcome(
            agent_id=args.agent_id,
            capability=args.capability,
            task_type=args.task_type,
            status=args.status,
            duration_seconds=args.duration,
            error_code=args.error_code,
        )
        _print_json({"agent_id": args.agent_id, "capability": args.capability, "status": "recorded"})
        return 0

    if args.command == "capability-score":
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        score = Registry(SQLiteStorage(Path(args.db))).get_capability_score(args.agent_id, args.capability)
        _print_json(score)
        return 0

    if args.command == "scoring":
        if args.scoring_command == "list":
            return _cmd_scoring_list()
        if args.scoring_command == "test":
            return _cmd_scoring_test(args)
        raise AssertionError(f"Unhandled scoring subcommand: {args.scoring_command}")

    if args.command == "claim":
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        task = Registry(SQLiteStorage(Path(args.db))).claim_next_task(
            agent_id=args.agent_id,
            capability=args.capability,
            project_context=args.project_context,
            best_effort=args.best_effort,
        )
        _print_json(task.model_dump(mode="json") if task is not None else None)
        return 0

    if args.command == "run-once":
        from mac.registry import Registry
        from mac.runner import command_agent_template
        from mac.storage.sqlite import SQLiteStorage

        command = [part for part in args.run_command if part]
        if not command:
            raise SystemExit("--command requires at least one executable argument")
        registry = Registry(SQLiteStorage(Path(args.db)))
        template = command_agent_template(
            agent_id=args.agent_id,
            name=args.name,
            capability=args.capability,
            command=command,
            timeout_seconds=args.timeout,
            project_context=args.project_context,
        )
        runner = template.create_runner(registry=registry)
        task = runner.run_once()
        _print_json(task.model_dump(mode="json") if task is not None else None)
        return 0

    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    import sys

    if sys.platform == "win32":
        try:
            import threading

            threading.stack_size(8 * 1024 * 1024)
        except (ValueError, threading.ThreadError):
            pass

    raise SystemExit(main())
