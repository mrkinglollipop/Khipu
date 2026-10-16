"""Owner writes made while the hub is away (offline and self-healing scope,
decision 4): closing an owed item, superseding a decision.

One JSON file per write in ``<data dir>/owner-writes/``, named so they sort
oldest first, written by temp file and rename like the capture outbox. A write
is replayed oldest first when the hub answers, so none lands out of order. One
the hub refuses (the commitment was removed meanwhile, say) moves to
``refused/`` with the error and is never dropped. Forgetting is not queued
here: a forget that only looked done would be a privacy failure.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TOOLS = ("owed_update", "decisions_update")
# What a queued write's reply repeats of the request, so a caller sees the same
# shape it would have had from the hub.
_ECHO = ("id", "link_id", "action", "until", "by")


def queue_dir() -> Path:
    from khipu.paths import data_dir

    return data_dir() / "owner-writes"


def _refused_dir() -> Path:
    return queue_dir() / "refused"


def _jobs() -> list[Path]:
    try:
        return sorted(p for p in queue_dir().glob("*.json") if not p.name.startswith("."))
    except OSError:
        return []


def waiting() -> bool:
    """Whether any write is waiting for the hub: one listdir."""
    return bool(_jobs())


def _read(path: Path) -> dict[str, Any] | None:
    try:
        job = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(job, dict) and job.get("tool") in TOOLS and isinstance(job.get("args"), dict):
        return job
    return None


def _apply(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    # Resolved when called: mcp_server imports this module's callers.
    from khipu import mcp_server

    fn = {"owed_update": mcp_server._owed_update_on_hub,
          "decisions_update": mcp_server._decisions_update_on_hub}[tool]
    return fn(args)


def _enqueue(tool: str, args: dict[str, Any], reason: str) -> dict[str, Any]:
    from khipu.outbox import _atomic_write

    queued_at = datetime.now(timezone.utc).isoformat()
    d = queue_dir()
    d.mkdir(parents=True, exist_ok=True)
    _atomic_write(d / f"{time.time_ns():020d}-{uuid.uuid4().hex[:8]}.json",
                  {"tool": tool, "args": args, "queued_at": queued_at, "reason": reason})
    return {"ok": True, "waiting_to_sync": True, "queued_at": queued_at,
            **{k: args[k] for k in _ECHO if args.get(k) is not None}}


def run(tool: str, args: dict[str, Any], *, queue_ok: bool = True) -> dict[str, Any]:
    """Apply a write on the hub, or queue it when the hub is away. Waiting
    writes go first, so this one never lands ahead of an older one."""
    from khipu.hub_snapshot import hub_connection_failed

    if queue_ok and _jobs():
        drain()
        if _jobs():
            return _enqueue(tool, args, "earlier writes are still waiting for the hub")
    try:
        return _apply(tool, args)
    except Exception as exc:  # noqa: BLE001 - only a hub that is away queues
        if queue_ok and hub_connection_failed(exc):
            return _enqueue(tool, args, f"{type(exc).__name__}: {str(exc)[:200]}")
        raise


def _refuse(path: Path, job: dict[str, Any], exc: BaseException) -> None:
    from khipu.outbox import _atomic_write

    _refused_dir().mkdir(parents=True, exist_ok=True)
    _atomic_write(_refused_dir() / path.name, {
        **job, "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        "refused_at": datetime.now(timezone.utc).isoformat()})
    path.unlink(missing_ok=True)


def drain() -> dict[str, Any]:
    """Replay waiting writes oldest first. Stops at the first sign the hub is
    away and keeps the rest; busy while another process is replaying."""
    from khipu.hub_snapshot import hub_connection_failed

    out: dict[str, Any] = {"replayed": 0, "refused": 0, "waiting": 0}
    if not _jobs():
        return out
    fd = os.open(str(queue_dir() / ".owner-writes.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {**out, "busy": True}
        for path in _jobs():
            job = _read(path)
            if job is None:  # never left to block the line behind it
                _refuse(path, {}, ValueError("this waiting write could not be read"))
                out["refused"] += 1
                continue
            try:
                _apply(job["tool"], job["args"])
            except Exception as exc:  # noqa: BLE001
                if hub_connection_failed(exc):
                    out["stopped_early"] = True
                    break
                _refuse(path, job, exc)
                out["refused"] += 1
                continue
            path.unlink(missing_ok=True)
            out["replayed"] += 1
    finally:
        os.close(fd)
    out["waiting"] = len(_jobs())
    return out


def status() -> dict[str, Any]:
    jobs = [j for j in (_read(p) for p in _jobs()) if j is not None]
    try:
        refused = len(list(_refused_dir().glob("*.json")))
    except OSError:
        refused = 0
    queued = sorted(str(j.get("queued_at") or "") for j in jobs)
    return {"pending": len(jobs), "refused": refused, "oldest_queued_at": queued[0] if queued else None}


def pending_marks(tool: str) -> dict[int, str]:
    """``{id: action}`` for rows with a write still waiting, the newest
    write for an id winning."""
    marks: dict[int, str] = {}
    for path in _jobs():
        job = _read(path)
        if job is None or job["tool"] != tool:
            continue
        try:
            marks[int(job["args"].get("id"))] = str(job["args"].get("action") or "")
        except (TypeError, ValueError):
            continue
    return marks


def annotate(rows: list[dict[str, Any]], tool: str) -> dict[str, Any]:
    """Mark rows that have a write waiting (``pending_sync``); returns the
    payload fields to add (``waiting_to_sync`` when anything waits or was
    refused)."""
    marks = pending_marks(tool)
    for row in rows:
        action = marks.get(row.get("id"))
        if action:
            row["pending_sync"] = action
    st = status()
    return {"waiting_to_sync": st} if st["pending"] or st["refused"] else {}
