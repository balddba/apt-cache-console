"""Verify login, cookie lifecycle, and authentication boundaries."""

from dataclasses import replace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.auth import COOKIE_NAME, SESSION_SECONDS, Sessions
from app.main import Settings, create_app

HEADERS = {"X-Requested-With": "squid-ui"}
type AuthClient = tuple[TestClient, dict[str, str]]


@pytest.fixture
def client() -> AuthClient:
    """Create an unauthenticated browser session and its configured credentials."""
    settings = Settings.load()
    return TestClient(app=create_app(settings=settings)), {
        "username": settings.username,
        "password": settings.password,
    }


def test_login_and_logout(client: AuthClient) -> None:
    """Sign in, rotate sessions on repeat login, and revoke copied logout tokens."""
    browser, credentials = client
    assert browser.get(url="/").url.path == "/login"
    response = browser.post(url="/api/login", json=credentials, headers=HEADERS)
    assert response.status_code == 204
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "path=/" in cookie
    assert f"max-age={SESSION_SECONDS}" in cookie
    assert "secure" not in cookie
    old_token = browser.cookies.get(COOKIE_NAME)
    assert old_token is not None
    assert credentials["password"] not in old_token
    assert browser.get(url="/").url.path == "/"
    assert browser.get(url="/login").url.path == "/"
    browser.post(url="/api/login", json=credentials, headers=HEADERS)
    token = browser.cookies.get(COOKIE_NAME)
    assert token is not None
    assert token != old_token
    with TestClient(app=browser.app) as other:
        other.cookies.set(name=COOKIE_NAME, value=old_token)
        assert other.get(url="/api/overview").status_code == 401
    assert browser.post(url="/api/logout", headers=HEADERS).status_code == 204
    assert browser.cookies.get(COOKIE_NAME) is None
    browser.cookies.set(name=COOKIE_NAME, value=token)
    assert browser.get(url="/api/overview").status_code == 401


@pytest.mark.parametrize(argnames="field", argvalues=["username", "password"])
def test_bad_credentials(client: AuthClient, field: str) -> None:
    """Return a generic failure without a session or HTTP authentication challenge."""
    browser, credentials = client
    credentials[field] = "incorrect"
    response = browser.post(url="/api/login", json=credentials, headers=HEADERS)
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid username or password"
    assert "www-authenticate" not in response.headers
    assert COOKIE_NAME not in browser.cookies


def test_session_action_protection(client: AuthClient) -> None:
    """Require a non-simple custom header for both login and logout."""
    browser, credentials = client
    assert browser.post(url="/api/login", json=credentials).status_code == 403
    assert browser.post(url="/api/logout").status_code == 403
    assert browser.post(url="/api/login", data=credentials).status_code == 403
    assert (
        browser.post(
            url="/api/login", json={"username": 42}, headers=HEADERS
        ).status_code
        == 422
    )


def test_expired_and_forged_sessions(client: AuthClient) -> None:
    """Reject unknown cookies, tampering, old Basic auth, and expired sessions."""
    browser, credentials = client
    browser.cookies.set(name=COOKIE_NAME, value="forged")
    response = browser.get(
        url="/api/overview",
        auth=(credentials["username"], credentials["password"]),
    )
    assert response.status_code == 401
    browser.cookies.clear()
    with patch(target="app.auth.time.time", return_value=1000):
        browser.post(url="/api/login", json=credentials, headers=HEADERS)
    with patch(target="app.auth.time.time", return_value=1000 + SESSION_SECONDS):
        assert browser.get(url="/api/overview").status_code == 401
        assert (
            browser.get(url="/", follow_redirects=False).headers["location"] == "/login"
        )
    assert browser.post(url="/api/logout", headers=HEADERS).status_code == 204


def test_secure_cookie() -> None:
    """Apply Secure to HTTPS session cookies when explicitly configured."""
    settings = replace(Settings.load(), cookie_secure=True)
    browser = TestClient(
        app=create_app(settings=settings), base_url="https://testserver"
    )
    response = browser.post(
        url="/api/login",
        json={"username": settings.username, "password": settings.password},
        headers=HEADERS,
    )
    assert "; Secure" in response.headers["set-cookie"]
    assert browser.get(url="/").url.path == "/"


@pytest.mark.parametrize(
    argnames="password", argvalues=["12345678", "long-enough-password"]
)
def test_password_minimum_accepts_valid_passwords(
    monkeypatch: pytest.MonkeyPatch, password: str
) -> None:
    """Accept and authenticate passwords at or above the eight-character minimum."""
    monkeypatch.setenv(name="UI_PASSWORD", value=password)
    settings = Settings.load()
    browser = TestClient(app=create_app(settings=settings))
    response = browser.post(
        url="/api/login",
        json={"username": settings.username, "password": password},
        headers=HEADERS,
    )
    assert response.status_code == 204
    assert browser.get(url="/").url.path == "/"


@pytest.mark.parametrize(argnames="password", argvalues=[None, "", "1234567"])
def test_password_minimum_rejects_short_passwords(
    monkeypatch: pytest.MonkeyPatch, password: str | None
) -> None:
    """Reject missing, empty, and seven-character passwords at startup."""
    if password is None:
        monkeypatch.delenv(name="UI_PASSWORD", raising=False)
    else:
        monkeypatch.setenv(name="UI_PASSWORD", value=password)
    with pytest.raises(
        expected_exception=ValueError, match="UI_PASSWORD must be at least 8 characters"
    ):
        Settings.load()


def test_secure_cookie_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validate the optional HTTPS-only cookie setting."""
    monkeypatch.setenv(name="UI_COOKIE_SECURE", value="true")
    assert Settings.load().cookie_secure is True
    monkeypatch.setenv(name="UI_COOKIE_SECURE", value="invalid")
    with pytest.raises(expected_exception=ValueError, match="UI_COOKIE_SECURE"):
        Settings.load()


def test_session_registry() -> None:
    """Bound session storage, evict oldest entries, and prune expired records."""
    sessions = Sessions()
    with patch(target="app.auth.time.time", return_value=1000):
        oldest = sessions.create()
        for _ in range(1024):
            newest = sessions.create()
        assert not sessions.valid(token=oldest)
        assert sessions.valid(token=newest)
        assert not sessions.valid(token=None)
    with patch(target="app.auth.time.time", return_value=1000 + SESSION_SECONDS):
        assert not sessions.valid(token=newest)
        current = sessions.create()
        assert len(sessions._tokens) == 1
        assert sessions.valid(token=current)
    assert not Sessions().valid(token=current)
