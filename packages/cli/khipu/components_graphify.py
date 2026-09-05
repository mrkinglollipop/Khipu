"""Graphify component install/upgrade under Application Support."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from khipu import sources
from khipu.components_matrix import (
    application_support_dir,
    effective_matrix,
    khipu_app_version,
    match_row_for_install,
    read_versions,
    refresh_matrix_cache,
    write_versions,
)


def load_versions() -> dict[str, Any]:
    return read_versions()


def save_versions(data: dict[str, Any]) -> None:
    write_versions(data)


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "khipu-components/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def _tar_member_escapes(member: tarfile.TarInfo, dest: Path) -> bool:
    names = [member.name]
    if member.issym() or member.islnk():
        names.append(member.linkname or "")
    dest_resolved = dest.resolve()
    for name in names:
        if not name:
            continue
        normalized = name.replace("\\", "/")
        if normalized.startswith("/") or Path(name).is_absolute():
            return True
        if ".." in Path(normalized).parts:
            return True
        try:
            (dest / normalized).resolve().relative_to(dest_resolved)
        except ValueError:
            return True
    return False


def _extract_tarball(archive: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if _tar_member_escapes(member, dest):
                raise tarfile.TarError(
                    f"refusing tar member that escapes dest: {member.name}"
                )
        extract_kwargs: dict[str, Any] = {}
        if hasattr(tarfile, "data_filter"):
            extract_kwargs["filter"] = "data"
        tar.extractall(dest, **extract_kwargs)
    # Normalize single top-level directory tarballs.
    children = [p for p in dest.iterdir() if p.name not in {".DS_Store"}]
    if len(children) == 1 and children[0].is_dir():
        nested = children[0]
        for item in nested.iterdir():
            shutil.move(str(item), str(dest / item.name))
        nested.rmdir()


def _ensure_empty_sources() -> None:
    path = sources.sources_file()
    if path.is_file():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(sources.default_document(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _promote_pending(versions: dict[str, Any]) -> None:
    pending = versions.pop("pending", None)
    if not isinstance(pending, dict):
        return
    postgres = versions.setdefault("postgres", {})
    if isinstance(postgres, dict):
        image = str(pending.get("postgres_image") or "").strip()
        if image:
            postgres["image"] = image


def _ensure_pending(versions: dict[str, Any]) -> dict[str, Any]:
    """Join/remote Welcome never called select-compat-row; pick a matrix row."""
    pending = versions.get("pending")
    if isinstance(pending, dict) and str(pending.get("graphify_semver") or "").strip():
        return versions
    postgres = versions.get("postgres") if isinstance(versions.get("postgres"), dict) else {}
    mode = str(postgres.get("mode") or "").strip()
    if mode != "local_docker":
        mode = "remote"
    from khipu.components_matrix import select_compat_row

    pgvector = str(postgres.get("pgvector") or "").strip() or None
    server_version = str(postgres.get("server_version") or "").strip() or None
    selected = select_compat_row(
        mode,
        pgvector_extversion=pgvector,
        server_version=server_version,
        refresh=True,
    )
    if not selected.get("ok"):
        return versions
    return load_versions()


def install_graphify(*, first_run: bool = True) -> dict[str, Any]:
    versions = _ensure_pending(load_versions())
    pending = versions.get("pending")
    if not isinstance(pending, dict):
        return {
            "ok": False,
            "error": "missing_pending_graphify",
            "fix": "complete the Database step first",
        }
    semver = str(pending.get("graphify_semver") or "").strip()
    url = str(pending.get("graphify_tarball_url") or "").strip()
    if not semver or not url:
        return {
            "ok": False,
            "error": "missing_pending_graphify_fields",
            "title": "The graph builder's install details are missing",
            "detail": "Khipu did not record which graph-builder version to install.",
            "fix": "Go back to the Database step and continue again, or skip this step and install the graph builder later from Settings → Components.",
        }

    postgres = versions.get("postgres") if isinstance(versions.get("postgres"), dict) else {}
    mode = str(postgres.get("mode") or "local_docker")
    match_mode = "remote" if mode == "remote" else "local_docker"
    match_kwargs = dict(
        graphify_semver=semver,
        graphify_tarball_url=url,
        postgres_image=str(pending.get("postgres_image") or postgres.get("image") or ""),
        pgvector_min=str(pending.get("pgvector_min") or ""),
        mode=match_mode,
    )
    row = match_row_for_install(**match_kwargs)
    if row is None:
        # The compat matrix may simply be stale (a dev build's version can be
        # newer than any khipu_app_min row) — refresh once and retry before
        # refusing for good.
        try:
            refresh_matrix_cache()
        except (OSError, urllib.error.URLError, ValueError):
            pass
        row = match_row_for_install(**match_kwargs)
    if row is None:
        app = khipu_app_version()
        return {
            "ok": False,
            "error": "matrix_row_refused",
            "title": "No graph builder is listed for this Khipu yet",
            "detail": f"Khipu {app} is not in the compatibility list (graph builder {semver}).",
            "fix": "Update Khipu, or skip this step and install the graph builder later from Settings → Components.",
            "khipu_app": app,
            "graphify_semver": semver,
            "graphify_tarball_url": url,
        }

    dest = application_support_dir() / "graphify" / semver
    with tempfile.TemporaryDirectory(prefix="khipu-graphify-") as tmpdir:
        archive = Path(tmpdir) / f"khipu-graphify-{semver}.tar.gz"
        try:
            _download(url, archive)
        except (OSError, urllib.error.URLError) as exc:
            return {
                "ok": False,
                "error": "download_failed",
                "title": "Could not download the graph builder",
                "detail": f"{type(exc).__name__}: {exc}",
                "fix": "Check the connection and retry.",
                "url": url,
            }
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        try:
            _extract_tarball(archive, dest)
        except (tarfile.TarError, OSError) as exc:
            return {
                "ok": False,
                "error": "extract_failed",
                "title": "Could not unpack the graph builder",
                "detail": f"{type(exc).__name__}: {exc}",
                "fix": "Retry the install; if it keeps failing, skip this step and install it later from Settings → Components.",
            }

    script = dest / "graphify_nightly.py"
    if not script.is_file():
        return {
            "ok": False,
            "error": "missing_graphify_nightly",
            "title": "The downloaded graph builder is incomplete",
            "detail": f"No graphify_nightly.py was found under {dest}.",
            "fix": "Retry the install; if it keeps failing, skip this step and install it later from Settings → Components.",
            "path": str(dest),
        }

    _ensure_empty_sources()
    versions["graphify"] = {"semver": semver, "path": str(dest)}
    _promote_pending(versions)
    save_versions(versions)
    os.environ["KHIPU_GRAPHIFY_NIGHTLY"] = str(script)
    return {
        "ok": True,
        "semver": semver,
        "path": str(dest),
        "sha256": digest,
        "first_run": first_run,
    }


def upgrade_graphify() -> dict[str, Any]:
    try:
        refresh_matrix_cache()
    except (OSError, urllib.error.URLError, ValueError):
        pass
    versions = load_versions()
    installed = versions.get("graphify")
    if not isinstance(installed, dict):
        return {"ok": False, "error": "graphify_not_installed"}
    current = str(installed.get("semver") or "").strip()
    postgres = versions.get("postgres") if isinstance(versions.get("postgres"), dict) else {}
    mode = str(postgres.get("mode") or "local_docker")
    rows, _meta = effective_matrix(refresh=False)
    app = khipu_app_version()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        target = str(row.get("graphify_semver") or "")
        if not target or target == current:
            continue
        matched = match_row_for_install(
            graphify_semver=target,
            graphify_tarball_url=str(row.get("graphify_tarball_url") or ""),
            postgres_image=str(postgres.get("image") or row.get("postgres_image") or ""),
            pgvector_min=str(row.get("pgvector_min") or ""),
            mode="remote" if mode == "remote" else "local_docker",
            app_semver=app,
        )
        if matched is not None:
            candidates.append(row)
    if not candidates:
        return {"ok": False, "error": "no_upgrade_row", "current": current}
    chosen = max(
        candidates, key=lambda r: __import__("packaging.version").version.Version(
            str(r.get("graphify_semver") or "0")
        )
    )
    semver = str(chosen.get("graphify_semver") or "")
    url = str(chosen.get("graphify_tarball_url") or "")
    dest = application_support_dir() / "graphify" / semver
    previous = str(installed.get("path") or "")
    with tempfile.TemporaryDirectory(prefix="khipu-graphify-upgrade-") as tmpdir:
        archive = Path(tmpdir) / f"khipu-graphify-{semver}.tar.gz"
        try:
            _download(url, archive)
        except (OSError, urllib.error.URLError) as exc:
            return {"ok": False, "error": "download_failed", "detail": str(exc), "url": url}
        try:
            _extract_tarball(archive, dest)
        except (tarfile.TarError, OSError) as exc:
            return {"ok": False, "error": "extract_failed", "detail": str(exc)}
    script = dest / "graphify_nightly.py"
    if not script.is_file():
        return {"ok": False, "error": "missing_graphify_nightly", "path": str(dest)}
    versions["graphify"] = {"semver": semver, "path": str(dest)}
    save_versions(versions)
    os.environ["KHIPU_GRAPHIFY_NIGHTLY"] = str(script)
    return {
        "ok": True,
        "semver": semver,
        "path": str(dest),
        "previous": previous,
        "kept_previous": bool(previous and Path(previous).exists()),
    }


def components_status() -> dict[str, Any]:
    versions = load_versions()
    graphify = versions.get("graphify") if isinstance(versions.get("graphify"), dict) else {}
    postgres = versions.get("postgres") if isinstance(versions.get("postgres"), dict) else {}
    pending = versions.get("pending") if isinstance(versions.get("pending"), dict) else None
    return {
        "ok": True,
        "khipu_app": versions.get("khipu_app"),
        "cli": versions.get("cli"),
        "graphify": graphify or None,
        "postgres": postgres or None,
        "pending": pending,
    }
