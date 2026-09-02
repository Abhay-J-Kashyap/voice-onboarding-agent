"""One-time passcode issuance and verification.

Every limit in here is enforced by the service. The prompt tells the agent that
passcodes expire and that attempts are finite; this module is what makes those
statements true when the model forgets, miscounts, or is talked into "just one
more try".

Three properties are worth stating explicitly, because they are the difference
between a passcode and a password:

* **The code is never stored.** Only a salted SHA-256 digest is kept, so a
  database disclosure yields no live codes and the audit trail cannot be
  replayed.
* **A challenge is single use.** `consumed_at` is set on first success and
  checked thereafter, so a code overheard on a recorded line cannot be reused.
* **Issuance is rate limited per customer, not per session.** Sessions are free
  to create, so a per-session cap would be no cap at all — an attacker could
  use the agent to flood a stranger's phone.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Customer, OnboardingSession, OtpChallenge
from app.observability import mask_email
from app.services.email import get_email_sender
from app.services.sms import get_sms_sender


def _now() -> datetime:
    return datetime.now(UTC)


def _as_aware(value: datetime) -> datetime:
    """SQLite returns naive datetimes; treat them as UTC for comparison."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def generate_code(length: int) -> str:
    """A numeric passcode with no leading-zero bias."""
    return "".join(str(secrets.randbelow(10)) for _ in range(length))


def hash_code(code: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}{code}".encode()).hexdigest()


class IssueOutcome(StrEnum):
    SENT = "sent"
    RATE_LIMITED = "rate_limited"
    RESENDS_EXHAUSTED = "resends_exhausted"
    DELIVERY_FAILED = "delivery_failed"


class VerifyOutcome(StrEnum):
    OK = "ok"
    WRONG_CODE = "wrong_code"
    EXPIRED = "expired"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    NO_CHALLENGE = "no_challenge"
    ALREADY_USED = "already_used"


@dataclass(frozen=True)
class IssueResult:
    outcome: IssueOutcome
    masked_phone: str | None = None
    masked_email: str | None = None
    #: Populated only when demo mode is enabled. Never reaches `agent_message`.
    demo_code: str | None = None
    retry_after_seconds: int | None = None


@dataclass(frozen=True)
class VerifyResult:
    outcome: VerifyOutcome
    attempts_remaining: int = 0


def _recent_challenge_count(db: Session, customer_id: int, window: int) -> int:
    cutoff = _now() - timedelta(seconds=window)
    rows = db.execute(
        select(OtpChallenge.created_at).where(OtpChallenge.customer_id == customer_id)
    ).scalars().all()
    return sum(1 for created in rows if _as_aware(created) >= cutoff)


def active_challenge(db: Session, session_id: str) -> OtpChallenge | None:
    """The most recent unconsumed challenge for a session, if any."""
    return db.execute(
        select(OtpChallenge)
        .where(
            OtpChallenge.session_id == session_id,
            OtpChallenge.consumed_at.is_(None),
        )
        .order_by(OtpChallenge.id.desc())
    ).scalars().first()


def mask_phone_tail(phone: str) -> str:
    """Last four digits only — enough for the caller to recognise their number."""
    digits = "".join(c for c in phone if c.isdigit())
    return digits[-4:] if len(digits) >= 4 else "****"


def issue_challenge(
    db: Session,
    *,
    session: OnboardingSession,
    customer: Customer,
    is_resend: bool = False,
) -> IssueResult:
    """Create and dispatch a passcode, subject to the resend and rate limits."""
    settings = get_settings()

    if is_resend and session.otp_resends >= settings.otp_max_resends:
        return IssueResult(IssueOutcome.RESENDS_EXHAUSTED)

    if (
        _recent_challenge_count(db, customer.id, settings.otp_rate_window_seconds)
        >= settings.otp_max_per_window
    ):
        return IssueResult(
            IssueOutcome.RATE_LIMITED,
            retry_after_seconds=settings.otp_rate_window_seconds,
        )

    # Any earlier challenge is retired, so only the newest code can succeed.
    previous = active_challenge(db, session.id)
    if previous is not None:
        previous.consumed_at = _now()

    code = generate_code(settings.otp_length)
    salt = secrets.token_hex(8)
    challenge = OtpChallenge(
        session_id=session.id,
        customer_id=customer.id,
        code_salt=salt,
        code_digest=hash_code(code, salt),
        expires_at=_now() + timedelta(seconds=settings.otp_ttl_seconds),
    )
    db.add(challenge)
    db.flush()

    minutes = max(1, settings.otp_ttl_seconds // 60)

    # Which channel actually carries the code. Email needs no DLT registration,
    # so it is the practical default; SMS remains available and fully tested
    # for deployments that have completed the paperwork.
    if settings.otp_delivery_channel == "email":
        if not customer.email:
            return IssueResult(IssueOutcome.DELIVERY_FAILED)
        delivered = get_email_sender().send_passcode(
            email=customer.email, code=code, ttl_minutes=minutes
        )
        if not delivered:
            return IssueResult(IssueOutcome.DELIVERY_FAILED)
        if is_resend:
            session.otp_resends += 1
        return IssueResult(
            IssueOutcome.SENT,
            masked_email=mask_email(customer.email),
            demo_code=code if settings.otp_demo_mode else None,
        )

    delivered = get_sms_sender().send_passcode(
        phone=customer.phone, code=code, ttl_minutes=minutes
    )
    if not delivered:
        return IssueResult(IssueOutcome.DELIVERY_FAILED)

    if is_resend:
        session.otp_resends += 1

    return IssueResult(
        IssueOutcome.SENT,
        masked_phone=mask_phone_tail(customer.phone),
        demo_code=code if settings.otp_demo_mode else None,
    )


def verify_challenge(db: Session, *, session_id: str, code: str) -> VerifyResult:
    """Check a submitted passcode against the active challenge."""
    settings = get_settings()
    challenge = active_challenge(db, session_id)

    if challenge is None:
        return VerifyResult(VerifyOutcome.NO_CHALLENGE)

    if challenge.consumed_at is not None:
        return VerifyResult(VerifyOutcome.ALREADY_USED)

    if _as_aware(challenge.expires_at) < _now():
        return VerifyResult(VerifyOutcome.EXPIRED)

    if challenge.attempts >= settings.otp_max_verify_attempts:
        return VerifyResult(VerifyOutcome.ATTEMPTS_EXHAUSTED)

    challenge.attempts += 1
    submitted = "".join(c for c in code if c.isdigit())

    # Constant-time comparison: a timing side channel on a six-digit code is a
    # small window, but it is free to close.
    if secrets.compare_digest(
        hash_code(submitted, challenge.code_salt), challenge.code_digest
    ):
        challenge.consumed_at = _now()
        return VerifyResult(VerifyOutcome.OK)

    remaining = settings.otp_max_verify_attempts - challenge.attempts
    if remaining <= 0:
        return VerifyResult(VerifyOutcome.ATTEMPTS_EXHAUSTED)
    return VerifyResult(VerifyOutcome.WRONG_CODE, attempts_remaining=remaining)
