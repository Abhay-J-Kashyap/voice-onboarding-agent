"""Passcode delivery over email.

Mirrors `sms.py` deliberately: same shape, same rules — never log the code
except in the console sender, never raise, return quickly. Email carries no DLT
requirement in India, which is what makes it the practical channel for a demo
run by an individual developer rather than a registered business.

Resend's `onboarding@resend.dev` sandbox sender works with only an API key and
no domain verification, restricted to sending to the address the account
holder signed up with. That restriction is exactly right for a demo: no way to
accidentally email a stranger's inbox with a test passcode.
"""

from __future__ import annotations

import logging
import time
from typing import Protocol

import httpx

from app.config import get_settings
from app.observability import log_event, mask_email


class EmailSender(Protocol):
    """Anything that can deliver a passcode to an email address."""

    def send_passcode(self, *, email: str, code: str, ttl_minutes: int) -> bool:
        """Return True when the provider accepted the message for delivery."""
        ...


class ConsoleEmailSender:
    """Writes the passcode to the structured log instead of sending it."""

    def send_passcode(self, *, email: str, code: str, ttl_minutes: int) -> bool:
        log_event(
            "email_dispatched",
            level=logging.INFO,
            provider="console",
            email=mask_email(email),
            passcode=code,
            ttl_minutes=ttl_minutes,
        )
        return True


class ResendEmailSender:
    """Delivery through Resend's transactional email API.

    Retries follow the same rule as the MSG91 sender: one retry on a timeout or
    a 5xx, none on a 4xx, because a bad API key or an unverified recipient will
    not fix itself on a second attempt.
    """

    ENDPOINT = "https://api.resend.com/emails"

    def __init__(
        self,
        *,
        api_key: str,
        from_address: str,
        timeout_seconds: float = 3.0,
        max_retries: int = 1,
    ) -> None:
        self._api_key = api_key
        self._from = from_address
        self._timeout = timeout_seconds
        self._max_retries = max_retries

    def send_passcode(self, *, email: str, code: str, ttl_minutes: int) -> bool:
        payload = {
            "from": self._from,
            "to": [email],
            "subject": "Your Meridian Finance verification code",
            "text": (
                f"{code} is your Meridian Finance verification code.\n\n"
                f"It expires in {ttl_minutes} minutes. Never share it with anyone."
            ),
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        for attempt in range(self._max_retries + 1):
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    response = client.post(
                        self.ENDPOINT, json=payload, headers=headers
                    )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                log_event(
                    "email_dispatch_failed",
                    level=logging.WARNING,
                    provider="resend",
                    email=mask_email(email),
                    attempt=attempt,
                    error_type=type(exc).__name__,
                )
                if attempt < self._max_retries:
                    time.sleep(0.25 * (attempt + 1))
                    continue
                return False

            if response.is_success:
                log_event(
                    "email_dispatched",
                    provider="resend",
                    email=mask_email(email),
                    status_code=response.status_code,
                )
                return True

            if response.is_server_error and attempt < self._max_retries:
                log_event(
                    "email_dispatch_retrying",
                    level=logging.WARNING,
                    provider="resend",
                    email=mask_email(email),
                    status_code=response.status_code,
                )
                time.sleep(0.25 * (attempt + 1))
                continue

            # 4xx: bad key, unverified recipient, malformed address. No retry.
            log_event(
                "email_dispatch_failed",
                level=logging.ERROR,
                provider="resend",
                email=mask_email(email),
                status_code=response.status_code,
            )
            return False

        return False


def _build_sender() -> EmailSender:
    settings = get_settings()
    if settings.email_provider == "resend":
        missing = [
            name
            for name, value in (
                ("RESEND_API_KEY", settings.resend_api_key),
                ("RESEND_FROM_EMAIL", settings.resend_from_email),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"EMAIL_PROVIDER=resend requires: {', '.join(missing)}")
        return ResendEmailSender(
            api_key=settings.resend_api_key,
            from_address=settings.resend_from_email,
            timeout_seconds=settings.email_timeout_seconds,
            max_retries=settings.email_max_retries,
        )
    return ConsoleEmailSender()


_sender: EmailSender | None = None


def get_email_sender() -> EmailSender:
    global _sender
    if _sender is None:
        _sender = _build_sender()
    return _sender


def set_email_sender(sender: EmailSender) -> None:
    """Override the sender. Used by tests to assert on delivery."""
    global _sender
    _sender = sender
