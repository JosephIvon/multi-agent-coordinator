from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class AgentCapability(BaseModel):
    """Structured capability claim advertised by an agent."""

    name: str
    proficiency: str = "intermediate"
    frameworks: list[str] = Field(default_factory=list)
    context_window: int | None = None
    max_payload_size: int = 64 * 1024
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentCard(BaseModel):
    """A2A-compatible agent card with MAC scheduling metadata."""

    agent_id: str
    name: str
    version: str = "1.0"
    capabilities: list[AgentCapability] = Field(default_factory=list)
    transport_url: str | None = None
    load: int = Field(default=0, ge=0, le=100)
    status: str = "online"
    last_heartbeat: float = 0
    project_context: str | None = None
    allowed_paths: list[str] = Field(default_factory=list)
    forbidden_paths: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Plan(BaseModel):
    """A lightweight grouping unit for coordinated multi-task work."""

    plan_id: str = Field(default_factory=lambda: str(uuid4()))
    goal: str
    status: Literal["draft", "active", "completed", "cancelled"] = "draft"
    task_ids: list[str] = Field(default_factory=list)
    created_by: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    closed_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class VerificationEntry(BaseModel):
    """A compact evidence item produced during agent handoff."""

    command: str
    result: Literal["pass", "fail"]
    description: str = ""


class HandoffResult(BaseModel):
    """Structured completion handoff produced by a worker agent."""

    task_id: str
    plan_id: str | None = None
    agent_id: str
    verification: list[VerificationEntry] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    docs_touched: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    boundary_review: Literal["pass", "block", "not_required"] = "not_required"
    violated_guardrail: list[str] = Field(default_factory=list)
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ConflictRecord(BaseModel):
    """A collaboration conflict that needs explicit resolution."""

    conflict_id: str = Field(default_factory=lambda: str(uuid4()))
    plan_id: str | None = None
    task_id: str | None = None
    source: str
    severity: Literal["blocking", "non_blocking"] = "non_blocking"
    description: str
    involved_agents: list[str] = Field(default_factory=list)
    involved_files: list[str] = Field(default_factory=list)
    resolved: bool = False
    resolution: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    resolved_at: str | None = None


class PathRule(BaseModel):
    """Project-level path guardrails. Empty/default rules mean no restriction."""

    allow_all: bool = True
    forbidden_patterns: list[str] = Field(default_factory=list)
    allowed_patterns: list[str] = Field(default_factory=list)


class CoordinationPolicy(BaseModel):
    """Feature switches for optional coordination behavior."""

    require_review: bool = False
    require_path_check: bool = False
    reviewer_capability: str | None = None
    path_rule: PathRule = Field(default_factory=PathRule)
    max_retry_count: int = Field(default=3, ge=0)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> CoordinationPolicy:
        """Build a policy from environment variables.

        Recognised variables (all optional):

        - ``MAC_REQUIRE_REVIEW`` / ``MAC_REQUIRE_PATH_CHECK`` — truthy values
          (``1``, ``true``, ``yes``, ``on``) enable the corresponding feature.
        - ``MAC_MAX_RETRY_COUNT`` — non-negative integer override for retry cap.
        - ``MAC_REVIEWER_CAPABILITY`` — capability name required for review actions.
        - ``MAC_PATH_RULES`` — two halves separated by ``|`` (allowed|forbidden),
          each half a comma-separated glob list. Whitespace around segments
          is stripped; empty halves default to their Pydantic defaults.

        An explicit ``env`` mapping is useful for tests; otherwise the
        process environment is consulted.
        """
        source = os.environ if env is None else env

        def _truthy(name: str) -> bool:
            return source.get(name, "").strip().lower() in {"1", "true", "yes", "on"}

        def _int(name: str, default: int) -> int:
            raw = source.get(name)
            if raw is None or raw.strip() == "":
                return default
            try:
                value = int(raw)
            except ValueError as exc:
                raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
            if value < 0:
                raise ValueError(f"{name} must be >= 0, got {value}")
            return value

        path_rule = PathRule()
        raw_rules = source.get("MAC_PATH_RULES")
        if raw_rules is not None and raw_rules.strip():
            allowed_raw, _, forbidden_raw = raw_rules.partition("|")
            allowed_patterns = [item.strip() for item in allowed_raw.split(",") if item.strip()]
            forbidden_patterns = [item.strip() for item in forbidden_raw.split(",") if item.strip()]
            path_rule = PathRule(
                allow_all=not allowed_patterns and not forbidden_patterns,
                allowed_patterns=allowed_patterns,
                forbidden_patterns=forbidden_patterns,
            )

        return cls(
            require_review=_truthy("MAC_REQUIRE_REVIEW"),
            require_path_check=_truthy("MAC_REQUIRE_PATH_CHECK"),
            reviewer_capability=source.get("MAC_REVIEWER_CAPABILITY") or None,
            max_retry_count=_int("MAC_MAX_RETRY_COUNT", cls.model_fields["max_retry_count"].default),
            path_rule=path_rule,
        )


