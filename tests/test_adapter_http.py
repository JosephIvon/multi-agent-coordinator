from mac.adapters import GenericHttpAdapter
from mac.adapters.http import HttpDispatchResult


def test_http_result_normalization() -> None:
    result = GenericHttpAdapter.normalize_result(HttpDispatchResult(202, '{"status":"accepted"}'))
    assert result.status == "completed"


def test_http_error_normalization() -> None:
    result = GenericHttpAdapter.normalize_result(HttpDispatchResult(500, "error"))
    assert result.status == "failed"
