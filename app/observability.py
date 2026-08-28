"""Structured logging, trace propagation and latency measurement.

Voice agents fail in ways that are only diagnosable after the fact: the call is
over, the caller is gone, and all that remains is what was written down. Every
request therefore emits a single structured line carrying a trace id, the tool
that ran, the outcome, and the wall-clock latency, with PII redacted at the
point of logging rather than at the point of ingestion.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

TRACE_ID: ContextVar[str] = ContextVar("trace_id", default="-")
SESSION_ID: ContextVar[str] = ContextVar("session_id", default="-")

TRACE_HEADER = "x-trace-id"


def mask_pan(pan: str | None) -> str | None:
    """Redact a PAN to its shape, keeping only enough to correlate support calls."""
    if not pan:
        return pan
    if len(pan) < 6:
        return "*" * len(pan)
    return f"{pan[:2]}{'*' * (len(pan) - 5)}{pan[-3:]}"


def mask_phone(phone: str | None) -> str | None:
    if not phone:
        return phone
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) < 4:
        return "*" * len(digits)
    return f"{'*' * (len(digits) - 4)}{digits[-4:]}"


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per line so logs are queryable without parsing rules."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": TRACE_ID.get(),
            "session_id": SESSION_ID.get(),
        }
        extra = getattr(record, "context", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # Uvicorn's own access log would duplicate what the middleware already emits.
    logging.getLogger("uvicorn.access").disabled = True


logger = logging.getLogger("kyc_agent")


def log_event(message: str, level: int = logging.INFO, **context: Any) -> None:
    """Log a structured event. Keyword arguments become top-level JSON fields."""
    logger.log(level, message, extra={"context": context})


class TracingMiddleware(BaseHTTPMiddleware):
    """Assign a trace id to every request and record its latency.

    The voice platform may supply its own call identifier via the trace header;
    when it does we adopt it, so a single id spans the telephony leg and every
    downstream tool call.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        trace_id = request.headers.get(TRACE_HEADER) or uuid.uuid4().hex
        token = TRACE_ID.set(trace_id)
        session_token = SESSION_ID.set("-")
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers[TRACE_HEADER] = trace_id
            return response
        finally:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            log_event(
                "http_request",
                method=request.method,
                path=request.url.path,
                status_code=status_code,
                latency_ms=elapsed_ms,
            )
            TRACE_ID.reset(token)
            SESSION_ID.reset(session_token)
