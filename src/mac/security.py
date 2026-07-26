"""Security helpers shared by HTTP and adapter boundaries."""
from __future__ import annotations

import hmac
import re
from typing import Any

_SENSITIVE = re.compile(r"(token|secret|password|passwd|api[_-]?key|authorization|cookie)", re.I)
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+\-/=]+")


def token_matches(expected: str, supplied: str | None) -> bool:
    if not expected or not supplied:
        return False
    prefix = "Bearer "
    if not supplied.startswith(prefix):
        return False
    return hmac.compare_digest(expected.encode(), supplied[len(prefix):].encode())


def redact(value: Any) -> Any:
    """Recursively redact secret-shaped fields and bearer credentials."""
    if isinstance(value, dict):
        return {key: "[REDACTED]" if _SENSITIVE.search(str(key)) else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, str):
        return _BEARER.sub(r"\1[REDACTED]", value)
    return value
