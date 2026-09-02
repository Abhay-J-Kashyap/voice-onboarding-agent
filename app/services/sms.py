"""Passcode delivery.

The interface passes the passcode and its context rather than a finished
message, which is a deliberate choice for the Indian market. TRAI's DLT regime
means commercial SMS is sent as a *registered template id plus variables*, not
as free text — a sender that accepted a rendered string could not talk to MSG91,
Gupshup or Kaleyra without unpicking it again. Providers that do accept free
text (Twilio internationally) can render it themselves.

Implementations must satisfy three rules:

* **Never log the passcode**, except the console sender, where logging *is* the
  delivery mechanism.
* **Never raise.** Return False and let the caller route to a human. A provider
  outage should degrade the call, not crash it.
* **Return quickly.** The voice platform abandons a tool call after ten seconds,
  so the timeout and retry budget here is deliberately tight.
"""

from __future__ import annotations

import logging
import time
from typing import Protocol

import httpx

from app.config import get_settings
from app.observability import log_event, mask_phone


class SmsSender(Protocol):
    """Anything that can deliver a passcode to a phone number."""

    def send_passcode(self, *, phone: str, code: str, ttl_minutes: int) -> bool:
        """Return True when the provider accepted the message for delivery."""
        ...


class ConsoleSmsSender:
    """Writes the passcode to the structured log instead of sending it.

    Used for local development and for the hosted demo, where no provider is
    wired up. This is the only sender permitted to log the code, because here
    the log *is* the delivery channel.
    """

    def send_passcode(self, *, phone: str, code: str, ttl_minutes: int) -> bool:
        log_event(
            "sms_dispatched",
            level=logging.INFO,
            provider="console",
            phone=mask_phone(phone),
            passcode=code,
            ttl_minutes=ttl_minutes,
        )
        return True


class Msg91SmsSender:
    """Delivery through MSG91's flow API.

    Requires a DLT-registered template whose variables match `var_code` and
    `var_ttl`. The template id, sender id and auth key all come from the MSG91
    console after DLT registration completes.

    Retries are bounded to one attempt on a timeout or a 5xx, because the call
    is happening while a customer waits on the line. A 4xx is a configuration
    error — a bad template id or an unregistered sender — and retrying it would
    burn the caller's patience to no purpose, so it fails immediately.
    """

    ENDPOINT = "https://control.msg91.com/api/v5/flow/"

    def __init__(
        self,
        *,
        auth_key: str,
        sender_id: str,
        template_id: str,
        timeout_seconds: float = 3.0,
        max_retries: int = 1,
    ) -> None:
        self._auth_key = auth_key
        self._sender_id = sender_id
        self._template_id = template_id
        self._timeout = timeout_seconds
        self._max_retries = max_retries

    def _normalise(self, phone: str) -> str:
        """MSG91 expects a bare country-code-prefixed number with no plus."""
        digits = "".join(c for c in phone if c.isdigit())
        return digits if digits.startswith("91") else f"91{digits}"

    def send_passcode(self, *, phone: str, code: str, ttl_minutes: int) -> bool:
        payload = {
            "template_id": self._template_id,
            "sender": self._sender_id,
            "short_url": "0",
            "recipients": [
                {
                    "mobiles": self._normalise(phone),
                    "var_code": code,
                    "var_ttl": str(ttl_minutes),
                }
            ],
        }
        headers = {"authkey": self._auth_key, "content-type": "application/json"}

        for attempt in range(self._max_retries + 1):
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    response = client.post(
                        self.ENDPOINT, json=payload, headers=headers
                    )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                # Transport problems are worth one more try.
                log_event(
                    "sms_dispatch_failed",
                    level=logging.WARNING,
                    provider="msg91",
                    phone=mask_phone(phone),
                    attempt=attempt,
                    error_type=type(exc).__name__,
                )
                if attempt < self._max_retries:
                    time.sleep(0.25 * (attempt + 1))
                    continue
                return False

            if response.is_success:
                log_event(
                    "sms_dispatched",
                    provider="msg91",
                    phone=mask_phone(phone),
                    # The passcode is deliberately absent from this line.
                    status_code=response.status_code,
                )
                return True

            if response.is_server_error and attempt < self._max_retries:
                log_event(
                    "sms_dispatch_retrying",
                    level=logging.WARNING,
                    provider="msg91",
                    phone=mask_phone(phone),
                    status_code=response.status_code,
                )
                time.sleep(0.25 * (attempt + 1))
                continue

            # 4xx: a misconfigured template, sender or key. Retrying cannot help.
            log_event(
                "sms_dispatch_failed",
                level=logging.ERROR,
                provider="msg91",
                phone=mask_phone(phone),
                status_code=response.status_code,
            )
            return False

        return False


def _build_sender() -> SmsSender:
    """Select a sender from configuration, defaulting to the console."""
    settings = get_settings()
    if settings.sms_provider == "msg91":
        missing = [
            name
            for name, value in (
                ("MSG91_AUTH_KEY", settings.msg91_auth_key),
                ("MSG91_SENDER_ID", settings.msg91_sender_id),
                ("MSG91_TEMPLATE_ID", settings.msg91_template_id),
            )
            if not value
        ]
        if missing:
            # Fail loudly at startup rather than silently on the first call.
            raise RuntimeError(
                f"SMS_PROVIDER=msg91 requires: {', '.join(missing)}"
            )
        return Msg91SmsSender(
            auth_key=settings.msg91_auth_key,
            sender_id=settings.msg91_sender_id,
            template_id=settings.msg91_template_id,
            timeout_seconds=settings.sms_timeout_seconds,
            max_retries=settings.sms_max_retries,
        )
    return ConsoleSmsSender()


_sender: SmsSender | None = None


def get_sms_sender() -> SmsSender:
    global _sender
    if _sender is None:
        _sender = _build_sender()
    return _sender


def set_sms_sender(sender: SmsSender) -> None:
    """Override the sender. Used by tests to assert on delivery."""
    global _sender
    _sender = sender