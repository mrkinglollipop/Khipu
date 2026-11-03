"""``khipu outbox span-covered``: dead capture jobs another episode already holds.

An old app's writer never announced its source identities, so the hub refused
its captures (``outbox.ANNOUNCE_REFUSAL``) until they were buried. Most have no
hub row at their own key, but their transcript range sits inside a later,
wider episode of the same session. A retry would land each as a new
near-duplicate episode with its commitments, decisions and embeddings, and
when the spanning row was forgotten, would land its text live again. So this
retires them instead, on the owner's word. It never runs from the drain.

A job retires only when, on a fresh read of its session's rows:

- it was refused for that reason and its range is digits:digits;
- nothing forgotten overlaps its range: not by ``deleted_at``, this Mac's forget
  list, a queued erasure or a forgotten source, in any harness. A forgotten row
  whose range can't be read counts as overlapping, as does every one in the
  session when the job's own range can't be read;
- one live row of the same session and harness contains its range and holds
  all its evidence (``capture._span_lacks``, the proof the record repeats).

Anything else holds it, and any check that can't run holds it too. The write is
``outbox.archive_resolved`` alone: the whole job and the covering row's raw in a
record written before the job goes. The hub gets no write and no fact steps.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

ACTION = "outbox.span_covered"
BATCH = 50
NO_COVERING_ROW = "no_covering_row"
RANGE_NOT_CONTAINED = "range_not_contained"
RANGE_NOT_NUMERIC = "range_not_numeric"
EVIDENCE_MISSING = "evidence_missing"
FORGOTTEN = "forgotten"
DIFFERENT_ERROR = "different_error"
CHECK_FAILED = "check_failed"
CHANGED = "changed"
HOLD_WORDS = {
    NO_COVERING_ROW: "no live episode in the same session and harness",
    RANGE_NOT_CONTAINED: "no live episode's range contains the job's",
    RANGE_NOT_NUMERIC: "the job's transcript range is not two numbers in order",
    EVIDENCE_MISSING: "the job carries evidence no covering episode holds",
    FORGOTTEN: "a forgotten episode overlaps the job, or its source is forgotten",
    DIFFERENT_ERROR: "the job failed with a different error",
    CHECK_FAILED: "a check could not run, so the job stays held",
    CHANGED: "the job changed after it was read",
}
# What a retry may land: not provably a duplicate, and nothing forgotten near it.
RETRYABLE = frozenset({NO_COVERING_ROW, RANGE_NOT_CONTAINED, RANGE_NOT_NUMERIC, EVIDENCE_MISSING})

_ROWS_SQL = ("SELECT id, session_id, transcript_range, deleted_at, raw FROM episodes "
             "WHERE session_id = ANY(%s) OR raw->>'session_id' = ANY(%s) ORDER BY id")
_UNREADABLE_HUB = "The hub could not be read, so every job it decides stays held."


def _read(paths: list[Path]) -> list[dict[str, Any]]:
    """Each dead job as read, with ``reason`` set when no hub read can matter."""
    from khipu import outbox
    from khipu.outbox_preferences import _hub_view

    entries: list[dict[str, Any]] = []
    for path in paths:
        entry: dict[str, Any] = {"path": path, "reason": None, "row": None, "retired": False}
        entries.append(entry)
        try:
            entry["snapshot"] = path.read_bytes()
            entry["job"] = outbox._read_job(path)
            entry["view"] = _hub_view(entry["job"]["payload"])
        except (OSError, ValueError, TypeError, RecursionError):
            entry["reason"] = CHECK_FAILED
            continue
        if not outbox.announce_refused(entry["job"]):
            entry["reason"] = DIFFERENT_ERROR
    return entries


def _rows(cur, sessions: set[str], *, lock: bool) -> dict[str, list[tuple]]:
    # forget-fence: a check; a row decides a job's fate and only the covering raw
    # goes on, into the local record forget's sweep reads
    cur.execute(_ROWS_SQL + (" FOR SHARE" if lock else ""), (sorted(sessions), sorted(sessions)))
    found: dict[str, list[tuple]] = {}
    for row in cur.fetchall():
        raw = row[4] if isinstance(row[4], dict) else {}
        for session in {row[1], raw.get("session_id")} & sessions:
            found.setdefault(session, []).append(row)
    return found


def _may_overlap(inner: tuple[int, int] | None, ranges: list[Any]) -> bool:
    """Whether a row with these ranges (its column's, its raw's) can overlap the
    job's, which adjacent ones do not. Not knowing counts as yes."""
    from khipu.capture import _transcript_span

    known = [value for value in ranges if value is not None]
    if inner is None or not known:
        return True
    for value in known:
        span = _transcript_span(value)
        if span is None or (span[0] < inner[1] and inner[0] < span[1]) \
                or span[0] <= inner[0] <= inner[1] <= span[1] or inner[0] <= span[0] <= span[1] <= inner[1]:
            return True
    return False


def _verdict(cur, view: dict[str, Any], rows: list[tuple], withheld: frozenset[int]) -> tuple[str | None, tuple | None]:
    """(why the job holds, None), or (None, the row that retires it)."""
    from khipu.capture import _span_lacks, _transcript_span
    from khipu.tombstones import hub_source_forgotten

    if hub_source_forgotten(cur, view):
        return FORGOTTEN, None
    inner = _transcript_span(view.get("transcript_range"))
    for eid, _session, column_range, deleted_at, raw in rows:
        raw = raw if isinstance(raw, dict) else {}
        if _may_overlap(inner, [column_range, raw.get("transcript_range")]) and (
                deleted_at is not None or int(eid) in withheld or (raw and hub_source_forgotten(cur, raw))):
            return FORGOTTEN, None
    if inner is None:
        return RANGE_NOT_NUMERIC, None
    live = [r for r in rows if r[3] is None and int(r[0]) not in withheld and isinstance(r[4], dict)
            and view.get("harness") and r[4].get("harness") == view["harness"]
            and r[4].get("session_id") == view.get("session_id")]
    if not live:
        return NO_COVERING_ROW, None
    covering = [(r, missing) for r in live if (missing := _span_lacks(r[4], view)) is not None]
    if not covering:
        return RANGE_NOT_CONTAINED, None
    for row, missing in covering:
        if missing == []:
            return None, row
    return EVIDENCE_MISSING, None


def _judge(cur, entries: list[dict[str, Any]], *, lock: bool = False) -> bool:
    """Set each unsettled entry's ``reason`` or ``row``; False when the session
    read itself failed. Every failure holds: a database error aborts the
    transaction, so each later check fails too."""
    from khipu.tombstones import withheld_episode_ids

    pending = [e for e in entries if e["reason"] is None]
    try:
        withheld = withheld_episode_ids()
        sessions = {s for e in pending if isinstance(s := e["view"].get("session_id"), str) and s}
        rows = _rows(cur, sessions, lock=lock) if sessions else {}
    except Exception:  # noqa: BLE001 - a read that can't run holds every job
        for entry in pending:
            entry["reason"] = CHECK_FAILED
        return False
    for entry in pending:
        session = entry["view"].get("session_id")
        try:
            entry["reason"], entry["row"] = _verdict(
                cur, entry["view"], rows.get(session, []) if isinstance(session, str) else [], withheld)
        except Exception:  # noqa: BLE001 - a check that can't run holds the job
            entry["reason"], entry["row"] = CHECK_FAILED, None
    return True


def _judged(entries: list[dict[str, Any]]) -> bool:
    """``_judge`` with no lock on its own connection; a hub it can't reach holds
    every job it would have decided."""
    from khipu.db import connect

    if not any(e["reason"] is None for e in entries):
        return True
    try:
        with connect() as conn, conn.cursor() as cur:
            read = _judge(cur, entries)
            conn.rollback()
            return read
    except Exception:  # noqa: BLE001 - a hub that can't be read holds every job
        for entry in entries:
            if entry["reason"] is None:
                entry["reason"], entry["row"] = CHECK_FAILED, None
        return False


def dry_run() -> dict[str, Any]:
    from khipu import outbox

    entries = _read(outbox.dead_jobs())
    read = _judged(entries)
    held = Counter(e["reason"] for e in entries if e["reason"])
    return {"ok": read, "dry_run": True, "dead_jobs": len(entries),
            "would_retire": sum(1 for e in entries if not e["reason"]), "would_hold": dict(sorted(held.items())),
            **({} if read else {"error": _UNREADABLE_HUB})}


def retryable() -> dict[Path, bytes]:
    """The refused dead jobs a retry may land, with the bytes judged; none when
    the hub could not be read."""
    from khipu import outbox

    entries = _read(outbox.dead_jobs())
    _judged(entries)
    return {e["path"]: e["snapshot"] for e in entries if e["reason"] in RETRYABLE}


def _record(entry: dict[str, Any]) -> dict[str, Any]:
    import json
    from datetime import datetime, timezone

    from khipu.capture import SPAN_KIND
    from khipu.capture_identity import payload_sha256

    eid, raw = int(entry["row"][0]), entry["row"][4]
    return {"payload": raw, "queued_evidence": json.loads(entry["snapshot"]),
            "resolution": {"kind": SPAN_KIND, "episode_id": eid, "original_payload_sha256": payload_sha256(raw),
                           "queued_payload_sha256": payload_sha256(entry["view"]),
                           "resolved_at": datetime.now(timezone.utc).isoformat()}}


def _retire_batch(cur, part: list[dict[str, Any]], record_resolutions) -> str | None:
    """One batch, judged afresh with its sessions' rows share-locked, logged, then
    moved job by job. Why the run must stop, or None."""
    import hashlib

    from khipu import outbox
    from khipu.tombstones import RegistryError

    if not _judge(cur, part, lock=True):
        return _UNREADABLE_HUB
    built = []
    for entry in part:
        if entry["reason"] is not None:
            continue
        try:
            record = _record(entry)
            outbox._validate_resolution(record)
        except (ValueError, TypeError, KeyError, RecursionError):
            entry["reason"] = CHECK_FAILED
            continue
        built.append((entry, record))
    if built:
        try:
            record_resolutions([{"episode_id": record["resolution"]["episode_id"], "where": "dead",
                                 "record": hashlib.sha256(entry["snapshot"]).hexdigest(),
                                 "job": entry["path"].name, "kind": record["resolution"]["kind"]}
                                for entry, record in built])
        except OSError:
            for entry, _ in built:
                entry["reason"], entry["row"] = CHECK_FAILED, None
            return "The owner-actions log could not be written, so the rest stay held."
    for entry, record in built:
        try:
            status = outbox.archive_resolved(entry["path"], entry["snapshot"], record)
        except (OSError, ValueError, TypeError, KeyError, RecursionError, RegistryError):
            status = None
        if status == "resolved":
            entry["retired"] = True
        else:
            entry["reason"] = {"changed": CHANGED, "forgotten": FORGOTTEN}.get(status, CHECK_FAILED)
    return None


def apply(*, record_resolutions) -> dict[str, Any]:
    """Retire every eligible job under the memory lock and a share lock on its
    session's rows, so no forget lands between the check and the record.
    ``record_resolutions`` logs a batch's jobs before any of them moves. What a
    failure stops is left whole in dead/."""
    from khipu import memory_lock, outbox
    from khipu.db import connect

    entries = _read(outbox.dead_jobs())
    candidates = [e for e in entries if e["reason"] is None]
    stopped = None
    try:
        if candidates:
            with connect() as conn, conn.cursor() as cur:
                for start in range(0, len(candidates), BATCH):
                    with memory_lock.shared():
                        stopped = _retire_batch(cur, candidates[start:start + BATCH], record_resolutions)
                        conn.rollback()
                    if stopped:
                        break
    except Exception:  # noqa: BLE001 - what is not retired stays whole in dead/
        stopped = _UNREADABLE_HUB
    for entry in candidates:
        if entry["reason"] is None and not entry["retired"]:
            entry["reason"] = CHECK_FAILED
    retired = sum(1 for e in entries if e["retired"])
    held = Counter(e["reason"] for e in entries if e["reason"])
    # A run that retired some happened, and its Undo must stay reachable.
    return {"ok": stopped is None or retired > 0, "dry_run": False, "dead_jobs": len(entries),
            "retired": retired, "held": dict(sorted(held.items())), **({"error": stopped} if stopped else {})}


def undo(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Put back each job a retire moved; a record forgotten since, or a job
    already in its place, stays and is counted."""
    from khipu import outbox
    from khipu.capture import SPAN_KIND

    restored = 0
    left = 0
    for entry in entries:
        try:
            status = outbox.unresolve(str(entry["record"]), outbox.dead_dir() / Path(str(entry["job"])).name, SPAN_KIND)
        except (OSError, ValueError, TypeError, KeyError):
            status = None
        restored += status == "restored"
        left += status not in ("restored", "absent")
    return {"ok": True, "restored": restored, "left_resolved": left}


def words(out: dict[str, Any]) -> str:
    """The result in plain words: counts only, never a job's text."""
    if "dead_jobs" not in out:
        return str(out.get("error") or "Nothing was changed.")
    jobs = f"{out['dead_jobs']} dead job{'' if out['dead_jobs'] == 1 else 's'}."
    if out["dry_run"]:
        held = out["would_hold"]
        lines = ["Dry run: nothing was changed.",
                 f"{jobs} Would retire {out['would_retire']}: another episode in the session "
                 "already holds their range and evidence.",
                 f"Would hold {sum(held.values())}."]
    else:
        held = out["held"]
        lines = [f"{jobs} Retired {out['retired']}, each kept whole in the outbox's resolved "
                 "folder with its record. Nothing was written to the hub.",
                 f"Held {sum(held.values())}."]
    lines += [f"  {count}: {HOLD_WORDS.get(reason, reason)}" for reason, count in held.items()]
    if out.get("error"):
        lines.append(out["error"])
    if out["dry_run"] and out["would_retire"]:
        lines.append("To retire them: `khipu outbox span-covered --apply` (the owner's: it asks for a typed yes).")
    if out.get("owner_action"):
        lines.append(f"Undo: `khipu owner-direction undo {out['owner_action']}`.")
    return "\n".join(lines)
