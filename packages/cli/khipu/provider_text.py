"""The one chokepoint for text that leaves this Mac for an embedding,
summariser, reflect, rerank or graph provider.

Every function that calls a provider client must get its text from here:
``fenced_texts`` for text derived from stored episodes (embeddings of episodes,
commitments and topics, briefs, reflect, rerank, hygiene judging) and
``live_input`` for input that is not stored memory (a typed query, a probe, a
transcript being captured, a library chunk, an image). The guard in
``tests/test_forget_fence.py`` fails any provider caller that calls neither.

A ``note:`` topic that no episode carries is a file the owner or an outside
tool wrote (no Khipu code writes a note from episode text), so it embeds. Forget
is episode-scoped: it does not chase text the owner copied into a separate
memory.

Fencing is by source episode: a text is withheld when ANY episode it derives
from is on this Mac's forget list or queued for erasure. A text with no source
ids (a legacy row with NULL provenance) or with a malformed id cannot be proven
live and is withheld too. When the list cannot be read nothing episode-derived
goes out. Only counts are logged.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import Any

_log = logging.getLogger("khipu.provider_text")

# Text derived from stored episodes: each needs source episode ids.
DERIVED_KINDS = frozenset({
    "episode", "commitment", "topic", "brief", "reflect", "rerank", "decision",
    "deliverable", "graph", "capture-edit", "capture-embed",
})
# Input that is not stored memory: nothing to fence by episode. "payload" is
# only the capture payload being written right now, never a stored row.
LIVE_KINDS = frozenset({"query", "probe", "transcript", "library", "payload", "media"})


# A memory-note topic (slug "note:...") that no episode carries was written by
# hand, not derived from an episode: a found, empty source list is proof there
# is nothing to forget. Any other empty list is unknown provenance (withheld).
NOTE_ORIGIN = "note-origin"


class SourcedText(str):
    """A text that carries the episode ids it derives from (``sources``), so a
    function that only receives strings can still reach the fence."""

    sources: tuple[Any, ...] | None  # None: the source is unknown (withheld)

    # str(x) and f"{x}" would otherwise hand back a plain str with the ids gone.
    def __str__(self) -> "SourcedText":  # type: ignore[override]
        return self

    def __format__(self, spec: str) -> str:
        return self if not spec else str.__format__(self, spec)

    def __new__(cls, text: Any, sources: Iterable[Any] | None = ()):
        obj = super().__new__(cls, text or "")
        if sources is None:
            obj.sources = None
        elif isinstance(sources, (str, bytes)) or not isinstance(sources, Iterable):
            obj.sources = (sources,)
        else:
            obj.sources = tuple(sources)  # malformed ids stay, so the fence can withhold the row
        return obj


def sourced(text: Any, sources: Iterable[Any] | None = ()) -> SourcedText:
    return SourcedText(text, sources)


def _ids(value: Any) -> list[int]:
    """The well-formed ids in ``value``; malformed ones are dropped. Use
    :func:`_parse_ids` where a malformed id must withhold the row."""
    return _parse_ids(value)[0]


def _parse_ids(value: Any) -> tuple[list[int], int]:
    """``(ids, malformed)``: ``malformed`` counts entries that are not a plain
    non-negative integer or digit string. ``NOTE_ORIGIN`` is a proven absence
    of episode sources, not an id."""
    if value is None:
        return [], 0
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        value = [value]
    out: list[int] = []
    bad = 0
    for item in value:
        if item == NOTE_ORIGIN:
            continue
        if isinstance(item, bool):
            bad += 1
        elif isinstance(item, int):
            out.append(item)
        elif isinstance(item, str) and item.strip().isdigit():
            out.append(int(item))
        else:
            bad += 1
    return out, bad


def fenced_texts(texts: Sequence[Any], source_ids: Sequence[Any] | None, kind: str) -> list[Any]:
    """``texts`` with each withheld entry replaced by None, same length and
    order. ``source_ids[i]`` is the episode id (or ids) text ``i`` derives
    from; None reads them from each text's ``SourcedText.sources``. A text
    with no known source (neither an id nor a ``SourcedText``), an empty source
    list, or a malformed id cannot be proven live, so it is withheld too. An unreadable forget list withholds
    every text."""
    from khipu import tombstones

    if kind not in DERIVED_KINDS:
        raise ValueError(f"fenced_texts is for episode-derived kinds, not {kind!r}")
    items = list(texts)
    if source_ids is not None and len(source_ids) != len(items):
        raise ValueError("source_ids must match texts")
    groups: list[list[int]] = []
    unproven: set[int] = set()
    malformed: set[int] = set()
    for i, text in enumerate(items):
        if source_ids is not None:
            raw = source_ids[i]
        elif isinstance(text, SourcedText):
            raw = text.sources
        else:
            raw = None
        ids, bad = _parse_ids(raw)
        noted = isinstance(raw, (list, tuple)) and NOTE_ORIGIN in raw
        if bad:
            malformed.add(i)
        elif not ids and not noted:
            unproven.add(i)
        groups.append(ids)
    rows = [{"kind": "episode", "id": eid, "slot": slot}
            for slot, ids in enumerate(groups) for eid in ids]
    try:
        tombstones.withheld_episode_ids()
        readable = True
    except Exception:  # noqa: BLE001 - an unreadable forget list sends nothing episode-derived
        readable = False
    if not readable:
        tombstones.withhold(rows)  # records the unreadable state for the doctor row
        _count(kind, len(items), len(items), readable=False)
        return [None] * len(items)
    kept_rows = {id(r) for r in tombstones.withhold(rows)}
    forgotten = {r["slot"] for r in rows if id(r) not in kept_rows}
    out = [None if (i in unproven or i in malformed or i in forgotten) else items[i]
           for i in range(len(items))]
    _count(kind, len(items), sum(1 for t in out if t is None), readable=True,
           unprovenanced=len(unproven), bad_source_id=len(malformed))
    return out


def fence_chunks(cur: Any, items: Sequence[tuple[str, str, Any]]) -> list[Any]:
    """The chokepoint for embedding batches: ``items`` are ``(kind, ref, text)``
    with kind episode, topic or commitment and ``ref`` the stored row's id or
    slug. Resolves each row's source episodes itself and returns the texts with
    withheld entries as None (same length and order)."""
    out: list[Any] = [None] * len(items)
    for kind in ("episode", "topic", "commitment"):
        idx = [i for i, it in enumerate(items) if it[0] == kind]
        if not idx:
            continue
        refs = [str(items[i][1]) for i in idx]
        if kind == "episode":
            sources: list[Any] = [[r] for r in refs]
        elif kind == "topic":
            found = episode_ids_of_topics(cur, refs)
            sources = [found.get(r) for r in refs]
        else:
            found = episode_ids_of_commitments(cur, refs)
            sources = [found.get(r) for r in refs]
        for i, text in zip(idx, fenced_texts([items[i][2] for i in idx], sources, kind)):
            out[i] = text
    unknown = [it[0] for it in items if it[0] not in ("episode", "topic", "commitment")]
    if unknown:
        raise ValueError(f"fence_chunks cannot place kind {unknown[0]!r}")
    return out


def recheck_chunks(cur: Any, items: Sequence[tuple[str, str, Any]]):
    """A callable for ``embed_batch(recheck=...)``: True while every item
    still passes ``fence_chunks``. Called before each send attempt, so a
    forget that lands during a retry back-off stops the retry."""
    frozen = list(items)
    return lambda: all(t is not None for t in fence_chunks(cur, frozen))


def recheck_texts(texts: Sequence[Any], source_ids: Sequence[Any] | None, kind: str):
    """``recheck_chunks`` for texts fenced with ``fenced_texts``."""
    frozen, ids = list(texts), (list(source_ids) if source_ids is not None else None)
    return lambda: all(t is not None for t in fenced_texts(frozen, ids, kind))


def live_input(value: Any, kind: str) -> Any:
    """``value`` unchanged: input that is not stored memory. Only the explicit
    ``LIVE_KINDS`` pass, and only a plain ``str`` or ``bytes``, or a list or
    tuple of them (an embedding batch, an image and its mime type). A value
    that carries source ids (a ``SourcedText``) came from a stored row and is
    refused: it must go through ``fenced_texts`` with those ids. A mapping, a
    row object or anything else is refused too, since its text could be a
    stored row's."""
    if kind not in LIVE_KINDS:
        raise ValueError(f"live_input is for input outside stored memory, not {kind!r}")
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, SourcedText):
            raise ValueError("live_input refuses text that carries source episode ids")
        if isinstance(item, (list, tuple)):
            stack.extend(item)
        elif not isinstance(item, (str, bytes)):
            raise ValueError(f"live_input takes plain text or bytes, not {type(item).__name__}")
    return value


