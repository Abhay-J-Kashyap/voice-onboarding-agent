"""Passcode delivery.

The interface is deliberately narrow — one method, one message — because the
only reason it exists is so a real provider can replace the console
implementation without touching the passcode logic. Swapping in MSG91, Twilio,
Gupshup or Kaleyra means writing one class and changing one line in
`get_sms_sender`.

Delivery is treated as best-effort and never blocks the passcode from being
issued: a provider outage should degrade to "I could not send that, let me pass
you to a colleague", not lose the challenge record.
"""

from __future__ import annotations

import logging
from typing import Protocol

from app.observability import log_event, mask_phone


class SmsSender(Protocol):
    """Anything that can deliver a short message to a phone number."""

    def send(self, *, phone: str, message: str) -> bool:
        """Return True when the provider accepted the message for delivery."""
        ...


class ConsoleSmsSender:
    """Writes the message to the structured log instead of sending it.

    Used for local development and for the hosted demo, where no SMS provider
    is wired up. The passcode is logged so a developer can complete a test call;
    this is why `otp_demo_mode` must never be enabled in a real deployment.
    """

    def send(self, *, phone: str, message: str) -> bool:
        log_event(
            "sms_dispatched",
            level=logging.INFO,
            provider="console",
            phone=mask_phone(phone),
            body=message,
        )
        return True


_sender: SmsSender = ConsoleSmsSender()


def get_sms_sender() -> SmsSender:
    return _sender


def set_sms_sender(sender: SmsSender) -> None:
    """Override the sender. Used by tests to assert on delivery."""
    global _sender
    _sender = sender
