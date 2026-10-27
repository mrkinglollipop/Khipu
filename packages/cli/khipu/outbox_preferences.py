"""``khipu outbox merge-line-preferences``: preference text that survives only
in memory-file lines, merged into the hub episode at the line's key.

The 2026-10-27 backlog (jobs/2026-10-26/diag-outbox-backlog) is recovery
lines held as ``evidence_conflict`` because the hub row lacks preferences the
line carries; the owner chose to merge that text into the hub rather than
lose it. A line qualifies only when appending its missing preferences to the
row makes it count as landed by ``capture._legacy_landed_line`` (the check
the drain's ``write_pg(legacy_projection=True)`` makes), so its next drain
archives it as resolved. Every other difference is counted and left alone.

Append only: an existing hub preference is never removed, reordered or
rewritten, and a forgotten episode is never touched. Each transaction's
additions (episode ids, exact strings, prior lists) are written to the
owner-actions log before it commits, so ``--undo`` restores the exact prior
preferences. Job files and memory files are never moved or deleted here.
"""
from __future__ import annotations

import json
from typing import Any

ACTION = "outbox.merge_preferences"
LEGACY_REASON = "recovery: the legacy key holds different facts"
BATCH = 50
SAMPLES = 5
_LOOKUP_CHUNK = 500

_LOOKUP_SQL = (
    "SELECT DISTINCT ON (v.i) v.i, e.id, e.deleted_at, e.raw, e.preferences "
    "FROM unnest(%s::text[], %s::text[]) WITH ORDINALITY AS v(ts, md, i) "
    "JOIN episodes e ON e.ts = v.ts::timestamptz AND md5(e.summary) = v.md "
    "ORDER BY v.i, e.deleted_at IS NOT NULL, e.id DESC")
_LOCK_SQL = ("SELECT id, deleted_at, raw, preferences FROM episodes "
             "WHERE id = ANY(%s) ORDER BY id FOR UPDATE")
_UPDATE_SQL = ("UPDATE episodes SET preferences = %s::jsonb, "
               "raw = jsonb_set(coalesce(raw, '{}'::jsonb), '{preferences}', %s::jsonb, true) "
               "WHERE id = %s AND deleted_at IS NULL")


def _str_list(value: Any) -> list[str] | None:
    """A preference list as stored, [] for an absent or empty one, None for
    any other shape (left alone: nothing here guesses at its meaning)."""
    if value is None or value == [] or value == "":
        return []
    if isinstance(value, list) and all(isinstance(p, str) for p in value):
        return list(value)
    return None


def _hub_view(payload: dict[str, Any]) -> dict[str, Any]:
    """The line as ``write_pg`` compares it: origin dropped, secrets redacted."""
    from khipu.capture import _redact_capture_values

    view = json.loads(json.dumps(payload))
    view.pop("origin", None)
    return _redact_capture_values(view)


def held_lines() -> list[dict[str, Any]]:
    """Every recovery line held for a legacy-key conflict, queued or buried.
    Unreadable files are counted by ``outbox status``; they are skipped here."""
    from khipu import outbox

    out: list[dict[str, Any]] = []
    for where, paths in (("outbox", outbox.jobs()), ("dead", outbox.dead_jobs())):
        for path in paths:
            try:
                job = outbox._read_job(path)
            except (OSError, ValueError, TypeError, RecursionError):
                continue
            if job.get("reason") != LEGACY_REASON or "origin" not in job or job["origin"] is not None:
                continue
            out.append({"job": path.name, "where": where, "payload": job["payload"]})
    return out


def classify(raw: Any, view: dict[str, Any]) -> tuple[str, list[str]]:
    """What separates a held line from its hub row's raw, and the preferences
    to append: ``landed`` (nothing), ``mergeable`` (only preferences the row
    lacks, and appending them lands the line), ``preference_order`` (only
    preferences differ, but no append-only change can land it: the row holds
    preferences the line lacks, or in another order), or ``other`` (decisions,
    facts or unknown fields differ; never merged)."""
    from khipu.capture import _legacy_landed_line, _legacy_projection_kind

    if not isinstance(raw, dict):
        return "other", []
    if _legacy_projection_kind(raw, view):
        return "landed", []
    line_prefs = _str_list(view.get("preferences"))
    hub_prefs = _str_list(raw.get("preferences"))
    if not line_prefs or hub_prefs is None:
        return "other", []
    rest = {k: v for k, v in view.items() if k != "preferences"}
    if not _legacy_landed_line(raw, rest):
        return "other", []
    missing = list(dict.fromkeys(p for p in line_prefs if p not in hub_prefs))
    if missing and _legacy_landed_line(dict(raw, preferences=hub_prefs + missing), view):
        return "mergeable", missing
    return "preference_order", []


