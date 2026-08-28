"""Persistence models.

Two things are deliberately separated here: the *reference data* a real lender
would already hold (customers, products), and the *audit record* the agent
produces (sessions, tool calls, consents, escalations). The audit tables are
append-only by convention so a completed call can always be reconstructed.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class SessionState(enum.StrEnum):
    """States an onboarding call can occupy.

    The agent's prompt describes this flow in prose; this enum enforces it. A
    model that hallucinates its way to `record_consent` before verification will
    be rejected by the service, not merely discouraged by the prompt.
    """

    STARTED = "started"
    IDENTITY_VERIFIED = "identity_verified"
    ELIGIBILITY_ASSESSED = "eligibility_assessed"
    CONSENT_RECORDED = "consent_recorded"
    COMPLETED = "completed"
    ESCALATED = "escalated"
    BLOCKED = "blocked"


#: Allowed forward transitions. Terminal states have no outgoing edges.
ALLOWED_TRANSITIONS: dict[SessionState, set[SessionState]] = {
    SessionState.STARTED: {
        SessionState.IDENTITY_VERIFIED,
        SessionState.ESCALATED,
        SessionState.BLOCKED,
    },
    SessionState.IDENTITY_VERIFIED: {
        SessionState.ELIGIBILITY_ASSESSED,
        SessionState.ESCALATED,
    },
    SessionState.ELIGIBILITY_ASSESSED: {
        SessionState.CONSENT_RECORDED,
        SessionState.ESCALATED,
    },
    SessionState.CONSENT_RECORDED: {
        SessionState.COMPLETED,
        SessionState.ESCALATED,
    },
    SessionState.COMPLETED: set(),
    SessionState.ESCALATED: set(),
    SessionState.BLOCKED: set(),
}

TERMINAL_STATES = {
    SessionState.COMPLETED,
    SessionState.ESCALATED,
    SessionState.BLOCKED,
}


class EligibilityDecision(enum.StrEnum):
    APPROVED = "approved"
    REFERRED = "referred"
    DECLINED = "declined"


class Customer(Base):
    """Reference data standing in for a core banking or CRM record."""

    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    date_of_birth: Mapped[str] = mapped_column(String(10), nullable=False)  # ISO date
    pan: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    monthly_income: Mapped[int] = mapped_column(Integer, nullable=False)
    employment_type: Mapped[str] = mapped_column(String(30), nullable=False)
    existing_emi: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    credit_score: Mapped[int] = mapped_column(Integer, nullable=False)
    is_sanctioned: Mapped[bool] = mapped_column(default=False, nullable=False)


class OnboardingSession(Base):
    """One voice call. Owns the state machine and the attempt counters."""

    __tablename__ = "onboarding_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    external_call_id: Mapped[str | None] = mapped_column(String(64), index=True)
    channel: Mapped[str] = mapped_column(String(20), default="voice", nullable=False)
    language: Mapped[str] = mapped_column(String(10), default="en-IN", nullable=False)
    state: Mapped[SessionState] = mapped_column(
        Enum(SessionState), default=SessionState.STARTED, nullable=False
    )
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    identity_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    customer: Mapped[Customer | None] = relationship()
    tool_calls: Mapped[list[ToolCall]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class ToolCall(Base):
    """Append-only record of every tool invocation, for replay and evaluation."""

    __tablename__ = "tool_calls"
    __table_args__ = (
        # Idempotency: the platform may retry a tool call after a timeout, and a
        # retried verification must not burn a second attempt.
        UniqueConstraint("session_id", "idempotency_key", name="uq_tool_idempotency"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("onboarding_sessions.id"), nullable=False, index=True
    )
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(60), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(30), nullable=False)
    latency_ms: Mapped[float] = mapped_column(nullable=False)
    request_digest: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    response_digest: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    session: Mapped[OnboardingSession] = relationship(back_populates="tool_calls")


class EligibilityAssessment(Base):
    """Decision plus the reasons behind it, pinned to a policy version."""

    __tablename__ = "eligibility_assessments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("onboarding_sessions.id"), nullable=False, index=True
    )
    product_code: Mapped[str] = mapped_column(String(40), nullable=False)
    requested_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    tenure_months: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[EligibilityDecision] = mapped_column(
        Enum(EligibilityDecision), nullable=False
    )
    approved_amount: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    interest_rate: Mapped[float] = mapped_column(default=0.0, nullable=False)
    monthly_instalment: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reasons: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ConsentRecord(Base):
    """Immutable consent evidence. This is the artefact an auditor asks for."""

    __tablename__ = "consent_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("onboarding_sessions.id"), nullable=False, index=True
    )
    consent_type: Mapped[str] = mapped_column(String(40), nullable=False)
    granted: Mapped[bool] = mapped_column(nullable=False)
    disclosure_version: Mapped[str] = mapped_column(String(20), nullable=False)
    verbatim_response: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class Escalation(Base):
    """Hand-off ticket. Carries enough context that a human need not start over."""

    __tablename__ = "escalations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_ref: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("onboarding_sessions.id"), nullable=False, index=True
    )
    reason_code: Mapped[str] = mapped_column(String(40), nullable=False)
    summary: Mapped[str] = mapped_column(String(1000), nullable=False)
    queue: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )