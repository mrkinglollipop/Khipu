"""Bounded graph-candidate expansion (Phase 3, session A; "BUILD — graph
candidates", docs/plans/2026-09-27-memory-reasoning-scope.md).

Two backends behind one shape, not one shared query: the hub (PostgreSQL,
``hub_candidates``) serves explicit search; the replica (SQLite,
``replica_candidates``) serves the local prompt lane and the stale-replica
payload. Both take the caller's already-fused list, seed from its top 5
rows, and return additional candidate rows plus whether the leg missed its
own deadline. Finding 7 (docs/research/hindsight-plan-review-2026-09-28.md):
no episode nodes, no persisted episode-to-topic edge, and never the unstable
SQL/PGQ path — every expansion below is a plain join or EXISTS/unnest
against ``edges``/``topics``/``episodes``.

Expansions, each a plain join:
  (a) topic seed  -> topics it links to/from via ``wiki_link``
  (b) episode seed -> topic pages named in its own ``topics`` array
  (c) topic seed  -> most recent episodes that name it (the reverse of (b))

The hub runs all three for every seed as one statement (it is across the
network, so round trips are the cost; see the hub section); a timeout
reports ``missed`` with no candidates. The replica runs them seed by seed,
serving (c) from an in-process slug map it builds in the background, and
keeps finished expansions when a later one misses.

A caller is responsible for the ``graph_candidates`` switch check
(``khipu.features.enabled``) and for feeding the result back into
``search_text.fuse_ranked_lists`` as one more ranked list — never score
arithmetic (finding 8: a score-space boost collapsed recall in Hindsight).
"""
from __future__ import annotations

import json
import sqlite3
import threading
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


def _remaining_ms(deadline: float) -> int:
    _check_deadline(deadline)
    return max(1, int((deadline - time.monotonic()) * 1000))


class _SqliteDeadlineConnection:
    """Connection proxy that bounds each lock wait by the remaining budget."""

    def __init__(self, con: Any, deadline: float):
        self._con, self._deadline = con, deadline

    def execute(self, sql: str, params: Any = ()):
        self._con.execute(f"PRAGMA busy_timeout={_remaining_ms(self._deadline)}")
        return self._con.execute(sql, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._con, name)


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
#
# The hub is usually across the network (Tailscale from a Mac: 40-50 ms a
# round trip, spikes past 150 ms). Measured on the live hub 2026-11-07, every
# query this leg ran took under 40 ms on the server, yet the leg missed its
# 400 ms deadline on every search: it ran two round trips per query (the
# per-query statement_timeout and the query), two to four queries per seed,
# up to five seeds, and fetched every wiki neighbour's body (12,500 for the
# `khipu` topic) to keep three. So the whole leg is now one statement for
# every seed and all three expansions, bounded per seed in SQL, pipelined
# with its setup, then one cleanup: two round trips whatever the seeds.

# Python's ``topic_graph.topic_slug_from_label`` in SQL: lowercase, runs of
# anything but a-z0-9 to "-", trim "-", cut at 80. Every non-ASCII character
# becomes "-" either way, so the two agree for every label except the two
# non-ASCII letters that lowercase into ASCII (the Kelvin sign and the dotted
# capital I) — the same caveat the replica's LIKE prefilter documents.
_PG_SLUG = "left(btrim(regexp_replace(lower({x}), '[^a-z0-9]+', '-', 'g'), '-'), 80)"
# ``topic_graph.peel_topic_id``: trim, then drop one "topic:"/"memory_topic:".
_PG_PEEL = "regexp_replace(btrim({x}), '^(topic:|memory_topic:)', '')"
# A JSONB ``topics`` value that is not an array names nothing (and must not
# make jsonb_array_elements_text raise for the whole leg).
_PG_TOPIC_LABELS = (
    "jsonb_array_elements_text(CASE WHEN jsonb_typeof({x}) = 'array' "
    "THEN {x} ELSE '[]'::jsonb END)"
)
# Rows kept per seed and relation in SQL. _select_candidates keeps at most
# PER_SEED_CAP per seed after dropping rows an earlier seed already took (at
# most TOTAL_CAP), so this many always leaves it the same choice it would
# make from the full list.
_HUB_PER_SEED_ROWS = TOTAL_CAP + PER_SEED_CAP
# The reverse lookup (c) reads only the most recent live episodes (older ones
# are a rarer target and each slug keeps just PER_SEED_CAP of them anyway).
_HUB_RECENT_EPISODES = 2000
_HUB_SAVEPOINT = "khipu_graph_candidates_deadline"


