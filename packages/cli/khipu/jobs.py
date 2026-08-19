"""Scheduled memory/graph jobs — thin wrappers around legacy consolidate/graphify scripts.

Khipu owns the launchd labels and CLI entrypoints; the engines stay unchanged.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from khipu.paths import DEFAULT_DIR, ensure_data_dir

CONSOLIDATE_NIGHTLY = Path(
    os.environ.get(
        "KHIPU_CONSOLIDATE_NIGHTLY",
        "/Volumes/Cloud Storage/Memory/conversations/scripts/consolidate_nightly.py",
    )
)
# Live Claude monthly agent runs launchd `_python/conversation-memory-monthly.py`
# (Memory tree). Do not default to Memory `consolidate_monthly.py` — that is the
# Cursor-era script keyed to CURSOR_ROOT and skips when that tree is gone.
CONSOLIDATE_MONTHLY = Path(
    os.environ.get(
        "KHIPU_CONSOLIDATE_MONTHLY",
        "/Volumes/Cloud Storage/Claude/launchd/drivers/_python/conversation-memory-monthly.py",
    )
)
GRAPHIFY_NIGHTLY = Path(
    os.environ.get(
        "KHIPU_GRAPHIFY_NIGHTLY",
        "/Volumes/Cloud Storage/Claude/UNIFICATION/scripts/graphify_nightly.py",
    )
)
BUILD_INDEX = Path(
    os.environ.get(
        "KHIPU_BUILD_INDEX",
        "/Volumes/Cloud Storage/Memory/conversations/scripts/build_index.py",
    )
)

LOG_DIR_FROZEN = Path.home() / "Library" / "Logs" / "frozen-threshold"
LOG_DIR_CONFIG = DEFAULT_DIR / "logs"

PLIST_NIGHTLY = "com.matt.khipu-nightly"
PLIST_MONTHLY = "com.matt.khipu-monthly"
PLIST_GRAPH = "com.matt.khipu-graph"

LEGACY_PLIST_NIGHTLY = "com.matt.conversation-memory-nightly"
LEGACY_PLIST_GRAPH = "com.matt.graphify-nightly"

INDEX_SLACK_S = int(os.environ.get("KHIPU_INDEX_SLACK_S", "1800"))

_JOB_SPECS: dict[str, dict[str, str]] = {
    "nightly": {
        "plist": PLIST_NIGHTLY,
        "log_stem": "khipu-nightly",
        "schedule": "daily 02:05",
    },
    "monthly": {
        "plist": PLIST_MONTHLY,
        "log_stem": "khipu-monthly",
        "schedule": "monthly day 1 09:00",
    },
    "graph_build": {
        "plist": PLIST_GRAPH,
        "log_stem": "khipu-graph",
        "schedule": "daily 02:17",
    },
}


def _log_paths(stem: str) -> tuple[Path, Path]:
    for base in (LOG_DIR_FROZEN, LOG_DIR_CONFIG):
        out = base / f"{stem}.out.log"
        err = base / f"{stem}.err.log"
        if out.parent.exists() or base == LOG_DIR_FROZEN:
            return out, err
    LOG_DIR_CONFIG.mkdir(parents=True, exist_ok=True)
    return LOG_DIR_CONFIG / f"{stem}.out.log", LOG_DIR_CONFIG / f"{stem}.err.log"


def _launchagents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _plist_path(label: str) -> Path:
    return _launchagents_dir() / f"{label}.plist"


def _plist_loaded(label: str) -> bool:
    """True only when launchctl reports the agent in the current GUI domain.

    A leftover plist on disk is not loaded. Nonzero print rc or any
    exception (timeout, missing launchctl) is not loaded.
    """
    try:
        uid = os.getuid()
        r = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/{label}"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _read_job_state(name: str) -> dict[str, Any] | None:
    path = ensure_data_dir() / "state" / f"job-{name}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _write_job_state(name: str, exit_code: int) -> None:
    state_dir = ensure_data_dir() / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "exit": exit_code,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    path = state_dir / f"job-{name}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    tmp.replace(path)


def _run_script(script: Path, *, args: list[str] | None = None, log_stem: str, state_name: str) -> int:
    if not script.is_file():
        raise FileNotFoundError(f"job script not found: {script}")
    out_log, err_log = _log_paths(log_stem)
    out_log.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    header = f"\n--- khipu job {state_name} {stamp} ---\n".encode()
    cmd = [sys.executable, str(script), *(args or [])]
    with open(out_log, "ab") as out_f, open(err_log, "ab") as err_f:
        out_f.write(header)
        err_f.write(header)
        proc = subprocess.run(
            cmd,
            stdout=out_f,
            stderr=err_f,
            env=os.environ.copy(),
        )
    _write_job_state(state_name, proc.returncode)
    return proc.returncode


def run_nightly() -> int:
    return _run_script(CONSOLIDATE_NIGHTLY, log_stem="khipu-nightly", state_name="nightly")


def run_monthly(*, dry_run: bool = False) -> int:
    args: list[str] = []
    if dry_run:
        # Live Claude monthly (conversation-memory-monthly.py) has no --dry-run.
        # Cursor-era consolidate_monthly.py does — only pass the flag there.
        if CONSOLIDATE_MONTHLY.name == "conversation-memory-monthly.py":
            print(
                "error: live monthly driver has no --dry-run "
                f"(script={CONSOLIDATE_MONTHLY})",
                file=sys.stderr,
            )
            return 2
        args = ["--dry-run"]
    return _run_script(
        CONSOLIDATE_MONTHLY,
        args=args,
        log_stem="khipu-monthly",
        state_name="monthly",
    )


def run_graph_build() -> int:
    return _run_script(GRAPHIFY_NIGHTLY, log_stem="khipu-graph", state_name="graph_build")


def run_build_index() -> int:
    return _run_script(BUILD_INDEX, log_stem="khipu-build-index", state_name="build_index")


def nightly_plist_path() -> Path:
    khipu = _plist_path(PLIST_NIGHTLY)
    legacy = _plist_path(LEGACY_PLIST_NIGHTLY)
    if khipu.is_file():
        return khipu
    return legacy


def nightly_log_path() -> Path:
    explicit = (os.environ.get("KHIPU_NIGHTLY_LOG") or "").strip()
    if explicit:
        return Path(explicit)
    khipu = LOG_DIR_FROZEN / "khipu-nightly.out.log"
    legacy = LOG_DIR_FROZEN / "conversation-memory-nightly.out.log"
    if khipu.is_file():
        if not legacy.is_file() or khipu.stat().st_mtime >= legacy.stat().st_mtime:
            return khipu
    if legacy.is_file():
        return legacy
    return khipu


def graph_plist_path() -> Path:
    khipu = _plist_path(PLIST_GRAPH)
    legacy = _plist_path(LEGACY_PLIST_GRAPH)
    if khipu.is_file():
        return khipu
    return legacy


def _job_entry(name: str) -> dict[str, Any]:
    spec = _JOB_SPECS[name]
    out_log, err_log = _log_paths(spec["log_stem"])
    last_run_mtime: float | None = None
    try:
        if out_log.is_file():
            last_run_mtime = out_log.stat().st_mtime
    except OSError:
        pass
    state = _read_job_state(name)
    last_exit = state.get("exit") if state else None
    return {
        "plist_label": spec["plist"],
        "log_path": str(out_log),
        "err_log_path": str(err_log),
        "last_run_mtime": last_run_mtime,
        "last_run_iso": (
            datetime.fromtimestamp(last_run_mtime, tz=timezone.utc).isoformat()
            if last_run_mtime is not None
            else None
        ),
        "plist_loaded": _plist_loaded(spec["plist"]),
        "next_schedule": spec["schedule"],
        "last_exit": last_exit,
        "last_exit_ts": state.get("ts") if state else None,
    }


def job_status() -> dict[str, Any]:
    return {
        "nightly": _job_entry("nightly"),
        "monthly": _job_entry("monthly"),
        "graph_build": _job_entry("graph_build"),
    }


def _is_index_sync_host() -> bool:
    from khipu.git_sync_health import is_sync_host

    return is_sync_host()


def index_freshness(*, memory_root: Path | None = None) -> dict[str, Any]:
    """Index files vs nightly log — red only when the nightly ran and index did not follow."""
    from khipu.config import path_setting

    mem = memory_root or path_setting("memory_root")
    out: dict[str, Any] = {
        "ok": True,
        "applicable": _is_index_sync_host(),
        "reasons": [],
    }
    if not out["applicable"]:
        out["note"] = "not the sync host — index freshness is judged on the nightly Mac"
        return out
    if mem is None or not mem.is_dir():
        out["ok"] = False
        out["reasons"].append("memory_root not configured")
        return out

    index_files: list[Path] = []
    memory_md = mem / "MEMORY.md"
    if memory_md.is_file():
        index_files.append(memory_md)
    index_files.extend(sorted(mem.glob("_index_*.md")))

    if not index_files:
        out["ok"] = False
        out["reasons"].append("no MEMORY.md or _index_*.md found under memory root")
        return out

    try:
        index_mtime = max(f.stat().st_mtime for f in index_files)
    except OSError as e:
        out["ok"] = False
        out["reasons"].append(f"could not stat index files: {e}")
        return out

    nightly_log = nightly_log_path()
    nightly_mtime: float | None = None
    try:
        if nightly_log.is_file():
            nightly_mtime = nightly_log.stat().st_mtime
    except OSError:
        pass

    out["index_mtime"] = index_mtime
    out["nightly_log"] = str(nightly_log)
    out["nightly_log_mtime"] = nightly_mtime
    out["index_files"] = [str(p.relative_to(mem)) for p in index_files[:20]]

    if nightly_mtime and nightly_mtime - index_mtime > INDEX_SLACK_S:
        lag_min = int((nightly_mtime - index_mtime) // 60)
        out["ok"] = False
        out["reasons"].append(
            f"index is {lag_min} min older than the last nightly log — rebuild may have been skipped"
        )
    return out
