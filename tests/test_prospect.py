"""The prospect branch: callers with no existing account.

A PAN that is not on file is a commercially different event from a PAN that is
on file with mismatched details. One caller mistyped something; the other is a
prospective customer being turned away. Collapsing both into "that does not
match our records" loses a lead and tells a real applicant they got their own
details wrong.

The guards that matter here are the ones stopping the two paths from bleeding
into each other: a prospect must never reach a credit decision, and an existing
customer must never reach lead capture.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models import Lead, SessionState
from tests.conftest import AUTH

#: Syntactically valid, deliberately absent from the seeded customers.
UNKNOWN = {
    "full_name": "Kavya Reddy",
    "date_of_birth": "1992-08-14",
    "pan": "ZZZZZ9999Z",
}

LEAD_DETAILS = {
    **UNKNOWN,
    "email": "kavya.reddy@example.com",
    "phone": "+919812345678",
    "product_interest": "personal_loan",
    "stated_monthly_income": 70_000,
}


def test_unknown_pan_is_not_a_verification_failure(client, session_id):
    response = client.post(
        "/v1/tools/verify_identity",
        json={"session_id": session_id, **UNKNOWN},
        headers=AUTH,
    )
    body = response.json()
    assert response.status_code == 200
    assert body["outcome"] == "not_registered"
    assert body["session_state"] == "prospect"
    assert body["data"]["registered"] is False


def test_unknown_pan_does_not_consume_an_attempt(client, session_id):
    """Blocking a prospective customer for 'failing' would be commercially absurd."""
    client.post(
        "/v1/tools/verify_identity",
        json={"session_id": session_id, **UNKNOWN},
        headers=AUTH,
    )
    audit = client.get(f"/v1/sessions/{session_id}", headers=AUTH).json()
    assert audit["identity_attempts"] == 0


def test_unknown_pan_message_does_not_blame_the_caller(client, session_id):
    body = client.post(
        "/v1/tools/verify_identity",
        json={"session_id": session_id, **UNKNOWN},
        headers=AUTH,
    ).json()
    message = body["agent_message"].lower()
    assert "does not match" not in message
    assert "could not find an existing account" in message


def test_mismatched_details_still_retry_rather_than_becoming_a_lead(
    client, session_id
):
    """An existing customer with a wrong date is not a prospect."""
    body = client.post(
        "/v1/tools/verify_identity",
        json={
            "session_id": session_id,
            "full_name": "Rajesh Kumar",
            "date_of_birth": "1990-01-01",
            "pan": "ABCDE1234F",
        },
        headers=AUTH,
    ).json()
    assert body["outcome"] == "retry"
    assert body["session_state"] == "started"


def test_lead_is_captured_with_a_reference(client, session_id):
    client.post(
        "/v1/tools/verify_identity",
        json={"session_id": session_id, **UNKNOWN},
        headers=AUTH,
    )
    body = client.post(
        "/v1/tools/capture_lead",
        json={"session_id": session_id, **LEAD_DETAILS},
        headers=AUTH,
    ).json()

    assert body["outcome"] == "ok"
    assert body["session_state"] == "lead_captured"
    assert body["data"]["lead_reference"].startswith("LEAD-")


def test_lead_is_stored_separately_from_customers(client, session_id, db_session):
    """An unverified person must never land in the customer table."""
    from app.models import Customer

    client.post(
        "/v1/tools/verify_identity",
        json={"session_id": session_id, **UNKNOWN},
        headers=AUTH,
    )
    client.post(
        "/v1/tools/capture_lead",
        json={"session_id": session_id, **LEAD_DETAILS},
        headers=AUTH,
    )

    lead = db_session.execute(
        select(Lead).where(Lead.pan == "ZZZZZ9999Z")
    ).scalars().first()
    assert lead is not None

    customer = db_session.execute(
        select(Customer).where(Customer.pan == "ZZZZZ9999Z")
    ).scalars().first()
    assert customer is None, "a lead must not become a customer record"


def test_prospect_can_never_reach_a_credit_decision(client, session_id):
    """No record, no bureau data, no basis for a decision. Refused by the machine."""
    client.post(
        "/v1/tools/verify_identity",
        json={"session_id": session_id, **UNKNOWN},
        headers=AUTH,
    )
    response = client.post(
        "/v1/tools/check_eligibility",
        json={
            "session_id": session_id,
            "product_code": "personal_loan",
            "requested_amount": 300_000,
            "tenure_months": 36,
            "declared_monthly_income": 70_000,
            "employment_type": "salaried",
        },
        headers=AUTH,
    )
    assert response.status_code == 409
    assert response.json()["data"]["code"] == "invalid_transition"


def test_prospect_cannot_verify_a_passcode(client, session_id):
    """There is no registered contact to have sent one to."""
    client.post(
        "/v1/tools/verify_identity",
        json={"session_id": session_id, **UNKNOWN},
        headers=AUTH,
    )
    response = client.post(
        "/v1/tools/verify_otp",
        json={"session_id": session_id, "code": "123456"},
        headers=AUTH,
    )
    assert response.status_code == 409


def test_existing_customer_cannot_reach_lead_capture(client, matched_session):
    """The state machine keeps the acquisition path off the servicing path."""
    response = client.post(
        "/v1/tools/capture_lead",
        json={"session_id": matched_session, **LEAD_DETAILS},
        headers=AUTH,
    )
    assert response.status_code == 409


def test_lead_capture_before_a_lookup_is_refused(client, session_id):
    response = client.post(
        "/v1/tools/capture_lead",
        json={"session_id": session_id, **LEAD_DETAILS},
        headers=AUTH,
    )
    assert response.status_code == 409


def test_lead_capture_is_terminal(client, session_id):
    client.post(
        "/v1/tools/verify_identity",
        json={"session_id": session_id, **UNKNOWN},
        headers=AUTH,
    )
    client.post(
        "/v1/tools/capture_lead",
        json={"session_id": session_id, **LEAD_DETAILS},
        headers=AUTH,
    )
    response = client.post(
        "/v1/tools/capture_lead",
        json={"session_id": session_id, **LEAD_DETAILS},
        headers=AUTH,
    )
    assert response.status_code == 409
    assert response.json()["data"]["code"] == "session_closed"


def test_prospect_can_still_escalate(client, session_id):
    client.post(
        "/v1/tools/verify_identity",
        json={"session_id": session_id, **UNKNOWN},
        headers=AUTH,
    )
    body = client.post(
        "/v1/tools/escalate",
        json={
            "session_id": session_id,
            "reason_code": "customer_requested_human",
            "summary": "Prospect asked to speak to someone.",
        },
        headers=AUTH,
    ).json()
    assert body["outcome"] == "ok"


def test_malformed_lead_email_names_the_field(client, session_id):
    client.post(
        "/v1/tools/verify_identity",
        json={"session_id": session_id, **UNKNOWN},
        headers=AUTH,
    )
    body = client.post(
        "/v1/tools/capture_lead",
        json={"session_id": session_id, **LEAD_DETAILS, "email": "not-an-email"},
        headers=AUTH,
    ).json()
    assert body["data"]["invalid_fields"] == ["email"]
    assert "email address" in body["agent_message"]


def test_lead_email_is_normalised(client, session_id, db_session):
    client.post(
        "/v1/tools/verify_identity",
        json={"session_id": session_id, **UNKNOWN},
        headers=AUTH,
    )
    client.post(
        "/v1/tools/capture_lead",
        json={"session_id": session_id, **LEAD_DETAILS, "email": " Kavya.Reddy@Example.COM "},
        headers=AUTH,
    )
    lead = db_session.execute(
        select(Lead).where(Lead.pan == "ZZZZZ9999Z")
    ).scalars().first()
    assert lead.email == "kavya.reddy@example.com"


def test_audit_record_shows_the_lead_with_a_masked_pan(client, session_id):
    client.post(
        "/v1/tools/verify_identity",
        json={"session_id": session_id, **UNKNOWN},
        headers=AUTH,
    )
    client.post(
        "/v1/tools/capture_lead",
        json={"session_id": session_id, **LEAD_DETAILS},
        headers=AUTH,
    )
    audit = client.get(f"/v1/sessions/{session_id}", headers=AUTH).json()

    assert audit["state"] == "lead_captured"
    assert audit["lead"]["reference"].startswith("LEAD-")
    assert "ZZZZZ9999Z" not in str(audit)
    assert audit["eligibility"] is None


def test_lead_capture_is_idempotent(client, session_id):
    client.post(
        "/v1/tools/verify_identity",
        json={"session_id": session_id, **UNKNOWN},
        headers=AUTH,
    )
    payload = {"session_id": session_id, **LEAD_DETAILS, "idempotency_key": "lead-1"}
    first = client.post("/v1/tools/capture_lead", json=payload, headers=AUTH).json()
    second = client.post("/v1/tools/capture_lead", json=payload, headers=AUTH).json()
    assert first["data"]["lead_reference"] == second["data"]["lead_reference"]


def test_escalation_reaches_every_live_state():
    """The invariant, asserted against the machine rather than a hardcoded list.

    This exists because adding the prospect state silently removed escalation
    from it: the endpoint enumerated its permitted states, and the new one was
    not on the list. A caller who could not be found and then asked for a human
    would have been refused. Asserting against ALLOWED_TRANSITIONS means the
    next state added has to keep the hand-off available or fail here.
    """
    from app.models import ALLOWED_TRANSITIONS, TERMINAL_STATES, SessionState

    for state in SessionState:
        if state in TERMINAL_STATES:
            continue
        assert SessionState.ESCALATED in ALLOWED_TRANSITIONS[state], (
            f"{state.value} cannot escalate; escalation must never be the "
            "thing that is unavailable"
        )


def test_state_machine_separates_the_two_paths():
    from app.models import ALLOWED_TRANSITIONS

    # A prospect cannot become a verified customer without a real record.
    assert (
        SessionState.IDENTITY_MATCHED
        not in ALLOWED_TRANSITIONS[SessionState.PROSPECT]
    )
    assert (
        SessionState.ELIGIBILITY_ASSESSED
        not in ALLOWED_TRANSITIONS[SessionState.PROSPECT]
    )
    # And a located record cannot be diverted into lead capture.
    assert (
        SessionState.LEAD_CAPTURED
        not in ALLOWED_TRANSITIONS[SessionState.IDENTITY_MATCHED]
    )
