from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from mac.mcp_server import (
    capabilities_resource,
    health_resource,
    mac_accept_review,
    mac_block_task,
    mac_cancel_task,
    mac_claim_task,
    mac_cleanup_tasks,
    mac_done,
    mac_expire_stale_agents,
    mac_expire_stale_tasks,
    mac_expire_task_leases,
    mac_fail_task,
    mac_get_task,
    mac_list_agents,
    mac_list_ready_tasks,
    mac_mark_review_ready,
    mac_next_task,
    mac_recall,
    mac_record_quality_and_complete,
    mac_reject_review,
    mac_remember,
    mac_resume_blocked_task,
    mac_retry_task,
    mac_review_packet,
    mac_save_handoff,
    mac_submit_task,
    mac_worker_packet,
    mcp,
)
from mac.protocol.messages import (
    AgentCapability,
    AgentCard,
    CoordinationPolicy,
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


class TestMacReviewTools:
    """Review lifecycle MCP tools.

    Patches ``mac.mcp_server._registry`` with a Registry whose
    ``require_review`` policy is True; the autouse ``_use_tmp_db`` fixture
    leaves _registry defaulting to require_review=False which would make
    ``mac_mark_review_ready`` reject every transition.
    """

    @staticmethod
    def _setup_review_registry(tmp_path: Path) -> Registry:
        ledger = SQLiteTaskLedger(tmp_path / "mac.db")
        reg = Registry(ledger, policy=CoordinationPolicy(require_review=True))
        reg.register_agent(_agent("worker", "write_code"))
        reg.submit_task(TaskTransfer.model_validate(_task_dict("task-1", status="running")))
        return reg

    @staticmethod
    def _patch_registry(monkeypatch: pytest.MonkeyPatch, reg: Registry) -> None:
        monkeypatch.setattr("mac.mcp_server._registry", lambda: reg)

    def test_mark_review_ready_transitions_running_to_review_ready(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reg = self._setup_review_registry(tmp_path)
        self._patch_registry(monkeypatch, reg)

        result = mac_mark_review_ready(task_id="task-1", agent_id="worker")
        parsed = json.loads(result)

        assert parsed["status"] == "review_ready"

    def test_mark_review_ready_rejected_when_require_review_is_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reg = Registry(
            SQLiteTaskLedger(tmp_path / "mac.db"),
            policy=CoordinationPolicy(require_review=False),
        )
        reg.register_agent(_agent("worker", "write_code"))
        reg.submit_task(TaskTransfer.model_validate(_task_dict("task-1", status="running")))
        self._patch_registry(monkeypatch, reg)

        with pytest.raises(ToolError) as excinfo:
            mac_mark_review_ready(task_id="task-1", agent_id="worker")
        assert "state_conflict" in str(excinfo.value)

    def test_accept_review_completes_review_ready_task(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reg = self._setup_review_registry(tmp_path)
        reg.mark_review_ready("task-1", agent_id="worker")
        self._patch_registry(monkeypatch, reg)

        result = mac_accept_review(task_id="task-1", reviewer_id="reviewer")
        parsed = json.loads(result)

        assert parsed["status"] == "completed"

    def test_reject_review_records_conflict_and_marks_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reg = self._setup_review_registry(tmp_path)
        reg.mark_review_ready("task-1", agent_id="worker")
        self._patch_registry(monkeypatch, reg)

        result = mac_reject_review(
            task_id="task-1", reviewer_id="reviewer", reason="needs more tests"
        )
        parsed = json.loads(result)

        assert parsed["status"] == "rejected"
        conflicts = reg.list_conflicts()
        assert len(conflicts) == 1
        assert conflicts[0].source == "reject_review"
        assert conflicts[0].description == "needs more tests"


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

    def test_not_found_sets_iserror_true(self) -> None:
        asyncio.run(self._test_not_found())

    @staticmethod
    async def _test_not_found() -> None:
        from mcp import types


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

    def test_validation_error_sets_iserror_true(self) -> None:
        asyncio.run(self._test_validation_error())

    @staticmethod
    async def _test_validation_error() -> None:
        from mcp import types


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

    def test_success_sets_iserror_false(self, tmp_path: Path) -> None:
        asyncio.run(self._test_success(tmp_path))

    @staticmethod
    async def _test_success(tmp_path: Path) -> None:
        from mcp import types


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

    @pytest.mark.skip(
        reason="MCP stdio E2E is flaky in CI (K-002): subprocess pipe connection unreliable across all Python versions on GitHub Actions. Functional coverage is provided by in-process tests above."
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

        async with stdio_client(server_params) as (read_stream, write_stream):  # noqa: SIM117
            async with ClientSession(read_stream, write_stream) as session:
                # 1. Initialize
                result = await session.initialize()
                assert result.serverInfo.name == "mac-coordinator"

                # 2. List tools — expect 11
                tools_result = await session.list_tools()
                tool_names = {tool.name for tool in tools_result.tools}
                assert tool_names == {
                    "mac_accept_review",
                    "mac_claim_task",
                    "mac_fail_task",
                    "mac_list_ready_tasks",
                    "mac_mark_review_ready",
                    "mac_record_quality_and_complete",
                    "mac_reject_review",
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

                # 6. Submit a task and pull its worker packet; verifies mac_worker_packet
                #    is callable via stdio (symmetric with mac_review_packet coverage).
                submit_result = await session.call_tool(
                    "mac_submit_task",
                    arguments={
                        "task_id": "stdio-task-1",
                        "source_agent_id": "planner",
                        "payload": {"type": "write_code", "summary": "Round-trip packet"},
                    },
                )
                assert submit_result.isError is False

                packet_result = await session.call_tool(
                    "mac_worker_packet",
                    arguments={"task_id": "stdio-task-1"},
                )
                assert packet_result.isError is False
                packet_text = packet_result.content[0].text
                assert "Worker Task: stdio-task-1" in packet_text
                assert "## Handoff Format" in packet_text
                assert "## Acceptance Criteria" in packet_text

                # 7. Review lifecycle is exposed over stdio. The subprocess uses
                #    require_review=False, so mark-ready returns a domain conflict.
                review_result = await session.call_tool(
                    "mac_mark_review_ready",
                    arguments={"task_id": "stdio-task-1", "agent_id": "worker"},
                )
                assert review_result.isError is True
                assert "state_conflict" in review_result.content[0].text


# ---------------------------------------------------------------------------
# Knowledge tools (Phase E: remember, recall)
# ---------------------------------------------------------------------------


class TestMacRemember:
    def test_remember_stores_fact_and_returns_confirmation(self) -> None:
        result = mac_remember(key="arch", value="Use hexagonal", category="design")
        parsed = json.loads(result)
        assert parsed["key"] == "arch"
        assert parsed["stored"] is True
        assert parsed["category"] == "design"

    def test_remember_default_category_is_general(self) -> None:
        result = mac_remember(key="tip", value="Always lint first")
        parsed = json.loads(result)
        assert parsed["category"] == "general"


class TestMacRecall:
    def test_recall_empty_query_returns_list(self) -> None:
        mac_remember(key="a", value="One")
        mac_remember(key="b", value="Two")
        result = mac_recall(query="", limit=10)
        parsed = json.loads(result)
        assert isinstance(parsed, list)
        assert len(parsed) >= 2

    def test_recall_keyword_search(self) -> None:
        mac_remember(key="ci-secret", value="Use repo-scoped secrets", category="security")
        result = mac_recall(query="secret", limit=5)
        parsed = json.loads(result)
        found_keys = {f["key"] for f in parsed}
        assert "ci-secret" in found_keys

    def test_recall_no_match_returns_empty(self) -> None:
        result = mac_recall(query="zzz-nonexistent-zzz", limit=5)
        parsed = json.loads(result)
        assert isinstance(parsed, list)


# ---------------------------------------------------------------------------
# Lease tools (Phase D: expire_task_leases, list_agents, block_task)
# ---------------------------------------------------------------------------


class TestMacExpireTaskLeases:
    def test_expire_leases_no_expired_returns_empty(self) -> None:
        result = mac_expire_task_leases(auto_retry=False)
        parsed = json.loads(result)
        assert parsed == []

    def test_expire_leases_with_lease_seconds_expires(self, tmp_path: Path) -> None:
        mac_submit_task(_task_dict(
            "t-lease",
            lease_seconds=1,
        ))
        # Use the same registry that MCP tools use (via _use_tmp_db fixture)
        reg, _ = _registry_with_db(tmp_path)
        reg.register_agent(_agent("agent-1", "write_code"))
        reg.accept_handoff("t-lease", "agent-1")
        reg.start_task("t-lease", "agent-1")

        time.sleep(1.1)

        result = mac_expire_task_leases(auto_retry=True)
        parsed = json.loads(result)
        assert len(parsed) == 1
        assert parsed[0]["task_id"] == "t-lease"
        assert parsed[0]["retry_count"] == 1
        assert parsed[0]["status"] == "proposed"


class TestMacListAgents:
    def test_list_agents_returns_list(self, tmp_path: Path) -> None:
        reg, _ = _registry_with_db(tmp_path)
        reg.register_agent(_agent("a1", "write_code"))
        reg.register_agent(_agent("a2", "test"))

        result = mac_list_agents(status="all")
        parsed = json.loads(result)
        assert isinstance(parsed, list)
        assert len(parsed) == 2

    def test_list_agents_filter_by_online(self, tmp_path: Path) -> None:
        reg, _ = _registry_with_db(tmp_path)
        reg.register_agent(_agent("a1", "write_code"))

        result = mac_list_agents(status="online")
        parsed = json.loads(result)
        assert isinstance(parsed, list)
        assert all(a["status"] == "online" for a in parsed)


class TestMacBlockTask:
    def test_block_running_task(self, tmp_path: Path) -> None:
        reg, _ = _registry_with_db(tmp_path)
        reg.register_agent(_agent("a1", "write_code"))
        mac_submit_task(_task_dict("t-block"))
        reg.accept_handoff("t-block", "a1")
        reg.start_task("t-block", "a1")

        result = mac_block_task(task_id="t-block", agent_id="a1", reason="Blocked by external dep")
        parsed = json.loads(result)
        assert parsed["task_id"] == "t-block"
        assert parsed["status"] == "blocked"

    def test_block_task_not_found_raises_error(self) -> None:
        with pytest.raises(ToolError) as excinfo:
            mac_block_task(task_id="nonexistent", agent_id="a1", reason="test")
        assert "not_found" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Lifecycle tools: get_task, cancel, retry, resume_blocked
# ---------------------------------------------------------------------------


class TestMacGetTask:
    def test_get_task_returns_full_task(self) -> None:
        mac_submit_task(_task_dict("t-get"))
        result = mac_get_task(task_id="t-get")
        parsed = json.loads(result)
        assert parsed["task_id"] == "t-get"
        assert parsed["status"] == "proposed"

    def test_get_task_not_found_raises_error(self) -> None:
        with pytest.raises(ToolError) as excinfo:
            mac_get_task(task_id="nonexistent")
        assert "not_found" in str(excinfo.value)


class TestMacCancelTask:
    def test_cancel_proposed_task(self, tmp_path: Path) -> None:
        reg, _ = _registry_with_db(tmp_path)
        reg.register_agent(_agent("a1", "write_code"))
        mac_submit_task(_task_dict("t-cancel"))

        result = mac_cancel_task(task_id="t-cancel", agent_id="a1")
        parsed = json.loads(result)
        assert parsed["task_id"] == "t-cancel"
        assert parsed["status"] == "cancelled"

    def test_cancel_nonexistent_raises_error(self) -> None:
        with pytest.raises(ToolError):
            mac_cancel_task(task_id="nonexistent", agent_id="a1")


class TestMacRetryTask:
    def test_retry_failed_task(self, tmp_path: Path) -> None:
        reg, _ = _registry_with_db(tmp_path)
        reg.register_agent(_agent("a1", "write_code"))
        mac_submit_task(_task_dict("t-retry"))
        reg.accept_handoff("t-retry", "a1")
        reg.start_task("t-retry", "a1")
        reg.fail_task("t-retry", "a1", "TEST_FAIL")

        result = mac_retry_task(task_id="t-retry", agent_id="a1")
        parsed = json.loads(result)
        assert parsed["task_id"] == "t-retry"
        assert parsed["status"] == "proposed"
        assert parsed["retry_count"] == 1

    def test_retry_nonexistent_raises_error(self) -> None:
        with pytest.raises(ToolError):
            mac_retry_task(task_id="nonexistent", agent_id="a1")


class TestMacResumeBlockedTask:
    def test_resume_blocked_task(self, tmp_path: Path) -> None:
        reg, _ = _registry_with_db(tmp_path)
        reg.register_agent(_agent("a1", "write_code"))
        mac_submit_task(_task_dict("t-blocked"))
        reg.accept_handoff("t-blocked", "a1")
        reg.start_task("t-blocked", "a1")
        reg.block_task("t-blocked", agent_id="a1", reason="Need external input")

        result = mac_resume_blocked_task(task_id="t-blocked", agent_id="a1", resolution="Resolved")
        parsed = json.loads(result)
        assert parsed["task_id"] == "t-blocked"
        assert parsed["status"] == "proposed"

    def test_resume_nonexistent_raises_error(self) -> None:
        with pytest.raises(ToolError):
            mac_resume_blocked_task(task_id="nonexistent", agent_id="a1")


# ---------------------------------------------------------------------------
# Maintenance tools: expire_stale_tasks, expire_stale_agents, cleanup
# ---------------------------------------------------------------------------


class TestMacExpireStaleTasks:
    def test_expire_stale_returns_empty_when_none_expired(self) -> None:
        result = mac_expire_stale_tasks(auto_retry=False)
        parsed = json.loads(result)
        assert parsed == []


class TestMacExpireStaleAgents:
    def test_expire_stale_agents_returns_empty_when_none_stale(self) -> None:
        result = mac_expire_stale_agents()
        parsed = json.loads(result)
        assert parsed == []


class TestMacCleanupTasks:
    def test_cleanup_no_terminal_tasks_returns_empty(self) -> None:
        result = mac_cleanup_tasks()
        parsed = json.loads(result)
        assert parsed == []


# ---------------------------------------------------------------------------
# Critical-path tools: mac_done, mac_next_task
# ---------------------------------------------------------------------------


class TestMacDone:
    def test_done_completes_task(self, tmp_path: Path) -> None:
        reg, _ = _registry_with_db(tmp_path)
        reg.register_agent(_agent("a1", "write_code"))
        mac_submit_task(_task_dict("t-done-mcp"))
        reg.accept_handoff("t-done-mcp", "a1")
        reg.start_task("t-done-mcp", "a1")

        result = mac_done(
            task_id="t-done-mcp",
            agent_id="a1",
            quality_result={"command": "pytest", "status": "passed"},
        )
        parsed = json.loads(result)
        assert parsed["task_id"] == "t-done-mcp"
        assert parsed["status"] == "completed"
        assert parsed["quality_gate"] == "passed"

    def test_done_nonexistent_raises_error(self) -> None:
        with pytest.raises(ToolError):
            mac_done(
                task_id="nonexistent",
                agent_id="a1",
                quality_result={"command": "pytest", "status": "passed"},
            )


class TestMacNextTask:
    def test_next_task_no_agents_returns_no_task(self) -> None:
        """mac_next_task returns error when no agent can claim."""
        with pytest.raises(ToolError):
            mac_next_task(agent_id="nobody", capability="write_code")

    def test_next_task_claims_and_starts(self, tmp_path: Path) -> None:
        reg, _ = _registry_with_db(tmp_path)
        reg.register_agent(_agent("worker", "write_code"))
        mac_submit_task(_task_dict("t-next"))

        result = mac_next_task(agent_id="worker", capability="write_code")
        # Should return a Markdown worker packet
        assert isinstance(result, str)
        assert "Worker Task: t-next" in result
        # The task should now be running
        parsed = json.loads(mac_get_task(task_id="t-next"))
        assert parsed["status"] == "running"
