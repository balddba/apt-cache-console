"""Validate and persist the shared repository allowlist."""

import fcntl
import hashlib
import os
import re
import tempfile
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictStr


class MirrorUpdate(BaseModel):
    """Accept bounded hostnames and the revision the editor loaded."""

    model_config = ConfigDict(extra="forbid")
    domains: list[StrictStr] = Field(min_length=1, max_length=200)
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")


def normalize_domains(domains: list[str]) -> list[str]:
    """Reject URLs, wildcards, ports, and Squid syntax; deduplicate hostnames."""
    result = []
    for value in domains:
        domain = value.strip().lower()
        host = domain.removeprefix(".")
        labels = host.split(sep=".")
        if (
            len(host) > 253
            or len(labels) < 2
            or not re.fullmatch(pattern=r"[a-z]{2,63}", string=labels[-1])
            or any(
                not re.fullmatch(
                    pattern=r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", string=label
                )
                for label in labels
            )
        ):
            raise HTTPException(
                status_code=422,
                detail="Use repository hostnames only, such as download.docker.com or .ubuntu.com; no URLs, ports, or wildcards.",
            )
        if domain not in result:
            result.append(domain)
    return result


def read_mirrors(path: Path) -> tuple[bytes, dict]:
    """Read bounded storage and return editable entries with a revision."""
    with path.open(mode="rb") as stream:
        content = stream.read(65537)
    if len(content) > 65536:
        raise HTTPException(
            status_code=503, detail="Mirror allowlist is too large to edit."
        )
    domains = [
        line.split(sep="#", maxsplit=1)[0].strip()
        for line in content.decode(encoding="utf-8").splitlines()
        if line.split(sep="#", maxsplit=1)[0].strip()
    ]
    return content, {
        "domains": domains,
        "revision": hashlib.sha256(data=content).hexdigest(),
    }


def replace_mirrors(path: Path, content: bytes) -> None:
    """Atomically publish a complete, Squid-readable allowlist."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fchmod(fd=stream.fileno(), mode=0o644)
            os.fsync(fd=stream.fileno())
        temporary.replace(target=path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def mirror_store(
    path: Path,
    update: MirrorUpdate | None = None,
    reload: Callable[[], str] | None = None,
) -> dict:
    """Serialize reads and updates, rejecting stale edits and reverting failed reloads."""
    domains = normalize_domains(domains=update.domains) if update else None
    try:
        with (path.parent / ".mirrors.lock").open(mode="a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            original, current = read_mirrors(path=path)
            if update is None:
                return current
            if update.revision != current["revision"]:
                raise HTTPException(
                    status_code=409,
                    detail="Mirrors changed in another session. Reload the list before saving.",
                )
            if reload is None:
                raise TypeError("A reload callback is required to update mirrors.")
            assert domains is not None
            content = (
                "# Managed from the Squid dashboard.\n" + "\n".join(domains) + "\n"
            ).encode()
            replace_mirrors(path=path, content=content)
            try:
                reload()
            except HTTPException as exc:
                replace_mirrors(path=path, content=original)
                with suppress(HTTPException):
                    reload()
                raise HTTPException(
                    status_code=502,
                    detail="Squid reload failed. The previous allowlist was restored; check proxy diagnostics before retrying.",
                ) from exc
            _, result = read_mirrors(path=path)
            return {**result, "message": "Mirrors saved and Squid reload requested."}
    except (OSError, UnicodeError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Mirror storage is unavailable. Check the shared mirror volume and permissions.",
        ) from exc
