"""Render Khipu LaunchAgent plists from templates — per-user Application Support paths."""
# this exact task; no further delegation is possible or appropriate here.

from __future__ import annotations

import os
import plistlib
import shlex
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
    PLIST_NOTES_WATCH,
    PLIST_QUEUE_DRAIN,
    PLIST_RECALL_DAEMON,
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
    "notes_watch": "com.khipu.notes-watch.plist",
    "queue_drain": "com.khipu.queue-drain.plist",
    "recall_daemon": "com.khipu.recall-daemon.plist",
}

# What a fresh install schedules. The recall service is opt-in
# (`khipu jobs install recall_daemon`): the hook works without it, so a first
# run must not start a resident process nobody asked for. Once its plist is
# installed it is refreshed and uninstalled with the rest.
_DEFAULT_JOBS = tuple(job for job in _JOB_TEMPLATE if job != "recall_daemon")

_LABELS = {
    "nightly": PLIST_NIGHTLY,
    "monthly": PLIST_MONTHLY,
    "graph_build": PLIST_GRAPH,
    "notes_watch": PLIST_NOTES_WATCH,
    "queue_drain": PLIST_QUEUE_DRAIN,
    "recall_daemon": PLIST_RECALL_DAEMON,
}

_DRAIN_WRAPPER_NAME = "khipu-queue-drain-retry"
_DRAIN_STARTED = "[khipu-queue-drain] entered Khipu"
_DRAIN_ATTEMPT = "[khipu-queue-drain] attempt "
_STARTUP_EINTR = "InterruptedError: [Errno 4] Interrupted system call"
_STARTUP_LOG_BYTES = 256 * 1024


def _queue_drain_arguments(python: str) -> list[str]:
    # Mark the boundary before importing ANY Khipu code, including __init__.
    # runpy preserves the -m entry point and argv without a second interpreter.
    shim = (
        "import sys, runpy; "
        f"sys.stderr.write({_DRAIN_STARTED + chr(10)!r}); sys.stderr.flush(); "
        "runpy.run_module('khipu', run_name='__main__', alter_sys=True)"
    )
    # Inline sh survives a fatal Python initialization failure and needs no
    # launcher file written into the signed bundle (or during a health check).
    script = f"""umask 077
python=$1
shift
err=$(mktemp "${{TMPDIR:-/tmp}}/khipu-drain.XXXXXX") || exit 1
trap 'rm -f "$err"' 0
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
attempt=1
while :; do
    printf '%s%s\\n' {shlex.quote(_DRAIN_ATTEMPT)} "$attempt" >&2
    "$python" -c {shlex.quote(shim)} "$@" 2>"$err"
    status=$?
    cat "$err" >&2
    if [ "$status" -eq 0 ]; then exit 0; fi
    if [ "$attempt" -ge 3 ] ||
       grep -Fxq {shlex.quote(_DRAIN_STARTED)} "$err" ||
       ! grep -Fq {shlex.quote(_STARTUP_EINTR)} "$err" ||
       ! grep -Eq 'Fatal Python error:|<frozen importlib' "$err"; then
        exit "$status"
    fi
    sleep "$attempt" || exit "$status"
    attempt=$((attempt + 1))
done
"""
    return ["/bin/sh", "-c", script, _DRAIN_WRAPPER_NAME, python, "sessions", "drain"]


