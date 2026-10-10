# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""Bounded graph-candidate expansion (Phase 3, session A; "BUILD — graph
candidates", docs/plans/2026-09-27-memory-reasoning-scope.md).

Two backends behind one shape, not one shared query: the hub (PostgreSQL,
``hub_candidates``) serves explicit search; the replica (SQLite,
``replica_candidates``) serves the local prompt lane and the stale-replica
payload. Both take the caller's already-fused list, seed from its top 5
rows, and return additional candidate rows plus whether the leg missed its
own deadline — never a partial leg (finding 7, docs/research/hindsight-
plan-review-2026-09-28.md: no episode nodes, no persisted episode-to-topic
edge, and never the unstable SQL/PGQ path — every expansion below is a
plain join or EXISTS/unnest against ``edges``/``topics``/``episodes``).

Expansions, each a plain join:
  (a) topic seed  -> topics it links to/from via ``wiki_link``
  (b) episode seed -> topic pages named in its own ``topics`` array
  (c) topic seed  -> most recent episodes that name it (the reverse of (b);
      there is no persisted index for this direction, so the join text-
      matches the same slug form ``topic_graph.topic_slug_from_label``
      produces — one Python function on the replica leg, via
      ``sqlite3.Connection.create_function``; a hand-mirrored SQL expression
      on the hub leg, which cannot call into Python mid-query)

A caller is responsible for the ``graph_candidates`` switch check
(``khipu.features.enabled``) and for feeding the result back into
``search_text.fuse_ranked_lists`` as one more ranked list — never score
arithmetic (finding 8: a score-space boost collapsed recall in Hindsight).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

SEED_CAP = 5
TOTAL_CAP = 8
PER_SEED_CAP = 3
REPLICA_LEG_DEADLINE_S = 0.150
HUB_LEG_DEADLINE_S = 0.400

_CAPTURE_TOPIC_RELATION = "capture_topic"
_RELATION_ORDER = ("wiki_link", _CAPTURE_TOPIC_RELATION)
_SEED_KINDS = ("topic", "episode")


class DeadlineMissed(Exception):
    """Raised internally when the leg's own deadline is already behind."""


