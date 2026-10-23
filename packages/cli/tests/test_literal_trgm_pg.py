"""Swarm-ready slice 4: migration 0041 (trigram indexes for the episode
literal leg), against a real PostgreSQL (tests/hermetic_pg.py; synthetic rows).

The five-column episode ILIKE only becomes a BitmapOr when every column has a
trigram index on the exact expression the query uses; the results must be the
same either way, and a hub without pg_trgm must skip rather than fail.
"""
from __future__ import annotations

import json

import pytest

from khipu import cli, migrate
from tests.hermetic_pg import MIGRATIONS, hub, pg_cluster  # noqa: F401

VERSION = "0041_episodes_literal_trgm_indexes"
COLS = ("topics", "decisions", "preferences", "people")
NAMES = tuple(f"idx_episodes_{c}_text_trgm" for c in COLS)


def _indexes(conn):
    rows = conn.execute(
        "SELECT i.indexrelid::regclass::text, i.indisvalid, pg_get_indexdef(i.indexrelid) "
        "FROM pg_index i WHERE i.indexrelid::regclass::text = ANY(%s) ORDER BY 1",
        (list(NAMES),)).fetchall()
    conn.commit()
    return rows


def _recorded(conn):
    n = conn.execute("SELECT count(*) FROM schema_migrations WHERE version = %s",
                     (VERSION,)).fetchone()[0]
    conn.commit()
    return n == 1


def _rewind(conn):
    conn.execute("DELETE FROM schema_migrations WHERE version = %s", (VERSION,))
    for n in NAMES:
        conn.execute(f"DROP INDEX IF EXISTS {n}")
    conn.commit()


@pytest.fixture
def conn(hub):  # noqa: F811
    c = hub.connect()
    migrate.run(conn=c, directory=MIGRATIONS)
    yield c


def _seed(conn, n=1500):
    for i in range(n):
        conn.execute(
            "INSERT INTO episodes (ts, session_id, summary, topics, decisions, preferences, people, raw, "
            "harness) VALUES (now() - make_interval(mins => %s), %s, %s, %s::jsonb, %s::jsonb, "
            "%s::jsonb, %s::jsonb, '{}'::jsonb, 'codex')",
            (i, f"codex:s{i}", f"summary number {i} about filler", json.dumps([f"topic-{i % 17}"]),
             json.dumps([f"decided thing {i}", "needle-in-decisions" if i == 40 else "plain"]),
             json.dumps(["likes pref-needle" if i == 41 else "none"]),
             json.dumps(["person needle-person" if i == 42 else "nobody"])))
    conn.execute("ANALYZE episodes")
    conn.commit()


def _episode_ids(conn, term):
    cur = conn.cursor()
    out = cli._search_query(cur, term, 10, kind="episode")
    conn.rollback()
    return [r["id"] for r in out]


def test_the_four_indexes_are_built_valid_and_recorded(conn):
    rows = _indexes(conn)
    assert [r[0] for r in rows] == sorted(NAMES)
    assert all(r[1] for r in rows)
    for c in COLS:
        assert any(f"COALESCE(({c})::text, ''::text) gin_trgm_ops" in r[2] for r in rows)
    assert _recorded(conn)
    assert conn.autocommit is False


def test_the_episode_literal_query_uses_all_five_indexes(conn):
    _seed(conn)
    conn.execute("SET enable_seqscan = off")
    cur = conn.cursor()
    cur.execute("EXPLAIN SELECT id FROM episodes WHERE deleted_at IS NULL AND ("
                + " OR ".join(f"{c} ILIKE '%needle%'" for c in cli._EPISODE_ILIKE_COLUMNS) + ")")
    plan = "\n".join(r[0] for r in cur.fetchall())
    conn.rollback()
    assert "BitmapOr" in plan and "Seq Scan" not in plan
    for n in NAMES + ("idx_episodes_summary_trgm",):
        assert n in plan, plan


def test_results_are_identical_with_and_without_the_indexes(conn):
    _seed(conn)
    terms = ["needle-in-decisions", "pref-needle", "needle-person", "topic-3", "filler"]
    with_idx = {t: _episode_ids(conn, t) for t in terms}
    assert with_idx["needle-in-decisions"] and with_idx["pref-needle"] and with_idx["needle-person"]
    _rewind(conn)
    assert {t: _episode_ids(conn, t) for t in terms} == with_idx


def test_a_hub_without_pg_trgm_skips_the_step_and_still_records(conn):
    _rewind(conn)
    conn.execute("DROP EXTENSION pg_trgm CASCADE")
    conn.commit()
    assert migrate.run(conn=conn, directory=MIGRATIONS)["ran"] == [VERSION]
    assert _indexes(conn) == []
    assert _recorded(conn)


def test_a_rerun_adopts_the_valid_indexes_without_a_rebuild(conn):
    before = conn.execute("SELECT indexrelid::oid::bigint FROM pg_index WHERE indexrelid = ANY("
                          "ARRAY(SELECT to_regclass(n) FROM unnest(%s::text[]) n)) ORDER BY 1",
                          (list(NAMES),)).fetchall()
    conn.execute("DELETE FROM schema_migrations WHERE version = %s", (VERSION,))
    conn.commit()
    assert migrate.run(conn=conn, directory=MIGRATIONS)["ran"] == [VERSION]
    after = conn.execute("SELECT indexrelid::oid::bigint FROM pg_index WHERE indexrelid = ANY("
                         "ARRAY(SELECT to_regclass(n) FROM unnest(%s::text[]) n)) ORDER BY 1",
                         (list(NAMES),)).fetchall()
    assert after == before and len(after) == 4
