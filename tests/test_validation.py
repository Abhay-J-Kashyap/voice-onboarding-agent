"""Validation errors must be recoverable inside the conversation.

These exist because of a real call that failed: a caller asked for a ten year
tenure, the schema capped it at seven, and the agent received a generic "I did
not catch that" naming no field. It retried the same rejected value, failed
identically, and escalated a call that never needed a human.

A schema rejection is usually a mishearing or an out-of-policy request. Both are
recoverable, but only if the response says which field was wrong and what would
be acceptable.
"""

from __future__ import annotations

import pytest

from app.schemas import (
    FIELD_GUIDANCE,
    CheckEligibilityRequest,
    EscalateRequest,
    RecordConsentRequest,
    VerifyIdentityRequest,
    VerifyOtpRequest,
    validation_message,
)
from tests.conftest import AUTH

#: Fields that belong to the platform, never to the caller. The agent should
#: never raise these in conversation, so they need no spoken guidance.
INTERNAL_FIELDS = {"session_id", "idempotency_key", "disclosure_version"}

REQUEST_MODELS = [
    VerifyIdentityRequest,
    VerifyOtpRequest,
    CheckEligibilityRequest,
    RecordConsentRequest,
    EscalateRequest,
]


def test_validation_guidance_covers_every_field():
    """Guard against drift: a new field must arrive with its recovery wording.

    Without this, adding a constrained field silently reintroduces the original
    bug — a rejection the agent cannot act on.
    """
    missing = set()
    for model in REQUEST_MODELS:
        for name in model.model_fields:
            if name in INTERNAL_FIELDS:
                continue
            if name not in FIELD_GUIDANCE:
                missing.add(f"{model.__name__}.{name}")
    assert not missing, f"fields with no spoken guidance: {sorted(missing)}"


def test_ten_year_tenure_is_accepted(client, verified_session):
    """The case that broke a live call: ten years is a real personal loan term."""
    response = client.post(
        "/v1/tools/check_eligibility",
        json={
            "session_id": verified_session,
            "product_code": "personal_loan",
            "requested_amount": 1_500_000,
            "tenure_months": 120,
            "declared_monthly_income": 95_000,
            "employment_type": "salaried",
        },
        headers=AUTH,
    )
    assert response.status_code == 200
    assert response.json()["outcome"] in {"ok", "declined"}


def test_tenure_beyond_policy_names_the_field(client, verified_session):
    response = client.post(
        "/v1/tools/check_eligibility",
        json={
            "session_id": verified_session,
            "product_code": "personal_loan",
            "requested_amount": 1_500_000,
            "tenure_months": 240,
            "declared_monthly_income": 95_000,
            "employment_type": "salaried",
        },
        headers=AUTH,
    )
    body = response.json()
    assert response.status_code == 422
    assert body["outcome"] == "retry"
    assert body["data"]["invalid_fields"] == ["tenure_months"]
    # The whole point: the agent is told what to ask for, not just that it failed.
    assert "repayment period" in body["agent_message"]
    assert "ten years" in body["agent_message"]


def test_malformed_pan_names_the_field(client, session_id):
    response = client.post(
        "/v1/tools/verify_identity",
        json={
            "session_id": session_id,
            "full_name": "Rajesh Kumar",
            "date_of_birth": "1988-04-12",
            "pan": "NOTAPAN",
        },
        headers=AUTH,
    )
    body = response.json()
    assert body["data"]["invalid_fields"] == ["pan"]
    assert "ten characters" in body["agent_message"]


def test_bad_employment_type_names_the_field(client, verified_session):
    response = client.post(
        "/v1/tools/check_eligibility",
        json={
            "session_id": verified_session,
            "product_code": "personal_loan",
            "requested_amount": 300_000,
            "tenure_months": 36,
            "declared_monthly_income": 95_000,
            "employment_type": "freelancer",
        },
        headers=AUTH,
    )
    body = response.json()
    assert body["data"]["invalid_fields"] == ["employment_type"]
    assert "self-employed" in body["agent_message"]


def test_multiple_bad_fields_are_all_named(client, verified_session):
    response = client.post(
        "/v1/tools/check_eligibility",
        json={
            "session_id": verified_session,
            "product_code": "personal_loan",
            "requested_amount": 300_000,
            "tenure_months": 500,
            "declared_monthly_income": -5,
            "employment_type": "salaried",
        },
        headers=AUTH,
    )
    body = response.json()
    assert set(body["data"]["invalid_fields"]) == {
        "tenure_months",
        "declared_monthly_income",
    }
    assert "repayment period" in body["agent_message"]
    assert "monthly income" in body["agent_message"]


def test_internal_field_failure_stays_generic(client):
    """A platform-level problem is never explained to a caller as their mistake."""
    message = validation_message(["session_id"])
    assert "session" not in message.lower()
    assert "did not catch that" in message


@pytest.mark.parametrize(
    "fields,expected_fragment",
    [
        (["tenure_months"], "Could you give me that again?"),
        (["tenure_months", "declared_monthly_income"], "Could you give me those again?"),
    ],
)
def test_message_agrees_in_number(fields, expected_fragment):
    assert expected_fragment in validation_message(fields)


def test_every_guidance_string_is_speakable():
    """No field names, underscores or code artefacts in anything said aloud."""
    for field, guidance in FIELD_GUIDANCE.items():
        assert "_" not in guidance, f"{field} guidance contains an identifier"
        assert guidance == guidance.strip()
        assert not guidance.endswith("."), f"{field} guidance should not end a sentence"
