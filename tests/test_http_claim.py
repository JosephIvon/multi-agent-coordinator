from fastapi.testclient import TestClient

from mac.protocol.messages import AgentCapability, AgentCard, ContextBundle, TaskPayload, TaskTransfer
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger
from mac.transport.http_ws import create_app


def test_http_claim_returns_matching_task_and_records_audit(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    client = TestClient(create_app(registry))

    tester = AgentCard(agent_id="tester", name="Tester", capabilities=[AgentCapability(name="write_test")])
    assert client.post("/agents/register", json=tester.model_dump(mode="json")).status_code == 201

    task = TaskTransfer(
        task_id="task-1",
        trace_id="trace-1",
        source_agent_id="planner",
        payload=TaskPayload(
            type="write_test",
            summary="Write focused HTTP claim tests",
            target_module="mac.transport.http_ws",
            coverage_goal=85,
        ),
        context=ContextBundle(summary="HTTP claim route"),
    )
    assert client.post("/tasks", json=task.model_dump(mode="json")).status_code == 201

    response = client.post("/agents/tester/claim", json={"capability": "write_test"})

    assert response.status_code == 200
    claimed = response.json()
    assert claimed["task_id"] == "task-1"
    assert claimed["status"] == "accepted"
    assert claimed["target_agent_id"] == "tester"
    assert claimed["target_agent_id"] == "tester"

    audit = client.get("/ledger/trace-1").json()
    assert [entry["action"] for entry in audit] == ["submit_task", "claim_task"]


def test_http_claim_endpoint_supports_best_effort(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    client = TestClient(create_app(registry))
    code_task = TaskTransfer(
        task_id="task-code",
        trace_id="trace-code",
        source_agent_id="planner",
        payload=TaskPayload(
            type="code_review",
            summary="Review code",
            mcp_uri="file://src/mac/registry.py",
            diff_hunk="diff --git a/src/mac/registry.py b/src/mac/registry.py",
        ),
        context=ContextBundle(summary="Review code"),
    )
    test_task = TaskTransfer(
        task_id="task-test",
        trace_id="trace-test",
        source_agent_id="planner",
        payload=TaskPayload(
            type="write_test",
            summary="Write tests",
            target_module="mac.registry",
            coverage_goal=80,
        ),
        context=ContextBundle(summary="Write tests"),
    )
    registry.submit_task(code_task)
    registry.submit_task(test_task)
    registry.record_task_outcome(
        agent_id="polyglot",
        capability="code_review",
        task_type="code_review",
        status="failed",
        duration_seconds=5,
        error_code="QUALITY_GATE_FAILED",
    )
    registry.record_task_outcome(
        agent_id="polyglot",
        capability="write_test",
        task_type="write_test",
        status="succeeded",
        duration_seconds=5,
    )

    response = client.post(
        "/agents/polyglot/claim",
        json={"capability": "code_review", "best_effort": True},
    )

    assert response.status_code == 200
    assert response.json()["task_id"] == "task-test"
