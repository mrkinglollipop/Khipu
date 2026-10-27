"""``khipu outbox merge-line-preferences``: preference text that survives only
in memory-file lines, merged into the hub episode at the line's key.

The 2026-10-27 backlog (jobs/2026-10-26/diag-outbox-backlog) is recovery
lines held as ``evidence_conflict`` because the hub row lacks preferences the
line carries; the owner chose to merge that text into the hub rather than
lose it. A line qualifies only when every field but its preferences already
matches the row (``capture._legacy_landed_line``). Its missing preferences
are appended, one running list per episode, so several lines for one episode
all count. Afterwards a line that matches the row exactly lands on its next
drain (the drain's exact check is unchanged); one whose every preference is
now on the row but in another order, or beside preferences it lacks, is
archived to resolved/ here as ``preferences_contained``
(``outbox.archive_resolved``: the record keeps the whole job, and forget's
sweep reads that folder). A line that differs only because the hub's writer
reshaped it (K6 scope cleanup, W5.1 topics moved to tags, preferences split
at other commas) is compared as the writer would have stored it, line side
only, and archived as ``preferences_contained_normalized`` once the row
holds it. Every other difference is counted as ``needs_review``, its
episode ids listed, and left alone.

Append only: an existing hub preference is never removed, reordered or
rewritten, and a forgotten episode (deleted, forgotten by id on this Mac, or
by source) is never touched. Each transaction's additions (episode ids,
exact strings, prior lists) and each job to resolve are written to the
owner-actions log before they happen, so ``--undo`` restores the exact prior
preferences and puts resolved jobs back. A job file is only ever moved into
resolved/, never deleted; memory files are never touched.
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
    """Every recovery line held for a legacy-key conflict, queued or buried,
    with the bytes it was read from. Unreadable files are counted by
    ``outbox status``; they are skipped here."""
    from khipu import outbox

    out: list[dict[str, Any]] = []
    for where, paths in (("outbox", outbox.jobs()), ("dead", outbox.dead_jobs())):
        for path in paths:
            try:
                snapshot = path.read_bytes()
                job = outbox._read_job(path)
            except (OSError, ValueError, TypeError, RecursionError):
                continue
            if job.get("reason") != LEGACY_REASON or "origin" not in job or job["origin"] is not None:
                continue
            out.append({"job": path.name, "where": where, "path": path, "snapshot": snapshot,
                        "payload": job["payload"]})
    return out


def classify(raw: Any, view: dict[str, Any]) -> tuple[str, list[str]]:
    """What separates a held line from its hub row's raw, and the preferences
    to append: ``landed`` (nothing), ``mergeable`` (only preferences the row
    lacks, and appending them lands the line), ``preference_order`` (only
    preferences differ, and no append lands it exactly: the row holds
    preferences the line lacks, or in another order; appending the missing
    ones leaves every line preference on the row), or ``other`` (decisions,
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
    return "preference_order", missing


NORMALIZED_KIND = "preferences_contained_normalized"


def _writer_projection(raw: dict[str, Any], view: dict[str, Any]) -> dict[str, Any] | None:
    """The line as ``write_pg`` would have stored it, line side only: scope
    through ``identity.normalize_scope`` (K6) and topics through the W5.1
    noise split (``hygiene.is_noise_slug``, which needs no database), noise
    topics removed from the line once every one of them is in the row's
    tags. None when the row can't hold what the writer would have written: a
    line scope K6 drops while the row keeps a non-null scope, or a noise
    topic (or line tag) the row's tags lack. Topic-alias folding needs the
    database, so a topic it renamed is not reproduced and stays a difference."""
    from khipu.hygiene import is_noise_slug
    from khipu.identity import normalize_scope

    out = dict(view)
    scope = view.get("scope")
    if isinstance(scope, str):
        kept = normalize_scope(scope)
        if kept is not None:
            out["scope"] = kept
        elif scope.strip() and raw.get("scope") is not None:
            return None
        else:
            out.pop("scope")
    topics = view.get("topics")
    if isinstance(topics, list) and topics:
        if not all(isinstance(t, str) for t in topics):
            return None
        split: dict[bool, list[str]] = {True: [], False: []}
        for t in topics:
            s = t.strip().lower()
            if s and s not in split[is_noise_slug(s)]:
                split[is_noise_slug(s)].append(s)
        out["topics"] = split[False]
        if split[True]:
            line_tags = view.get("tags") or []
            hub_tags = raw.get("tags")
            if not (isinstance(line_tags, list) and isinstance(hub_tags, list)
                    and all(isinstance(t, str) for t in [*line_tags, *hub_tags])
                    and set(line_tags) | set(split[True]) <= set(hub_tags)):
                return None
            out.pop("tags", None)
    return out


PROJECTED_FIELDS = ("scope", "topics", "tags")


def projection_of(projected: dict[str, Any]) -> dict[str, Any]:
    """The fields ``_writer_projection`` reshapes, as matched: kept in the
    resolved/ record so a later change to the noise list or the scope limit
    never decides whether an old record still proves its match."""
    return {k: projected[k] for k in PROJECTED_FIELDS if k in projected}


def _stored_projection(raw: dict[str, Any], view: dict[str, Any], stored: Any) -> dict[str, Any] | None:
    """The line with a record's stored projection applied, after the checks
    that need no noise list or scope limit: every kept topic is one of the
    line's (stripped, lowercased), the topics it moved and the line's tags are
    all in the row's tags, and a line scope it dropped meets a null row scope."""
    if not isinstance(stored, dict) or not set(stored) <= set(PROJECTED_FIELDS):
        return None
    out = {k: v for k, v in view.items() if k not in PROJECTED_FIELDS}
    out.update(stored)
    scope = view.get("scope")
    if "scope" in stored:
        if (not (isinstance(stored["scope"], str) and stored["scope"]) if isinstance(scope, str)
                else stored["scope"] != scope):
            return None
    elif isinstance(scope, str) and scope.strip() and raw.get("scope") is not None:
        return None
    topics = view.get("topics")
    if isinstance(topics, list) and topics:
        if not all(isinstance(t, str) for t in topics):
            return None
        kept = stored.get("topics")
        line_topics = {t.strip().lower() for t in topics} - {""}
        if not (isinstance(kept, list) and all(isinstance(t, str) for t in kept) and set(kept) <= line_topics):
            return None
        moved = line_topics - set(kept)
        if moved:
            line_tags, hub_tags = view.get("tags") or [], raw.get("tags")
            if not ("tags" not in stored and isinstance(line_tags, list) and isinstance(hub_tags, list)
                    and all(isinstance(t, str) for t in [*line_tags, *hub_tags])
                    and set(line_tags) | moved <= set(hub_tags)):
                return None
        elif stored.get("tags") != view.get("tags"):
            return None
    elif "topics" in stored and stored["topics"] != topics:
        return None
    return out


def _contiguous(run: list[str], within: list[str]) -> bool:
    n = len(run)
    return any(within[i:i + n] == run for i in range(len(within) - n + 1))


def normalized_missing(raw: Any, view: dict[str, Any], projection: Any = None) -> list[str] | None:
    """The line's preferences the row lacks once the line is projected the
    way the hub's writer reshapes it (``_writer_projection``, or a record's
    stored ``projection``), each preference held only when its comma pieces
    are a contiguous, in-order run of the row's comma-joined preferences;
    None when anything but preferences still differs. ``[]``: the row holds
    the whole line, which this command archives as
    ``preferences_contained_normalized`` (the drain's exact check is
    unchanged and never lands such a line)."""
    from khipu.capture import _legacy_comma_join, _legacy_landed_line

    if not isinstance(raw, dict):
        return None
    if projection is None:
        projected = _writer_projection(raw, view)
    else:
        projected = _stored_projection(raw, view, projection)
    if projected is None:
        return None
    line_prefs, hub_prefs = _str_list(projected.get("preferences")), _str_list(raw.get("preferences"))
    if line_prefs is None or hub_prefs is None:
        return None
    if not _legacy_landed_line(raw, {k: v for k, v in projected.items() if k != "preferences"}):
        return None
    pieces = _legacy_comma_join(hub_prefs)
    return list(dict.fromkeys(p for p in line_prefs
                              if not _contiguous(_legacy_comma_join([p]), pieces)))


def _forgotten(cur, eid: int, view: dict[str, Any]) -> bool:
    """The fences every reader and writer uses: forgotten on this Mac by id
    (``episode_hidden``: a backup's older raw no source identity matches) or
    by source, here or in the hub (``hub_source_forgotten``)."""
    from khipu.tombstones import episode_hidden, hub_source_forgotten

    return episode_hidden(eid) or hub_source_forgotten(cur, view)


def _episode_plan(row: tuple, lines: list[dict[str, Any]]) -> tuple[list[str], list[str]] | None:
    """One hub row's held lines against the row as the merge leaves it: each
    line's outcome (``landed`` as it stands, ``mergeable`` it lands once the
    additions are made, ``contained`` resolved here as
    ``preferences_contained``, ``other``) and the preferences to append, in
    one running list. A line no exact rule matches is tried once more
    through ``normalized_missing``: ``normalized`` when the merged row holds
    it after the writer's projection, resolved here as
    ``preferences_contained_normalized``. None when apply would skip the row
    for its shape (a raw that is not an object, or a preference list in
    either place that is not a list of strings)."""
    from khipu.capture import _legacy_preferences_contained, _legacy_projection_kind

    _eid, _deleted_at, raw, column = row
    if not isinstance(raw, dict):
        return None
    hub_prefs, col_prefs = _str_list(raw.get("preferences")), _str_list(column)
    if hub_prefs is None or col_prefs is None:
        return None
    added: list[str] = []
    before: list[str] = []
    for line in lines:
        now = dict(raw, preferences=hub_prefs + added)
        kind, missing = classify(now, line["view"])
        before.append(classify(raw, line["view"])[0])
        if kind in ("preference_order", "other"):
            # No append lands this line exactly, so add only what the
            # comma-joined comparison finds missing: a preference split at
            # other commas is already on the row and is never appended again.
            normalized = normalized_missing(now, line["view"])
            if normalized is not None:
                missing = normalized
            elif kind == "other":
                missing = []
        added += [p for p in missing if p not in added]
    final = dict(raw, preferences=hub_prefs + added) if added else raw
    kinds = []
    for line, was in zip(lines, before):
        if _legacy_projection_kind(final, line["view"]):
            kinds.append("landed" if was == "landed" else "mergeable")
        elif _legacy_preferences_contained(final, line["view"]):
            kinds.append("contained")
        elif normalized_missing(final, line["view"]) == []:
            kinds.append("normalized")
        else:
            kinds.append("other")
    return kinds, added


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


_COUNTS = ("mergeable", "landed", "preferences_contained", NORMALIZED_KIND, "needs_review", "forgotten",
           "no_hub_row", "column_shape")
_COUNT_OF = {"contained": "preferences_contained", "normalized": NORMALIZED_KIND, "other": "needs_review"}


def _plan(cur) -> tuple[dict[str, Any], dict[int, list[dict[str, Any]]], dict[int, list[str]]]:
    """Counts by outcome (with the episode ids of lines left for the owner's
    review: a real difference no projection explains), the held lines
    grouped by the episode they belong to (only episodes with something to
    do), and each one's additions."""
    lines = held_lines()
    counts: dict[str, Any] = {"held_lines": len(lines), **{k: 0 for k in _COUNTS}}
    review: set[int] = set()
    grouped: dict[int, list[dict[str, Any]]] = {}
    rows: dict[int, tuple] = {}
    for i, row in _lookup(cur, lines).items():
        lines[i]["row"] = row
    for line in lines:
        row = line.get("row")
        if row is None:
            counts["no_hub_row"] += 1
            continue
        view = _hub_view(line["payload"])
        if row[1] is not None or _forgotten(cur, row[0], view):
            counts["forgotten"] += 1
            continue
        rows[row[0]] = row
        grouped.setdefault(row[0], []).append(dict(line, view=view))
    by_episode: dict[int, list[dict[str, Any]]] = {}
    additions: dict[int, list[str]] = {}
    for eid, group in grouped.items():
        planned = _episode_plan(rows[eid], group)
        if planned is None:
            counts["column_shape"] += len(group)
            continue
        kinds, added = planned
        for kind in kinds:
            counts[_COUNT_OF.get(kind, kind)] += 1
        if "other" in kinds:
            review.add(eid)
        if added or "contained" in kinds or "normalized" in kinds:
            by_episode[eid], additions[eid] = group, added
    counts["needs_review_episode_ids"] = sorted(review)
    return counts, by_episode, additions


def _sample(by_episode: dict[int, list[dict[str, Any]]], additions: dict[int, list[str]]) -> list[dict[str, Any]]:
    """Ids and counts only: preference text never reaches the summary."""
    out = []
    for eid in sorted(by_episode)[:SAMPLES]:
        line = by_episode[eid][0]
        out.append({"episode_id": eid, "job": line["job"], "where": line["where"],
                    "lines": len(by_episode[eid]), "preferences_to_add": len(additions[eid])})
    return out


def dry_run() -> dict[str, Any]:
    from khipu.db import connect

    with connect() as conn, conn.cursor() as cur:
        counts, by_episode, additions = _plan(cur)
        conn.rollback()
    return {"ok": True, "dry_run": True, **counts,
            "episodes": sum(1 for eid in by_episode if additions[eid]),
            "preferences_to_add": sum(len(a) for a in additions.values()),
            "samples": _sample(by_episode, additions),
            "next": ("Merge them with `khipu outbox merge-line-preferences --apply --owner-direction`; "
                     "lines that differ in anything else stay held (needs_review_episode_ids)." if by_episode else
                     "No held line differs from its hub episode only by preferences the hub lacks.")}


def _merge_episode(cur, row: tuple, lines: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list]:
    """The append-only change for one locked row, re-proved against it now:
    what to record and write (None when nothing is to be added any more), and
    the lines to resolve (``preferences_contained`` or
    ``preferences_contained_normalized``) once it commits, each with the
    row's raw as the merge leaves it."""
    eid, deleted_at, raw, column = row
    if deleted_at is not None:
        return None, []
    live = [line for line in lines if not _forgotten(cur, eid, line["view"])]
    planned = _episode_plan(row, live) if live else None
    if planned is None:
        return None, []
    kinds, added = planned
    hub_prefs, col_prefs = _str_list(raw.get("preferences")), _str_list(column)
    final = dict(raw, preferences=hub_prefs + added) if added else raw
    resolve = [(line, final) for line, kind in zip(live, kinds) if kind in ("contained", "normalized")]
    if not added:
        return None, resolve
    identities = sorted({tag for line in live for tag in _identity_tags(line["view"])})
    return ({"episode_id": eid, "added": added,
             "prior_raw": raw.get("preferences"), "prior_raw_set": "preferences" in raw,
             "prior_column": column,
             "column_added": [p for p in added if p not in col_prefs],
             "identities": identities}, resolve)


def _identity_tags(view: dict[str, Any]) -> set[str]:
    """The registry identities a merged line is known by (hashes, never
    text), kept so Undo re-checks the same source fence apply did."""
    from khipu.tombstones import identity_strings

    return identity_strings(view)


def _resolution(line: dict[str, Any], final_raw: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    """The resolved/ record for one held line, built from the bytes it was
    read from (the whole queued job, so a scope or topic the writer reshaped
    is kept verbatim), and its record id; None when those bytes no longer
    prove it."""
    import hashlib
    from datetime import datetime, timezone

    from khipu.capture import _legacy_preferences_contained
    from khipu.capture_identity import payload_sha256

    queued = json.loads(line["snapshot"])
    view = _hub_view(queued["payload"])
    extra: dict[str, Any] = {}
    if _legacy_preferences_contained(final_raw, view):
        kind = "preferences_contained"
    elif normalized_missing(final_raw, view) == []:
        kind = NORMALIZED_KIND
        extra["normalized_projection"] = projection_of(_writer_projection(final_raw, view))
    else:
        return None
    record = {"payload": final_raw, "queued_evidence": queued,
              "resolution": {"kind": kind, "episode_id": int(line["row"][0]),
                             "original_payload_sha256": payload_sha256(final_raw),
                             "queued_payload_sha256": payload_sha256(view),
                             "resolved_at": datetime.now(timezone.utc).isoformat(), **extra}}
    return record, hashlib.sha256(line["snapshot"]).hexdigest()


def _resolve(resolve: list, record_resolutions) -> int:
    """Archive each contained line (logged first), after its row's merge
    committed. A line whose file changed, or whose source is forgotten, is
    left where it is."""
    from khipu import outbox

    built = [(line, *made) for line, final in resolve if (made := _resolution(line, final))]
    if not built:
        return 0
    record_resolutions([{"episode_id": int(line["row"][0]), "record": record_id, "job": line["job"],
                         "where": line["where"], "kind": record["resolution"]["kind"]}
                        for line, record, record_id in built])
    return sum(outbox.archive_resolved(line["path"], line["snapshot"], record) == "resolved"
               for line, record, _record_id in built)


def apply(*, record, record_resolutions, record_committed=None) -> dict[str, Any]:
    """Merge every qualifying line, BATCH episodes per transaction, each row
    locked in id order and re-proved under its lock. ``record(entries)``
    writes a transaction's entries to the owner-actions log before it
    commits; when it raises, that transaction is rolled back.
    ``record_committed(episode_ids)`` marks them committed once the commit
    returns: Undo reverts only an entry proven committed. Then each line
    the merged row now contains is archived as resolved
    (``record_resolutions`` logs them first)."""
    from khipu import memory_lock
    from khipu.db import connect

    merged: list[dict[str, Any]] = []
    resolved = 0
    with connect() as conn:
        with conn.cursor() as cur:
            counts, by_episode, _additions = _plan(cur)
            conn.rollback()
            ids = sorted(by_episode)
            for start in range(0, len(ids), BATCH):
                part = ids[start:start + BATCH]
                entries: list[dict[str, Any]] = []
                to_resolve: list = []
                with memory_lock.shared():
                    try:
                        cur.execute(_LOCK_SQL, (part,))
                        for r in cur.fetchall():
                            for line in by_episode[int(r[0])]:
                                line["row"] = tuple(r[:4])
                            entry, resolve = _merge_episode(cur, tuple(r[:4]), by_episode[int(r[0])])
                            entries += [entry] if entry else []
                            to_resolve += resolve
                        if entries:
                            record(entries)
                            for e in entries:
                                hub = _str_list(e["prior_raw"]) or []
                                col = _str_list(e["prior_column"]) or []
                                cur.execute(_UPDATE_SQL, (json.dumps(col + e["column_added"], ensure_ascii=False),
                                                          json.dumps(hub + e["added"], ensure_ascii=False),
                                                          e["episode_id"]))
                            conn.commit()
                        else:
                            conn.rollback()
                    except BaseException:
                        conn.rollback()
                        raise
                if entries and record_committed is not None:
                    try:
                        record_committed([e["episode_id"] for e in entries])
                    except OSError:
                        # Unproven, so Undo leaves this batch's text alone
                        # unless the finished result names its episodes.
                        pass
                merged += entries
                resolved += _resolve(to_resolve, record_resolutions)
    return {"ok": True, "applied": True, **counts, "episodes_merged": len(merged),
            "preferences_added": sum(len(e["added"]) for e in merged),
            "episode_ids": [e["episode_id"] for e in merged], "lines_resolved": resolved,
            "next": ("Each merged line now counts as landed: a queued one resolves on its next drain; "
                     "a held one in dead/ resolves after `khipu outbox retry-dead`. Lines the hub now "
                     "holds every preference of, in another order, were archived as resolved."
                     if merged or resolved else "Nothing qualified; the hub is unchanged.")}


def _undo_fenced(cur, eid: int, entry: dict[str, Any]) -> bool:
    """Apply's fences, re-checked for Undo: forgotten by id on this Mac, or
    by any source identity the merged lines carried, here or in the hub."""
    from khipu.db import has_table
    from khipu.tombstones import episode_hidden, hub_form, read_gate

    if episode_hidden(eid):
        return True
    tags = set(entry.get("identities") or [])
    if not tags:
        return False
    try:
        if tags & read_gate().refusals:
            return True
    except Exception:  # noqa: BLE001 - an unreadable registry shows no episode
        return True
    if not has_table(cur, "forgotten_sources"):
        return False
    cur.execute("SELECT 1 FROM forgotten_sources WHERE identity = ANY(%s) LIMIT 1",
                (sorted(f for t in tags if (f := hub_form(t))),))
    return cur.fetchone() is not None


def undo(entries: list[dict[str, Any]], resolutions: list[dict[str, Any]] | None = None,
         committed: set[int] | frozenset[int] = frozenset(), include_unproven: bool = False) -> dict[str, Any]:
    """Put every job the merge resolved back where it was held, and the exact
    prior preferences of every episode it changed, when the row still holds
    exactly what the merge left. A row changed since (or forgotten) is left
    alone and named: undo never removes text something else wrote. Only an
    entry whose episode id is in ``committed`` is reverted. One whose batch
    never proved its commit and whose row still holds exactly prior + added
    is ``unproven`` (identical text may be a later writer's): left alone and
    named, never counted as restored, until the owner asks again with
    ``include_unproven``. An unproven entry whose row holds anything else
    never landed, or has changed since, and counts as already restored. A
    resolved record undo can't move back (forgotten, or a job already in its
    place) stays and is named."""
    from khipu import memory_lock, outbox
    from khipu.db import connect

    unresolved, still_resolved = 0, []
    for r in resolutions or []:
        folder = outbox.outbox_dir() if r.get("where") == "outbox" else outbox.dead_dir()
        kind = r.get("kind") if r.get("kind") == NORMALIZED_KIND else "preferences_contained"
        status = outbox.unresolve(str(r["record"]), folder / str(r["job"]), kind)
        if status == "restored":
            unresolved += 1
        elif status in ("forgotten", "present"):
            still_resolved.append({"record": str(r["record"]), "job": str(r["job"]), "why": status})
    latest = {int(e["episode_id"]): e for e in entries}  # one entry per episode per merge
    restored, already, left_alone, unproven, reverted = 0, 0, [], [], []
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
                        proven = eid in committed
                        if (row is None or row[1] is not None or not isinstance(row[2], dict)
                                or _undo_fenced(cur, eid, e)):
                            if proven:
                                left_alone.append(eid)
                            else:
                                already += 1
                            continue
                        raw_now, col_now = row[2].get("preferences"), row[3]
                        prior_raw = _str_list(e["prior_raw"]) or []
                        prior_col = _str_list(e["prior_column"]) or []
                        if (_str_list(raw_now) == prior_raw and (_str_list(col_now) or []) == prior_col):
                            already += 1
                            continue
                        if (_str_list(raw_now) != prior_raw + list(e["added"])
                                or (_str_list(col_now) or []) != prior_col + list(e["column_added"])):
                            if proven:
                                left_alone.append(eid)
                            else:
                                already += 1
                            continue
                        if not proven:
                            if not include_unproven:
                                unproven.append(eid)
                                continue
                            reverted.append(eid)
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
    out = {"ok": True, "restored": restored, "already_restored": already, "left_alone": left_alone,
           "lines_unresolved": unresolved, "lines_left_resolved": still_resolved, "unproven": unproven}
    if reverted:
        out["unproven_reverted"] = reverted
    if unproven:
        out["next"] = (f"Episodes {unproven} still hold the merged text: Khipu has no proof their batch committed, "
                       "so it can't tell the text from a later writer's identical text. This action is not fully "
                       "undone. To revert them, run the undo again with --include-unproven (owner direction "
                       "required, logged).")
    return out