def _check_deadline(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise DeadlineMissed()


def _ts_key(value: Any) -> float:
    """Best-effort epoch seconds for recency ordering; 0.0 (oldest) for
    anything unparseable rather than raising — recency is a tiebreak, never
    a requirement."""
    if value is None:
        return 0.0
    if hasattr(value, "timestamp"):
        try:
            return value.timestamp()
        except (OverflowError, OSError, ValueError):
            return 0.0
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _select_candidates(
    raw: list[dict[str, Any]], *, exclude: set[tuple[str, str]]
) -> list[dict[str, Any]]:
    """Apply exclusion, per-seed/total caps and the (seed rank, relation
    kind, recency) order to raw candidate rows. Each item of ``raw`` carries
    the public ``kind``/``id``/``label``/``snippet`` fields plus internal
    ``_seed_rank``/``_seed_key``/``_relation``/``_recency`` — stripped here,
    where ``_seed_key``/``_relation`` become the public ``via`` dict."""
    ordered = sorted(
        raw,
        key=lambda r: (
            r["_seed_rank"],
            _RELATION_ORDER.index(r["_relation"])
            if r["_relation"] in _RELATION_ORDER
            else len(_RELATION_ORDER),
            -r["_recency"],
        ),
    )
    per_seed: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for row in ordered:
        key = (str(row.get("kind")), str(row.get("id")))
        if key in exclude or key in seen:
            continue
        seed_key = row["_seed_key"]
        if per_seed.get(seed_key, 0) >= PER_SEED_CAP:
            continue
        if len(out) >= TOTAL_CAP:
            break
        seen.add(key)
        per_seed[seed_key] = per_seed.get(seed_key, 0) + 1
        clean = {k: v for k, v in row.items() if not k.startswith("_")}
        clean["via"] = {"seed": seed_key, "relation": row["_relation"]}
        out.append(clean)
    return out


def _seeds(fused: Sequence[Mapping[str, Any]]) -> list[tuple[int, Mapping[str, Any]]]:
    return [
        (i, r) for i, r in enumerate(fused[:SEED_CAP]) if r.get("kind") in _SEED_KINDS
    ]


# ---- hub (PostgreSQL) --------------------------------------------------


def _hub_topic_wiki(cur, slug: str, *, seed_rank: int, seed_key: str) -> list[dict[str, Any]]:
    from khipu import topic_graph

    aliases = topic_graph.topic_aliases(slug)
    cur.execute(
        "SELECT dst FROM edges WHERE src = ANY(%(a)s) AND type = %(t)s "
        "UNION "
        "SELECT src FROM edges WHERE dst = ANY(%(a)s) AND type = %(t)s",
        {"a": aliases, "t": topic_graph.WIKI_EDGE},
    )
    others = {topic_graph.peel_topic_id(r[0]) for r in cur.fetchall()}
    others.discard(slug)
    if not others:
        return []
    cur.execute(
        "SELECT slug, title, body, updated_at, created_at FROM topics "
        "WHERE slug = ANY(%(s)s) AND deleted_at IS NULL",
        {"s": sorted(others)},
    )
    out = []
    for oslug, title, body, updated_at, created_at in cur.fetchall():
        out.append({
            "kind": "topic", "id": oslug, "label": title or oslug,
            "snippet": (body or "")[:4000],
            "_seed_rank": seed_rank, "_seed_key": seed_key,
            "_relation": topic_graph.WIKI_EDGE,
            "_recency": _ts_key(updated_at or created_at),
        })
    return out


def _hub_topic_recent_episodes(cur, slug: str, *, seed_rank: int, seed_key: str) -> list[dict[str, Any]]:
    from khipu.db import has_columns

    live = "deleted_at IS NULL AND " if has_columns(cur, "episodes", "deleted_at") else ""
    # Mirrors khipu.topic_graph.topic_slug_from_label byte-for-byte: lower,
    # collapse a run of non-alphanumerics to one '-', strip leading/trailing
    # '-', cap at 80 chars. This leg cannot call the Python function
    # mid-query (unlike the replica leg below, via create_function), so the
    # two must be kept in sync by hand — a mismatch here only misses a
    # candidate, it can never mint or corrupt anything. ``episodes.topics``
    # is JSONB (migration 0001), not a native array, so this unnests with
    # ``jsonb_array_elements_text`` rather than ``unnest``.
    cur.execute(
        f"SELECT id, ts, summary FROM episodes e WHERE {live} EXISTS ("
        "  SELECT 1 FROM jsonb_array_elements_text(e.topics) AS label WHERE "
        "  left(trim(both '-' from regexp_replace(lower(trim(label)), "
        "  '[^a-z0-9]+', '-', 'g')), 80) = %(slug)s"
        ") ORDER BY ts DESC LIMIT %(cap)s",
        {"slug": slug, "cap": PER_SEED_CAP},
    )
    out = []
    for eid, ts, summary in cur.fetchall():
        out.append({
            "kind": "episode", "id": str(eid), "label": (summary or "")[:200],
            "snippet": (summary or "")[:4000],
            "_seed_rank": seed_rank, "_seed_key": seed_key,
            "_relation": _CAPTURE_TOPIC_RELATION,
            "_recency": _ts_key(ts),
        })
    return out


def _hub_episode_topics(cur, episode_id: str, *, seed_rank: int, seed_key: str) -> list[dict[str, Any]]:
    from khipu import topic_graph

    cur.execute("SELECT topics FROM episodes WHERE id::text = %s", (episode_id,))
    row = cur.fetchone()
    if row is None or not row[0]:
        return []
    slugs: list[str] = []
    for label in row[0]:
        s = topic_graph.topic_slug_from_label(str(label))
        if s and s not in slugs:
            slugs.append(s)
    if not slugs:
        return []
    cur.execute(
        "SELECT slug, title, body, updated_at, created_at FROM topics "
        "WHERE slug = ANY(%(s)s) AND deleted_at IS NULL",
        {"s": slugs},
    )
    out = []
    for oslug, title, body, updated_at, created_at in cur.fetchall():
        out.append({
            "kind": "topic", "id": oslug, "label": title or oslug,
            "snippet": (body or "")[:4000],
            "_seed_rank": seed_rank, "_seed_key": seed_key,
            "_relation": _CAPTURE_TOPIC_RELATION,
            "_recency": _ts_key(updated_at or created_at),
        })
    return out


def hub_candidates(
    cur, fused: Sequence[Mapping[str, Any]], *, deadline: float
) -> tuple[list[dict[str, Any]], bool]:
    """``(candidates, missed)`` for the hub (PostgreSQL) leg. ``deadline`` is
    an absolute ``time.monotonic()`` value the caller computes (typically
    ``time.monotonic() + HUB_LEG_DEADLINE_S``), so a test can pass an
    already-expired one to force the miss path deterministically. A miss
    drops the WHOLE leg — ``candidates`` is always ``[]`` when
    ``missed`` is ``True``, never a partial list."""
    exclude = {(str(r.get("kind")), str(r.get("id"))) for r in fused}
    seeds = _seeds(fused)
    if not seeds:
        return [], False
    try:
        raw: list[dict[str, Any]] = []
        for rank, seed in seeds:
            _check_deadline(deadline)
            skind, sid = seed.get("kind"), str(seed.get("id"))
            seed_key = f"{skind}:{sid}"
            if skind == "topic":
                raw.extend(_hub_topic_wiki(cur, sid, seed_rank=rank, seed_key=seed_key))
                _check_deadline(deadline)
                raw.extend(_hub_topic_recent_episodes(cur, sid, seed_rank=rank, seed_key=seed_key))
            else:
                raw.extend(_hub_episode_topics(cur, sid, seed_rank=rank, seed_key=seed_key))
            _check_deadline(deadline)
    except DeadlineMissed:
        return [], True
    return _select_candidates(raw, exclude=exclude), False


# ---- replica (SQLite) ---------------------------------------------------


def _replica_topic_wiki(con, slug: str, *, seed_rank: int, seed_key: str) -> list[dict[str, Any]]:
    from khipu import topic_graph

    aliases = topic_graph.topic_aliases(slug)
    ph = ",".join("?" * len(aliases))
    rows = con.execute(
        f"SELECT dst FROM edges WHERE src IN ({ph}) AND type = ? "
        f"UNION "
        f"SELECT src FROM edges WHERE dst IN ({ph}) AND type = ?",
        (*aliases, topic_graph.WIKI_EDGE, *aliases, topic_graph.WIKI_EDGE),
    ).fetchall()
    others = {topic_graph.peel_topic_id(r[0]) for r in rows}
    others.discard(slug)
    if not others:
        return []
    others_sorted = sorted(others)
    ph2 = ",".join("?" * len(others_sorted))
    out = []
    for oslug, title, body, updated_at, created_at in con.execute(
        f"SELECT slug, title, body, updated_at, created_at FROM topics "
        f"WHERE slug IN ({ph2}) AND deleted_at IS NULL",
        tuple(others_sorted),
    ).fetchall():
        out.append({
            "kind": "topic", "id": oslug, "label": title or oslug,
            "snippet": (body or "")[:4000],
            "_seed_rank": seed_rank, "_seed_key": seed_key,
            "_relation": topic_graph.WIKI_EDGE,
            "_recency": _ts_key(updated_at or created_at),
        })
    return out


def _replica_episode_topics(con, episode_id: str, *, seed_rank: int, seed_key: str) -> list[dict[str, Any]]:
    import json

    from khipu import topic_graph

    row = con.execute("SELECT topics FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    if row is None or not row[0]:
        return []
    try:
        labels = json.loads(row[0]) or []
    except (ValueError, TypeError):
        labels = []
    slugs: list[str] = []
    for label in labels:
        s = topic_graph.topic_slug_from_label(str(label))
        if s and s not in slugs:
            slugs.append(s)
    if not slugs:
        return []
    ph = ",".join("?" * len(slugs))
    out = []
    for oslug, title, body, updated_at, created_at in con.execute(
        f"SELECT slug, title, body, updated_at, created_at FROM topics "
        f"WHERE slug IN ({ph}) AND deleted_at IS NULL",
        tuple(slugs),
    ).fetchall():
        out.append({
            "kind": "topic", "id": oslug, "label": title or oslug,
            "snippet": (body or "")[:4000],
            "_seed_rank": seed_rank, "_seed_key": seed_key,
            "_relation": _CAPTURE_TOPIC_RELATION,
            "_recency": _ts_key(updated_at or created_at),
        })
    return out


def _slug_prefilter_word(slug: str) -> str:
    """The longest word of ``slug`` that is certain to appear in its label.

    A slug is cut at 80 characters, so its last word may be a fragment of the
    label's; any earlier word is whole. LIKE wildcards cannot occur: a slug
    holds only a-z, 0-9 and "-"."""
    from khipu import topic_graph

    words = [w for w in slug.split("-") if w]
    if len(slug) >= 80 and len(words) > 1:
        words = words[:-1]
    elif len(slug) >= 80:
        return ""
    if topic_graph.topic_slug_from_label(slug) != slug:
        return ""
    return max(words, key=len, default="")


def _replica_topic_recent_episodes(con, slug: str, *, seed_rank: int, seed_key: str) -> list[dict[str, Any]]:
    from khipu.hub_snapshot import _snapshot_table_columns
    from khipu import topic_graph

    # A single source of truth for the slug form (unlike the hub leg, which
    # cannot call into Python mid-query): registers the SAME function
    # topic_graph.persist_capture_graph already slugifies episode topics
    # with, so this join can never drift from it.
    con.create_function("khipu_topic_slug", 1, topic_graph.topic_slug_from_label)
    ep_cols = _snapshot_table_columns(con, "episodes")
    live = "e.deleted_at IS NULL AND " if "deleted_at" in ep_cols else ""
    # Calling back into Python for every topic of every episode was the single
    # largest cost of a per-prompt recall (about 64,000 calls, a third of the
    # search) and pushed it past its budget on a busy Mac. A slug is its label
    # lowercased with runs of other characters turned into "-", so every whole
    # word of the slug is in the label as written: let SQLite's own LIKE (which
    # ignores ASCII case) discard the episodes that cannot match first. (The
    # two non-ASCII letters that lowercase into ASCII, the Kelvin sign and the
    # dotted capital I, are the only labels this could miss.)
    word = _slug_prefilter_word(slug)
    prefilter, params = ("e.topics LIKE ? AND ", [f"%{word}%"]) if word else ("", [])
    rows = con.execute(
        f"SELECT e.id, e.ts, e.summary FROM episodes e, json_each(e.topics) je "
        f"WHERE {live}{prefilter}khipu_topic_slug(je.value) = ? "
        f"ORDER BY e.ts DESC LIMIT ?",
        (*params, slug, PER_SEED_CAP),
    ).fetchall()
    out = []
    for eid, ts, summary in rows:
        out.append({
            "kind": "episode", "id": str(eid), "label": (summary or "")[:200],
            "snippet": (summary or "")[:4000],
            "_seed_rank": seed_rank, "_seed_key": seed_key,
            "_relation": _CAPTURE_TOPIC_RELATION,
            "_recency": _ts_key(ts),
        })
    return out


def replica_candidates(
    con, fused: Sequence[Mapping[str, Any]], *, deadline: float
) -> tuple[list[dict[str, Any]], bool]:
    """``(candidates, missed)`` for the replica (SQLite) leg — same contract
    as ``hub_candidates``, see its docstring for the deadline convention."""
    exclude = {(str(r.get("kind")), str(r.get("id"))) for r in fused}
    seeds = _seeds(fused)
    if not seeds:
        return [], False
    try:
        raw: list[dict[str, Any]] = []
        for rank, seed in seeds:
            _check_deadline(deadline)
            skind, sid = seed.get("kind"), str(seed.get("id"))
            seed_key = f"{skind}:{sid}"
            if skind == "topic":
                raw.extend(_replica_topic_wiki(con, sid, seed_rank=rank, seed_key=seed_key))
                _check_deadline(deadline)
                raw.extend(_replica_topic_recent_episodes(con, sid, seed_rank=rank, seed_key=seed_key))
            else:
                raw.extend(_replica_episode_topics(con, sid, seed_rank=rank, seed_key=seed_key))
            _check_deadline(deadline)
    except DeadlineMissed:
        return [], True
    return _select_candidates(raw, exclude=exclude), False
