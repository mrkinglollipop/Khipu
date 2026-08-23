"""Render Khipu LaunchAgent plists from templates — per-user Application Support paths."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from khipu.components_matrix import application_support_dir, read_versions, write_versions
from khipu.jobs import (
    PLIST_GRAPH,
    PLIST_MONTHLY,
    PLIST_NIGHTLY,
    _JOB_SPECS,
    _launchagents_dir,
    _log_paths,
    _plist_path,
)

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "launchd"

_JOB_TEMPLATE: dict[str, str] = {
    "nightly": "com.matt.khipu-nightly.plist",
    "monthly": "com.matt.khipu-monthly.plist",
    "graph_build": "com.matt.khipu-graph.plist",
}

_LABELS = {
    "nightly": PLIST_NIGHTLY,
    "monthly": PLIST_MONTHLY,
    "graph_build": PLIST_GRAPH,
}


def _repo_root() -> Path:
    from khipu.paths import repo_root

    return repo_root()


def _bundled_python() -> Path:
    root = _repo_root()
    for rel in (
        "python/bin/python3.11",
        "python/bin/python3",
    ):
        candidate = root / rel
        if candidate.is_file():
            return candidate
    return Path(shutil.which("python3.11") or shutil.which("python3") or "python3")


def render_context() -> dict[str, str]:
    root = _repo_root()
    cli = root / "packages" / "cli"
    lib = root / "lib"
    pythonpath_parts = [str(cli)]
    if lib.is_dir():
        pythonpath_parts.append(str(lib))
    legacy = root.parent / ".python_libs"
    if legacy.is_dir():
        pythonpath_parts.append(str(legacy))
    support = application_support_dir()
    support.mkdir(parents=True, exist_ok=True)
    return {
        "KHIPU_ROOT": str(root),
        "KHIPU_PYTHON": str(_bundled_python()),
        "PYTHONPATH": ":".join(pythonpath_parts),
        "WORKING_DIRECTORY": str(support),
    }


# Maintainer-style installs point jobs at scripts outside the bundle via env.
# Whatever is set when the plist is rendered is baked in, otherwise the job
# resolves only through versions.json and fails closed.
PASSTHROUGH_ENV = (
    "KHIPU_CONSOLIDATE_NIGHTLY",
    "KHIPU_CONSOLIDATE_MONTHLY",
    "KHIPU_GRAPHIFY_NIGHTLY",
    "KHIPU_BUILD_INDEX",
    "KHIPU_GRAPH_SNAPSHOT_DIR",
    "KHIPU_GRAPH_SOURCES_RESOLVED",
)


def render_extra_env(environ: dict[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    lines = []
    for key in PASSTHROUGH_ENV:
        value = (env.get(key) or "").strip()
        if value:
            lines.append(f"\n\t\t<key>{key}</key>\n\t\t<string>{escape(value)}</string>")
    return "".join(lines)


def render_plist(job: str) -> bytes:
    template_name = _JOB_TEMPLATE.get(job)
    if not template_name:
        raise ValueError(f"unknown scheduled job: {job}")
    template_path = TEMPLATE_DIR / template_name
    if not template_path.is_file():
        raise FileNotFoundError(f"missing launchd template: {template_path}")
    text = template_path.read_text(encoding="utf-8")
    ctx = render_context()
    for key, value in ctx.items():
        text = text.replace(f"{{{{{key}}}}}", value)
    spec = _JOB_SPECS.get(job, {})
    stem = str(spec.get("log_stem") or job)
    out_log, err_log = _log_paths(stem)
    text = text.replace("{{EXTRA_ENV}}", render_extra_env())
    text = text.replace("{{STDOUT_LOG}}", str(out_log))
    text = text.replace("{{STDERR_LOG}}", str(err_log))
    return text.encode("utf-8")


def _launchctl_load(label: str, plist_path: Path) -> dict[str, Any]:
    uid = os.getuid()
    domain = f"gui/{uid}"
    unload = subprocess.run(
        ["launchctl", "bootout", domain, str(plist_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    load = subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if load.returncode != 0:
        err = (load.stderr or load.stdout or unload.stderr or "").strip()
        return {"ok": False, "error": err or "launchctl bootstrap failed", "label": label}
    return {"ok": True, "label": label, "path": str(plist_path)}


def _launchctl_unload(label: str, plist_path: Path) -> None:
    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{label}"],
        capture_output=True,
        text=True,
        check=False,
    )
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}", str(plist_path)],
        capture_output=True,
        text=True,
        check=False,
    )


def install_job(job: str) -> dict[str, Any]:
    label = _LABELS.get(job)
    if not label:
        return {"ok": False, "error": "unknown_job", "job": job}
    agents = _launchagents_dir()
    agents.mkdir(parents=True, exist_ok=True)
    dest = _plist_path(label)
    dest.write_bytes(render_plist(job))
    loaded = _launchctl_load(label, dest)
    if not loaded.get("ok"):
        return loaded
    versions = read_versions()
    scheduled = versions.setdefault("scheduled_jobs", {})
    if isinstance(scheduled, dict):
        scheduled[job] = True
        if job == "graph_build":
            scheduled["graph"] = True
            versions["graph_producer"] = True
    write_versions(versions)
    return {"ok": True, "job": job, "label": label, "path": str(dest)}


def uninstall_job(job: str) -> dict[str, Any]:
    label = _LABELS.get(job)
    if not label:
        return {"ok": False, "error": "unknown_job", "job": job}
    dest = _plist_path(label)
    if dest.is_file():
        _launchctl_unload(label, dest)
        dest.unlink(missing_ok=True)
    versions = read_versions()
    scheduled = versions.get("scheduled_jobs")
    if isinstance(scheduled, dict):
        scheduled.pop(job, None)
        if job == "graph_build":
            scheduled.pop("graph", None)
        if job == "nightly" and not scheduled.get("graph_build"):
            versions.pop("graph_producer", None)
        if not scheduled:
            versions.pop("scheduled_jobs", None)
    write_versions(versions)
    return {"ok": True, "job": job, "removed": str(dest)}


def install_scheduled_jobs(jobs: list[str] | None = None) -> dict[str, Any]:
    names = jobs or list(_JOB_TEMPLATE)
    results: list[dict[str, Any]] = []
    for job in names:
        results.append(install_job(job))
    failed = [r for r in results if not r.get("ok")]
    if failed:
        return {"ok": False, "results": results, "error": "install_partial_failure"}
    return {"ok": True, "results": results}


def uninstall_scheduled_jobs(jobs: list[str] | None = None) -> dict[str, Any]:
    names = jobs or list(_JOB_TEMPLATE)
    results = [uninstall_job(job) for job in names]
    return {"ok": True, "results": results}