def _lookup(cur, lines: list[dict[str, Any]]) -> dict[int, tuple]:
    """Each line's hub row by index: the row the drain's legacy check reads
    (live first, newest id), as (id, deleted_at, raw, preferences)."""
    from khipu.capture import _md5_hex

    rows: dict[int, tuple] = {}
    for start in range(0, len(lines), _LOOKUP_CHUNK):
        part = lines[start:start + _LOOKUP_CHUNK]
        cur.execute(_LOOKUP_SQL, ([str(x["payload"].get("ts") or "") for x in part],
                                  [_md5_hex(x["payload"].get("summary") or "") for x in part]))
        for i, eid, deleted_at, raw, prefs in cur.fetchall():
            rows[start + int(i) - 1] = (int(eid), deleted_at, raw, prefs)
    return rows


def _plan(cur) -> tuple[dict[str, int], dict[int, list[dict[str, Any]]]]:
    """Counts by outcome, and the mergeable lines grouped by episode id."""
    from khipu.tombstones import hub_source_forgotten

    lines = held_lines()
    counts = {"held_lines": len(lines), "mergeable": 0, "landed": 0, "preference_order": 0,
              "other": 0, "forgotten": 0, "no_hub_row": 0}
    by_episode: dict[int, list[dict[str, Any]]] = {}
    for i, row in _lookup(cur, lines).items():
        lines[i]["row"] = row
    for line in lines:
        row = line.get("row")
        if row is None:
            counts["no_hub_row"] += 1
            continue
        eid, deleted_at, raw, _prefs = row
        view = _hub_view(line["payload"])
        if deleted_at is not None or hub_source_forgotten(cur, view):
            counts["forgotten"] += 1
            continue
        kind, missing = classify(raw, view)
        counts[kind] += 1
        if kind == "mergeable":
            by_episode.setdefault(eid, []).append(dict(line, view=view, missing=missing))
    return counts, by_episode


