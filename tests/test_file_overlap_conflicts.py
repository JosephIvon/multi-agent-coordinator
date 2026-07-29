import pytest
from mac.protocol.messages import AgentCapability, AgentCard, ContextBundle, TaskPayload, TaskTransfer, HandoffResult
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger
from mac.testing.contracts import TestContract


def _task(task_id):
    return TaskTransfer(
        task_id=task_id,
        trace_id='trace-' + task_id,
        source_agent_id='planner',
        payload=TaskPayload(type='custom', summary='work on ' + task_id),
        context=ContextBundle(summary='context ' + task_id),
        test_contract=TestContract.for_risk('low'),
    )


def _handoff(task_id, agent_id, files):
    return HandoffResult(
        task_id=task_id,
        agent_id=agent_id,
        changed_files=list(files or []),
    )


def _run_task(registry, task_id, agent_id, files):
    registry.register(AgentCard(agent_id=agent_id, name=agent_id, capabilities=[AgentCapability(name='write_test')]))
    registry.submit_task(_task(task_id))
    registry.accept_handoff(task_id, agent_id)
    registry.start_task(task_id, agent_id)
    registry.submit_quality_result(task_id, {'command': 'pytest related tests or smoke test', 'status': 'passed', 'evidence': ['test_output']})
    if files:
        registry.save_handoff_result(_handoff(task_id, agent_id, files))
    registry.complete_task(task_id, agent_id)


def test_no_overlap_no_conflicts(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / 'mac.db'))
    _run_task(registry, 'task-a', 'a', ['src/a.py'])
    _run_task(registry, 'task-b', 'b', ['src/b.py'])
    created = registry.detect_file_overlap_conflicts('task-a')
    assert created == []
    assert registry.list_conflicts(resolved=False) == []


def test_overlap_creates_conflict(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / 'mac.db'))
    _run_task(registry, 'task-a', 'a', ['src/shared.py', 'src/a.py'])
    _run_task(registry, 'task-b', 'b', ['src/shared.py', 'src/b.py'])
    created = registry.detect_file_overlap_conflicts('task-a')
    assert len(created) == 1
    c = created[0]
    assert c.source == 'file_overlap'
    assert c.severity == 'non_blocking'
    assert 'src/shared.py' in c.involved_files
    assert set(c.involved_agents) == {'a', 'b'}
    assert c.task_id == 'task-a'
    assert c.conflict_id.startswith('overlap:')


def test_multiple_paths_creates_multiple_conflicts(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / 'mac.db'))
    _run_task(registry, 'task-a', 'a', ['x.py', 'y.py'])
    _run_task(registry, 'task-b', 'b', ['x.py', 'y.py', 'z.py'])
    created = registry.detect_file_overlap_conflicts('task-a')
    assert {c.involved_files[0] for c in created} == {'x.py', 'y.py'}


def test_idempotent_detection(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / 'mac.db'))
    _run_task(registry, 'task-a', 'a', ['shared.py'])
    _run_task(registry, 'task-b', 'b', ['shared.py'])
    first = registry.detect_file_overlap_conflicts('task-a')
    second = registry.detect_file_overlap_conflicts('task-a')
    assert len(first) == 1
    assert second == []
    assert len(registry.list_conflicts(resolved=False)) == 1


def test_done_with_detect_conflicts_attaches_to_result(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / 'mac.db'))
    _run_task(registry, 'task-a', 'a', ['shared.py'])
    _run_task(registry, 'task-b', 'b', ['shared.py'])
    registry.register(AgentCard(agent_id='c', name='c', capabilities=[AgentCapability(name='write_test')]))
    registry.submit_task(_task('task-c'))
    registry.accept_handoff('task-c', 'c')
    registry.start_task('task-c', 'c')
    registry.submit_quality_result('task-c', {'command': 'pytest related tests or smoke test', 'status': 'passed', 'evidence': ['test_output']})
    handoff = _handoff('task-c', 'c', ['shared.py'])
    result = registry.done('task-c', 'c', handoff=handoff, detect_conflicts=True)
    assert result['status'] == 'completed'
    assert 'conflicts' in result
    assert len(result['conflicts']) == 2
    assert result['conflicts'][0]['involved_files'] == ['shared.py']


def test_in_progress_other_task_excluded(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / 'mac.db'))
    _run_task(registry, 'task-a', 'a', ['shared.py'])
    registry.register(AgentCard(agent_id='b', name='b', capabilities=[AgentCapability(name='write_test')]))
    registry.submit_task(_task('task-b'))
    registry.save_handoff_result(_handoff('task-b', 'b', ['shared.py']))
    created = registry.detect_file_overlap_conflicts('task-a')
    assert created == []


def test_failed_other_task_excluded(tmp_path):
    registry = Registry(SQLiteTaskLedger(tmp_path / 'mac.db'))
    _run_task(registry, 'task-a', 'a', ['shared.py'])
    registry.register(AgentCard(agent_id='b', name='b', capabilities=[AgentCapability(name='write_test')]))
    registry.submit_task(_task('task-b'))
    registry.accept_handoff('task-b', 'b')
    registry.start_task('task-b', 'b')
    registry.save_handoff_result(_handoff('task-b', 'b', ['shared.py']))
    registry.fail_task('task-b', 'b', error_code='x', message='m')
    created = registry.detect_file_overlap_conflicts('task-a')
    assert created == []