def queue_drain_startup_failures() -> dict[str, Any]:
    """Count startup EINTRs in a bounded log tail, including legacy direct runs."""
    data = _installed_plist(_plist_path(PLIST_QUEUE_DRAIN)) or {}
    # An installed plist is authoritative: its log may differ from today's
    # generator defaults. Do not repeat a stale/default-path health check.
    log_path = data.get("StandardErrorPath")
    if not isinstance(log_path, str) or not log_path:
        _, fallback = _log_paths(_JOB_SPECS["queue_drain"]["log_stem"])
        log_path = str(fallback)
    out: dict[str, Any] = {"count": 0, "log_path": log_path, "tail_bytes": _STARTUP_LOG_BYTES}
    try:
        with open(log_path, "rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            offset = max(0, size - _STARTUP_LOG_BYTES)
            fh.seek(offset)
            tail = fh.read(_STARTUP_LOG_BYTES)
        out["truncated"] = offset > 0
        if offset:
            tail = tail.partition(b"\n")[2]
    except FileNotFoundError:
        out["missing"] = True
        return out
    except OSError as exc:
        out["count"] = None
        out["error"] = str(exc)
        return out
    entered = startup = importing = False
    for line in tail.decode("utf-8", errors="replace").splitlines():
        if line.startswith(_DRAIN_ATTEMPT):
            entered = startup = importing = False
        elif line == _DRAIN_STARTED:
            entered = True
        elif "Python path configuration:" in line or "Fatal Python error:" in line:
            startup = True
        elif "<frozen importlib" in line:
            importing = True
        elif line.startswith(_STARTUP_EINTR):
            if not entered and (startup or importing):
                out["count"] += 1
            startup = importing = False
    return out


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
    # Bytecode cache goes OUTSIDE any signed .app bundle — see
    # khipu.paths.pycache_dir. Every other launcher (hook wrappers, the
    # desktop app's own shell-out) exports the same PYTHONPYCACHEPREFIX; a
    # launchd job pointed the bundled Python at itself just as directly, so it
    # gets the same env key here rather than a separate mechanism.
    from khipu.paths import pycache_dir

    pycache = pycache_dir()
    pycache.mkdir(parents=True, exist_ok=True)
    return {
        "KHIPU_ROOT": str(root),
        "KHIPU_PYTHON": str(_bundled_python()),
        "PYTHONPATH": ":".join(pythonpath_parts),
        "WORKING_DIRECTORY": str(support),
        "PYTHONPYCACHEPREFIX": str(pycache),
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
    "KHIPU_LEGACY_LOG_DIR",
    "KHIPU_VOLUME_ROOT",
    "KHIPU_USER_ALIASES",
)


def render_extra_env(environ: dict[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    lines = []
    for key in PASSTHROUGH_ENV:
        value = (env.get(key) or "").strip()
        if value:
            lines.append(f"\n\t\t<key>{key}</key>\n\t\t<string>{escape(value)}</string>")
    return "".join(lines)


def render_watch_paths_xml() -> str:
    """F1: every memory dir the notes scanner reads from (``khipu.notes.
    memory_dirs()``), rendered as indented ``<string>`` entries for the
    WatchPaths array. Computed at render time (``khipu jobs install`` /
    ``refresh``), not at launchd-load time — a project added to
    ``~/.claude/projects`` after install needs another install/refresh to
    be watched, same as every other baked-at-render-time value in these
    templates."""
    from khipu.notes import memory_dirs

    lines = [f"\n\t\t<string>{escape(str(p))}</string>" for p in memory_dirs()]
    return "".join(lines)


def render_plist(job: str, environ: dict[str, str] | None = None) -> bytes:
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
    text = text.replace("{{EXTRA_ENV}}", render_extra_env(environ))
    text = text.replace("{{STDOUT_LOG}}", str(out_log))
    text = text.replace("{{STDERR_LOG}}", str(err_log))
    if job == "notes_watch":
        text = text.replace("{{WATCH_PATHS}}", render_watch_paths_xml())
    if job == "queue_drain":
        data = plistlib.loads(text.encode("utf-8"))
        data["ProgramArguments"] = _queue_drain_arguments(ctx["KHIPU_PYTHON"])
        return plistlib.dumps(data)
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


def install_job(job: str, *, environ: dict[str, str] | None = None) -> dict[str, Any]:
    label = _LABELS.get(job)
    if not label:
        return {"ok": False, "error": "unknown_job", "job": job}
    agents = _launchagents_dir()
    agents.mkdir(parents=True, exist_ok=True)
    dest = _plist_path(label)
    dest.write_bytes(render_plist(job, environ))
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
    names = jobs or list(_DEFAULT_JOBS)
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


def ensure_scheduled_jobs(jobs: list[str] | None = None) -> dict[str, Any]:
    """What a first-run or reconnect needs: every job scheduled, without
    clobbering one that is already there. Missing → installed; installed
    but stale (app-rendered) → re-rendered with its baked env kept;
    maintainer-managed (plist_external) → left exactly as it is; current →
    untouched. install_scheduled_jobs() alone rewrote all three every time,
    which on a Mac that runs the jobs from a source checkout would have
    silently repointed the nightly at the app bundle (2026-09-05)."""
    names = jobs or list(_DEFAULT_JOBS)
    out: dict[str, Any] = {"installed": [], "refreshed": [], "external": [], "current": [], "results": []}
    for job in names:
        label = _LABELS.get(job)
        if not label:
            continue
        if not _plist_path(label).is_file():
            r = install_job(job)
            out["results"].append(r)
            if r.get("ok"):
                out["installed"].append(job)
        elif plist_external(job):
            out["external"].append(job)
        elif plist_current(job) is False:
            r = install_job(job, environ=_refresh_environ(job))
            out["results"].append(r)
            if r.get("ok"):
                out["refreshed"].append(job)
        else:
            out["current"].append(job)
    out["ok"] = all(r.get("ok") for r in out["results"])
    return out


def installed_jobs() -> list[str]:
    """Jobs in `_JOB_TEMPLATE` whose plist file actually exists on disk."""
    return [job for job in _JOB_TEMPLATE if _plist_path(_LABELS[job]).is_file()]


def _installed_plist(dest: Path) -> dict[str, Any] | None:
    import plistlib

    try:
        with open(dest, "rb") as fh:
            data = plistlib.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def plist_external(job: str) -> bool:
    """True when the installed plist was not rendered by this app: its
    interpreter lives outside any .app bundle and is not the one this render
    would use (a maintainer Mac runs the jobs from a source checkout with
    Homebrew Python while the app carries its own). Such a plist is never
    rewritten by refresh — re-rendering it would silently repoint the nightly
    at the bundled CLI of whatever app version happens to be installed."""
    label = _LABELS.get(job)
    dest = _plist_path(label) if label else None
    data = _installed_plist(dest) if dest and dest.is_file() else None
    if not data:
        return False
    args = data.get("ProgramArguments") or []
    prog = str(args[0]) if args else ""
    # The wrapper's shell is not the interpreter used to classify ownership.
    # Keep older wrapper bodies recognizable when the generator is updated.
    if (job == "queue_drain" and len(args) >= 5
            and args[:2] == ["/bin/sh", "-c"] and args[3] == _DRAIN_WRAPPER_NAME):
        prog = str(args[4])
    if not prog or ".app/Contents/" in prog:
        return False
    # Same interpreter this render would use (a source checkout run by a
    # maintainer, or a test): it is ours to manage after all.
    return prog != str(_bundled_python())


def _refresh_environ(job: str) -> dict[str, str]:
    """os.environ plus the PASSTHROUGH_ENV values already baked into the
    installed plist. The app launches `jobs refresh` without the maintainer's
    shell environment, and a re-render that dropped those keys would make the
    job fail closed."""
    env = dict(os.environ)
    label = _LABELS.get(job)
    dest = _plist_path(label) if label else None
    data = _installed_plist(dest) if dest and dest.is_file() else None
    baked = (data or {}).get("EnvironmentVariables") or {}
    if isinstance(baked, dict):
        for key in PASSTHROUGH_ENV:
            if key not in env and str(baked.get(key) or "").strip():
                env[key] = str(baked[key])
    return env


def plist_current(job: str) -> bool | None:
    """Whether the installed plist for `job` matches what would render today.

    `None` when the job has no plist installed, or when the plist is external
    (maintainer-managed, see plist_external) — there is nothing this app
    should compare it against."""
    label = _LABELS.get(job)
    if not label:
        return None
    dest = _plist_path(label)
    if not dest.is_file() or plist_external(job):
        return None
    return dest.read_bytes() == render_plist(job, _refresh_environ(job))


def refresh_scheduled_jobs(jobs: list[str] | None = None) -> dict[str, Any]:
    """Re-render and reload every installed LaunchAgent whose plist is stale.

    This is what the desktop app runs at every launch: a bundled-Python path
    or Application Support path can change after an app update, and nothing
    else re-renders the plists that were baked at a previous install time. A
    job that was never installed is left alone — this never installs a new
    job, only refreshes ones already present.
    """
    present = set(installed_jobs())
    names = [j for j in (jobs or list(_JOB_TEMPLATE)) if j in present]
    missing = [j for j in (jobs or list(_DEFAULT_JOBS)) if j not in present]
    refreshed: list[str] = []
    current: list[str] = []
    external: list[str] = []
    results: list[dict[str, Any]] = []
    for job in names:
        if plist_external(job):
            external.append(job)
            continue
        if plist_current(job) is False:
            result = install_job(job, environ=_refresh_environ(job))
            results.append(result)
            if result.get("ok"):
                refreshed.append(job)
        else:
            current.append(job)
    failed = [r for r in results if not r.get("ok")]
    return {
        "ok": not failed,
        "refreshed": refreshed,
        "current": current,
        "external": external,
        "missing": missing,
        "results": results,
        "startup_failures": (
            {"queue_drain": queue_drain_startup_failures()}
            if jobs is None or "queue_drain" in jobs else {}
        ),
    }
