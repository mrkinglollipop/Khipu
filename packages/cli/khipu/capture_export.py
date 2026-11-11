"""Append-only export of hub episodes to the session files (row 11).

In hub mode a capture writes only the hub row; capture_v2 no longer runs, so
nothing writes the capture's line into ``episodes.jsonl`` at capture time.
Once a night (``jobs._run_nightly_locked``, before the outside consolidate
script so its git sync carries the lines offsite the same night) this adds
one line for every live hub episode that has no line yet. By hand:
``khipu capture export [--dry-run] [--json]``.

What it never does:
  - rewrite, reorder or delete an existing line. A line whose content differs
    from its hub row (the old preference pass, raw topic slugs, scope strings
    the hub drops) stays exactly as it is; it is only counted;
  - write a forgotten episode: a row the hub tombstoned, or one this Mac's
    forget list holds while the hub row is still live;
  - write anything that is not a captured episode (library rows live in their
    own tables; the 2020 smoke row is skipped).

"Has a line" is the key every file reader of the hub has always used:
``(ts, md5(summary))``, plus ``episode_sources`` for a row a merge or an edit
rewrote, across every episodes file in the folder (``drift.episode_files``:
``episodes.jsonl`` and the rotated ``episodes_<YYYY>.jsonl``).

The line has the shape the folder's writer (capture_v2 through the Memory
repo's ``capture.py``) gives it and its readers (``consolidate_nightly.py``,
``nightly_topic_amend.py``) read: ``ts``, ``summary``, ``topics``, ``people``,
``decisions``, ``preferences``, then ``scope`` and ``session_id`` when the hub
holds them. Fields the hub does not hold are left out.

All new lines go to the file in one append, after the whole list is built, so
a run that stops early leaves the file as it was, and a second run adds
nothing. A real run holds the forget write gate (the per-Mac memory lock,
shared, and the registry lock) from its reads through its append, so a forget
on this Mac never interleaves with it. That gate cannot cover another Mac:
immediately before the append the run reads the hub's tombstones for the rows
it is about to write once more and drops any forgotten since its first read
(no lock across Macs; the window left is that last query to the append).

A run that stopped inside its append can leave a last line with no newline.
Those bytes are an unfinished write, not a line: before appending, under the
gate, a real run cuts the file back to just after its last newline
(``repaired_torn_tail``). The one exception is a last line that is whole JSON
and whole UTF-8 (the file's last line written without its newline): it is
kept and given its newline. Complete lines are never changed. Nothing else
writes this file while the gate is held: capture_v2 (legacy and dual modes)
holds the same registry lock for its whole run, and a forget's rewrite holds
the memory lock exclusively.

``--dry-run`` changes neither the Memory files nor the hub, and opens the
hub without writing this Mac's hub-reachability memo.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

CURRENT_FILE = "episodes.jsonl"

_LIST_FIELDS = ("topics", "people", "decisions", "preferences")


def _log(msg: str) -> None:
    print(f"[khipu-capture-export] {msg}", file=sys.stderr)


def _md5(text: str) -> str:
    return hashlib.md5((text or "").encode("utf-8")).hexdigest()


def _norm_ts(value: Any) -> str | None:
    """One instant as a fixed UTC string, so a file's ``...Z``, ``+00:00`` or
    fractional form and the hub's timestamptz compare equal, as the hub's own
    ``::timestamptz`` cast does."""
    if isinstance(value, datetime):
        d = value
    else:
        try:
            d = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")


def _line_ts(d: datetime) -> str:
    """The ``ts`` a new line carries: UTC with ``Z``, seconds precision when
    the instant has no fraction (the form ``khipu capture`` mints)."""
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    d = d.astimezone(timezone.utc)
    if d.microsecond:
        return d.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def _items(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [x if isinstance(x, str) else json.dumps(x, ensure_ascii=False, sort_keys=True)
            for x in value]


def line_for(row: dict[str, Any]) -> dict[str, Any]:
    """The file line for one hub episode row."""
    line: dict[str, Any] = {"ts": _line_ts(row["ts"]), "summary": row["summary"]}
    for field in _LIST_FIELDS:
        line[field] = _items(row.get(field))
    if row.get("scope"):
        line["scope"] = row["scope"]
    if row.get("session_id"):
        line["session_id"] = row["session_id"]
    return line


def _same_content(obj: dict[str, Any], row: dict[str, Any]) -> bool:
    """Whether a file line already says what the hub row says, field by field
    (the key already matched ``ts`` and ``summary``)."""
    for field in _LIST_FIELDS:
        if [str(x) for x in (obj.get(field) or [])] != _items(row.get(field)):
            return False
    if (obj.get("scope") or None) != (row.get("scope") or None):
        return False
    return (obj.get("session_id") or None) == (row.get("session_id") or None)


def _file_lines(memory_root: Path) -> Iterator[tuple[str, dict[str, Any] | None]]:
    """(file name, parsed line or None when unparseable) for every non-blank
    line of every episodes file. Reads only."""
    from khipu.drift import episode_files
    from khipu.lines import split_lines

    for path in episode_files(memory_root):
        text = path.read_text(encoding="utf-8", errors="replace")
        for raw in split_lines(text):
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                yield path.name, None
                continue
            yield path.name, obj if isinstance(obj, dict) else None


def _read_hub(cur) -> tuple[list[dict[str, Any]], dict[tuple[str, str], list[int]]]:
    """Every hub episode (live and tombstoned) and every source key, with
    every episode that holds it: the schema does not make a historical
    ``(ts, md5(summary))`` unique, so two edited episodes can share one."""
    # forget-fence source: _plan fences the rows it writes (tombstones.withhold)
    from khipu.capture_identity import sources_ready

    cur.execute(
        "SELECT id, ts, summary, session_id, topics, people, decisions, preferences, scope, "
        "deleted_at IS NOT NULL FROM episodes ORDER BY ts, id"
    )
    rows = [
        {"id": int(r[0]), "ts": r[1], "summary": r[2] or "", "session_id": r[3],
         "topics": r[4], "people": r[5], "decisions": r[6], "preferences": r[7],
         "scope": r[8], "deleted": bool(r[9])}
        for r in cur.fetchall()
    ]
    sources: dict[tuple[str, str], list[int]] = {}
    if sources_ready(cur):
        cur.execute(
            "SELECT episode_id, ts, summary_md5 FROM episode_sources "
            "WHERE ts IS NOT NULL AND summary_md5 IS NOT NULL"
        )
        for episode_id, ts, md in cur.fetchall():
            key = (_norm_ts(ts), md)
            if key[0] is not None:
                ids = sources.setdefault(key, [])
                if int(episode_id) not in ids:
                    ids.append(int(episode_id))
    return rows, sources


def _raws(cur, ids: list[int]) -> dict[int, Any]:
    # forget-fence source: _plan fences the rows it writes (tombstones.withhold)
    out: dict[int, Any] = {}
    for start in range(0, len(ids), 1000):
        cur.execute("SELECT id, raw FROM episodes WHERE id = ANY(%s)", (ids[start:start + 1000],))
        out.update({int(i): raw for i, raw in cur.fetchall()})
    return out


def _tombstoned(cur, ids: list[int]) -> set[int]:
    """Which of ``ids`` the hub has tombstoned now."""
    # forget-fence: returns the ids the export refuses to write; no text
    out: set[int] = set()
    for start in range(0, len(ids), 1000):
        cur.execute("SELECT id FROM episodes WHERE id = ANY(%s) AND deleted_at IS NOT NULL",
                    (ids[start:start + 1000],))
        out.update(int(r[0]) for r in cur.fetchall())
    return out


def _plan(memory_root: Path, cur, gate) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The counts and the hub rows (live, fenced, with no line) to write."""
    from khipu.drift import is_smoke_row
    from khipu.tombstones import withheld_episode_ids, withhold

    rows, sources = _read_hub(cur)
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    by_id: dict[int, dict[str, Any]] = {}
    for row in rows:
        by_id[row["id"]] = row
        by_key.setdefault((_norm_ts(row["ts"]), _md5(row["summary"])), row)

    counts = {
        "lines": 0, "unparseable": 0, "no_ts": 0, "smoke": 0,
        "present_identical": 0, "present_different": 0, "matched_via_sources": 0,
        "matches_forgotten": 0, "file_only": 0,
    }
    has_line: set[int] = set()
    for _name, obj in _file_lines(memory_root):
        counts["lines"] += 1
        if obj is None:
            counts["unparseable"] += 1
            continue
        if not obj.get("ts"):
            counts["no_ts"] += 1
            continue
        if is_smoke_row(obj):
            counts["smoke"] += 1
            continue
        key = (_norm_ts(obj["ts"]), _md5(str(obj.get("summary") or "")))
        row = by_key.get(key) if key[0] is not None else None
        matched = [row] if row is not None else []
        via_source = False
        if row is None and key in sources:
            # A line with a historical key is the line of every episode
            # that holds that key.
            matched = [by_id[i] for i in sources[key] if i in by_id]
            via_source = bool(matched)
        if not matched:
            counts["file_only"] += 1
            continue
        has_line.update(r["id"] for r in matched)
        if all(r["deleted"] for r in matched):
            counts["matches_forgotten"] += 1
        elif via_source:
            counts["matched_via_sources"] += 1
        elif _same_content(obj, matched[0]):
            counts["present_identical"] += 1
        else:
            counts["present_different"] += 1

    candidates = []
    skipped_empty = skipped_smoke = 0
    for row in rows:
        if row["deleted"] or row["id"] in has_line:
            continue
        if is_smoke_row({"ts": _line_ts(row["ts"]), "summary": row["summary"]}):
            skipped_smoke += 1
            continue
        if not row["summary"].strip():
            # The folder's writer never took an empty summary (capture_v2
            # refuses it), so its readers have never seen one.
            skipped_empty += 1
            continue
        candidates.append(row)

    # This Mac's forget list and the replica's queued erasures; raises when
    # either cannot be read, so nothing is written then.
    hidden = set(gate.episode_ids) | set(withheld_episode_ids())
    raws = _raws(cur, [r["id"] for r in candidates]) if candidates and not gate.empty else {}
    allowed = []
    for row in candidates:
        raw = raws.get(row["id"])
        if row["id"] in hidden or (isinstance(raw, dict) and not gate.allows_stored(raw)):
            continue
        allowed.append(row)
    # The one forget fence, as every other output applies it.
    new = withhold(allowed, episode_key="id")
    skipped_here = len(candidates) - len(new)
    counts.update(
        hub_live=sum(1 for r in rows if not r["deleted"]),
        would_add=len(new),
        skipped_empty_summary=skipped_empty,
        skipped_smoke_rows=skipped_smoke,
        skipped_forgotten_here=skipped_here,
    )
    return counts, new