def _hub_candidates_sql(
    seeds: Sequence[tuple[int, Mapping[str, Any]]],
    exclude: set[tuple[str, str]],
    *,
    live_episodes: bool,
) -> tuple[str, dict[str, Any]]:
    """One statement answering every seed: (a) and (c) for topic seeds, (b)
    for episode seeds. Each row is ``(seed_rank, seed_kind, seed_id, relation,
    kind, id, title, snippet, recency_epoch)``."""
    from khipu import topic_graph

    t_rank, t_sid, t_a1, t_a2, t_a3 = [], [], [], [], []
    e_rank, e_sid = [], []
    for rank, seed in seeds:
        sid = str(seed.get("id"))
        if seed.get("kind") == "topic":
            aliases = topic_graph.topic_aliases(sid) or [sid]
            aliases = (aliases + [aliases[0]] * 3)[:3]
            t_rank.append(rank)
            t_sid.append(sid)
            t_a1.append(aliases[0])
            t_a2.append(aliases[1])
            t_a3.append(aliases[2])
        else:
            e_rank.append(rank)
            e_sid.append(sid)
    params: dict[str, Any] = {
        "t_rank": t_rank, "t_sid": t_sid, "t_a1": t_a1, "t_a2": t_a2, "t_a3": t_a3,
        "e_rank": e_rank, "e_sid": e_sid,
        "ex_topics": sorted(i for k, i in exclude if k == "topic"),
        "ex_episodes": sorted(i for k, i in exclude if k == "episode"),
        "wiki": topic_graph.WIKI_EDGE, "capture": _CAPTURE_TOPIC_RELATION,
        "cap": _HUB_PER_SEED_ROWS, "recent": _HUB_RECENT_EPISODES,
    }
    # (c) can only match a seed id that is itself a slug (a "note:..." id
    # never is). Every whole word of a slug is in its label as written, so
    # ILIKE on the episode's topics text discards most recent episodes
    # before any slug is computed, as the replica's prefilter does.
    c_sids = [sid for sid in t_sid if sid and topic_graph.topic_slug_from_label(sid) == sid]
    words = [_slug_prefilter_word(sid) for sid in c_sids]
    params["c_sid"] = c_sids
    params["c_like"] = [f"%{w}%" for w in words] if all(words) else []
    live = "deleted_at IS NULL" if live_episodes else "TRUE"
    ctes = [
        "tseed AS (SELECT * FROM unnest(%(t_rank)s::int[], %(t_sid)s::text[], "
        "%(t_a1)s::text[], %(t_a2)s::text[], %(t_a3)s::text[]) AS s(rank, sid, a1, a2, a3))",
        "eseed AS (SELECT * FROM unnest(%(e_rank)s::int[], %(e_sid)s::text[]) AS s(rank, sid))",
    ]
    topic_cols = (
        "t.slug, t.title, left(t.body, 4000), "
        "coalesce(extract(epoch FROM coalesce(t.updated_at, t.created_at)), 0)::float8"
    )
    parts = []
    if t_rank:
        # (a) topic seed -> topics it links to/from via wiki_link.
        parts.append(
            "SELECT s.rank, 'topic'::text, s.sid, %(wiki)s::text, 'topic'::text, n.* FROM tseed s "
            "CROSS JOIN LATERAL ("
            f"SELECT {topic_cols} AS rec FROM topics t WHERE t.slug IN ("
            f"SELECT {_PG_PEEL.format(x='e.dst')} FROM edges e "
            "WHERE e.src IN (s.a1, s.a2, s.a3) AND e.type = %(wiki)s "
            f"UNION SELECT {_PG_PEEL.format(x='e.src')} FROM edges e "
            "WHERE e.dst IN (s.a1, s.a2, s.a3) AND e.type = %(wiki)s) "
            "AND t.deleted_at IS NULL AND t.slug <> s.sid "
            "AND t.slug <> ALL(%(ex_topics)s::text[]) "
            "ORDER BY rec DESC LIMIT %(cap)s) n"
        )
    if c_sids:
        # (c) topic seed -> the most recent live episodes that name it.
        prefilter = " AND r.topics::text ILIKE ANY(%(c_like)s::text[])" if params["c_like"] else ""
        ctes.append(
            "recent AS MATERIALIZED (SELECT id, ts, left(summary, 4000) AS summary, topics "
            f"FROM episodes WHERE {live} ORDER BY ts DESC LIMIT %(recent)s)"
        )
        ctes.append(
            "named AS MATERIALIZED (SELECT DISTINCT r.id, r.ts, r.summary, l.slug FROM recent r "
            f"CROSS JOIN LATERAL (SELECT {_PG_SLUG.format(x='lbl')} AS slug "
            f"FROM {_PG_TOPIC_LABELS.format(x='r.topics')} AS lbl) l "
            f"WHERE l.slug = ANY(%(c_sid)s::text[]){prefilter})"
        )
        parts.append(
            "SELECT s.rank, 'topic'::text, s.sid, %(capture)s::text, 'episode'::text, x.* FROM tseed s "
            "CROSS JOIN LATERAL (SELECT n.id::text, NULL::text, n.summary, "
            "coalesce(extract(epoch FROM n.ts), 0)::float8 AS rec FROM named n "
            "WHERE n.slug = s.sid AND n.id::text <> ALL(%(ex_episodes)s::text[]) "
            "ORDER BY n.ts DESC LIMIT %(cap)s) x"
        )
    if e_rank:
        # (b) episode seed -> topic pages named in its own topics array. A
        # forgotten seed episode links nothing, like the replica's live view.
        ep_live = " AND ep.deleted_at IS NULL" if live_episodes else ""
        parts.append(
            "SELECT s.rank, 'episode'::text, s.sid, %(capture)s::text, 'topic'::text, x.* FROM eseed s "
            f"JOIN episodes ep ON ep.id::text = s.sid{ep_live} "
            f"CROSS JOIN LATERAL (SELECT {topic_cols} AS rec FROM topics t WHERE t.slug IN ("
            f"SELECT {_PG_SLUG.format(x='lbl')} FROM {_PG_TOPIC_LABELS.format(x='ep.topics')} AS lbl) "
            "AND t.deleted_at IS NULL AND t.slug <> ALL(%(ex_topics)s::text[]) "
            "ORDER BY rec DESC LIMIT %(cap)s) x"
        )
    sql = "WITH " + ", ".join(ctes) + " " + " UNION ALL ".join(parts)
    return sql, params


