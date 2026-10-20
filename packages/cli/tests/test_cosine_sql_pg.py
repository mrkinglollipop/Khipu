"""Swarm-ready slice 3: the memory cosine statement, against a real PostgreSQL.

Scope: docs/plans/2026-10-19-swarm-ready.md (slice 3, "Kind-specific branches").
A hermetic hub (tests/hermetic_pg.py) is seeded with synthetic episodes, topics
and unit-ish random vectors under an HNSW expression index shaped like the
hub's. Never the live hub; every row is made up.

Three claims: the filtered plan has no per-vector rescan of ``episodes``; the
rows match what the old nested-OR statement returned; and the HNSW path recalls
the exact nearest neighbours.
"""
from __future__ import annotations

import contextlib
import random
from unittest import mock

import pytest

from khipu import db, embed
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401

DIM = 8
PROFILE = f"cosine-test@{DIM}"
N_EPISODES = 3000
N_TOPICS = 40


def _vec(rng: random.Random) -> list[float]:
    return [rng.gauss(0, 1) for _ in range(DIM)]


def _lit(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


@pytest.fixture
def seeded(hub, monkeypatch):  # noqa: F811
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    rng = random.Random(7)
    conn = hub.connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO embedding_profiles (id, provider, model, dim, is_active) "
        "VALUES (%s, 'local', 'test', %s, false)", (PROFILE, DIM))
    for i in range(N_EPISODES):
        project = "church-harness" if i % 5 == 0 else f"other-{i % 7}"
        cur.execute(
            "INSERT INTO episodes (ts, session_id, summary, raw, harness, project, deleted_at) "
            "VALUES (now() - make_interval(mins => %s), %s, %s, '{}'::jsonb, 'codex', %s, "
            "CASE WHEN %s THEN now() END) RETURNING id",
            (i, f"codex:s{i}", f"episode {i} about thing {i % 11}", project, i % 97 == 3))
        eid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO memory_embeddings (profile, kind, ref, chunk_idx, chunk_text, content_hash, embedding) "
            "VALUES (%s, 'episode', %s, 0, %s, 'h', %s::vector)",
            (PROFILE, str(eid), f"episode {eid} chunk", _lit(_vec(rng))))
    for i in range(N_TOPICS):
        cur.execute(
            "INSERT INTO topics (slug, title, deleted_at) VALUES (%s, %s, CASE WHEN %s THEN now() END)",
            (f"topic-{i}", f"Topic {i}", i % 13 == 5))
        cur.execute(
            "INSERT INTO memory_embeddings (profile, kind, ref, chunk_idx, chunk_text, content_hash, embedding) "
            "VALUES (%s, 'topic', %s, 0, %s, 'h', %s::vector)",
            (PROFILE, f"topic-{i}", f"topic {i} chunk", _lit(_vec(rng))))
    cur.execute(
        f"CREATE INDEX ON memory_embeddings USING hnsw ((embedding::vector({DIM})) vector_cosine_ops) "
        f"WHERE profile = '{PROFILE}'")
    conn.commit()
    cur.execute("ANALYZE")
    conn.commit()
    return hub, rng


@contextlib.contextmanager
def _patched(hub_):
    @contextlib.contextmanager
    def connect(*a, **k):
        conn = hub_.connect()
        try:
            yield conn
        finally:
            conn.close()

    with mock.patch("khipu.db.connect", connect):
        yield


def _run(hub_, qvec, **kw):
    db._TABLE_COLUMNS_CACHE.clear()
    with _patched(hub_), \
            mock.patch.object(embed, "_active_profile", return_value=PROFILE), \
            mock.patch.object(embed, "uses_task_prefixes", return_value=False), \
            mock.patch.object(embed, "_query_vec", return_value=(qvec, "off")):
        return embed._cosine_candidates("q", **kw)


def _old_query(cur, qvec, lim, filters=None):
    live = ("AND ((m.kind = 'topic' AND EXISTS (SELECT 1 FROM topics t WHERE t.slug = m.ref AND t.deleted_at IS NULL)) "
            "OR (m.kind = 'episode' AND EXISTS (SELECT 1 FROM episodes e WHERE e.id::text = m.ref AND e.deleted_at IS NULL)) "
            "OR m.kind NOT IN ('episode', 'topic'))")
    params = {"q": _lit(qvec), "p": PROFILE, "lim": lim}
    filt = ""
    if filters is not None:
        filters.expand_project(cur)
        params.update(filters.params)
        filt = ("AND (m.kind = 'episode' AND EXISTS (SELECT 1 FROM episodes e WHERE e.id::text = m.ref AND "
                f"({filters.episode_sql(cur, 'e')})))")
    vc = f"vector({DIM})"
    cur.execute(
        f"SELECT m.kind, m.ref FROM memory_embeddings m WHERE m.profile = %(p)s AND m.kind != 'commitment' "
        f"{live} {filt} ORDER BY m.embedding::{vc} <=> %(q)s::{vc} LIMIT %(lim)s", params)
    return [(k, r) for k, r in cur.fetchall()]


def _sql_for(cur, filters, kind=None):
    flags = embed._episode_schema_flags(cur)
    if filters is not None:
        filters.expand_project(cur)
    return embed._cosine_sql(
        f"vector({DIM})", filters, kind=kind,
        episode_live=" AND e.deleted_at IS NULL" if flags["deleted_at"] else "",
        has_media=False,
        episode_filter=filters.episode_sql(cur, "e") if filters else "",
        topic_filter=filters.topic_sql("t") if filters else "")


