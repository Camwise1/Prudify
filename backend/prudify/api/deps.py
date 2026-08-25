"""Shared FastAPI dependencies: config access and authentication.

Authentication follows the *arr applications, because their users are this
project's users. A browser authenticates by whichever ``auth.method`` is
configured -- a login form and session cookie by default -- while a valid
``X-Api-Key`` is *always* accepted so scripts, the CLI and Home Assistant keep
working regardless of how the UI is secured.
"""

from __future__ import annotations

import base64
import binascii
import logging
import secrets

from fastapi import Header, HTTPException, Query, Request, status

from ..config import Config
from ..security import (
    LoginThrottle,
    SessionError,
    address_in_networks,
    csrf_ok,
    is_local_address,
    read_session,
    verify_password,
)

log = logging.getLogger(__name__)

SESSION_COOKIE = "prudify_session"
CSRF_COOKIE = "prudify_csrf"
CSRF_HEADER = "X-Prudify-CSRF"

# Methods that cannot change state, and so do not need a CSRF token.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Shared across requests; per-process is the right scope for a single-process
# service, and the throttle is advisory rather than a security boundary.
login_throttle = LoginThrottle()


def get_config(request: Request) -> Config:
    return request.app.state.config


def client_address(request: Request, config: Config) -> str:
    """The peer address, honouring X-Forwarded-For only from trusted proxies.

    Taking the header unconditionally would let anyone forge their source
    address, which matters because two features key off it: the
    "no authentication on the local network" option and ``external`` auth.
    """
    peer = request.client.host if request.client else ""
    trusted = config.auth.trusted_proxies
    if trusted and address_in_networks(peer, trusted):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            # Left-most entry is the original client.
            return forwarded.split(",")[0].strip()
    return peer


def _api_key_matches(config: Config, provided: str | None) -> bool:
    expected = config.server.api_key
    if not provided or not expected:
        return False
    # compare_digest raises TypeError on non-ASCII str; compare bytes instead.
    return secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _session_user(request: Request, config: Config) -> str | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        return read_session(
            token,
            config.auth.session_secret,
            config.auth.session_epoch,
        )
    except SessionError:
        return None


def _basic_user(config: Config, header: str | None) -> str | None:
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
        username, _, password = decoded.partition(":")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    if not secrets.compare_digest(username.encode(), config.auth.username.encode()):
        return None
    return username if verify_password(password, config.auth.password_hash) else None


def _proxy_user(request: Request, config: Config, peer: str) -> str | None:
    """Identity asserted by a reverse proxy, trusted only from known networks."""
    if not config.auth.trusted_proxies:
        # Without a trust list the header is attacker-controlled. Refusing is
        # the only safe reading, and the settings UI says so.
        return None
    raw_peer = request.client.host if request.client else ""
    if not address_in_networks(raw_peer, config.auth.trusted_proxies):
        return None
    user = request.headers.get(config.auth.proxy_user_header.lower(), "").strip()
    return user or None


def _unauthorized(config: Config, detail: str = "Authentication required") -> HTTPException:
    headers: dict[str, str] = {}
    if config.auth.method == "basic":
        # Only send this for Basic. Sending it for forms auth makes browsers
        # pop their own credential dialog over the top of the login page.
        headers["WWW-Authenticate"] = 'Basic realm="Prudify"'
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail=detail, headers=headers
    )


async def require_auth(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
) -> None:
    """Authenticate an API request.

    Note there is deliberately no ``?apikey=`` query fallback here. It used to
    be on this shared dependency, which meant the key authenticated *every*
    endpoint from the URL -- including the one that returns the key itself --
    and reverse proxies log query strings by default. Only the SSE stream
    accepts it, via :func:`require_auth_stream`.
    """
    await _authenticate(request, x_api_key, allow_query_key=None)


async def require_auth_stream(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    apikey: str | None = Query(default=None),
) -> None:
    """Authenticate the Server-Sent Events stream.

    ``EventSource`` cannot set request headers. Browsers do not need the
    fallback -- the session cookie is sent automatically -- but non-browser
    clients holding only an API key do, so it is accepted on this one route.
    """
    await _authenticate(request, x_api_key, allow_query_key=apikey)


async def _authenticate(
    request: Request,
    header_key: str | None,
    allow_query_key: str | None,
) -> None:
    config: Config = request.app.state.config
    auth = config.auth

    if auth.method == "none":
        return

    peer = client_address(request, config)
    if auth.required == "disabled_for_local" and is_local_address(peer):
        return

    # The API key is always a valid credential, whatever the browser method.
    if _api_key_matches(config, header_key) or _api_key_matches(config, allow_query_key):
        request.state.user = "api-key"
        return

    if auth.method == "apikey":
        raise _unauthorized(config, "Invalid or missing API key")

    if auth.method == "external":
        user = _proxy_user(request, config, peer)
        if user:
            request.state.user = user
            return
        raise _unauthorized(config, "No trusted proxy identity")

    # forms and basic both rest on the local account, and both accept a
    # session cookie -- basic auth users still get a session after the first
    # successful prompt, which keeps CSRF handling uniform.
    if not auth.configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is enabled but no account has been created yet.",
        )

    user = _session_user(request, config)
    if user is None and auth.method == "basic":
        user = _basic_user(config, request.headers.get("authorization"))

    if user is None:
        raise _unauthorized(config)

    _require_csrf(request, config)
    request.state.user = user


def _require_csrf(request: Request, config: Config) -> None:
    """Double-submit CSRF check for cookie-authenticated state changes.

    Only applies to cookie auth. An API key travels in a header that a
    cross-site page cannot set, so it is not forgeable this way; a session
    cookie is sent by the browser automatically, so it is.
    """
    if request.method in _SAFE_METHODS:
        return
    if not request.cookies.get(SESSION_COOKIE):
        return  # authenticated by Basic, not by a cookie
    if not csrf_ok(request.cookies.get(CSRF_COOKIE), request.headers.get(CSRF_HEADER)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing or invalid CSRF token",
        )


# Kept so existing imports and any downstream code keep working.
require_api_key = require_auth
