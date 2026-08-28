"""Evaluation scenarios.

Each scenario is a scripted sequence of tool calls representing one caller
persona, paired with the outcome policy requires. This is the layer that can be
scored deterministically; the conversational quality of the voice agent itself
is scored separately by hand against `evals/rubric.md`.

Scenarios are data, not code, so adding a regression case for a bug found in
production is a matter of appending a dict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Step:
    """One tool call and the outcome the policy requires it to produce."""

    tool: str
    payload: dict[str, Any]
    expect_outcome: str
    expect_state: str | None = None
    #: Optional subset of `data` that must be present in the response.
    expect_data: dict[str, Any] = field(default_factory=dict)
    #: Expected HTTP status, for cases where rejection is the correct behaviour.
    expect_status: int = 200


@dataclass(frozen=True)
class Scenario:
    id: str
    persona: str
    #: What this case is protecting against, in one line.
    intent: str
    steps: list[Step]


RAJESH = {
    "full_name": "Rajesh Kumar",
    "date_of_birth": "1988-04-12",
    "pan": "ABCDE1234F",
}
PRIYA = {
    "full_name": "Priya Sharma",
    "date_of_birth": "1994-11-03",
    "pan": "BCDEF2345G",
}
IMRAN = {
    "full_name": "Imran Qureshi",
    "date_of_birth": "1991-07-21",
    "pan": "CDEFG3456H",
}
VIKRAM = {
    "full_name": "Vikram Anand",
    "date_of_birth": "1979-09-15",
    "pan": "EFGHI5678J",
}


SCENARIOS: list[Scenario] = [
    Scenario(
        id="S01",
        persona="Cooperative applicant, clean approval",
        intent="The happy path must complete and record consent.",
        steps=[
            Step("verify_identity", RAJESH, "ok", "identity_verified"),
            Step(
                "check_eligibility",
                {
                    "product_code": "personal_loan",
                    "requested_amount": 300_000,
                    "tenure_months": 36,
                    "declared_monthly_income": 95_000,
                    "employment_type": "salaried",
                },
                "ok",
                "eligibility_assessed",
                {"decision": "approved"},
            ),
            Step(
                "record_consent",
                {
                    "consent_type": "terms_and_conditions",
                    "granted": True,
                    "verbatim_response": "yes, I agree to the terms",
                },
                "ok",
                "completed",
            ),
        ],
    ),
    Scenario(
        id="S02",
        persona="Applicant asks for more than they can service",
        intent="An unaffordable request becomes a counter-offer, not a decline.",
        steps=[
            Step("verify_identity", PRIYA, "ok", "identity_verified"),
            Step(
                "check_eligibility",
                {
                    "product_code": "personal_loan",
                    "requested_amount": 1_500_000,
                    "tenure_months": 36,
                    "declared_monthly_income": 42_000,
                    "employment_type": "salaried",
                },
                "ok",
                "eligibility_assessed",
                {"decision": "approved"},
            ),
        ],
    ),
    Scenario(
        id="S03",
        persona="Applicant below the credit floor",
        intent="A decline is delivered without inventing an alternative offer.",
        steps=[
            Step("verify_identity", IMRAN, "ok", "identity_verified"),
            Step(
                "check_eligibility",
                {
                    "product_code": "personal_loan",
                    "requested_amount": 200_000,
                    "tenure_months": 24,
                    "declared_monthly_income": 60_000,
                    "employment_type": "self_employed",
                },
                "declined",
                "eligibility_assessed",
                {"decision": "declined", "approved_amount": 0},
            ),
        ],
    ),
    Scenario(
        id="S04",
        persona="Caller misheard by speech-to-text, then corrects themselves",
        intent="A wrong detail is recoverable within the attempt budget.",
        steps=[
            Step(
                "verify_identity",
                {**RAJESH, "date_of_birth": "1988-04-21"},
                "retry",
                "started",
                {"retries_remaining": 1},
            ),
            Step("verify_identity", RAJESH, "ok", "identity_verified"),
        ],
    ),
    Scenario(
        id="S05",
        persona="Caller cannot be verified at all",
        intent="Attempts are capped and the call is blocked, not looped forever.",
        steps=[
            Step("verify_identity", {**RAJESH, "date_of_birth": "1990-01-01"}, "retry"),
            Step("verify_identity", {**RAJESH, "date_of_birth": "1990-01-02"}, "blocked"),
            Step(
                "verify_identity",
                {**RAJESH, "date_of_birth": "1990-01-03"},
                "blocked",
                expect_status=403,
            ),
        ],
    ),
    Scenario(
        id="S06",
        persona="Sanctioned individual",
        intent="Never disclose the reason; route to a human.",
        steps=[
            Step("verify_identity", VIKRAM, "blocked", "started"),
        ],
    ),
    Scenario(
        id="S07",
        persona="Applicant declines the terms",
        intent="Refusal is recorded as evidence, not treated as a failure.",
        steps=[
            Step("verify_identity", RAJESH, "ok"),
            Step(
                "check_eligibility",
                {
                    "product_code": "personal_loan",
                    "requested_amount": 100_000,
                    "tenure_months": 24,
                    "declared_monthly_income": 95_000,
                    "employment_type": "salaried",
                },
                "ok",
            ),
            Step(
                "record_consent",
                {
                    "consent_type": "terms_and_conditions",
                    "granted": False,
                    "verbatim_response": "no, I need to think about it",
                },
                "declined",
                None,
                {"granted": False},
            ),
        ],
    ),
    Scenario(
        id="S08",
        persona="Applicant disputes the decision",
        intent="Disputes route to underwriting rather than being argued with.",
        steps=[
            Step("verify_identity", IMRAN, "ok"),
            Step(
                "check_eligibility",
                {
                    "product_code": "personal_loan",
                    "requested_amount": 200_000,
                    "tenure_months": 24,
                    "declared_monthly_income": 60_000,
                    "employment_type": "self_employed",
                },
                "declined",
            ),
            Step(
                "escalate",
                {
                    "reason_code": "customer_disputes_decision",
                    "summary": "Caller believes their score is wrong.",
                },
                "ok",
                "escalated",
                {"queue": "underwriting"},
            ),
        ],
    ),
    Scenario(
        id="S09",
        persona="Caller asks for a human immediately",
        intent="Escalation must be reachable from any live state.",
        steps=[
            Step(
                "escalate",
                {
                    "reason_code": "customer_requested_human",
                    "summary": "Caller asked for an agent before verification.",
                },
                "ok",
                "escalated",
            ),
        ],
    ),
    Scenario(
        id="S10",
        persona="Model skips verification and jumps to consent",
        intent="Out-of-order tool calls are refused by the service.",
        steps=[
            Step(
                "record_consent",
                {
                    "consent_type": "terms_and_conditions",
                    "granted": True,
                    "verbatim_response": "yes",
                },
                "rejected",
                expect_status=409,
            ),
        ],
    ),
    Scenario(
        id="S11",
        persona="Model continues talking after the call was escalated",
        intent="Terminal sessions reject further writes.",
        steps=[
            Step(
                "escalate",
                {"reason_code": "technical_failure", "summary": "Audio dropped."},
                "ok",
                "escalated",
            ),
            Step("verify_identity", RAJESH, "rejected", expect_status=409),
        ],
    ),
    Scenario(
        id="S12",
        persona="Platform retries a tool call after a timeout",
        intent="A retry must not consume a verification attempt.",
        steps=[
            Step(
                "verify_identity",
                {**RAJESH, "date_of_birth": "1990-01-01", "idempotency_key": "k-1"},
                "retry",
                None,
                {"retries_remaining": 1},
            ),
            Step(
                "verify_identity",
                {**RAJESH, "date_of_birth": "1990-01-01", "idempotency_key": "k-1"},
                "retry",
                None,
                {"retries_remaining": 1},
            ),
        ],
    ),
    Scenario(
        id="S13",
        persona="Speech-to-text emits a malformed PAN",
        intent="Garbled input asks the caller again rather than failing the call.",
        steps=[
            Step(
                "verify_identity",
                {**RAJESH, "pan": "NOTAPANAT"},
                "retry",
                expect_status=422,
            ),
        ],
    ),
    Scenario(
        id="S15",
        persona="Platform retries a hand-off that already succeeded",
        intent="A retried escalation returns the original ticket, not a second one.",
        steps=[
            Step(
                "escalate",
                {
                    "reason_code": "customer_requested_human",
                    "summary": "Caller asked for a person.",
                    "idempotency_key": "esc-1",
                },
                "ok",
                "escalated",
            ),
            Step(
                "escalate",
                {
                    "reason_code": "customer_requested_human",
                    "summary": "Caller asked for a person.",
                    "idempotency_key": "esc-1",
                },
                "ok",
                "escalated",
            ),
        ],
    ),
    Scenario(
        id="S14",
        persona="Declared income far exceeds the record",
        intent="Suspicious data is referred to a human, never auto-approved.",
        steps=[
            Step("verify_identity", PRIYA, "ok"),
            Step(
                "check_eligibility",
                {
                    "product_code": "personal_loan",
                    "requested_amount": 100_000,
                    "tenure_months": 24,
                    "declared_monthly_income": 250_000,
                    "employment_type": "salaried",
                },
                "ok",
                "eligibility_assessed",
                {"decision": "referred"},
            ),
        ],
    ),
]
