"""Compatibility matrix loader for portable Khipu component installs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

COMPAT_REPO_JSON = (
    "https://github.com/mrkinglollipop/khipu-compat/releases/latest/download/"
    "khipu-graphify-postgres.json"
)
COMPAT_REPO_SHA = (
    "https://github.com/mrkinglollipop/khipu-compat/releases/latest/download/"
    "khipu-graphify-postgres.json.sha256"
)

FORBIDDEN_POSTGRES_IMAGES = frozenset(
    {
        "postgres:latest",
        "docker.io/library/postgres:latest",
    }
)


def application_support_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "Khipu"


def matrix_cache_path() -> Path:
    return application_support_dir() / "matrix.json"


def versions_path() -> Path:
    return application_support_dir() / "versions.json"


def read_versions() -> dict[str, Any]:
    path = versions_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def write_versions(data: dict[str, Any]) -> None:
    path = versions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _repo_root() -> Path:
    from khipu.paths import repo_root

    return repo_root()


def bundled_matrix_path() -> Path:
    root = _repo_root()
    for candidate in (
        root / "info.json",
        root / "docs" / "compat" / "khipu-graphify-postgres.json",
        root / "apps" / "desktop" / "khipu-resources" / "info.json",
    ):
        if candidate.is_file():
            return candidate
    return root / "docs" / "compat" / "khipu-graphify-postgres.json"


def _load_json(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("matrix"), list):
        raise ValueError(f"invalid matrix file: {path}")
    return raw


def _fetch_url(url: str, *, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "khipu-components/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def refresh_matrix_cache(*, force: bool = False) -> dict[str, Any]:
    bundled = _load_json(bundled_matrix_path())
    try:
        body = _fetch_url(COMPAT_REPO_JSON)
        sha_body = _fetch_url(COMPAT_REPO_SHA).decode("utf-8").strip()
        expected = sha_body.split()[0] if sha_body else ""
        digest = hashlib.sha256(body).hexdigest()
        if expected and digest != expected:
            raise ValueError("matrix checksum mismatch")
        fetched = json.loads(body.decode("utf-8"))
        if not isinstance(fetched, dict) or not isinstance(fetched.get("matrix"), list):
            raise ValueError("fetched matrix invalid")
        cache = matrix_cache_path()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(body)
        return fetched
    except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError):
        if force:
            raise
        cache = matrix_cache_path()
        if cache.is_file():
            try:
                return _load_json(cache)
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        return bundled


def effective_matrix(
    *, refresh: bool = False
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    bundled_path = bundled_matrix_path()
    bundled = _load_json(bundled_path)
    cached: dict[str, Any] | None = None
    cache_path = matrix_cache_path()
    if refresh:
        try:
            cached = refresh_matrix_cache()
        except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError):
            cached = None
    elif cache_path.is_file():
        try:
            cached = _load_json(cache_path)
        except (OSError, ValueError, json.JSONDecodeError):
            cached = None
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in (bundled, cached or {}):
        for row in source.get("matrix", []):
            if not isinstance(row, dict):
                continue
            key = json.dumps(row, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    meta = {
        "bundled_path": str(bundled_path),
        "cache_path": str(cache_path) if cache_path.is_file() else None,
        "row_count": len(rows),
    }
    return rows, meta


def khipu_app_version() -> str:
    for key in ("KHIPU_APP_VERSION", "KHIPU_VERSION"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            return raw
    versions = read_versions()
    app = str(versions.get("khipu_app") or "").strip()
    if app:
        return app
    return "0.3.4"


def is_forbidden_postgres_image(image: str) -> bool:
    normalized = (image or "").strip().lower()
    if not normalized:
        return True
    if normalized in FORBIDDEN_POSTGRES_IMAGES:
        return True
    if normalized.startswith("alzy/postgres") or "/alzy/postgres" in normalized:
        return True
    if normalized.endswith(":latest") and "postgres" in normalized:
        return True
    return False


def _postgres_major(image: str) -> int | None:
    tag = (image or "").rsplit(":", 1)[-1]
    if tag.endswith("-pgvector"):
        tag = tag[: -len("-pgvector")]
    match = re.match(r"^(\d+)", tag)
    return int(match.group(1)) if match else None


def _local_row_eligible(row: dict[str, Any]) -> bool:
    image = str(row.get("postgres_image") or "").strip()
    if not image or is_forbidden_postgres_image(image):
        return False
    major = _postgres_major(image)
    return major == 19


def _semver_at_least(current: str, minimum: str) -> bool:
    try:
        return Version(current) >= Version(minimum)
    except (InvalidVersion, TypeError, ValueError):
        return False


def _row_matches_app(row: dict[str, Any], app_semver: str) -> bool:
    minimum = str(row.get("khipu_app_min") or "").strip()
    return bool(minimum) and _semver_at_least(app_semver, minimum)


def _postgres_tag_semver(image: str) -> str:
    tag = (image or "").rsplit(":", 1)[-1]
    if tag.endswith("-pgvector"):
        return tag[: -len("-pgvector")]
    return tag


def find_full_row(
    rows: list[dict[str, Any]],
    pending: dict[str, Any],
    *,
    mode: str = "local_docker",
    app_semver: str | None = None,
) -> dict[str, Any] | None:
    app = app_semver or khipu_app_version()
    for row in rows:
        if not _row_matches_app(row, app):
            continue
        if str(row.get("graphify_semver") or "") != str(
            pending.get("graphify_semver") or ""
        ):
            continue
        if str(row.get("graphify_tarball_url") or "") != str(
            pending.get("graphify_tarball_url") or ""
        ):
            continue
        if str(row.get("pgvector_min") or "") != str(pending.get("pgvector_min") or ""):
            continue
        if mode == "local_docker":
            if str(row.get("postgres_image") or "") != str(
                pending.get("postgres_image") or ""
            ):
                continue
        return row
    return None


def match_row_for_install(
    *,
    graphify_semver: str,
    graphify_tarball_url: str,
    postgres_image: str | None = None,
    pgvector_min: str | None = None,
    mode: str = "local_docker",
    app_semver: str | None = None,
) -> dict[str, Any] | None:
    rows, _meta = effective_matrix(refresh=False)
    pending = {
        "graphify_semver": graphify_semver,
        "graphify_tarball_url": graphify_tarball_url,
        "pgvector_min": pgvector_min or "",
        "postgres_image": postgres_image or "",
    }
    if app_semver:
        return find_full_row(rows, pending, mode=mode, app_semver=app_semver)
    return find_full_row(rows, pending, mode=mode)


def select_compat_row(
    mode: str,
    *,
    pgvector_extversion: str | None = None,
    server_version: str | None = None,
    pgvector: str | None = None,
    refresh: bool = True,
) -> dict[str, Any]:
    if refresh:
        try:
            refresh_matrix_cache()
        except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError):
            pass
    rows, meta = effective_matrix(refresh=False)
    app = khipu_app_version()
    candidates: list[dict[str, Any]] = []
    extversion = pgvector_extversion or pgvector
    for row in rows:
        if not _row_matches_app(row, app):
            continue
        if mode == "local_docker":
            if not _local_row_eligible(row):
                continue
        if mode == "remote" and extversion:
            minimum = str(row.get("pgvector_min") or "0")
            if not _semver_at_least(str(extversion), minimum):
                continue
        candidates.append(row)
    if not candidates:
        return {
            "ok": False,
            "error": "matrix_no_matching_row",
            "mode": mode,
            "app_semver": app,
            "matrix": meta,
        }
    if mode == "remote":
        chosen = max(
            candidates, key=lambda r: Version(str(r.get("graphify_semver") or "0"))
        )
    else:
        chosen = max(
            candidates,
            key=lambda r: (
                Version(_postgres_tag_semver(str(r.get("postgres_image") or "0"))),
                Version(str(r.get("graphify_semver") or "0")),
            ),
        )
    pending = {
        "graphify_semver": str(chosen.get("graphify_semver") or ""),
        "graphify_tarball_url": str(chosen.get("graphify_tarball_url") or ""),
        "pgvector_min": str(chosen.get("pgvector_min") or ""),
    }
    if mode != "remote":
        pending["postgres_image"] = str(chosen.get("postgres_image") or "")
    versions = read_versions()
    versions["pending"] = pending
    if mode == "local_docker":
        versions.setdefault("postgres", {})
        if isinstance(versions["postgres"], dict):
            versions["postgres"].update(
                {
                    "mode": "local_docker",
                    "volume": "khipu-pgdata",
                    "container": "khipu-pg19",
                }
            )
    if mode == "remote":
        if server_version:
            versions.setdefault("postgres", {})
            if isinstance(versions["postgres"], dict):
                versions["postgres"]["mode"] = "remote"
                versions["postgres"]["server_version"] = server_version
                if extversion:
                    versions["postgres"]["pgvector"] = extversion
    write_versions(versions)
    return {"ok": True, "pending": pending, "row": chosen, "matrix": meta}
