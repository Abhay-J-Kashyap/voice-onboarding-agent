"""Authentication for tool endpoints.

The voice platform calls these endpoints over the public internet, so they are
not left open. A shared secret is the right weight for this surface: the caller
is one trusted machine, not a user population.
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from app.config import get_settings


async def require_api_key(x_api_key: str = Header(default="")) -> None:
    """Constant-time comparison of the shared secret."""
    expected = get_settings().api_key
    if not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized", "message": "Invalid or missing API key."},
        )
