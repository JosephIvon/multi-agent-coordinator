"""Tests for the public mac.schema_extensions facade."""

from __future__ import annotations

import sqlite3

import pytest

from mac import extensions
from mac import schema_extensions as schema
from mac.storage import SQLiteTaskLedger


@pytest.fixture(autouse=True)
def _reset_extensions() -> None:
    extensions.reset()
    yield
    extensions.reset()


def test_register_table_is_idempotent_and_replaceable(tmp_path) -> None:
    schema.register_table(
        "example",
        "CREATE TABLE IF NOT EXISTS example (first_value INTEGER)",
    )
    schema.register_table(
        "example",
        "CREATE TABLE IF NOT EXISTS example (ignored_value TEXT)",
    )

    db_path = tmp_path / "mac.db"
    with schema.connection(str(db_path)) as connection:
        columns = [
            row["name"]
            for row in connection.execute("PRAGMA table_info(example)")
        ]
    assert columns == ["first_value"]
    assert schema.registered_tables() == ["example"]

    schema.register_table(
        "example",
        "CREATE TABLE IF NOT EXISTS example (replacement_value TEXT)",
        replace=True,
    )
    replacement_path = tmp_path / "replacement.db"
    with schema.connection(str(replacement_path)) as connection:
        columns = [
            row["name"]
            for row in connection.execute("PRAGMA table_info(example)")
        ]
    assert columns == ["replacement_value"]


def test_connection_uses_mac_db_path_and_applies_registered_ddl(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "configured.db"
    monkeypatch.setenv("MAC_DB_PATH", str(db_path))
    schema.register_table(
        "configured",
        "CREATE TABLE IF NOT EXISTS configured (id TEXT PRIMARY KEY)",
    )

    with schema.connection() as connection:
        assert connection.row_factory is sqlite3.Row
        connection.execute("INSERT INTO configured (id) VALUES ('row-1')")

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT id FROM configured"
        ).fetchone() == ("row-1",)


def test_sqlite_ledger_initialization_applies_extension_ddl(tmp_path) -> None:
    extensions.register(
        extensions.Extension(
            name="example",
            version="1.0.0",
            table_ddl=[
                "CREATE TABLE IF NOT EXISTS extension_owned "
                "(id TEXT PRIMARY KEY)"
            ],
        )
    )

    ledger = SQLiteTaskLedger(tmp_path / "mac.db")

    with sqlite3.connect(ledger.db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "agent_cards" in tables
    assert "extension_owned" in tables


def test_apply_ddl_does_not_commit_the_callers_transaction() -> None:
    extensions.register(
        extensions.Extension(
            name="example",
            version="1.0.0",
            table_ddl=[
                "CREATE TABLE IF NOT EXISTS extension_owned "
                "(id TEXT PRIMARY KEY)"
            ],
        )
    )
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE base (value TEXT)")
    connection.commit()
    connection.execute("INSERT INTO base (value) VALUES ('pending')")

    extensions.apply_ddl(connection)
    connection.rollback()

    assert connection.execute("SELECT value FROM base").fetchall() == []
    connection.close()


@pytest.mark.parametrize(
    ("name", "ddl"),
    [
        ("", "CREATE TABLE x (id TEXT)"),
        ("x", ""),
        ("x", "DROP TABLE x"),
    ],
)
def test_register_table_rejects_invalid_input(name: str, ddl: str) -> None:
    with pytest.raises(ValueError):
        schema.register_table(name, ddl)
