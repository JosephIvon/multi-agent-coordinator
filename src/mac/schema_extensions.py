"""mac.schema_extensions -- thin facade over mac.extensions for downstream packages.

Provides the function-style surface that mac_coffee and other downstream
packages called for in docs/research/mac-agent.md and ADR-005:

* ``register_table(name, ddl)`` -- register a CREATE TABLE statement
  against the mac-agent SQLite file. Idempotent: re-registering the
  same name is a no-op (use ``replace=True`` to override).
* ``connection()`` -- open a fresh sqlite3 connection to the mac-agent
  SQLite file, apply ALL registered DDL (mac-agent's own plus every
  extension's), and return the connection.

Implementation notes
--------------------

* Each registered schema object gets an immutable entry in the
  thread-safe :mod:`mac.extensions` registry.
* The same extension registry is consulted by ``SQLiteTaskLedger._initialize``
  (sqlite.py), which now calls ``extensions.apply_ddl(conn)`` immediately
  after creating its own tables. That means a mac-agent process that
  boots before mac_coffee registers its tables will replay them on
  next ``_initialize`` via the ``CREATE TABLE IF NOT EXISTS`` semantics.
* This module is the function-style facade used by downstream packages.
"""
from __future__ import annotations

import os
import re
import sqlite3

from . import extensions as _ext
from .extensions import Extension

__all__ = [
    "register_table",
    "registered_tables",
    "reset",
    "connection",
]

_DEFAULT_DB_PATH = "mac.db"
_REGISTRY_PREFIX = "mac.schema_extensions."
_DDL_PATTERN = re.compile(
    r"\bCREATE\s+(?:TABLE|INDEX|VIEW|TRIGGER|VIRTUAL\s+TABLE)\b",
    re.IGNORECASE,
)


def register_table(name: str, ddl: str, *, replace: bool = False) -> None:
    """Register ``ddl`` (a CREATE TABLE / CREATE INDEX statement) under
    ``name`` against the mac-agent SQLite file.

    Parameters
    ----------
    name : str
        Stable identifier for the table (used as a key in mac-agent's
        ``Extension.table_ddl`` list). Conventionally the SQL table
        name itself, e.g. ``"mac_coffee_claim"``.
    ddl : str
        The DDL statement (typically ``CREATE TABLE IF NOT EXISTS ...``
        so re-runs are idempotent at the SQL layer).
    replace : bool, default False
        If a table named ``name`` is already registered, this method is
        a no-op unless ``replace=True``, in which case the prior
        statement is replaced.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a non-empty string")
    if not isinstance(ddl, str) or _DDL_PATTERN.search(ddl) is None:
        raise ValueError(
            "ddl must contain a supported CREATE statement "
            "(TABLE, INDEX, VIEW, TRIGGER, or VIRTUAL TABLE)"
        )
    normalized_name = name.strip()
    registry_key = f"{_REGISTRY_PREFIX}{normalized_name}"
    if _ext.get(registry_key) is not None and not replace:
        return
    _ext.register(
        Extension(
            name=registry_key,
            version="1",
            table_ddl=[
                f"-- schema extension: {normalized_name}\n{ddl.strip()}"
            ],
            public_symbols={"schema_table": normalized_name},
        ),
        strict=False,
    )


def registered_tables() -> list[str]:
    """Return the names of all tables registered so far."""
    return [
        str(extension.public_symbols["schema_table"])
        for extension in _ext.list_extensions()
        if extension.name.startswith(_REGISTRY_PREFIX)
        and "schema_table" in extension.public_symbols
    ]


def reset() -> None:
    """Drop all registered tables and unregister our singleton. Tests only."""
    for extension in _ext.list_extensions():
        if extension.name.startswith(_REGISTRY_PREFIX):
            _ext.unregister(extension.name)


def connection(db_path: str | None = None) -> sqlite3.Connection:
    """Open a new sqlite3 connection to the mac-agent SQLite file, apply
    every registered ``table_ddl``, and return the live connection.

    The caller owns the connection's lifecycle: close it when done.
    SQLite ``CREATE TABLE IF NOT EXISTS`` semantics keep this idempotent
    across successive calls.
    """
    path = db_path or os.environ.get("MAC_DB_PATH", _DEFAULT_DB_PATH)
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        _ext.apply_ddl(conn)
        conn.commit()
        return conn
    except Exception:
        conn.close()
        raise
