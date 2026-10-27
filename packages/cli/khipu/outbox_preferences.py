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

``--include-content`` (Matt 2026-10-27) also appends, for a held line, the
topic items the row lacks: each one where the hub's writer would put it
(``hygiene.classify_topics``: a noise slug in tags, any other in topics,
folded onto its alias), in the column and in raw, under the same fences,
batches, log and Undo. A line topic already in the row's topics or tags is
held. A line whose every field the row then holds is archived as
``content_contained_normalized``; one that differs only by a non-null scope
other than the row's is archived as ``scope_differs_kept`` (the hub scope is
never changed; the whole job, scope text included, is kept in resolved/).
Decisions are not appended by ``--include-content``: the hub keeps each
decision as its own row (the ``decisions`` registry, its supersede/retract
lifecycle and the 0047 fact claims) beside the episode's list, so a line
whose decisions differ stays ``needs_review`` there.

``--include-decisions`` (Matt 2026-10-27; implies ``--include-content``)
stores each decision a held line carries that its episode lacks through the
capture's own decision steps (``decisions.insert_decisions_from_episode``
then ``detect_reversals_from_episode``, as ``capture._merge_into_episode``
runs them for an existing episode), attached to that episode, and appends
it to the episode's decision list in column and raw. The log holds the
texts and the created row ids before the commit; a decision the writer
dedups onto a row already on file is never counted as created. Undo
retracts exactly the created rows (``decisions.retract``, reason naming the
owner action) and restores the prior list, refusing an episode whose rows
changed since. A line the row then holds, decisions read by the legacy
comma join as contiguous runs, is archived as
``decisions_contained_normalized``.
"""
from __future__ import annotations

import json
from typing import Any

ACTION = "outbox.merge_preferences"
CONTENT_KIND = "content_contained_normalized"
SCOPE_KIND = "scope_differs_kept"
DECISIONS_KIND = "decisions_contained_normalized"
_CONTENT_FIELDS = ("topics", "tags")
_LIST_FIELDS = ("topics", "tags", "decisions")
_COLUMN_AT = {"preferences": 3, "topics": 4, "tags": 5, "decisions": 6}
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
_CONTENT_LOCK_SQL = ("SELECT id, deleted_at, raw, preferences, topics, tags FROM episodes "
                     "WHERE id = ANY(%s) ORDER BY id FOR UPDATE")
_DECISIONS_LOCK_SQL = ("SELECT id, deleted_at, raw, preferences, topics, tags, decisions, project, session_id, ts "
                       "FROM episodes WHERE id = ANY(%s) ORDER BY id FOR UPDATE")
_DECISION_IDS_SQL = "SELECT id, text FROM decisions WHERE episode_id = %s ORDER BY id"
_DECISION_MAX_SQL = "SELECT coalesce(max(id), 0) FROM decisions"
_DECISION_ROWS_SQL = ("SELECT id, text, episode_id, superseded_by, retracted_at FROM decisions "
                      "WHERE id = ANY(%s) ORDER BY id FOR UPDATE")


def _field_update_sql(field: str) -> str:
    """``_UPDATE_SQL`` for one list field (column and raw), from a fixed set."""
    if field not in _COLUMN_AT:
        raise ValueError(f"not a merged field: {field}")
    return (f"UPDATE episodes SET {field} = %s::jsonb, "
            f"raw = jsonb_set(coalesce(raw, '{{}}'::jsonb), '{{{field}}}', %s::jsonb, true) "
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


def _content_items(view: dict[str, Any]) -> tuple[list[str], list[str]] | None:
    """A line's topic slugs (stripped, lowercased, deduped, as the writer
    reads them) and its own tags (kept as written, as the writer keeps
    them); None for any other shape."""
    topics = view.get("topics") if view.get("topics") not in (None, "") else []
    tags = view.get("tags") if view.get("tags") not in (None, "") else []
    if not (isinstance(topics, list) and isinstance(tags, list)
            and all(isinstance(t, str) for t in [*topics, *tags])):
        return None
    return (list(dict.fromkeys(s for t in topics if (s := t.strip().lower()))),
            list(dict.fromkeys(t for t in tags if t)))


def _placer(cur, decisions: bool = False):
    """Where the hub's writer would put one topic slug now:
    ``("tags", slug)`` for a noise slug, ``("topics", canonical)`` for any
    other (folded onto an existing page or alias), None when the check
    itself failed (the line stays held). ``hygiene.classify_topics`` itself,
    one slug at a time so the fold is unambiguous, in its own savepoint as
    write_pg runs it."""
    from khipu import hygiene

    cache: dict[str, tuple[str, str] | None] = {}

    def place(slug: str) -> tuple[str, str] | None:
        if slug not in cache:
            cur.execute("SAVEPOINT outbox_content_place")
            try:
                resolved, tags, unresolved = hygiene.classify_topics(cur, [slug])
            except Exception:  # noqa: BLE001 - an unknown placement keeps the line held
                resolved, tags, unresolved = [], [], True
            if unresolved:
                cur.execute("ROLLBACK TO SAVEPOINT outbox_content_place")
                cache[slug] = None
            else:
                cur.execute("RELEASE SAVEPOINT outbox_content_place")
                cache[slug] = (("tags", tags[0]) if tags else ("topics", resolved[0]) if resolved else None)
        return cache[slug]

    place.decisions = decisions  # --include-decisions: decisions are content too
    return place


def decisions_missing(raw: Any, view: dict[str, Any]) -> list[str] | None:
    """The line's decision strings the row lacks, each held only when its
    comma pieces are a contiguous, in-order run of the row's comma-joined
    decisions (the drain's legacy join, as ``normalized_missing`` reads
    preferences); None for a shape that is not a list of strings."""
    from khipu.capture import _legacy_comma_join

    if not isinstance(raw, dict):
        return None
    line = view.get("decisions")
    line = [] if line in (None, "", []) else line
    hub = _str_list(raw.get("decisions"))
    if hub is None or not isinstance(line, list) or not all(isinstance(d, str) for d in line):
        return None
    pieces = _legacy_comma_join(hub)
    return list(dict.fromkeys(d.strip() for d in line
                              if _legacy_comma_join([d]) and not _contiguous(_legacy_comma_join([d]), pieces)))


def content_missing(raw: Any, view: dict[str, Any], place, projection: Any = None,
                    decisions: bool | None = None) -> dict[str, list[str]] | None:
    """What the row lacks of a line once topics and tags are read as held
    content: ``{"preferences", "topics", "tags"}`` to append (preferences
    as ``normalized_missing`` finds them, on the line without its topics and
    tags), or None when anything else still differs (decisions, people,
    scope, unknown fields) or a topic's place is unknown. A line topic (or
    tag) already in the row's topics or tags is held; a missing one goes
    where ``place`` says, unless that slug is already held. With
    ``decisions`` (default: the placer's own ``decisions`` flag, set by
    ``--include-decisions``) the line's decisions are content too: the ones
    ``decisions_missing`` finds are returned under ``"decisions"``."""
    if not isinstance(raw, dict):
        return None
    if decisions is None:
        decisions = bool(getattr(place, "decisions", False))
    missing_decisions: list[str] = []
    if decisions:
        found = decisions_missing(raw, view)
        if found is None:
            return None
        missing_decisions = found
        view = {k: v for k, v in view.items() if k != "decisions"}
    items = _content_items(view)
    hub_topics, hub_tags = _str_list(raw.get("topics")), _str_list(raw.get("tags"))
    if items is None or hub_topics is None or hub_tags is None:
        return None
    prefs = normalized_missing(raw, {k: v for k, v in view.items() if k not in _CONTENT_FIELDS}, projection)
    if prefs is None:
        return None
    held = set(hub_topics) | set(hub_tags)
    add: dict[str, list[str]] = {"topics": [], "tags": []}
    slugs, line_tags = items
    for s in slugs:
        if s in held:
            continue
        where = place(s)
        if where is None or where[0] not in add or not isinstance(where[1], str) or not where[1]:
            return None
        field, slug = where
        if field == "topics" and slug in hub_tags and slug not in hub_topics:
            # The writer would have put the folded topic in topics; the row
            # has it only as a tag, which no record proves (content_proved
            # accepts a fold only into topics), so the line stays held.
            return None
        if slug not in held and slug not in add[field]:
            add[field].append(slug)
    for t in line_tags:
        if t not in held and t not in add["tags"]:
            add["tags"].append(t)
    out = {"preferences": prefs, **add}
    if decisions:
        out["decisions"] = missing_decisions
    return out


def _all_held(missing: dict[str, list[str]] | None) -> bool:
    return missing is not None and not any(missing.values())


def _scope_differs_kept(raw: dict[str, Any], view: dict[str, Any]) -> bool:
    """The one test for ``scope_differs_kept``, used to plan it and to prove
    its record: the line names a scope, the row keeps a non-empty one, and
    they differ."""
    scope, hub = view.get("scope"), raw.get("scope")
    return (isinstance(scope, str) and bool(scope.strip()) and isinstance(hub, str) and bool(hub)
            and scope.strip() != hub)


def _scope_dropped(raw: dict[str, Any], view: dict[str, Any]) -> dict[str, Any] | None:
    """The line without its scope, when ``_scope_differs_kept`` holds (the
    hub's scope is never changed); None otherwise."""
    if _scope_differs_kept(raw, view):
        return {k: v for k, v in view.items() if k != "scope"}
    return None


def _content_or_scope(raw: dict[str, Any], view: dict[str, Any], place,
                      decisions: bool | None = None) -> tuple[str, dict[str, Any]] | None:
    """``("content", view)`` when the row, with ``content_missing`` appended,
    holds the line; else ``("scope_kept", view without scope)`` when it
    holds the line but for a scope other than the row's; else None."""
    if content_missing(raw, view, place, decisions=decisions) is not None:
        return "content", view
    dropped = _scope_dropped(raw, view)
    if dropped is not None and content_missing(raw, dropped, place, decisions=decisions) is not None:
        return "scope_kept", dropped
    return None


def _held_kind(raw: dict[str, Any], view: dict[str, Any], place) -> tuple[str, dict[str, Any]] | None:
    """How the row holds a line once read as content: ``content`` /
    ``scope_kept`` without reading decisions as content, else (only with
    ``--include-decisions``) ``decisions`` / ``decisions_scope_kept``, with
    the line as proved (scope dropped for a kept one); None when it does not."""
    found = _content_or_scope(raw, view, place, decisions=False)
    if found is not None and _all_held(content_missing(raw, found[1], place, decisions=False)):
        return found
    if getattr(place, "decisions", False):
        found = _content_or_scope(raw, view, place, decisions=True)
        if found is not None and _all_held(content_missing(raw, found[1], place, decisions=True)):
            return ("decisions" if found[0] == "content" else "decisions_scope_kept"), found[1]
    return None


def _alias_folds(raw: dict[str, Any], view: dict[str, Any], place) -> dict[str, str]:
    """The line topics the row holds only under their folded slug (kept in
    the record, so validating it never needs the database)."""
    items = _content_items(view)
    if items is None:
        return {}
    held = set(_str_list(raw.get("topics")) or []) | set(_str_list(raw.get("tags")) or [])
    out = {}
    for s in items[0]:
        if s not in held and (where := place(s)) is not None and where[0] == "topics" and where[1] != s:
            out[s] = where[1]
    return out


def content_proved(payload: Any, view: dict[str, Any], resolution: dict[str, Any]) -> bool:
    """A ``content_contained_normalized`` or ``scope_differs_kept`` record's
    proof, from the record alone: no exact or preference rule holds; every
    line topic and tag is in the row's topics or tags (a topic under its
    stored fold, which must be in topics); the rest holds under the stored
    scope projection. ``scope_differs_kept`` also needs a line scope other
    than the row's non-null one, and is proved on the line without it."""
    from khipu.capture import _legacy_preferences_contained, _legacy_projection_kind

    if (not isinstance(payload, dict) or _legacy_projection_kind(payload, view) is not None
            or _legacy_preferences_contained(payload, view)):
        return False
    folds = resolution.get("topic_folds", {})
    if not (isinstance(folds, dict) and all(isinstance(k, str) and isinstance(v, str) and v
                                            for k, v in folds.items())):
        return False
    decisions = resolution["kind"] == DECISIONS_KIND
    scope_kept = resolution.get("scope_kept", False)
    if type(scope_kept) is not bool or (scope_kept and not decisions):
        return False
    if decisions and _decisions_held(payload, view):
        # Decisions the row holds exactly need no decision proof: such a
        # line is a content or scope kind, never this one.
        return False
    if resolution["kind"] == SCOPE_KIND or scope_kept:
        if not _scope_differs_kept(payload, view):
            return False
        view = {k: v for k, v in view.items() if k != "scope"}
    hub_topics = _str_list(payload.get("topics")) or []

    def place(slug: str) -> tuple[str, str] | None:
        return ("topics", folds[slug]) if slug in folds and folds[slug] in hub_topics else None

    return _all_held(content_missing(payload, view, place, resolution.get("normalized_projection"),
                                     decisions=decisions))


def _forgotten(cur, eid: int, view: dict[str, Any]) -> bool:
    """The fences every reader and writer uses: forgotten on this Mac by id
    (``episode_hidden``: a backup's older raw no source identity matches) or
    by source, here or in the hub (``hub_source_forgotten``)."""
    from khipu.tombstones import episode_hidden, hub_source_forgotten

    return episode_hidden(eid) or hub_source_forgotten(cur, view)


def _episode_plan(row: tuple, lines: list[dict[str, Any]], place=None
                  ) -> tuple[list[str], list[str], dict[str, list[str]]] | None:
    """One hub row's held lines against the row as the merge leaves it: each
    line's outcome (``landed`` as it stands, ``mergeable`` it lands once the
    additions are made, ``contained`` resolved here as
    ``preferences_contained``, ``other``) and the preferences to append, in
    one running list. A line no exact rule matches is tried once more
    through ``normalized_missing``: ``normalized`` when the merged row holds
    it after the writer's projection, resolved here as
    ``preferences_contained_normalized``. With ``place`` (``--include-content``)
    a line still ``other`` is tried through ``content_missing``: its topic
    and tag items are appended too (third value, by field), and it is
    ``content`` or ``scope_kept`` when the merged row then holds it. With a
    placer whose ``decisions`` flag is set (``--include-decisions``) its
    missing decisions are content too, and a line held only that way is
    ``decisions`` or ``decisions_scope_kept``. None when
    apply would skip the row for its shape (a raw that is not an object, or a
    preference, topic, tag or (with decisions) decision list in either place
    that is not a list of strings)."""
    from khipu.capture import _legacy_preferences_contained, _legacy_projection_kind

    _eid, _deleted_at, raw, column = row[:4]
    if not isinstance(raw, dict):
        return None
    hub_prefs, col_prefs = _str_list(raw.get("preferences")), _str_list(column)
    if hub_prefs is None or col_prefs is None:
        return None
    hub_content: dict[str, list[str]] = {}
    if place is not None:
        for f in (_LIST_FIELDS if getattr(place, "decisions", False) else _CONTENT_FIELDS):
            hub_content[f] = _str_list(raw.get(f))
            if hub_content[f] is None or (len(row) > _COLUMN_AT[f] and _str_list(row[_COLUMN_AT[f]]) is None):
                return None
    added: list[str] = []
    content_added: dict[str, list[str]] = {f: [] for f in hub_content}

    def merged(force_prefs: bool) -> dict[str, Any]:
        out = dict(raw, preferences=hub_prefs + added) if (added or force_prefs) else dict(raw)
        for f, extra in content_added.items():
            if extra:
                out[f] = hub_content[f] + extra
        return out

    before: list[str] = []
    for line in lines:
        now = merged(True)
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
                found = _content_or_scope(now, line["view"], place) if place is not None else None
                if found is not None:
                    m = content_missing(now, found[1], place)
                    missing = m["preferences"]
                    for f in content_added:
                        content_added[f] += [x for x in m[f] if x not in content_added[f]]
        added += [p for p in missing if p not in added]
    final = merged(False) if (added or any(content_added.values())) else raw
    kinds = []
    for line, was in zip(lines, before):
        if _legacy_projection_kind(final, line["view"]):
            kinds.append("landed" if was == "landed" else "mergeable")
        elif _legacy_preferences_contained(final, line["view"]):
            kinds.append("contained")
        elif normalized_missing(final, line["view"]) == []:
            kinds.append("normalized")
        elif place is not None and (found := _held_kind(final, line["view"], place)) is not None:
            kinds.append(found[0])
        else:
            kinds.append("other")
    return kinds, added, content_added


def _lookup(cur, lines: list[dict[str, Any]], content: bool = False, decisions: bool = False) -> dict[int, tuple]:
    """Each line's hub row by index: the row the drain's legacy check reads
    (live first, newest id), as (id, deleted_at, raw, preferences), plus the
    topics and tags columns with ``content``."""
    from khipu.capture import _md5_hex

    sql = _LOOKUP_SQL
    if decisions:
        sql = sql.replace("e.raw, e.preferences ", "e.raw, e.preferences, e.topics, e.tags, e.decisions ")
    elif content:
        sql = sql.replace("e.raw, e.preferences ", "e.raw, e.preferences, e.topics, e.tags ")
    rows: dict[int, tuple] = {}
    for start in range(0, len(lines), _LOOKUP_CHUNK):
        part = lines[start:start + _LOOKUP_CHUNK]
        cur.execute(sql, ([str(x["payload"].get("ts") or "") for x in part],
                          [_md5_hex(x["payload"].get("summary") or "") for x in part]))
        for i, eid, deleted_at, *rest in cur.fetchall():
            rows[start + int(i) - 1] = (int(eid), deleted_at, *rest)
    return rows


_COUNTS = ("mergeable", "landed", "preferences_contained", NORMALIZED_KIND, "needs_review", "forgotten",
           "no_hub_row", "column_shape")
_CONTENT_COUNTS = (CONTENT_KIND, SCOPE_KIND)
_COUNT_OF = {"contained": "preferences_contained", "normalized": NORMALIZED_KIND, "other": "needs_review",
             "content": CONTENT_KIND, "scope_kept": SCOPE_KIND, "decisions": DECISIONS_KIND,
             "decisions_scope_kept": DECISIONS_KIND}
_RESOLVED_KINDS = ("contained", "normalized", "content", "scope_kept", "decisions", "decisions_scope_kept")


def _plan(cur, place=None) -> tuple[dict[str, Any], dict[int, list[dict[str, Any]]], dict[int, list[str]],
                                    dict[int, dict[str, list[str]]]]:
    """Counts by outcome (with the episode ids of lines left for the owner's
    review: a real difference no projection explains), the held lines
    grouped by the episode they belong to (only episodes with something to
    do), each one's preference additions and (with ``place``) its topic and
    tag additions."""
    lines = held_lines()
    counts: dict[str, Any] = {"held_lines": len(lines), **{k: 0 for k in _COUNTS}}
    decisions = bool(getattr(place, "decisions", False))
    if place is not None:
        counts.update({k: 0 for k in _CONTENT_COUNTS})
    if decisions:
        counts[DECISIONS_KIND] = 0
    review: set[int] = set()
    review_decisions: set[int] = set()
    scope_kept: set[int] = set()
    grouped: dict[int, list[dict[str, Any]]] = {}
    rows: dict[int, tuple] = {}
    for i, row in _lookup(cur, lines, content=place is not None, decisions=decisions).items():
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
    content_additions: dict[int, dict[str, list[str]]] = {}
    for eid, group in grouped.items():
        planned = _episode_plan(rows[eid], group, place)
        if planned is None:
            counts["column_shape"] += len(group)
            continue
        kinds, added, content_added = planned
        for kind, line in zip(kinds, group):
            counts[_COUNT_OF.get(kind, kind)] += 1
            if kind in ("scope_kept", "decisions_scope_kept"):
                scope_kept.add(eid)
            if kind == "other" and place is not None and not _decisions_held(rows[eid][2], line["view"]):
                review_decisions.add(eid)
        if "other" in kinds:
            review.add(eid)
        if added or any(content_added.values()) or any(k in _RESOLVED_KINDS for k in kinds):
            by_episode[eid], additions[eid], content_additions[eid] = group, added, content_added
    counts["needs_review_episode_ids"] = sorted(review)
    if place is not None:
        counts["needs_review_decisions_episode_ids"] = sorted(review_decisions)
        counts["scope_differs_kept_episode_ids"] = sorted(scope_kept)
    return counts, by_episode, additions, content_additions


def _decisions_held(raw: Any, view: dict[str, Any]) -> bool:
    """Whether the row holds the line's decisions (the drain's comma-joined
    rule); a line whose decisions differ is never merged."""
    from khipu.capture import _legacy_landed_line

    return _legacy_landed_line(raw, {"decisions": view.get("decisions")})


def _sample(by_episode: dict[int, list[dict[str, Any]]], additions: dict[int, list[str]],
            content_additions: dict[int, dict[str, list[str]]] | None = None) -> list[dict[str, Any]]:
    """Ids and counts only: preference and topic text never reaches the summary."""
    out = []
    for eid in sorted(by_episode)[:SAMPLES]:
        line = by_episode[eid][0]
        item = {"episode_id": eid, "job": line["job"], "where": line["where"],
                "lines": len(by_episode[eid]), "preferences_to_add": len(additions[eid])}
        if content_additions is not None:
            item.update({f"{f}_to_add": len(content_additions[eid].get(f) or []) for f in _LIST_FIELDS
                         if f in content_additions[eid] or f in _CONTENT_FIELDS})
        out.append(item)
    return out


def _content_ready(cur) -> bool:
    from khipu.db import has_columns

    return has_columns(cur, "episodes", "topics", "tags")


def _decisions_ready(cur) -> bool:
    """The decisions registry with its lifecycle (0019 + 0024: Undo retracts)
    and the episode columns the writer reads."""
    from khipu import decisions
    from khipu.db import has_columns

    return (decisions._decisions_ready(cur) and decisions._evidence_ready(cur)
            and has_columns(cur, "episodes", "decisions", "project", "session_id"))


_NO_DECISIONS = {"ok": False, "code": "schema",
                 "error": "--include-decisions needs the hub's decisions registry with its retract columns "
                          "(migrations 0019 and 0024); nothing was changed."}


_NO_TAGS = {"ok": False, "code": "schema",
            "error": "--include-content needs the hub's episodes.tags column (migration 0010); nothing was changed."}


def dry_run(include_content: bool = False, include_decisions: bool = False) -> dict[str, Any]:
    from khipu.db import connect

    include_content = include_content or include_decisions
    with connect() as conn, conn.cursor() as cur:
        if include_content and not _content_ready(cur):
            conn.rollback()
            return dict(_NO_TAGS)
        if include_decisions and not _decisions_ready(cur):
            conn.rollback()
            return dict(_NO_DECISIONS)
        place = _placer(cur, include_decisions) if include_content else None
        counts, by_episode, additions, content_additions = _plan(cur, place)
        conn.rollback()
    out = {"ok": True, "dry_run": True, **counts,
           "episodes": sum(1 for eid in by_episode if additions[eid] or any(content_additions[eid].values())),
           "preferences_to_add": sum(len(a) for a in additions.values()),
           "samples": _sample(by_episode, additions, content_additions if include_content else None),
           "next": ("Merge them with `khipu outbox merge-line-preferences --apply --owner-direction`; "
                    "lines that differ in anything else stay held (needs_review_episode_ids)." if by_episode else
                    "No held line differs from its hub episode only by preferences the hub lacks.")}
    if include_content:
        for f in _CONTENT_FIELDS:
            out[f"{f}_to_add"] = sum(len(c.get(f) or []) for c in content_additions.values())
        out["next"] = ("Merge them with `khipu outbox merge-line-preferences --include-content --apply "
                       "--owner-direction`; decisions are never appended, so lines whose decisions differ stay "
                       "held (needs_review_decisions_episode_ids)." if by_episode else
                       "No held line differs from its hub episode only by preferences or topics the hub lacks.")
    if include_decisions:
        out["decisions_to_add"] = sum(len(c.get("decisions") or []) for c in content_additions.values())
        out["decisions_episode_ids"] = sorted(eid for eid, c in content_additions.items() if c.get("decisions"))
        out["next"] = ("Import them with `khipu outbox merge-line-preferences --include-decisions --apply "
                       "--owner-direction`: each missing decision goes through the capture's decision writer "
                       "(its own row, dedup, reversal detection, fact claims); lines still differing stay held "
                       "(needs_review_episode_ids)." if by_episode else
                       "No held line differs from its hub episode only by items the hub lacks.")
    return out


def _merge_episode(cur, row: tuple, lines: list[dict[str, Any]], place=None) -> tuple[dict[str, Any] | None, list]:
    """The append-only change for one locked row, re-proved against it now
    for its ``lines`` that passed the forget fences (``apply`` drops the rest
    under the memory lock and the row lock, before it calls this):
    what to record and write (None when nothing is to be added any more), and
    the lines to resolve (``preferences_contained``,
    ``preferences_contained_normalized`` or, with ``place``,
    ``content_contained_normalized`` / ``scope_differs_kept``) once it
    commits, each with the row's raw as the merge leaves it."""
    eid, deleted_at, raw, column = row[:4]
    if deleted_at is not None:
        return None, []
    live = lines  # past the forget fences: ``apply`` checks them under both locks
    planned = _episode_plan(row, live, place) if live else None
    if planned is None:
        return None, []
    kinds, added, content_added = planned
    hub_prefs, col_prefs = _str_list(raw.get("preferences")), _str_list(column)
    final = dict(raw, preferences=hub_prefs + added) if added else dict(raw)
    for f, extra in content_added.items():
        if extra:
            final[f] = (_str_list(raw.get(f)) or []) + extra
    resolve = [(line, final, place) for line, kind in zip(live, kinds) if kind in _RESOLVED_KINDS]
    if not added and not any(content_added.values()):
        return None, resolve
    identities = sorted({tag for line in live for tag in _identity_tags(line["view"])})
    entry = {"episode_id": eid, "added": added,
             "prior_raw": raw.get("preferences"), "prior_raw_set": "preferences" in raw,
             "prior_column": column,
             "column_added": [p for p in added if p not in col_prefs],
             "identities": identities}
    content = {f: {"added": extra, "prior_raw": raw.get(f), "prior_raw_set": f in raw,
                   "prior_column": row[_COLUMN_AT[f]],
                   "column_added": [x for x in extra if x not in (_str_list(row[_COLUMN_AT[f]]) or [])]}
               for f, extra in content_added.items() if extra}
    if content:
        entry["content"] = content
    return entry, resolve


def _identity_tags(view: dict[str, Any]) -> set[str]:
    """The registry identities a merged line is known by (hashes, never
    text), kept so Undo re-checks the same source fence apply did."""
    from khipu.tombstones import identity_strings

    return identity_strings(view)


def _resolution(line: dict[str, Any], final_raw: dict[str, Any], place=None) -> tuple[dict[str, Any], str] | None:
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
    found = None
    if _legacy_preferences_contained(final_raw, view):
        kind = "preferences_contained"
    elif normalized_missing(final_raw, view) == []:
        kind = NORMALIZED_KIND
        extra["normalized_projection"] = projection_of(_writer_projection(final_raw, view))
    elif place is not None and (found := _held_kind(final_raw, view, place)) is not None:
        kind = {"content": CONTENT_KIND, "scope_kept": SCOPE_KIND}.get(found[0], DECISIONS_KIND)
        if found[0] == "decisions_scope_kept":
            extra["scope_kept"] = True
        rest = {k: v for k, v in found[1].items()
                if k not in _CONTENT_FIELDS and not (kind == DECISIONS_KIND and k == "decisions")}
        extra["normalized_projection"] = projection_of(_writer_projection(final_raw, rest))
        extra["topic_folds"] = _alias_folds(final_raw, found[1], place)
    else:
        return None
    record = {"payload": final_raw, "queued_evidence": queued,
              "resolution": {"kind": kind, "episode_id": int(line["row"][0]),
                             "original_payload_sha256": payload_sha256(final_raw),
                             "queued_payload_sha256": payload_sha256(view),
                             "resolved_at": datetime.now(timezone.utc).isoformat(), **extra}}
    return record, hashlib.sha256(line["snapshot"]).hexdigest()


def _resolve(resolve: list, record_resolutions, failed: list | None = None) -> int:
    """Archive each contained line (logged first), after its row's merge
    committed. A line whose file changed, or whose source is forgotten, is
    left where it is. A line whose record fails its own proof (validated
    before it is logged, and again by the archive) stays held and is added
    to ``failed`` by job name and episode id: one bad line never stops the
    rest."""
    from khipu import outbox

    done = 0
    pending = list(resolve)
    failed = [] if failed is None else failed

    def fail(line: dict[str, Any], exc: Exception) -> None:
        failed.append({"job": line["job"], "where": line["where"], "episode_id": int(line["row"][0]),
                       "error": f"{type(exc).__name__}: {exc}"})

    for _round in range(_RESOLVE_ROUNDS):
        built = []
        for line, final, place in pending:
            made = _resolution(line, final, place)
            if not made:
                continue
            try:
                outbox._validate_resolution(made[0])
            except (ValueError, TypeError, KeyError) as exc:
                fail(line, exc)
                continue
            built.append((line, final, place, *made))
        if not built:
            break
        record_resolutions([{"episode_id": int(line["row"][0]), "record": record_id, "job": line["job"],
                             "where": line["where"], "kind": record["resolution"]["kind"]}
                            for line, _final, _place, record, record_id in built])
        pending = []
        for line, final, place, record, _record_id in built:
            try:
                status = outbox.archive_resolved(line["path"], line["snapshot"], record)
            except ValueError as exc:
                fail(line, exc)
                continue
            if status == "resolved":
                done += 1
            elif status == "changed":
                # A queued line is rewritten by every drain pass (attempts,
                # last error) and can move to dead/ while the merge runs, so
                # the bytes read at planning time go stale. Re-read it and
                # retry when it is still the same held line.
                fresh = _reread(line)
                if fresh is not None:
                    pending.append((fresh, final, place))
        if not pending:
            break
    return done


_RESOLVE_ROUNDS = 3


def _reread(line: dict[str, Any]) -> dict[str, Any] | None:
    """The same held line as it sits now, in outbox/ or dead/, with its
    current bytes; None when it is gone, unreadable, no longer a held legacy
    line, or carries a different payload than the one that was proved."""
    from khipu import outbox

    for where, folder in (("outbox", outbox.outbox_dir()), ("dead", outbox.dead_dir())):
        path = folder / line["job"]
        try:
            snapshot = path.read_bytes()
            job = outbox._read_job(path)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, TypeError, RecursionError):
            return None
        if (job.get("reason") != LEGACY_REASON or "origin" not in job or job["origin"] is not None
                or job["payload"] != line["payload"]):
            return None
        return dict(line, where=where, path=path, snapshot=snapshot)
    return None


