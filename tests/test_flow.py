"""End-to-end flow, ordering guarantees, hand-off and auditability."""

from __future__ import annotations

from app.observability import TRACE_HEADER
from tests.conftest import AUTH

ELIGIBILITY_PAYLOAD = {
    "product_code": "personal_loan",
    "requested_amount": 300_000,
    "tenure_months": 36,
    "declared_monthly_income": 95_000,
    "employment_type": "salaried",
}


def test_happy_path_completes(client, verified_session):
    client.post(
        "/v1/tools/check_eligibility",
        json={"session_id": verified_session, **ELIGIBILITY_PAYLOAD},
        headers=AUTH,
    )
    consent = client.post(
        "/v1/tools/record_consent",
        json={
            "session_id": verified_session,
            "consent_type": "terms_and_conditions",
            "granted": True,
            "verbatim_response": "yes I agree",
        },
        headers=AUTH,
    ).json()

    assert consent["outcome"] == "ok"
    assert consent["session_state"] == "completed"


def test_consent_before_verification_is_rejected(client, session_id):
    """The ordering rule holds even if the model skips a step."""
    response = client.post(
        "/v1/tools/record_consent",
        json={
            "session_id": session_id,
            "consent_type": "terms_and_conditions",
            "granted": True,
            "verbatim_response": "yes",
        },
        headers=AUTH,
    )
    assert response.status_code == 409
    assert response.json()["data"]["code"] == "invalid_transition"


def test_eligibility_before_verification_is_rejected(client, session_id):
    response = client.post(
        "/v1/tools/check_eligibility",
        json={"session_id": session_id, **ELIGIBILITY_PAYLOAD},
        headers=AUTH,
    )
    assert response.status_code == 409


def test_refused_consent_is_recorded_not_retried(client, verified_session):
    client.post(
        "/v1/tools/check_eligibility",
        json={"session_id": verified_session, **ELIGIBILITY_PAYLOAD},
        headers=AUTH,
    )
    response = client.post(
        "/v1/tools/record_consent",
        json={
            "session_id": verified_session,
            "consent_type": "terms_and_conditions",
            "granted": False,
            "verbatim_response": "no, I want to think about it",
        },
        headers=AUTH,
    ).json()
    assert response["outcome"] == "declined"
    assert response["data"]["granted"] is False


def test_escalation_is_available_from_any_live_state(client, session_id):
    response = client.post(
        "/v1/tools/escalate",
        json={
            "session_id": session_id,
            "reason_code": "customer_requested_human",
            "summary": "Caller asked for a person before verification.",
        },
        headers=AUTH,
    ).json()
    assert response["outcome"] == "ok"
    assert response["data"]["ticket_ref"].startswith("ESC-")
    assert response["session_state"] == "escalated"


def test_escalation_routes_to_the_right_queue(client, session_id):
    response = client.post(
        "/v1/tools/escalate",
        json={
            "session_id": session_id,
            "reason_code": "customer_disputes_decision",
            "summary": "Caller disputes the declined outcome.",
        },
        headers=AUTH,
    ).json()
    assert response["data"]["queue"] == "underwriting"


def test_closed_session_refuses_further_tool_calls(client, session_id):
    client.post(
        "/v1/tools/escalate",
        json={
            "session_id": session_id,
            "reason_code": "technical_failure",
            "summary": "Audio dropped.",
        },
        headers=AUTH,
    )
    response = client.post(
        "/v1/tools/verify_identity",
        json={
            "session_id": session_id,
            "full_name": "Rajesh Kumar",
            "date_of_birth": "1988-04-12",
            "pan": "ABCDE1234F",
        },
        headers=AUTH,
    )
    assert response.status_code == 409
    assert response.json()["data"]["code"] == "session_closed"


def test_audit_endpoint_reconstructs_the_call(client, verified_session):
    live = client.post(
        "/v1/tools/check_eligibility",
        json={"session_id": verified_session, **ELIGIBILITY_PAYLOAD},
        headers=AUTH,
    ).json()
    client.post(
        "/v1/tools/record_consent",
        json={
            "session_id": verified_session,
            "consent_type": "terms_and_conditions",
            "granted": True,
            "verbatim_response": "yes I agree",
        },
        headers=AUTH,
    )

    audit = client.get(f"/v1/sessions/{verified_session}", headers=AUTH).json()

    assert audit["state"] == "completed"
    assert [c["tool_name"] for c in audit["tool_calls"]] == [
        "verify_identity",
        "check_eligibility",
        "record_consent",
    ]
    assert all(c["latency_ms"] >= 0 for c in audit["tool_calls"])
    assert audit["eligibility"]["decision"] == "approved"
    # Regression: the audit row once hard-coded this to 0 instead of persisting
    # the value the live tool response already returned. Assert equality against
    # the live figure, not a literal, so a re-introduced mismatch fails loudly.
    assert audit["eligibility"]["monthly_instalment"] > 0
    assert audit["eligibility"]["monthly_instalment"] == live["data"]["monthly_instalment"]
    assert audit["consents"][0]["granted"] is True


def test_trace_id_is_echoed_and_reused(client, session_id):
    response = client.post(
        "/v1/tools/escalate",
        json={
            "session_id": session_id,
            "reason_code": "out_of_scope_request",
            "summary": "Asked about a product we do not offer.",
        },
        headers={**AUTH, TRACE_HEADER: "call-abc-123"},
    )
    assert response.headers[TRACE_HEADER] == "call-abc-123"
    assert response.json()["trace_id"] == "call-abc-123"


def test_tool_endpoints_require_an_api_key(client, session_id):
    response = client.post(
        "/v1/tools/escalate",
        json={
            "session_id": session_id,
            "reason_code": "technical_failure",
            "summary": "test",
        },
    )
    assert response.status_code == 401


def test_health_and_readiness(client):
    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/readyz").json()["status"] == "ready"