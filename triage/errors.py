"""Exception types. Everything the service raises on purpose derives from TriageError."""
from __future__ import annotations


class TriageError(Exception):
    """Base class for expected, handled failures."""


class BudgetExceeded(TriageError):
    def __init__(self, what: str, limit: float, used: float):
        super().__init__(f"budget exceeded: {what} used {used:g} > limit {limit:g}")
        self.what, self.limit, self.used = what, limit, used


class RunTimeout(TriageError):
    """The per-run wall-clock deadline passed between graph steps."""


class ToolError(TriageError):
    """A tool call was rejected (bad arguments, unknown tool, path outside the sandbox, ...).
    The message is returned to the agent as an observation so it can recover."""
