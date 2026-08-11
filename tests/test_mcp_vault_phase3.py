"""Tests for Phase 3 vault tool enhancements.

Covers:
- mac_search_vault with type and path_prefix filters
- mac_save_to_vault default path (00-inbox/) and status: draft
- mac_promote_to_knowledge tool
- mac_done EOD hint when 3+ tasks completed today
- session-context resource daily_notes field
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from mac.mcp_server import (
    mac_done,
    mac_promote_to_knowledge,
    mac_save_to_vault,
    mac_search_vault,
    session_context_resource,
)
from mac.protocol.messages import (
    AgentCapability,
    AgentCard,
    TaskPayload,
    TaskTransfer,
)
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _registry_with_db(tmp_path: Path) -> tuple[Registry, SQLiteTaskLedger]:
    ledger = SQLiteTaskLedger(tmp_path / "mac.db")
    return Registry(ledger), ledger


def _agent(agent_id: str = "agent-1", capability: str = "write_code") -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        name=agent_id,
        capabilities=[AgentCapability(name=capability)],
    )


def _task_dict(
    task_id: str = "task-1",
    *,
    capability: str = "write_code",
    source: str = "planner",
    **overrides: Any,
) -> dict:
    base = TaskTransfer(
        task_id=task_id,
        source_agent_id=source,
        payload=TaskPayload(type=capability, summary=f"{task_id} summary"),
    ).model_dump()
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _use_tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import mac.mcp_server as mod

    db_path = tmp_path / "mac.db"
    monkeypatch.setattr(mod, "_DB_PATH", db_path)

    _orig_registry = mod._registry

    def _patched_registry() -> Registry:
        return Registry(SQLiteTaskLedger(db_path))

    monkeypatch.setattr(mod, "_registry", _patched_registry)


# ---------------------------------------------------------------------------
# Mock helpers for Obsidian REST API
# ---------------------------------------------------------------------------


def _mock_urlopen(response_data: dict | list | None = None, status: int = 200):
    """Create a mock for urllib.request.urlopen that returns JSON data."""
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.read.return_value = json.dumps(response_data or {}).encode("utf-8")
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _get_url(mock_urlopen) -> str:
    """Extract the URL from the most recent urlopen call.

    urlopen is called with a Request object, so we read ``full_url``.
    """
    call_args = mock_urlopen.call_args
    if not call_args:
        return ""
    arg = call_args[0][0] if call_args[0] else call_args[1].get("url", "")
    return getattr(arg, "full_url", str(arg))


# ---------------------------------------------------------------------------
# Test: mac_search_vault with type and path_prefix
# ---------------------------------------------------------------------------


class TestMacSearchVaultFilters:
    """Test mac_search_vault type and path_prefix parameters."""

    @patch("urllib.request.urlopen")
    @patch("mac.mcp_server._obsidian_headers", return_value={"Authorization": "Bearer test"})
    def test_basic_search_no_filters(self, _headers, mock_urlopen) -> None:
        mock_urlopen.return_value = _mock_urlopen({"results": []})
        mac_search_vault("test query")
        # Verify the URL was called with just the query
        url = _get_url(mock_urlopen)
        # URL should contain just the query, no tag: or path: prefix
        assert "test%20query" in url or "test+query" in url

    @patch("urllib.request.urlopen")
    @patch("mac.mcp_server._obsidian_headers", return_value={"Authorization": "Bearer test"})
    def test_type_filter_decision(self, _headers, mock_urlopen) -> None:
        mock_urlopen.return_value = _mock_urlopen({"results": []})
        mac_search_vault("api design", type="decision")
        # Verify URL contains tag:decision
        url = _get_url(mock_urlopen)
        assert "tag%3Adecision" in url or "tag:decision" in url

    @patch("urllib.request.urlopen")
    @patch("mac.mcp_server._obsidian_headers", return_value={"Authorization": "Bearer test"})
    def test_type_filter_pitfall(self, _headers, mock_urlopen) -> None:
        mock_urlopen.return_value = _mock_urlopen({"results": []})
        mac_search_vault("encoding", type="pitfall")
        url = _get_url(mock_urlopen)
        assert "tag%3Apitfall" in url or "tag:pitfall" in url

    @patch("urllib.request.urlopen")
    @patch("mac.mcp_server._obsidian_headers", return_value={"Authorization": "Bearer test"})
    def test_path_prefix_filter(self, _headers, mock_urlopen) -> None:
        mock_urlopen.return_value = _mock_urlopen({"results": []})
        mac_search_vault("test", path_prefix="10-projects/")
        url = _get_url(mock_urlopen)
        assert "path%3A10-projects" in url or "path:10-projects" in url

    @patch("urllib.request.urlopen")
    @patch("mac.mcp_server._obsidian_headers", return_value={"Authorization": "Bearer test"})
    def test_combined_type_and_path(self, _headers, mock_urlopen) -> None:
        mock_urlopen.return_value = _mock_urlopen({"results": []})
        mac_search_vault("api", type="decision", path_prefix="10-projects/")
        url = _get_url(mock_urlopen)
        assert "tag%3Adecision" in url or "tag:decision" in url
        assert "path%3A10-projects" in url or "path:10-projects" in url

    @patch("urllib.request.urlopen")
    @patch("mac.mcp_server._obsidian_headers", return_value={"Authorization": "Bearer test"})
    def test_unknown_type_is_ignored(self, _headers, mock_urlopen) -> None:
        mock_urlopen.return_value = _mock_urlopen({"results": []})
        mac_search_vault("test", type="unknown_type")
        url = _get_url(mock_urlopen)
        # Should NOT contain tag:unknown_type
        assert "tag%3Aunknown_type" not in url and "tag:unknown_type" not in url


# ---------------------------------------------------------------------------
# Test: mac_save_to_vault default path and status
# ---------------------------------------------------------------------------


class TestMacSaveToVaultDefaults:
    """Test mac_save_to_vault defaults to 00-inbox/ with status: draft."""

    @patch("urllib.request.urlopen")
    @patch("mac.mcp_server._obsidian_headers", return_value={"Authorization": "Bearer test"})
    def test_default_path_is_inbox(self, _headers, mock_urlopen) -> None:
        mock_urlopen.return_value = _mock_urlopen({"saved": True}, status=200)
        result = mac_save_to_vault(content="# Test Note\nSome content")
        parsed = json.loads(result)
        assert "00-inbox/" in parsed["saved"]

    @patch("urllib.request.urlopen")
    @patch("mac.mcp_server._obsidian_headers", return_value={"Authorization": "Bearer test"})
    def test_default_status_is_draft(self, _headers, mock_urlopen) -> None:
        mock_urlopen.return_value = _mock_urlopen({"saved": True}, status=200)
        mac_save_to_vault(content="# Test Note\nSome content")
        # Verify the content sent includes status: draft
        call_args = mock_urlopen.call_args
        sent_data = json.loads(call_args[0][0].data) if call_args[0] else {}
        if not sent_data:
            sent_data = json.loads(call_args[1].get("data", b"{}"))
        content = sent_data.get("content", "")
        assert "status: draft" in content

    @patch("urllib.request.urlopen")
    @patch("mac.mcp_server._obsidian_headers", return_value={"Authorization": "Bearer test"})
    def test_explicit_path_overrides_default(self, _headers, mock_urlopen) -> None:
        mock_urlopen.return_value = _mock_urlopen({"saved": True}, status=200)
        result = mac_save_to_vault(
            content="# Test",
            path="10-projects/my-project/api.md",
        )
        parsed = json.loads(result)
        assert parsed["saved"] == "10-projects/my-project/api.md"

    @patch("urllib.request.urlopen")
    @patch("mac.mcp_server._obsidian_headers", return_value={"Authorization": "Bearer test"})
    def test_slug_from_heading(self, _headers, mock_urlopen) -> None:
        mock_urlopen.return_value = _mock_urlopen({"saved": True}, status=200)
        result = mac_save_to_vault(content="# API Design Decisions\nContent here")
        parsed = json.loads(result)
        assert "api-design-decisions" in parsed["saved"]
        assert "00-inbox/" in parsed["saved"]

    @patch("urllib.request.urlopen")
    @patch("mac.mcp_server._obsidian_headers", return_value={"Authorization": "Bearer test"})
    def test_status_reviewed(self, _headers, mock_urlopen) -> None:
        mock_urlopen.return_value = _mock_urlopen({"saved": True}, status=200)
        mac_save_to_vault(
            content="# Reviewed Note",
            status="reviewed",
        )
        call_args = mock_urlopen.call_args
        sent_data = json.loads(call_args[0][0].data)
        content = sent_data.get("content", "")
        assert "status: reviewed" in content


# ---------------------------------------------------------------------------
# Test: mac_promote_to_knowledge
# ---------------------------------------------------------------------------


class TestMacPromoteToKnowledge:
    """Test mac_promote_to_knowledge moves draft to permanent zone."""

    @patch("urllib.request.urlopen")
    @patch("mac.mcp_server._obsidian_headers", return_value={"Authorization": "Bearer test"})
    def test_promote_updates_status_and_moves(self, _headers, mock_urlopen) -> None:
        # Mock: first call = read source, second = write target, third = delete source
        source_content = {
            "content": "---\ncreated: 2026-08-10\nprivacy: private\nstatus: draft\n---\n\n# API Design"
        }
        read_resp = _mock_urlopen(source_content)
        write_resp = _mock_urlopen({"saved": True}, status=200)
        delete_resp = _mock_urlopen({}, status=204)

        mock_urlopen.side_effect = [read_resp, write_resp, delete_resp]

        result = mac_promote_to_knowledge(
            source_path="00-inbox/2026-08-10-api-design.md",
            target_path="10-projects/my-project/api-design.md",
        )
        parsed = json.loads(result)
        assert parsed["promoted"] is True
        assert parsed["from"] == "00-inbox/2026-08-10-api-design.md"
        assert parsed["to"] == "10-projects/my-project/api-design.md"
        assert parsed["status"] == "promoted"

        # Verify the written content has status: promoted
        write_call = mock_urlopen.call_args_list[1]
        sent_data = json.loads(write_call[0][0].data)
        content = sent_data.get("content", "")
        assert "status: promoted" in content

    @patch("urllib.request.urlopen")
    @patch("mac.mcp_server._obsidian_headers", return_value={"Authorization": "Bearer test"})
    def test_promote_source_not_found(self, _headers, mock_urlopen) -> None:
        import urllib.error

        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="", code=404, msg="Not Found", hdrs=None, fp=None
        )
        with pytest.raises(ToolError, match="source not found"):
            mac_promote_to_knowledge(
                source_path="00-inbox/nonexistent.md",
                target_path="10-projects/test.md",
            )


# ---------------------------------------------------------------------------
# Test: mac_done EOD hint
# ---------------------------------------------------------------------------


class TestMacDoneEodHint:
    """Test mac_done returns eod_hint when 3+ tasks completed today."""

    def _setup_and_complete_tasks(self, count: int) -> dict:
        """Submit, claim, and complete `count` tasks, return last done result."""
        import mac.mcp_server as mod
        from mac.mcp_server import (
            mac_claim_task,
            mac_submit_task,
        )

        # Register agent directly via registry (no mac_register_agent tool)
        reg = mod._registry()
        reg.register(_agent("eod-test-agent"))

        last_result: dict = {}
        for i in range(count):
            task_id = f"eod-task-{i}"
            mac_submit_task(_task_dict(task_id, source="eod-test-agent"))
            claim_result = json.loads(mac_claim_task(agent_id="eod-test-agent", capability="write_code"))
            claimed_id = claim_result.get("task_id", task_id)
            last_result = json.loads(mac_done(task_id=claimed_id, agent_id="eod-test-agent"))

        return last_result

    def test_eod_hint_not_shown_for_few_tasks(self) -> None:
        """Less than 3 completed tasks should NOT trigger eod_hint."""
        result = self._setup_and_complete_tasks(2)
        assert "eod_hint" not in result

    def test_eod_hint_shown_for_many_tasks(self) -> None:
        """3+ completed tasks should trigger eod_hint."""
        result = self._setup_and_complete_tasks(3)
        assert "eod_hint" in result
        assert "EOD" in result["eod_hint"] or "eod" in result["eod_hint"].lower()


# ---------------------------------------------------------------------------
# Test: session-context resource daily_notes
# ---------------------------------------------------------------------------


class TestSessionContextDailyNotes:
    """Test mac://session-context includes daily_notes field."""

    def test_daily_notes_field_exists_without_obsidian(self) -> None:
        """daily_notes should be empty list when Obsidian is not available."""
        result = json.loads(session_context_resource())
        assert "daily_notes" in result
        # Without Obsidian running, should be empty list
        assert isinstance(result["daily_notes"], list)

    @patch("urllib.request.urlopen")
    @patch("mac.mcp_server._obsidian_headers", return_value={"Authorization": "Bearer test"})
    def test_daily_notes_populated_when_available(self, _headers, mock_urlopen) -> None:
        """daily_notes should contain notes when Obsidian returns them."""
        from datetime import datetime, timedelta, timezone

        # Mock 3 daily note reads
        today = datetime.now(timezone.utc)
        responses = []
        for i in range(3):
            d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            note = {
                "content": f"---\ncreated: {d}\n---\n\n# EOD {d}\n\n## Done\n- Task {i}"
            }
            responses.append(_mock_urlopen(note))

        # Add a 404 for one day to test graceful degradation
        import urllib.error

        responses[2] = urllib.error.HTTPError(
            url="", code=404, msg="Not Found", hdrs=None, fp=None
        )

        mock_urlopen.side_effect = responses

        result = json.loads(session_context_resource())
        assert "daily_notes" in result
        # Should have 2 notes (one 404 was skipped)
        assert len(result["daily_notes"]) == 2
