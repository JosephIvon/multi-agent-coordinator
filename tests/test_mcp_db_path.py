"""Tests for MAC_DB_PATH env var resolution in mcp_server and CLI."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from mac.mcp_server import _resolve_db_path


def test_resolve_db_path_env_var_set(monkeypatch):
    """_resolve_db_path should use MAC_DB_PATH env var when set."""
    monkeypatch.setenv("MAC_DB_PATH", "/custom/path/mac_custom.db")
    # Clear the module-level memo to force re-resolution
    import mac.mcp_server as mod

    mod._DB_PATH = None
    result = _resolve_db_path()
    assert result == Path("/custom/path/mac_custom.db").resolve()


def test_resolve_db_path_env_var_unset(monkeypatch):
    """_resolve_db_path should fall back to 'mac.db' when env var is absent."""
    monkeypatch.delenv("MAC_DB_PATH", raising=False)
    import mac.mcp_server as mod

    mod._DB_PATH = None
    result = _resolve_db_path()
    assert result == Path("mac.db").resolve()


def test_resolve_db_path_absolute_path(monkeypatch):
    """_resolve_db_path should handle absolute paths correctly."""
    monkeypatch.setenv("MAC_DB_PATH", r"C:\tmp\collab-smoke\mac.db")
    import mac.mcp_server as mod

    mod._DB_PATH = None
    result = _resolve_db_path()
    assert result == Path(r"C:\tmp\collab-smoke\mac.db")  # .resolve() keeps absolute paths


def test_resolve_db_path_does_not_yield_none(monkeypatch):
    """_resolve_db_path must never return None regardless of env state."""
    monkeypatch.delenv("MAC_DB_PATH", raising=False)
    import mac.mcp_server as mod

    mod._DB_PATH = None
    result = _resolve_db_path()
    assert result is not None
    assert isinstance(result, Path)


def test_resolve_db_path_is_memoised(monkeypatch):
    """_resolve_db_path should return the same Path across repeated calls."""
    monkeypatch.setenv("MAC_DB_PATH", "/tmp/memo_test.db")
    import mac.mcp_server as mod

    mod._DB_PATH = None
    first = _resolve_db_path()
    second = _resolve_db_path()
    assert first == second
    assert first is second  # memoised — same object


def test_cli_db_arg_reads_env(monkeypatch):
    """_cli_db_arg should read MAC_DB_PATH from the environment."""
    from mac.cli import _cli_db_arg

    monkeypatch.setenv("MAC_DB_PATH", "/custom/cli-path.db")
    assert _cli_db_arg() == "/custom/cli-path.db"


def test_cli_db_arg_falls_back_to_default(monkeypatch):
    """_cli_db_arg should return 'mac.db' when MAC_DB_PATH is not set."""
    from mac.cli import _cli_db_arg

    monkeypatch.delenv("MAC_DB_PATH", raising=False)
    assert _cli_db_arg() == "mac.db"
