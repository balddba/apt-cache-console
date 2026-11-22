"""Authenticated dashboard and a bounded interface to Squid's cache manager."""

import base64
import hmac
import http.client
import math
import os
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlsplit, urlunsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.base import RequestResponseEndpoint

from app.auth import COOKIE_NAME, SESSION_SECONDS, Sessions
from app.mirrors import MirrorUpdate, mirror_store


@dataclass(frozen=True)
class Settings:
    """Validated runtime configuration."""

    host: str
    port: int
    manager_password: str
    username: str
    password: str
    log: Path
    mirrors: Path = Path("/var/lib/squid-mirrors/mirrors.acl")
    cookie_secure: bool = False

    @classmethod
    def load(cls) -> Settings:
        """Load environment configuration and reject unsafe or missing values."""
        password = os.environ.get("UI_PASSWORD", "")
        manager = os.environ.get("SQUID_MANAGER_PASSWORD", "")
        if len(password) < 8:
            raise ValueError("UI_PASSWORD must be at least 8 characters")
        if not re.fullmatch(r"[A-Za-z0-9_-]{24,128}", manager):
            raise ValueError("SQUID_MANAGER_PASSWORD must be 24-128 safe characters")
        host = os.environ.get("SQUID_HOST", "squid-deb-proxy")
        port = int(os.environ.get("SQUID_PORT", "8001"))
        username = os.environ.get("UI_USERNAME", "admin")
        if not re.fullmatch(r"[A-Za-z0-9.-]+", host) or not 1 <= port <= 65535:
            raise ValueError("Invalid Squid host or port")
        if not username or ":" in username:
            raise ValueError("UI_USERNAME must be nonempty and cannot contain a colon")
        cookie_secure = os.environ.get("UI_COOKIE_SECURE", "false").lower()
        if cookie_secure not in {"true", "false"}:
            raise ValueError("UI_COOKIE_SECURE must be true or false")
        return cls(
            host,
            port,
            manager,
            username,
            password,
            Path(os.environ.get("ACCESS_LOG", "/var/log/squid-deb-proxy/access.log")),
            Path(os.environ.get("MIRRORS_FILE", "/var/lib/squid-mirrors/mirrors.acl")),
            cookie_secure == "true",
        )


class LoginCredentials(BaseModel):
    """Bound the credential payload accepted by the login endpoint."""

    model_config = ConfigDict(extra="forbid")
    username: str = Field(strict=True, min_length=1, max_length=256)
    password: str = Field(strict=True, min_length=1, max_length=4096)


def manager_report(settings: Settings, report: str) -> str:
    """Fetch a bounded cache manager response with a finite timeout.

    Args:
        settings (Settings): Connection settings.
        report (str): Internal report or action selected by the application.

    Returns:
        str: Squid's text response.

    Raises:
        HTTPException: Squid is unavailable or rejects the request.
    """
    connection = http.client.HTTPConnection(settings.host, settings.port, timeout=4)
    token = base64.b64encode(f"admin:{settings.manager_password}".encode()).decode()
    try:
        connection.request(
            "GET",
            f"http://{settings.host}:{settings.port}/squid-internal-mgr/{report}",
            headers={"Authorization": f"Basic {token}"},
        )
        response = connection.getresponse()
        body = response.read(2_000_001)
        if response.status != 200 or len(body) > 2_000_000:
            raise HTTPException(
                502, f"Squid rejected the {report} request (HTTP {response.status})"
            )
        return body.decode("utf-8", errors="replace")
    except (OSError, http.client.HTTPException) as exc:
        raise HTTPException(503, "Cannot reach Squid's cache manager") from exc
    finally:
        connection.close()


def parse_counters(body: str) -> dict[str, float]:
    """Extract numeric manager counters without assuming a Squid version."""
    return {
        key: float(value)
        for key, value in re.findall(
            r"^([\w.]+)\s*=\s*([\d.eE+-]+)\s*$", body, re.MULTILINE
        )
    }


def parse_storage(body: str) -> dict[str, int | None]:
    """Extract total disk cache usage and its configured limit from storedir.

    Squid labels these values as KB but reports units of 1024 bytes.
    Missing or malformed totals remain unknown instead of appearing empty.
    """
    storage = {}
    for key, label in (
        ("used_bytes", "Current Store Swap Size"),
        ("capacity_bytes", "Maximum Swap Size"),
    ):
        match = re.search(
            rf"^\s*{label}\s*:\s*([0-9]+(?:\.[0-9]+)?)\s+KB\s*$",
            body,
            re.MULTILINE,
        )
        value = float(match[1]) * 1024 if match else None
        storage[key] = (
            int(value) if value is not None and math.isfinite(value) else None
        )
    return storage


