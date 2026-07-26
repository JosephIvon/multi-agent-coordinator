from mac.adapters import AgentResult, SessionState


def test_normalized_agent_result_is_json_safe() -> None:
    result = AgentResult(status="completed", changed_files=("src/a.py",), risks=("manual check",))
    assert result.to_dict()["changed_files"] == ["src/a.py"]


def test_session_state_tracks_tool_independent_identity() -> None:
    state = SessionState(agent_id="worker-1", session_id="cursor-1", task_id="t1", status="running")
    assert state.to_dict()["agent_id"] == "worker-1"