def _decision_payload(row: tuple, texts: list[str], lines: list[dict[str, Any]]) -> dict[str, Any]:
    """What the capture's decision steps read, for the hub episode itself:
    its own project, session and ts (the writer dedups in the 30 days before
    the episode's ts, never now), the decisions to add, and any
    ``decision_details`` the lines carry for exactly those texts."""
    eid, _deleted_at, raw, *_rest = row
    project, session_id, ts = row[7], row[8], row[9]
    ts = ts.isoformat() if hasattr(ts, "isoformat") else (ts or raw.get("ts"))
    wanted = set(texts)
    details = []
    for line in lines:
        for item in line["view"].get("decision_details") or []:
            if (isinstance(item, dict) and str(item.get("text") or "").strip() in wanted
                    and item not in details):
                details.append(item)
    payload = {"decisions": list(texts), "project": project if project is not None else raw.get("project"),
               "session_id": session_id if session_id is not None else raw.get("session_id"), "ts": ts}
    if details:
        payload["decision_details"] = details
    return payload


def _write_decisions(cur, row: tuple, spec: dict[str, Any], lines: list[dict[str, Any]]) -> bool:
    """Store one episode's missing decisions through the capture's own
    decision steps (``decisions.insert_decisions_from_episode``: a row each,
    the 30-day dedup, 0047 fact claims; then
    ``decisions.detect_reversals_from_episode``), attached to the hub
    episode, in savepoints as ``capture._merge_into_episode`` runs them for
    an existing episode. Records in ``spec`` the rows it created (id and
    text: Undo retracts exactly these) and the texts it deduped onto a row
    already on file (never created, never retracted). False when the insert
    step failed: nothing of it stays, and the episode is skipped."""
    from khipu import decisions
    from khipu.decisions import normalize_text

    eid = int(row[0])
    payload = _decision_payload(row, spec["added"], lines)
    cur.execute(_DECISION_IDS_SQL, (eid,))
    before = {int(r[0]) for r in cur.fetchall()}
    # A row the writer creates takes an id above every row on file now. A
    # duplicate it claims instead keeps its own, lower id, even when the claim
    # re-anchors it to this episode (0047: an owner's decision takes its first
    # live episode claimant), so it is never counted as created.
    cur.execute(_DECISION_MAX_SQL)
    ceiling = int(cur.fetchone()[0] or 0)
    cur.execute("SAVEPOINT outbox_decisions")
    try:
        decisions.insert_decisions_from_episode(cur, payload, eid)
    except Exception as exc:  # noqa: BLE001 - the episode is skipped, its lines stay held
        cur.execute("ROLLBACK TO SAVEPOINT outbox_decisions")
        decisions._log(f"outbox decisions import skipped episode {eid} ({type(exc).__name__})")
        return False
    cur.execute("RELEASE SAVEPOINT outbox_decisions")
    cur.execute(_DECISION_IDS_SQL, (eid,))
    asked = {normalize_text(decisions._redacted_value(t)) for t in spec["added"]}
    created = [{"id": int(i), "text": t} for i, t in cur.fetchall()
               if int(i) not in before and int(i) > ceiling and normalize_text(t or "") in asked]
    made = {normalize_text(c["text"]) for c in created}
    spec["created"] = created
    spec["deduped"] = [t for t in spec["added"] if normalize_text(decisions._redacted_value(t)) not in made]
    # Reversal detection reads the rows just written; as in the capture, a
    # failure here never takes those rows down with it.
    cur.execute("SAVEPOINT outbox_decision_detection")
    try:
        decisions.detect_reversals_from_episode(cur, payload, eid)
    except Exception as exc:  # noqa: BLE001 - fail-open, as the capture's step
        cur.execute("ROLLBACK TO SAVEPOINT outbox_decision_detection")
        decisions._log(f"outbox decision detection failed for episode {eid} ({type(exc).__name__})")
    else:
        cur.execute("RELEASE SAVEPOINT outbox_decision_detection")
    return True


