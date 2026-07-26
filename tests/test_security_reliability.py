from __future__ import annotations

import sys
import threading

from mac.adapters.generic import GenericCliAdapter, PreparedContext
from mac.security import redact


def test_cli_dispatch_timeout_and_cancel(tmp_path):
    context = tmp_path / "context.md"
    context.write_text("x", encoding="utf-8")
    prepared = PreparedContext(task_id="t", content="x", path=context)
    adapter = GenericCliAdapter()
    timeout = adapter.dispatch(prepared, [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.1)
    assert timeout.returncode == 124
    event = threading.Event()
    event.set()
    cancelled = adapter.dispatch(prepared, [sys.executable, "-c", "import time; time.sleep(5)"], cancel_event=event)
    assert cancelled.returncode == 130


def test_sensitive_fields_and_bearer_tokens_are_redacted():
    result = redact({"api_key": "abc", "nested": {"password": "p"}, "text": "Bearer token-value"})
    assert result["api_key"] == "[REDACTED]"
    assert result["nested"]["password"] == "[REDACTED]"
    assert "token-value" not in result["text"]
