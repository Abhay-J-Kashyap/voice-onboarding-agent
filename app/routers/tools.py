"""Tool endpoints invoked by the voice agent during a live call.

Every handler follows the same shape:

    guard the state -> check for a retry -> do the work -> record the audit row
    -> return a speakable envelope

Handlers stay thin; the rules live in `app.services`. That separation is what
makes the policy testable without standing up HTTP.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.errors import AttemptsExhausted
from app.models import EligibilityAssessment, SessionState
from app.observability import SESSION_ID, TRACE_ID, log_event, mask_pan
from app.schemas import (
    CaptureLeadRequest,
    CheckEligibilityRequest,
    EscalateRequest,
    RecordConsentRequest,
    ResendOtpRequest,
    StartSessionRequest,
    StartSessionResponse,
    ToolResponse,
    VerifyIdentityRequest,
    VerifyOtpRequest,
)
from app.security import require_api_key
from app.services import eligibility as eligibility_service
from app.services import handoff, kyc, leads, links, sessions
from app.services import otp as otp_service
from app.services.email import get_email_sender

router = APIRouter(
    prefix="/v1",
    tags=["tools"],
    dependencies=[Depends(require_api_key)],
)


def _passcode_response(
    issued, session, *, first_issue: bool
) -> ToolResponse:
    """Turn a passcode issuance result into a speakable envelope.

    The code itself is placed in `data.demo_otp` only when demo mode is on, and
    never in `agent_message` — the agent's response template reads the message,
    so the model has no way to say the passcode out loud even by accident.
    """
    if issued.outcome is otp_service.IssueOutcome.SENT:
        opener = "Thank you, I have found your record." if first_issue else "Done."
        if issued.masked_email is not None:
            # Reading a masked email aloud is nonsensical for a voice call, so
            # the channel is named but the address is not spoken.
            message = (
                f"{opener} I have sent a passcode to your registered email. "
                "Could you read it back to me?"
            )
            data: dict = {"email_masked": issued.masked_email}
        else:
            message = (
                f"{opener} I have sent a passcode to your registered mobile "
                f"ending {issued.masked_phone}. Could you read it back to me?"
            )
            data = {"phone_last4": issued.masked_phone}
        if issued.demo_code is not None:
            data["demo_otp"] = issued.demo_code
        return ToolResponse(
            outcome="otp_sent",
            agent_message=message,
            session_state=session.state,
            data=data,
        )

    if issued.outcome is otp_service.IssueOutcome.RESENDS_EXHAUSTED:
        return ToolResponse(
            outcome="blocked",
            agent_message=(
                "I have sent as many codes as I can on this call. "
                "Let me pass you to a colleague."
            ),
            session_state=session.state,
            data={"reason": "resends_exhausted"},
        )

    if issued.outcome is otp_service.IssueOutcome.RATE_LIMITED:
        return ToolResponse(
            outcome="blocked",
            agent_message=(
                "Too many codes have been requested for this number recently. "
                "Let me pass you to a colleague."
            ),
            session_state=session.state,
            data={"reason": "rate_limited"},
        )

    return ToolResponse(
        outcome="error",
        agent_message=(
            "I could not send the passcode just now. "
            "Let me pass you to a colleague."
        ),
        session_state=session.state,
        data={"reason": "delivery_failed"},
    )


def _rupees(amount: int) -> str:
    """Format an amount the way a voice model should read it back."""
    return f"{amount:,}".replace(",", ",")


def _finalize(
    db: Session,
    *,
    session,
    tool_name: str,
    response: ToolResponse,
    started: float,
    request_digest: dict,
    idempotency_key: str | None,
) -> ToolResponse:
    """Persist the audit row, commit, and return the response.

    The commit happens here rather than in the request-scoped dependency for a
    specific reason: dependency teardown runs after the response has already
    reached the caller, so a fast agent can issue its next tool call against a
    state this one has not yet committed. Committing before returning makes the
    result durable by the time the agent hears it.
    """
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    recorded = sessions.record_tool_call(
        db,
        session=session,
        tool_name=tool_name,
        outcome=response.outcome,
        latency_ms=latency_ms,
        request_digest=request_digest,
        response_digest=response.model_dump(exclude={"trace_id"}, mode="json"),
        idempotency_key=idempotency_key,
    )

    if recorded is None:
        # A concurrent duplicate won the race. Discard this attempt entirely —
        # including any counter it incremented — and serve the durable result.
        session_id = session.id
        db.rollback()
        prior = sessions.find_replayed_call(
            db, session_id, tool_name, idempotency_key
        )
        if prior is not None:
            return ToolResponse(**prior.response_digest, trace_id=TRACE_ID.get())

    db.commit()
    response.trace_id = TRACE_ID.get()
    return response


@router.post("/sessions", response_model=StartSessionResponse, status_code=201)
def start_session(
    payload: StartSessionRequest, db: Session = Depends(get_db)
) -> StartSessionResponse:
    """Open a call. Called once, at the start of the conversation."""
    session = sessions.create_session(
        db,
        external_call_id=payload.external_call_id,
        channel=payload.channel,
        language=payload.language,
    )
    SESSION_ID.set(session.id)
    # Commit before responding: the agent will call a tool against this session
    # immediately, and must not race the write that created it.
    db.commit()
    return StartSessionResponse(
        session_id=session.id,
        state=session.state,
        agent_message=(
            "Thanks for calling. I can help you start your application today. "
            "First, may I take a few details to verify who you are?"
        ),
    )


@router.post("/tools/verify_identity", response_model=ToolResponse)
def verify_identity(
    payload: VerifyIdentityRequest, db: Session = Depends(get_db)
) -> ToolResponse:
    """Verify the caller against the customer record.

    The attempt limit is enforced here rather than in the prompt. A model that
    loses count, or is talked into 'one more try', still cannot exceed it.
    """
    started = time.perf_counter()
    settings = get_settings()
    session = sessions.load_session(db, payload.session_id)
    SESSION_ID.set(session.id)
    # The replay check precedes the state guard deliberately. A tool call that
    # succeeded and then timed out will be retried against a session it has
    # already advanced; treating that as an ordering violation would break the
    # exact case idempotency exists to handle.
    replay = sessions.find_replayed_call(
        db, session.id, "verify_identity", payload.idempotency_key
    )
    if replay is not None:
        log_event("tool_call_replayed", tool="verify_identity", session_id=session.id)
        return ToolResponse(**replay.response_digest, trace_id=TRACE_ID.get())

    sessions.require_state(session, SessionState.STARTED, tool="verify_identity")

    if session.identity_attempts >= settings.max_identity_attempts:
        raise AttemptsExhausted(
            "I have not been able to verify those details. "
            "Let me pass you to a colleague who can help.",
            session_id=session.id,
        )

    result = kyc.verify_identity(
        db,
        full_name=payload.full_name,
        date_of_birth=payload.date_of_birth,
        pan=payload.pan,
    )

    if result.matched and result.customer is not None:
        session.customer_id = result.customer.id
        sessions.transition(session, SessionState.IDENTITY_MATCHED)
        # Matching the record is a knowledge factor only. The caller is not
        # verified until they prove possession of the registered mobile.
        issued = otp_service.issue_challenge(
            db, session=session, customer=result.customer
        )
        response = _passcode_response(issued, session, first_issue=True)
        if issued.outcome is otp_service.IssueOutcome.SENT:
            response.data["customer_reference"] = f"CUST-{result.customer.id:06d}"
    elif result.failure_reason == "pan_not_found":
        # Not a failed verification: there is simply no account here. This does
        # not consume an attempt, because the caller has done nothing wrong and
        # blocking a prospective customer for "failing" three times would be
        # both hostile and commercially absurd.
        sessions.transition(session, SessionState.PROSPECT)
        response = ToolResponse(
            outcome="not_registered",
            agent_message=(
                "I could not find an existing account with those details. "
                "I can take a few details and get an application started for "
                "you, if you would like."
            ),
            session_state=session.state,
            data={"registered": False},
        )
    else:
        session.identity_attempts += 1
        remaining = settings.max_identity_attempts - session.identity_attempts
        if result.failure_reason == "sanctions_hit" or remaining <= 0:
            # A sanctions hit is never explained to the caller, and never retried.
            response = ToolResponse(
                outcome="blocked",
                agent_message=(
                    "I am not able to verify these details over the phone. "
                    "Let me connect you to a colleague."
                ),
                session_state=session.state,
                data={"retries_remaining": 0},
            )
        else:
            response = ToolResponse(
                outcome="retry",
                agent_message=(
                    "That does not match our records. "
                    "Could you repeat your PAN and date of birth for me?"
                ),
                session_state=session.state,
                data={"retries_remaining": remaining},
            )

    return _finalize(
        db,
        session=session,
        tool_name="verify_identity",
        response=response,
        started=started,
        # Only the redacted PAN and the failure reason are persisted.
        request_digest={
            "pan": mask_pan(payload.pan),
            "failure_reason": result.failure_reason,
            "attempt": session.identity_attempts,
        },
        idempotency_key=payload.idempotency_key,
    )


@router.post("/tools/verify_otp", response_model=ToolResponse)
def verify_otp(
    payload: VerifyOtpRequest, db: Session = Depends(get_db)
) -> ToolResponse:
    """Check the passcode the caller read back.

    This is the step that actually authorises the application. Everything before
    it establishes only that the caller knows details printed on a PAN card.
    """
    started = time.perf_counter()
    session = sessions.load_session(db, payload.session_id)
    SESSION_ID.set(session.id)

    replay = sessions.find_replayed_call(
        db, session.id, "verify_otp", payload.idempotency_key
    )
    if replay is not None:
        log_event("tool_call_replayed", tool="verify_otp", session_id=session.id)
        return ToolResponse(**replay.response_digest, trace_id=TRACE_ID.get())

    sessions.require_state(session, SessionState.IDENTITY_MATCHED, tool="verify_otp")

    result = otp_service.verify_challenge(db, session_id=session.id, code=payload.code)

    if result.outcome is otp_service.VerifyOutcome.OK:
        sessions.transition(session, SessionState.IDENTITY_VERIFIED)
        response = ToolResponse(
            outcome="ok",
            agent_message="Thank you, your identity is verified.",
            session_state=session.state,
            data={"verified": True},
        )
    elif result.outcome is otp_service.VerifyOutcome.WRONG_CODE:
        response = ToolResponse(
            outcome="retry",
            agent_message=(
                "That code does not match. Could you read it out once more?"
            ),
            session_state=session.state,
            data={"attempts_remaining": result.attempts_remaining},
        )
    elif result.outcome is otp_service.VerifyOutcome.EXPIRED:
        response = ToolResponse(
            outcome="retry",
            agent_message=(
                "That code has expired. I can send you a new one if you like."
            ),
            session_state=session.state,
            data={"reason": "expired"},
        )
    else:
        # Exhausted attempts, a reused code, or no challenge at all. None of
        # these are recoverable inside the call.
        response = ToolResponse(
            outcome="blocked",
            agent_message=(
                "I have not been able to verify that code. "
                "Let me pass you to a colleague."
            ),
            session_state=session.state,
            data={"reason": result.outcome.value},
        )

    return _finalize(
        db,
        session=session,
        tool_name="verify_otp",
        response=response,
        started=started,
        # The submitted code is never persisted, only the verdict.
        request_digest={"result": result.outcome.value},
        idempotency_key=payload.idempotency_key,
    )


@router.post("/tools/resend_otp", response_model=ToolResponse)
def resend_otp(
    payload: ResendOtpRequest, db: Session = Depends(get_db)
) -> ToolResponse:
    """Issue a fresh passcode, retiring any earlier one for this session."""
    started = time.perf_counter()
    session = sessions.load_session(db, payload.session_id)
    SESSION_ID.set(session.id)

    replay = sessions.find_replayed_call(
        db, session.id, "resend_otp", payload.idempotency_key
    )
    if replay is not None:
        log_event("tool_call_replayed", tool="resend_otp", session_id=session.id)
        return ToolResponse(**replay.response_digest, trace_id=TRACE_ID.get())

    sessions.require_state(session, SessionState.IDENTITY_MATCHED, tool="resend_otp")

    issued = otp_service.issue_challenge(
        db, session=session, customer=session.customer, is_resend=True
    )
    response = _passcode_response(issued, session, first_issue=False)

    return _finalize(
        db,
        session=session,
        tool_name="resend_otp",
        response=response,
        started=started,
        request_digest={"issue_outcome": issued.outcome.value},
        idempotency_key=payload.idempotency_key,
    )


@router.post("/tools/capture_lead", response_model=ToolResponse)
def capture_lead(
    payload: CaptureLeadRequest, db: Session = Depends(get_db)
) -> ToolResponse:
    """Record a prospective customer's stated details for follow-up.

    Reachable only from `prospect`, which is only reachable when no account
    exists. The state machine is what stops this being used to bypass
    verification: an existing customer can never reach it, and a prospect can
    never reach eligibility.
    """
    started = time.perf_counter()
    session = sessions.load_session(db, payload.session_id)
    SESSION_ID.set(session.id)

    replay = sessions.find_replayed_call(
        db, session.id, "capture_lead", payload.idempotency_key
    )
    if replay is not None:
        log_event("tool_call_replayed", tool="capture_lead", session_id=session.id)
        return ToolResponse(**replay.response_digest, trace_id=TRACE_ID.get())

    sessions.require_state(session, SessionState.PROSPECT, tool="capture_lead")

    lead = leads.create_lead(
        db,
        session=session,
        full_name=payload.full_name,
        date_of_birth=payload.date_of_birth.isoformat(),
        pan=payload.pan,
        email=payload.email,
        phone=payload.phone,
        product_interest=payload.product_interest,
        stated_monthly_income=payload.stated_monthly_income,
    )
    sessions.transition(session, SessionState.LEAD_CAPTURED)

    # The lead is the durable record; the link is a convenience on top of it.
    # If delivery fails, the lead still stands and a human can follow up, so the
    # call ends successfully either way — telling a caller their application
    # failed because an email bounced would be both wrong and alarming.
    link, token = links.issue_link(db, session=session, lead=lead)
    delivered = get_email_sender().send_application_link(
        email=lead.email,
        full_name=lead.full_name,
        reference=lead.reference,
        url=links.build_url(token),
    )
    if not delivered:
        log_event(
            "application_link_delivery_failed",
            level=logging.WARNING,
            session_id=session.id,
            reference=lead.reference,
        )

    if delivered:
        closing = (
            "We have emailed you a secure link to finish the identity checks."
        )
    else:
        closing = (
            "One of my colleagues will be in touch shortly to finish the "
            "identity checks."
        )

    response = ToolResponse(
        outcome="ok",
        agent_message=(
            f"Thank you, I have those details. Your reference is "
            f"{lead.reference}. {closing}"
        ),
        session_state=session.state,
        data={
            "lead_reference": lead.reference,
            "link_emailed": delivered,
            "link_expires_at": link.expires_at.isoformat(),
        },
    )

    return _finalize(
        db,
        session=session,
        tool_name="capture_lead",
        response=response,
        started=started,
        # Redacted like every other audit digest.
        request_digest={
            "pan": mask_pan(payload.pan),
            "product_interest": payload.product_interest,
        },
        idempotency_key=payload.idempotency_key,
    )


@router.post("/tools/check_eligibility", response_model=ToolResponse)
def check_eligibility(
    payload: CheckEligibilityRequest, db: Session = Depends(get_db)
) -> ToolResponse:
    """Run the credit policy and return the decision with its reasons."""
    started = time.perf_counter()
    session = sessions.load_session(db, payload.session_id)
    SESSION_ID.set(session.id)
    replay = sessions.find_replayed_call(
        db, session.id, "check_eligibility", payload.idempotency_key
    )
    if replay is not None:
        log_event("tool_call_replayed", tool="check_eligibility", session_id=session.id)
        return ToolResponse(**replay.response_digest, trace_id=TRACE_ID.get())

    sessions.require_state(
        session, SessionState.IDENTITY_VERIFIED, tool="check_eligibility"
    )

    outcome = eligibility_service.assess(
        customer=session.customer,
        product_code=payload.product_code,
        requested_amount=payload.requested_amount,
        tenure_months=payload.tenure_months,
        declared_monthly_income=payload.declared_monthly_income,
        employment_type=payload.employment_type,
    )

    db.add(
        EligibilityAssessment(
            session_id=session.id,
            product_code=payload.product_code,
            requested_amount=payload.requested_amount,
            tenure_months=payload.tenure_months,
            decision=outcome.decision,
            approved_amount=outcome.approved_amount,
            interest_rate=outcome.interest_rate,
            monthly_instalment=outcome.monthly_instalment,
            reasons=outcome.reasons,
            policy_version=outcome.policy_version,
        )
    )

    if outcome.decision.value == "declined":
        # A decline still advances the state: the call continues, it just ends
        # in a different place. Only the terms differ, not the flow.
        agent_message = (
            "Based on the details you have given me, I am not able to offer this "
            "product today. I can explain what would need to change, or pass you "
            "to a colleague."
        )
        response_outcome = "declined"
    elif outcome.decision.value == "referred":
        agent_message = (
            "Your application needs a quick review by one of my colleagues. "
            "I can arrange that now."
        )
        response_outcome = "ok"
    else:
        agent_message = (
            f"Good news. You are eligible for {_rupees(outcome.approved_amount)} rupees "
            f"over {payload.tenure_months} months, at {outcome.interest_rate} percent "
            f"a year. That works out to about {_rupees(outcome.monthly_instalment)} "
            "rupees a month."
        )
        response_outcome = "ok"

    sessions.transition(session, SessionState.ELIGIBILITY_ASSESSED)

    response = ToolResponse(
        outcome=response_outcome,
        agent_message=agent_message,
        session_state=session.state,
        data={
            "decision": outcome.decision.value,
            "approved_amount": outcome.approved_amount,
            "interest_rate": outcome.interest_rate,
            "monthly_instalment": outcome.monthly_instalment,
            "reasons": outcome.reasons,
            "policy_version": outcome.policy_version,
        },
    )

    return _finalize(
        db,
        session=session,
        tool_name="check_eligibility",
        response=response,
        started=started,
        request_digest={
            "product_code": payload.product_code,
            "requested_amount": payload.requested_amount,
            "tenure_months": payload.tenure_months,
        },
        idempotency_key=payload.idempotency_key,
    )


@router.post("/tools/record_consent", response_model=ToolResponse)
def record_consent(
    payload: RecordConsentRequest, db: Session = Depends(get_db)
) -> ToolResponse:
    """Persist consent evidence, including what the caller actually said."""
    started = time.perf_counter()
    session = sessions.load_session(db, payload.session_id)
    SESSION_ID.set(session.id)
    replay = sessions.find_replayed_call(
        db, session.id, "record_consent", payload.idempotency_key
    )
    if replay is not None:
        log_event("tool_call_replayed", tool="record_consent", session_id=session.id)
        return ToolResponse(**replay.response_digest, trace_id=TRACE_ID.get())

    sessions.require_state(
        session, SessionState.ELIGIBILITY_ASSESSED, tool="record_consent"
    )

    record = handoff.record_consent(
        db,
        session=session,
        consent_type=payload.consent_type,
        granted=payload.granted,
        verbatim_response=payload.verbatim_response,
        disclosure_version=payload.disclosure_version,
    )

    if payload.granted:
        sessions.transition(session, SessionState.CONSENT_RECORDED)
        sessions.transition(session, SessionState.COMPLETED)
        response = ToolResponse(
            outcome="ok",
            agent_message=(
                "Thank you, I have recorded your agreement. Your application is "
                "submitted and you will get a confirmation by SMS shortly."
            ),
            session_state=session.state,
            data={"consent_id": record.id, "granted": True},
        )
    else:
        # Declining consent is a legitimate ending, not a failure to recover from.
        response = ToolResponse(
            outcome="declined",
            agent_message=(
                "That is completely fine. I have recorded that you did not agree, "
                "and I will not proceed with the application."
            ),
            session_state=session.state,
            data={"consent_id": record.id, "granted": False},
        )

    return _finalize(
        db,
        session=session,
        tool_name="record_consent",
        response=response,
        started=started,
        request_digest={
            "consent_type": payload.consent_type,
            "granted": payload.granted,
        },
        idempotency_key=payload.idempotency_key,
    )


@router.post("/tools/escalate", response_model=ToolResponse)
def escalate(payload: EscalateRequest, db: Session = Depends(get_db)) -> ToolResponse:
    """Hand the call to a human.

    Callable from any non-terminal state by design. Escalation is the one path
    that must never be blocked by the state machine — if the agent decides it is
    out of its depth, the service should not argue.
    """
    started = time.perf_counter()
    session = sessions.load_session(db, payload.session_id)
    SESSION_ID.set(session.id)
    replay = sessions.find_replayed_call(
        db, session.id, "escalate", payload.idempotency_key
    )
    if replay is not None:
        log_event("tool_call_replayed", tool="escalate", session_id=session.id)
        return ToolResponse(**replay.response_digest, trace_id=TRACE_ID.get())

    # Every live state, expressed as an invariant rather than a list — see
    # require_live. Escalation must never be the thing that is unavailable.
    sessions.require_live(session, tool="escalate")

    escalation = handoff.create_escalation(
        db, session=session, reason_code=payload.reason_code, summary=payload.summary
    )
    sessions.transition(session, SessionState.ESCALATED)

    response = ToolResponse(
        outcome="ok",
        agent_message=(
            "I am transferring you to a colleague now. Your reference number is "
            f"{escalation.ticket_ref}. Please stay on the line."
        ),
        session_state=session.state,
        data={"ticket_ref": escalation.ticket_ref, "queue": escalation.queue},
    )

    return _finalize(
        db,
        session=session,
        tool_name="escalate",
        response=response,
        started=started,
        request_digest={"reason_code": payload.reason_code, "queue": escalation.queue},
        idempotency_key=payload.idempotency_key,
    )