def apply(*, record, record_resolutions, record_committed=None, include_content: bool = False,
          include_decisions: bool = False) -> dict[str, Any]:
    """Merge every qualifying line, BATCH episodes per transaction, each row
    locked in id order and re-proved under its lock. ``record(entries)``
    writes a transaction's entries to the owner-actions log before it
    commits; when it raises, that transaction is rolled back.
    ``record_committed(episode_ids)`` marks them committed once the commit
    returns: Undo reverts only an entry proven committed. Then each line
    the merged row now contains is archived as resolved
    (``record_resolutions`` logs them first). ``include_content`` also
    appends topic and tag items (``content_missing``), written the same way
    and recorded under ``content`` in each entry. ``include_decisions``
    (implies ``include_content``) also stores each missing decision through
    ``_write_decisions`` before the transaction's entries are logged, so the
    log holds the texts and the created row ids before it commits."""
    from khipu import memory_lock
    from khipu.db import connect
    from khipu.tombstones import episode_hidden, hub_source_forgotten

    include_content = include_content or include_decisions
    merged: list[dict[str, Any]] = []
    resolved = 0
    failed_lines: list[dict[str, Any]] = []
    decisions_failed: list[int] = []
    with connect() as conn:
        with conn.cursor() as cur:
            if include_content and not _content_ready(cur):
                conn.rollback()
                return dict(_NO_TAGS)
            if include_decisions and not _decisions_ready(cur):
                conn.rollback()
                return dict(_NO_DECISIONS)
            planned = _plan(cur, _placer(cur, include_decisions)) if include_content else _plan(cur)
            counts, by_episode = planned[0], planned[1]
            conn.rollback()
            place = _placer(cur, include_decisions) if include_content else None
            width = 10 if include_decisions else 6 if include_content else 4
            lock_sql = _DECISIONS_LOCK_SQL if include_decisions else _CONTENT_LOCK_SQL if include_content \
                else _LOCK_SQL
            ids = sorted(by_episode)
            for start in range(0, len(ids), BATCH):
                part = ids[start:start + BATCH]
                entries: list[dict[str, Any]] = []
                to_resolve: list = []
                with memory_lock.shared():
                    try:
                        cur.execute(lock_sql, (part,))
                        for r in cur.fetchall():
                            eid = int(r[0])
                            lines = by_episode[eid]
                            for line in lines:
                                line["row"] = tuple(r[:4])
                            # The forget fences, under the memory lock and the
                            # row lock, before anything of this episode is
                            # written: forgotten by id on this Mac, or a line's
                            # source forgotten here or in the hub.
                            live = [] if episode_hidden(eid) else [
                                line for line in lines if not hub_source_forgotten(cur, line["view"])]
                            entry, resolve = _merge_episode(cur, tuple(r[:width]), live, place)
                            spec = ((entry or {}).get("content") or {}).get("decisions")
                            # Decision details come from the live lines only:
                            # a line forgotten under the lock lends nothing.
                            if spec and not _write_decisions(cur, tuple(r[:width]), spec, live):
                                # Nothing of this episode is written or
                                # resolved: its lines stay held.
                                decisions_failed.append(int(r[0]))
                                continue
                            entries += [entry] if entry else []
                            to_resolve += resolve
                        if entries:
                            record(entries)
                            for e in entries:
                                if e["added"]:
                                    hub = _str_list(e["prior_raw"]) or []
                                    col = _str_list(e["prior_column"]) or []
                                    cur.execute(_UPDATE_SQL, (json.dumps(col + e["column_added"], ensure_ascii=False),
                                                              json.dumps(hub + e["added"], ensure_ascii=False),
                                                              e["episode_id"]))
                                for f, c in (e.get("content") or {}).items():
                                    hub = _str_list(c["prior_raw"]) or []
                                    col = _str_list(c["prior_column"]) or []
                                    cur.execute(_field_update_sql(f),
                                                (json.dumps(col + c["column_added"], ensure_ascii=False),
                                                 json.dumps(hub + c["added"], ensure_ascii=False), e["episode_id"]))
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
                resolved += _resolve(to_resolve, record_resolutions, failed_lines)
    out = {"ok": True, "applied": True, **counts, "episodes_merged": len(merged),
           "preferences_added": sum(len(e["added"]) for e in merged),
           "episode_ids": [e["episode_id"] for e in merged], "lines_resolved": resolved,
           "next": ("Each merged line now counts as landed: a queued one resolves on its next drain; "
                    "a held one in dead/ resolves after `khipu outbox retry-dead`. Lines the hub now "
                    "holds every preference of, in another order, were archived as resolved."
                    if merged or resolved else "Nothing qualified; the hub is unchanged.")}
    if include_content:
        for f in _CONTENT_FIELDS:
            out[f"{f}_added"] = sum(len(((e.get("content") or {}).get(f) or {}).get("added") or []) for e in merged)
    if include_decisions:
        specs = [(e["episode_id"], (e.get("content") or {}).get("decisions")) for e in merged]
        specs = [(eid, d) for eid, d in specs if d]
        out["decisions_added"] = sum(len(d["added"]) for _eid, d in specs)
        out["decision_ids_created"] = sorted(c["id"] for _eid, d in specs for c in d.get("created") or [])
        out["decisions_deduped"] = sum(len(d.get("deduped") or []) for _eid, d in specs)
        out["decisions_failed_episode_ids"] = decisions_failed
    if failed_lines:
        # One line whose record fails its own proof stays held; the rest
        # were still merged and archived.
        out["lines_failed"] = failed_lines
        out["next"] += (f" {len(failed_lines)} line(s) could not be archived (lines_failed) and stay held; "
                        "every other line was handled.")
    return out


