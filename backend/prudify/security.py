"""Password hashing, session tokens and CSRF, with no third-party crypto.

Everything here is standard library. That is a deliberate constraint, not
laziness: Prudify ships as a multi-architecture container and as a `pip
install` on Windows, macOS, x86 Linux and ARM NAS boxes. ``argon2-cffi`` and
``bcrypt`` are compiled extensions, and a missing wheel for a new Python or an
unusual architecture turns into "the app will not install" for someone with no
compiler. ``hashlib.scrypt`` is memory-hard, has been in the standard library
since 3.6, and rides on the OpenSSL that Python already links.

Sessions are stateless signed cookies rather than rows in a table. Revocation
still works, because every token carries the ``epoch`` it was issued under and
the server rejects anything older than the current one -- so changing the
password or choosing "sign out everywhere" invalidates outstanding sessions
without needing to store them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import time

# scrypt cost parameters. n=2**15 with r=8 costs roughly 32 MB and ~50-100ms
# on a modern CPU -- slow enough to make offline cracking expensive, fast
# enough that a NAS login does not feel broken. Raising n costs memory
# quadratically, which matters when the target might be a 2 GB Synology.
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SALT_BYTES = 16

_HASH_PREFIX = "scrypt"


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Hash a password for storage in config.yaml.

    Returns a self-describing string so the parameters can be raised later
    without invalidating existing hashes:
    ``scrypt$n$r$p$<salt-b64>$<hash-b64>``
    """
    if not password:
        raise ValueError("Password must not be empty")
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_N * _SCRYPT_R * 256,
    )
    return "$".join(
        [
            _HASH_PREFIX,
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            _b64(salt),
            _b64(derived),
        ]
    )


def verify_password(password: str, stored: str) -> bool:
    """Check a password against a stored hash. Never raises on bad input."""
    if not password or not stored:
        return False
    try:
        prefix, n_s, r_s, p_s, salt_b64, hash_b64 = stored.split("$")
        if prefix != _HASH_PREFIX:
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = _unb64(salt_b64)
        expected = _unb64(hash_b64)
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
            maxmem=n * r * 256,
        )
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(derived, expected)


def needs_rehash(stored: str) -> bool:
    """True when a stored hash used weaker parameters than we now use."""
    try:
        prefix, n_s, r_s, p_s, _salt, _hash = stored.split("$")
    except ValueError:
        return True
    if prefix != _HASH_PREFIX:
        return True
    return (int(n_s), int(r_s), int(p_s)) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P)


# ---------------------------------------------------------------------------
# Session tokens
# ---------------------------------------------------------------------------


class SessionError(Exception):
    """A session token was absent, malformed, tampered with, or expired."""


def issue_session(
    username: str,
    secret: str,
    epoch: int,
    lifetime_seconds: int,
    *,
    now: float | None = None,
) -> str:
    """Create a signed session token.

    The payload is not secret -- it is signed, not encrypted -- so it carries
    only the username, the issuing epoch and an expiry.
    """
    issued = int(now if now is not None else time.time())
    payload = {
        "u": username,
        "e": int(epoch),
        "iat": issued,
        "exp": issued + int(lifetime_seconds),
    }
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    return f"{body}.{_sign(body, secret)}"


def read_session(
    token: str,
    secret: str,
    epoch: int,
    *,
    now: float | None = None,
) -> str:
    """Validate a session token and return its username.

    Raises :class:`SessionError` for anything untrustworthy. The signature is
    checked *before* the payload is parsed, so malformed input from an
    attacker never reaches the JSON decoder with any authority.
    """
    if not token or not secret:
        raise SessionError("No session")
    try:
        body, signature = token.split(".", 1)
    except ValueError as exc:
        raise SessionError("Malformed session token") from exc

    if not hmac.compare_digest(signature, _sign(body, secret)):
        raise SessionError("Bad session signature")

    try:
        payload = json.loads(_unb64(body))
        username = str(payload["u"])
        token_epoch = int(payload["e"])
        expires = int(payload["exp"])
    except (ValueError, TypeError, KeyError) as exc:
        raise SessionError("Malformed session payload") from exc

    # Epoch mismatch means the password changed or the user signed out
    # everywhere. This is what gives stateless tokens revocation.
    if token_epoch != int(epoch):
        raise SessionError("Session has been revoked")

    current = now if now is not None else time.time()
    if current >= expires:
        raise SessionError("Session expired")

    return username


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_ok(cookie_value: str | None, header_value: str | None) -> bool:
    """Double-submit check: the header must echo the cookie.

    A cross-site page can cause the browser to *send* our cookies, but it
    cannot read them, so it cannot reproduce the value in a custom header.
    SameSite=Lax already blocks the classic cross-site form POST; this covers
    the cases it does not, and costs nothing.
    """
    if not cookie_value or not header_value:
        return False
    return hmac.compare_digest(cookie_value, header_value)


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------


def is_local_address(host: str | None) -> bool:
    """True for loopback, private and link-local addresses.

    Used for the "do not require authentication on the local network" option
    that the *arr applications offer. Note this trusts the *socket* peer, not
    a forwarded header -- behind a reverse proxy every request appears to come
    from the proxy, which is why that option warns against combining the two.
    """
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host.strip().strip("[]"))
    except ValueError:
        return False
    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_unspecified
    )


def address_in_networks(host: str | None, networks: list[str]) -> bool:
    """True when host falls inside any of the given CIDR ranges."""
    if not host or not networks:
        return False
    try:
        address = ipaddress.ip_address(host.strip().strip("[]"))
    except ValueError:
        return False
    for entry in networks:
        try:
            if address in ipaddress.ip_network(entry.strip(), strict=False):
                return True
        except ValueError:
            continue
    return False


# ---------------------------------------------------------------------------
# Login throttling
# ---------------------------------------------------------------------------


class LoginThrottle:
    """Slow down password guessing, per client address.

    Deliberately simple and in-memory: this is a single-process service, and
    an attacker who can restart it can already do worse. The goal is to make
    online guessing impractical, not to survive a determined botnet.
    """

    def __init__(self, max_attempts: int = 5, window_seconds: int = 300) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._failures: dict[str, list[float]] = {}

    def _recent(self, key: str, now: float) -> list[float]:
        cutoff = now - self.window_seconds
        recent = [t for t in self._failures.get(key, []) if t > cutoff]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def locked_for(self, key: str, *, now: float | None = None) -> int:
        """Seconds remaining before this client may try again. 0 means go."""
        current = now if now is not None else time.time()
        recent = self._recent(key, current)
        if len(recent) < self.max_attempts:
            return 0
        return max(0, int(recent[0] + self.window_seconds - current) + 1)

    def record_failure(self, key: str, *, now: float | None = None) -> None:
        current = now if now is not None else time.time()
        self._recent(key, current)
        self._failures.setdefault(key, []).append(current)

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _sign(body: str, secret: str) -> str:
    return _b64(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)
