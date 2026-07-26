from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.request import Request, urlopen

from mac.adapters.lifecycle import AgentResult
from mac.adapters.protocol import AdapterManifest


@dataclass(frozen=True)
class HttpDispatchResult:
    status_code: int
    body: str


class GenericHttpAdapter:
    """Adapter for remote agents exposing a JSON HTTP dispatch endpoint."""

    manifest = AdapterManifest("generic-http", "Generic HTTP Agent", capabilities=frozenset({"http_dispatch", "callback"}))

    def dispatch(self, url: str, payload: dict, *, token: str | None = None, timeout: float = 60) -> HttpDispatchResult:
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(url, data=data, headers=headers, method="POST")
        with urlopen(request, timeout=timeout) as response:
            return HttpDispatchResult(response.status, response.read().decode("utf-8"))

    @staticmethod
    def normalize_result(result: HttpDispatchResult) -> AgentResult:
        from mac.security import redact
        status = "completed" if 200 <= result.status_code < 300 else "failed"
        return AgentResult(status=status, summary=redact(result.body[-4000:]), raw={"status_code": result.status_code})
