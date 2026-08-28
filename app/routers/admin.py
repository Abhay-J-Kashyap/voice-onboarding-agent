"""Operational endpoints: liveness, readiness, and call replay.

The audit endpoint exists for evaluation as much as for support. After a test
call, it returns the full tool sequence with latencies — which is the raw
material for the scoring harness in `evals/`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import SessionNotFound
from app.models import ConsentRecord, EligibilityAssessment, Escalation
from app.schemas import EligibilityData, SessionAuditView, ToolCallView
from app.security import require_api_key
from app.services.sessions import load_session

router = APIRouter(tags=["operations"])


@router.get("/healthz")
def liveness() -> dict[str, str]:
    """Process is up. Deliberately does not touch the database."""
    return {"status": "ok"}


@router.get("/readyz")
def readiness(db: Session = Depends(get_db)) -> dict[str, str]:
    """Dependencies are reachable. This is what the load balancer should poll."""
    db.execute(text("SELECT 1"))
    return {"status": "ready", "database": "reachable"}


@router.get(
    "/v1/sessions/{session_id}",
    response_model=SessionAuditView,
    dependencies=[Depends(require_api_key)],
)
def get_session_audit(
    session_id: str, db: Session = Depends(get_db)
) -> SessionAuditView:
    """Return the redacted, replayable record of one call."""
    session = load_session(db, session_id)

    assessment = db.execute(
        select(EligibilityAssessment)
        .where(EligibilityAssessment.session_id == session_id)
        .order_by(EligibilityAssessment.id.desc())
    ).scalars().first()

    consents = db.execute(
        select(ConsentRecord).where(ConsentRecord.session_id == session_id)
    ).scalars().all()

    escalation = db.execute(
        select(Escalation).where(Escalation.session_id == session_id)
    ).scalars().first()

    return SessionAuditView(
        session_id=session.id,
        state=session.state,
        language=session.language,
        identity_attempts=session.identity_attempts,
        customer_reference=(
            f"CUST-{session.customer_id:06d}" if session.customer_id else None
        ),
        tool_calls=[ToolCallView.model_validate(c) for c in session.tool_calls],
        eligibility=(
            EligibilityData(
                decision=assessment.decision,
                approved_amount=assessment.approved_amount,
                interest_rate=assessment.interest_rate,
                monthly_instalment=assessment.monthly_instalment,
                reasons=assessment.reasons,
                policy_version=assessment.policy_version,
            )
            if assessment
            else None
        ),
        consents=[
            {
                "consent_type": c.consent_type,
                "granted": c.granted,
                "disclosure_version": c.disclosure_version,
                "recorded_at": c.created_at,
            }
            for c in consents
        ],
        escalation=(
            {
                "ticket_ref": escalation.ticket_ref,
                "reason_code": escalation.reason_code,
                "queue": escalation.queue,
            }
            if escalation
            else None
        ),
    )


__all__ = ["router", "SessionNotFound"]