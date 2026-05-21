from __future__ import annotations

import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from mac.protocol.messages import AgentCapability, AgentCard, TaskTransfer
from mac.registry import Registry
from mac.runner.local import LocalAgentRunner, TaskRunResult, command_task_handler


TaskHandler = Callable[[TaskTransfer], TaskRunResult | dict[str, Any]]


@dataclass(frozen=True)
class LocalAgentTemplate:
    """Reusable local adapter definition for creating one-shot runners."""

    agent_id: str
    name: str
    capability: str
    handler: TaskHandler
    version: str = "1.0"
    load: int = 0
    project_context: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def agent_card(self, *, project_context: str | None = None) -> AgentCard:
        effective_project_context = self.project_context if project_context is None else project_context
        return AgentCard(
            agent_id=self.agent_id,
            name=self.name,
            version=self.version,
            capabilities=[AgentCapability(name=self.capability)],
            load=self.load,
            project_context=effective_project_context,
            metadata=dict(self.metadata),
        )

    def create_runner(
        self,
        *,
        registry: Registry,
        project_context: str | None = None,
    ) -> LocalAgentRunner:
        effective_project_context = self.project_context if project_context is None else project_context
        return LocalAgentRunner(
            registry=registry,
            agent=self.agent_card(project_context=effective_project_context),
            capability=self.capability,
            handler=self.handler,
            project_context=effective_project_context,
        )


def command_agent_template(
    *,
    agent_id: str,
    name: str,
    capability: str,
    command: Sequence[str],
    cwd: str | Path | None = None,
    timeout_seconds: float = 60,
    evidence_on_success: list[str] | None = None,
    project_context: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> LocalAgentTemplate:
    return LocalAgentTemplate(
        agent_id=agent_id,
        name=name,
        capability=capability,
        handler=command_task_handler(
            command,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            evidence_on_success=list(evidence_on_success) if evidence_on_success is not None else None,
        ),
        project_context=project_context,
        metadata=dict(metadata or {}),
    )


def pytest_agent_template(
    *,
    agent_id: str = "pytest-runner",
    name: str = "Pytest Runner",
    capability: str = "validate_tests",
    pytest_args: Sequence[str] | None = None,
    python_executable: str = sys.executable,
    cwd: str | Path | None = None,
    timeout_seconds: float = 60,
    evidence_on_success: list[str] | None = None,
    project_context: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> LocalAgentTemplate:
    command = [python_executable, "-m", "pytest", *(pytest_args or ["-q"])]
    return command_agent_template(
        agent_id=agent_id,
        name=name,
        capability=capability,
        command=command,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        evidence_on_success=evidence_on_success or ["test_output"],
        project_context=project_context,
        metadata=metadata,
    )


def runner_from_template(
    template: LocalAgentTemplate,
    *,
    registry: Registry,
    project_context: str | None = None,
) -> LocalAgentRunner:
    return template.create_runner(registry=registry, project_context=project_context)
