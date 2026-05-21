from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mac-agent")
    subcommands = parser.add_subparsers(dest="command", required=True)

    contract = subcommands.add_parser("contract", help="Generate a risk-based test contract")
    contract.add_argument("--risk", choices=["low", "medium", "high"], required=True)

    register = subcommands.add_parser("register", help="Register an agent in the local ledger")
    register.add_argument("--db", default="mac.db")
    register.add_argument("--agent-id", required=True)
    register.add_argument("--name", required=True)
    register.add_argument("--capability", action="append", required=True)
    register.add_argument("--project-context")
    register.add_argument("--load", type=int, default=0)

    discover = subcommands.add_parser("discover", help="Discover agents by capability")
    discover.add_argument("--db", default="mac.db")
    discover.add_argument("--capability", required=True)
    discover.add_argument("--project-context")

    submit = subcommands.add_parser("submit", help="Submit a task to the local ledger")
    submit.add_argument("--db", default="mac.db")
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

    status = subcommands.add_parser("status", help="Print task status")
    status.add_argument("--db", default="mac.db")
    status.add_argument("--task-id", required=True)

    tasks = subcommands.add_parser("tasks", help="List tasks from the local ledger")
    tasks.add_argument("--db", default="mac.db")
    tasks.add_argument("--status")
    tasks.add_argument("--capability")
    tasks.add_argument("--agent-id")
    tasks.add_argument("--project-context")

    task_evidence = subcommands.add_parser("task-evidence", help="Print a task evidence bundle")
    task_evidence.add_argument("--db", default="mac.db")
    task_evidence.add_argument("--task-id", required=True)

    quality_preview = subcommands.add_parser("quality-preview", help="Preview whether task quality evidence satisfies its contract")
    quality_preview.add_argument("--db", default="mac.db")
    quality_preview.add_argument("--task-id", required=True)

    task_readiness = subcommands.add_parser("task-readiness", help="Preview a task's recommended next action")
    task_readiness.add_argument("--db", default="mac.db")
    task_readiness.add_argument("--task-id", required=True)

    accept = subcommands.add_parser("accept", help="Accept a task handoff")
    accept.add_argument("--db", default="mac.db")
    accept.add_argument("--task-id", required=True)
    accept.add_argument("--agent-id", required=True)

    start = subcommands.add_parser("start", help="Mark a task running")
    start.add_argument("--db", default="mac.db")
    start.add_argument("--task-id", required=True)
    start.add_argument("--agent-id", required=True)

    quality = subcommands.add_parser("quality", help="Record quality evidence")
    quality.add_argument("--db", default="mac.db")
    quality.add_argument("--task-id", required=True)
    quality.add_argument("--command", dest="quality_command", required=True)
    quality.add_argument("--status", choices=["passed", "failed"], required=True)
    quality.add_argument("--evidence", action="append", default=[])

    complete = subcommands.add_parser("complete", help="Complete a task after quality gate")
    complete.add_argument("--db", default="mac.db")
    complete.add_argument("--task-id", required=True)
    complete.add_argument("--agent-id", required=True)

    fail = subcommands.add_parser("fail", help="Mark a task failed")
    fail.add_argument("--db", default="mac.db")
    fail.add_argument("--task-id", required=True)
    fail.add_argument("--agent-id", required=True)
    fail.add_argument("--error-code", required=True)
    fail.add_argument("--message", default="")

    checkpoint = subcommands.add_parser("checkpoint", help="Record a task recovery checkpoint")
    checkpoint.add_argument("--db", default="mac.db")
    checkpoint.add_argument("--task-id", required=True)
    checkpoint.add_argument("--agent-id", required=True)
    checkpoint.add_argument("--summary", required=True)
    checkpoint.add_argument("--artifact-ref", action="append", default=[])

    retry = subcommands.add_parser("retry", help="Retry a failed task")
    retry.add_argument("--db", default="mac.db")
    retry.add_argument("--task-id", required=True)
    retry.add_argument("--agent-id", required=True)
    retry.add_argument("--fallback-agent-id")

    cancel = subcommands.add_parser("cancel", help="Cancel a task")
    cancel.add_argument("--db", default="mac.db")
    cancel.add_argument("--task-id", required=True)
    cancel.add_argument("--agent-id", required=True)
    cancel.add_argument("--reason", default="")

    audit = subcommands.add_parser("audit", help="Print audit trail by trace id")
    audit.add_argument("--db", default="mac.db")
    audit.add_argument("--trace-id", required=True)

    observe = subcommands.add_parser("observe", help="Record an observed agent outcome")
    observe.add_argument("--db", default="mac.db")
    observe.add_argument("--agent-id", required=True)
    observe.add_argument("--capability", required=True)
    observe.add_argument("--task-type", required=True)
    observe.add_argument("--status", choices=["succeeded", "failed"], required=True)
    observe.add_argument("--duration", type=float, required=True)
    observe.add_argument("--error-code")

    score = subcommands.add_parser("capability-score", help="Print observed capability score")
    score.add_argument("--db", default="mac.db")
    score.add_argument("--agent-id", required=True)
    score.add_argument("--capability", required=True)

    claim = subcommands.add_parser("claim", help="Claim the next proposed task by capability")
    claim.add_argument("--db", default="mac.db")
    claim.add_argument("--agent-id", required=True)
    claim.add_argument("--capability", required=True)
    claim.add_argument("--project-context")
    claim.add_argument("--best-effort", action="store_true")

    run_once = subcommands.add_parser("run-once", help="Run one local agent adapter cycle")
    run_once.add_argument("--db", default="mac.db")
    run_once.add_argument("--agent-id", required=True)
    run_once.add_argument("--name", required=True)
    run_once.add_argument("--capability", required=True)
    run_once.add_argument("--project-context")
    run_once.add_argument("--timeout", type=float, default=60)
    run_once.add_argument("--command", dest="run_command", nargs=argparse.REMAINDER, required=True)

    return parser


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.command == "contract":
        from mac.testing.contracts import TestContract

        _print_json(TestContract.for_risk(args.risk).model_dump())
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
        task = TaskTransfer(
            task_id=args.task_id,
            trace_id=args.trace_id or args.task_id,
            source_agent_id=args.source_agent_id,
            target_agent_id=args.target_agent_id,
            payload=payload,
            context=ContextBundle(summary=args.summary, artifact_refs=args.context_ref),
            test_contract=TestContract.for_risk(args.risk) if args.risk else None,
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

    if args.command == "audit":
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        entries = Registry(SQLiteStorage(Path(args.db))).get_audit_trail(args.trace_id)
        _print_json([entry.model_dump(mode="json") for entry in entries])
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
    raise SystemExit(main())
