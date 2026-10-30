"""The one chokepoint for text that leaves this Mac for an embedding,
summariser, reflect, rerank or graph provider.

Every function that calls a provider client must get its text from here:
``fenced_texts`` for text derived from stored episodes (embeddings of episodes,
commitments and topics, briefs, reflect, rerank, hygiene judging) and
``live_input`` for input that is not stored memory (a typed query, a probe, a
transcript being captured, a library chunk, an image). The guard in
``tests/test_forget_fence.py`` fails any provider caller that calls neither.

Fencing is by source episode: a text is withheld when ANY episode it derives
from is on this Mac's forget list or queued for erasure. When the list cannot
be read nothing episode-derived goes out. Only counts are logged.
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
# Input that is not stored memory: nothing to fence by episode.
LIVE_KINDS = frozenset({"query", "probe", "transcript", "library", "payload", "media"})


class SourcedText(str):
    """A text that carries the episode ids it derives from (``sources``), so a
    function that only receives strings can still reach the fence."""

    sources: tuple[int, ...] | None  # None: the source is unknown (withheld)

    def __new__(cls, text: Any, sources: Iterable[Any] | None = ()):
        obj = super().__new__(cls, text or "")
        obj.sources = None if sources is None else tuple(_ids(sources))
        return obj


def sourced(text: Any, sources: Iterable[Any] | None = ()) -> SourcedText:
    return SourcedText(text, sources)


def _ids(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        value = [value]
    out: list[int] = []
    for item in value:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def fenced_texts(texts: Sequence[Any], source_ids: Sequence[Any] | None, kind: str) -> list[Any]:
    """``texts`` with each withheld entry replaced by None, same length and
    order. ``source_ids[i]`` is the episode id (or ids) text ``i`` derives
    from; None reads them from each text's ``SourcedText.sources``. A text
    with no known source (neither an id nor a ``SourcedText``) cannot be
    proven live, so it is withheld too. An unreadable forget list withholds
    every text."""
    from khipu import tombstones

    if kind not in DERIVED_KINDS:
        raise ValueError(f"fenced_texts is for episode-derived kinds, not {kind!r}")
    items = list(texts)
    if source_ids is not None and len(source_ids) != len(items):
        raise ValueError("source_ids must match texts")
    groups: list[list[int] | None] = []
    for i, text in enumerate(items):
        if source_ids is not None:
            groups.append(None if source_ids[i] is None else _ids(source_ids[i]))
        elif isinstance(text, SourcedText):
            groups.append(None if text.sources is None else list(text.sources))
        else:
            groups.append(None)
    rows = [{"kind": "episode", "id": eid, "slot": slot}
            for slot, ids in enumerate(groups) for eid in (ids or [])]
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
    bad = {r["slot"] for r in rows if id(r) not in kept_rows}
    out = [None if (groups[i] is None or i in bad) else items[i] for i in range(len(items))]
    _count(kind, len(items), sum(1 for t in out if t is None), readable=True)
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
            sources: list[Any] = [_ids(r) for r in refs]
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


def live_input(value: Any, kind: str) -> Any:
    """``value`` unchanged: input that is not stored memory. Episode-derived
    kinds are refused so they cannot use this door."""
    if kind not in LIVE_KINDS:
        raise ValueError(f"live_input is for input outside stored memory, not {kind!r}")
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
        return out
    except Exception:  # noqa: BLE001 - unknown sources are withheld
        return {}


def _count(kind: str, offered: int, withheld: int, *, readable: bool) -> None:
    _log.info("provider text kind=%s offered=%d withheld=%d list_readable=%s",
              kind, offered, withheld, readable)
