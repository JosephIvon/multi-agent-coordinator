class MACError(RuntimeError):
    """Base class for MAC domain errors."""


class StateConflictError(MACError):
    """Raised when a task state transition is not allowed."""


class QualityGateError(MACError):
    """Raised when a task is completed without satisfying its test contract."""


class TaskExpiredError(MACError):
    """Raised when a task has passed its TTL."""


class MaxHopsExceededError(MACError):
    """Raised when a task exceeds its handoff hop limit."""
