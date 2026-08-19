"""Is the memory tree's nightly git auto-sync actually landing on GitHub?

`git_sync.py` (the last step of the legacy nightly consolidate) is deliberately
soft-failed so a git problem can never mask as a consolidation failure. The
price was that an exit 1 (branch pushed, PR left open) or exit 2 (secret gate
tripped, nothing synced) produced a green nightly, a green doctor and a Notion
line reading "Success" — the same silent-failure shape as capture (B15/B16).
Opened as state-of-play item 7 on 2026-08-17.

The fix mirrors ``session_capture``: the sync writes a **heartbeat** every run,
this module judges it, and ``khipu doctor`` / the tray / the Doctor card go red
on **evidence** — never on idleness. A clean tree ("nothing to sync") is a
successful run and stays green.

Red when any of:
  * the last recorded run exited non-zero (failed mid-flow, or secret gate);
  * the secret-gate marker (``/tmp/.memory_sync_blocked.json``) is newer than
    the last successful run — covers pre-heartbeat gates too;
  * the memory repo is not on ``main`` or still holds a ``memory-autosync-*``
    branch — the flow died between ``checkout -b`` and the post-merge cleanup;
  * the nightly demonstrably RAN after the last recorded sync — its own log is
    newer than the heartbeat. Plain wall-clock staleness is not used: a Mac that
    was asleep or powered off for a weekend has a stale heartbeat and nothing
    wrong with it, and a health check that goes red for being on vacation is a
    check people learn to ignore (audit 2026-08-17).

Only the Mac that runs the nightly is judged (``is_sync_host``); everywhere
else this is *not applicable* and green, exactly like ``graph_sync``'s producer
check.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

HEARTBEAT_NAME = "git-sync.json"
# The nightly consolidate's own log. Its mtime says when the nightly last ran at
# all, which is what separates "the sync is broken" from "the machine was off":
# log newer than the heartbeat means the nightly ran and the sync did not record.
# Slack between the nightly starting and git_sync writing its heartbeat at the end.
NIGHTLY_SLACK_S = int(os.environ.get("KHIPU_GIT_SYNC_SLACK_S", "1800"))
BLOCKED_MARKER = Path(os.environ.get("KHIPU_GIT_SYNC_BLOCKED_MARKER") or "/tmp/.memory_sync_blocked.json")


def nightly_plist_path() -> Path:
    """Prefer Khipu-owned nightly plist; fall back to legacy during soak."""
    from khipu.jobs import nightly_plist_path as _jobs_nightly_plist

    return _jobs_nightly_plist()


def nightly_log_path() -> Path:
    """Prefer Khipu nightly log when present/newer; env override wins."""
    from khipu.jobs import nightly_log_path as _jobs_nightly_log

    return _jobs_nightly_log()
# Short on purpose: this runs inside `khipu doctor`, which the tray calls at
# startup, and the memory repo lives on a mounted volume. A slow or unmounting
# volume must degrade the check, never stall the health report (audit 2026-08-17).
GIT_TIMEOUT_S = int(os.environ.get("KHIPU_GIT_SYNC_GIT_TIMEOUT_S", "3"))


def heartbeat_candidates() -> list[Path]:
    """Where a heartbeat may live. ``git_sync.py`` is dependency-free and cannot
    follow Khipu's data-dir pointer file, so it writes under ``KHIPU_DATA_DIR``
    or ``~/.config/khipu``; the reader checks Khipu's resolved data dir first
    and that fixed default second."""
    from khipu.paths import DEFAULT_DIR, data_dir

    explicit = os.environ.get("KHIPU_GIT_SYNC_HEARTBEAT")
    if explicit:
        return [Path(explicit)]
    out: list[Path] = []
    for base in (data_dir(), DEFAULT_DIR):
        p = base / "state" / HEARTBEAT_NAME
        if p not in out:
            out.append(p)
    return out


def read_heartbeat() -> dict[str, Any] | None:
    for p in heartbeat_candidates():
        if p.is_file():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    d["_path"] = str(p)
                    return d
            except Exception:  # noqa: BLE001 — a corrupt heartbeat is judged as none
                continue
    return None


def is_sync_host() -> bool:
    """Only the Mac whose launchd runs the nightly consolidate performs the sync."""
    env = os.environ.get("KHIPU_GIT_SYNC_HOST", "").strip().lower()
    if env in {"1", "true", "yes"}:
        return True
    if env in {"0", "false", "no"}:
        return False
    return nightly_plist_path().is_file()


def _parse_ts(raw: Any) -> float | None:
    if not raw:
        return None
    try:
        s = str(raw).replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except Exception:  # noqa: BLE001
        return None


def _git(repo: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, timeout=GIT_TIMEOUT_S
        )
    except Exception:  # noqa: BLE001
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def repo_state(repo: Path | None) -> dict[str, Any]:
    """Cheap local-only evidence: which branch the memory repo is on and whether
    a sync branch was left behind. No network."""
    if repo is None or not (repo / ".git").exists():
        return {"present": False}
    branch = _git(repo, "branch", "--show-current")
    listed = _git(repo, "branch", "--list", "memory-autosync-*")
    stray = [b.strip().lstrip("* ").strip() for b in (listed or "").splitlines() if b.strip()]
    return {"present": True, "branch": branch, "stray_branches": stray}


def status(*, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    out: dict[str, Any] = {
        "ok": True,
        "applicable": is_sync_host(),
        "seen": False,
        "reasons": [],
    }
    if not out["applicable"]:
        out["note"] = "not the sync host — this Mac does not run the nightly consolidate"
        return out

    hb = read_heartbeat()
    repo: Path | None = None
    if hb:
        out["seen"] = True
        out["heartbeat"] = {k: v for k, v in hb.items() if not k.startswith("_")}
        out["heartbeat_path"] = hb.get("_path")
        ts = _parse_ts(hb.get("ts"))
        out["last_run_age_s"] = int(now - ts) if ts else None
        rc = hb.get("exit")
        if rc not in (0, None):
            note = str(hb.get("note") or hb.get("outcome") or "")[:160]
            out["reasons"].append(f"last sync exited {rc}: {note}" if note else f"last sync exited {rc}")
        try:
            nightly_log = nightly_log_path()
            nightly_ran = nightly_log.stat().st_mtime if nightly_log.is_file() else None
        except OSError:
            nightly_ran = None
        out["nightly_log_age_s"] = int(now - nightly_ran) if nightly_ran else None
        if ts and nightly_ran and nightly_ran - ts > NIGHTLY_SLACK_S:
            out["reasons"].append(
                f"the nightly ran {int((nightly_ran - ts) // 60)} min after the last recorded sync "
                "— it ran and the sync did not record a result"
            )
        rp = hb.get("repo")
        repo = Path(str(rp)) if rp else None
    else:
        out["note"] = "no sync has run since the heartbeat was added (first run is the next nightly)"
    if repo is None:
        # Before the first heartbeat (or if it omitted the path) still inspect the
        # repo the sync is known to own, so a stranded branch is visible tonight.
        from khipu.config import path_setting

        default_repo = path_setting("memory_repo")
        repo = default_repo if default_repo and (default_repo / ".git").exists() else None

    if BLOCKED_MARKER.is_file():
        try:
            marker = json.loads(BLOCKED_MARKER.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            marker = {}
        mts = _parse_ts(marker.get("timestamp")) or BLOCKED_MARKER.stat().st_mtime
        last_ok = _parse_ts(hb.get("ts")) if hb and hb.get("exit") == 0 else None
        if last_ok is None or mts > last_ok:
            files = ", ".join(list((marker.get("files") or {}).keys())[:3]) or "see marker"
            out["reasons"].append(f"secret gate tripped — nothing synced ({files}); {BLOCKED_MARKER}")
            out["blocked_marker"] = str(BLOCKED_MARKER)

    rs = repo_state(repo)
    out["repo"] = rs
    if rs.get("present"):
        if rs.get("branch") not in (None, "main"):
            out["reasons"].append(f"memory repo is on '{rs['branch']}', not main — a sync died mid-flow")
        if rs.get("stray_branches"):
            out["reasons"].append(
                f"stray sync branch left behind: {', '.join(rs['stray_branches'][:3])}"
            )
    elif hb and hb.get("repo"):
        out["reasons"].append(f"memory repo not found at {hb.get('repo')}")

    out["ok"] = not out["reasons"]
    return out
