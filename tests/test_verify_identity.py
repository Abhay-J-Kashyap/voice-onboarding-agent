"""Identity verification behaviour, including the limits the prompt cannot enforce."""

from __future__ import annotations

import pytest

from app.observability import mask_pan
from app.services.kyc import name_similarity, normalise_name
from tests.conftest import AUTH

VALID = {
    "full_name": "Rajesh Kumar",
    "date_of_birth": "1988-04-12",
    "pan": "ABCDE1234F",
}


def test_matching_details_send_a_passcode(client, session_id, sms):
    """A record match is a knowledge factor: it sends a code, it does not verify."""
    response = client.post(
        "/v1/tools/verify_identity",
        json={"session_id": session_id, **VALID},
        headers=AUTH,
    )
    body = response.json()
    assert response.status_code == 200
    assert body["outcome"] == "otp_sent"
    assert body["session_state"] == "identity_matched"
    assert body["data"]["customer_reference"].startswith("CUST-")
    assert body["data"]["phone_last4"] == "0001"
    # The passcode must never reach anything the agent can speak.
    assert "demo_otp" not in body["data"]
    assert sms.last_code not in body["agent_message"]


def test_lowercase_and_spaced_pan_is_normalised(client, session_id, sms):
    response = client.post(
        "/v1/tools/verify_identity",
        json={"session_id": session_id, **VALID, "pan": "abcde 1234 f"},
        headers=AUTH,
    )
    assert response.json()["outcome"] == "otp_sent"


def test_honorific_does_not_break_name_match(client, session_id, sms):
    response = client.post(
        "/v1/tools/verify_identity",
        json={"session_id": session_id, **VALID, "full_name": "Mr. Rajesh Kumar"},
        headers=AUTH,
    )
    assert response.json()["outcome"] == "otp_sent"


def test_dob_mismatch_asks_for_retry(client, session_id):
    response = client.post(
        "/v1/tools/verify_identity",
        json={"session_id": session_id, **VALID, "date_of_birth": "1990-01-01"},
        headers=AUTH,
    )
    body = response.json()
    assert body["outcome"] == "retry"
    assert body["data"]["retries_remaining"] == 1


def test_attempts_are_capped_server_side(client, session_id):
    """The third attempt is refused even though the model may keep trying."""
    bad = {"session_id": session_id, **VALID, "date_of_birth": "1990-01-01"}

    first = client.post("/v1/tools/verify_identity", json=bad, headers=AUTH).json()
    assert first["outcome"] == "retry"

    second = client.post("/v1/tools/verify_identity", json=bad, headers=AUTH).json()
    assert second["outcome"] == "blocked"

    third = client.post("/v1/tools/verify_identity", json=bad, headers=AUTH)
    assert third.status_code == 403
    assert third.json()["data"]["code"] == "attempts_exhausted"


def test_sanctioned_customer_is_blocked_without_disclosure(client, session_id):
    response = client.post(
        "/v1/tools/verify_identity",
        json={
            "session_id": session_id,
            "full_name": "Vikram Anand",
            "date_of_birth": "1979-09-15",
            "pan": "EFGHI5678J",
        },
        headers=AUTH,
    )
    body = response.json()
    assert body["outcome"] == "blocked"
    # The caller must not learn why.
    assert "sanction" not in body["agent_message"].lower()


def test_idempotency_key_does_not_consume_an_extra_attempt(client, session_id):
    bad = {
        "session_id": session_id,
        **VALID,
        "date_of_birth": "1990-01-01",
        "idempotency_key": "retry-after-timeout",
    }
    first = client.post("/v1/tools/verify_identity", json=bad, headers=AUTH).json()
    replayed = client.post("/v1/tools/verify_identity", json=bad, headers=AUTH).json()

    assert first["data"]["retries_remaining"] == replayed["data"]["retries_remaining"]
    assert first["outcome"] == replayed["outcome"] == "retry"


def test_malformed_pan_returns_recoverable_retry(client, session_id):
    response = client.post(
        "/v1/tools/verify_identity",
        json={"session_id": session_id, **VALID, "pan": "1234567890"},
        headers=AUTH,
    )
    assert response.status_code == 422
    body = response.json()
    assert body["outcome"] == "retry"
    assert "pan" in body["data"]["invalid_fields"]


def test_minor_is_rejected_at_the_schema_boundary(client, session_id):
    response = client.post(
        "/v1/tools/verify_identity",
        json={"session_id": session_id, **VALID, "date_of_birth": "2015-01-01"},
        headers=AUTH,
    )
    assert response.status_code == 422


def test_unknown_session_is_a_clean_404(client):
    response = client.post(
        "/v1/tools/verify_identity",
        json={"session_id": "does-not-exist", **VALID},
        headers=AUTH,
    )
    assert response.status_code == 404
    assert response.json()["data"]["code"] == "session_not_found"


@pytest.mark.parametrize(
    "raw,expected",
    [("ABCDE1234F", "AB*****34F"), ("ABC", "***"), (None, None)],
)
def test_pan_masking(raw, expected):
    assert mask_pan(raw) == expected


def test_name_normalisation_and_similarity():
    assert normalise_name("Mr.  Rajesh   Kumar") == "rajesh kumar"
    assert name_similarity("Rajesh Kumar", "Rajesh Kumar") == 1.0
    assert name_similarity("Completely Different", "Rajesh Kumar") == 0.0
