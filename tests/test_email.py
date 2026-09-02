"""Email delivery behaviour.

Structured identically to test_sms.py: no network, bounded retries, nothing
raises, the passcode never reaches a log line except in the console sender.
A second block tests that `issue_challenge` actually dispatches to the
configured channel — this is the part that would silently mis-route a
passcode if the two services drifted apart.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.services.email import ConsoleEmailSender, ResendEmailSender


class FakeTransport(httpx.BaseTransport):
    def __init__(self, *behaviours) -> None:
        self.behaviours = list(behaviours)
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        behaviour = (
            self.behaviours.pop(0) if self.behaviours else httpx.Response(200)
        )
        if isinstance(behaviour, Exception):
            raise behaviour
        return behaviour


@pytest.fixture
def sender_factory(monkeypatch):
    def build(*behaviours, max_retries: int = 1) -> tuple[ResendEmailSender, FakeTransport]:
        transport = FakeTransport(*behaviours)
        original = httpx.Client

        def patched(*args, **kwargs):
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        monkeypatch.setattr(httpx, "Client", patched)
        sender = ResendEmailSender(
            api_key="key",
            from_address="Meridian Finance <onboarding@resend.dev>",
            timeout_seconds=0.5,
            max_retries=max_retries,
        )
        return sender, transport

    return build


def logged_values(caplog) -> str:
    parts = [record.getMessage() for record in caplog.records]
    for record in caplog.records:
        context = getattr(record, "context", None)
        if isinstance(context, dict):
            parts.extend(str(value) for value in context.values())
    return " ".join(parts)


def test_successful_send_returns_true(sender_factory):
    sender, transport = sender_factory(httpx.Response(200))
    assert sender.send_passcode(email="rajesh@example.com", code="123456", ttl_minutes=5)
    assert len(transport.requests) == 1


def test_payload_carries_the_code_and_ttl(sender_factory):
    sender, transport = sender_factory(httpx.Response(200))
    sender.send_passcode(email="rajesh@example.com", code="654321", ttl_minutes=5)

    body = json.loads(transport.requests[0].content)
    assert body["to"] == ["rajesh@example.com"]
    assert "654321" in body["text"]
    assert "5 minutes" in body["text"]


def test_server_error_is_retried_once(sender_factory):
    sender, transport = sender_factory(
        httpx.Response(503), httpx.Response(200), max_retries=1
    )
    assert sender.send_passcode(email="rajesh@example.com", code="123456", ttl_minutes=5)
    assert len(transport.requests) == 2


def test_client_error_is_not_retried(sender_factory):
    """An unverified recipient or bad key will not fix itself; fail fast."""
    sender, transport = sender_factory(httpx.Response(422), max_retries=1)
    assert not sender.send_passcode(
        email="rajesh@example.com", code="123456", ttl_minutes=5
    )
    assert len(transport.requests) == 1


def test_timeout_is_retried_then_gives_up(sender_factory):
    sender, transport = sender_factory(
        httpx.TimeoutException("slow"),
        httpx.TimeoutException("slow again"),
        max_retries=1,
    )
    assert not sender.send_passcode(
        email="rajesh@example.com", code="123456", ttl_minutes=5
    )
    assert len(transport.requests) == 2


def test_transport_failure_never_raises(sender_factory):
    sender, _ = sender_factory(httpx.ConnectError("no route"), max_retries=0)
    assert (
        sender.send_passcode(email="rajesh@example.com", code="123456", ttl_minutes=5)
        is False
    )


def test_provider_sender_never_logs_the_passcode(sender_factory, caplog):
    sender, _ = sender_factory(httpx.Response(200))
    with caplog.at_level("INFO"):
        sender.send_passcode(email="rajesh@example.com", code="987654", ttl_minutes=5)
    assert "987654" not in logged_values(caplog)


def test_provider_sender_masks_the_email_in_logs(sender_factory, caplog):
    sender, _ = sender_factory(httpx.Response(200))
    with caplog.at_level("INFO"):
        sender.send_passcode(email="rajesh@example.com", code="987654", ttl_minutes=5)
    logged = logged_values(caplog)
    assert "rajesh@example.com" not in logged
    assert "ra***" in logged


def test_console_sender_does_log_the_passcode(caplog):
    with caplog.at_level("INFO"):
        assert ConsoleEmailSender().send_passcode(
            email="rajesh@example.com", code="222333", ttl_minutes=5
        )
    assert "222333" in logged_values(caplog)


def test_resend_requires_full_configuration(monkeypatch):
    from app.config import Settings
    from app.services import email

    monkeypatch.setattr(
        email, "get_settings", lambda: Settings(email_provider="resend", resend_api_key="")
    )
    with pytest.raises(RuntimeError) as excinfo:
        email._build_sender()
    assert "RESEND_API_KEY" in str(excinfo.value)


def test_console_is_the_default_email_provider(monkeypatch):
    from app.config import Settings
    from app.services import email

    monkeypatch.setattr(email, "get_settings", lambda: Settings())
    assert isinstance(email._build_sender(), ConsoleEmailSender)


def test_mask_email_keeps_recognisability_hides_the_rest():
    from app.observability import mask_email

    assert mask_email("rajesh.kumar@example.com") == "ra***@ex***.com"
    assert mask_email(None) is None
    assert mask_email("a@b.co") == "a***@b***.co"


# --- Channel dispatch: does issue_challenge actually route to the configured
# channel? This is the seam most likely to silently misroute a passcode if the
# email and SMS services drift apart from each other. ---


class CapturingEmailSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, int]] = []

    def send_passcode(self, *, email: str, code: str, ttl_minutes: int) -> bool:
        self.sent.append((email, code, ttl_minutes))
        return True


def test_issue_challenge_routes_to_email_when_configured(db_session, monkeypatch):
    from app.config import Settings
    from app.models import Customer, OnboardingSession, SessionState
    from app.services import email as email_module
    from app.services import otp as otp_module

    settings = Settings(otp_delivery_channel="email", otp_demo_mode=True)
    monkeypatch.setattr(otp_module, "get_settings", lambda: settings)

    captured = CapturingEmailSender()
    monkeypatch.setattr(email_module, "_sender", captured)

    customer = Customer(
        full_name="Test Person",
        date_of_birth="1990-01-01",
        pan="ZZZZZ9999Z",
        phone="+919800000000",
        email="test@example.com",
        monthly_income=50_000,
        employment_type="salaried",
        existing_emi=0,
        credit_score=750,
    )
    db_session.add(customer)
    db_session.flush()

    session = OnboardingSession(
        id="sess-email-test",
        channel="voice",
        language="en-IN",
        state=SessionState.STARTED,
    )
    db_session.add(session)
    db_session.flush()

    result = otp_module.issue_challenge(db_session, session=session, customer=customer)

    assert result.outcome is otp_module.IssueOutcome.SENT
    assert result.masked_email is not None
    assert result.masked_phone is None
    assert len(captured.sent) == 1
    assert captured.sent[0][0] == "test@example.com"


def test_issue_challenge_fails_cleanly_when_customer_has_no_email(
    db_session, monkeypatch
):
    from app.config import Settings
    from app.models import Customer, OnboardingSession, SessionState
    from app.services import otp as otp_module

    settings = Settings(otp_delivery_channel="email")
    monkeypatch.setattr(otp_module, "get_settings", lambda: settings)

    customer = Customer(
        full_name="No Email Person",
        date_of_birth="1990-01-01",
        pan="YYYYY8888Y",
        phone="+919800000099",
        email=None,
        monthly_income=50_000,
        employment_type="salaried",
        existing_emi=0,
        credit_score=750,
    )
    db_session.add(customer)
    db_session.flush()

    session = OnboardingSession(
        id="sess-no-email-test",
        channel="voice",
        language="en-IN",
        state=SessionState.STARTED,
    )
    db_session.add(session)
    db_session.flush()

    result = otp_module.issue_challenge(db_session, session=session, customer=customer)
    assert result.outcome is otp_module.IssueOutcome.DELIVERY_FAILED
