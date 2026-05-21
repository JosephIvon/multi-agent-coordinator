from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import PurePosixPath

from mac.testing.contracts import RiskLevel, TestContract


_HIGH_RISK_SIGNALS = (
    "auth/",
    "/auth/",
    "security/",
    "/security/",
    "payment/",
    "/payment/",
    "migrations/",
    "migration/",
    "/migrations/",
    "/migration/",
    "schema/",
    "/schema/",
    "_auth.",
    ".auth.",
    "auth.py",
    "auth_",
    "_security.",
    ".security.",
    "security.py",
    "security_",
    "_payment.",
    ".payment.",
    "payment.py",
    "payment_",
)


def plan_test_contract(
    changed_files: Iterable[str],
    risk_hint: RiskLevel | None = None,
) -> TestContract:
    if risk_hint is not None:
        return TestContract.for_risk(risk_hint)

    paths = [_normalize_path(path) for path in changed_files]

    if any(_has_high_risk_signal(path) for path in paths):
        return TestContract.for_risk("high")

    if not paths or all(_is_docs_file(path) or _is_test_file(path) for path in paths):
        return TestContract.for_risk("low")

    return TestContract.for_risk("medium")


def _normalize_path(path: str) -> str:
    normalized = path.replace("\\", "/").lower()
    if sys.platform == "win32" and len(normalized) > 1 and normalized[1] == ":":
        return normalized[2:] if normalized[2:] else normalized
    return normalized


def _has_high_risk_signal(path: str) -> bool:
    for signal in _HIGH_RISK_SIGNALS:
        if signal in path:
            return True
    return False


def _is_docs_file(path: str) -> bool:
    return (
        path.startswith("docs/")
        or path in {"readme.md", "changelog.md"}
        or path.endswith((".md", ".rst", ".txt"))
    )


def _is_test_file(path: str) -> bool:
    parts = PurePosixPath(path).parts
    filename = parts[-1] if parts else path
    return "tests" in parts or filename.startswith("test_") or filename.endswith("_test.py")