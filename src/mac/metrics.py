"""MAC trace metrics: aggregate collaboration ledger into observable indicators.

Six indicators, all derived from existing SQLite tables (no new schema):
- task_cycle_time_seconds: avg submit_task audit → task_transfers updated_at (status=completed)
- handoff_success_rate: handoff_results.payload.boundary_review=='pass' / total
- quality_gate_pass_rate: quality_results.payload.status=='passed' / total
- retry_rate: task_transfers.payload.retry_count > 0 / total
- conflict_rate: conflict_records count / task_transfers count
- active_agents: agent_cards.status='online' count

Pure read-only; safe to call repeatedly. SQL stays portable (no json_extract,
which requires SQLite 3.38+; this project supports Python 3.10+ whose stdlib
sqlite3 ships SQLite 3.37). Payload JSON is deserialized in Python and
aggregated there.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from mac.storage.sqlite import SQLiteTaskLedger


_METRIC_VERSION = "1.0"


def compute_metrics(ledger: SQLiteTaskLedger) -> dict[str, Any]:
    """Aggregate collaboration metrics from the existing ledger."""
    with ledger._connect() as conn:
        completed_durations = _collect_completed_durations(conn)
        handoff_pass, handoff_total = _collect_handoff_outcomes(conn)
        quality_pass, quality_total = _collect_quality_outcomes(conn)
        retried, tasks_total = _collect_retry_counts(conn)
        conflict_total = _collect_conflict_total(conn)
        active_agents = _collect_active_agents(conn)

    avg_cycle = (
        sum(completed_durations) / len(completed_durations)
        if completed_durations
        else 0.0
    )

    return {
        "metric_version": _METRIC_VERSION,
        "task_cycle_time_seconds": round(avg_cycle, 4),
        "handoff_success_rate": round(handoff_pass / handoff_total, 4) if handoff_total else 0.0,
        "quality_gate_pass_rate": round(quality_pass / quality_total, 4) if quality_total else 0.0,
        "retry_rate": round(retried / tasks_total, 4) if tasks_total else 0.0,
        "conflict_rate": round(conflict_total / tasks_total, 4) if tasks_total else 0.0,
        "active_agents": int(active_agents),
        "samples": {
            "completed_tasks": len(completed_durations),
            "handoffs": handoff_total,
            "quality_results": quality_total,
            "task_transfers": tasks_total,
            "conflicts": conflict_total,
        },
    }


def format_table(metrics: dict[str, Any]) -> str:
    """Format metrics as a human-friendly terminal table."""
    samples = metrics["samples"]
    lines = [
        f"MAC trace metrics (v{metrics['metric_version']})",
        "",
        f"  {'Metric':<28} {'Value':>12}",
        f"  {'-' * 28} {'-' * 12}",
        f"  {'task_cycle_time_seconds':<28} {metrics['task_cycle_time_seconds']:>12.4f}",
        f"  {'handoff_success_rate':<28} {metrics['handoff_success_rate']:>12.4f}",
        f"  {'quality_gate_pass_rate':<28} {metrics['quality_gate_pass_rate']:>12.4f}",
        f"  {'retry_rate':<28} {metrics['retry_rate']:>12.4f}",
        f"  {'conflict_rate':<28} {metrics['conflict_rate']:>12.4f}",
        f"  {'active_agents':<28} {metrics['active_agents']:>12d}",
        "",
        "  Samples:",
        f"    completed_tasks:    {samples['completed_tasks']}",
        f"    handoffs:           {samples['handoffs']}",
        f"    quality_results:    {samples['quality_results']}",
        f"    task_transfers:     {samples['task_transfers']}",
        f"    conflicts:          {samples['conflicts']}",
    ]
    return "\n".join(lines)


def _collect_completed_durations(conn: Any) -> list[float]:
    """Seconds between first submit_task audit and task_transfers.updated_at (status=completed).

    action lives in audit_entries.payload (JSON). Aggregate in Python to stay
    portable across Python 3.10+ / stdlib sqlite3 (SQLite 3.37 lacks json_extract).
    """
    audit_rows = conn.execute(
        "SELECT task_id, created_at, payload FROM audit_entries"
    ).fetchall()
    first_submit: dict[str, str] = {}
    for row in audit_rows:
        payload = json.loads(row["payload"])
        if payload.get("action") != "submit_task":
            continue
        task_id = row["task_id"]
        created = row["created_at"]
        if task_id not in first_submit or created < first_submit[task_id]:
            first_submit[task_id] = created

    completed_rows = conn.execute(
        "SELECT task_id, updated_at FROM task_transfers WHERE status = 'completed'"
    ).fetchall()
    durations: list[float] = []
    for row in completed_rows:
        if row["task_id"] not in first_submit:
            continue
        seconds = _iso_seconds_delta(first_submit[row["task_id"]], row["updated_at"])
        if seconds is not None and seconds >= 0:
            durations.append(seconds)
    return durations


def _collect_handoff_outcomes(conn: Any) -> tuple[int, int]:
    """Count handoff_results with boundary_review=='pass' vs total."""
    rows = conn.execute("SELECT payload FROM handoff_results").fetchall()
    total = len(rows)
    passed = 0
    for row in rows:
        payload = json.loads(row["payload"])
        if payload.get("boundary_review") == "pass":
            passed += 1
    return passed, total


def _collect_quality_outcomes(conn: Any) -> tuple[int, int]:
    """Count quality_results with status=='passed' vs total."""
    rows = conn.execute("SELECT payload FROM quality_results").fetchall()
    total = len(rows)
    passed = 0
    for row in rows:
        payload = json.loads(row["payload"])
        if payload.get("status") == "passed":
            passed += 1
    return passed, total


def _collect_retry_counts(conn: Any) -> tuple[int, int]:
    """Count task_transfers with retry_count > 0 vs total."""
    rows = conn.execute("SELECT payload FROM task_transfers").fetchall()
    total = len(rows)
    retried = 0
    for row in rows:
        payload = json.loads(row["payload"])
        if int(payload.get("retry_count", 0) or 0) > 0:
            retried += 1
    return retried, total


def _collect_conflict_total(conn: Any) -> int:
    """Total conflict_records rows."""
    row = conn.execute("SELECT COUNT(*) FROM conflict_records").fetchone()
    return int(row[0]) if row else 0


def _collect_active_agents(conn: Any) -> int:
    """agent_cards with status='online'."""
    row = conn.execute(
        "SELECT COUNT(*) FROM agent_cards WHERE status = 'online'"
    ).fetchone()
    return int(row[0]) if row else 0


def _iso_seconds_delta(start_iso: str, end_iso: str) -> float | None:
    """Compute seconds between two ISO-8601 timestamps. None on parse error."""
    try:
        start = datetime.fromisoformat(start_iso)
        end = datetime.fromisoformat(end_iso)
    except (TypeError, ValueError):
        return None
    return (end - start).total_seconds()