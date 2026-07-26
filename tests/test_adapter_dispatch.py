from mac.adapters import GenericCliAdapter
from mac.adapters.generic import CliDispatchResult


def test_cli_result_normalization_is_tool_neutral() -> None:
    result = GenericCliAdapter.normalize_result(CliDispatchResult(0, "done", ""))
    assert result.status == "completed"
    assert result.summary == "done"


def test_cli_failure_is_normalized() -> None:
    result = GenericCliAdapter.normalize_result(CliDispatchResult(1, "", "boom"))
    assert result.status == "failed"
    assert result.raw["stderr"] == "boom"
