"""Authentication: primitives, the auth dependency, and the login routes."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from prudify import security
from prudify.api.deps import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, login_throttle
from prudify.config import _migrate_auth

# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


class TestPasswordHashing:
    def test_round_trip(self):
        stored = security.hash_password("correct horse battery staple")
        assert security.verify_password("correct horse battery staple", stored)

    def test_rejects_wrong_password(self):
        stored = security.hash_password("hunter2")
        assert not security.verify_password("hunter3", stored)

    def test_is_salted(self):
        assert security.hash_password("same") != security.hash_password("same")

    @pytest.mark.parametrize("stored", ["", "garbage", "scrypt$x$8$1$aa$bb", "bcrypt$2b$12$x"])
    def test_malformed_hashes_return_false_rather_than_raising(self, stored):
        assert not security.verify_password("anything", stored)

    def test_empty_password_is_never_valid(self):
        assert not security.verify_password("", security.hash_password("real"))

    def test_hashing_an_empty_password_is_refused(self):
        with pytest.raises(ValueError):
            security.hash_password("")


class TestSessions:
    SECRET = "a-server-secret"

    def test_round_trip(self):
        token = security.issue_session("cam", self.SECRET, epoch=1, lifetime_seconds=60)
        assert security.read_session(token, self.SECRET, 1) == "cam"

    def test_rejects_a_different_secret(self):
        token = security.issue_session("cam", self.SECRET, epoch=1, lifetime_seconds=60)
        with pytest.raises(security.SessionError):
            security.read_session(token, "other-secret", 1)

    def test_epoch_bump_revokes_outstanding_sessions(self):
        """This is what gives stateless cookies revocation."""
        token = security.issue_session("cam", self.SECRET, epoch=1, lifetime_seconds=60)
        with pytest.raises(security.SessionError):
            security.read_session(token, self.SECRET, 2)

    def test_expiry_is_enforced(self):
        token = security.issue_session("cam", self.SECRET, epoch=1, lifetime_seconds=60)
        with pytest.raises(security.SessionError):
            security.read_session(token, self.SECRET, 1, now=time.time() + 61)

    def test_a_forged_payload_fails_the_signature(self):
        import base64
        import json

        token = security.issue_session("cam", self.SECRET, epoch=1, lifetime_seconds=60)
        body, signature = token.split(".", 1)
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        payload["u"] = "attacker"
        forged = (
            base64.urlsafe_b64encode(
                json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
            )
            .decode()
            .rstrip("=")
        )
        with pytest.raises(security.SessionError):
            security.read_session(f"{forged}.{signature}", self.SECRET, 1)

    @pytest.mark.parametrize("token", ["", "no-dot", "a.b.c", "...."])
    def test_malformed_tokens_raise_session_error(self, token):
        with pytest.raises(security.SessionError):
            security.read_session(token, self.SECRET, 1)


class TestLocalAddresses:
    @pytest.mark.parametrize(
        "host,expected",
        [
            ("127.0.0.1", True), ("192.168.1.5", True), ("10.0.0.1", True),
            ("172.16.5.5", True), ("::1", True), ("169.254.1.1", True),
            ("8.8.8.8", False), ("1.1.1.1", False), ("example.com", False),
            ("", False), (None, False),
        ],
    )
    def test_classification(self, host, expected):
        assert security.is_local_address(host) is expected

    def test_cidr_containment(self):
        assert security.address_in_networks("10.1.2.3", ["10.0.0.0/8"])
        assert not security.address_in_networks("8.8.8.8", ["10.0.0.0/8"])

    def test_bad_cidr_entries_are_skipped_not_fatal(self):
        assert not security.address_in_networks("10.1.2.3", ["nonsense", ""])
        assert security.address_in_networks("10.1.2.3", ["nonsense", "10.0.0.0/8"])


class TestThrottle:
    def test_locks_after_the_limit_and_recovers(self):
        throttle = security.LoginThrottle(max_attempts=3, window_seconds=60)
        now = 1000.0
        assert throttle.locked_for("ip", now=now) == 0
        for i in range(3):
            throttle.record_failure("ip", now=now + i)
        assert throttle.locked_for("ip", now=now + 3) > 0
        assert throttle.locked_for("ip", now=now + 61) == 0

    def test_is_per_client(self):
        """One attacker must not be able to lock everyone else out."""
        throttle = security.LoginThrottle(max_attempts=2, window_seconds=60)
        for _ in range(5):
            throttle.record_failure("attacker", now=1000.0)
        assert throttle.locked_for("attacker", now=1000.0) > 0
        assert throttle.locked_for("someone-else", now=1000.0) == 0

    def test_success_clears_the_record(self):
        throttle = security.LoginThrottle(max_attempts=2, window_seconds=60)
        throttle.record_failure("ip", now=1000.0)
        throttle.record_failure("ip", now=1000.0)
        throttle.reset("ip")
        assert throttle.locked_for("ip", now=1000.0) == 0


class TestConfigMigration:
    """An upgrade must never lock the owner out of their own server."""

    def test_existing_install_keeps_api_key_auth(self):
        raw = _migrate_auth({"server": {"require_api_key": True}})
        assert raw["auth"]["method"] == "apikey"

    def test_existing_install_with_auth_off_stays_off(self):
        raw = _migrate_auth({"server": {"require_api_key": False}})
        assert raw["auth"]["method"] == "none"

    def test_an_existing_auth_block_is_left_alone(self):
        raw = _migrate_auth({"server": {}, "auth": {"method": "forms"}})
        assert raw["auth"]["method"] == "forms"


# ---------------------------------------------------------------------------
# HTTP behaviour
# ---------------------------------------------------------------------------


@pytest.fixture
def forms_client(config):
    """A client whose server uses forms auth with no account yet."""
    from prudify.config import save_config
    from prudify.main import create_app

    config.auth.method = "forms"
    config.auth.username = ""
    config.auth.password_hash = ""
    config.processing.scan_interval_minutes = 0
    save_config(config)
    # A real address: several code paths classify the peer, and the default
    # TestClient host ("testclient") is not an IP.
    for key in ("1.2.3.4", "127.0.0.1", "testclient"):
        login_throttle.reset(key)
    with TestClient(create_app(config), client=("1.2.3.4", 5000)) as client:
        yield client


class TestFirstRun:
    def test_status_reports_setup_needed(self, forms_client):
        body = forms_client.get("/api/v1/auth/status").json()
        assert body["needs_setup"] is True
        assert body["authenticated"] is False

    def test_status_is_reachable_without_credentials(self, forms_client):
        """The login page needs this before anyone can possibly be signed in."""
        assert forms_client.get("/api/v1/auth/status").status_code == 200

    def test_setup_creates_the_account_and_signs_in(self, forms_client):
        response = forms_client.post(
            "/api/v1/auth/setup", json={"username": "cam", "password": "a-good-password"}
        )
        assert response.status_code == 200
        assert response.json()["authenticated"] is True
        assert SESSION_COOKIE in response.cookies or SESSION_COOKIE in forms_client.cookies

    def test_setup_cannot_be_used_twice(self, forms_client):
        """Otherwise it is an unauthenticated password reset."""
        forms_client.post(
            "/api/v1/auth/setup", json={"username": "cam", "password": "a-good-password"}
        )
        again = forms_client.post(
            "/api/v1/auth/setup", json={"username": "attacker", "password": "another-password"}
        )
        assert again.status_code == 409

    def test_short_passwords_are_refused(self, forms_client):
        response = forms_client.post(
            "/api/v1/auth/setup", json={"username": "cam", "password": "short"}
        )
        assert response.status_code == 422


class TestLoginAndAccess:
    @pytest.fixture(autouse=True)
    def _account(self, forms_client):
        forms_client.post(
            "/api/v1/auth/setup", json={"username": "cam", "password": "a-good-password"}
        )
        forms_client.cookies.clear()

    def test_protected_route_rejects_anonymous(self, forms_client):
        assert forms_client.get("/api/v1/system/status").status_code == 401

    def test_login_then_access(self, forms_client):
        assert forms_client.post(
            "/api/v1/auth/login", json={"username": "cam", "password": "a-good-password"}
        ).status_code == 200
        assert forms_client.get("/api/v1/system/status").status_code == 200

    def test_wrong_password_is_rejected(self, forms_client):
        assert forms_client.post(
            "/api/v1/auth/login", json={"username": "cam", "password": "wrong"}
        ).status_code == 401

    def test_error_does_not_reveal_whether_the_username_exists(self, forms_client):
        bad_user = forms_client.post(
            "/api/v1/auth/login", json={"username": "nobody", "password": "a-good-password"}
        )
        bad_pass = forms_client.post(
            "/api/v1/auth/login", json={"username": "cam", "password": "wrong"}
        )
        assert bad_user.json()["detail"] == bad_pass.json()["detail"]

    def test_logout_ends_the_session(self, forms_client):
        forms_client.post(
            "/api/v1/auth/login", json={"username": "cam", "password": "a-good-password"}
        )
        forms_client.post("/api/v1/auth/logout")
        forms_client.cookies.clear()
        assert forms_client.get("/api/v1/system/status").status_code == 401

    def test_session_cookie_is_httponly(self, forms_client):
        """An XSS bug must not be able to read the credential."""
        response = forms_client.post(
            "/api/v1/auth/login", json={"username": "cam", "password": "a-good-password"}
        )
        cookie_header = response.headers.get("set-cookie", "")
        assert "httponly" in cookie_header.lower()
        assert "samesite=lax" in cookie_header.lower().replace(" ", "")


class TestApiKeyStillWorks:
    """Scripts, the CLI and Home Assistant must keep working after login exists."""

    def test_api_key_authenticates_without_a_session(self, forms_client, config):
        forms_client.post(
            "/api/v1/auth/setup", json={"username": "cam", "password": "a-good-password"}
        )
        forms_client.cookies.clear()
        response = forms_client.get(
            "/api/v1/system/status", headers={"X-Api-Key": config.server.api_key}
        )
        assert response.status_code == 200

    def test_a_wrong_api_key_is_rejected(self, forms_client):
        forms_client.cookies.clear()
        assert forms_client.get(
            "/api/v1/system/status", headers={"X-Api-Key": "nope"}
        ).status_code == 401


class TestCsrf:
    def test_state_change_needs_the_csrf_header(self, forms_client):
        forms_client.post(
            "/api/v1/auth/setup", json={"username": "cam", "password": "a-good-password"}
        )
        # Cookie auth without the echoed token: this is the cross-site case.
        response = forms_client.post("/api/v1/queue/pause")
        assert response.status_code == 403

    def test_state_change_succeeds_with_the_token(self, forms_client):
        forms_client.post(
            "/api/v1/auth/setup", json={"username": "cam", "password": "a-good-password"}
        )
        token = forms_client.cookies.get(CSRF_COOKIE)
        assert token, "setup must issue a CSRF cookie"
        response = forms_client.post("/api/v1/queue/pause", headers={CSRF_HEADER: token})
        assert response.status_code == 200

    def test_reads_do_not_need_a_token(self, forms_client):
        forms_client.post(
            "/api/v1/auth/setup", json={"username": "cam", "password": "a-good-password"}
        )
        assert forms_client.get("/api/v1/system/status").status_code == 200


class TestQueryParameterKeyIsScoped:
    """?apikey= used to authenticate every route, including the one returning the key."""

    def test_query_key_is_refused_on_ordinary_routes(self, forms_client, config):
        forms_client.cookies.clear()
        response = forms_client.get(
            f"/api/v1/settings?apikey={config.server.api_key}"
        )
        assert response.status_code == 401


class TestPasswordChange:
    @pytest.fixture(autouse=True)
    def _signed_in(self, forms_client):
        forms_client.post(
            "/api/v1/auth/setup", json={"username": "cam", "password": "a-good-password"}
        )

    def _csrf(self, client):
        return {CSRF_HEADER: client.cookies.get(CSRF_COOKIE) or ""}

    def test_requires_the_current_password(self, forms_client):
        response = forms_client.post(
            "/api/v1/auth/password",
            json={"current_password": "wrong", "new_password": "another-good-one"},
            headers=self._csrf(forms_client),
        )
        assert response.status_code == 401

    def test_changes_the_password(self, forms_client):
        response = forms_client.post(
            "/api/v1/auth/password",
            json={
                "current_password": "a-good-password",
                "new_password": "another-good-one",
            },
            headers=self._csrf(forms_client),
        )
        assert response.status_code == 200
        forms_client.cookies.clear()
        assert forms_client.post(
            "/api/v1/auth/login", json={"username": "cam", "password": "another-good-one"}
        ).status_code == 200

    def test_sign_out_everywhere_revokes_other_sessions(self, forms_client, config):
        stale = security.issue_session(
            "cam",
            config.auth.session_secret,
            config.auth.session_epoch,
            3600,
        )
        forms_client.post(
            "/api/v1/auth/password",
            json={
                "current_password": "a-good-password",
                "new_password": "another-good-one",
                "sign_out_everywhere": True,
            },
            headers=self._csrf(forms_client),
        )
        forms_client.cookies.clear()
        forms_client.cookies.set(SESSION_COOKIE, stale)
        assert forms_client.get("/api/v1/system/status").status_code == 401


class TestAuthMethods:
    def test_none_allows_everything(self, config):
        from prudify.main import create_app

        config.auth.method = "none"
        with TestClient(create_app(config), client=("1.2.3.4", 5000)) as client:
            assert client.get("/api/v1/system/status").status_code == 200

    def test_external_refuses_the_header_without_a_trust_list(self, config):
        """Otherwise anyone could simply send the header themselves."""
        from prudify.main import create_app

        config.auth.method = "external"
        config.auth.trusted_proxies = []
        with TestClient(create_app(config), client=("10.1.2.3", 5000)) as client:
            response = client.get(
                "/api/v1/system/status", headers={"X-Forwarded-User": "attacker"}
            )
            assert response.status_code == 401

    def test_external_accepts_the_header_from_a_trusted_proxy(self, config):
        from prudify.main import create_app

        config.auth.method = "external"
        config.auth.trusted_proxies = ["10.0.0.0/8"]
        with TestClient(create_app(config), client=("10.1.2.3", 5000)) as client:
            response = client.get(
                "/api/v1/system/status", headers={"X-Forwarded-User": "cam"}
            )
            assert response.status_code == 200
