"""IDE-agnostic adapter contracts for extending MAC to new coding tools."""
from __future__ import annotations

from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class AdapterManifest:
    adapter_id: str
    name: str
    version: str = "1.0"
    capabilities: frozenset[str] = frozenset({"context_file"})
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"adapter_id": self.adapter_id, "name": self.name, "version": self.version, "capabilities": sorted(self.capabilities), "metadata": self.metadata}

@dataclass(frozen=True)
class PreparedContext:
    task_id: str
    content: str
    path: Path | None = None

class AgentAdapter(Protocol):
    manifest: AdapterManifest
    def prepare_context(self, *, task_id: str, context: str, output_dir: Path) -> PreparedContext: ...

def discover_adapters() -> dict[str, AgentAdapter]:
    from mac.adapters.generic import GenericCliAdapter, GenericContextAdapter, GenericMcpAdapter
    discovered: dict[str, AgentAdapter] = {
        "generic-context": GenericContextAdapter(),
        "generic-cli": GenericCliAdapter(),
        "generic-mcp": GenericMcpAdapter(),
    }
    selected = entry_points(group="mac.adapters")
    for entry_point in selected:
        adapter = entry_point.load()()
        adapter_id = adapter.manifest.adapter_id
        if adapter_id in discovered:
            raise ValueError(f"duplicate MAC adapter id: {adapter_id}")
        discovered[adapter_id] = adapter
    return discovered

def list_adapters() -> list[AdapterManifest]:
    return [adapter.manifest for adapter in discover_adapters().values()]
