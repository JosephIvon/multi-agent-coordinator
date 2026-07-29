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

class BoundaryViolationError(MACError):
    """Raised when a handoff changes files outside the agent allowed paths.

    Carries the list of violating path patterns so the caller can show
    them to the user. Distinct from the soft `boundary_review=block`
    flag stored on a HandoffResult, which only annotates without
    refusing the write. Use this exception for hard enforcement
    (e.g. the Multica webhook bridge opting into strict mode).
    """

    def __init__(self, violations: list[str]):
        self.violations = list(violations)
        super().__init__(
            f"handoff violates agent path boundary: {self.violations}"
        )
