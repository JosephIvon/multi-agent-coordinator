from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from mac.protocol.messages import AuditEntry


class StatusConflict(RuntimeError):
    """Raised when a compare-and-swap task status update sees a stale status."""

    def __init__(self, task_id: str, expected: str, actual: str) -> None:
        super().__init__(
            f"Task {task_id!r} status is {actual!r}, expected {expected!r}."
        )
        self.task_id = task_id
        self.expected = expected
        self.actual = actual


class SQLiteTaskLedger:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def save_agent_card(self, agent: Any) -> None:
        data = _to_dict(agent)
        agent_id = data["agent_id"]
        capabilities = _capability_names(data.get("capabilities", []))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_cards (
                    agent_id, status, load, project_context, capabilities, payload, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    status = excluded.status,
                    load = excluded.load,
                    project_context = excluded.project_context,
                    capabilities = excluded.capabilities,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    agent_id,
                    data.get("status"),
                    int(data.get("load", 0) or 0),
                    data.get("project_context"),
                    json.dumps(capabilities, sort_keys=True),
                    _json(data),
                    _now(),
                ),
            )

    def get_agent_card(self, agent_id: str) -> Any | None:
        row = self._fetch_one("SELECT payload FROM agent_cards WHERE agent_id = ?", agent_id)
        if row is None:
            return None
        return _from_dict("AgentCard", json.loads(row["payload"]))

    def list_agent_cards(
        self,
        *,
        capability: str | None = None,
        status: str | None = None,
        max_load: int | None = None,
        project_context: str | None = None,
    ) -> list[Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if max_load is not None:
            clauses.append("load <= ?")
            params.append(max_load)
        if project_context is not None:
            clauses.append("project_context = ?")
            params.append(project_context)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._fetch_all(
            f"SELECT payload, capabilities FROM agent_cards {where} ORDER BY load ASC, agent_id ASC",
            *params,
        )
        agents = []
        for row in rows:
            capability_names = json.loads(row["capabilities"])
            if capability is not None and capability not in capability_names:
                continue
            agents.append(_from_dict("AgentCard", json.loads(row["payload"])))
        return agents

    def save_task_transfer(self, task: Any) -> None:
        data = _to_dict(task)
        task_id = data["task_id"]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO task_transfers (
                    task_id, status, project_context, payload, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    status = excluded.status,
                    project_context = excluded.project_context,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    task_id,
                    data.get("status"),
                    data.get("project_context"),
                    _json(data),
                    _now(),
                ),
            )

    def get_task_transfer(self, task_id: str) -> Any | None:
        row = self._fetch_one("SELECT payload FROM task_transfers WHERE task_id = ?", task_id)
        if row is None:
            return None
        return _from_dict("TaskTransfer", json.loads(row["payload"]))

    def list_task_transfers(
        self,
        *,
        status: str | None = None,
        project_context: str | None = None,
    ) -> list[Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if project_context is not None:
            clauses.append("project_context = ?")
            params.append(project_context)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._fetch_all(
            f"SELECT payload FROM task_transfers {where} ORDER BY updated_at ASC, task_id ASC",
            *params,
        )
        return [_from_dict("TaskTransfer", json.loads(row["payload"])) for row in rows]

    def delete_task_transfer(self, task_id: str) -> bool:
        """Delete a task transfer row. Returns True if a row was deleted."""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM task_transfers WHERE task_id = ?",
                (task_id,),
            )
            return cursor.rowcount > 0

    def save_plan(self, plan: Any) -> None:
        data = _to_dict(plan)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO plans (
                    plan_id, status, created_at, payload, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(plan_id) DO UPDATE SET
                    status = excluded.status,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    data["plan_id"],
                    data.get("status"),
                    data.get("created_at") or _now(),
                    _json(data),
                    _now(),
                ),
            )

    def get_plan(self, plan_id: str) -> Any | None:
        row = self._fetch_one("SELECT payload FROM plans WHERE plan_id = ?", plan_id)
        if row is None:
            return None
        return _from_dict("Plan", json.loads(row["payload"]))

    def list_plans(self, *, status: str | None = None) -> list[Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._fetch_all(
            f"SELECT payload FROM plans {where} ORDER BY created_at ASC, plan_id ASC",
            *params,
        )
        return [_from_dict("Plan", json.loads(row["payload"])) for row in rows]

    def save_handoff_result(self, handoff: Any) -> None:
        data = _to_dict(handoff)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO handoff_results (
                    task_id, plan_id, agent_id, payload, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    plan_id = excluded.plan_id,
                    agent_id = excluded.agent_id,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    data["task_id"],
                    data.get("plan_id"),
                    data.get("agent_id"),
                    _json(data),
                    _now(),
                ),
            )

    def get_handoff_result(self, task_id: str) -> Any | None:
        row = self._fetch_one("SELECT payload FROM handoff_results WHERE task_id = ?", task_id)
        if row is None:
            return None
        return _from_dict("HandoffResult", json.loads(row["payload"]))

    def record_conflict(self, conflict: Any) -> Any:
        data = _to_dict(conflict)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO conflict_records (
                    conflict_id, plan_id, task_id, source, severity, resolved, payload, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conflict_id) DO UPDATE SET
                    plan_id = excluded.plan_id,
                    task_id = excluded.task_id,
                    source = excluded.source,
                    severity = excluded.severity,
                    resolved = excluded.resolved,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    data["conflict_id"],
                    data.get("plan_id"),
                    data.get("task_id"),
                    data.get("source"),
                    data.get("severity"),
                    1 if data.get("resolved") else 0,
                    _json(data),
                    _now(),
                ),
            )
        return _from_dict("ConflictRecord", data)

    def get_conflict(self, conflict_id: str) -> Any | None:
        row = self._fetch_one("SELECT payload FROM conflict_records WHERE conflict_id = ?", conflict_id)
        if row is None:
            return None
        return _from_dict("ConflictRecord", json.loads(row["payload"]))

    def list_conflicts(
        self,
        *,
        plan_id: str | None = None,
        resolved: bool | None = None,
    ) -> list[Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if plan_id is not None:
            clauses.append("plan_id = ?")
            params.append(plan_id)
        if resolved is not None:
            clauses.append("resolved = ?")
            params.append(1 if resolved else 0)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._fetch_all(
            f"SELECT payload FROM conflict_records {where} ORDER BY updated_at ASC, conflict_id ASC",
            *params,
        )
        return [_from_dict("ConflictRecord", json.loads(row["payload"])) for row in rows]

    def resolve_conflict(self, conflict_id: str, resolution: str) -> Any:
        conflict = self.get_conflict(conflict_id)
        if conflict is None:
            raise KeyError(conflict_id)
        data = _to_dict(conflict)
        data["resolved"] = True
        data["resolution"] = resolution
        data["resolved_at"] = _now()
        return self.record_conflict(_from_dict("ConflictRecord", data))

    def update_task_status(
        self,
        task_id: str,
        status: str,
        *,
        expected_status: str | None = None,
        actor: str = "system",
        details: dict[str, Any] | None = None,
    ) -> Any:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status, payload FROM task_transfers WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(task_id)

            actual_status = row["status"]
            if expected_status is not None and actual_status != expected_status:
                raise StatusConflict(task_id, expected_status, actual_status)

            data = json.loads(row["payload"])
            data["status"] = status
            updated_at = _now()
            if expected_status is None:
                cursor = conn.execute(
                    """
                    UPDATE task_transfers
                    SET status = ?, payload = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (status, _json(data), updated_at, task_id),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE task_transfers
                    SET status = ?, payload = ?, updated_at = ?
                    WHERE task_id = ? AND status = ?
                    """,
                    (status, _json(data), updated_at, task_id, expected_status),
                )
            if cursor.rowcount != 1:
                actual = conn.execute("SELECT status FROM task_transfers WHERE task_id = ?", (task_id,)).fetchone()
                actual_status = actual["status"] if actual is not None else "<missing>"
                raise StatusConflict(task_id, expected_status or actual_status, actual_status)
            audit = AuditEntry(
                entry_id=str(uuid4()),
                task_id=task_id,
                actor=actor,
                action="task_status_updated",
                details={
                    "from_status": actual_status,
                    "to_status": status,
                    **(details or {}),
                },
                created_at=_now(),
            )
            self._insert_audit(conn, _to_dict(audit))
            return _from_dict("TaskTransfer", data)

    def record_audit_entry(self, entry: Any) -> None:
        with self._connect() as conn:
            self._insert_audit(conn, _to_dict(entry))

    def list_audit_entries(self, task_id: str) -> list[Any]:
        rows = self._fetch_all(
            "SELECT payload FROM audit_entries WHERE task_id = ? ORDER BY created_at ASC, entry_id ASC",
            task_id,
        )
        return [_from_dict("AuditEntry", json.loads(row["payload"])) for row in rows]

    def _insert_audit(self, conn: sqlite3.Connection, data: dict[str, Any]) -> None:
        if not data.get("created_at"):
            data["created_at"] = _now()
        trace_id = str(data.get("trace_id") or "")
        conn.execute(
            """
            INSERT INTO audit_entries (entry_id, task_id, created_at, trace_id, payload)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                data["entry_id"],
                data["task_id"],
                data["created_at"],
                trace_id,
                _json(data),
            ),
        )

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_cards (
                    agent_id TEXT PRIMARY KEY,
                    status TEXT,
                    load INTEGER NOT NULL DEFAULT 0,
                    project_context TEXT,
                    capabilities TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_transfers (
                    task_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    project_context TEXT,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_entries (
                    entry_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    trace_id TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL
                )
                """
            )
            # Migration: pre-existing databases lack the trace_id column.
            existing_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(audit_entries)").fetchall()
            }
            if "trace_id" not in existing_columns:
                conn.execute(
                    "ALTER TABLE audit_entries ADD COLUMN trace_id TEXT NOT NULL DEFAULT ''"
                )
                # Backfill trace_id from payload JSON for rows written before the
                # migration. Done in Python (json.loads) rather than SQL because
                # stdlib sqlite3 on Python 3.10 ships SQLite 3.37, which lacks
                # json_extract. Bound to O(n) rows once at startup; cost is
                # negligible compared to the subsequent indexed lookup.
                pending = conn.execute(
                    "SELECT entry_id, payload FROM audit_entries WHERE trace_id = ''"
                ).fetchall()
                for row in pending:
                    try:
                        payload_obj = json.loads(row["payload"])
                    except (TypeError, ValueError):
                        continue
                    recovered = str(payload_obj.get("trace_id") or "")
                    if recovered:
                        conn.execute(
                            "UPDATE audit_entries SET trace_id = ? WHERE entry_id = ?",
                            (recovered, row["entry_id"]),
                        )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS quality_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    duration_seconds REAL NOT NULL,
                    error_code TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS plans (
                    plan_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS handoff_results (
                    task_id TEXT PRIMARY KEY,
                    plan_id TEXT,
                    agent_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conflict_records (
                    conflict_id TEXT PRIMARY KEY,
                    plan_id TEXT,
                    task_id TEXT,
                    source TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    resolved INTEGER NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_discovery ON agent_cards(status, project_context, load)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_task ON audit_entries(task_id, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_trace ON audit_entries(trace_id, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_quality_task ON quality_results(task_id, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_outcomes ON agent_outcomes(agent_id, capability, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_plans_status ON plans(status, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_handoff_plan ON handoff_results(plan_id, updated_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conflicts_plan ON conflict_records(plan_id, resolved, updated_at)"
            )

    def record_quality_result(self, task_id: str, result: dict[str, Any]) -> None:
        data = dict(result)
        data.setdefault("task_id", task_id)
        data.setdefault("created_at", _now())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO quality_results (task_id, created_at, payload)
                VALUES (?, ?, ?)
                """,
                (task_id, data["created_at"], _json(data)),
            )

    def get_quality_results(self, task_id: str) -> list[dict[str, Any]]:
        rows = self._fetch_all(
            "SELECT payload FROM quality_results WHERE task_id = ? ORDER BY created_at ASC, id ASC",
            task_id,
        )
        return [json.loads(row["payload"]) for row in rows]

    def get_audit_trail(self, trace_id: str) -> list[Any]:
        rows = self._fetch_all(
            "SELECT payload FROM audit_entries WHERE trace_id = ? "
            "ORDER BY created_at ASC, entry_id ASC",
            trace_id,
        )
        return [_from_dict("AuditEntry", json.loads(row["payload"])) for row in rows]

    def record_task_outcome(
        self,
        *,
        agent_id: str,
        capability: str,
        task_type: str,
        status: str,
        duration_seconds: float,
        error_code: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_outcomes (
                    agent_id, capability, task_type, status, duration_seconds, error_code, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    capability,
                    task_type,
                    status,
                    float(duration_seconds),
                    error_code,
                    _now(),
                ),
            )

    def get_capability_score(self, agent_id: str, capability: str) -> dict[str, Any]:
        rows = self._fetch_all(
            """
            SELECT status, duration_seconds, error_code
            FROM agent_outcomes
            WHERE agent_id = ? AND capability = ?
            ORDER BY created_at ASC, id ASC
            """,
            agent_id,
            capability,
        )
        total = len(rows)
        succeeded = sum(1 for row in rows if row["status"] == "succeeded")
        failed = sum(1 for row in rows if row["status"] == "failed")
        average_duration = (
            sum(float(row["duration_seconds"]) for row in rows) / total if total else 0.0
        )
        last_error_code = None
        for row in reversed(rows):
            if row["error_code"]:
                last_error_code = row["error_code"]
                break

        return {
            "agent_id": agent_id,
            "capability": capability,
            "total": total,
            "succeeded": succeeded,
            "failed": failed,
            "success_rate": round(succeeded / total, 4) if total else 0.0,
            "average_duration_seconds": round(average_duration, 4),
            "last_error_code": last_error_code,
        }

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _fetch_one(self, sql: str, *params: Any) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(sql, params).fetchone()

    def _fetch_all(self, sql: str, *params: Any) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return list(conn.execute(sql, params).fetchall())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def _to_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return dict(value)
    raise TypeError(f"Cannot serialize {type(value)!r}")


def _from_dict(model_name: str, data: dict[str, Any]) -> Any:
    model = _message_model(model_name)
    if hasattr(model, "model_validate"):
        return model.model_validate(data)
    return model(**data)


def _message_model(model_name: str) -> type[Any]:
    from mac.protocol import messages

    if hasattr(messages, model_name):
        return getattr(messages, model_name)
    raise AttributeError(f"Model {model_name!r} not found in mac.protocol.messages")


def _capability_names(capabilities: list[Any]) -> list[str]:
    names: list[str] = []
    for capability in capabilities:
        if isinstance(capability, str):
            names.append(capability)
        elif isinstance(capability, dict):
            names.append(str(capability.get("name") or capability.get("capability")))
        else:
            names.append(str(getattr(capability, "name", getattr(capability, "capability", ""))))
    return sorted(name for name in names if name)


SQLiteStorage = SQLiteTaskLedger
