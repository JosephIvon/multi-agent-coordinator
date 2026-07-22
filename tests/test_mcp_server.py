from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from mcp.server.fastmcp.exceptions import ToolError

from mac.mcp_server import (
    capabilities_resource,
    health_resource,
    mac_claim_task,
    mac_fail_task,
    mac_list_ready_tasks,
    mac_record_quality_and_complete,
    mac_review_packet,
    mac_save_handoff,
    mac_submit_task,
    mac_worker_packet,
    mcp,
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
    """Create a fresh Registry + Ledger pair for each test."""
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
    """Build a TaskTransfer-compatible dict for mac_submit_task."""
    base = TaskTransfer(
        task_id=task_id,
        source_agent_id=source,
        payload=TaskPayload(type=capability, summary=f"{task_id} summary"),
    ).model_dump()
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Patch mcp_server._DB_PATH so each test uses its own database
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _use_tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect the MCP server to use a per-test temporary database."""
    db_path = tmp_path / "mac.db"
    import mac.mcp_server as mod

    monkeypatch.setattr(mod, "_DB_PATH", db_path)
    # Also patch _registry to use the tmp db so all tools share the same ledger
    _orig_registry = mod._registry

    def _patched_registry() -> Registry:
        return Registry(SQLiteTaskLedger(db_path))

    monkeypatch.setattr(mod, "_registry", _patched_registry)


# ---------------------------------------------------------------------------
# Tool tests (7 tools)
# ---------------------------------------------------------------------------


class TestMacSubmitTask:
    def test_submit_returns_created_task(self) -> None:
        result = mac_submit_task(_task_dict("task-1"))
        parsed = json.loads(result)
        assert parsed["task_id"] == "task-1"
        assert parsed["status"] == "proposed"

    def test_submit_with_full_task_transfer_shape(self, tmp_path: Path) -> None:
        reg, _ = _registry_with_db(tmp_path)
        # Create the plan first so plan_id reference is valid
        reg.create_plan(goal="Test plan", created_by="planner", plan_id="plan-1")
        task = _task_dict("task-2", depends_on=["task-1"], plan_id="plan-1")
        result = mac_submit_task(task)
        parsed = json.loads(result)
        assert parsed["task_id"] == "task-2"
        assert parsed["depends_on"] == ["task-1"]
        assert parsed["plan_id"] == "plan-1"

    def test_submit_invalid_shape_returns_error(self) -> None:
        with pytest.raises(ToolError) as excinfo:
            mac_submit_task({"bad": "data"})
        assert "validation_failed" in str(excinfo.value)


class TestMacClaimTask:
    def test_claim_returns_accepted_and_started_task(self, tmp_path: Path) -> None:
        reg, _ = _registry_with_db(tmp_path)
        reg.register_agent(_agent())
        reg.submit_task(
            TaskTransfer(
                task_id="task-1",
                source_agent_id="planner",
                payload=TaskPayload(type="write_code", summary="Do work"),
            )
        )

        result = mac_claim_task(agent_id="agent-1", capability="write_code")
        parsed = json.loads(result)
        assert parsed["task_id"] == "task-1"
        assert parsed["status"] == "running"

    def test_claim_no_matching_task_returns_not_found(self) -> None:
        with pytest.raises(ToolError) as excinfo:
            mac_claim_task(agent_id="agent-1", capability="nonexistent")
        assert "not_found" in str(excinfo.value)


class TestMacRecordQualityAndComplete:
    def _setup_running_task(self, reg: Registry) -> None:
        """Register agent + submit + claim + start → running task."""
        reg.register_agent(_agent())
        task = TaskTransfer(
            task_id="task-1",
            source_agent_id="planner",
            payload=TaskPayload(type="write_code", summary="Do work"),
        )
        reg.submit_task(task)
        reg.claim_next_task(agent_id="agent-1", capability="write_code")
        reg.start_task("task-1", "agent-1")

    def test_gate_passes_completes_task(self, tmp_path: Path) -> None:
        reg, _ = _registry_with_db(tmp_path)
        self._setup_running_task(reg)

        result = mac_record_quality_and_complete(
            task_id="task-1",
            agent_id="agent-1",
            result={"command": "pytest", "status": "passed", "evidence": ["test_output"]},
        )
        parsed = json.loads(result)
        assert parsed["status"] == "completed"

    def test_gate_fails_returns_running_with_reason(self, tmp_path: Path) -> None:
        from mac.testing.contracts import TestContract

        reg, _ = _registry_with_db(tmp_path)
        self._setup_running_task(reg)
        # Assign a test contract so the gate actually checks results
        task = reg.ledger.get_task_transfer("task-1")
        task.test_contract = TestContract(
            risk_level="medium",
            required_commands=["python -m pytest tests"],
            required_evidence=["test_output"],
        )
        reg.ledger.save_task_transfer(task)

        result = mac_record_quality_and_complete(
            task_id="task-1",
            agent_id="agent-1",
            result={"command": "pytest", "status": "failed"},
        )
        parsed = json.loads(result)
        assert parsed["status"] == "running"
        assert parsed["reason"] is not None

    def test_no_contract_auto_completes(self, tmp_path: Path) -> None:
        reg, _ = _registry_with_db(tmp_path)
        self._setup_running_task(reg)
        # Task has no test_contract → gate passes on any passed result
        result = mac_record_quality_and_complete(
            task_id="task-1",
            agent_id="agent-1",
            result={"command": "pytest", "status": "passed"},
        )
        parsed = json.loads(result)
        assert parsed["status"] == "completed"


class TestMacFailTask:
    def test_fail_running_task(self, tmp_path: Path) -> None:
        reg, _ = _registry_with_db(tmp_path)
        reg.register_agent(_agent())
        task = TaskTransfer(
            task_id="task-1",
            source_agent_id="planner",
            payload=TaskPayload(type="write_code", summary="Do work"),
        )
        reg.submit_task(task)
        reg.claim_next_task(agent_id="agent-1", capability="write_code")
        reg.start_task("task-1", "agent-1")

        result = mac_fail_task(
            task_id="task-1",
            agent_id="agent-1",
            error_code="QUALITY_GATE_FAILED",
        )
        parsed = json.loads(result)
        assert parsed["status"] == "failed"
        assert parsed["error_code"] == "QUALITY_GATE_FAILED"


class TestMacSaveHandoff:
    def test_save_handoff_result(self, tmp_path: Path) -> None:
        reg, _ = _registry_with_db(tmp_path)
        reg.register_agent(_agent())
        task = TaskTransfer(
            task_id="task-1",
            source_agent_id="planner",
            payload=TaskPayload(type="write_code", summary="Do work"),
        )
        reg.submit_task(task)

        result = mac_save_handoff(
            task_id="task-1",
            agent_id="agent-1",
            changed_files=["src/main.py", "tests/test_main.py"],
            verification_passed=True,
        )
        parsed = json.loads(result)
        assert parsed["task_id"] == "task-1"
        assert parsed["changed_files"] == ["src/main.py", "tests/test_main.py"]


class TestMacListReadyTasks:
    def test_list_ready_tasks_empty(self) -> None:
        result = mac_list_ready_tasks(capability="write_code")
        parsed = json.loads(result)
        assert parsed == []

    def test_list_ready_tasks_returns_proposed(self, tmp_path: Path) -> None:
        reg, _ = _registry_with_db(tmp_path)
        reg.submit_task(
            TaskTransfer(
                task_id="task-1",
                source_agent_id="planner",
                payload=TaskPayload(type="write_code", summary="Do work"),
            )
        )
        result = mac_list_ready_tasks(capability="write_code")
        parsed = json.loads(result)
        assert len(parsed) == 1
        assert parsed[0]["task_id"] == "task-1"


class TestMacReviewPacket:
    def test_review_packet_returns_markdown(self, tmp_path: Path) -> None:
        reg, _ = _registry_with_db(tmp_path)
        reg.submit_task(
            TaskTransfer(
                task_id="task-1",
                source_agent_id="planner",
                payload=TaskPayload(type="write_code", summary="Do work"),
            )
        )
        result = mac_review_packet(task_id="task-1")
        assert "task-1" in result
        assert "##" in result  # Markdown header

    def test_review_packet_not_found(self) -> None:
        with pytest.raises(ToolError) as excinfo:
            mac_review_packet(task_id="nonexistent")
        assert "not_found" in str(excinfo.value)


class TestMacWorkerPacket:
    def test_worker_packet_returns_markdown_after_claim(self, tmp_path: Path) -> None:
        reg, _ = _registry_with_db(tmp_path)
        reg.register_agent(_agent())
        reg.submit_task(
            TaskTransfer(
                task_id="task-1",
                source_agent_id="planner",
                payload=TaskPayload(type="write_code", summary="Ship worker packet"),
            )
        )
        claimed = reg.claim_next_task(agent_id="agent-1", capability="write_code")
        assert claimed is not None

        result = mac_worker_packet(task_id="task-1", agent_id="agent-1")

        assert "Worker Task: task-1" in result
        assert "## Goal" in result
        assert "## Acceptance Criteria" in result
        assert "## Handoff Format" in result

    def test_worker_packet_includes_agent_boundary_when_agent_id_given(
        self, tmp_path: Path
    ) -> None:
        reg, _ = _registry_with_db(tmp_path)
        reg.register_agent(
            AgentCard(
                agent_id="agent-1",
                name="agent-1",
                capabilities=[AgentCapability(name="write_code")],
                allowed_paths=["src/**"],
                forbidden_paths=["src/secrets/**"],
            )
        )
        reg.submit_task(
            TaskTransfer(
                task_id="task-1",
                source_agent_id="planner",
                payload=TaskPayload(type="write_code", summary="Ship worker packet"),
            )
        )
        reg.claim_next_task(agent_id="agent-1", capability="write_code")

        result = mac_worker_packet(task_id="task-1", agent_id="agent-1")

        assert "## Agent Boundary" in result
        assert "src/**" in result
        assert "src/secrets/**" in result

    def test_worker_packet_omits_boundary_when_agent_id_omitted(
        self, tmp_path: Path
    ) -> None:
        reg, _ = _registry_with_db(tmp_path)
        reg.submit_task(
            TaskTransfer(
                task_id="task-1",
                source_agent_id="planner",
                payload=TaskPayload(type="write_code", summary="Just task, no agent"),
            )
        )

        result = mac_worker_packet(task_id="task-1")

        assert "Worker Task: task-1" in result
        assert "## Agent Boundary" not in result

    def test_worker_packet_not_found(self) -> None:
        with pytest.raises(ToolError) as excinfo:
            mac_worker_packet(task_id="nonexistent")
        assert "not_found" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Transport-layer error semantics (isError=True via MCP request handler)
# ---------------------------------------------------------------------------


class TestToolErrorIsErrorFlag:
    """Exercise the low-level MCP request handler so we can assert that
    MAC domain errors surface as ``CallToolResult(isError=True)``.

    Runs in-process via ``mcp._mcp_server.request_handlers`` — the same
    code path the stdio transport invokes. Does not require a subprocess.
    """

    @pytest.mark.asyncio
    async def test_not_found_sets_iserror_true(self) -> None:
        from mcp import types

        from mac.mcp_server import mcp

        handler = mcp._mcp_server.request_handlers[types.CallToolRequest]
        req = types.CallToolRequest(
            params=types.CallToolRequestParams(
                name="mac_claim_task",
                arguments={"agent_id": "agent-1", "capability": "nonexistent"},
            )
        )
        result = await handler(req)
        assert result.root.isError is True
        assert "not_found" in result.root.content[0].text

    @pytest.mark.asyncio
    async def test_validation_error_sets_iserror_true(self) -> None:
        from mcp import types

        from mac.mcp_server import mcp

        handler = mcp._mcp_server.request_handlers[types.CallToolRequest]
        req = types.CallToolRequest(
            params=types.CallToolRequestParams(
                name="mac_submit_task",
                arguments={"bad": "data"},
            )
        )
        result = await handler(req)
        # The Pydantic ValidationError is raised by FastMCP's arg validator
        # *before* entering _safe_call, so the SDK wraps it as a ToolError.
        # We only need to confirm isError=True; the precise text format
        # is SDK-owned.
        assert result.root.isError is True
        assert "validation" in result.root.content[0].text.lower()

    @pytest.mark.asyncio
    async def test_success_sets_iserror_false(self, tmp_path: Path) -> None:
        from mcp import types

        from mac.mcp_server import mcp

        reg, _ = _registry_with_db(tmp_path)
        reg.submit_task(
            TaskTransfer(
                task_id="task-1",
                source_agent_id="planner",
                payload=TaskPayload(type="write_code", summary="Do work"),
            )
        )

        handler = mcp._mcp_server.request_handlers[types.CallToolRequest]
        req = types.CallToolRequest(
            params=types.CallToolRequestParams(
                name="mac_list_ready_tasks",
                arguments={"capability": "write_code"},
            )
        )
        result = await handler(req)
        assert result.root.isError is False
        parsed = json.loads(result.root.content[0].text)
        assert len(parsed) == 1
        assert parsed[0]["task_id"] == "task-1"


# ---------------------------------------------------------------------------
# Resource tests (2 resources)
# ---------------------------------------------------------------------------


class TestCapabilitiesResource:
    def test_capabilities_empty(self) -> None:
        result = capabilities_resource()
        parsed = json.loads(result)
        assert parsed == {}

    def test_capabilities_with_agents(self, tmp_path: Path) -> None:
        reg, _ = _registry_with_db(tmp_path)
        reg.register_agent(_agent("agent-1", "write_code"))
        reg.register_agent(_agent("agent-2", "write_test"))
        result = capabilities_resource()
        parsed = json.loads(result)
        assert "write_code" in parsed
        assert "write_test" in parsed
        assert "agent-1" in parsed["write_code"]
        assert "agent-2" in parsed["write_test"]


class TestHealthResource:
    def test_health_empty(self) -> None:
        result = health_resource()
        parsed = json.loads(result)
        assert parsed["open_tasks"] == 0
        assert parsed["inflight_agents"] == 0

    def test_health_with_data(self, tmp_path: Path) -> None:
        reg, _ = _registry_with_db(tmp_path)
        reg.register_agent(_agent())
        reg.submit_task(
            TaskTransfer(
                task_id="task-1",
                source_agent_id="planner",
                payload=TaskPayload(type="write_code", summary="Do work"),
            )
        )
        result = health_resource()
        parsed = json.loads(result)
        assert parsed["open_tasks"] == 1


# ---------------------------------------------------------------------------
# Stdio E2E test (verifies real MCP JSON-RPC transport over subprocess)
# ---------------------------------------------------------------------------


class TestStdioE2E:
    """Launch the MCP server as a real subprocess and exercise the JSON-RPC
    protocol via mcp.client.stdio + ClientSession.

    NOTE: Skipped on Windows due to ProactorEventLoop subprocess pipe
    limitations (see K-002). The in-process tests above cover all
    functional paths; this test validates the stdio transport layer
    on Linux/macOS only.
    """

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows ProactorEventLoop does not support subprocess stdio pipes (K-002)",
    )
    def test_initialize_list_tools_and_call_tool(self, tmp_path: Path) -> None:
        """Full round-trip: initialize → list_tools → call_tool over stdio."""
        asyncio.run(self._run(tmp_path))

    @staticmethod
    async def _run(tmp_path: Path) -> None:
        import os

        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        server_params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mac.mcp_server"],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            cwd=str(tmp_path),  # mac.db will be created here
        )

        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                # 1. Initialize
                result = await session.initialize()
                assert result.serverInfo.name == "mac-coordinator"

                # 2. List tools — expect 8
                tools_result = await session.list_tools()
                tool_names = {tool.name for tool in tools_result.tools}
                assert tool_names == {
                    "mac_claim_task",
                    "mac_fail_task",
                    "mac_list_ready_tasks",
                    "mac_record_quality_and_complete",
                    "mac_review_packet",
                    "mac_save_handoff",
                    "mac_submit_task",
                    "mac_worker_packet",
                }

                # 3. List resources — expect 2
                resources_result = await session.list_resources()
                resource_uris = {r.uri for r in resources_result.resources}
                assert resource_uris == {
                    "mac://capabilities",
                    "mac://health",
                }

                # 4. Call a read-only tool (happy path)
                ready_result = await session.call_tool(
                    "mac_list_ready_tasks",
                    arguments={"capability": "write_code"},
                )
                # Result is a list of TextContent; parse the first one
                assert ready_result.isError is False
                assert len(ready_result.content) >= 1
                text = ready_result.content[0].text
                parsed = json.loads(text)
                assert parsed == []  # empty ledger → no ready tasks

                # 5. Call a tool that triggers a domain error; expect isError=True
                #    (the real MCP contract — distinct from legacy JSON error string).
                err_result = await session.call_tool(
                    "mac_claim_task",
                    arguments={"agent_id": "agent-1", "capability": "nonexistent"},
                )
                assert err_result.isError is True
                assert "not_found" in err_result.content[0].text
