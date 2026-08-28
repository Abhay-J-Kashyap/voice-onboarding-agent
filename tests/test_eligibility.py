"""Eligibility policy: the decisions the model is not allowed to make."""

from __future__ import annotations

import pytest

from app.models import Customer, EligibilityDecision
from app.services.eligibility import assess, credit_band, monthly_instalment
from tests.conftest import AUTH


def make_customer(**overrides) -> Customer:
    defaults = dict(
        id=1,
        full_name="Test Person",
        date_of_birth="1990-01-01",
        pan="ZZZZZ9999Z",
        phone="+919800000000",
        monthly_income=100_000,
        employment_type="salaried",
        existing_emi=0,
        credit_score=780,
        is_sanctioned=False,
    )
    defaults.update(overrides)
    return Customer(**defaults)


def test_emi_matches_standard_amortisation():
    # 100,000 at 12% over 12 months is a well-known 8,885.
    assert monthly_instalment(100_000, 12.0, 12) == 8885


def test_zero_interest_divides_evenly():
    assert monthly_instalment(120_000, 0.0, 12) == 10_000


@pytest.mark.parametrize(
    "score,band",
    [(800, "prime"), (750, "prime"), (720, "near_prime"), (699, "subprime")],
)
def test_credit_bands(score, band):
    assert credit_band(score) == band


def test_clean_approval():
    outcome = assess(
        customer=make_customer(),
        product_code="personal_loan",
        requested_amount=300_000,
        tenure_months=36,
        declared_monthly_income=100_000,
        employment_type="salaried",
    )
    assert outcome.decision is EligibilityDecision.APPROVED
    assert outcome.approved_amount == 300_000
    assert "within_debt_service_capacity" in outcome.reasons


def test_low_credit_score_declines():
    outcome = assess(
        customer=make_customer(credit_score=610),
        product_code="personal_loan",
        requested_amount=100_000,
        tenure_months=24,
        declared_monthly_income=100_000,
        employment_type="salaried",
    )
    assert outcome.decision is EligibilityDecision.DECLINED
    assert "credit_score_below_threshold" in outcome.reasons


def test_existing_obligations_can_exhaust_capacity():
    outcome = assess(
        customer=make_customer(monthly_income=38_000, existing_emi=25_000),
        product_code="personal_loan",
        requested_amount=100_000,
        tenure_months=24,
        declared_monthly_income=38_000,
        employment_type="salaried",
    )
    assert outcome.decision is EligibilityDecision.DECLINED
    assert "existing_obligations_exhaust_capacity" in outcome.reasons


def test_unaffordable_request_becomes_a_counter_offer():
    outcome = assess(
        customer=make_customer(monthly_income=42_000, existing_emi=9_500, credit_score=715),
        product_code="personal_loan",
        requested_amount=2_000_000,
        tenure_months=36,
        declared_monthly_income=42_000,
        employment_type="salaried",
    )
    assert outcome.decision is EligibilityDecision.APPROVED
    assert 0 < outcome.approved_amount < 2_000_000
    assert "counter_offer_limited_by_debt_service_capacity" in outcome.reasons


def test_counter_offer_stays_within_headroom():
    """The offered instalment must never exceed the policy's debt-service limit."""
    customer = make_customer(monthly_income=42_000, existing_emi=9_500, credit_score=715)
    outcome = assess(
        customer=customer,
        product_code="personal_loan",
        requested_amount=2_000_000,
        tenure_months=36,
        declared_monthly_income=42_000,
        employment_type="salaried",
    )
    headroom = 42_000 * 0.45 - 9_500
    assert outcome.monthly_instalment <= headroom + 1


def test_income_deviation_refers_rather_than_approves():
    outcome = assess(
        customer=make_customer(monthly_income=40_000),
        product_code="personal_loan",
        requested_amount=50_000,
        tenure_months=24,
        declared_monthly_income=200_000,
        employment_type="salaried",
    )
    assert outcome.decision is EligibilityDecision.REFERRED
    assert "declared_income_deviates_from_record" in outcome.reasons


def test_unemployed_is_declined_immediately():
    outcome = assess(
        customer=make_customer(),
        product_code="personal_loan",
        requested_amount=50_000,
        tenure_months=24,
        declared_monthly_income=1,
        employment_type="unemployed",
    )
    assert outcome.decision is EligibilityDecision.DECLINED
    assert outcome.reasons == ["no_verifiable_income"]


def test_self_employed_pays_a_higher_rate():
    salaried = assess(
        customer=make_customer(),
        product_code="personal_loan",
        requested_amount=100_000,
        tenure_months=24,
        declared_monthly_income=100_000,
        employment_type="salaried",
    )
    self_employed = assess(
        customer=make_customer(employment_type="self_employed"),
        product_code="personal_loan",
        requested_amount=100_000,
        tenure_months=24,
        declared_monthly_income=100_000,
        employment_type="self_employed",
    )
    assert self_employed.interest_rate > salaried.interest_rate


def test_every_outcome_is_pinned_to_a_policy_version():
    outcome = assess(
        customer=make_customer(),
        product_code="personal_loan",
        requested_amount=100_000,
        tenure_months=24,
        declared_monthly_income=100_000,
        employment_type="salaried",
    )
    assert outcome.policy_version


def test_eligibility_endpoint_returns_speakable_terms(client, verified_session):
    response = client.post(
        "/v1/tools/check_eligibility",
        json={
            "session_id": verified_session,
            "product_code": "personal_loan",
            "requested_amount": 300_000,
            "tenure_months": 36,
            "declared_monthly_income": 95_000,
            "employment_type": "salaried",
        },
        headers=AUTH,
    )
    body = response.json()
    assert body["outcome"] == "ok"
    assert body["data"]["decision"] == "approved"
    assert "rupees" in body["agent_message"]
