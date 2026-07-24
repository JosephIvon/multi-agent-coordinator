from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

RiskLevel = Literal["low", "medium", "high"]


class TestContract(BaseModel):
    __test__ = False

    risk_level: RiskLevel
    recommended_commands: list[str] = Field(default_factory=list)
    required_commands: list[str] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)
    allow_manual_override: bool = False

    @classmethod
    def for_risk(
        cls,
        risk_level: RiskLevel,
        *,
        custom_commands: list[str] | None = None,
        custom_evidence: list[str] | None = None,
    ) -> TestContract:
        risk = risk_level

        if custom_commands is not None:
            recommended = list(custom_commands)
            required = list(custom_commands)
        else:
            recommended = list(_COMMANDS_BY_RISK[risk])
            required = list(_REQUIRED_COMMANDS_BY_RISK[risk])

        evidence = list(custom_evidence) if custom_evidence is not None else list(_EVIDENCE_BY_RISK[risk])

        return cls(
            risk_level=risk,
            recommended_commands=recommended,
            required_commands=required,
            required_evidence=evidence,
        )


_COMMANDS_BY_RISK: dict[str, tuple[str, ...]] = {
    "low": (
        "pytest related tests or smoke test",
    ),
    "medium": (
        "pytest",
        "python -m pytest",
        "python -m pytest tests",
    ),
    "high": (
        "pytest",
        "python -m pytest",
        "python -m pytest --cov",
    ),
}

_REQUIRED_COMMANDS_BY_RISK: dict[str, tuple[str, ...]] = {
    "low": ("pytest related tests or smoke test",),
    "medium": ("python -m pytest tests",),
    "high": ("python -m pytest --cov",),
}

_EVIDENCE_BY_RISK: dict[str, tuple[str, ...]] = {
    "low": ("test_output",),
    "medium": (
        "test_output",
        "changed_files",
    ),
    "high": (
        "test_output",
        "coverage_report",
        "review_notes",
    ),
}
