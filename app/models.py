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
    #: No record exists for this PAN. The caller is not a failed customer, they
    #: are a prospective one, and the call becomes an acquisition conversation
    #: rather than a servicing one.
    PROSPECT = "prospect"
    #: Details captured for follow-up through a channel that can actually
    #: complete KYC. This is as far as a voice call can legitimately take an
    #: unknown person.
    LEAD_CAPTURED = "lead_captured"
    #: Record located and the caller knows its details. A knowledge factor only:
    #: anyone holding a photocopy of the PAN card gets this far.
    IDENTITY_MATCHED = "identity_matched"
    #: Possession of the registered mobile proven by passcode. This is the state
    #: that actually authorises the application to proceed.
    IDENTITY_VERIFIED = "identity_verified"
    ELIGIBILITY_ASSESSED = "eligibility_assessed"
    CONSENT_RECORDED = "consent_recorded"
    COMPLETED = "completed"
    ESCALATED = "escalated"
    BLOCKED = "blocked"


#: Allowed forward transitions. Terminal states have no outgoing edges.
ALLOWED_TRANSITIONS: dict[SessionState, set[SessionState]] = {
    SessionState.STARTED: {
        SessionState.IDENTITY_MATCHED,
        SessionState.PROSPECT,
        SessionState.ESCALATED,
        SessionState.BLOCKED,
    },
    SessionState.PROSPECT: {
        SessionState.LEAD_CAPTURED,
        SessionState.ESCALATED,
        SessionState.BLOCKED,
    },
    SessionState.LEAD_CAPTURED: set(),
    SessionState.IDENTITY_MATCHED: {
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
    SessionState.LEAD_CAPTURED,
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
    #: Nullable because SMS-only customers are a real segment; the OTP channel
    #: is chosen per deployment, not per customer.
    email: Mapped[str | None] = mapped_column(String(255))
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
    otp_resends: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
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


class OtpChallenge(Base):
    """A one-time passcode issued to a customer's registered mobile.

    The code itself is never stored. Only a salted digest is kept, so a database
    disclosure does not hand an attacker live passcodes, and nothing in the
    audit trail can be replayed. Challenges are single use: `consumed_at` is set
    on the first successful verification and checked on every subsequent one.
    """

    __tablename__ = "otp_challenges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("onboarding_sessions.id"), nullable=False, index=True
    )
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id"), nullable=False, index=True
    )
    code_salt: Mapped[str] = mapped_column(String(32), nullable=False)
    code_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class Lead(Base):
    """A prospective customer captured on a call.

    Deliberately separate from `customers`: a lead has stated their details but
    nobody has verified them. Writing an unverified person into the customer
    table would let the next call treat self-asserted details as an established
    record, which is precisely the confusion this table exists to prevent.
    """

    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reference: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("onboarding_sessions.id"), nullable=False, index=True
    )
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    date_of_birth: Mapped[str] = mapped_column(String(10), nullable=False)
    pan: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(20))
    product_interest: Mapped[str] = mapped_column(String(40), nullable=False)
    #: What the caller said they earn. Unverified by definition — no record
    #: exists to check it against — so it informs follow-up, never a decision.
    stated_monthly_income: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ApplicationLink(Base):
    """A one-time link emailed to a prospect so they can finish on the web.

    The token is hashed before storage, but unlike a passcode it is *not*
    salted. That is deliberate rather than an oversight: a salt defends a
    low-entropy secret against precomputation, which is why the six-digit
    passcode has one. This token is 32 random bytes, so precomputation is not a
    threat, and an unsalted digest is what makes lookup-by-token possible at
    all — with a per-row salt there would be nothing to look up by.

    Possession of the link is the only credential. That is the accepted trade
    for not asking someone to invent a password mid-application, and it is why
    the link expires and why it stops working once used.
    """

    __tablename__ = "application_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_digest: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    lead_id: Mapped[int] = mapped_column(
        ForeignKey("leads.id"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("onboarding_sessions.id"), nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ApplicationSubmission(Base):
    """What the prospect filled in on the web, tied back to the call."""

    __tablename__ = "application_submissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(
        ForeignKey("leads.id"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("onboarding_sessions.id"), nullable=False, index=True
    )
    employment_type: Mapped[str] = mapped_column(String(30), nullable=False)
    monthly_income: Mapped[int] = mapped_column(Integer, nullable=False)
    address_line: Mapped[str] = mapped_column(String(200), nullable=False)
    city: Mapped[str] = mapped_column(String(80), nullable=False)
    pincode: Mapped[str] = mapped_column(String(10), nullable=False)
    credit_check_consent: Mapped[bool] = mapped_column(nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


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
