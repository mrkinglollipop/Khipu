# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""Real-SQL tests for migration 0025 and khipu.briefs against an isolated,
throwaway PostgreSQL. They run ONLY when ``KHIPU_SCRATCH_DSN`` is set and skip
cleanly otherwise — same gate as tests/test_pg_scratch.py:

  cd packages/cli && S=$(mktemp -d) && KHIPU_SCRATCH_DSN='postgresql://khipu_scratch@/khipu_scratch?host=<socket dir>&port=54329' \\
      PYTHONPYCACHEPREFIX="$S/pyc" PYTHONPATH="$PWD:$PWD/../../.python_libs" \\
      python3.11 -m pytest -q -p no:cacheprovider tests/test_briefs_scratch.py

Every test cleans up the rows it creates, by an invented topic slug.
"""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

import pytest

from khipu import briefs, forget

SCRATCH_DSN = os.environ.get("KHIPU_SCRATCH_DSN")


def _scratch_dbname(dsn: str) -> str:
    from psycopg.conninfo import conninfo_to_dict

    return str((conninfo_to_dict(dsn) or {}).get("dbname") or "")


if SCRATCH_DSN:
    _dbname = _scratch_dbname(SCRATCH_DSN)
    assert "scratch" in _dbname.lower(), (
        f"refusing to run tests/test_briefs_scratch.py against database {_dbname!r} — "
        "KHIPU_SCRATCH_DSN must name a database containing 'scratch'"
    )

pytestmark = pytest.mark.skipif(
    not SCRATCH_DSN,
    reason="KHIPU_SCRATCH_DSN not set — this file only runs against the isolated scratch database",
)

SLUG = "briefs-scratch-topic"
SUMMARY_PREFIX = "briefs-scratch episode"


def _direct_connect():
    """A connection made with EXACTLY the given DSN — never khipu.db's own
    resolution, so there is no path to a different database."""
    import psycopg

    return psycopg.connect(SCRATCH_DSN)


def _migration_sql() -> str:
    from khipu import migrate

    for version, path in migrate.available():
        if version == "0025_briefs":
            return path.read_text(encoding="utf-8")
    raise AssertionError("0025_briefs.sql not found under ops/migrations")


def _apply_migration() -> None:
    with _direct_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(_migration_sql())
        conn.commit()
    from khipu import db as _db

    _db._TABLE_COLUMNS_CACHE.clear()


@pytest.fixture(autouse=True)
def _ensure_migration_applied():
    """0025 is idempotent, so re-applying it before every test makes test
    order irrelevant."""
    _apply_migration()
    yield


def _answer(*claims) -> str:
    return json.dumps({"body": "A short body.", "claims": [
        {"text": text, "episode_ids": list(ids)} for text, ids in claims
    ]})


class BriefsMigrationScratchTest(unittest.TestCase):
    def test_it_is_idempotent_and_records_itself(self):
        _apply_migration()
        _apply_migration()
        with _direct_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM schema_migrations WHERE version = '0025_briefs'")
                self.assertEqual(cur.fetchone()[0], 1)
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'briefs'"
                )
                columns = {r[0] for r in cur.fetchall()}
        self.assertTrue(set(briefs._TABLE_COLUMNS) <= columns)

    def test_the_state_defaults_and_array_column_round_trip(self):
        with _direct_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO briefs (topic_slug, source_hash, source_episode_ids) "
                    "VALUES (%s, 'h', %s::bigint[]) RETURNING state, derivation_version, claims, source_episode_ids",
                    (SLUG, [5, 6_000_000_000]),
                )
                state, version, claims, ids = cur.fetchone()
                conn.rollback()
        self.assertEqual((state, version, claims, ids), ("current", 1, [], [5, 6_000_000_000]))


class BriefsScratchTest(unittest.TestCase):
    def setUp(self):
        self.conn = _direct_connect()
        self.cur = self.conn.cursor()
        self._clean()
        self.cur.execute("INSERT INTO topics (slug, title, body) VALUES (%s, %s, '')", (SLUG, "Scratch topic"))
        self.ids = [self._episode(f"{SUMMARY_PREFIX} one: chose cursor pagination"),
                    self._episode(f"{SUMMARY_PREFIX} two: shipped behind a flag")]
        self.conn.commit()

    def tearDown(self):
        self.conn.rollback()
        self._clean()
        self.conn.commit()
        self.cur.close()
        self.conn.close()

    def _clean(self):
        self.cur.execute("DELETE FROM briefs WHERE topic_slug = %s", (SLUG,))
        self.cur.execute("DELETE FROM episodes WHERE summary LIKE %s", (SUMMARY_PREFIX + "%",))
        self.cur.execute("DELETE FROM topics WHERE slug = %s", (SLUG,))

    def _episode(self, summary: str) -> int:
        self.cur.execute(
            "INSERT INTO episodes (ts, session_id, summary, topics) VALUES (now(), 'scratch:briefs', %s, %s::jsonb) "
            "RETURNING id",
            (summary, json.dumps([SLUG])),
        )
        return self.cur.fetchone()[0]

    def _planned(self):
        return next((p for p in briefs.plan(self.cur)["topics"] if p["topic"] == SLUG), None)

    def _build(self, answer: str) -> dict:
        with mock.patch("khipu.extract._generate", return_value=answer):
            out = briefs.build_one(self.cur, SLUG)
        self.conn.commit()
        return out

    def test_plan_build_and_read_round_trip(self):
        self.assertEqual(self._planned()["reason"], "missing")
        out = self._build(_answer(("Cursor pagination was chosen.", [self.ids[0]]),
                                  ("It shipped behind a flag.", [self.ids[1]])))
        self.assertEqual(out["status"], "built")
        self.assertIsNone(self._planned())
        payload = briefs.read_brief(self.cur, SLUG)
        self.assertTrue(payload["found"])
        self.assertEqual(payload["state"], "current")
        self.assertEqual(payload["source_count"], 2)
        self.assertEqual(payload["claims"][0]["episode_ids"], [self.ids[0]])
        self.assertGreaterEqual(payload["age_seconds"], 0)

    def test_a_retry_with_the_same_sources_makes_no_second_row(self):
        self._build(_answer(("Fact.", [self.ids[0]])))
        with mock.patch("khipu.extract._generate") as gen:
            self.assertEqual(briefs.build_one(self.cur, SLUG)["status"], "unchanged")
        gen.assert_not_called()
        self.cur.execute("SELECT count(*) FROM briefs WHERE topic_slug = %s", (SLUG,))
        self.assertEqual(self.cur.fetchone()[0], 1)

    def test_a_new_episode_makes_it_stale_and_the_rebuild_supersedes(self):
        self._build(_answer(("Fact.", [self.ids[0]])))
        third = self._episode(f"{SUMMARY_PREFIX} three: added export")
        self.conn.commit()
        self.assertEqual(self._planned()["reason"], "stale")
        self._build(_answer(("Export was added.", [third])))
        self.cur.execute(
            "SELECT count(*) FROM briefs WHERE topic_slug = %s AND superseded_at IS NULL AND state = 'current'",
            (SLUG,),
        )
        self.assertEqual(self.cur.fetchone()[0], 1)
        self.cur.execute("SELECT count(*) FROM briefs WHERE topic_slug = %s", (SLUG,))
        self.assertEqual(self.cur.fetchone()[0], 2)

    def test_a_failed_build_keeps_the_previous_brief_current(self):
        self._build(_answer(("Fact.", [self.ids[0]])))
        self._episode(f"{SUMMARY_PREFIX} three: added export")
        self.conn.commit()
        out = self._build(_answer(("Uncited fact.", [])))
        self.assertEqual(out["status"], "failed")
        payload = briefs.read_brief(self.cur, SLUG)
        self.assertEqual(payload["claims"][0]["text"], "Fact.")
        self.cur.execute("SELECT count(*) FROM briefs WHERE topic_slug = %s AND state = 'failed'", (SLUG,))
        self.assertEqual(self.cur.fetchone()[0], 1)

    def test_forgetting_a_cited_episode_stales_the_brief_and_withholds_it(self):
        self._build(_answer(("Fact.", [self.ids[0]])))
        out = forget.forget_episode(self.cur, self.ids[0])
        self.conn.commit()
        self.assertEqual(out["briefs_staled"], 1)
        payload = briefs.read_brief(self.cur, SLUG)
        self.assertEqual(payload["state"], "stale")
        self.assertEqual(payload["body"], "")
        self.assertEqual(self._planned()["reason"], "stale")

    def test_the_build_lock_admits_one_holder(self):
        other = _direct_connect()
        try:
            other_cur = other.cursor()
            self.assertTrue(briefs.acquire_build_lock(self.cur))
            self.assertFalse(briefs.acquire_build_lock(other_cur))
            briefs.release_build_lock(self.cur)
            self.assertTrue(briefs.acquire_build_lock(other_cur))
            briefs.release_build_lock(other_cur)
        finally:
            other.close()


if __name__ == "__main__":
    unittest.main()
