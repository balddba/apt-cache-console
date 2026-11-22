"""Exercise authenticated allowlist edits, validation, and failure recovery."""

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from httpx import Response

from app.main import Settings, create_app

type MirrorClient = tuple[TestClient, Path]


@pytest.fixture
def mirror_client(tmp_path: Path) -> MirrorClient:
    """Create an authenticated app with an isolated persistent allowlist."""
    path = tmp_path / "mirrors.acl"
    path.write_text(data="# Existing mirrors\n.ubuntu.com\ndownload.docker.com\n")
    settings = replace(Settings.load(), mirrors=path)
    client = TestClient(app=create_app(settings=settings))
    assert (
        client.post(
            url="/api/login",
            json={"username": settings.username, "password": settings.password},
            headers={"X-Requested-With": "squid-ui"},
        ).status_code
        == 204
    )
    return client, path


def update(
    client: TestClient,
    domains: Sequence[str | int],
    revision: str | None = None,
    headers: dict[str, str] | None = None,
) -> Response:
    """Submit a versioned allowlist update with action protection by default."""
    if revision is None:
        revision = client.get(url="/api/mirrors").json()["revision"]
    return client.put(
        url="/api/mirrors",
        json={"domains": domains, "revision": revision},
        headers={"X-Requested-With": "squid-ui"} if headers is None else headers,
    )


def test_save_normalizes_and_persists(mirror_client: MirrorClient) -> None:
    """Normalize hostnames, retain subdomain semantics, and publish readable storage."""
    client, path = mirror_client
    before = client.get(url="/api/mirrors").json()
    assert before["domains"] == [".ubuntu.com", "download.docker.com"]
    with patch(target="app.main.manager_report", return_value="ok") as manager:
        response = update(
            client=client,
            domains=[" Download.Docker.Com ", ".ubuntu.com", "download.docker.com"],
        )
    assert response.status_code == 200
    assert response.json()["domains"] == ["download.docker.com", ".ubuntu.com"]
    assert response.json()["revision"] != before["revision"]
    assert (
        client.get(url="/api/mirrors").json()["domains"] == response.json()["domains"]
    )
    assert path.stat().st_mode & 0o777 == 0o644
    manager.assert_called_once()
    assert manager.call_args.kwargs["report"] == "reconfigure"


@pytest.mark.parametrize(
    argnames="domain",
    argvalues=[
        "https://download.docker.com/linux/ubuntu",
        "*.docker.com",
        "docker.com:443",
        "all",
        "10.1.4.176",
        ".com",
        "-bad.com",
        "bad-.com",
        "docker.com\nhttp_access allow all",
        "docker.com # comment",
        "a..com",
        "",
        "x" * 64 + ".com",
    ],
)
def test_invalid_domains_never_write_or_reload(
    mirror_client: MirrorClient, domain: str
) -> None:
    """Reject malformed domains and configuration injection before changing storage."""
    client, path = mirror_client
    original = path.read_bytes()
    with patch(target="app.main.manager_report") as manager:
        assert update(client=client, domains=[domain]).status_code == 422
    assert path.read_bytes() == original
    manager.assert_not_called()


@pytest.mark.parametrize(argnames="domains", argvalues=[[], ["docker.com"] * 201, [42]])
def test_bounded_payload(
    mirror_client: MirrorClient, domains: Sequence[str | int]
) -> None:
    """Bound entry counts and reject non-string hostnames."""
    client, _ = mirror_client
    assert update(client=client, domains=domains).status_code == 422


def test_stale_editor(mirror_client: MirrorClient) -> None:
    """Reject stale browser revisions without overwriting a newer list."""
    client, path = mirror_client
    revision = client.get(url="/api/mirrors").json()["revision"]
    path.write_text(data="example.com\n")
    with patch(target="app.main.manager_report") as manager:
        assert (
            update(client=client, domains=["docker.com"], revision=revision).status_code
            == 409
        )
    assert path.read_text() == "example.com\n"
    manager.assert_not_called()


def test_reload_failure_restores_previous_file(mirror_client: MirrorClient) -> None:
    """Restore the original bytes and request recovery when the reload fails."""
    client, path = mirror_client
    original = path.read_bytes()
    with patch(
        target="app.main.manager_report",
        side_effect=HTTPException(status_code=503, detail="offline"),
    ) as manager:
        response = update(client=client, domains=["docker.com"])
    assert response.status_code == 502
    assert path.read_bytes() == original
    assert manager.call_count == 2


def test_storage_failures(mirror_client: MirrorClient) -> None:
    """Surface missing and unwritable shared storage without reporting success."""
    client, path = mirror_client
    with patch(target="app.mirrors.replace_mirrors", side_effect=PermissionError):
        assert update(client=client, domains=["docker.com"]).status_code == 503
    path.unlink()
    assert client.get(url="/api/mirrors").status_code == 503


def test_auth_and_action_boundary(mirror_client: MirrorClient) -> None:
    """Require authentication and the action header for edits."""
    client, _ = mirror_client
    cookie = client.cookies.get("squid_ui_session")
    assert cookie is not None
    client.cookies.clear()
    assert client.get(url="/api/mirrors").status_code == 401
    assert client.put(url="/api/mirrors", json={}).status_code == 401
    client.cookies.set(name="squid_ui_session", value=cookie)
    assert update(client=client, domains=["docker.com"], headers={}).status_code == 403
    revision = client.get(url="/api/mirrors").json()["revision"]
    assert (
        client.put(
            url="/api/mirrors",
            json={"domains": ["docker.com"], "revision": revision, "extra": True},
            headers={"X-Requested-With": "squid-ui"},
        ).status_code
        == 422
    )
