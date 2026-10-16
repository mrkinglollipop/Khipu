"""Owner writes made while the hub is away (offline and self-healing scope,
decision 4): closing an owed item, superseding a decision.

Every owner write from this Mac is an entry in one ledger,
``<data dir>/owner-writes/``: one JSON file per write, named in the order the
owner made it (the name is taken under a lock, so two writes made at once still
sort as they were made), written by temp file and rename like the capture
outbox. An entry is in one of three states:

- ``reserved``: its caller is applying it now, and holds a lock beside it
  (``.<name>.held``). No replay passes it, so nothing made later lands first.
- ``waiting``: the hub was away; it is replayed in order when the hub answers.
- ``sending``: a replay sent it and has not yet seen the answer.

A ``sending`` entry, or a ``reserved`` one whose caller died, may already be on
the hub: it is checked there (``_landed``) and only sent again when the hub
does not show it, so a crash between the hub's commit and the entry's removal
never applies a write twice. One the hub refuses (an error, or ``ok: false``)
moves to ``refused/`` with the reason and is reported, never counted as done.
Forgetting is not queued here: a forget that only looked done would be a
privacy failure.
"""
from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TOOLS = ("owed_update", "decisions_update")
# What a queued write's reply repeats of the request, so a caller sees the same
# shape it would have had from the hub.
_ECHO = ("id", "link_id", "action", "until", "by")
_HELD = threading.local()  # the entry this thread's run() is applying


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


def _landed(tool: str, args: dict[str, Any]) -> bool:
    """Whether the hub already shows this write, for an entry whose last send
    may have committed before this Mac saw the answer. Raises when the hub
    cannot be asked."""
    from khipu import offline
    from khipu.db import connect

    action = str(args.get("action") or "").strip().lower()
    with connect(statement_timeout_s=offline.setting("hub_statement_timeout")) as conn:
        with conn.cursor() as cur:
            if tool == "owed_update":
                cur.execute("SELECT status, due_after > now() FROM commitments WHERE id = %s", (args.get("id"),))
                row = cur.fetchone()
                if row is None:
                    return False
                if action == "snooze":
                    return bool(row[1])
                return row[0] == ("closed" if action == "close" else "open")
            if action in ("confirm", "reject"):
                cur.execute("SELECT state FROM decision_links WHERE id = %s", (args.get("link_id"),))
                row = cur.fetchone()
                return row is not None and row[0] != "candidate"
            cur.execute("SELECT superseded_by, retracted_at FROM decisions WHERE id = %s", (args.get("id"),))
            row = cur.fetchone()
            if row is None:
                return False
            superseded_by, retracted_at = row
            if action == "supersede":
                return superseded_by is not None and (args.get("by") is None or int(args["by"]) == superseded_by)
            if action == "retract":
                return retracted_at is not None
            if action == "restore":
                return superseded_by is None
            return False


def _write(path: Path, job: dict[str, Any]) -> None:
    from khipu.outbox import _atomic_write

    _atomic_write(path, job)


def _held_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.held")


