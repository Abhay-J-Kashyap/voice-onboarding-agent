"""Request and response contracts for the tool API.

Two conventions matter for a voice agent:

1. Every response uses the same envelope, with an `agent_message` written to be
   spoken. Short sentences, no markdown, no digits the model has to reformat.
2. Inputs are validated strictly at the edge. Speech-to-text output is noisy and
   a malformed PAN should fail here with a recoverable message rather than
   halfway through a database call.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import EligibilityDecision, SessionState

PAN_PATTERN = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")

#: Outcome vocabulary shared by every tool. The agent branches on this field.
#: `otp_sent` is distinct from `ok` so the model cannot mistake a located record
#: for a verified caller — the two mean very different things.
Outcome = Literal[
    "ok",
    "otp_sent",
    # No account exists for this PAN. Distinct from `retry` because the caller
    # did nothing wrong — they are a prospect, not a failed customer, and the
    # conversation should turn into acquisition rather than another attempt.
    "not_registered",
    "retry",
    "declined",
    "rejected",
    "blocked",
    "error",
]

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ToolResponse(BaseModel):
    """Uniform envelope returned by every tool endpoint."""

    outcome: Outcome
    agent_message: str = Field(
        description="Speakable sentence for the agent to deliver verbatim."
    )
    session_state: SessionState | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = None


class StartSessionRequest(BaseModel):
    external_call_id: str | None = Field(default=None, max_length=64)
    channel: Literal["voice", "chat"] = "voice"
    language: str = Field(default="en-IN", max_length=10)


class StartSessionResponse(BaseModel):
    session_id: str
    state: SessionState
    agent_message: str


class VerifyIdentityRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    session_id: str
    full_name: str = Field(min_length=2, max_length=120)
    date_of_birth: date
    # Deliberately wider than a PAN: speech-to-text emits "abcde 1234 f" with
    # spaces and separators, so the field must accept the raw utterance and let
    # the validator below normalise it before checking shape.
    pan: str = Field(min_length=10, max_length=30)
    idempotency_key: str | None = Field(default=None, max_length=64)

    @field_validator("pan")
    @classmethod
    def normalise_pan(cls, value: str) -> str:
        # Callers spell PANs aloud; casing, spacing and hyphens vary.
        candidate = "".join(c for c in value if c.isalnum()).upper()
        if not PAN_PATTERN.match(candidate):
            raise ValueError("PAN must match five letters, four digits, one letter")
        return candidate

    @field_validator("date_of_birth")
    @classmethod
    def must_be_adult(cls, value: date) -> date:
        today = date.today()
        age = today.year - value.year - ((today.month, today.day) < (value.month, value.day))
        if age < 18:
            raise ValueError("applicant must be at least 18 years old")
        if age > 100:
            raise ValueError("date of birth is out of the accepted range")
        return value


class VerifyOtpRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    session_id: str
    # Wider than the passcode: speech-to-text renders spoken digits with spaces
    # and hyphens, and the service strips them before comparison.
    code: str = Field(min_length=4, max_length=20)
    idempotency_key: str | None = Field(default=None, max_length=64)


class ResendOtpRequest(BaseModel):
    session_id: str
    idempotency_key: str | None = Field(default=None, max_length=64)


class CaptureLeadRequest(BaseModel):
    """Details stated by someone with no existing account.

    Every field here is self-asserted. Nothing in this request has been checked
    against anything, which is why it produces a lead for follow-up and never a
    credit decision.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    session_id: str
    full_name: str = Field(min_length=2, max_length=120)
    date_of_birth: date
    pan: str = Field(min_length=10, max_length=30)
    email: str = Field(min_length=5, max_length=255)
    phone: str | None = Field(default=None, max_length=20)
    product_interest: Literal["personal_loan", "credit_card"] = "personal_loan"
    stated_monthly_income: int | None = Field(default=None, gt=0, le=10_000_000)
    idempotency_key: str | None = Field(default=None, max_length=64)

    @field_validator("pan")
    @classmethod
    def normalise_pan(cls, value: str) -> str:
        candidate = "".join(c for c in value if c.isalnum()).upper()
        if not PAN_PATTERN.match(candidate):
            raise ValueError("PAN must match five letters, four digits, one letter")
        return candidate

    @field_validator("email")
    @classmethod
    def check_email(cls, value: str) -> str:
        candidate = value.replace(" ", "").lower()
        if not EMAIL_PATTERN.match(candidate):
            raise ValueError("email address is not valid")
        return candidate

    @field_validator("date_of_birth")
    @classmethod
    def must_be_adult(cls, value: date) -> date:
        today = date.today()
        age = today.year - value.year - (
            (today.month, today.day) < (value.month, value.day)
        )
        if age < 18:
            raise ValueError("applicant must be at least 18 years old")
        if age > 100:
            raise ValueError("date of birth is out of the accepted range")
        return value