def _hub_rows_to_raw(rows: Sequence[Sequence[Any]]) -> list[dict[str, Any]]:
    out = []
    for rank, skind, sid, relation, kind, rid, title, snippet, recency in rows:
        snippet = snippet or ""
        label = (title or rid) if kind == "topic" else snippet[:200]
        out.append({
            "kind": kind, "id": str(rid), "label": label, "snippet": snippet[:4000],
            "_seed_rank": int(rank), "_seed_key": f"{skind}:{sid}",
            "_relation": relation, "_recency": float(recency or 0.0),
        })
    return out


def _is_timeout(exc: BaseException, deadline: float) -> bool:
    return (
        time.monotonic() >= deadline
        or exc.__class__.__name__ in {"QueryCanceled", "QueryCanceledError"}
        or "statement timeout" in str(exc).lower()
    )


def _pipelined(cur):
    """psycopg's pipeline mode on a real connection (the savepoint, the
    timeout and the statement leave in one flush: one round trip, not three);
    a no-op for anything else, so a stand-in cursor runs the same calls in
    order."""
    import contextlib

    try:
        import psycopg

        conn = getattr(cur, "connection", None)
        if isinstance(conn, psycopg.Connection) and psycopg.Pipeline.is_supported():
            return conn.pipeline()
    except Exception:  # noqa: BLE001 — no pipeline is only one more round trip
        pass
    return contextlib.nullcontext()


