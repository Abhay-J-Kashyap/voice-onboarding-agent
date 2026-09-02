"""SMS delivery behaviour.

These tests never touch the network. They assert the properties that matter when
a provider misbehaves while a customer is waiting on the line: bounded retries,
no exceptions escaping, and the passcode never appearing in a log line.
"""

from __future__ import annotations

import httpx
import pytest

from app.services.sms import ConsoleSmsSender, Msg91SmsSender


class FakeTransport(httpx.BaseTransport):
    """Returns scripted responses, recording every request it receives."""

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
    """Build an Msg91SmsSender whose HTTP client uses a scripted transport."""

    def build(*behaviours, max_retries: int = 1) -> tuple[Msg91SmsSender, FakeTransport]:
        transport = FakeTransport(*behaviours)
        original = httpx.Client

        def patched(*args, **kwargs):
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        monkeypatch.setattr(httpx, "Client", patched)
        sender = Msg91SmsSender(
            auth_key="key",
            sender_id="MERIDN",
            template_id="tmpl-1",
            timeout_seconds=0.5,
            max_retries=max_retries,
        )
        return sender, transport

    return build


def test_successful_send_returns_true(sender_factory):
    sender, transport = sender_factory(httpx.Response(200, json={"type": "success"}))
    assert sender.send_passcode(phone="+919800000001", code="123456", ttl_minutes=5)
    assert len(transport.requests) == 1


def test_payload_carries_template_variables_not_prose(sender_factory):
    """DLT templates are filled by the provider, so we send variables."""
    sender, transport = sender_factory(httpx.Response(200))
    sender.send_passcode(phone="+919800000001", code="654321", ttl_minutes=5)

    import json

    body = json.loads(transport.requests[0].content)
    assert body["template_id"] == "tmpl-1"
    assert body["sender"] == "MERIDN"
    recipient = body["recipients"][0]
    assert recipient["var_code"] == "654321"
    assert recipient["var_ttl"] == "5"


def test_phone_is_normalised_for_the_provider(sender_factory):
    sender, transport = sender_factory(httpx.Response(200))
    sender.send_passcode(phone="+91 98000-00001", code="111111", ttl_minutes=5)

    import json

    body = json.loads(transport.requests[0].content)
    assert body["recipients"][0]["mobiles"] == "919800000001"


def test_server_error_is_retried_once(sender_factory):
    sender, transport = sender_factory(
        httpx.Response(503), httpx.Response(200), max_retries=1
    )
    assert sender.send_passcode(phone="+919800000001", code="123456", ttl_minutes=5)
    assert len(transport.requests) == 2


def test_client_error_is_not_retried(sender_factory):
    """A bad template or sender id will not fix itself; fail fast."""
    sender, transport = sender_factory(httpx.Response(400), max_retries=1)
    assert not sender.send_passcode(
        phone="+919800000001", code="123456", ttl_minutes=5
    )
    assert len(transport.requests) == 1


def test_timeout_is_retried_then_gives_up(sender_factory):
    sender, transport = sender_factory(
        httpx.TimeoutException("slow"),
        httpx.TimeoutException("slow again"),
        max_retries=1,
    )
    assert not sender.send_passcode(
        phone="+919800000001", code="123456", ttl_minutes=5
    )
    assert len(transport.requests) == 2


def test_transport_failure_never_raises(sender_factory):
    """The caller routes to a human on False; an exception would crash the call."""
    sender, _ = sender_factory(httpx.ConnectError("no route"), max_retries=0)
    assert (
        sender.send_passcode(phone="+919800000001", code="123456", ttl_minutes=5)
        is False
    )


def test_retry_budget_is_bounded(sender_factory):
    """Worst case must stay inside the voice platform's tool timeout."""
    sender, transport = sender_factory(
        httpx.Response(500), httpx.Response(500), httpx.Response(500), max_retries=1
    )
    sender.send_passcode(phone="+919800000001", code="123456", ttl_minutes=5)
    assert len(transport.requests) == 2


def logged_values(caplog) -> str:
    """Flatten every structured field the logger emitted.

    `caplog.text` only holds the message, not the context dictionary the JSON
    formatter renders — asserting against it would pass whatever the code did.
    """
    parts = [record.getMessage() for record in caplog.records]
    for record in caplog.records:
        context = getattr(record, "context", None)
        if isinstance(context, dict):
            parts.extend(str(value) for value in context.values())
    return " ".join(parts)


def test_provider_sender_never_logs_the_passcode(sender_factory, caplog):
    sender, _ = sender_factory(httpx.Response(200))
    with caplog.at_level("INFO"):
        sender.send_passcode(phone="+919800000001", code="987654", ttl_minutes=5)
    assert "987654" not in logged_values(caplog)


def test_provider_sender_never_logs_the_passcode_on_failure(sender_factory, caplog):
    sender, _ = sender_factory(httpx.Response(400))
    with caplog.at_level("INFO"):
        sender.send_passcode(phone="+919800000001", code="987654", ttl_minutes=5)
    assert "987654" not in logged_values(caplog)


def test_provider_sender_masks_the_phone_number(sender_factory, caplog):
    sender, _ = sender_factory(httpx.Response(200))
    with caplog.at_level("INFO"):
        sender.send_passcode(phone="+919800000001", code="987654", ttl_minutes=5)
    logged = logged_values(caplog)
    assert "919800000001" not in logged
    assert "0001" in logged


def test_console_sender_does_log_the_passcode(caplog):
    """The console sender is the exception: here the log is the delivery channel."""
    with caplog.at_level("INFO"):
        assert ConsoleSmsSender().send_passcode(
            phone="+919800000001", code="222333", ttl_minutes=5
        )
    assert "222333" in logged_values(caplog)


def test_msg91_requires_full_configuration(monkeypatch):
    """Missing credentials fail at startup, not on the first customer call."""
    from app.config import Settings
    from app.services import sms

    monkeypatch.setattr(
        sms, "get_settings", lambda: Settings(sms_provider="msg91", msg91_auth_key="k")
    )
    with pytest.raises(RuntimeError) as excinfo:
        sms._build_sender()
    assert "MSG91_SENDER_ID" in str(excinfo.value)
    assert "MSG91_TEMPLATE_ID" in str(excinfo.value)


def test_console_is_the_default_provider(monkeypatch):
    from app.config import Settings
    from app.services import sms

    monkeypatch.setattr(sms, "get_settings", lambda: Settings())
    assert isinstance(sms._build_sender(), ConsoleSmsSender)