_TAIL_CHUNK = 1 << 16


def _tail(path: Path) -> tuple[str, int]:
    """What the end of ``path`` is: ``("clean", size)`` when empty, missing
    or ending in a newline; ``("unterminated", size)`` when its last line is
    whole JSON and whole UTF-8 but lacks the newline; ``("torn", keep)``
    otherwise, ``keep`` being the length up to and including the last
    newline (0 when there is none). Reads only."""
    try:
        fh = path.open("rb")
    except FileNotFoundError:
        return "clean", 0
    with fh:
        size = fh.seek(0, os.SEEK_END)
        if size == 0:
            return "clean", 0
        fh.seek(size - 1)
        if fh.read(1) == b"\n":
            return "clean", size
        end, tail = size, b""
        while end > 0:
            start = max(0, end - _TAIL_CHUNK)
            fh.seek(start)
            tail = fh.read(end - start) + tail
            end = start
            cut = tail.rfind(b"\n")
            if cut >= 0:
                keep = start + cut + 1
                last = tail[cut + 1:]
                break
        else:
            keep, last = 0, tail
    try:
        obj = json.loads(last.decode("utf-8"))
    except ValueError:  # UnicodeDecodeError is a ValueError
        return "torn", keep
    return ("unterminated", size) if isinstance(obj, dict) else ("torn", keep)