def hub_candidates(
    cur, fused: Sequence[Mapping[str, Any]], *, deadline: float
) -> tuple[list[dict[str, Any]], bool]:
    # forget-fence source: per-prompt push candidates; prior_work_for_prompt fences them
    """``(candidates, missed)`` for the hub (PostgreSQL) leg. ``deadline`` is
    an absolute ``time.monotonic()`` value the caller computes (typically
    ``time.monotonic() + HUB_LEG_DEADLINE_S``), so a test can pass an
    already-expired one to force the miss path deterministically.

    Two round trips whatever the seeds: a pipelined savepoint, the remaining
    budget as a local ``statement_timeout`` and the one statement from
    ``_hub_candidates_sql``; then ``ROLLBACK TO``/``RELEASE`` of the
    savepoint (the rollback also undoes the timeout; the leg only reads). A
    timeout reports ``missed`` with no candidates."""
    from khipu.db import has_columns

    exclude = {(str(r.get("kind")), str(r.get("id"))) for r in fused}
    seeds = _seeds(fused)
    if not seeds:
        return [], False
    try:
        _check_deadline(deadline)
        live_episodes = has_columns(cur, "episodes", "deleted_at")
        sql, params = _hub_candidates_sql(seeds, exclude, live_episodes=live_episodes)
        timeout = f"{_remaining_ms(deadline)}ms"
    except DeadlineMissed:
        return [], True
    savepoint_started = False
    try:
        with _pipelined(cur):
            cur.execute(f"SAVEPOINT {_HUB_SAVEPOINT}")
            savepoint_started = True
            cur.execute("SELECT set_config('statement_timeout', %s, true)", (timeout,))
            cur.execute(sql, params)
            rows = cur.fetchall()
    except Exception as exc:
        if _is_timeout(exc, deadline):
            return [], True
        raise
    finally:
        if savepoint_started:
            cur.execute(
                f"ROLLBACK TO SAVEPOINT {_HUB_SAVEPOINT}; RELEASE SAVEPOINT {_HUB_SAVEPOINT}"
            )
    return _select_candidates(_hub_rows_to_raw(rows), exclude=exclude), False


# ---- replica (SQLite) ---------------------------------------------------


def _replica_topic_wiki(con, slug: str, *, seed_rank: int, seed_key: str) -> list[dict[str, Any]]:
    # forget-fence source: per-prompt push candidates; prior_work_for_prompt fences them
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
    # forget-fence source: per-prompt push candidates; prior_work_for_prompt fences them
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


# slug -> its PER_SEED_CAP most recent live episodes as (ts, id, summary), for
# the replica identity in "key". Turning every topic label into a slug costs
# about 45,000 Python calls, and on each prompt that was a fifth of the
# per-prompt lane's budget on a busy Mac (2026-10-12); a long-lived process
# pays it once per replica generation instead.
_SLUG_LOCK = threading.Lock()
_SLUG_CACHE: dict[str, Any] = {}
_SLUG_RELOADING = False


def _slug_map_key() -> tuple[Any, ...] | None:
    from khipu import hub_snapshot

    path = hub_snapshot.snapshot_path()
    try:
        st = path.stat()
    except OSError:
        return None
    # Pending erasures hide episodes without touching the file.
    pending = frozenset(hub_snapshot._pending_erasure_ids(path))
    return (str(path), st.st_mtime_ns, st.st_size, pending)


def _build_slug_map() -> dict[str, list[tuple[Any, Any, Any]]]:
    # forget-fence source: per-prompt push candidates; prior_work_for_prompt fences them
    from khipu import hub_snapshot, topic_graph

    con = hub_snapshot.open_snapshot()
    try:
        live = "WHERE deleted_at IS NULL " if "deleted_at" in hub_snapshot._snapshot_table_columns(con, "episodes") else ""
        rows = con.execute(f"SELECT id, ts, summary, topics FROM episodes {live}ORDER BY ts DESC").fetchall()
    finally:
        con.close()
    out: dict[str, list[tuple[Any, Any, Any]]] = {}
    for eid, ts, summary, topics in rows:
        try:
            labels = json.loads(topics) if topics else []
        except (ValueError, TypeError):
            continue
        for slug in {topic_graph.topic_slug_from_label(str(label)) for label in labels or []}:
            bucket = out.setdefault(slug, [])
            if len(bucket) < PER_SEED_CAP:
                bucket.append((ts, eid, summary))
    return out


def _rebuild_slug_map(key: tuple[Any, ...]) -> None:
    # forget-fence: caches the slug map the candidate builders read; returns nothing
    global _SLUG_RELOADING
    try:
        by_slug = _build_slug_map()
    except Exception:  # noqa: BLE001 — the stale map keeps serving
        by_slug = None
    with _SLUG_LOCK:
        if by_slug is not None:
            _SLUG_CACHE.update({"key": key, "map": by_slug})
        _SLUG_RELOADING = False


def _episode_slug_map() -> dict[str, list[tuple[Any, Any, Any]]] | None:
    """The cached map, serving the previous generation (and rebuilding it on
    one daemon thread) when the replica has moved. None while there is no
    generation yet or it cannot be built: the SQL path runs. The first build
    also runs on a thread: a one-shot hook has exactly one prompt, and a
    270 ms build on it would cost more than the join it replaces."""
    global _SLUG_RELOADING
    key = _slug_map_key()
    if key is None:
        return None
    with _SLUG_LOCK:
        cached = _SLUG_CACHE.get("key")
        if cached == key:
            return _SLUG_CACHE["map"]
        if not _SLUG_RELOADING:
            _SLUG_RELOADING = True
            threading.Thread(target=_rebuild_slug_map, args=(key,), daemon=True).start()
        if cached is not None and cached[0] == key[0]:
            return _SLUG_CACHE["map"]
        return None


