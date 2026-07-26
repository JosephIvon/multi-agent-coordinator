"""End-to-end remote callback example using FastAPI's in-process client."""
from __future__ import annotations

from fastapi.testclient import TestClient

from mac.protocol import AgentCard, TaskTransfer
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger
from mac.transport import create_app


def main() -> None:
    ledger = SQLiteTaskLedger("remote-callback-demo.db")
    registry = Registry(ledger)
    registry.register(AgentCard(agent_id="remote", name="Remote Worker"))
    registry.submit_task(TaskTransfer(task_id="demo", target_agent_id="remote"))
    registry.accept_handoff("demo", "remote")
    registry.start_task("demo", "remote")

    client = TestClient(create_app(registry, token="demo-token"))
    headers = {"Authorization": "Bearer demo-token"}
    session = client.post("/sessions", headers=headers, json={
        "agent_id": "remote", "task_id": "demo", "callback_url": "http://localhost/callbacks/result-1"
    }).json()
    payload = {
        "session_id": session["session_id"], "task_id": "demo", "agent_id": "remote",
        "result": {"status": "completed", "summary": "Done", "verification": ["pytest"]},
    }
    print(client.post("/callbacks/result-1", headers=headers, json=payload).json())
    print(client.post("/callbacks/result-1", headers=headers, json=payload).json())  # idempotent replay
    print(client.get("/tasks/demo", headers=headers).json())


if __name__ == "__main__":
    main()
