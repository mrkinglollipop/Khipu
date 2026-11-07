"""Proofs for the old owner merges' resolution records.

Two one-time owner commands (``khipu outbox merge-line-preferences`` and
``span-covered``) archived held recovery lines into ``resolved/``. The
commands are gone; the records stay, and ``outbox._validate_resolution``
still re-proves each one from the record alone. This module holds only that
proof: pure, no database, nothing here writes.
"""
from __future__ import annotations

from typing import Any

PROJECTED_FIELDS = ("scope", "topics", "tags")
CONTENT_KIND = "content_contained_normalized"
SCOPE_KIND = "scope_differs_kept"
DECISIONS_KIND = "decisions_contained_normalized"
_CONTENT_FIELDS = ("topics", "tags")


def _str_list(value: Any) -> list[str] | None:
    """A preference list as stored, [] for an absent or empty one, None for
    any other shape (left alone: nothing here guesses at its meaning)."""
    if value is None or value == [] or value == "":
        return []
    if isinstance(value, list) and all(isinstance(p, str) for p in value):
        return list(value)
    return None


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
        elif scope.strip() and raw.get("scope") is not None and raw["scope"] != scope.strip():
            # A row holding the scope K6 drops, verbatim (written before K6),
            # holds the line's scope; any other kept scope is a difference.
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
    elif isinstance(scope, str) and scope.strip() and raw.get("scope") not in (None, scope.strip()):
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
    if not _legacy_landed_line(raw, {k: v for k, v in projected.items() if k not in ("preferences", "people")}):
        return None
    if not _people_held(raw, projected):
        return None
    pieces = _legacy_comma_join(hub_prefs)
    return list(dict.fromkeys(p for p in line_prefs
                              if not _contiguous(_legacy_comma_join([p]), pieces)))


def _people_held(raw: dict[str, Any], view: dict[str, Any]) -> bool:
    """The row holds the line's ``people``, compared the way decisions are:
    each name's comma pieces are a contiguous, in-order run of the row's
    comma-joined people. An empty line value carries nothing; any other
    shape falls back to the drain's exact rule."""
    from khipu.capture import _legacy_comma_join, _legacy_landed_line

    line, hub = view.get("people"), raw.get("people")
    if line in (None, "", [], {}):
        return True
    if not (isinstance(line, list) and isinstance(hub, list)
            and all(isinstance(n, str) for n in [*line, *hub])):
        return _legacy_landed_line(raw, {"people": line})
    pieces = _legacy_comma_join(hub)
    return all(_contiguous(_legacy_comma_join([n]), pieces) for n in line if _legacy_comma_join([n]))


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


def _decisions_held(raw: Any, view: dict[str, Any]) -> bool:
    """Whether the row holds the line's decisions (the drain's comma-joined
    rule); a line whose decisions differ is never merged."""
    from khipu.capture import _legacy_landed_line

    return _legacy_landed_line(raw, {"decisions": view.get("decisions")})