def _reserve(tool: str, args: dict[str, Any]) -> tuple[Path, int]:
    """Add an entry in the order this write was made and hold it. The name is
    taken and the file written under one lock, so an entry never appears with
    a name sorting before one already there."""
    d = queue_dir()
    d.mkdir(parents=True, exist_ok=True)
    order = os.open(str(d / ".owner-writes.order.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(order, fcntl.LOCK_EX)
        last = _jobs()
        seq = time.time_ns()
        if last:
            try:
                seq = max(seq, int(last[-1].name.split("-", 1)[0]) + 1)
            except ValueError:
                pass
        path = d / f"{seq:020d}-{os.getpid()}.json"
        held = os.open(str(_held_path(path)), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(held, fcntl.LOCK_EX)
        _write(path, {"tool": tool, "args": args, "state": "reserved",
                      "made_at": datetime.now(timezone.utc).isoformat()})
    finally:
        os.close(order)
    return path, held


def _release(path: Path, held: int) -> None:
    _held_path(path).unlink(missing_ok=True)
    os.close(held)


def _remove(path: Path) -> None:
    path.unlink(missing_ok=True)
    _held_path(path).unlink(missing_ok=True)


def _claim(path: Path) -> int | None:
    """Take an entry's held lock, or ``None`` while a live caller holds it.
    A caller writes its entry's last state before letting go, so what is read
    after the claim is final."""
    try:
        fd = os.open(str(_held_path(path)), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _enqueue(tool: str, args: dict[str, Any], reason: str) -> dict[str, Any]:
    """Mark a write as waiting for the hub: this thread's held entry, or a new
    entry at the end of the line."""
    queued_at = datetime.now(timezone.utc).isoformat()
    mine = getattr(_HELD, "entry", None)
    if mine is not None and mine[1] == (tool, args):
        path = mine[0]
    else:
        path, held = _reserve(tool, args)
        _release(path, held)
    _write(path, {"tool": tool, "args": args, "state": "waiting", "queued_at": queued_at, "reason": reason})
    return {"ok": True, "waiting_to_sync": True, "queued_at": queued_at,
            **{k: args[k] for k in _ECHO if args.get(k) is not None}}


def run(tool: str, args: dict[str, Any], *, queue_ok: bool = True) -> dict[str, Any]:
    """Apply a write on the hub, or queue it when the hub is away. Its entry
    is made first, so a write made after it can never land ahead of it, and
    it is applied directly only when no older entry is still in line."""
    from khipu.hub_snapshot import hub_connection_failed

    if not queue_ok:
        return _apply(tool, args)
    path, held = _reserve(tool, args)
    _HELD.entry = (path, (tool, args))
    try:
        if _jobs()[0] != path:
            drain()
            if _jobs()[0] != path:
                return _enqueue(tool, args, "earlier writes are still waiting for the hub")
        try:
            result = _apply(tool, args)
        except Exception as exc:  # noqa: BLE001 - only a hub that is away queues
            if hub_connection_failed(exc):
                return _enqueue(tool, args, f"{type(exc).__name__}: {str(exc)[:200]}")
            _remove(path)
            raise
        _remove(path)
        return result
    finally:
        _HELD.entry = None
        _release(path, held)


def _refuse(path: Path, job: dict[str, Any], error: str) -> None:
    _refused_dir().mkdir(parents=True, exist_ok=True)
    _write(_refused_dir() / path.name, {
        **job, "error": error[:300], "refused_at": datetime.now(timezone.utc).isoformat()})
    _remove(path)


def _replay(path: Path) -> str | None:
    """Replay one claimed entry: ``"replayed"``, ``"refused"``, ``"away"``,
    or ``None`` when it was already gone."""
    from khipu.hub_snapshot import hub_connection_failed

    if not path.exists():
        _held_path(path).unlink(missing_ok=True)
        return None
    job = _read(path)
    if job is None:  # never left to block the line behind it
        _refuse(path, {}, "ValueError: this waiting write could not be read")
        return "refused"
    try:
        # Reserved and unheld means its caller died mid-apply: as unknown as a send.
        if job.get("state") in ("sending", "reserved") and _landed(job["tool"], job["args"]):
            _remove(path)
            return "replayed"
        _write(path, {**job, "state": "sending", "sent_at": datetime.now(timezone.utc).isoformat()})
        result = _apply(job["tool"], job["args"])
    except Exception as exc:  # noqa: BLE001
        if hub_connection_failed(exc):
            return "away"
        _refuse(path, job, f"{type(exc).__name__}: {str(exc)[:200]}")
        return "refused"
    if isinstance(result, dict) and result.get("ok") is False:
        _refuse(path, job, str(result.get("error") or "the hub refused this write: "
                               + json.dumps(result, default=str)[:200]))
        return "refused"
    _remove(path)
    return "replayed"


def drain() -> dict[str, Any]:
    """Replay waiting writes in the order they were made. Stops at the first
    sign the hub is away, and at an entry a live caller is applying; busy while
    another process is replaying."""
    out: dict[str, Any] = {"replayed": 0, "refused": 0, "waiting": 0}
    if not _jobs():
        return out
    fd = os.open(str(queue_dir() / ".owner-writes.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {**out, "busy": True}
        mine = getattr(_HELD, "entry", None)
        while (jobs := _jobs()) and not (mine is not None and jobs[0] == mine[0]):
            path = jobs[0]
            claim = _claim(path)
            if claim is None:
                out["in_flight"] = True
                break
            try:
                step = _replay(path)
            finally:
                os.close(claim)
            if step == "away":
                out["stopped_early"] = True
                break
            if step:
                out[step] += 1
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
    queued = sorted(str(j.get("queued_at") or j.get("made_at") or "") for j in jobs)
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
