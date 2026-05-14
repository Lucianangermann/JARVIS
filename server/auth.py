"""Bearer-token authentication for both HTTP and WebSocket.

The JARVIS_AUTH_TOKEN is a shared secret that every client (Windows or web)
must present. We compare with `secrets.compare_digest` to avoid timing
side-channels. There is no user database — this is a single-user assistant.
"""
from __future__ import annotations

import secrets
import time
from collections import defaultdict, deque
from typing import Deque

from fastapi import Header, HTTPException, status
from fastapi.security.utils import get_authorization_scheme_param
from starlette.websockets import WebSocket

from .config import settings


# ----- Rate limiter (per token, sliding 60 s window) ---------------------- #

_rate_buckets: dict[str, Deque[float]] = defaultdict(deque)


def _check_rate(token: str) -> None:
    """Raise 429 if `token` exceeded RATE_LIMIT_PER_MINUTE in the last 60 s."""
    now = time.monotonic()
    bucket = _rate_buckets[token]
    while bucket and now - bucket[0] > 60.0:
        bucket.popleft()
    if len(bucket) >= settings.RATE_LIMIT_PER_MINUTE:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded — max "
            f"{settings.RATE_LIMIT_PER_MINUTE} requests per minute.",
        )
    bucket.append(now)


# ----- HTTP dependency ---------------------------------------------------- #

def require_token(authorization: str | None = Header(default=None)) -> str:
    """FastAPI dependency that validates the `Authorization: Bearer …` header.

    Returns the token (so handlers can use it as a session key) or raises 401.
    """
    scheme, token = get_authorization_scheme_param(authorization)
    if not token or scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not secrets.compare_digest(token, settings.JARVIS_AUTH_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    _check_rate(token)
    return token


# ----- WebSocket helper --------------------------------------------------- #

async def authorize_websocket(ws: WebSocket) -> str | None:
    """Validate a WS handshake.

    Accepts the token via either the `Authorization` header (preferred) or a
    `?token=…` query string (necessary for browser WebSocket clients, which
    can't set custom headers). Closes the socket with code 4401 on failure
    and returns None; otherwise returns the token.
    """
    # 1. Authorization header.
    header = ws.headers.get("authorization")
    scheme, token = get_authorization_scheme_param(header)
    if not (token and scheme.lower() == "bearer"):
        # 2. Query string fallback.
        token = ws.query_params.get("token", "")

    if not token or not secrets.compare_digest(token, settings.JARVIS_AUTH_TOKEN):
        await ws.close(code=4401, reason="invalid token")
        return None

    try:
        _check_rate(token)
    except HTTPException:
        await ws.close(code=4429, reason="rate limit")
        return None

    return token
