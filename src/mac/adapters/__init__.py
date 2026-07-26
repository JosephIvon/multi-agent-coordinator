from mac.adapters.generic import GenericCliAdapter, GenericContextAdapter, GenericMcpAdapter
from mac.adapters.http import GenericHttpAdapter, HttpDispatchResult
from mac.adapters.lifecycle import AgentResult, SessionState
from mac.adapters.protocol import AdapterManifest, AgentAdapter, PreparedContext, discover_adapters, list_adapters

__all__ = [
    "AgentAdapter",
    "AgentResult",
    "AdapterManifest",
    "GenericCliAdapter",
    "GenericContextAdapter",
    "GenericMcpAdapter",
    "GenericHttpAdapter",
    "HttpDispatchResult",
    "PreparedContext",
    "SessionState",
    "discover_adapters",
    "list_adapters",
]




