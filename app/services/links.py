"""Emailed application links.

A voice call can capture a lead. It cannot complete KYC — that needs documents
and a channel that can show them. This module issues the link that carries the
applicant from one to the other, and it is the only thing standing between a
stranger with a URL and someone else's half-finished application.

The token is the credential. There is no password, because asking someone to
invent one mid-application is how applications get abandoned. That trade is paid
for with entropy, expiry, and single use.
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
from app.models import ApplicationLink, Lead, OnboardingSession

#: 32 bytes of randomness. Long enough that guessing is not a strategy, short
#: enough that the URL survives being pasted into a phone browser.
TOKEN_BYTES = 32


def _now() -> datetime:
    return datetime.now(UTC)


def _as_aware(value: datetime) -> datetime:
    """SQLite returns naive datetimes; treat them as UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def generate_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def digest(token: str) -> str:
    """Unsalted SHA-256.

    Salting defends low-entropy secrets against precomputation — which is why
    the six-digit passcode is salted. A 32-byte random token has no such
    weakness, and an unsalted digest is what allows a lookup by token at all.
    """
    return hashlib.sha256(token.encode()).hexdigest()


class LinkStatus(StrEnum):
    VALID = "valid"
    NOT_FOUND = "not_found"
    EXPIRED = "expired"
    ALREADY_USED = "already_used"


@dataclass(frozen=True)
class LinkResolution:
    status: LinkStatus
    link: ApplicationLink | None = None
    lead: Lead | None = None


def issue_link(
    db: Session, *, session: OnboardingSession, lead: Lead
) -> tuple[ApplicationLink, str]:
    """Create a link for this lead and return it with its cleartext token.

    The cleartext is returned once, to be emailed and then forgotten. Nothing
    persists it, so a database disclosure yields no working links.
    """
    settings = get_settings()
    token = generate_token()
    link = ApplicationLink(
        token_digest=digest(token),
        lead_id=lead.id,
        session_id=session.id,
        expires_at=_now() + timedelta(hours=settings.application_link_ttl_hours),
    )
    db.add(link)
    db.flush()
    return link, token


def build_url(token: str) -> str:
    settings = get_settings()
    return f"{settings.public_base_url.rstrip('/')}/apply/{token}"


def resolve(db: Session, token: str) -> LinkResolution:
    """Look up a token and say whether it may still be used.

    Every rejection returns the same shape so the caller can render one page for
    all of them: an applicant does not need to know whether a dead link is
    expired, spent, or never existed, and telling them distinguishes a real
    token from a guessed one.
    """
    link = db.execute(
        select(ApplicationLink).where(ApplicationLink.token_digest == digest(token))
    ).scalar_one_or_none()

    if link is None:
        return LinkResolution(LinkStatus.NOT_FOUND)
    if link.consumed_at is not None:
        return LinkResolution(LinkStatus.ALREADY_USED, link=link)
    if _as_aware(link.expires_at) < _now():
        return LinkResolution(LinkStatus.EXPIRED, link=link)

    lead = db.get(Lead, link.lead_id)
    if lead is None:
        return LinkResolution(LinkStatus.NOT_FOUND, link=link)

    return LinkResolution(LinkStatus.VALID, link=link, lead=lead)


def mark_opened(db: Session, link: ApplicationLink) -> None:
    """Record the first view. Useful for chasing applicants who never opened."""
    if link.opened_at is None:
        link.opened_at = _now()
        db.flush()


def consume(db: Session, link: ApplicationLink) -> None:
    link.consumed_at = _now()
    db.flush()