def _repair_tail(path: Path, state: tuple[str, int]) -> None:
    """Cut a torn tail back to just after the last newline. Run under the
    gate, so no writer this Mac runs is mid-write."""
    kind, keep = state
    if kind != "torn":
        return
    fd = os.open(path, os.O_WRONLY)
    try:
        os.ftruncate(fd, keep)
        os.fsync(fd)
    finally:
        os.close(fd)


def _append(path: Path, lines: list[dict[str, Any]], unterminated: bool = False) -> int:
    """Every line in one append. Bytes already in the file are never touched;
    an ``unterminated`` whole last line gets its newline first, so the new
    first line is not glued onto it."""
    blob = "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines).encode("utf-8")
    if unterminated:
        blob = b"\n" + blob
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        view = memoryview(blob)
        while view:
            n = os.write(fd, view)
            view = view[n:]
        os.fsync(fd)
    finally:
        os.close(fd)
    return len(lines)


def export_new_lines(memory_root: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Append one line to ``memory_root/episodes.jsonl`` for every live hub
    episode with no line in any episodes file. ``dry_run`` writes nothing and
    returns the same counts. Raises when the hub or this Mac's forget list
    cannot be read (nothing is written then)."""
    from khipu import tombstones
    from khipu.db import connect

    memory_root = Path(memory_root)
    if not memory_root.is_dir():
        return {"ok": False, "dry_run": dry_run, "error": f"memory root missing: {memory_root}"}
    target = memory_root / CURRENT_FILE

    def hub():
        # A dry run writes no hub-reachability memo, creates no data
        # directory and runs no local recovery.
        conn = connect(record_reachability=False) if dry_run else connect()
        try:
            conn.read_only = True
        except Exception:  # noqa: BLE001 - a driver without the flag still only reads here
            pass
        return conn

    def run(gate) -> dict[str, Any]:
        with hub() as conn:
            with conn.cursor() as cur:
                counts, rows = _plan(memory_root, cur, gate)
            conn.rollback()
        dropped = 0
        if rows:
            # Another Mac's forget may have committed since that read: its
            # tombstones, read again right before the append.
            with hub() as conn:
                with conn.cursor() as cur:
                    gone = _tombstoned(cur, [r["id"] for r in rows])
                conn.rollback()
            dropped = sum(1 for r in rows if r["id"] in gone)
            rows = [r for r in rows if r["id"] not in gone]
            counts["would_add"] = len(rows)
        tail = _tail(target)
        torn = int(tail[0] == "torn")
        written = repaired = 0
        if not dry_run:
            _repair_tail(target, tail)
            repaired = torn
            if rows:
                written = _append(target, [line_for(r) for r in rows],
                                  unterminated=tail[0] == "unterminated")
        return {"ok": True, "dry_run": dry_run, "target": CURRENT_FILE, "added": written,
                **counts, "dropped_forgotten_since_read": dropped,
                "torn_tail": torn, "repaired_torn_tail": repaired}

    if dry_run:
        out = run(tombstones.read_gate())
    else:
        with tombstones.write_gate() as gate:
            out = run(gate)
    _log(("dry run: " if dry_run else "") + json.dumps({k: v for k, v in out.items() if k != "target"},
                                                         sort_keys=True))
    return out
