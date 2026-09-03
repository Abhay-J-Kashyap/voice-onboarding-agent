"""Prospective customer capture.

A lead is what a voice call can legitimately produce for someone with no
existing account. It is not an application and not an account: nothing in it has
been verified, so it exists to be followed up through a channel that can
actually complete KYC.

The separation from `customers` is deliberate and load-bearing. If a lead were
written into the customer table, the next call would find the record, treat
self-asserted details as an established one, and issue a passcode to an address
the caller chose — turning "I typed my own email" into "the registered contact
confirmed it". Keeping the tables apart makes that mistake impossible rather
than merely discouraged.
"""

from __future__ import annotations

import secrets

from sqlalchemy.orm import Session

from app.models import Lead, OnboardingSession


def generate_reference() -> str:
    return f"LEAD-{secrets.token_hex(4).upper()}"


def create_lead(
    db: Session,
    *,
    session: OnboardingSession,
    full_name: str,
    date_of_birth: str,
    pan: str,
    email: str,
    phone: str | None,
    product_interest: str,
    stated_monthly_income: int | None,
) -> Lead:
    lead = Lead(
        reference=generate_reference(),
        session_id=session.id,
        full_name=full_name,
        date_of_birth=date_of_birth,
        pan=pan,
        email=email,
        phone=phone,
        product_interest=product_interest,
        stated_monthly_income=stated_monthly_income,
    )
    db.add(lead)
    db.flush()
    return lead
