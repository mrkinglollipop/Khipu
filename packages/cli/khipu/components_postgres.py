"""Local PostgreSQL 19 Docker installer + remote probes."""

from __future__ import annotations

import secrets
import shutil
import socket
import subprocess
import time
import urllib.error
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from khipu.components_matrix import (
    effective_matrix,
    find_full_row,
    is_forbidden_postgres_image,
    khipu_app_version,
    read_versions,
    versions_path,
    write_versions,
)
from khipu.keychain import set_dsn
from khipu.paths import repo_root

PG_CONTAINER = "khipu-pg19"
PG_VOLUME = "khipu-pgdata"
DEFAULT_PORT = 54329
PORT_RANGE = range(DEFAULT_PORT, DEFAULT_PORT + 11)
PG19_MIN_NUM = 190_000
DOCKER_PULL_TIMEOUT_S = 600
DOCKER_BUILD_TIMEOUT_S = 1800
READY_TIMEOUT_S = 120
DISK_WARN_GIB = 10


def _run(
    args: list[str],
    *,
    timeout: float | None = None,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
        input=input_text,
    )


def docker_available() -> dict[str, Any]:
    if shutil.which("docker") is None:
        return {"ok": False, "error": "docker_not_found"}
    try:
        proc = _run(["docker", "info"], timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return {"ok": False, "error": err or "docker info failed"}
    return {"ok": True}


def free_disk_gib(path: Path | None = None) -> float | None:
    target = path or Path.home()
    try:
        usage = shutil.disk_usage(target)
    except OSError:
        return None
    return usage.free / (1024**3)


def disk_headroom_warning() -> dict[str, Any] | None:
    free = free_disk_gib()
    if free is None:
        return None
    if free < DISK_WARN_GIB:
        return {
            "warning": "low_disk_space",
            "free_gib": round(free, 2),
            "min_recommended_gib": DISK_WARN_GIB,
        }
    return None


def server_version_num(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SHOW server_version_num")
        row = cur.fetchone()
    return int(row[0])


def server_version_string(conn) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT current_setting('server_version')")
        row = cur.fetchone()
    return str(row[0])


def pgvector_extversion(conn) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        row = cur.fetchone()
    return str(row[0]) if row else None


def require_pg19_num(version_num: int) -> dict[str, Any]:
    if version_num < PG19_MIN_NUM:
        return {
            "ok": False,
            "error": "postgres_version_too_old",
            "server_version_num": version_num,
            "required_min": PG19_MIN_NUM,
        }
    return {"ok": True, "server_version_num": version_num}


def check_server_version_num(version_num: int) -> dict[str, Any]:
    """Public gate used by tests and remote preflight."""
    return require_pg19_num(version_num)


def probe_graph_table(conn) -> dict[str, Any]:
    with conn.cursor() as cur:
        try:
            cur.execute(
                """
                SELECT * FROM GRAPH_TABLE (
                  alzy_graph
                  MATCH (a IS node)-[r IS edge]->(b IS node)
                  COLUMNS (a.id AS src, b.id AS dst, r.type AS edge_type)
                ) LIMIT 1
                """
            )
            cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True}


def check_remote_postgres(*, full: bool = False) -> dict[str, Any]:
    from khipu.db import connect

    try:
        with connect() as conn:
            ver_num = server_version_num(conn)
            gate = require_pg19_num(ver_num)
            if not gate.get("ok"):
                return {
                    **gate,
                    "server_version": server_version_string(conn),
                }
            out: dict[str, Any] = {
                "ok": True,
                "server_version": server_version_string(conn),
                "server_version_num": ver_num,
            }
            if not full:
                return out
            ext = pgvector_extversion(conn)
            if not ext:
                return {
                    "ok": False,
                    "error": "vector_extension_missing",
                    "server_version": out["server_version"],
                }
            graph = probe_graph_table(conn)
            if not graph.get("ok"):
                return {
                    "ok": False,
                    "error": "graph_table_probe_failed",
                    "pgvector": ext,
                    "detail": graph.get("error"),
                    "server_version": out["server_version"],
                }
            out["pgvector"] = ext
            return out
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def choose_host_port() -> int | None:
    for port in PORT_RANGE:
        if not _port_in_use(port):
            return port
    return None


def _docker(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return _run(["docker", *args], **kwargs)


def pull_postgres_image(image: str) -> dict[str, Any]:
    if is_forbidden_postgres_image(image):
        return {"ok": False, "error": "forbidden_postgres_image", "image": image}
    try:
        proc = _docker(["pull", image], timeout=DOCKER_PULL_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "docker_pull_timeout", "image": image}
    if proc.returncode == 0:
        return {"ok": True, "image": image, "source": "pull"}
    err = (proc.stderr or proc.stdout or "").strip()
    return {"ok": False, "error": err or "docker pull failed", "image": image}


def build_postgres_image(image: str) -> dict[str, Any]:
    root = repo_root()
    dockerfile = root / "ops" / "docker" / "Dockerfile.pgvector"
    context = root / "ops" / "docker"
    if not dockerfile.is_file():
        return {"ok": False, "error": "dockerfile_missing", "path": str(dockerfile)}
    try:
        proc = _docker(
            ["build", "-t", image, "-f", str(dockerfile), str(context)],
            timeout=DOCKER_BUILD_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "docker_build_timeout", "image": image}
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return {"ok": False, "error": err or "docker build failed", "image": image}
    return {"ok": True, "image": image, "source": "build"}


def ensure_postgres_image(image: str) -> dict[str, Any]:
    pulled = pull_postgres_image(image)
    if pulled.get("ok"):
        return pulled
    built = build_postgres_image(image)
    if built.get("ok"):
        return built
    return {
        "ok": False,
        "error": "image_unavailable",
        "pull": pulled,
        "build": built,
    }


def _ensure_volume(name: str) -> None:
    proc = _docker(["volume", "inspect", name], check=False)
    if proc.returncode == 0:
        return
    _docker(["volume", "create", name], check=True)


def _remove_container(name: str) -> None:
    _docker(["rm", "-f", name], check=False)


def _wait_pg_ready(dsn: str, *, timeout_s: float = READY_TIMEOUT_S) -> bool:
    import psycopg

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(dsn, connect_timeout=3) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
        except Exception:  # noqa: BLE001
            time.sleep(1)
            continue
        proc = _docker(
            [
                "exec",
                PG_CONTAINER,
                "pg_isready",
                "-U",
                "khipu",
                "-d",
                "khipu",
            ],
            check=False,
        )
        if proc.returncode == 0:
            return True
        time.sleep(1)
    return False


def _generate_password() -> str:
    return secrets.token_urlsafe(24)


def _local_dsn(port: int, password: str) -> str:
    user = quote("khipu", safe="")
    pw = quote(password, safe="")
    return f"postgresql://{user}:{pw}@127.0.0.1:{port}/khipu?sslmode=disable"


def _verify_pending_row(pending: dict[str, Any]) -> dict[str, Any]:
    rows, meta = effective_matrix(refresh=False)
    row = find_full_row(rows, pending, mode="local_docker")
    if row is None:
        return {
            "ok": False,
            "error": "pending_row_not_in_matrix",
            "matrix": meta,
        }
    return {"ok": True, "row": row}


def _probe_local_cluster(dsn: str, pgvector_min: str) -> dict[str, Any]:
    import psycopg

    from packaging.version import InvalidVersion, Version

    with psycopg.connect(dsn, connect_timeout=10) as conn:
        ver_num = server_version_num(conn)
        gate = require_pg19_num(ver_num)
        if not gate.get("ok"):
            return {**gate, "server_version": server_version_string(conn)}
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.commit()
        ext = pgvector_extversion(conn)
        if not ext:
            return {"ok": False, "error": "vector_extension_missing"}
        try:
            required = Version(str(pgvector_min))
            probed = Version(str(ext))
        except InvalidVersion:
            return {"ok": False, "error": "invalid_pgvector_version", "pgvector": ext}
        if probed < required:
            return {
                "ok": False,
                "error": "pgvector_below_minimum",
                "pgvector": ext,
                "pgvector_min": str(pgvector_min),
            }
        return {
            "ok": True,
            "server_version": server_version_string(conn),
            "server_version_num": ver_num,
            "pgvector": ext,
        }


def _run_migrations() -> dict[str, Any]:
    from khipu.migrate import run as migrate_run

    result = migrate_run(dry_run=False)
    pending = result.get("pending") or []
    if pending:
        return {"ok": False, "error": "migrations_pending", "pending": pending}
    return {"ok": True, "ran": result.get("ran") or []}


def install_local_postgres() -> dict[str, Any]:
    docker = docker_available()
    if not docker.get("ok"):
        return docker

    versions = read_versions()
    pending = versions.get("pending")
    if not isinstance(pending, dict):
        return {
            "ok": False,
            "error": "pending_missing",
            "fix": "select_compat_row first",
        }

    verified = _verify_pending_row(pending)
    if not verified.get("ok"):
        return verified

    image = str(pending.get("postgres_image") or "")
    if not image:
        return {"ok": False, "error": "pending_postgres_image_missing"}

    disk = disk_headroom_warning()
    image_result = ensure_postgres_image(image)
    if not image_result.get("ok"):
        out = dict(image_result)
        if disk:
            out["disk"] = disk
        return out

    pulled_image = str(image_result.get("image") or image)
    password = _generate_password()
    port = choose_host_port()
    if port is None:
        return {"ok": False, "error": "no_free_port", "range": list(PORT_RANGE)}

    _ensure_volume(PG_VOLUME)
    _remove_container(PG_CONTAINER)
    run_proc = _docker(
        [
            "run",
            "-d",
            "--name",
            PG_CONTAINER,
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
            f"{PG_VOLUME}:/var/lib/postgresql",
            image,
        ],
        check=False,
    )
    if run_proc.returncode != 0:
        err = (run_proc.stderr or run_proc.stdout or "").strip()
        return {"ok": False, "error": err or "docker run failed"}

    dsn = _local_dsn(port, password)
    if not _wait_pg_ready(dsn):
        return {"ok": False, "error": "postgres_not_ready", "port": port}

    probe = _probe_local_cluster(dsn, str(pending.get("pgvector_min") or "0"))
    if not probe.get("ok"):
        return probe

    try:
        set_dsn(dsn)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"keychain_write_failed: {exc}"}

    versions = read_versions()
    versions.setdefault("khipu_app", khipu_app_version())
    versions.setdefault("cli", khipu_app_version())
    versions["postgres"] = {
        "mode": "local_docker",
        "image": pulled_image,
        "server_version": probe["server_version"],
        "pgvector": probe["pgvector"],
        "volume": PG_VOLUME,
        "container": PG_CONTAINER,
        "port": port,
    }
    write_versions(versions)

    migrated = _run_migrations()
    if not migrated.get("ok"):
        return migrated

    out: dict[str, Any] = {
        "ok": True,
        "dsn_source": "keychain",
        "image": pulled_image,
        "port": port,
        "server_version": probe["server_version"],
        "pgvector": probe["pgvector"],
    }
    if disk:
        out["disk"] = disk
    return out


def components_status() -> dict[str, Any]:
    from packaging.version import Version

    from khipu.components_matrix import (
        effective_matrix,
        khipu_app_version,
        match_row_for_install,
        refresh_matrix_cache,
    )

    try:
        refresh_matrix_cache()
    except (OSError, urllib.error.URLError, ValueError):
        pass

    versions = read_versions()
    docker = docker_available()
    pending = versions.get("pending")
    postgres = (
        versions.get("postgres") if isinstance(versions.get("postgres"), dict) else {}
    )
    graphify = (
        versions.get("graphify") if isinstance(versions.get("graphify"), dict) else {}
    )

    postgres_probe: dict[str, Any] | None = None
    if not postgres:
        probe = check_remote_postgres(full=True)
        if probe.get("ok"):
            postgres = {
                "mode": "remote",
                "source": "dsn",
                "server_version": probe.get("server_version"),
                "pgvector": probe.get("pgvector"),
            }
        else:
            postgres_probe = {"ok": False, "error": probe.get("error")}

    if not graphify:
        from khipu.jobs import graphify_nightly_path

        script = graphify_nightly_path()
        if script is not None:
            graphify = {
                "semver": "external",
                "path": str(script.parent),
                "source": "env",
            }

    rows, matrix_meta = effective_matrix(refresh=False)
    app = khipu_app_version()
    mode = str(postgres.get("mode") or "local_docker")

    postgres_upgrade: dict[str, Any] | None = None
    if mode == "local_docker":
        current_image = str(postgres.get("image") or "")
        current_graphify = str(graphify.get("semver") or "")
        candidates: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            target_image = str(row.get("postgres_image") or "")
            if not target_image or target_image == current_image:
                continue
            matched = match_row_for_install(
                graphify_semver=str(row.get("graphify_semver") or current_graphify),
                graphify_tarball_url=str(row.get("graphify_tarball_url") or ""),
                postgres_image=target_image,
                pgvector_min=str(row.get("pgvector_min") or ""),
                mode="local_docker",
                app_semver=app,
            )
            if matched is not None:
                candidates.append(row)
        if candidates:
            chosen = max(
                candidates,
                key=lambda r: Version(
                    str(r.get("postgres_image") or "0")
                    .rsplit(":", 1)[-1]
                    .replace("-pgvector", "")
                    or "0"
                ),
            )
            target = str(chosen.get("postgres_image") or "")
            postgres_upgrade = {
                "available": True,
                "current": current_image,
                "target": target,
                "current_tag": current_image.rsplit(":", 1)[-1],
                "target_tag": target.rsplit(":", 1)[-1],
            }
        elif current_image:
            postgres_upgrade = {
                "available": False,
                "current": current_image,
                "current_tag": current_image.rsplit(":", 1)[-1],
            }

    graphify_upgrade: dict[str, Any] | None = None
    current_gy = str(graphify.get("semver") or "")
    if current_gy and graphify.get("source") != "env":
        gy_candidates: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            target = str(row.get("graphify_semver") or "")
            if not target or target == current_gy:
                continue
            matched = match_row_for_install(
                graphify_semver=target,
                graphify_tarball_url=str(row.get("graphify_tarball_url") or ""),
                postgres_image=str(
                    postgres.get("image") or row.get("postgres_image") or ""
                ),
                pgvector_min=str(row.get("pgvector_min") or ""),
                mode="remote" if mode == "remote" else "local_docker",
                app_semver=app,
            )
            if matched is not None:
                gy_candidates.append(row)
        if gy_candidates:
            chosen_gy = max(
                gy_candidates,
                key=lambda r: Version(str(r.get("graphify_semver") or "0")),
            )
            graphify_upgrade = {
                "available": True,
                "current": current_gy,
                "target": str(chosen_gy.get("graphify_semver") or ""),
            }
        else:
            graphify_upgrade = {"available": False, "current": current_gy}

    return {
        "ok": True,
        "khipu_app": versions.get("khipu_app") or khipu_app_version(),
        "cli": versions.get("cli") or khipu_app_version(),
        "docker": docker,
        "pending": pending,
        "postgres": postgres,
        "postgres_probe": postgres_probe,
        "graphify": graphify,
        "postgres_upgrade": postgres_upgrade,
        "graphify_upgrade": graphify_upgrade,
        "matrix": matrix_meta,
        "versions_path": str(versions_path()),
    }


def _table_row_counts(conn) -> dict[str, int]:
    tables = (
        "episodes",
        "topics",
        "nodes",
        "edges",
        "memory_embeddings",
        "ops_events",
        "schema_migrations",
    )
    counts: dict[str, int] = {}
    with conn.cursor() as cur:
        for table in tables:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            counts[table] = int(cur.fetchone()[0])
    return counts


def _stop_container(name: str) -> None:
    _docker(["stop", name], check=False)
    _remove_container(name)


def _docker_volume_rename(old: str, new: str) -> dict[str, Any]:
    """Rename by copy — Docker lacks native volume rename."""
    inspect = _docker(["volume", "inspect", old], check=False)
    if inspect.returncode != 0:
        return {"ok": False, "error": "volume_missing", "volume": old}
    _docker(["volume", "rm", "-f", new], check=False)
    cp = _docker(
        [
            "run",
            "--rm",
            "-v",
            f"{old}:/from:ro",
            "-v",
            f"{new}:/to",
            "alpine:3",
            "sh",
            "-c",
            "cp -a /from/. /to/",
        ],
        check=False,
        timeout=900,
    )
    if cp.returncode != 0:
        err = (cp.stderr or cp.stdout or "").strip()
        return {"ok": False, "error": err or "volume_copy_failed"}
    _docker(["volume", "rm", "-f", old], check=False)
    return {"ok": True}


def _read_dsn_password_port(versions: dict[str, Any]) -> dict[str, Any]:
    from khipu.db import resolve_dsn

    dsn = resolve_dsn()
    postgres = (
        versions.get("postgres") if isinstance(versions.get("postgres"), dict) else {}
    )
    port = int(postgres.get("port") or DEFAULT_PORT)
    password = ""
    if "@" in dsn and ":" in dsn.split("@", 1)[0]:
        userinfo = dsn.split("@", 1)[0].split("://", 1)[-1]
        if ":" in userinfo:
            password = unquote(userinfo.split(":", 1)[1])
    if not password:
        # Upgrade must reuse the live DSN secret — never mint a replacement.
        return {"ok": False, "error": "dsn_password_missing"}
    return {"ok": True, "password": password, "port": port}


def upgrade_postgres() -> dict[str, Any]:
    from datetime import datetime, timezone

    from khipu.components_backup import _dump_live_db, _restore_dump_file
    from khipu.components_matrix import (
        effective_matrix,
        find_full_row,
        khipu_app_version,
        refresh_matrix_cache,
    )
    from khipu.keychain import set_dsn

    docker = docker_available()
    if not docker.get("ok"):
        return docker

    try:
        refresh_matrix_cache()
    except (OSError, urllib.error.URLError, ValueError):
        pass

    versions = read_versions()
    postgres = (
        versions.get("postgres") if isinstance(versions.get("postgres"), dict) else {}
    )
    if str(postgres.get("mode") or "") != "local_docker":
        return {"ok": False, "error": "upgrade_requires_local_docker"}

    current_image = str(postgres.get("image") or "")
    graphify = (
        versions.get("graphify") if isinstance(versions.get("graphify"), dict) else {}
    )
    current_graphify = str(graphify.get("semver") or "")
    if not current_image:
        return {"ok": False, "error": "postgres_image_not_installed"}

    rows, _meta = effective_matrix(refresh=False)
    app = khipu_app_version()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        target_image = str(row.get("postgres_image") or "")
        if not target_image or target_image == current_image:
            continue
        pending_probe = {
            "graphify_semver": str(row.get("graphify_semver") or current_graphify),
            "graphify_tarball_url": str(row.get("graphify_tarball_url") or ""),
            "pgvector_min": str(row.get("pgvector_min") or ""),
            "postgres_image": target_image,
        }
        if (
            find_full_row(rows, pending_probe, mode="local_docker", app_semver=app)
            is None
        ):
            continue
        candidates.append(row)
    if not candidates:
        return {"ok": False, "error": "no_upgrade_row", "current": current_image}

    from packaging.version import Version

    chosen = max(
        candidates,
        key=lambda r: Version(
            str(r.get("postgres_image") or "0")
            .rsplit(":", 1)[-1]
            .replace("-pgvector", "")
            or "0"
        ),
    )
    target_image = str(chosen.get("postgres_image") or "")
    pgvector_min = str(chosen.get("pgvector_min") or "0")

    pre_upgrade_image = current_image
    creds = _read_dsn_password_port(versions)
    if not creds.get("ok"):
        return creds
    password = str(creds["password"])
    port = int(creds["port"])

    import psycopg

    with psycopg.connect(_local_dsn(port, password), connect_timeout=10) as conn:
        pre_counts = _table_row_counts(conn)

    from khipu.components_backup import _backup_dir

    dump_result = _dump_live_db(_backup_dir())
    if not dump_result.get("ok"):
        return dump_result
    dump_path = Path(str(dump_result["path"]))

    _stop_container(PG_CONTAINER)
    prev_volume = f"{PG_VOLUME}-prev"
    _docker(["volume", "rm", "-f", prev_volume], check=False)
    renamed = _docker_volume_rename(PG_VOLUME, prev_volume)
    if not renamed.get("ok"):
        return renamed

    _ensure_volume(PG_VOLUME)
    pulled = ensure_postgres_image(target_image)
    if not pulled.get("ok"):
        return {"ok": False, "error": "image_pull_failed", "detail": pulled}

    run_proc = _docker(
        [
            "run",
            "-d",
            "--name",
            PG_CONTAINER,
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
            f"{PG_VOLUME}:/var/lib/postgresql",
            target_image,
        ],
        check=False,
    )
    if run_proc.returncode != 0:
        err = (run_proc.stderr or run_proc.stdout or "").strip()
        return {"ok": False, "error": err or "docker run failed", "rollback": "manual"}

    dsn = _local_dsn(port, password)
    if not _wait_pg_ready(dsn):
        return {"ok": False, "error": "postgres_not_ready", "rollback": "manual"}

    try:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.commit()
        restored = _restore_dump_file(
            dump_path,
            container=PG_CONTAINER,
            dsn=dsn,
            password=password,
        )
        if not restored.get("ok"):
            raise RuntimeError(str(restored.get("error") or "pg_restore failed"))
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            probe = _probe_local_cluster(dsn, pgvector_min)
            if not probe.get("ok"):
                raise RuntimeError(str(probe.get("error") or "probe_failed"))
            post_counts = _table_row_counts(conn)
        for table, before in pre_counts.items():
            after = post_counts.get(table, 0)
            if after < before:
                raise RuntimeError(
                    f"count_regression:{table}: before={before} after={after}"
                )
    except Exception as exc:  # noqa: BLE001
        _stop_container(PG_CONTAINER)
        failed_vol = f"{PG_VOLUME}-failed-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        _docker(["volume", "rm", "-f", failed_vol], check=False)
        _docker_volume_rename(PG_VOLUME, failed_vol)
        _docker(["volume", "rm", "-f", PG_VOLUME], check=False)
        _docker_volume_rename(prev_volume, PG_VOLUME)
        ensure_postgres_image(pre_upgrade_image)
        _ensure_volume(PG_VOLUME)
        _docker(
            [
                "run",
                "-d",
                "--name",
                PG_CONTAINER,
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
                f"{PG_VOLUME}:/var/lib/postgresql",
                pre_upgrade_image,
            ],
            check=False,
        )
        return {"ok": False, "error": str(exc), "rolled_back": True}

    try:
        set_dsn(dsn)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"keychain_write_failed: {exc}"}

    versions = read_versions()
    pg = versions.setdefault("postgres", {})
    if isinstance(pg, dict):
        pg.update(
            {
                "mode": "local_docker",
                "image": target_image,
                "server_version": probe["server_version"],
                "pgvector": probe["pgvector"],
                "volume": PG_VOLUME,
                "container": PG_CONTAINER,
                "port": port,
            }
        )
    write_versions(versions)
    _docker(["volume", "rm", "-f", prev_volume], check=False)
    return {
        "ok": True,
        "image": target_image,
        "previous_image": pre_upgrade_image,
        "server_version": probe["server_version"],
        "pgvector": probe["pgvector"],
    }


def install_graphify_stub() -> dict[str, Any]:
    from khipu.components_graphify import install_graphify

    return install_graphify(first_run=True)


def upgrade_graphify_stub() -> dict[str, Any]:
    from khipu.components_graphify import upgrade_graphify

    return upgrade_graphify()
