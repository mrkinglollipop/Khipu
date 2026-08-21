"""Local pg_dump + restore_drill for doctor backup_ok."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

from khipu.components_matrix import read_versions
from khipu.components_postgres import (
    PG_CONTAINER,
    _docker,
    _generate_password,
    _local_dsn,
    _run,
    docker_available,
    ensure_postgres_image,
)
from khipu.ops_events import record

DRILL_CONTAINER = "khipu-pg19-drill"
DRILL_VOLUME = "khipu-pgdata-drill"
RESTORE_TIMEOUT_S = 600
_CONTAINER_DUMP = "/tmp/khipu-backup.dump"


def _backup_dir() -> Path:
    d = Path.home() / "Library" / "Application Support" / "Khipu" / "backups" / "pg"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _env_pg_tool(var_name: str) -> str | None:
    val = (os.environ.get(var_name) or "").strip()
    return val or None


def _pg_dump_path() -> str | None:
    return _env_pg_tool("KHIPU_PG_DUMP")


def _pg_restore_path() -> str | None:
    return _env_pg_tool("KHIPU_PG_RESTORE")


def _password_from_dsn(dsn: str) -> str:
    if "@" not in dsn:
        return ""
    userinfo = dsn.split("@", 1)[0].split("://", 1)[-1]
    if ":" not in userinfo:
        return ""
    return unquote(userinfo.split(":", 1)[1])


def _docker_exec_pg(
    container: str,
    args: list[str],
    *,
    password: str = "",
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = ["exec"]
    if password:
        cmd.extend(["-e", f"PGPASSWORD={password}"])
    cmd.append(container)
    cmd.extend(args)
    return _docker(cmd, timeout=timeout, check=False)


def _proc_error(proc: subprocess.CompletedProcess[str], fallback: str) -> str:
    return (proc.stderr or proc.stdout or "").strip() or fallback


def _is_pg_restore_error_line(line: str) -> bool:
    lower = line.strip().lower()
    return lower.startswith("pg_restore: error:") or lower.startswith("error:")


def _restore_error_is_already_exists(line: str) -> bool:
    """OK when the error is an object collision after pre-CREATE EXTENSION.

    Radio A runs ``CREATE EXTENSION vector`` on an empty drill cluster, then
    ``pg_restore``. The dump TOC also creates the extension, its types, and
    operator classes — those lines are ``already exists``, not a failed drill.
    Do not use a bare ``vector`` substring (that would hide unrelated errors).
    Reject OOM / connect / other non-collision errors.
    """
    return "already exists" in line.lower()


def _pg_restore_ok(returncode: int, stdout: str, stderr: str) -> bool:
    """Exit 1 is success only when every error line is an already-exists collision.

    Do not treat every exit 1 as OK. Exit 0 succeeds. Exit 2+ fails. Exit 1
    with no error lines, or any error that is not already-exists, fails.
    """
    if returncode == 0:
        return True
    if returncode != 1:
        return False
    text = f"{stderr or ''}\n{stdout or ''}"
    lower_all = text.lower()
    if "fatal:" in lower_all or "panic:" in lower_all:
        return False
    error_lines = [ln for ln in text.splitlines() if _is_pg_restore_error_line(ln)]
    if not error_lines:
        return False
    return all(_restore_error_is_already_exists(ln) for ln in error_lines)


def _pg_restore_result(proc: subprocess.CompletedProcess[str]) -> dict:
    if _pg_restore_ok(proc.returncode, proc.stdout or "", proc.stderr or ""):
        return {"ok": True}
    return {"ok": False, "error": _proc_error(proc, "pg_restore failed")}


def _dump_live_db(dest: Path) -> dict:
    from khipu.db import resolve_dsn

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest_file = dest / f"khipu-local-{stamp}.dump"
    host_dump = _pg_dump_path()
    if host_dump:
        dsn = resolve_dsn()
        proc = _run(
            [
                host_dump,
                "--format=custom",
                "--no-owner",
                "--no-acl",
                f"--file={dest_file}",
                dsn,
            ],
            timeout=RESTORE_TIMEOUT_S,
            check=False,
        )
        if proc.returncode != 0:
            return {"ok": False, "error": _proc_error(proc, "pg_dump failed")}
        return {"ok": True, "path": str(dest_file), "bytes": dest_file.stat().st_size}

    try:
        dsn = resolve_dsn()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"dsn_unavailable: {exc}"}
    password = _password_from_dsn(dsn)
    try:
        dump_proc = _docker_exec_pg(
            PG_CONTAINER,
            [
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-acl",
                "-U",
                "khipu",
                "-d",
                "khipu",
                "-f",
                _CONTAINER_DUMP,
            ],
            password=password,
            timeout=RESTORE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "pg_dump_timeout"}
    if dump_proc.returncode != 0:
        return {"ok": False, "error": _proc_error(dump_proc, "pg_dump failed")}
    try:
        try:
            cp_proc = _docker(
                ["cp", f"{PG_CONTAINER}:{_CONTAINER_DUMP}", str(dest_file)],
                timeout=RESTORE_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "docker_cp_dump_timeout"}
        if cp_proc.returncode != 0:
            return {"ok": False, "error": _proc_error(cp_proc, "docker cp dump failed")}
        if not dest_file.is_file() or dest_file.stat().st_size == 0:
            return {"ok": False, "error": "dump_file_missing"}
        return {"ok": True, "path": str(dest_file), "bytes": dest_file.stat().st_size}
    finally:
        _docker_exec_pg(PG_CONTAINER, ["rm", "-f", _CONTAINER_DUMP])


def _restore_dump_file(
    dump_path: Path,
    *,
    container: str,
    dsn: str,
    password: str = "",
) -> dict:
    host_restore = _pg_restore_path()
    if host_restore:
        proc = _run(
            [
                host_restore,
                "--no-owner",
                "--no-acl",
                f"--dbname={dsn}",
                str(dump_path),
            ],
            timeout=RESTORE_TIMEOUT_S,
            check=False,
        )
        return _pg_restore_result(proc)

    try:
        cp_proc = _docker(
            ["cp", str(dump_path), f"{container}:{_CONTAINER_DUMP}"],
            timeout=RESTORE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _docker_exec_pg(container, ["rm", "-f", _CONTAINER_DUMP])
        return {"ok": False, "error": "docker_cp_restore_timeout"}
    if cp_proc.returncode != 0:
        return {"ok": False, "error": _proc_error(cp_proc, "docker cp restore failed")}
    try:
        try:
            proc = _docker_exec_pg(
                container,
                [
                    "pg_restore",
                    "--no-owner",
                    "--no-acl",
                    "-U",
                    "khipu",
                    "-d",
                    "khipu",
                    _CONTAINER_DUMP,
                ],
                password=password,
                timeout=RESTORE_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "pg_restore_timeout"}
        return _pg_restore_result(proc)
    finally:
        _docker_exec_pg(container, ["rm", "-f", _CONTAINER_DUMP])


def _start_drill_cluster(image: str, port: int, password: str) -> dict:
    # Wipe leftover datadir first — reusing khipu-pgdata-drill keeps the prior
    # POSTGRES_PASSWORD and auth then fails against a newly generated secret.
    _cleanup_drill()
    leftover = _docker(["volume", "inspect", DRILL_VOLUME], check=False)
    if leftover.returncode == 0:
        return {"ok": False, "error": "drill_volume_not_removed"}
    created = _docker(["volume", "create", DRILL_VOLUME], check=False)
    if created.returncode != 0:
        err = (created.stderr or created.stdout or "").strip()
        return {"ok": False, "error": err or "drill volume create failed"}
    proc = _docker(
        [
            "run",
            "-d",
            "--name",
            DRILL_CONTAINER,
            "-e",
            "POSTGRES_USER=khipu",
            "-e",
            "POSTGRES_DB=khipu",
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "-e",
            "POSTGRES_HOST_AUTH_METHOD=scram-sha-256",
            "-p",
            f"127.0.0.1:{port}:5432",
            "-v",
            f"{DRILL_VOLUME}:/var/lib/postgresql",
            image,
        ],
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return {"ok": False, "error": err or "drill container start failed"}
    return {"ok": True, "port": port, "password": password}


def _wait_drill_ready(port: int, password: str, *, timeout_s: float) -> bool:
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        ready = _docker_exec_pg(
            DRILL_CONTAINER,
            [
                "pg_isready",
                "-U",
                "khipu",
                "-d",
                "khipu",
            ],
            password=password,
        )
        if ready.returncode == 0:
            return True
        time.sleep(1)
    return False


def _restore_drill(dump_path: Path, port: int, password: str) -> dict:
    import psycopg

    dsn = _local_dsn(port, password)
    try:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.commit()
    except Exception as exc:  # noqa: BLE001 — connect/auth must not raise to Welcome
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return _restore_dump_file(
        dump_path,
        container=DRILL_CONTAINER,
        dsn=dsn,
        password=password,
    )


def _cleanup_drill() -> None:
    _docker(["rm", "-f", DRILL_CONTAINER], check=False)
    _docker(["volume", "rm", "-f", DRILL_VOLUME], check=False)


def bootstrap_local_backup() -> dict:
    docker = docker_available()
    if not docker.get("ok"):
        return docker

    versions = read_versions()
    pending = versions.get("pending")
    postgres = (
        versions.get("postgres") if isinstance(versions.get("postgres"), dict) else {}
    )
    image = ""
    if isinstance(pending, dict):
        image = str(pending.get("postgres_image") or "")
    if not image:
        image = str(postgres.get("image") or "")

    if not image:
        return {"ok": False, "error": "postgres_image_unknown"}

    image_result = ensure_postgres_image(image)
    if not image_result.get("ok"):
        return image_result

    dump_result = _dump_live_db(_backup_dir())
    if not dump_result.get("ok"):
        try:
            record("pg_dump", "fail", {"error": dump_result.get("error")})
        except Exception:  # noqa: BLE001
            pass
        return dump_result

    dump_path = Path(str(dump_result["path"]))
    drill_port = 54338
    password = _generate_password()
    started = _start_drill_cluster(image, drill_port, password)
    if not started.get("ok"):
        _cleanup_drill()
        return started

    try:
        if not _wait_drill_ready(drill_port, password, timeout_s=120):
            detail = {"error": "drill_cluster_not_ready"}
            record("restore_drill", "fail", detail)
            return {"ok": False, **detail}

        restore = _restore_drill(dump_path, drill_port, password)
        dump_event = record(
            "pg_dump",
            "ok",
            {"path": str(dump_path), "bytes": dump_result.get("bytes")},
        )
        if restore.get("ok"):
            record(
                "restore_drill",
                "ok",
                {"dump": str(dump_path), "drill_container": DRILL_CONTAINER},
            )
            return {"ok": True, "dump": dump_event, "restore_drill": True}
        record("restore_drill", "fail", {"error": restore.get("error")})
        return {"ok": False, "error": restore.get("error")}
    finally:
        _cleanup_drill()
