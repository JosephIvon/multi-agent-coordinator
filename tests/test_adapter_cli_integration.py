import sys
from pathlib import Path

from mac.cli import main
from mac.protocol.messages import AgentCapability, AgentCard, TaskPayload, TaskTransfer
from mac.registry import Registry
from mac.storage.sqlite import SQLiteStorage


def test_adapter_run_syncs_successful_cli_result(tmp_path: Path) -> None:
    db = tmp_path / "mac.db"
    registry = Registry(SQLiteStorage(db))
    registry.register(AgentCard(agent_id="worker", name="Worker", capabilities=[AgentCapability(name="write_code")]))
    registry.submit_task(TaskTransfer(task_id="t1", source_agent_id="planner", target_agent_id="worker", payload=TaskPayload(type="write_code", summary="Do work")))
    registry.accept_handoff("t1", "worker")
    registry.start_task("t1", "worker")
    rc = main(["adapter", "run", "generic-cli", "--db", str(db), "--task-id", "t1", "--agent-id", "worker", "--output-dir", str(tmp_path / "ctx"), "--command", sys.executable, "-c", "print('ok')"])
    assert rc == 0
    assert registry.get_task("t1").status == "completed"
