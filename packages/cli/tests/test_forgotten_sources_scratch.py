"""Migration 0030 (forgotten_sources and its triggers) against a real scratch
Postgres. Same guard and environment as test_episode_origin_scratch.py. Each
test runs inside one transaction that is rolled back, so the scratch database
is left as found."""
from __future__ import annotations

import json
import os
import unittest

import psycopg
import pytest

from tests import connect_scratch, validate_scratch_dsn

SCRATCH_DSN = os.environ.get("KHIPU_SCRATCH_DSN")

if SCRATCH_DSN and os.environ.get("KHIPU_LIVE_TESTS") == "1":
    validate_scratch_dsn(SCRATCH_DSN)

pytestmark = pytest.mark.skipif(
    not SCRATCH_DSN or os.environ.get("KHIPU_LIVE_TESTS") != "1",
    reason="requires KHIPU_LIVE_TESTS=1 and a provisioned nonce-scoped KHIPU_SCRATCH_DSN",
)

VERSION = "0030_forgotten_sources"


def _migration_sql() -> str:
    from khipu import migrate

    return next(p for v, p in migrate.available() if v == VERSION).read_text(encoding="utf-8")


class ForgottenSourcesScratchTest(unittest.TestCase):
    def setUp(self):
        from khipu import capture_identity as ci

        self.ci = ci
        self.conn = connect_scratch(SCRATCH_DSN)
        self.cur = self.conn.cursor()
        self.cur.execute("TRUNCATE episodes CASCADE")
        self.cur.execute(_migration_sql())
        self.cur.execute(_migration_sql())  # safe to run twice

    def tearDown(self):
        self.conn.rollback()
        self.conn.close()

    def _insert(self, payload: dict) -> int:
        self.cur.execute(
            "INSERT INTO episodes (ts, session_id, summary, raw, harness, transcript_range) "
            "VALUES (%s::timestamptz, %s, %s, %s::jsonb, %s, %s) RETURNING id",
            (payload["ts"], payload["session_id"], payload["summary"], json.dumps(payload),
             payload["harness"], payload["transcript_range"]))
        return self.cur.fetchone()[0]

    def _forget(self, payload: dict, episode_id: int) -> None:
        from khipu import db, tombstones

        db._TABLE_COLUMNS_CACHE.clear()
        tombstones.publish_hub_forget(self.cur, episode_id, [self.ci.source_identity(payload)])
        self.cur.execute("UPDATE episodes SET deleted_at = now() WHERE id = %s", (episode_id,))

    P = {"ts": "2026-10-15T09:00:00Z", "summary": "scratch forgotten source",
         "harness": "codex", "session_id": "codex:scratch", "transcript_range": "0:10"}

    def test_an_insert_of_a_forgotten_natural_key_is_refused(self):
        self._forget(self.P, self._insert(self.P))
        self.cur.execute("SAVEPOINT refused")
        with self.assertRaises(psycopg.errors.CheckViolation) as caught:
            self._insert({**self.P, "summary": "the same span, synthesized again"})
        self.assertIn("khipu: forgotten source", str(caught.exception))
        self.cur.execute("ROLLBACK TO SAVEPOINT refused")

    def test_un_forgetting_an_episode_is_refused(self):
        eid = self._insert(self.P)
        self._forget(self.P, eid)
        self.cur.execute("SAVEPOINT refused")
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.cur.execute("UPDATE episodes SET deleted_at = NULL WHERE id = %s", (eid,))
        self.cur.execute("ROLLBACK TO SAVEPOINT refused")
        # Metadata on a forgotten row still updates (hygiene, origin stamping).
        self.cur.execute("UPDATE episodes SET project = 'acme/scratch' WHERE id = %s", (eid,))
        self.assertEqual(self.cur.rowcount, 1)

    def test_an_unrelated_insert_passes(self):
        self._forget(self.P, self._insert(self.P))
        other = {**self.P, "session_id": "codex:other", "summary": "unrelated"}
        self.assertIsInstance(self._insert(other), int)

    def test_the_hub_gate_reads_the_table(self):
        from khipu import db, tombstones

        self._forget(self.P, self._insert(self.P))
        db._TABLE_COLUMNS_CACHE.clear()
        self.assertTrue(tombstones.hub_source_forgotten(self.cur, {**self.P, "summary": "x"}))
        self.assertFalse(tombstones.hub_source_forgotten(
            self.cur, {**self.P, "session_id": "codex:other"}))


if __name__ == "__main__":
    unittest.main()