class AccessLogRow(TypedDict):
    """Represent one parsed Squid request with its original field types."""

    timestamp: float
    duration: int
    client: str
    result: str
    status: int
    bytes: int
    method: str
    url: str
    host: str
    hit: bool
    error: bool


def read_requests(path: Path, now: float) -> dict:
    """Summarize at most the last 2 MB of the current access log.

    Args:
        path (Path): Shared Squid native-format access log.
        now (float): Current Unix time.

    Returns:
        dict: Window totals, time buckets, client totals, and recent requests.
    """
    rows: list[AccessLogRow] = []
    available = True
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            log_size = stream.tell()
            stream.seek(max(0, log_size - 2_000_000))
            if log_size > 2_000_000:
                stream.readline()
            lines = (
                stream.read(2_000_000).decode("utf-8", errors="replace").splitlines()
            )
    except OSError:
        available = False
        lines = []
    for line in lines:
        parts = line.split()
        if len(parts) < 10:
            continue
        try:
            timestamp, duration, size = float(parts[0]), int(parts[1]), int(parts[4])
            url = urlsplit(parts[6])
            if "/squid-internal-mgr/" in url.path:
                continue
            # Do not send URL credentials, query parameters or fragments to browsers.
            clean_url = urlunsplit((url.scheme, url.hostname or "", url.path, "", ""))
            status = int(parts[3].rsplit("/", 1)[1])
            if status == 0 or parts[5] == "-":
                continue
            rows.append(
                {
                    "timestamp": timestamp,
                    "duration": duration,
                    "client": parts[2],
                    "result": parts[3].split("/")[0],
                    "status": status,
                    "bytes": size,
                    "method": parts[5],
                    "url": clean_url,
                    "host": url.hostname or "tunnel",
                    "hit": "HIT" in parts[3]
                    or parts[3].split("/")[0]
                    in {"TCP_REFRESH_UNMODIFIED", "TCP_REFRESH_FAIL_OLD"},
                    "error": status >= 400,
                }
            )
        except ValueError, IndexError:
            continue
    window = [row for row in rows if now - 3600 <= row["timestamp"] <= now]
    clients = Counter(row["client"] for row in window)
    hosts = Counter(row["host"] for row in window)
    buckets = []
    end = int(now // 300) * 300
    for start in range(end - 3600, end + 1, 300):
        group = [row for row in window if start <= row["timestamp"] < start + 300]
        buckets.append(
            {
                "timestamp": start,
                "requests": len(group),
                "hits": sum(row["hit"] for row in group),
                "bytes": sum(row["bytes"] for row in group),
            }
        )
    return {
        "available": available,
        "sampled_bytes": min(log_size, 2_000_000) if available else 0,
        "requests": len(window),
        "hits": sum(row["hit"] for row in window),
        "errors": sum(row["error"] for row in window),
        "bytes": sum(row["bytes"] for row in window),
        "saved_bytes": sum(row["bytes"] for row in window if row["hit"]),
        "client_count": len(clients),
        "clients": [
            {"client": key, "requests": count} for key, count in clients.most_common(10)
        ],
        "hosts": [
            {"host": key, "requests": count} for key, count in hosts.most_common(6)
        ],
        "timeline": buckets,
        "recent": list(reversed(rows[-100:])),
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the dashboard app with injectable configuration for testing."""
    config = settings or Settings.load()
    application = FastAPI(
        title="APT Cache Console", docs_url=None, redoc_url=None, openapi_url=None
    )
    sessions = Sessions()
    static = Path(__file__).parent / "static"

    def authenticate(request: Request) -> None:
        """Require an active browser session without triggering HTTP Basic UI."""
        if not sessions.valid(request.cookies.get(COOKIE_NAME)):
            raise HTTPException(401, "Please sign in to continue")

    def require_action_header(request: Request) -> None:
        """Reject cross-origin form submissions to session-changing endpoints."""
        if request.headers.get("X-Requested-With") != "squid-ui":
            raise HTTPException(403, "Missing action request header")

    @application.post("/api/login", dependencies=[Depends(require_action_header)])
    def login(credentials: LoginCredentials, request: Request) -> Response:
        """Verify credentials and rotate the browser's opaque session cookie."""
        valid_user = hmac.compare_digest(
            credentials.username.encode(), config.username.encode()
        )
        valid_password = hmac.compare_digest(
            credentials.password.encode(), config.password.encode()
        )
        if not (valid_user and valid_password):
            raise HTTPException(401, "Invalid username or password")
        sessions.discard(request.cookies.get(COOKIE_NAME))
        response = Response(status_code=204)
        response.set_cookie(
            COOKIE_NAME,
            sessions.create(),
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=config.cookie_secure,
            samesite="strict",
        )
        return response

    @application.post("/api/logout", dependencies=[Depends(require_action_header)])
    def logout(request: Request) -> Response:
        """Revoke the session and clear the cookie, even if it has expired."""
        sessions.discard(request.cookies.get(COOKIE_NAME))
        response = Response(status_code=204)
        response.delete_cookie(
            COOKIE_NAME, httponly=True, secure=config.cookie_secure, samesite="strict"
        )
        return response

    @application.get("/login", response_model=None)
    def login_page(request: Request) -> FileResponse | RedirectResponse:
        """Show the sign-in form or return an authenticated user to the dashboard."""
        if sessions.valid(request.cookies.get(COOKIE_NAME)):
            return RedirectResponse("/", status_code=303)
        return FileResponse(static / "login.html")

    @application.middleware("http")
    async def security_headers(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Apply browser protections and prevent caching authenticated responses."""
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @application.get("/healthz")
    def health() -> dict:
        """Report UI process health independently of proxy availability."""
        return {"status": "ok"}

    @application.get("/", response_model=None)
    def index(request: Request) -> FileResponse | RedirectResponse:
        """Serve the authenticated dashboard shell."""
        if not sessions.valid(request.cookies.get(COOKIE_NAME)):
            return RedirectResponse("/login", status_code=303)
        return FileResponse(static / "index.html")

    @application.get("/api/overview", dependencies=[Depends(authenticate)])
    def overview() -> dict:
        """Combine live lifetime counters and bounded access log statistics."""
        data = {
            "online": False,
            "error": None,
            "counters": {},
            "info": "",
            "storage": parse_storage(""),
            "fetched_at": time.time(),
            "activity": read_requests(config.log, time.time()),
        }
        try:
            with ThreadPoolExecutor(max_workers=3) as pool:
                counters = pool.submit(manager_report, config, "counters")
                info = pool.submit(manager_report, config, "info")
                storage = pool.submit(manager_report, config, "storedir")
                data["counters"] = parse_counters(counters.result())
                data["info"] = info.result()
                # Keep live traffic available when the storage report fails.
                with suppress(HTTPException):
                    data["storage"] = parse_storage(storage.result())
            data["online"] = True
        except HTTPException as exc:
            data["error"] = exc.detail
        return data

    @application.get(
        "/api/reports/{report}",
        dependencies=[Depends(authenticate)],
        response_class=PlainTextResponse,
    )
    def report(report: str) -> str:
        """Return one of the explicitly permitted diagnostic reports."""
        if report not in {"info", "counters", "5min", "utilization", "storedir"}:
            raise HTTPException(404, "Unknown report")
        return manager_report(config, report)

    @application.get("/api/mirrors", dependencies=[Depends(authenticate)])
    def mirrors() -> dict:
        """Return the configured repository allowlist and its editing revision."""
        return mirror_store(config.mirrors)

    @application.put("/api/mirrors", dependencies=[Depends(authenticate)])
    def update_mirrors(update: MirrorUpdate, request: Request) -> dict:
        """Validate and atomically apply an authenticated allowlist update."""
        if request.headers.get("X-Requested-With") != "squid-ui":
            raise HTTPException(403, "Missing action request header")
        return mirror_store(
            config.mirrors, update, lambda: manager_report(config, "reconfigure")
        )

    @application.post("/api/actions/reconfigure", dependencies=[Depends(authenticate)])
    def reconfigure(request: Request) -> dict:
        """Reload mounted configuration after checking the custom request header."""
        if request.headers.get("X-Requested-With") != "squid-ui":
            raise HTTPException(403, "Missing action request header")
        manager_report(config, "reconfigure")
        return {
            "message": "Configuration reload requested. Check diagnostics to verify proxy health."
        }

    application.mount("/static", StaticFiles(directory=static), name="static")
    return application


app = create_app()
