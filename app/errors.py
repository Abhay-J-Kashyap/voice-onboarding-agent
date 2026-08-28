"""Domain errors.

Every error carries two audiences: a machine-readable `code` for logs and
dashboards, and an `agent_message` that is safe to speak aloud to a caller. The
model is never asked to invent recovery wording for a failure it did not cause.
"""

from __future__ import annotations


class ToolError(Exception):
    """Base class for failures a tool call can return to the agent."""

    status_code: int = 400
    code: str = "tool_error"
    outcome: str = "error"

    def __init__(self, agent_message: str, **details: object) -> None:
        super().__init__(agent_message)
        self.agent_message = agent_message
        self.details = details


class SessionNotFound(ToolError):
    status_code = 404
    code = "session_not_found"
    outcome = "error"


class InvalidTransition(ToolError):
    """A tool was called out of order — for example consent before verification."""

    status_code = 409
    code = "invalid_transition"
    outcome = "rejected"


class SessionClosed(ToolError):
    """The call already reached a terminal state; further tool calls are refused."""

    status_code = 409
    code = "session_closed"
    outcome = "rejected"


class AttemptsExhausted(ToolError):
    status_code = 403
    code = "attempts_exhausted"
    outcome = "blocked"


class PolicyViolation(ToolError):
    status_code = 422
    code = "policy_violation"
    outcome = "rejected"