def _replica_topic_recent_episodes(con, slug: str, *, seed_rank: int, seed_key: str) -> list[dict[str, Any]]:
    # forget-fence source: per-prompt push candidates; prior_work_for_prompt fences them
    from khipu.hub_snapshot import _snapshot_table_columns
    from khipu import topic_graph

    by_slug = _episode_slug_map()
    if by_slug is not None:
        cached = by_slug.get(slug, [])
        # The map can be a generation behind the replica, built before an
        # episode was forgotten: keep only ids the replica's live view still
        # holds, or the forgotten episode's summary comes back as a candidate.
        live: set[str] = set()
        if cached:
            ids = [str(eid) for _ts, eid, _summary in cached]
            live = {str(row[0]) for row in con.execute(
                f"SELECT CAST(id AS TEXT) FROM episodes WHERE CAST(id AS TEXT) IN "
                f"({', '.join('?' * len(ids))})", ids).fetchall()}
        return [
            _recent_episode_row(eid, ts, summary, seed_rank=seed_rank, seed_key=seed_key)
            for ts, eid, summary in cached if str(eid) in live
        ]
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
    return [
        _recent_episode_row(eid, ts, summary, seed_rank=seed_rank, seed_key=seed_key)
        for eid, ts, summary in rows
    ]


def _recent_episode_row(eid, ts, summary, *, seed_rank: int, seed_key: str) -> dict[str, Any]:
    return {
        "kind": "episode", "id": str(eid), "label": (summary or "")[:200],
        "snippet": (summary or "")[:4000],
        "_seed_rank": seed_rank, "_seed_key": seed_key,
        "_relation": _CAPTURE_TOPIC_RELATION,
        "_recency": _ts_key(ts),
    }


def replica_candidates(
    con, fused: Sequence[Mapping[str, Any]], *, deadline: float
) -> tuple[list[dict[str, Any]], bool]:
    # forget-fence source: per-prompt push candidates; prior_work_for_prompt fences them
    """``(candidates, missed)`` for the replica (SQLite) leg — same contract
    as ``hub_candidates``, see its docstring for the deadline convention."""
    exclude = {(str(r.get("kind")), str(r.get("id"))) for r in fused}
    seeds = _seeds(fused)
    if not seeds:
        return [], False
    from khipu import hub_snapshot

    hub_snapshot.ensure_edge_indexes()
    previous_busy = int(con.execute("PRAGMA busy_timeout").fetchone()[0])
    bounded_con = _SqliteDeadlineConnection(con, deadline)
    try:
        con.execute(f"PRAGMA busy_timeout={_remaining_ms(deadline)}")
        # sqlite3 exposes no getter for an existing progress handler; callers lend
        # a connection with no handler installed, and this leg always clears its
        # own handler before returning.
        con.set_progress_handler(lambda: 1 if time.monotonic() >= deadline else 0, 100)
        raw: list[dict[str, Any]] = []
        missed = False
        for rank, seed in seeds:
            try:
                _check_deadline(deadline)
                skind, sid = seed.get("kind"), str(seed.get("id"))
                seed_key = f"{skind}:{sid}"
                if skind == "topic":
                    raw.extend(_replica_topic_wiki(bounded_con, sid, seed_rank=rank, seed_key=seed_key))
                    _check_deadline(deadline)
                    raw.extend(_replica_topic_recent_episodes(
                        bounded_con, sid, seed_rank=rank, seed_key=seed_key))
                else:
                    raw.extend(_replica_episode_topics(
                        bounded_con, sid, seed_rank=rank, seed_key=seed_key))
                _check_deadline(deadline)
            except DeadlineMissed:
                missed = True
                break
            except sqlite3.OperationalError as exc:
                if any(token in str(exc).lower() for token in ("interrupted", "locked", "busy")):
                    missed = True
                    break
                raise
    except DeadlineMissed:
        return [], True
    finally:
        con.set_progress_handler(None, 0)
        con.execute(f"PRAGMA busy_timeout={previous_busy}")
    return _select_candidates(raw, exclude=exclude), missed
