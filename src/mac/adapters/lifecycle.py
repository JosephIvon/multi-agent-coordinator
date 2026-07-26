from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class AgentResult:
    """Normalized result emitted by any IDE or remote agent adapter."""

    status: str
    summary: str = ""
    changed_files: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    verification: tuple[str, ...] = ()
    blocker: str | None = None
    handoff_to: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "changed_files": list(self.changed_files),
            "risks": list(self.risks),
            "verification": list(self.verification),
            "blocker": self.blocker,
            "handoff_to": self.handoff_to,
            "raw": self.raw,
        }


@dataclass(frozen=True)
class SessionState:
    """Durable execution session shared by HTTP, CLI, and IDE adapters."""

    agent_id: str
    session_id: str
    task_id: str | None = None
    status: str = "registered"
    callback_url: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    last_heartbeat: float = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "status": self.status,
            "callback_url": self.callback_url,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_heartbeat": self.last_heartbeat,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionState:
        return cls(**data)