def episode_ids_of_commitments(cur: Any, ids: Sequence[Any]) -> dict[str, list[int]]:
    """Source episode ids per commitment id: the episode it was opened in and
    the one that closed it. A lookup that fails returns nothing, and a
    commitment missing from the answer has an unknown source (withheld)."""
    # forget-fence: source-id lookup for the chokepoint; returns episode ids, no text
    wanted = _ids(ids)
    if not wanted:
        return {}
    try:
        cur.execute("SELECT id, opened_episode, closed_episode FROM commitments WHERE id = ANY(%s)", (wanted,))
        return {str(r[0]): _ids([r[1], r[2]]) for r in cur.fetchall()}
    except Exception:  # noqa: BLE001 - unknown sources are withheld
        return {}


def episode_ids_of_topics(cur: Any, slugs: Sequence[str]) -> dict[str, list[int]]:
    """Every episode (live or not) that carries each topic slug: a topic page
    aggregates them, so one forgotten source withholds the whole topic."""
    # forget-fence: source-id lookup for the chokepoint; returns episode ids, no text
    wanted = [str(s) for s in slugs]
    if not wanted:
        return {}
    out: dict[str, list[int]] = {s: [] for s in wanted}
    try:
        cur.execute("SELECT t.slug, e.id FROM topics t JOIN episodes e ON e.topics ? t.slug "
                    "WHERE t.slug = ANY(%s)", (wanted,))
        for slug, eid in cur.fetchall():
            out.setdefault(str(slug), []).append(int(eid))
        uncarried = [s for s, found in out.items() if not found]
        if uncarried:
            # A topic page no episode ever carried (the hub never hard-deletes
            # episodes, forgotten ones included) is an owner-written file, like
            # a note: nothing in it derives from a capture. A slug with no
            # topic row stays unproven and is withheld.
            cur.execute("SELECT slug FROM topics WHERE slug = ANY(%s)", (uncarried,))
            pages = {str(row[0]) for row in cur.fetchall()}
            for slug in uncarried:
                if slug in pages or slug.startswith("note:"):
                    out[slug] = [NOTE_ORIGIN]
        return out
    except Exception:  # noqa: BLE001 - unknown sources are withheld
        return {}


def _count(kind: str, offered: int, withheld: int, *, readable: bool,
           unprovenanced: int = 0, bad_source_id: int = 0) -> None:
    _log.info("provider text kind=%s offered=%d withheld=%d list_readable=%s "
              "unprovenanced=%d bad_source_id=%d",
              kind, offered, withheld, readable, unprovenanced, bad_source_id)
