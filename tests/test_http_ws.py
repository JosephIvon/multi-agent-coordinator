from fastapi.testclient import TestClient

from mac.protocol.messages import AgentCapability, AgentCard, ContextBundle, TaskPayload, TaskTransfer
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger
from mac.testing.contracts import TestContract
from mac.transport.http_ws import create_app


def test_http_app_runs_local_handoff_loop(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    client = TestClient(create_app(registry))

    writer = AgentCard(agent_id="writer", name="Writer", capabilities=[AgentCapability(name="write_code")])
    tester = AgentCard(agent_id="tester", name="Tester", capabilities=[AgentCapability(name="write_test")])

    assert client.post("/agents/register", json=writer.model_dump(mode="json")).status_code == 201
    assert client.post("/agents/register", json=tester.model_dump(mode="json")).status_code == 201

    agents = client.get("/agents", params={"capability": "write_test"}).json()
    assert [agent["agent_id"] for agent in agents] == ["tester"]

    task = TaskTransfer(
        task_id="task-1",
        trace_id="trace-1",
        source_agent_id="writer",
        target_agent_id="tester",
        payload=TaskPayload(
            type="write_test",
            summary="Write focused HTTP adapter tests",
            target_module="mac.transport.http_ws",
            coverage_goal=85,
            risk_level="high",
        ),
        context=ContextBundle(summary="HTTP adapter", artifact_refs=["file://src/mac/transport/http_ws.py"]),
        test_contract=TestContract.for_risk("high"),
    )

    submitted = client.post("/tasks", json=task.model_dump(mode="json")).json()
    assert submitted["status"] == "proposed"

    accepted = client.post("/tasks/task-1/accept", json={"agent_id": "tester"}).json()
    assert accepted["status"] == "accepted"

    running = client.post("/tasks/task-1/start", json={"agent_id": "tester"}).json()
    assert running["status"] == "running"

    quality_response = client.post(
        "/tasks/task-1/quality-results",
        json={
            "agent_id": "tester",
            "command": "python -m pytest --cov",
            "status": "passed",
            "evidence": ["test_output", "coverage_report", "review_notes"],
        },
    )
    assert quality_response.status_code == 204

    completed = client.post("/tasks/task-1/complete", json={"agent_id": "tester"}).json()
    assert completed["status"] == "completed"

    ledger = client.get("/ledger/trace-1").json()
    assert [entry["action"] for entry in ledger] == [
        "submit_task",
        "accept_handoff",
        "start_task",
        "submit_quality_result",
        "complete_task",
    ]


def test_http_heartbeat_refreshes_agent_card(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    client = TestClient(create_app(registry))
    agent = AgentCard(
        agent_id="tester",
        name="Tester",
        capabilities=[AgentCapability(name="write_test")],
        status="offline",
        load=90,
        last_heartbeat=1,
    )
    assert client.post("/agents/register", json=agent.model_dump(mode="json")).status_code == 201

    response = client.post("/agents/heartbeat", json={"agent_id": "tester", "status": "online", "load": 25})

    assert response.status_code == 204
    refreshed = client.get("/agents/tester").json()
    assert refreshed["status"] == "online"
    assert refreshed["load"] == 25
    assert refreshed["last_heartbeat"] > 1


def test_http_app_runs_failure_recovery_loop(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    client = TestClient(create_app(registry))
    task = TaskTransfer(
        task_id="task-recover",
        trace_id="trace-recover",
        source_agent_id="planner",
        target_agent_id="tester",
        payload=TaskPayload(
            type="write_test",
            summary="Recover HTTP task",
            target_module="mac.transport.http_ws",
            coverage_goal=80,
        ),
        context=ContextBundle(summary="Recover HTTP task"),
    )
    assert client.post("/tasks", json=task.model_dump(mode="json")).status_code == 201
    assert client.post("/tasks/task-recover/accept", json={"agent_id": "tester"}).status_code == 200
    assert client.post("/tasks/task-recover/start", json={"agent_id": "tester"}).status_code == 200

    checkpointed = client.post(
        "/tasks/task-recover/checkpoint",
        json={"agent_id": "tester", "checkpoint": {"summary": "halfway"}},
    ).json()
    assert checkpointed["metadata"]["checkpoints"][0]["summary"] == "halfway"

    failed = client.post(
        "/tasks/task-recover/fail",
        json={"agent_id": "tester", "error_code": "HANDLER_ERROR", "message": "handler crashed"},
    ).json()
    assert failed["status"] == "failed"

    retried = client.post(
        "/tasks/task-recover/retry",
        json={"agent_id": "planner", "fallback_agent_id": "fallback"},
    ).json()
    assert retried["status"] == "proposed"
    assert retried["target_agent_id"] == "fallback"
    assert retried["retry_count"] == 1

    cancelled = client.post(
        "/tasks/task-recover/cancel",
        json={"agent_id": "planner", "reason": "obsolete"},
    ).json()
    assert cancelled["status"] == "cancelled"


def test_http_metrics_returns_aggregate_indicators(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    client = TestClient(create_app(registry))

    # Empty ledger — all metrics should be zero.
    response = client.get("/metrics")
    assert response.status_code == 200
    metrics = response.json()
    assert metrics["active_agents"] == 0
    assert metrics["conflict_rate"] == 0.0
    assert metrics["quality_gate_pass_rate"] == 0.0

    # Register an agent and submit a task to get non-zero samples.
    agent = AgentCard(agent_id="worker", name="Worker", capabilities=[AgentCapability(name="write_code")])
    client.post("/agents/register", json=agent.model_dump(mode="json"))
    task = TaskTransfer(
        task_id="task-1",
        payload=TaskPayload(type="write_code", summary="Metrics test"),
    )
    client.post("/tasks", json=task.model_dump(mode="json"))

    metrics_after = client.get("/metrics").json()
    assert metrics_after["samples"]["task_transfers"] == 1
    assert metrics_after["active_agents"] == 1
