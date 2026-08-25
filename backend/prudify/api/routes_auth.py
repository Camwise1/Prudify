"""Login, logout, account setup and password change.

These routes are mounted *outside* the authenticated API router, for the
obvious reason that you cannot require a session in order to create one. Each
one therefore does its own authorisation, and the rules are stated at every
handler rather than assumed.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from ..config import Config, save_config
from ..security import (
    hash_password,
    issue_session,
    needs_rehash,
    new_csrf_token,
    verify_password,
)
from .deps import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    client_address,
    login_throttle,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str
    password: str


class SetupIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=1024)


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=1024)
    sign_out_everywhere: bool = False


class AuthStatusOut(BaseModel):
    method: str
    required: str
    needs_setup: bool
    authenticated: bool
    username: str = ""
    # True when the deployment can never show a login form, so the UI knows to
    # ask for an API key instead of rendering one.
    supports_login: bool = False


def _config(request: Request) -> Config:
    return request.app.state.config


def _set_session_cookies(
    response: Response, request: Request, config: Config, username: str
) -> None:
    token = issue_session(
        username,
        config.auth.session_secret,
        config.auth.session_epoch,
        config.auth.session_lifetime_hours * 3600,
    )
    csrf = new_csrf_token()
    # Secure only over HTTPS: setting it unconditionally would silently break
    # every plain-http LAN deployment, which is most of them.
    secure = request.url.scheme == "https"
    base = config.server.url_base or "/"
    max_age = config.auth.session_lifetime_hours * 3600

    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,       # unreadable from JavaScript, unlike localStorage
        samesite="lax",      # blocks the classic cross-site form POST
        secure=secure,
        path=base,
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=max_age,
        httponly=False,      # the SPA must read this to echo it in a header
        samesite="lax",
        secure=secure,
        path=base,
    )


def _clear_session_cookies(response: Response, config: Config) -> None:
    base = config.server.url_base or "/"
    response.delete_cookie(SESSION_COOKIE, path=base)
    response.delete_cookie(CSRF_COOKIE, path=base)


@router.get("/status", response_model=AuthStatusOut)
def auth_status(request: Request) -> AuthStatusOut:
    """Unauthenticated by design: the login page needs this before signing in.

    It deliberately exposes only which *kind* of authentication is in use and
    whether the caller currently holds a session -- never the username of an
    unauthenticated caller, and never anything about the account itself.
    """
    from .deps import _session_user  # local import to avoid a cycle at import time

    config = _config(request)
    user = _session_user(request, config) or ""
    return AuthStatusOut(
        method=config.auth.method,
        required=config.auth.required,
        needs_setup=config.auth.needs_setup,
        authenticated=bool(user),
        username=user,
        supports_login=config.auth.method in ("forms", "basic"),
    )


@router.post("/setup", response_model=AuthStatusOut)
def setup(payload: SetupIn, request: Request, response: Response) -> AuthStatusOut:
    """Create the first account.

    Only possible while no account exists. Once one does, this route is closed
    permanently -- otherwise it would be an unauthenticated password reset.
    """
    config = _config(request)
    if config.auth.configured:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account already exists. Sign in, or reset the password "
            "from the command line with `prudify auth set-password`.",
        )
    if config.auth.method in ("none", "apikey", "external"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Authentication method is '{config.auth.method}', which uses no account.",
        )

    config.auth.username = payload.username.strip()
    config.auth.password_hash = hash_password(payload.password)
    save_config(config)
    log.info("Created the initial account for %r", config.auth.username)

    _set_session_cookies(response, request, config, config.auth.username)
    return AuthStatusOut(
        method=config.auth.method,
        required=config.auth.required,
        needs_setup=False,
        authenticated=True,
        username=config.auth.username,
        supports_login=True,
    )


@router.post("/login", response_model=AuthStatusOut)
def login(payload: LoginIn, request: Request, response: Response) -> AuthStatusOut:
    config = _config(request)
    if not config.auth.configured:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No account exists yet.",
        )

    peer = client_address(request, config) or "unknown"
    locked = login_throttle.locked_for(peer)
    if locked:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed attempts. Try again in {locked} seconds.",
            headers={"Retry-After": str(locked)},
        )

    username_ok = secrets_equal(payload.username.strip(), config.auth.username)
    password_ok = verify_password(payload.password, config.auth.password_hash)
    if not (username_ok and password_ok):
        login_throttle.record_failure(peer)
        log.warning("Failed login for %r from %s", payload.username[:64], peer)
        # One message for both cases: saying which half was wrong tells an
        # attacker whether a username exists.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
        )

    login_throttle.reset(peer)

    # Opportunistically upgrade the stored hash if the cost parameters have
    # been raised since it was written; we have the plaintext exactly here.
    if needs_rehash(config.auth.password_hash):
        config.auth.password_hash = hash_password(payload.password)
        save_config(config)

    _set_session_cookies(response, request, config, config.auth.username)
    log.info("Signed in: %r from %s", config.auth.username, peer)
    return AuthStatusOut(
        method=config.auth.method,
        required=config.auth.required,
        needs_setup=False,
        authenticated=True,
        username=config.auth.username,
        supports_login=True,
    )


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    config = _config(request)
    _clear_session_cookies(response, config)
    return {"ok": True}


@router.post("/password")
def change_password(
    payload: ChangePasswordIn, request: Request, response: Response
) -> dict:
    """Change the password. Requires the current one, even with a session.

    Re-asking protects against someone using an unattended browser, and it is
    what every comparable application does.
    """
    from .deps import _session_user

    config = _config(request)
    if not config.auth.configured:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No account exists")

    user = _session_user(request, config)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in first"
        )
    if not verify_password(payload.current_password, config.auth.password_hash):
        peer = client_address(request, config) or "unknown"
        login_throttle.record_failure(peer)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Current password is incorrect"
        )

    config.auth.password_hash = hash_password(payload.new_password)
    if payload.sign_out_everywhere:
        # Every outstanding token carries the old epoch and stops validating.
        config.auth.session_epoch += 1
    save_config(config)

    # Re-issue for this browser so the person changing the password is not
    # signed out by their own action.
    _set_session_cookies(response, request, config, config.auth.username)
    log.info("Password changed for %r", config.auth.username)
    return {"ok": True, "signed_out_everywhere": payload.sign_out_everywhere}


def secrets_equal(a: str, b: str) -> bool:
    import secrets as _s

    return _s.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