def _sample(by_episode: dict[int, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Ids and counts only: preference text never reaches the summary."""
    out = []
    for eid in sorted(by_episode)[:SAMPLES]:
        line = by_episode[eid][0]
        out.append({"episode_id": eid, "job": line["job"], "where": line["where"],
                    "preferences_to_add": len(line["missing"])})
    return out


def dry_run() -> dict[str, Any]:
    from khipu.db import connect

    with connect() as conn, conn.cursor() as cur:
        counts, by_episode = _plan(cur)
        conn.rollback()
    return {"ok": True, "dry_run": True, **counts, "episodes": len(by_episode),
            "preferences_to_add": sum(len(x["missing"]) for lines in by_episode.values() for x in lines),
            "samples": _sample(by_episode),
            "next": ("Merge them with `khipu outbox merge-line-preferences --apply --owner-direction`; "
                     "lines that differ in anything else stay held." if by_episode else
                     "No held line differs from its hub episode only by preferences the hub lacks.")}


def _merge_episode(cur, row: tuple, lines: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The append-only change for one locked row, re-proved against it now:
    what to record and write, or None when nothing qualifies any more."""
    from khipu.tombstones import hub_source_forgotten

    eid, deleted_at, raw, column = row
    raw = raw if isinstance(raw, dict) else None
    hub_prefs = _str_list((raw or {}).get("preferences"))
    col_prefs = _str_list(column)
    if deleted_at is not None or raw is None or hub_prefs is None or col_prefs is None:
        return None
    added: list[str] = []
    for line in lines:
        if hub_source_forgotten(cur, line["view"]):
            continue
        kind, missing = classify(dict(raw, preferences=hub_prefs + added), line["view"])
        if kind == "mergeable":
            added += [p for p in missing if p not in added]
    if not added:
        return None
    return {"episode_id": eid, "added": added,
            "prior_raw": (raw or {}).get("preferences"), "prior_raw_set": "preferences" in raw,
            "prior_column": column,
            "column_added": [p for p in added if p not in col_prefs]}


def apply(*, record) -> dict[str, Any]:
    """Merge every qualifying line, BATCH episodes per transaction, each row
    locked in id order and re-proved under its lock. ``record(entries)``
    writes a transaction's entries to the owner-actions log before it
    commits; when it raises, that transaction is rolled back."""
    from khipu import memory_lock
    from khipu.db import connect

    merged: list[dict[str, Any]] = []
    with connect() as conn:
        with conn.cursor() as cur:
            counts, by_episode = _plan(cur)
            conn.rollback()
            ids = sorted(by_episode)
            for start in range(0, len(ids), BATCH):
                part = ids[start:start + BATCH]
                with memory_lock.shared():
                    try:
                        cur.execute(_LOCK_SQL, (part,))
                        entries = [e for e in (_merge_episode(cur, tuple(r[:4]), by_episode[int(r[0])])
                                               for r in cur.fetchall()) if e]
                        if not entries:
                            conn.rollback()
                            continue
                        record(entries)
                        for e in entries:
                            hub = _str_list(e["prior_raw"]) or []
                            col = _str_list(e["prior_column"]) or []
                            cur.execute(_UPDATE_SQL, (json.dumps(col + e["column_added"], ensure_ascii=False),
                                                      json.dumps(hub + e["added"], ensure_ascii=False),
                                                      e["episode_id"]))
                        conn.commit()
                    except BaseException:
                        conn.rollback()
                        raise
                merged += entries
    return {"ok": True, "applied": True, **counts, "episodes_merged": len(merged),
            "preferences_added": sum(len(e["added"]) for e in merged),
            "episode_ids": [e["episode_id"] for e in merged],
            "next": ("Each merged line now counts as landed: a queued one resolves on its next drain; "
                     "a held one in dead/ resolves after `khipu outbox retry-dead`." if merged else
                     "Nothing qualified; the hub is unchanged.")}


def undo(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Put back the exact prior preferences of every episode a merge changed,
    when the row still holds exactly what the merge left. A row changed
    since (or forgotten) is left alone and named: undo never removes text
    something else wrote."""
    from khipu import memory_lock
    from khipu.db import connect

    latest = {int(e["episode_id"]): e for e in entries}  # one entry per episode per merge
    restored, already, left_alone = 0, 0, []
    ids = sorted(latest)
    with connect() as conn, conn.cursor() as cur:
        for start in range(0, len(ids), BATCH):
            part = ids[start:start + BATCH]
            with memory_lock.shared():
                try:
                    cur.execute(_LOCK_SQL, (part,))
                    rows = {int(r[0]): r for r in cur.fetchall()}
                    for eid in part:
                        e, row = latest[eid], rows.get(eid)
                        if row is None or row[1] is not None or not isinstance(row[2], dict):
                            left_alone.append(eid)
                            continue
                        raw_now, col_now = row[2].get("preferences"), row[3]
                        prior_raw = _str_list(e["prior_raw"]) or []
                        prior_col = _str_list(e["prior_column"]) or []
                        if (_str_list(raw_now) == prior_raw and (_str_list(col_now) or []) == prior_col):
                            already += 1
                            continue
                        if (_str_list(raw_now) != prior_raw + list(e["added"])
                                or (_str_list(col_now) or []) != prior_col + list(e["column_added"])):
                            left_alone.append(eid)
                            continue
                        if e.get("prior_raw_set"):
                            raw_sql = ("raw = jsonb_set(coalesce(raw, '{}'::jsonb), '{preferences}', "
                                       "%s::jsonb, true)")
                            raw_arg = [json.dumps(e["prior_raw"], ensure_ascii=False)]
                        else:
                            raw_sql, raw_arg = "raw = raw - 'preferences'", []
                        cur.execute(f"UPDATE episodes SET preferences = %s::jsonb, {raw_sql} "
                                    "WHERE id = %s AND deleted_at IS NULL",
                                    (None if e["prior_column"] is None
                                     else json.dumps(e["prior_column"], ensure_ascii=False), *raw_arg, eid))
                        restored += 1
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
    return {"ok": True, "restored": restored, "already_restored": already, "left_alone": left_alone}
