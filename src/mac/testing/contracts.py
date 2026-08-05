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
    # C-2 quality gate strengthening (2026-08-05)
    max_diff_lines: int | None = None
    require_changelog: bool = False
    require_acceptance_criteria: bool = False

    @classmethod
    def for_risk(
        cls,
        risk_level: RiskLevel,
        *,
        custom_commands: list[str] | None = None,
        custom_evidence: list[str] | None = None,
        max_diff_lines: int | None = None,
        require_changelog: bool | None = None,
        require_acceptance_criteria: bool | None = None,
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
            max_diff_lines=max_diff_lines if max_diff_lines is not None else _DIFF_LIMITS_BY_RISK[risk],
            require_changelog=require_changelog if require_changelog is not None else _CHANGELOG_BY_RISK[risk],
            require_acceptance_criteria=require_acceptance_criteria
            if require_acceptance_criteria is not None
            else _ACCEPTANCE_CRITERIA_BY_RISK[risk],
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

# C-2: diff line limits by risk level
_DIFF_LIMITS_BY_RISK: dict[str, int | None] = {
    "low": None,       # low risk: no diff limit
    "medium": 500,     # medium risk: 500 lines max
    "high": 300,       # high risk: 300 lines max
}

# C-2: changelog requirement by risk level
_CHANGELOG_BY_RISK: dict[str, bool] = {
    "low": False,
    "medium": True,
    "high": True,
}

# C-2: acceptance criteria verification by risk level
_ACCEPTANCE_CRITERIA_BY_RISK: dict[str, bool] = {
    "low": False,
    "medium": False,
    "high": True,
}
