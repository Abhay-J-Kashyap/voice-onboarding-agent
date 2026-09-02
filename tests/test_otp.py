"""Passcode behaviour: the second identity factor and the limits around it.

The distinction being protected here is that matching a record proves knowledge
of details printed on a PAN card, while a passcode proves possession of the
registered mobile. Only the second authorises the application to proceed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models import OtpChallenge, SessionState
from app.services import otp as otp_service
from tests.conftest import AUTH

VALID = {
    "full_name": "Rajesh Kumar",
    "date_of_birth": "1988-04-12",
    "pan": "ABCDE1234F",
}


def test_correct_passcode_verifies(client, matched_session, sms):
    response = client.post(
        "/v1/tools/verify_otp",
        json={"session_id": matched_session, "code": sms.last_code},
        headers=AUTH,
    )
    body = response.json()
    assert body["outcome"] == "ok"
    assert body["session_state"] == "identity_verified"


def test_spoken_passcode_with_spaces_is_normalised(client, matched_session, sms):
    """Speech-to-text renders "four two nine" with spaces between digits."""
    spaced = " ".join(sms.last_code)
    response = client.post(
        "/v1/tools/verify_otp",
        json={"session_id": matched_session, "code": spaced},
        headers=AUTH,
    )
    assert response.json()["outcome"] == "ok"


def test_wrong_passcode_allows_retry(client, matched_session, sms):
    wrong = "".join("9" if d != "9" else "1" for d in sms.last_code)
    body = client.post(
        "/v1/tools/verify_otp",
        json={"session_id": matched_session, "code": wrong},
        headers=AUTH,
    ).json()
    assert body["outcome"] == "retry"
    assert body["data"]["attempts_remaining"] == 2
    assert body["session_state"] == "identity_matched"


def test_passcode_attempts_are_capped(client, matched_session, sms):
    wrong = "".join("9" if d != "9" else "1" for d in sms.last_code)
    payload = {"session_id": matched_session, "code": wrong}

    first = client.post("/v1/tools/verify_otp", json=payload, headers=AUTH).json()
    second = client.post("/v1/tools/verify_otp", json=payload, headers=AUTH).json()
    third = client.post("/v1/tools/verify_otp", json=payload, headers=AUTH).json()

    assert first["outcome"] == "retry"
    assert second["outcome"] == "retry"
    assert third["outcome"] == "blocked"
    assert third["data"]["reason"] == "attempts_exhausted"


def test_correct_code_after_exhausted_attempts_still_fails(
    client, matched_session, sms
):
    """Burning the attempt budget closes the challenge, right code or not."""
    wrong = "".join("9" if d != "9" else "1" for d in sms.last_code)
    for _ in range(3):
        client.post(
            "/v1/tools/verify_otp",
            json={"session_id": matched_session, "code": wrong},
            headers=AUTH,
        )

    body = client.post(
        "/v1/tools/verify_otp",
        json={"session_id": matched_session, "code": sms.last_code},
        headers=AUTH,
    ).json()
    assert body["outcome"] == "blocked"


def test_passcode_cannot_be_reused(client, matched_session, sms, db_session):
    """A code overheard on a recorded line must not work twice."""
    code = sms.last_code
    first = client.post(
        "/v1/tools/verify_otp",
        json={"session_id": matched_session, "code": code},
        headers=AUTH,
    ).json()
    assert first["outcome"] == "ok"

    challenge = db_session.execute(
        select(OtpChallenge).where(OtpChallenge.session_id == matched_session)
    ).scalars().first()
    assert challenge.consumed_at is not None


def test_expired_passcode_is_refused(client, matched_session, sms, db_session):
    challenge = db_session.execute(
        select(OtpChallenge).where(OtpChallenge.session_id == matched_session)
    ).scalars().first()
    challenge.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.flush()

    body = client.post(
        "/v1/tools/verify_otp",
        json={"session_id": matched_session, "code": sms.last_code},
        headers=AUTH,
    ).json()
    assert body["outcome"] == "retry"
    assert body["data"]["reason"] == "expired"


def test_resend_issues_a_new_code_and_retires_the_old(client, matched_session, sms):
    original = sms.last_code
    body = client.post(
        "/v1/tools/resend_otp", json={"session_id": matched_session}, headers=AUTH
    ).json()
    assert body["outcome"] == "otp_sent"

    replacement = sms.last_code
    assert replacement != original

    stale = client.post(
        "/v1/tools/verify_otp",
        json={"session_id": matched_session, "code": original},
        headers=AUTH,
    ).json()
    assert stale["outcome"] in {"retry", "blocked"}

    fresh = client.post(
        "/v1/tools/verify_otp",
        json={"session_id": matched_session, "code": replacement},
        headers=AUTH,
    ).json()
    assert fresh["outcome"] == "ok"


def test_resends_are_capped(client, matched_session):
    first = client.post(
        "/v1/tools/resend_otp", json={"session_id": matched_session}, headers=AUTH
    ).json()
    second = client.post(
        "/v1/tools/resend_otp", json={"session_id": matched_session}, headers=AUTH
    ).json()
    third = client.post(
        "/v1/tools/resend_otp", json={"session_id": matched_session}, headers=AUTH
    ).json()

    assert first["outcome"] == "otp_sent"
    assert second["outcome"] == "otp_sent"
    # The third breaches whichever limit binds first: resends or the per-customer
    # issuance window. Either way the caller is routed to a human.
    assert third["outcome"] == "blocked"


def test_issuance_is_rate_limited_per_customer_not_per_session(client, sms):
    """Sessions are free to create, so a per-session cap would be no cap at all."""
    outcomes = []
    for _ in range(4):
        session_id = client.post(
            "/v1/sessions", json={}, headers=AUTH
        ).json()["session_id"]
        body = client.post(
            "/v1/tools/verify_identity",
            json={"session_id": session_id, **VALID},
            headers=AUTH,
        ).json()
        outcomes.append(body["outcome"])

    assert outcomes[:3] == ["otp_sent", "otp_sent", "otp_sent"]
    assert outcomes[3] == "blocked"


def test_eligibility_is_refused_before_the_passcode(client, matched_session):
    """A located record is not a verified caller."""
    response = client.post(
        "/v1/tools/check_eligibility",
        json={
            "session_id": matched_session,
            "product_code": "personal_loan",
            "requested_amount": 300_000,
            "tenure_months": 36,
            "declared_monthly_income": 95_000,
            "employment_type": "salaried",
        },
        headers=AUTH,
    )
    assert response.status_code == 409
    assert response.json()["data"]["code"] == "invalid_transition"


def test_verify_otp_before_a_record_match_is_refused(client, session_id):
    response = client.post(
        "/v1/tools/verify_otp",
        json={"session_id": session_id, "code": "123456"},
        headers=AUTH,
    )
    assert response.status_code == 409


def test_delivery_failure_routes_to_a_human(client, session_id, sms):
    sms.should_fail = True
    body = client.post(
        "/v1/tools/verify_identity",
        json={"session_id": session_id, **VALID},
        headers=AUTH,
    ).json()
    assert body["outcome"] == "error"
    assert body["data"]["reason"] == "delivery_failed"


def test_code_is_never_stored_in_plaintext(client, matched_session, sms, db_session):
    challenge = db_session.execute(
        select(OtpChallenge).where(OtpChallenge.session_id == matched_session)
    ).scalars().first()
    code = sms.last_code
    assert code not in challenge.code_digest
    assert code not in challenge.code_salt
    assert len(challenge.code_digest) == 64


def test_audit_record_proves_issuance_without_revealing_the_code(
    client, matched_session, sms
):
    audit = client.get(f"/v1/sessions/{matched_session}", headers=AUTH).json()
    assert audit["state"] == "identity_matched"
    assert len(audit["otp_challenges"]) == 1
    assert audit["otp_challenges"][0]["consumed"] is False
    assert sms.last_code not in str(audit)


def test_replayed_verify_otp_returns_the_same_verdict(client, matched_session, sms):
    payload = {
        "session_id": matched_session,
        "code": sms.last_code,
        "idempotency_key": "otp-1",
    }
    first = client.post("/v1/tools/verify_otp", json=payload, headers=AUTH).json()
    second = client.post("/v1/tools/verify_otp", json=payload, headers=AUTH).json()
    assert first["outcome"] == second["outcome"] == "ok"


def test_hash_is_salted(client):
    """Two identical codes must not produce the same digest."""
    a = otp_service.hash_code("123456", "salt-a")
    b = otp_service.hash_code("123456", "salt-b")
    assert a != b


def test_generated_codes_have_the_configured_length():
    assert len(otp_service.generate_code(6)) == 6
    assert otp_service.generate_code(6).isdigit()


def test_state_machine_requires_both_factors_in_order():
    from app.models import ALLOWED_TRANSITIONS

    assert (
        SessionState.IDENTITY_VERIFIED
        not in ALLOWED_TRANSITIONS[SessionState.STARTED]
    )
    assert (
        SessionState.IDENTITY_VERIFIED
        in ALLOWED_TRANSITIONS[SessionState.IDENTITY_MATCHED]
    )
