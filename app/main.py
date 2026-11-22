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
        password = os.environ.get(key="UI_PASSWORD", default="")
        manager = os.environ.get(key="SQUID_MANAGER_PASSWORD", default="")
        if len(password) < 8:
            raise ValueError("UI_PASSWORD must be at least 8 characters")
        if not re.fullmatch(pattern=r"[A-Za-z0-9_-]{24,128}", string=manager):
            raise ValueError("SQUID_MANAGER_PASSWORD must be 24-128 safe characters")
        host = os.environ.get(key="SQUID_HOST", default="squid-deb-proxy")
        port = int(os.environ.get(key="SQUID_PORT", default="8001"))
        username = os.environ.get(key="UI_USERNAME", default="admin")
        if (
            not re.fullmatch(pattern=r"[A-Za-z0-9.-]+", string=host)
            or not 1 <= port <= 65535
        ):
            raise ValueError("Invalid Squid host or port")
        if not username or ":" in username:
            raise ValueError("UI_USERNAME must be nonempty and cannot contain a colon")
        cookie_secure = os.environ.get(key="UI_COOKIE_SECURE", default="false").lower()
        if cookie_secure not in {"true", "false"}:
            raise ValueError("UI_COOKIE_SECURE must be true or false")
        return cls(
            host=host,
            port=port,
            manager_password=manager,
            username=username,
            password=password,
            log=Path(
                os.environ.get(
                    key="ACCESS_LOG", default="/var/log/squid-deb-proxy/access.log"
                )
            ),
            mirrors=Path(
                os.environ.get(
                    key="MIRRORS_FILE",
                    default="/var/lib/squid-mirrors/mirrors.acl",
                )
            ),
            cookie_secure=cookie_secure == "true",
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
    connection = http.client.HTTPConnection(
        host=settings.host, port=settings.port, timeout=4
    )
    token = base64.b64encode(s=f"admin:{settings.manager_password}".encode()).decode(
        encoding="utf-8"
    )
    try:
        connection.request(
            method="GET",
            url=f"http://{settings.host}:{settings.port}/squid-internal-mgr/{report}",
            headers={"Authorization": f"Basic {token}"},
        )
        response = connection.getresponse()
        body = response.read(amt=2_000_001)
        if response.status != 200 or len(body) > 2_000_000:
            raise HTTPException(
                status_code=502,
                detail=f"Squid rejected the {report} request (HTTP {response.status})",
            )
        return body.decode(encoding="utf-8", errors="replace")
    except (OSError, http.client.HTTPException) as exc:
        raise HTTPException(
            status_code=503, detail="Cannot reach Squid's cache manager"
        ) from exc
    finally:
        connection.close()


def parse_counters(body: str) -> dict[str, float]:
    """Extract numeric manager counters without assuming a Squid version."""
    return {
        key: float(value)
        for key, value in re.findall(
            pattern=r"^([\w.]+)\s*=\s*([\d.eE+-]+)\s*$",
            string=body,
            flags=re.MULTILINE,
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
            pattern=rf"^\s*{label}\s*:\s*([0-9]+(?:\.[0-9]+)?)\s+KB\s*$",
            string=body,
            flags=re.MULTILINE,
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
        with path.open(mode="rb") as stream:
            stream.seek(0, 2)
            log_size = stream.tell()
            stream.seek(max(0, log_size - 2_000_000))
            if log_size > 2_000_000:
                stream.readline()
            lines = (
                stream.read(2_000_000)
                .decode(encoding="utf-8", errors="replace")
                .splitlines()
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
            url = urlsplit(url=parts[6])
            if "/squid-internal-mgr/" in url.path:
                continue
            # Do not send URL credentials, query parameters or fragments to browsers.
            clean_url = urlunsplit(
                components=(url.scheme, url.hostname or "", url.path, "", "")
            )
            status = int(parts[3].rsplit(sep="/", maxsplit=1)[1])
            if status == 0 or parts[5] == "-":
                continue
            rows.append(
                {
                    "timestamp": timestamp,
                    "duration": duration,
                    "client": parts[2],
                    "result": parts[3].split(sep="/")[0],
                    "status": status,
                    "bytes": size,
                    "method": parts[5],
                    "url": clean_url,
                    "host": url.hostname or "tunnel",
                    "hit": "HIT" in parts[3]
                    or parts[3].split(sep="/")[0]
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
            {"client": key, "requests": count}
            for key, count in clients.most_common(n=10)
        ],
        "hosts": [
            {"host": key, "requests": count} for key, count in hosts.most_common(n=6)
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
        if not sessions.valid(token=request.cookies.get(COOKIE_NAME)):
            raise HTTPException(status_code=401, detail="Please sign in to continue")

    def require_action_header(request: Request) -> None:
        """Reject cross-origin form submissions to session-changing endpoints."""
        if request.headers.get(key="X-Requested-With") != "squid-ui":
            raise HTTPException(status_code=403, detail="Missing action request header")

    @application.post(
        path="/api/login", dependencies=[Depends(dependency=require_action_header)]
    )
    def login(credentials: LoginCredentials, request: Request) -> Response:
        """Verify credentials and rotate the browser's opaque session cookie."""
        valid_user = hmac.compare_digest(
            credentials.username.encode(encoding="utf-8"),
            config.username.encode(encoding="utf-8"),
        )
        valid_password = hmac.compare_digest(
            credentials.password.encode(encoding="utf-8"),
            config.password.encode(encoding="utf-8"),
        )
        if not (valid_user and valid_password):
            raise HTTPException(status_code=401, detail="Invalid username or password")
        sessions.discard(token=request.cookies.get(COOKIE_NAME))
        response = Response(status_code=204)
        response.set_cookie(
            key=COOKIE_NAME,
            value=sessions.create(),
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=config.cookie_secure,
            samesite="strict",
        )
        return response

    @application.post(
        path="/api/logout", dependencies=[Depends(dependency=require_action_header)]
    )
    def logout(request: Request) -> Response:
        """Revoke the session and clear the cookie, even if it has expired."""
        sessions.discard(token=request.cookies.get(COOKIE_NAME))
        response = Response(status_code=204)
        response.delete_cookie(
            key=COOKIE_NAME,
            httponly=True,
            secure=config.cookie_secure,
            samesite="strict",
        )
        return response

    @application.get(path="/login", response_model=None)
    def login_page(request: Request) -> FileResponse | RedirectResponse:
        """Show the sign-in form or return an authenticated user to the dashboard."""
        if sessions.valid(token=request.cookies.get(COOKIE_NAME)):
            return RedirectResponse(url="/", status_code=303)
        return FileResponse(path=static / "login.html")

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

    @application.get(path="/healthz")
    def health() -> dict:
        """Report UI process health independently of proxy availability."""
        return {"status": "ok"}

    @application.get(path="/", response_model=None)
    def index(request: Request) -> FileResponse | RedirectResponse:
        """Serve the authenticated dashboard shell."""
        if not sessions.valid(token=request.cookies.get(COOKIE_NAME)):
            return RedirectResponse(url="/login", status_code=303)
        return FileResponse(path=static / "index.html")

    @application.get(
        path="/api/overview", dependencies=[Depends(dependency=authenticate)]
    )
    def overview() -> dict:
        """Combine live lifetime counters and bounded access log statistics."""
        data = {
            "online": False,
            "error": None,
            "counters": {},
            "info": "",
            "storage": parse_storage(body=""),
            "fetched_at": time.time(),
            "activity": read_requests(path=config.log, now=time.time()),
        }
        try:
            with ThreadPoolExecutor(max_workers=3) as pool:
                counters = pool.submit(
                    manager_report, settings=config, report="counters"
                )
                info = pool.submit(manager_report, settings=config, report="info")
                storage = pool.submit(
                    manager_report, settings=config, report="storedir"
                )
                data["counters"] = parse_counters(body=counters.result())
                data["info"] = info.result()
                # Keep live traffic available when the storage report fails.
                with suppress(HTTPException):
                    data["storage"] = parse_storage(body=storage.result())
            data["online"] = True
        except HTTPException as exc:
            data["error"] = exc.detail
        return data

    @application.get(
        path="/api/reports/{report}",
        dependencies=[Depends(dependency=authenticate)],
        response_class=PlainTextResponse,
    )
    def report(report: str) -> str:
        """Return one of the explicitly permitted diagnostic reports."""
        if report not in {"info", "counters", "5min", "utilization", "storedir"}:
            raise HTTPException(status_code=404, detail="Unknown report")
        return manager_report(settings=config, report=report)

    @application.get(
        path="/api/mirrors", dependencies=[Depends(dependency=authenticate)]
    )
    def mirrors() -> dict:
        """Return the configured repository allowlist and its editing revision."""
        return mirror_store(path=config.mirrors)

    @application.put(
        path="/api/mirrors", dependencies=[Depends(dependency=authenticate)]
    )
    def update_mirrors(update: MirrorUpdate, request: Request) -> dict:
        """Validate and atomically apply an authenticated allowlist update."""
        if request.headers.get(key="X-Requested-With") != "squid-ui":
            raise HTTPException(status_code=403, detail="Missing action request header")
        return mirror_store(
            path=config.mirrors,
            update=update,
            reload=lambda: manager_report(settings=config, report="reconfigure"),
        )

    @application.post(
        path="/api/actions/reconfigure",
        dependencies=[Depends(dependency=authenticate)],
    )
    def reconfigure(request: Request) -> dict:
        """Reload mounted configuration after checking the custom request header."""
        if request.headers.get(key="X-Requested-With") != "squid-ui":
            raise HTTPException(status_code=403, detail="Missing action request header")
        manager_report(settings=config, report="reconfigure")
        return {
            "message": "Configuration reload requested. Check diagnostics to verify proxy health."
        }

    application.mount(path="/static", app=StaticFiles(directory=static), name="static")
    return application


app = create_app()
