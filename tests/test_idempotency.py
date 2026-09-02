"""Regression tests for the retry-safety guarantees.

The bug these protect against was found by the scenario harness, not by the
unit tests: the request-scoped session committed during dependency teardown,
which runs after the response has reached the caller. A fast agent could
therefore issue its next tool call against state the previous call had not yet
committed, and a retried verification would be double-counted.
"""

from __future__ import annotations

from app.models import OnboardingSession, SessionState
from app.services import sessions
from tests.conftest import AUTH


def _make_session(db_session) -> OnboardingSession:
    session = sessions.create_session(
        db_session, external_call_id="dup-test", channel="voice", language="en-IN"
    )
    db_session.commit()
    return session


def test_duplicate_idempotency_key_is_refused_by_the_database(db_session):
    """The unique constraint is the real guarantee, not the pre-flight check."""
    session = _make_session(db_session)
    common = dict(
        session=session,
        tool_name="verify_identity",
        outcome="retry",
        latency_ms=1.0,
        request_digest={},
        response_digest={"outcome": "retry", "agent_message": "again please"},
        idempotency_key="dup-key",
    )

    first = sessions.record_tool_call(db_session, **common)
    assert first is not None

    # Simulates two retries racing past the pre-flight check simultaneously.
    second = sessions.record_tool_call(db_session, **common)
    assert second is None


def test_distinct_keys_are_both_recorded(db_session):
    session = _make_session(db_session)
    base = dict(
        session=session,
        tool_name="verify_identity",
        outcome="retry",
        latency_ms=1.0,
        request_digest={},
        response_digest={"outcome": "retry", "agent_message": "again"},
    )
    assert sessions.record_tool_call(db_session, idempotency_key="a", **base) is not None
    assert sessions.record_tool_call(db_session, idempotency_key="b", **base) is not None


def test_calls_without_a_key_are_never_deduplicated(db_session):
    """Absent a key, every call is a distinct event and must be audited."""
    session = _make_session(db_session)
    base = dict(
        session=session,
        tool_name="escalate",
        outcome="ok",
        latency_ms=1.0,
        request_digest={},
        response_digest={"outcome": "ok", "agent_message": "transferring"},
        idempotency_key=None,
    )
    assert sessions.record_tool_call(db_session, **base) is not None
    assert sessions.record_tool_call(db_session, **base) is not None


def test_replayed_escalation_returns_the_same_ticket(client, session_id):
    """A retried hand-off must not open a second ticket."""
    payload = {
        "session_id": session_id,
        "reason_code": "customer_requested_human",
        "summary": "Caller asked for a person.",
        "idempotency_key": "esc-1",
    }
    first = client.post("/v1/tools/escalate", json=payload, headers=AUTH).json()
    second = client.post("/v1/tools/escalate", json=payload, headers=AUTH).json()
    assert first["data"]["ticket_ref"] == second["data"]["ticket_ref"]


def test_state_transitions_reject_undefined_edges(db_session):
    session = _make_session(db_session)
    sessions.transition(session, SessionState.IDENTITY_MATCHED)
    # identity_matched -> completed is not an edge in the machine.
    try:
        sessions.transition(session, SessionState.COMPLETED)
    except Exception as exc:  # noqa: BLE001 - asserting the type below
        assert exc.__class__.__name__ == "InvalidTransition"
    else:  # pragma: no cover
        raise AssertionError("expected InvalidTransition")
