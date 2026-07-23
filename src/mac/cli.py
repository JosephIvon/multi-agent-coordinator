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
    register.add_argument("--allowed-path", action="append", default=[])
    register.add_argument("--forbidden-path", action="append", default=[])

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
    submit.add_argument("--plan-id")
    submit.add_argument("--depends-on", action="append", default=[])

    status = subcommands.add_parser("status", help="Print task status")
    status.add_argument("--db", default="mac.db")
    status.add_argument("--task-id", required=True)

    tasks = subcommands.add_parser("tasks", help="List tasks from the local ledger")
    tasks.add_argument("--db", default="mac.db")
    tasks.add_argument("--status")
    tasks.add_argument("--capability")
    tasks.add_argument("--agent-id")
    tasks.add_argument("--project-context")

    plan = subcommands.add_parser("plan", help="Manage collaboration plans")
    plan_subcommands = plan.add_subparsers(dest="plan_command", required=True)
    plan_create = plan_subcommands.add_parser("create", help="Create a collaboration plan")
    plan_create.add_argument("--db", default="mac.db")
    plan_create.add_argument("--plan-id")
    plan_create.add_argument("--goal", required=True)
    plan_create.add_argument("--created-by", default="")
    plan_activate = plan_subcommands.add_parser("activate", help="Activate a collaboration plan")
    plan_activate.add_argument("--db", default="mac.db")
    plan_activate.add_argument("--plan-id", required=True)
    plan_close = plan_subcommands.add_parser("close", help="Close a collaboration plan")
    plan_close.add_argument("--db", default="mac.db")
    plan_close.add_argument("--plan-id", required=True)
    plan_close.add_argument("--status", choices=["completed", "cancelled"], default="completed")
    plan_list = plan_subcommands.add_parser("list", help="List collaboration plans")
    plan_list.add_argument("--db", default="mac.db")
    plan_list.add_argument("--status")

    ready_tasks = subcommands.add_parser("ready-tasks", help="List dependency-unblocked proposed tasks")
    ready_tasks.add_argument("--db", default="mac.db")
    ready_tasks.add_argument("--agent-id")
    ready_tasks.add_argument("--capability")
    ready_tasks.add_argument("--project-context")

    metrics = subcommands.add_parser("metrics", help="Show collaboration trace metrics")
    metrics.add_argument("--db", default="mac.db")
    metrics.add_argument("--json", action="store_true", help="Emit metrics as JSON")

    handoff = subcommands.add_parser("handoff", help="Save or print a structured task handoff")
    handoff.add_argument("--db", default="mac.db")
    handoff.add_argument("--task-id", required=True)
    handoff.add_argument("--agent-id")
    handoff.add_argument("--plan-id")
    handoff.add_argument("--verification", action="append", default=[])
    handoff.add_argument("--changed-file", action="append", default=[])
    handoff.add_argument("--doc", action="append", default=[])
    handoff.add_argument("--risk", action="append", default=[])

    record_conflict = subcommands.add_parser("record-conflict", help="Record a collaboration conflict")
    record_conflict.add_argument("--db", default="mac.db")
    record_conflict.add_argument("--conflict-id")
    record_conflict.add_argument("--plan-id")
    record_conflict.add_argument("--task-id")
    record_conflict.add_argument("--source", required=True)
    record_conflict.add_argument("--severity", choices=["blocking", "non_blocking"], default="non_blocking")
    record_conflict.add_argument("--description", required=True)
    record_conflict.add_argument("--agent", action="append", default=[])
    record_conflict.add_argument("--file", action="append", default=[])

    conflicts = subcommands.add_parser("conflicts", help="List collaboration conflicts")
    conflicts.add_argument("--db", default="mac.db")
    conflicts.add_argument("--plan-id")
    conflicts.add_argument("--resolved", action="store_true")
    conflicts.add_argument("--unresolved", action="store_true")

    resolve_conflict = subcommands.add_parser("resolve-conflict", help="Resolve a collaboration conflict")
    resolve_conflict.add_argument("--db", default="mac.db")
    resolve_conflict.add_argument("--conflict-id", required=True)
    resolve_conflict.add_argument("--resolution", required=True)

    worker_packet = subcommands.add_parser("worker-packet", help="Print a worker task packet")
    worker_packet.add_argument("--db", default="mac.db")
    worker_packet.add_argument("--task-id", required=True)
    worker_packet.add_argument("--agent-id")

    review_packet = subcommands.add_parser("review-packet", help="Print a review task packet")
    review_packet.add_argument("--db", default="mac.db")
    review_packet.add_argument("--task-id", required=True)

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

    review_lifecycle = subcommands.add_parser(
        "review-lifecycle",
        help="Mark, accept, or reject a task review",
    )
    review_lifecycle.add_argument("--db", default="mac.db")
    review_lifecycle.add_argument("--action", choices=["mark-ready", "accept", "reject"], required=True)
    review_lifecycle.add_argument("--task-id", required=True)
    review_lifecycle.add_argument("--agent-id")
    review_lifecycle.add_argument("--reviewer-id")
    review_lifecycle.add_argument("--reason", default="")
    review_lifecycle.add_argument("--plan-id")

    cancel = subcommands.add_parser("cancel", help="Cancel a task")
    cancel.add_argument("--db", default="mac.db")
    cancel.add_argument("--task-id", required=True)
    cancel.add_argument("--agent-id", required=True)
    cancel.add_argument("--reason", default="")

    audit = subcommands.add_parser("audit", help="Print audit trail by trace id")
    audit.add_argument("--db", default="mac.db")
    audit.add_argument("--trace-id", required=True)

    expire = subcommands.add_parser("expire-stale", help="Expire tasks past their TTL")
    expire.add_argument("--db", default="mac.db")

    next_cmd = subcommands.add_parser(
        "next", help="Claim + start the next ready task and print its worker packet"
    )
    next_cmd.add_argument("--db", default="mac.db")
    next_cmd.add_argument("--agent-id", required=True)
    next_cmd.add_argument("--capability", required=True)
    next_cmd.add_argument("--best-effort", action="store_true")

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


def _parse_verification(value: str):
    from mac.protocol.messages import VerificationEntry

    command, result, description = (value.split(":", 2) + ["", ""])[:3]
    return VerificationEntry(command=command, result=result, description=description)


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
            plan_id=args.plan_id,
            depends_on=args.depends_on,
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
            "description": args.description,
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

        entries = Registry(SQLiteStorage(Path(args.db))).get_audit_trail(args.trace_id)
        _print_json([entry.model_dump(mode="json") for entry in entries])
        return 0

    if args.command == "expire-stale":
        from mac.registry import Registry
        from mac.storage.sqlite import SQLiteStorage

        expired = Registry(SQLiteStorage(Path(args.db))).expire_stale_tasks()
        if expired:
            for task in expired:
                print(f"Expired: {task.task_id} (TTL_EXPIRED)")
        else:
            print("No stale tasks found.")
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
            print("No claimable tasks found.", file=sys.stderr)
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
