"""Verify parsing, authentication, manager failures, and action boundaries."""

import http.client
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import (
    Settings,
    create_app,
    manager_report,
    parse_counters,
    parse_storage,
    read_requests,
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Return isolated settings for a temporary access log."""
    return Settings(
        "squid-deb-proxy",
        8001,
        "test-manager-password-123456",
        "admin",
        "test-dashboard-password",
        tmp_path / "access.log",
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    """Create an authenticated test client."""
    test_client = TestClient(create_app(settings))
    assert (
        test_client.post(
            "/api/login",
            json={"username": settings.username, "password": settings.password},
            headers={"X-Requested-With": "squid-ui"},
        ).status_code
        == 204
    )
    return test_client


def test_configuration_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject missing or invalid secrets and destinations."""
    monkeypatch.setenv("UI_PASSWORD", "short")
    with pytest.raises(ValueError, match="UI_PASSWORD"):
        Settings.load()
    monkeypatch.setenv("UI_PASSWORD", "long-enough-password")
    monkeypatch.setenv("SQUID_MANAGER_PASSWORD", "bad\nconfig injection")
    with pytest.raises(ValueError, match="SQUID_MANAGER_PASSWORD"):
        Settings.load()
    monkeypatch.setenv("SQUID_MANAGER_PASSWORD", "test-manager-password-123456")
    monkeypatch.setenv("SQUID_PORT", "0")
    with pytest.raises(ValueError, match="host or port"):
        Settings.load()


def test_authentication(client: TestClient) -> None:
    """Protect statistics, diagnostics, dashboard, and mutating actions."""
    client.cookies.clear()
    for path in ("/api/overview", "/api/reports/info"):
        response = client.get(path)
        assert response.status_code == 401
        assert "www-authenticate" not in response.headers
    assert (
        client.post("/api/actions/reconfigure", auth=("bad", "bad")).status_code == 401
    )
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert 'id="login-form"' in client.get("/login").text
    assert client.get("/healthz", auth=None).json() == {"status": "ok"}


def test_counter_parser() -> None:
    """Ignore report headings and preserve decimal values."""
    assert parse_counters("client_http.requests = 12\nwall_time = 1.5\nheading\n") == {
        "client_http.requests": 12.0,
        "wall_time": 1.5,
    }


def test_storage_parser() -> None:
    """Convert Squid's aggregate KB totals without counting per-directory sizes."""
    assert parse_storage(
        "Store Directory Statistics:\n"
        "Maximum Swap Size      : 10240000 KB\n"
        "Current Store Swap Size: 1536.50 KB\n"
        "Current Capacity       : 0.02% used, 99.98% free\n"
        "\nStore Directory #0 (ufs): /var/cache/squid-deb-proxy\n"
        "Current Size: 1536.50 KB\n"
    ) == {"used_bytes": 1573376, "capacity_bytes": 10485760000}


@pytest.mark.parametrize("value", ["0", "0.00"])
def test_empty_disk_cache(value: str) -> None:
    """Preserve genuine zero usage and a disabled disk cache limit."""
    assert parse_storage(
        f"Maximum Swap Size: 0 KB\nCurrent Store Swap Size: {value} KB\n"
    ) == {"used_bytes": 0, "capacity_bytes": 0}


@pytest.mark.parametrize("value", ["-1", "NaN", "inf", "broken", "9" * 400])
def test_invalid_storage_totals(value: str) -> None:
    """Do not present invalid or absent totals as zero usage."""
    assert parse_storage(f"Current Store Swap Size: {value} KB\n") == {
        "used_bytes": None,
        "capacity_bytes": None,
    }
    assert parse_storage("Unrecognized report\n") == {
        "used_bytes": None,
        "capacity_bytes": None,
    }


def test_overview_storage(client: TestClient) -> None:
    """Expose live storage independently of lifetime traffic counters."""
    reports = {
        "counters": "client_http.requests = 42",
        "info": "Squid info",
        "storedir": "Maximum Swap Size: 4096 KB\nCurrent Store Swap Size: 1024 KB\n",
    }
    with patch(
        "app.main.manager_report", side_effect=lambda _, report: reports[report]
    ):
        data = client.get("/api/overview").json()
    assert data["online"] is True
    assert data["storage"] == {"used_bytes": 1048576, "capacity_bytes": 4194304}


def test_storage_report_failure_preserves_overview(client: TestClient) -> None:
    """Keep a reachable proxy online even when its storage report is denied."""

    def report(_: Settings, name: str) -> str:
        """Reject storage access while serving normal counters and info."""
        if name == "storedir":
            raise HTTPException(502, "Storage report unavailable")
        return "client_http.requests = 42" if name == "counters" else "Squid info"

    with patch("app.main.manager_report", side_effect=report):
        data = client.get("/api/overview").json()
    assert data["online"] is True
    assert data["error"] is None
    assert data["counters"]["client_http.requests"] == 42
    assert data["storage"] == {"used_bytes": None, "capacity_bytes": None}


def test_log_summary(tmp_path: Path) -> None:
    """Handle malformed lines, old rows, manager traffic, and sensitive URLs."""
    path = tmp_path / "access.log"
    path.write_text(
        "10000.000 15 10.0.0.2 TCP_HIT/200 2048 GET http://user:secret@deb.debian.org/pool/a.deb?token=hidden - HIER_NONE/- application/x-deb\n"
        "10001.000 30 10.0.0.3 TCP_MISS/404 100 GET http://deb.debian.org/missing - HIER_DIRECT/1 text/html\n"
        "10001.000 1 10.0.0.4 TCP_MISS/200 1 GET http://squid:8001/squid-internal-mgr/info - HIER_NONE/- text/plain\n"
        "1.000 1 10.0.0.5 TCP_MISS/200 1 GET http://deb.debian.org/old - HIER_NONE/- text/plain\n"
        "broken\n10000 bad bogus invalid size GET http://x - - -\n"
        "10001 0 127.0.0.1 NONE_NONE/000 0 - error:transaction-end-before-headers - HIER_NONE/- -\n"
    )
    data = read_requests(path, 10002)
    assert data["requests"] == 2
    assert data["hits"] == 1
    assert data["errors"] == 1
    assert data["saved_bytes"] == 2048
    assert data["client_count"] == 2
    assert data["recent"][2]["url"] == "http://deb.debian.org/pool/a.deb"
    assert sum(bucket["requests"] for bucket in data["timeline"]) == 2


def test_missing_and_empty_logs(tmp_path: Path) -> None:
    """Distinguish a missing log from an empty working log."""
    path = tmp_path / "missing.log"
    assert read_requests(path, 10)["available"] is False
    path.touch()
    assert read_requests(path, 10)["available"] is True
    assert read_requests(path, 10)["requests"] == 0


def test_many_clients(tmp_path: Path) -> None:
    """Count all clients while bounding the ranked list."""
    path = tmp_path / "access.log"
    path.write_text(
        "".join(
            f"1000 1 10.0.0.{i} TCP_MISS/200 1 GET http://deb.debian.org/a - - -\n"
            for i in range(20)
        )
    )
    data = read_requests(path, 1001)
    assert data["client_count"] == 20
    assert len(data["clients"]) == 10


def test_large_log_and_revalidated_hit(tmp_path: Path) -> None:
    """Bound log reads, discard a partial first line, and count revalidated hits."""
    path = tmp_path / "access.log"
    path.write_text(
        "x" * 2_100_000
        + "\n1000 1 10.0.0.2 TCP_REFRESH_UNMODIFIED/200 42 GET http://deb.debian.org/a - - -\n"
    )
    data = read_requests(path, 1001)
    assert data["requests"] == 1
    assert data["hits"] == 1
    assert data["sampled_bytes"] == 2_000_000


def test_timeline_includes_partial_first_bucket(tmp_path: Path) -> None:
    """Include the full last hour even between five-minute boundaries."""
    path = tmp_path / "access.log"
    path.write_text(
        "6500 1 10.0.0.2 TCP_HIT/200 42 GET http://deb.debian.org/a - - -\n"
    )
    data = read_requests(path, 10002)
    assert data["requests"] == 1
    assert sum(bucket["requests"] for bucket in data["timeline"]) == 1


def test_overview_online_and_offline(client: TestClient) -> None:
    """Retain log statistics when manager connectivity fails."""
    with patch(
        "app.main.manager_report",
        side_effect=lambda _, report: (
            "client_http.requests = 42" if report == "counters" else "Squid info"
        ),
    ):
        data = client.get("/api/overview").json()
        assert data["online"] is True
        assert data["counters"]["client_http.requests"] == 42
    with patch(
        "app.main.manager_report", side_effect=HTTPException(503, "unreachable")
    ):
        data = client.get("/api/overview").json()
        assert data["online"] is False
        assert data["error"] == "unreachable"
        assert data["storage"] == {"used_bytes": None, "capacity_bytes": None}


def test_action_and_report_allowlist(client: TestClient) -> None:
    """Deny arbitrary reports and cross-origin form-triggered actions."""
    assert client.get("/api/reports/shutdown").status_code == 404
    assert client.get("/api/reports/config").status_code == 404
    assert client.post("/api/actions/reconfigure").status_code == 403
    with patch("app.main.manager_report", return_value="ok") as report:
        response = client.post(
            "/api/actions/reconfigure", headers={"X-Requested-With": "squid-ui"}
        )
        assert response.status_code == 200
        assert report.call_args.args[1] == "reconfigure"


@pytest.mark.parametrize(
    ("status", "body"), [(403, b"denied"), (200, b"x" * 2_000_001)]
)
def test_manager_rejections(settings: Settings, status: int, body: bytes) -> None:
    """Translate denied and oversized responses into a safe API failure."""
    with patch("app.main.http.client.HTTPConnection") as connection:
        response = MagicMock(status=status)
        response.read.return_value = body
        connection.return_value.getresponse.return_value = response
        with pytest.raises(HTTPException) as error:
            manager_report(settings, "info")
        assert error.value.status_code == 502
        connection.return_value.close.assert_called_once()


def test_manager_connection_failure(settings: Settings) -> None:
    """Handle both socket and HTTP protocol failures."""
    for error in (OSError("refused"), http.client.BadStatusLine("bad")):
        with patch("app.main.http.client.HTTPConnection") as connection:
            connection.return_value.request.side_effect = error
            with pytest.raises(HTTPException) as raised:
                manager_report(settings, "info")
            assert raised.value.status_code == 503


def test_headers(client: TestClient) -> None:
    """Keep reports out of caches and disable embedding and inline scripts."""
    response = client.get("/")
    assert response.headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
