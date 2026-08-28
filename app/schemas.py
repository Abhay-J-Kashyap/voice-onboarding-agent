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
Outcome = Literal["ok", "retry", "declined", "rejected", "blocked", "error"]


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


class CheckEligibilityRequest(BaseModel):
    session_id: str
    product_code: Literal["personal_loan", "credit_card"] = "personal_loan"
    requested_amount: int = Field(gt=0, le=5_000_000)
    tenure_months: int = Field(ge=6, le=84)
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
    customer_reference: str | None
    tool_calls: list[ToolCallView]
    eligibility: EligibilityData | None
    consents: list[dict[str, Any]]
    escalation: dict[str, Any] | None
