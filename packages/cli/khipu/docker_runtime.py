"""Locate, start, or install a Docker-compatible runtime for local Postgres.

Finder-launched GUI apps do not inherit Homebrew/Docker Desktop PATH, so
``shutil.which("docker")`` is not enough. We do not vendor Docker Desktop
(license); ``ensure_docker(install=True)`` downloads the official DMG from
``desktop.docker.com`` at the user's click, copies ``Docker.app`` into
``/Applications``, and opens it. First launch still needs Docker's own
Accept / privileged-helper prompt — that cannot be skipped.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from khipu.components_matrix import application_support_dir

DOCKER_APP = Path("/Applications/Docker.app")
ORBSTACK_APP = Path("/Applications/OrbStack.app")
DOCKER_WAIT_S = 180
DOCKER_DOWNLOAD_TIMEOUT_S = 600
DOCKER_DMG_MIN_BYTES = 50 * 1024 * 1024
DOCKER_DMG_HOST = "desktop.docker.com"


def docker_desktop_dmg_url() -> str:
    override = (os.environ.get("KHIPU_DOCKER_DMG_URL") or "").strip()
    if override:
        return override
    machine = platform.machine().lower()
    arch = "amd64" if machine in ("x86_64", "amd64") else "arm64"
    return f"https://{DOCKER_DMG_HOST}/mac/main/{arch}/Docker.dmg"


def docker_cli() -> Path | None:
    seen: set[str] = set()
    candidates: list[Path] = []
    which = shutil.which("docker")
    if which:
        candidates.append(Path(which))
    home = Path.home()
    candidates.extend(
        [
            Path("/usr/local/bin/docker"),
            home / ".docker" / "bin" / "docker",
            DOCKER_APP / "Contents" / "Resources" / "bin" / "docker",
            Path("/opt/homebrew/bin/docker"),
        ]
    )
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def docker_path_env(cli: Path) -> dict[str, str]:
    env = os.environ.copy()
    extras = [
        str(cli.parent),
        "/usr/local/bin",
        "/opt/homebrew/bin",
        str(Path.home() / ".docker" / "bin"),
        str(DOCKER_APP / "Contents" / "Resources" / "bin"),
    ]
    env["PATH"] = os.pathsep.join([*extras, env.get("PATH", "")])
    return env


def docker_app_installed() -> bool:
    return DOCKER_APP.is_dir()


def _run(
    args: list[str],
    *,
    timeout: float | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
        env=env,
    )


def _daemon_looks_stopped(err: str) -> bool:
    low = err.lower()
    needles = (
        "cannot connect to the docker daemon",
        "is the docker daemon running",
        "docker desktop is not running",
        "error during connect",
        "dockerdesktoplinuxengine",
    )
    return any(n in low for n in needles)


def docker_available() -> dict[str, Any]:
    cli = docker_cli()
    base: dict[str, Any] = {
        "app_installed": docker_app_installed(),
        "cli": str(cli) if cli is not None else None,
    }
    if cli is None:
        return {
            **base,
            "ok": False,
            "error": "docker_not_found",
            "code": "docker_not_found",
        }
    try:
        proc = _run(
            [str(cli), "info"],
            timeout=30,
            check=False,
            env=docker_path_env(cli),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            **base,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "code": "docker_info_failed",
        }
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or "docker info failed"
        code = "docker_daemon_stopped" if _daemon_looks_stopped(err) else "docker_info_failed"
        return {**base, "ok": False, "error": err, "code": code}
    return {**base, "ok": True}


def _start_container_runtime() -> str | None:
    if docker_app_installed():
        _run(["open", "-a", "Docker"], check=False)
        return "Docker"
    if ORBSTACK_APP.is_dir():
        _run(["open", "-a", "OrbStack"], check=False)
        return "OrbStack"
    colima = shutil.which("colima")
    if colima:
        _run([colima, "start"], timeout=120, check=False)
        return "colima"
    return None


def _wait_docker_ready(*, timeout_s: float | None = None) -> dict[str, Any]:
    if timeout_s is None:
        raw = (os.environ.get("KHIPU_DOCKER_WAIT_S") or "").strip()
        timeout_s = float(raw) if raw else DOCKER_WAIT_S
    deadline = time.monotonic() + timeout_s
    last = docker_available()
    if last.get("ok"):
        return last
    while time.monotonic() < deadline:
        last = docker_available()
        if last.get("ok"):
            return last
        time.sleep(2)
    return {
        **last,
        "code": last.get("code") or "docker_starting",
        "error": last.get("error") or "Docker Desktop is still starting",
    }


def _official_dmg_url_or_error(url: str) -> dict[str, Any] | None:
    if (os.environ.get("KHIPU_DOCKER_DMG_URL") or "").strip():
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != DOCKER_DMG_HOST:
        return {"ok": False, "error": "docker_dmg_url_not_official", "url": url}
    return None


def _download_docker_dmg(dest: Path) -> dict[str, Any]:
    url = docker_desktop_dmg_url()
    bad = _official_dmg_url_or_error(url)
    if bad:
        return bad
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "khipu-components/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=DOCKER_DOWNLOAD_TIMEOUT_S) as resp:
            with dest.open("wb") as out:
                shutil.copyfileobj(resp, out)
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        return {"ok": False, "error": f"docker_dmg_download_failed: {exc}", "code": "docker_download_failed"}
    size = dest.stat().st_size
    min_bytes = 1 if (os.environ.get("KHIPU_DOCKER_DMG_URL") or "").strip() else DOCKER_DMG_MIN_BYTES
    if size < min_bytes:
        dest.unlink(missing_ok=True)
        return {"ok": False, "error": "docker_dmg_too_small", "bytes": size, "code": "docker_download_failed"}
    return {"ok": True, "path": str(dest), "bytes": size}


def _install_docker_app_from_dmg(dmg: Path) -> dict[str, Any]:
    opened = {
        "ok": False,
        "code": "docker_dmg_opened",
        "error": "Drag Docker.app into Applications, then return here and recheck.",
    }
    mnt = Path(tempfile.mkdtemp(prefix="khipu-docker-dmg-"))
    attach = _run(
        [
            "hdiutil",
            "attach",
            str(dmg),
            "-nobrowse",
            "-readonly",
            "-mountpoint",
            str(mnt),
        ],
        check=False,
        timeout=60,
    )
    if attach.returncode != 0:
        _run(["open", str(dmg)], check=False)
        shutil.rmtree(mnt, ignore_errors=True)
        return opened
    try:
        src = mnt / "Docker.app"
        if not src.is_dir():
            apps = [p for p in mnt.glob("*.app") if p.is_dir()]
            if apps:
                src = apps[0]
        if not src.is_dir():
            _run(["open", str(dmg)], check=False)
            return opened
        dest = DOCKER_APP
        copy = _run(["ditto", str(src), str(dest)], check=False, timeout=120)
        if copy.returncode != 0 or not dest.is_dir():
            _run(["open", str(dmg)], check=False)
            return opened
        return {"ok": True, "path": str(dest)}
    finally:
        _run(["hdiutil", "detach", str(mnt), "-force"], check=False, timeout=60)
        shutil.rmtree(mnt, ignore_errors=True)


def ensure_docker(*, install: bool = False) -> dict[str, Any]:
    current = docker_available()
    if current.get("ok"):
        return {**current, "action": "already_ok"}

    started = _start_container_runtime()
    if started:
        waited = _wait_docker_ready()
        action = f"started_{started.lower()}"
        if waited.get("ok"):
            return {**waited, "action": action}
        return {**waited, "action": action, "code": waited.get("code") or "docker_starting"}

    if not install:
        return {
            **current,
            "action": "need_install",
            "code": current.get("code") or "docker_not_found",
        }

    dest = application_support_dir() / "cache" / "Docker.dmg"
    downloaded = _download_docker_dmg(dest)
    if not downloaded.get("ok"):
        return downloaded
    copied = _install_docker_app_from_dmg(dest)
    try:
        dest.unlink(missing_ok=True)
    except OSError as exc:
        copied = {**copied, "cache_cleanup_error": f"{type(exc).__name__}: {exc}"}
    if not copied.get("ok"):
        return copied
    _start_container_runtime()
    waited = _wait_docker_ready()
    if waited.get("ok"):
        return {**waited, "action": "installed_docker_desktop"}
    return {
        **waited,
        "action": "installed_docker_desktop",
        "code": waited.get("code") or "docker_starting",
        "error": waited.get("error")
        or "Docker Desktop is installed. Finish its first-launch prompts, then recheck.",
    }