class ContextBundle(BaseModel):
    """Handoff-ready context package for another agent."""

    summary: str
    artifact_refs: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    decision_log: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskPayload(BaseModel):
    """Structured task intent. Large source/log bodies should be referenced, not embedded."""

    model_config = ConfigDict(populate_by_name=True)

    schema_version: str = "1.0"
    type: str = Field(validation_alias=AliasChoices("type", "task_type"))
    summary: str = Field(default="", validation_alias=AliasChoices("summary", "instruction"))
    mcp_uri: str | None = None
    file_path: str | None = None
    requirements: list[str] = Field(default_factory=list)

    diff_hunk: str | None = None
    target_module: str | None = None
    function_signature: str | None = None
    coverage_goal: int | None = Field(default=None, ge=0, le=100)
    risk_level: str | None = None
    target_test_suite: str | None = None
    validation_framework: str | None = None

    artifact_path: str | None = None
    environment: str | None = None
    target_files: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    test_commands: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_task_specific_contract(self) -> TaskPayload:
        if self.type == "code_review":
            missing = []
            if not (self.mcp_uri or self.file_path):
                missing.append("mcp_uri or file_path")
            if not self.summary:
                missing.append("summary")
            if not self.diff_hunk:
                missing.append("diff_hunk")
            if missing:
                raise ValueError("code_review payload requires " + ", ".join(missing))

        if self.type == "write_test":
            spec_shape = self.target_module is not None and self.coverage_goal is not None
            legacy_shape = bool(self.target_files and self.acceptance_criteria)
            if not (spec_shape or legacy_shape):
                raise ValueError(
                    "write_test payload requires target_module and coverage_goal "
                    "or target_files and acceptance_criteria"
                )

        if self.type == "validate_tests":
            spec_shape = self.target_test_suite is not None and self.validation_framework is not None
            legacy_shape = bool(self.test_commands)
            if not (spec_shape or legacy_shape):
                raise ValueError(
                    "validate_tests payload requires target_test_suite and validation_framework "
                    "or test_commands"
                )

        return self


class TaskTransfer(BaseModel):
    """Durable task handoff record tracked by the MAC ledger."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    trace_id: str = Field(default_factory=lambda: str(uuid4()))
    source_agent_id: str | None = None
    target_agent_id: str | None = None
    payload: TaskPayload | None = None
    context: ContextBundle | None = None
    test_contract: Any | None = None
    priority: int = Field(default=5, ge=1, le=10)
    status: str = "proposed"
    plan_id: str | None = None
    depends_on: list[str] = Field(default_factory=list)

    max_hops: int = Field(default=5, ge=1)
    current_hops: int = Field(default=0, ge=0)
    ttl_seconds: int = Field(default=3600, ge=1)

    error_code: str | None = None
    retry_count: int = Field(default=0, ge=0)
    fallback_agent_id: str | None = None

    title: str = ""
    description: str = ""
    project_context: str | None = None

    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def migrate_removed_fields(cls, data: Any) -> Any:
        if isinstance(data, dict) and "hop_count" in data:
            migrated = dict(data)
            if "current_hops" not in migrated:
                migrated["current_hops"] = migrated["hop_count"]
            migrated.pop("hop_count", None)
            return migrated
        return data

    @model_validator(mode="after")
    def fill_default_nested_models(self) -> TaskTransfer:
        if self.payload is None:
            self.payload = TaskPayload(type="custom", summary=self.description or self.title or "custom task")
        if self.context is None:
            self.context = ContextBundle(summary=self.description or self.title or self.payload.summary)
        return self


class AuditEntry(BaseModel):
    """Append-only audit event for a task trace."""

    entry_id: str = Field(default_factory=lambda: str(uuid4()))
    trace_id: str = ""
    task_id: str
    agent_id: str = ""
    actor: str = ""
    action: str
    from_status: str | None = None
    to_status: str | None = None
    message: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None

    @model_validator(mode="after")
    def fill_actor_aliases(self) -> AuditEntry:
        if not self.agent_id and self.actor:
            self.agent_id = self.actor
        if not self.actor and self.agent_id:
            self.actor = self.agent_id
        if not self.created_at:
            self.created_at = self.timestamp.isoformat()
        return self


class TaskEvidenceBundle(BaseModel):
    """Read-only aggregate view of task state, evidence, audit, and observed score."""

    task_id: str
    trace_id: str
    task: TaskTransfer
    quality_results: list[dict[str, Any]] = Field(default_factory=list)
    audit_trail: list[AuditEntry] = Field(default_factory=list)
    execution_agent_id: str | None = None
    required_capability: str | None = None
    observed_capability_score: dict[str, Any] | None = None
    handoff_result: HandoffResult | None = None


class QualityGatePreview(BaseModel):
    """Read-only preview of whether current quality evidence satisfies a task contract."""

    task_id: str
    trace_id: str
    has_contract: bool
    allowed: bool
    reason: str | None = None
    required_commands: list[str] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)
    passed_commands: list[str] = Field(default_factory=list)
    present_evidence: list[str] = Field(default_factory=list)
    missing_commands: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    quality_results_count: int = 0


class TaskReadinessReport(BaseModel):
    """Read-only next-action guidance for the current task state."""

    task_id: str
    trace_id: str
    status: str
    execution_agent_id: str | None = None
    required_capability: str | None = None
    next_action: str
    blocking_reason: str | None = None
    quality_allowed: bool | None = None
    missing_commands: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    quality_results_count: int = 0
    audit_event_count: int = 0