def test_filtered_plan_scans_episodes_once_not_per_vector(seeded):
    hub_, rng = seeded
    conn = hub_.connect()
    cur = conn.cursor()
    filters = embed._SearchFilters(project="church-harness")
    sql = _sql_for(cur, filters)
    params = {"q": _lit(_vec(rng)), "p": PROFILE, "kind": None, "lim": 50,
              "fetch": 200, "rank_fetch": 800, **filters.params}
    cur.execute("EXPLAIN (COSTS OFF) " + sql, params)
    plan = "\n".join(r[0] for r in cur.fetchall())
    assert "SubPlan" not in plan, plan
    assert plan.count("on episodes") == 1, plan
    assert "Materialize" in plan or "CTE ep" in plan, plan


def test_unfiltered_plan_joins_episodes_through_an_index_not_a_subplan(seeded):
    hub_, rng = seeded
    conn = hub_.connect()
    cur = conn.cursor()
    sql = _sql_for(cur, None)
    cur.execute("EXPLAIN (COSTS OFF) " + sql, {
        "q": _lit(_vec(rng)), "p": PROFILE, "kind": None, "lim": 50, "fetch": 200, "rank_fetch": 800})
    plan = "\n".join(r[0] for r in cur.fetchall())
    assert "SubPlan" not in plan, plan


@pytest.mark.parametrize("project", [None, "church-harness"])
def test_results_match_the_old_statement(seeded, project):
    hub_, rng = seeded
    filters = embed._SearchFilters(project=project) if project else None
    conn = hub_.connect()
    cur = conn.cursor()
    for _ in range(3):
        q = _vec(rng)
        old = _old_query(cur, q, 30, filters)
        new = _run(hub_, q, limit=30, filters=filters)
        got = [(r["kind"], r["id"]) for r in new]
        if project:  # exact in both: order must be identical
            assert got == old
        else:  # HNSW is approximate; the old statement was too
            assert len(set(got) & set(old)) >= 0.95 * len(old)
        assert all(r["label"] for r in new)
        assert [r["score"] for r in new] == sorted((r["score"] for r in new), reverse=True)


@pytest.mark.parametrize("project", [None, "church-harness"])
def test_top_50_recall_against_exact_search(seeded, project):
    hub_, rng = seeded
    filters = embed._SearchFilters(project=project) if project else None
    conn = hub_.connect()
    cur = conn.cursor()
    hits = total = 0
    for _ in range(8):
        q = _vec(rng)
        got = {(r["kind"], r["id"]) for r in _run(hub_, q, limit=50, filters=filters)}
        cur.execute("SET enable_indexscan = off; SET enable_bitmapscan = off")
        exact = set(_old_query(cur, q, 50, filters))
        cur.execute("RESET enable_indexscan; RESET enable_bitmapscan")
        hits += len(got & exact)
        total += len(exact)
    assert hits / total >= 0.95


def test_kind_and_soft_delete_are_honoured(seeded):
    hub_, rng = seeded
    q = _vec(rng)
    topics = _run(hub_, q, limit=200, kind="topic")
    assert topics and {r["kind"] for r in topics} == {"topic"}
    assert not {r["id"] for r in topics} & {f"topic-{i}" for i in range(N_TOPICS) if i % 13 == 5}
    eps = _run(hub_, q, limit=200, kind="episode")
    conn = hub_.connect()
    cur = conn.cursor()
    cur.execute("SELECT id::text FROM episodes WHERE deleted_at IS NOT NULL")
    dead = {r[0] for r in cur.fetchall()}
    assert eps and not {r["id"] for r in eps} & dead
    assert _run(hub_, q, limit=5, kind="commitment") == []


def test_hnsw_is_widened_for_the_transaction(seeded):
    hub_, rng = seeded
    cur = hub_.connect().cursor()
    embed._tune_hnsw(cur, 200)
    cur.execute("SHOW hnsw.ef_search")
    assert cur.fetchone()[0] == "400"
    cur.execute("SHOW hnsw.iterative_scan")
    assert cur.fetchone()[0] == "relaxed_order"
    embed._tune_hnsw(cur, 3)
    cur.execute("SHOW hnsw.ef_search")
    assert cur.fetchone()[0] == "100"


@pytest.mark.parametrize("project", [None, "church-harness"])
def test_the_statement_search_sends_has_no_per_row_exists_on_episodes(seeded, project):
    hub_, rng = seeded
    sent: list[str] = []
    real = hub_.connect

    class Spy:
        def __init__(self, cur):
            self._cur = cur

        def execute(self, sql, params=None):
            sent.append(" ".join(str(sql).split()))
            return self._cur.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._cur, name)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class SpyConn:
        def __init__(self, conn):
            self._conn = conn

        def cursor(self):
            return Spy(self._conn.cursor())

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    db._TABLE_COLUMNS_CACHE.clear()
    filters = embed._SearchFilters(project=project) if project else None
    with mock.patch("khipu.db.connect", lambda *a, **k: SpyConn(real())), \
            mock.patch.object(embed, "_active_profile", return_value=PROFILE), \
            mock.patch.object(embed, "uses_task_prefixes", return_value=False), \
            mock.patch.object(embed, "_query_vec", return_value=(_vec(rng), "off")):
        embed._cosine_candidates("q", limit=10, filters=filters)
    main = next(q for q in sent if "FROM memory_embeddings m" in q)
    assert "EXISTS" not in main
    assert ("UNION ALL" in main) == (project is None)  # a project filter is episode-only
