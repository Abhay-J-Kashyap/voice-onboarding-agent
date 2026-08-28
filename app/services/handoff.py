"""Consent evidence and human hand-off.

Both are append-only. A consent record is the artefact produced if a customer
later disputes the application, and an escalation ticket is the only thing a
human agent has when they pick the call up mid-flow.
"""

from __future__ import annotations

import secrets

from sqlalchemy.orm import Session

from app.models import ConsentRecord, Escalation, OnboardingSession

#: Which queue a reason code routes to. Kept as data so routing can be changed
#: without touching control flow.
QUEUE_ROUTING: dict[str, str] = {
    "identity_verification_failed": "kyc_review",
    "customer_disputes_decision": "underwriting",
    "out_of_scope_request": "general_support",
    "customer_requested_human": "general_support",
    "low_confidence_transcription": "general_support",
    "technical_failure": "engineering_oncall",
}


def record_consent(
    db: Session,
    *,
    session: OnboardingSession,
    consent_type: str,
    granted: bool,
    verbatim_response: str,
    disclosure_version: str,
) -> ConsentRecord:
    record = ConsentRecord(
        session_id=session.id,
        consent_type=consent_type,
        granted=granted,
        disclosure_version=disclosure_version,
        verbatim_response=verbatim_response,
    )
    db.add(record)
    db.flush()
    return record


def generate_ticket_ref() -> str:
    return f"ESC-{secrets.token_hex(4).upper()}"


def create_escalation(
    db: Session, *, session: OnboardingSession, reason_code: str, summary: str
) -> Escalation:
    escalation = Escalation(
        ticket_ref=generate_ticket_ref(),
        session_id=session.id,
        reason_code=reason_code,
        summary=summary,
        queue=QUEUE_ROUTING.get(reason_code, "general_support"),
    )
    db.add(escalation)
    db.flush()
    return escalation
