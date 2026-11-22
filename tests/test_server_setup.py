"""Exercise server setup with isolated paths and mocked host commands."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

type ServerSetup = tuple[Path, Path, Path, Path, Path, dict[str, str]]


@pytest.fixture
def server_setup(tmp_path: Path) -> ServerSetup:
    """Copy setup into an isolated project and replace system commands."""
    project = tmp_path / "project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    (project / ".env").write_text("# Test configuration\n")
    release = tmp_path / "os-release"
    release.write_text("ID=ubuntu\n")
    keyrings = tmp_path / "keyrings"
    keyrings.mkdir()
    key = keyrings / "docker.asc"
    key.write_text("existing-key")
    source_path = Path(__file__).resolve().parents[1] / "scripts/setup-server.sh"
    source = source_path.read_text()
    script = scripts / "setup-server.sh"
    script.write_text(
        source.replace("/etc/os-release", str(release)).replace(
            "/etc/apt/keyrings", str(keyrings)
        )
    )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    mock = binaries / "mock"
    mock.write_text(
        """#!/bin/sh
set -eu
command=${0##*/}
printf '%s\\n' "$command $*" >> "$SETUP_LOG"
case "$command" in
    id) printf '0\\n' ;;
    curl)
        [ "${DOWNLOAD_MODE:-success}" != fail ] || exit 22
        if [ "${DOWNLOAD_MODE:-success}" = empty ]; then
            : > "$4"
        else
            printf 'downloaded-key' > "$4"
        fi
        ;;
    install) exec "$REAL_INSTALL" "$@" ;;
    apt-get) exit "${APT_STATUS:-0}" ;;
    docker) exit 0 ;;
esac
"""
    )
    mock.chmod(0o755)
    for command in ("id", "curl", "install", "apt-get", "docker"):
        (binaries / command).symlink_to(mock)
    log = tmp_path / "commands.log"
    install = shutil.which("install")
    assert install is not None
    env = dict(
        os.environ,
        PATH=f"{binaries}:{os.environ['PATH']}",
        SETUP_LOG=str(log),
        REAL_INSTALL=install,
    )
    return script, project, release, key, log, env


def run_setup(
    server_setup: ServerSetup, **overrides: str
) -> subprocess.CompletedProcess[str]:
    """Run the copied script without touching host APT, Docker, or keyrings."""
    script, _, _, _, _, env = server_setup
    return subprocess.run(
        ["sh", str(script)],
        env=env | overrides,
        cwd=script.parent,
        capture_output=True,
        text=True,
        check=False,
    )


def test_server_setup_installs_key_before_apt_and_startup(
    server_setup: ServerSetup,
) -> None:
    """Install a readable key before updating APT on each setup run."""
    _, _, _, key, log, _ = server_setup
    for _ in range(2):
        result = run_setup(server_setup)
        assert result.returncode == 0, result.stderr
        assert key.read_text() == "downloaded-key"
        assert key.stat().st_mode & 0o777 == 0o644
    commands = log.read_text()
    assert commands.index("curl ") < commands.index("install -m 0644")
    assert commands.index("install -m 0644") < commands.index("apt-get update")
    assert commands.index("apt-get update") < commands.index(
        "docker compose up -d --build"
    )


@pytest.mark.parametrize("mode", ["fail", "empty"])
def test_server_setup_preserves_key_when_download_fails(
    server_setup: ServerSetup, mode: str
) -> None:
    """Keep the old key and skip APT and startup after unusable downloads."""
    _, _, _, key, log, _ = server_setup
    result = run_setup(server_setup, DOWNLOAD_MODE=mode)
    assert result.returncode != 0
    assert key.read_text() == "existing-key"
    assert "apt-get" not in log.read_text()
    assert "docker compose up" not in log.read_text()


def test_server_setup_stops_when_apt_fails(server_setup: ServerSetup) -> None:
    """Do not start containers if package index verification fails."""
    _, _, _, _, log, _ = server_setup
    result = run_setup(server_setup, APT_STATUS="100")
    assert result.returncode == 100
    assert "docker compose up" not in log.read_text()


@pytest.mark.parametrize("missing_env", [True, False])
def test_server_setup_rejects_missing_config_or_wrong_os(
    server_setup: ServerSetup, missing_env: bool
) -> None:
    """Reject invalid prerequisites before invoking host commands."""
    _, project, release, _, log, _ = server_setup
    if missing_env:
        (project / ".env").unlink()
    else:
        release.write_text("ID=debian\n")
    result = run_setup(server_setup)
    assert result.returncode != 0
    assert not log.exists()
