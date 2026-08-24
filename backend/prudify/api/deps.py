"""Shared FastAPI dependencies: config access and API-key auth."""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, Query, Request, status

from ..config import Config


def get_config(request: Request) -> Config:
    return request.app.state.config


async def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    apikey: str | None = Query(default=None),
) -> None:
    """Validate the API key unless the server was configured to skip it.

    Uses the ``X-Api-Key`` header that self-hosted services conventionally
    expect, with a query parameter fallback so ``EventSource`` (which cannot
    set headers) can authenticate the SSE stream.
    """
    config: Config = request.app.state.config
    if not config.server.require_api_key:
        return

    provided = x_api_key or apikey or ""
    expected = config.server.api_key
    if not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