class CheckEligibilityRequest(BaseModel):
    session_id: str
    product_code: Literal["personal_loan", "credit_card"] = "personal_loan"
    requested_amount: int = Field(gt=0, le=5_000_000)
    # Ten years. Personal loans routinely run this long at larger ticket sizes,
    # and a caller asking for a decade should get an offer or a decline on the
    # merits, not a validation error they cannot act on.
    tenure_months: int = Field(ge=6, le=120)
    declared_monthly_income: int = Field(gt=0, le=10_000_000)
    employment_type: Literal["salaried", "self_employed", "unemployed"]
    idempotency_key: str | None = Field(default=None, max_length=64)


class EligibilityData(BaseModel):
    decision: EligibilityDecision
    approved_amount: int
    interest_rate: float
    monthly_instalment: int
    reasons: list[str]
    policy_version: str


class RecordConsentRequest(BaseModel):
    session_id: str
    consent_type: Literal["terms_and_conditions", "credit_bureau_pull", "data_processing"]
    granted: bool
    verbatim_response: str = Field(min_length=1, max_length=500)
    disclosure_version: str = Field(default="v1.0.0", max_length=20)
    idempotency_key: str | None = Field(default=None, max_length=64)


class EscalateRequest(BaseModel):
    session_id: str
    reason_code: Literal[
        "identity_verification_failed",
        "customer_disputes_decision",
        "out_of_scope_request",
        "customer_requested_human",
        "low_confidence_transcription",
        "technical_failure",
    ]
    summary: str = Field(min_length=1, max_length=1000)
    idempotency_key: str | None = Field(default=None, max_length=64)


#: What to say when a field fails validation.
#:
#: A schema rejection is usually the caller being misheard or asking for
#: something outside policy, and both are recoverable — but only if the agent is
#: told *which* field was wrong and what would be acceptable. Returning a
#: generic "sorry, say that again" makes the model retry the same bad value,
#: fail identically, and escalate a call that never needed a human.
#:
#: Phrased to be spoken. Kept beside the field definitions above so the two
#: cannot drift apart; `test_validation_guidance_covers_every_field` enforces it.
FIELD_GUIDANCE: dict[str, str] = {
    "full_name": "I need your full name as it appears on your PAN card",
    "date_of_birth": "I need your date of birth, including the year",
    "pan": (
        "a PAN is ten characters, five letters then four digits then one letter"
    ),
    "code": "the passcode is six digits",
    "product_code": "I can help with a personal loan or a credit card",
    "requested_amount": (
        "the amount needs to be between one thousand and fifty lakh rupees"
    ),
    "tenure_months": (
        "the repayment period needs to be between six months and ten years"
    ),
    "declared_monthly_income": "I need your monthly income as a number",
    "employment_type": (
        "I need to know if you are salaried, self-employed, or not currently working"
    ),
    "consent_type": "I need to record which agreement you are giving",
    "granted": "I need a clear yes or no",
    "verbatim_response": "I need to record what you said",
    "email": "I need an email address I can send your application link to",
    "phone": "I need a mobile number, ten digits",
    "product_interest": "I can help with a personal loan or a credit card",
    "stated_monthly_income": "I need your monthly income as a number",
    "reason_code": "I need a reason for the transfer",
    "summary": "I need a short summary for my colleague",
}


def validation_message(fields: list[str]) -> str:
    """Compose a speakable recovery message naming what was wrong.

    Falls back to a generic prompt for fields with no guidance — chiefly
    `session_id` and `idempotency_key`, which are the platform's business and
    never something to raise with a caller.
    """
    guidance = [FIELD_GUIDANCE[f] for f in fields if f in FIELD_GUIDANCE]
    if not guidance:
        return "Sorry, I did not catch that correctly. Could you say it once more?"
    if len(guidance) == 1:
        return f"Sorry, {guidance[0]}. Could you give me that again?"
    joined = "; and ".join(guidance) if len(guidance) == 2 else (
        ", ".join(guidance[:-1]) + "; and " + guidance[-1]
    )
    return f"Sorry, {joined}. Could you give me those again?"


class ToolCallView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    tool_name: str
    outcome: str
    latency_ms: float
    trace_id: str
    created_at: Any


class SessionAuditView(BaseModel):
    """Everything needed to replay a call, with PII already redacted."""

    session_id: str
    state: SessionState
    language: str
    identity_attempts: int
    otp_resends: int
    otp_challenges: list[dict[str, Any]]
    customer_reference: str | None
    tool_calls: list[ToolCallView]
    eligibility: EligibilityData | None
    consents: list[dict[str, Any]]
    escalation: dict[str, Any] | None
    lead: dict[str, Any] | None = None
