from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentCapability:
    name: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentCard:
    agent_id: str
    name: str
    capabilities: list[AgentCapability | dict[str, Any] | str] = field(default_factory=list)
    status: str = "available"
    load: int = 0
    project_context: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.capabilities = [
            capability
            if isinstance(capability, AgentCapability)
            else AgentCapability(**capability)
            if isinstance(capability, dict)
            else AgentCapability(name=str(capability))
            for capability in self.capabilities
        ]


@dataclass
class TaskTransfer:
    task_id: str
    title: str = ""
    description: str = ""
    source_agent_id: str | None = None
    target_agent_id: str | None = None
    status: str = "pending"
    project_context: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AuditEntry:
    entry_id: str
    task_id: str
    actor: str
    action: str
    details: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None
