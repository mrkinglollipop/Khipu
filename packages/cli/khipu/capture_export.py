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
never interleaves with it.
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


def _read_hub(cur) -> tuple[list[dict[str, Any]], dict[tuple[str, str], int]]:
    """Every hub episode (live and tombstoned) and every source key."""
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
    sources: dict[tuple[str, str], int] = {}
    if sources_ready(cur):
        cur.execute(
            "SELECT episode_id, ts, summary_md5 FROM episode_sources "
            "WHERE ts IS NOT NULL AND summary_md5 IS NOT NULL"
        )
        for episode_id, ts, md in cur.fetchall():
            key = (_norm_ts(ts), md)
            if key[0] is not None:
                sources.setdefault(key, int(episode_id))
    return rows, sources


def _raws(cur, ids: list[int]) -> dict[int, Any]:
    out: dict[int, Any] = {}
    for start in range(0, len(ids), 1000):
        cur.execute("SELECT id, raw FROM episodes WHERE id = ANY(%s)", (ids[start:start + 1000],))
        out.update({int(i): raw for i, raw in cur.fetchall()})
    return out


def _plan(memory_root: Path, cur, gate) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from khipu.drift import is_smoke_row

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
        via_source = False
        if row is None and key in sources:
            row = by_id.get(sources[key])
            via_source = row is not None
        if row is None:
            counts["file_only"] += 1
            continue
        has_line.add(row["id"])
        if row["deleted"]:
            counts["matches_forgotten"] += 1
        elif via_source:
            counts["matched_via_sources"] += 1
        elif _same_content(obj, row):
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

    hidden = set(gate.episode_ids)
    raws = _raws(cur, [r["id"] for r in candidates]) if candidates and not gate.empty else {}
    new: list[dict[str, Any]] = []
    skipped_here = 0
    for row in candidates:
        raw = raws.get(row["id"])
        if row["id"] in hidden or (isinstance(raw, dict) and not gate.allows_stored(raw)):
            skipped_here += 1
            continue
        new.append(line_for(row))
    counts.update(
        hub_live=sum(1 for r in rows if not r["deleted"]),
        would_add=len(new),
        skipped_empty_summary=skipped_empty,
        skipped_smoke_rows=skipped_smoke,
        skipped_forgotten_here=skipped_here,
    )
    return counts, new


def _append(path: Path, lines: list[dict[str, Any]]) -> int:
    """Every line in one append. Bytes already in the file are never touched;
    a file whose last line lacks its newline gets one first, so the new first
    line is not glued onto it."""
    blob = "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines).encode("utf-8")
    try:
        with path.open("rb") as fh:
            if fh.seek(0, os.SEEK_END) > 0:
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    blob = b"\n" + blob
    except FileNotFoundError:
        pass
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

    def run(gate) -> dict[str, Any]:
        with connect() as conn:
            try:
                conn.read_only = True
            except Exception:  # noqa: BLE001 - a driver without the flag still only reads here
                pass
            with conn.cursor() as cur:
                counts, new = _plan(memory_root, cur, gate)
            conn.rollback()
        written = 0 if dry_run or not new else _append(target, new)
        return {"ok": True, "dry_run": dry_run, "target": CURRENT_FILE, "added": written, **counts}

    if dry_run:
        out = run(tombstones.read_gate())
    else:
        with tombstones.write_gate() as gate:
            out = run(gate)
    _log(("dry run: " if dry_run else "") + json.dumps({k: v for k, v in out.items() if k != "target"},
                                                         sort_keys=True))
    return out
