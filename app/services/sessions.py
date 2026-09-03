"""Session lifecycle: the guard rail every tool call passes through.

The design assumption here is that the language model will, eventually, do the
wrong thing — call a tool twice, skip a step, or try to continue a call that has
already been handed to a human. None of those should corrupt the record, so the
ordering rules live in this module rather than in the prompt.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import InvalidTransition, SessionClosed, SessionNotFound
from app.models import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    OnboardingSession,
    SessionState,
    ToolCall,
)
from app.observability import TRACE_ID, log_event


def create_session(
    db: Session,
    *,
    external_call_id: str | None,
    channel: str,
    language: str,
) -> OnboardingSession:
    session = OnboardingSession(
        id=uuid.uuid4().hex,
        external_call_id=external_call_id,
        channel=channel,
        language=language,
        state=SessionState.STARTED,
    )
    db.add(session)
    db.flush()
    log_event(
        "session_started",
        session_id=session.id,
        external_call_id=external_call_id,
        channel=channel,
        language=language,
    )
    return session


def load_session(db: Session, session_id: str) -> OnboardingSession:
    session = db.get(OnboardingSession, session_id)
    if session is None:
        raise SessionNotFound(
            "I could not find this application. Let me transfer you to a colleague.",
            session_id=session_id,
        )
    return session


def require_state(
    session: OnboardingSession, *expected: SessionState, tool: str
) -> None:
    """Reject a tool call that arrives out of order.

    Terminal sessions are refused outright: once a call has been escalated or
    blocked, replaying tools against it would rewrite an audited outcome.
    """
    if session.state in TERMINAL_STATES:
        raise SessionClosed(
            "This application has already been closed and passed to a colleague.",
            tool=tool,
            state=session.state.value,
        )
    if session.state not in expected:
        raise InvalidTransition(
            "I still need a couple of details before we can continue.",
            tool=tool,
            state=session.state.value,
            expected=[state.value for state in expected],
        )


def require_live(session: OnboardingSession, *, tool: str) -> None:
    """Reject only if the session has already closed.

    Used by escalation, which must work from *every* live state. Enumerating the
    permitted states there is a latent bug: adding a state to the machine
    silently removes the hand-off from it, and the failure only shows up when a
    caller in that state asks for a human and does not get one.
    """
    if session.state in TERMINAL_STATES:
        raise SessionClosed(
            "This application has already been closed and passed to a colleague.",
            tool=tool,
            state=session.state.value,
        )


def transition(session: OnboardingSession, target: SessionState) -> None:
    """Move the session forward, refusing edges the state machine does not define."""
    if target not in ALLOWED_TRANSITIONS[session.state]:
        raise InvalidTransition(
            "I cannot complete that step right now.",
            current=session.state.value,
            target=target.value,
        )
    previous = session.state
    session.state = target
    log_event(
        "session_transition",
        session_id=session.id,
        from_state=previous.value,
        to_state=target.value,
    )


def find_replayed_call(
    db: Session, session_id: str, tool_name: str, idempotency_key: str | None
) -> ToolCall | None:
    """Return a prior identical call, if the platform is retrying one.

    Telephony platforms retry on timeout. Without this, a retried verification
    would consume a second attempt and could block a caller who did nothing
    wrong.
    """
    if not idempotency_key:
        return None
    stmt = select(ToolCall).where(
        ToolCall.session_id == session_id,
        ToolCall.tool_name == tool_name,
        ToolCall.idempotency_key == idempotency_key,
    )
    return db.execute(stmt).scalar_one_or_none()


def record_tool_call(
    db: Session,
    *,
    session: OnboardingSession,
    tool_name: str,
    outcome: str,
    latency_ms: float,
    request_digest: dict[str, Any],
    response_digest: dict[str, Any],
    idempotency_key: str | None = None,
) -> ToolCall | None:
    """Append an audit row. Digests must already be redacted by the caller.

    Returns ``None`` when the row collides with an existing idempotency key.
    The pre-flight check in :func:`find_replayed_call` catches the ordinary
    case, but two retries arriving concurrently can both pass it; the unique
    constraint is what actually guarantees the invariant. The savepoint keeps
    that collision from poisoning the surrounding transaction.
    """
    call = ToolCall(
        session_id=session.id,
        trace_id=TRACE_ID.get(),
        tool_name=tool_name,
        idempotency_key=idempotency_key,
        outcome=outcome,
        latency_ms=latency_ms,
        request_digest=request_digest,
        response_digest=response_digest,
    )
    try:
        with db.begin_nested():
            db.add(call)
            db.flush()
    except IntegrityError:
        log_event(
            "tool_call_duplicate_suppressed",
            session_id=session.id,
            tool=tool_name,
            idempotency_key=idempotency_key,
        )
        return None

    log_event(
        "tool_call",
        session_id=session.id,
        tool=tool_name,
        outcome=outcome,
        latency_ms=latency_ms,
        **request_digest,
    )
    return call
