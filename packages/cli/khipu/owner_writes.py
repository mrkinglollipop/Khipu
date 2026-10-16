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

Every write carries a unique id (``write_id``, in its args). The hub records
the id in ``owner_writes_applied`` (migration 0031) in the same transaction as
the write (``record_on_hub``) and never applies an id it already holds. Any
entry, whatever its state, may already be on the hub (a lost COMMIT reply
leaves it ``waiting``; a crash leaves it ``sending`` or ``reserved``), so a
replay first asks the hub whether its id was answered (``_landed``) and sends
it only when it was not: exactly once, read from the record, never inferred
from the rows the write touched. The record keeps the outcome: a write the hub
refuses (an error, or ``ok: false``) commits its id as refused, with the
reason, in one transaction with the undone write (``apply_on_hub``), so a
replay after a lost reply reports the refusal and never calls it applied. A
hub without the record (0031 not applied) keeps every entry waiting. A refused
entry moves to ``refused/`` with the reason and is reported, never counted as
done. An entry with no id was queued by a build before ids existed; whether
it landed cannot be known, so it is refused for the owner to check, never sent.
Forgetting is not queued here: a forget that only looked done would be a
privacy failure.
"""
from __future__ import annotations

import fcntl
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TOOLS = ("owed_update", "decisions_update")
# What a queued write's reply repeats of the request, so a caller sees the same
# shape it would have had from the hub.
_ECHO = ("id", "ids", "link_id", "action", "until", "by")
_HELD = threading.local()  # the entry this thread's run() is applying
_NO_ID = "queued by an older build; check it and redo it"


class HubRecordMissing(RuntimeError):
    """The hub has no ``owner_writes_applied`` table (migration 0031 is not
    applied there), so whether a write landed cannot be read."""


def _with_id(args: dict[str, Any]) -> dict[str, Any]:
    return args if args.get("write_id") else {**args, "write_id": uuid.uuid4().hex}


def record_on_hub(cur, tool: str, args: dict[str, Any]) -> bool:
    """Record this write's id on the hub in the caller's transaction, before
    the write itself. False: the hub already applied this id, and the caller
    must roll back and apply nothing."""
    import psycopg

    write_id = _write_id(args)
    try:
        cur.execute("INSERT INTO owner_writes_applied (write_id, tool) VALUES (%s, %s) "
                    "ON CONFLICT (write_id) DO NOTHING", (write_id, tool))
    except psycopg.errors.UndefinedTable as exc:
        raise HubRecordMissing("the hub has no record of applied owner writes; run khipu migrate (0031)") from exc
    return cur.rowcount == 1


def _write_id(args: dict[str, Any]) -> str:
    write_id = str(args.get("write_id") or "")
    if not write_id:
        raise ValueError("an owner write reached the hub without its write_id")
    return write_id


def _refuse_on_hub(conn, cur, tool: str, args: dict[str, Any], error: str) -> None:
    """Undo this write and commit its id with the outcome refused, in one
    transaction."""
    conn.rollback()
    cur.execute("INSERT INTO owner_writes_applied (write_id, tool, outcome, error) "
                "VALUES (%s, %s, 'refused', %s) ON CONFLICT (write_id) DO NOTHING",
                (_write_id(args), tool, error[:300]))
    conn.commit()


def _outcome_on_hub(cur, args: dict[str, Any]) -> tuple[str, str | None] | None:
    """``(outcome, error)`` the hub recorded for this write, or ``None``."""
    cur.execute("SELECT outcome, error FROM owner_writes_applied WHERE write_id = %s",
                (_write_id(args),))
    row = cur.fetchone()
    return None if row is None else (str(row[0]), row[1])


def _refusal(out: dict[str, Any]) -> str:
    return str(out.get("error") or "the hub refused this write: " + json.dumps(out, default=str)[:200])


def apply_on_hub(conn, cur, tool: str, args: dict[str, Any], write, echo: dict[str, Any]) -> dict[str, Any]:
    """Apply one owner write on the hub with its outcome recorded in the same
    transaction. ``write()`` makes the change in ``cur`` and returns the reply;
    a reply with ``ok: false`` or a ``ValueError`` is a refusal: the change is
    undone and the id committed as refused. An id the hub already answered
    applies nothing and replies with the recorded outcome."""
    if not record_on_hub(cur, tool, args):
        conn.rollback()
        prior = _outcome_on_hub(cur, args)
        if prior is not None and prior[0] == "refused":
            return {"ok": False, "already_refused": True, "error": prior[1], **echo}
        return {"ok": True, "already_applied": True, **echo}
    try:
        out = write()
    except ValueError as exc:
        _refuse_on_hub(conn, cur, tool, args, f"{type(exc).__name__}: {exc}")
        raise
    if isinstance(out, dict) and out.get("ok") is False:
        _refuse_on_hub(conn, cur, tool, args, _refusal(out))
    else:
        conn.commit()
    return out


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


def _landed(tool: str, args: dict[str, Any]) -> tuple[str, str | None] | None:
    """``(outcome, error)`` when the hub answered this write (its id is in
    the hub's record), else ``None``. Raises when the hub cannot be asked, and
    ``HubRecordMissing`` when it keeps no record."""
    import psycopg

    from khipu import offline
    from khipu.db import connect

    with connect(statement_timeout_s=offline.setting("hub_statement_timeout")) as conn:
        with conn.cursor() as cur:
            try:
                return _outcome_on_hub(cur, args)
            except psycopg.errors.UndefinedTable as exc:
                raise HubRecordMissing(
                    "the hub has no record of applied owner writes; run khipu migrate (0031)") from exc


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
        args = _with_id(args)
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

    args = _with_id(args)
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
    ``"unrecorded"`` (the hub keeps no record to ask), or ``None`` when it
    was already gone."""
    from khipu.hub_snapshot import hub_connection_failed

    if not path.exists():
        _held_path(path).unlink(missing_ok=True)
        return None
    job = _read(path)
    if job is None:  # never left to block the line behind it
        _refuse(path, {}, "ValueError: this waiting write could not be read")
        return "refused"
    if not job["args"].get("write_id"):
        # It may have landed already, and no record can say: never sent.
        _refuse(path, job, _NO_ID)
        return "refused"
    try:
        landed = _landed(job["tool"], job["args"])
        if landed is not None:
            if landed[0] == "refused":
                _refuse(path, job, landed[1] or "the hub refused this write")
                return "refused"
            _remove(path)
            return "replayed"
        _write(path, {**job, "state": "sending", "sent_at": datetime.now(timezone.utc).isoformat()})
        result = _apply(job["tool"], job["args"])
    except HubRecordMissing:
        return "unrecorded"
    except Exception as exc:  # noqa: BLE001
        if hub_connection_failed(exc):
            return "away"
        _refuse(path, job, f"{type(exc).__name__}: {str(exc)[:200]}")
        return "refused"
    if isinstance(result, dict) and result.get("ok") is False:
        _refuse(path, job, _refusal(result))
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
            if step in ("away", "unrecorded"):
                out["stopped_early"] = True
                if step == "unrecorded":
                    out["hub_needs_migrate"] = True
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
        ids = job["args"].get("ids")
        for one in (ids if isinstance(ids, list) else [job["args"].get("id")]):
            try:
                marks[int(one)] = str(job["args"].get("action") or "")
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
