"""Eligibility policy.

Credit decisions are the one thing in this system the model must never make. It
collects inputs and reads back the outcome; the rules below are deterministic,
versioned, and produce an explicit reason list so any decision can be defended
after the fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import get_settings
from app.models import Customer, EligibilityDecision

# Base annual rates by product and employment type, in percent.
BASE_RATES: dict[str, dict[str, float]] = {
    "personal_loan": {"salaried": 12.5, "self_employed": 14.5},
    "credit_card": {"salaried": 36.0, "self_employed": 38.0},
}

# Maximum share of monthly income that may go to debt servicing, by credit band.
MAX_FOIR = {"prime": 0.55, "near_prime": 0.45, "subprime": 0.35}

MIN_CREDIT_SCORE = 650
MIN_MONTHLY_INCOME = 20_000
INCOME_DECLARATION_TOLERANCE = 0.30


@dataclass
class EligibilityOutcome:
    decision: EligibilityDecision
    approved_amount: int
    interest_rate: float
    monthly_instalment: int
    reasons: list[str] = field(default_factory=list)
    policy_version: str = ""


def credit_band(score: int) -> str:
    if score >= 750:
        return "prime"
    if score >= 700:
        return "near_prime"
    return "subprime"


def monthly_instalment(principal: int, annual_rate: float, months: int) -> int:
    """Standard amortising EMI, rounded to whole rupees."""
    if principal <= 0 or months <= 0:
        return 0
    monthly_rate = annual_rate / 12 / 100
    if monthly_rate == 0:
        return round(principal / months)
    factor = (1 + monthly_rate) ** months
    return round(principal * monthly_rate * factor / (factor - 1))


def assess(
    *,
    customer: Customer,
    product_code: str,
    requested_amount: int,
    tenure_months: int,
    declared_monthly_income: int,
    employment_type: str,
) -> EligibilityOutcome:
    """Apply policy and return a decision with its reasons.

    `referred` is a first-class outcome, not a fallback. Anything the rules
    cannot settle cleanly goes to a human rather than being forced into an
    approval or a decline.
    """
    settings = get_settings()
    version = settings.eligibility_policy_version
    reasons: list[str] = []

    if employment_type == "unemployed":
        return EligibilityOutcome(
            decision=EligibilityDecision.DECLINED,
            approved_amount=0,
            interest_rate=0.0,
            monthly_instalment=0,
            reasons=["no_verifiable_income"],
            policy_version=version,
        )

    # A large gap between what the caller says and what the file says is a
    # data-quality signal, not a decline. Send it to an underwriter.
    on_file = customer.monthly_income
    if on_file > 0:
        deviation = abs(declared_monthly_income - on_file) / on_file
        if deviation > INCOME_DECLARATION_TOLERANCE:
            reasons.append("declared_income_deviates_from_record")

    if customer.credit_score < MIN_CREDIT_SCORE:
        return EligibilityOutcome(
            decision=EligibilityDecision.DECLINED,
            approved_amount=0,
            interest_rate=0.0,
            monthly_instalment=0,
            reasons=[*reasons, "credit_score_below_threshold"],
            policy_version=version,
        )

    assessable_income = (
        min(declared_monthly_income, on_file) if on_file else declared_monthly_income
    )
    if assessable_income < MIN_MONTHLY_INCOME:
        return EligibilityOutcome(
            decision=EligibilityDecision.DECLINED,
            approved_amount=0,
            interest_rate=0.0,
            monthly_instalment=0,
            reasons=[*reasons, "income_below_minimum"],
            policy_version=version,
        )

    band = credit_band(customer.credit_score)
    rate = BASE_RATES[product_code][employment_type]
    if band == "near_prime":
        rate += 1.5
    elif band == "subprime":
        rate += 3.0

    # Headroom is what is left of the permitted debt burden after existing EMIs.
    headroom = assessable_income * MAX_FOIR[band] - customer.existing_emi
    if headroom <= 0:
        return EligibilityOutcome(
            decision=EligibilityDecision.DECLINED,
            approved_amount=0,
            interest_rate=0.0,
            monthly_instalment=0,
            reasons=[*reasons, "existing_obligations_exhaust_capacity"],
            policy_version=version,
        )

    requested_emi = monthly_instalment(requested_amount, rate, tenure_months)
    if requested_emi <= headroom:
        approved = requested_amount
        emi = requested_emi
        reasons.append("within_debt_service_capacity")
    else:
        # Solve for the principal the caller can actually service at this rate.
        monthly_rate = rate / 12 / 100
        factor = (1 + monthly_rate) ** tenure_months
        affordable = int(headroom * (factor - 1) / (monthly_rate * factor))
        approved = max(0, (affordable // 1000) * 1000)
        emi = monthly_instalment(approved, rate, tenure_months)
        reasons.append("counter_offer_limited_by_debt_service_capacity")

    if approved <= 0:
        return EligibilityOutcome(
            decision=EligibilityDecision.DECLINED,
            approved_amount=0,
            interest_rate=round(rate, 2),
            monthly_instalment=0,
            reasons=[*reasons, "no_affordable_principal"],
            policy_version=version,
        )

    decision = (
        EligibilityDecision.REFERRED
        if "declared_income_deviates_from_record" in reasons
        else EligibilityDecision.APPROVED
    )

    return EligibilityOutcome(
        decision=decision,
        approved_amount=approved,
        interest_rate=round(rate, 2),
        monthly_instalment=emi,
        reasons=reasons,
        policy_version=version,
    )
