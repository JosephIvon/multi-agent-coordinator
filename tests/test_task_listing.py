from mac.protocol.messages import ContextBundle, TaskPayload, TaskTransfer
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger


def _task(
    task_id: str,
    task_type: str,
    *,
    status: str = "proposed",
    target_agent_id: str | None = None,
    project_context: str | None = None,
) -> TaskTransfer:
    payload_kwargs = {"type": task_type, "summary": f"{task_type} task"}
    if task_type == "write_test":
        payload_kwargs.update({"target_module": "mac.registry", "coverage_goal": 85})
    if task_type == "code_review":
        payload_kwargs.update({"file_path": "src/mac/registry.py", "diff_hunk": "@@ -1 +1 @@"})
    return TaskTransfer(
        task_id=task_id,
        trace_id=f"trace-{task_id}",
        source_agent_id="planner",
        target_agent_id=target_agent_id,
        payload=TaskPayload(**payload_kwargs),
        context=ContextBundle(summary=f"{task_type} task"),
        status=status,
        project_context=project_context,
    )


def test_list_tasks_filters_by_status_capability_assignment_and_project_context(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task("task-open", "write_test", project_context="project-a"))
    registry.submit_task(_task("task-other-agent", "write_test", target_agent_id="other", project_context="project-a"))
    registry.submit_task(_task("task-review", "code_review", project_context="project-a"))
    registry.submit_task(_task("task-running", "write_test", status="running", project_context="project-a"))
    registry.submit_task(_task("task-other-project", "write_test", project_context="project-b"))

    tasks = registry.list_tasks(
        status="proposed",
        capability="write_test",
        agent_id="tester",
        project_context="project-a",
    )

    assert [task.task_id for task in tasks] == ["task-open"]


def test_list_tasks_includes_unassigned_and_agent_assigned_tasks(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task("task-open", "write_test"))
    registry.submit_task(_task("task-assigned", "write_test", target_agent_id="tester"))
    registry.submit_task(_task("task-other-agent", "write_test", target_agent_id="other"))

    tasks = registry.list_tasks(status="proposed", capability="write_test", agent_id="tester")

    assert [task.task_id for task in tasks] == ["task-open", "task-assigned"]


def test_list_tasks_is_read_only_and_does_not_append_audit_events(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task("task-open", "write_test"))

    original = registry.get_task("task-open")
    assert original is not None
    before = [entry.action for entry in registry.get_audit_trail("trace-task-open")]
    registry.list_tasks(status="proposed", capability="write_test", agent_id="tester")
    after_task = registry.get_task("task-open")
    assert after_task is not None
    after = [entry.action for entry in registry.get_audit_trail("trace-task-open")]

    assert after_task.updated_at == original.updated_at
    assert before == ["submit_task"]
    assert after == before


def test_list_tasks_uses_required_capability_override(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    task = _task("task-custom", "custom")
    task.payload.extra["required_capability"] = "write_test"
    registry.submit_task(task)

    tasks = registry.list_tasks(status="proposed", capability="write_test", agent_id="tester")

    assert [task.task_id for task in tasks] == ["task-custom"]


def test_list_tasks_without_agent_id_includes_all_assignments(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task("task-open", "write_test"))
    registry.submit_task(_task("task-assigned", "write_test", target_agent_id="tester"))
    registry.submit_task(_task("task-other-agent", "write_test", target_agent_id="other"))

    tasks = registry.list_tasks(status="proposed", capability="write_test")

    assert [task.task_id for task in tasks] == ["task-open", "task-assigned", "task-other-agent"]


def test_list_tasks_filters_project_context_when_status_is_omitted(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / "mac.db"))
    registry.submit_task(_task("task-a", "write_test", project_context="project-a"))
    registry.submit_task(_task("task-b", "write_test", project_context="project-b"))
    registry.claim_next_task(agent_id="tester", capability="write_test", project_context="project-a")

    tasks = registry.list_tasks(capability="write_test", project_context="project-a")

    assert [task.task_id for task in tasks] == ["task-a"]
    assert tasks[0].status == "accepted"