def _undo_specs(entry: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Each field one merge entry changed, with its prior lists and
    additions: preferences (every entry from before ``--include-content``
    has them), then topics, tags and decisions under ``content``."""
    specs = []
    if entry.get("added") or not entry.get("content"):
        specs.append(("preferences", entry))
    for f in _LIST_FIELDS:
        spec = (entry.get("content") or {}).get(f)
        if isinstance(spec, dict) and spec.get("added"):
            specs.append((f, spec))
    return specs


def _field_state(row: tuple, field: str, spec: dict[str, Any]) -> str:
    """``prior`` when the row holds exactly the field's prior lists,
    ``merged`` when it holds exactly prior + added, else ``changed``."""
    raw_now = _str_list(row[2].get(field))
    col_now = _str_list(row[_COLUMN_AT[field]]) or [] if len(row) > _COLUMN_AT[field] else None
    prior_raw, prior_col = _str_list(spec["prior_raw"]) or [], _str_list(spec["prior_column"]) or []
    if raw_now == prior_raw and col_now == prior_col:
        return "prior"
    if raw_now == prior_raw + list(spec["added"]) and col_now == prior_col + list(spec["column_added"]):
        return "merged"
    return "changed"


def _changed_decisions(cur, eid: int, created: list[dict[str, Any]]) -> list[int]:
    """The created decision rows (locked, after their episode) that changed
    since the import: gone, reworded, moved to another episode, superseded,
    retracted, in a link someone resolved (confirmed, rejected, restored), or
    claimed by another episode or the owner. Undo retracts none of a row's
    episode's rows when any of them changed."""
    from khipu import decisions

    ids = sorted(int(c["id"]) for c in created)
    cur.execute(_DECISION_ROWS_SQL, (ids,))
    rows = {int(r[0]): r for r in cur.fetchall()}
    changed = set()
    for c in created:
        r = rows.get(int(c["id"]))
        if (r is None or r[1] != c["text"] or r[2] is None or int(r[2]) != eid
                or r[3] is not None or r[4] is not None):
            changed.add(int(c["id"]))
    if decisions._links_ready(cur):
        cur.execute("SELECT old_id, new_id FROM decision_links WHERE (old_id = ANY(%s) OR new_id = ANY(%s)) "
                    "AND state <> 'candidate'", (ids, ids))
        changed |= {int(i) for pair in cur.fetchall() for i in pair if int(i) in ids}
    if decisions._provenance_ready(cur):
        cur.execute("SELECT DISTINCT fact_id FROM fact_claims WHERE kind = 'decision' AND fact_id = ANY(%s) "
                    "AND (claimant <> 'episode' OR episode_id IS DISTINCT FROM %s)", (ids, eid))
        changed |= {int(r[0]) for r in cur.fetchall()}
    return sorted(changed)


def undo(entries: list[dict[str, Any]], resolutions: list[dict[str, Any]] | None = None,
         committed: set[int] | frozenset[int] = frozenset(), include_unproven: bool = False,
         retract_reason: str | None = None) -> dict[str, Any]:
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
    place) stays and is named. An entry that imported decisions also
    retracts exactly the rows it created (``decisions.retract``, the normal
    path, with ``retract_reason`` naming the owner action), never one it
    deduped onto; when any created row changed since (``_changed_decisions``)
    nothing of that episode is undone and the rows are named."""
    from khipu import decisions, memory_lock, outbox
    from khipu.db import connect
    from khipu.tombstones import RegistryError, episode_hidden, hub_source_forgotten_tags

    def forgotten_source(entry: dict[str, Any]) -> bool:
        # Apply's source fence, re-checked by the identities the merged lines
        # carried (kept in the entry): here or in the hub. An unreadable
        # registry undoes nothing of the episode.
        try:
            return hub_source_forgotten_tags(cur, entry.get("identities"))
        except RegistryError:
            return True

    unresolved, still_resolved = 0, []
    for r in resolutions or []:
        folder = outbox.outbox_dir() if r.get("where") == "outbox" else outbox.dead_dir()
        kind = (r.get("kind") if r.get("kind") in (NORMALIZED_KIND, CONTENT_KIND, SCOPE_KIND, DECISIONS_KIND)
                else "preferences_contained")
        status = outbox.unresolve(str(r["record"]), folder / str(r["job"]), kind)
        if status == "restored":
            unresolved += 1
        elif status in ("forgotten", "present"):
            still_resolved.append({"record": str(r["record"]), "job": str(r["job"]), "why": status})
    latest = {int(e["episode_id"]): e for e in entries}  # one entry per episode per merge
    restored, already, left_alone, unproven, reverted = 0, 0, [], [], []
    retracted: list[int] = []
    decisions_changed: list[dict[str, Any]] = []
    ids = sorted(latest)
    content = any(e.get("content") for e in latest.values())
    with_decisions = any((e.get("content") or {}).get("decisions") for e in latest.values())
    lock_sql = _DECISIONS_LOCK_SQL if with_decisions else _CONTENT_LOCK_SQL if content else _LOCK_SQL
    with connect() as conn, conn.cursor() as cur:
        for start in range(0, len(ids), BATCH):
            part = ids[start:start + BATCH]
            with memory_lock.shared():
                try:
                    cur.execute(lock_sql, (part,))
                    rows = {int(r[0]): r for r in cur.fetchall()}
                    for eid in part:
                        e, row = latest[eid], rows.get(eid)
                        proven = eid in committed
                        if (row is None or row[1] is not None or not isinstance(row[2], dict)
                                or episode_hidden(eid) or forgotten_source(e)):
                            if proven:
                                left_alone.append(eid)
                            else:
                                already += 1
                            continue
                        specs = _undo_specs(e)
                        states = {_field_state(row, f, spec) for f, spec in specs}
                        if states == {"prior"}:
                            already += 1
                            continue
                        if states != {"merged"}:
                            if proven:
                                left_alone.append(eid)
                            else:
                                already += 1
                            continue
                        if not proven:
                            if not include_unproven:
                                unproven.append(eid)
                                continue
                        created = ((e.get("content") or {}).get("decisions") or {}).get("created") or []
                        if created:
                            changed = _changed_decisions(cur, eid, created)
                            if changed:
                                left_alone.append(eid)
                                decisions_changed.append({"episode_id": eid, "decision_ids": changed})
                                continue
                        if not proven:
                            reverted.append(eid)
                        for c in created:
                            if decisions.retract(cur, int(c["id"]), retract_reason):
                                retracted.append(int(c["id"]))
                        for f, spec in specs:
                            if spec.get("prior_raw_set"):
                                raw_sql = (f"raw = jsonb_set(coalesce(raw, '{{}}'::jsonb), '{{{f}}}', "
                                           "%s::jsonb, true)")
                                raw_arg = [json.dumps(spec["prior_raw"], ensure_ascii=False)]
                            else:
                                raw_sql, raw_arg = f"raw = raw - '{f}'", []
                            cur.execute(f"UPDATE episodes SET {f} = %s::jsonb, {raw_sql} "
                                        "WHERE id = %s AND deleted_at IS NULL",
                                        (None if spec["prior_column"] is None
                                         else json.dumps(spec["prior_column"], ensure_ascii=False), *raw_arg, eid))
                        restored += 1
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
    out = {"ok": True, "restored": restored, "already_restored": already, "left_alone": left_alone,
           "lines_unresolved": unresolved, "lines_left_resolved": still_resolved, "unproven": unproven}
    if with_decisions:
        out["decisions_retracted"] = sorted(retracted)
        out["decisions_changed"] = decisions_changed
    if reverted:
        out["unproven_reverted"] = reverted
    if unproven:
        out["next"] = (f"Episodes {unproven} still hold the merged text: Khipu has no proof their batch committed, "
                       "so it can't tell the text from a later writer's identical text. This action is not fully "
                       "undone. To revert them, run the undo again with --include-unproven (owner direction "
                       "required, logged).")
    return out
