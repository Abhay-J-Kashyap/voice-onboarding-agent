"""Application entry point.

The exception handlers are the interesting part. A tool call that fails must
still return something the agent can say out loud — a bare 500 leaves the model
improvising to a caller, which is exactly how a compliance incident starts.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db import init_db
from app.errors import ToolError
from app.observability import TRACE_ID, TracingMiddleware, configure_logging, log_event
from app.routers import admin, tools
from app.schemas import validation_message

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.log_level)
    init_db()
    log_event("service_started", environment=settings.environment)
    yield
    log_event("service_stopping")


app = FastAPI(
    title="KYC onboarding tool service",
    description=(
        "Backend tools for a voice onboarding agent: identity verification, "
        "eligibility, consent capture and human hand-off."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(TracingMiddleware)
app.include_router(admin.router)
app.include_router(tools.router)


@app.exception_handler(ToolError)
async def tool_error_handler(request: Request, exc: ToolError) -> JSONResponse:
    """Return domain failures in the same envelope as successes."""
    log_event(
        "tool_error",
        level=logging.WARNING,
        code=exc.code,
        path=request.url.path,
        **exc.details,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "outcome": exc.outcome,
            "agent_message": exc.agent_message,
            "data": {"code": exc.code},
            "trace_id": TRACE_ID.get(),
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Turn schema failures into something the agent can recover from.

    Malformed input usually means the caller was misheard or asked for something
    outside policy. Both are recoverable, but only if the response says *which*
    field was wrong and what would be acceptable — a generic apology makes the
    model retry the same rejected value, fail identically, and escalate a call
    that never needed a human.
    """
    fields = sorted({str(err["loc"][-1]) for err in exc.errors()})
    log_event(
        "validation_error",
        level=logging.WARNING,
        path=request.url.path,
        fields=fields,
    )
    return JSONResponse(
        status_code=422,
        content={
            "outcome": "retry",
            "agent_message": validation_message(fields),
            "data": {"code": "validation_error", "invalid_fields": fields},
            "trace_id": TRACE_ID.get(),
        },
    )


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last resort. Never leaks an internal message to the call."""
    log_event(
        "unhandled_exception",
        level=logging.ERROR,
        path=request.url.path,
        error_type=type(exc).__name__,
    )
    return JSONResponse(
        status_code=500,
        content={
            "outcome": "error",
            "agent_message": (
                "I am having a technical problem at my end. "
                "Let me pass you to a colleague."
            ),
            "data": {"code": "internal_error"},
            "trace_id": TRACE_ID.get(),
        },
    )
