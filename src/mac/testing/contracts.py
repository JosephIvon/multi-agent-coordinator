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
    def for_risk(cls, risk_level: RiskLevel) -> TestContract:
        contract = cls(risk_level=risk_level)
        risk = contract.risk_level

        return cls(
            risk_level=risk,
            recommended_commands=list(_COMMANDS_BY_RISK[risk]),
            required_commands=list(_REQUIRED_COMMANDS_BY_RISK[risk]),
            required_evidence=list(_EVIDENCE_BY_RISK[risk]),
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
