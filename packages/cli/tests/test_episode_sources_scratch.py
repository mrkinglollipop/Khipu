"""Migration 0027 (episode_sources) and the phase 1 dual write against a real,
separately provisioned scratch Postgres. Same guard and environment as
test_pg_scratch.py. Migration tests run inside one transaction that is rolled
back, so the scratch database is left as it was found."""
from __future__ import annotations

import hashlib
import json
import os
import unittest
from datetime import datetime, timezone
from unittest import mock

import pytest

from khipu import capture_identity as ci
from tests import connect_scratch, validate_scratch_dsn

SCRATCH_DSN = os.environ.get("KHIPU_SCRATCH_DSN")

if SCRATCH_DSN and os.environ.get("KHIPU_LIVE_TESTS") == "1":
    validate_scratch_dsn(SCRATCH_DSN)

pytestmark = pytest.mark.skipif(
    not SCRATCH_DSN or os.environ.get("KHIPU_LIVE_TESTS") != "1",
    reason="requires KHIPU_LIVE_TESTS=1 and a provisioned nonce-scoped KHIPU_SCRATCH_DSN",
)

VERSION = "0027_episode_sources"


def _direct_connect():
    return connect_scratch(SCRATCH_DSN)


def _migration_sql() -> str:
    from khipu import migrate

    for version, path in migrate.available():
        if version == VERSION:
            return path.read_text(encoding="utf-8")
    raise AssertionError(f"{VERSION}.sql not found under ops/migrations")


@pytest.fixture(autouse=True)
def _ensure_migration_applied():
    with _direct_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(_migration_sql())
        conn.commit()
    from khipu import db as _db

    _db._TABLE_COLUMNS_CACHE.clear()
    yield
    _db._TABLE_COLUMNS_CACHE.clear()


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


class MigrationScratchTest(unittest.TestCase):
    def setUp(self):
        self.conn = _direct_connect()
        self.cur = self.conn.cursor()
        # A hub as it stood before 0027, and empty, for this transaction only.
        self.cur.execute("DROP TABLE IF EXISTS episode_sources")
        self.cur.execute("DELETE FROM schema_migrations WHERE version = %s", (VERSION,))
        self.cur.execute("TRUNCATE episodes CASCADE")

    def tearDown(self):
        self.conn.rollback()
        self.conn.close()

    def _apply(self):
        self.cur.execute(_migration_sql())

    def _rows(self):
        self.cur.execute(
            "SELECT source_key, episode_id, role, harness, session_id, transcript_range, "
            "window_id, part, ts, summary_md5, payload_sha256, created_at, deleted_at "
            "FROM episode_sources ORDER BY source_key")
        return self.cur.fetchall()

    def _episode(self, *, ts="2026-10-01T00:00:00Z", summary="s", session_id=None,
                 harness=None, transcript_range=None, window_id=None, raw=None,
                 deleted=False) -> int:
        self.cur.execute(
            "INSERT INTO episodes (ts, session_id, summary, raw, harness, transcript_range, "
            "window_id, deleted_at) VALUES (%s::timestamptz, %s, %s, %s::jsonb, %s, %s, %s, "
            "CASE WHEN %s THEN now() END) RETURNING id",
            (ts, session_id, summary, json.dumps(raw) if raw is not None else None, harness,
             transcript_range, window_id, deleted))
        return int(self.cur.fetchone()[0])

    def test_empty_hub_applies_and_a_second_run_changes_nothing(self):
        self._apply()
        self.cur.execute("SELECT to_regclass('public.episode_sources') IS NOT NULL")
        self.assertTrue(self.cur.fetchone()[0])
        self.assertEqual(self._rows(), [])
        self.cur.execute("SELECT count(*) FROM schema_migrations WHERE version = %s", (VERSION,))
        self.assertEqual(self.cur.fetchone()[0], 1)
        self._apply()
        self.assertEqual(self._rows(), [])

    def test_seeded_hub_backfill(self):
        natural = {"harness": "claude_code", "session_id": "claude_code:a",
                   "transcript_range": "0:10", "window_id": "w", "part": "1/2"}
        n1 = self._episode(summary="natural", raw={**natural, "summary": "natural"}, **{
            k: natural[k] for k in ("harness", "session_id", "transcript_range", "window_id")})
        # Edited after a merge: the live summary differs from the first write.
        merged_payload = {"summary": "second capture", "ts": "2026-10-01T00:01:00Z",
                          "harness": "codex", "session_id": "codex:b", "transcript_range": "5:9"}
        gateway_payload = {"summary": "gateway note", "ts": "2026-10-01T00:02:00Z",
                           "session_id": "gw:c"}
        merged_from = [
            {"source_id": ci.payload_sha256(merged_payload), "session_id": "codex:b",
             "ts": merged_payload["ts"], "payload": merged_payload},
            {"source_id": ci.payload_sha256(gateway_payload), "session_id": "gw:c",
             "ts": gateway_payload["ts"], "payload": gateway_payload},
            {"session_id": "old:d", "ts": "not a time"},
            "not an object",
        ]
        n2 = self._episode(summary="edited text", session_id="claude_code:e",
                           harness="claude_code", transcript_range="0:5",
                           raw={"summary": "first text", "ts": "2026-10-01T00:00:00Z",
                                "merged_from": merged_from})
        # Summary-only (no transcript_range): no reproducible payload, episode key.
        n3 = self._episode(summary="summary only", session_id="gw:f",
                           raw={"summary": "summary only"})
        # No raw at all, and a raw whose merged_from is not an array.
        n4 = self._episode(summary="no raw")
        n5 = self._episode(summary="odd raw", raw={"merged_from": {"x": 1}, "summary": 7})
        # Two live episodes claiming one natural key: the older keeps it.
        dup = {"harness": "claude_code", "session_id": "claude_code:g", "transcript_range": "0:3"}
        d1 = self._episode(summary="dup one", **dup)
        d2 = self._episode(summary="dup two", **dup)
        # A tombstoned episode gets nothing.
        self._episode(summary="gone", deleted=True, **{**dup, "session_id": "claude_code:h"})

        self._apply()
        rows = self._rows()
        by_key = {r[0]: r for r in rows}
        origin = {r[1]: r for r in rows if r[2] == "origin"}
        merged = [r for r in rows if r[2] == "merged"]

        self.assertEqual(sorted(origin), sorted([n1, n2, n3, n4, n5, d1, d2]))
        self.assertEqual(len(merged), 3)

        self.assertEqual(origin[n1][0], ci.source_key(natural))
        self.assertEqual(origin[n1][3:8], ("claude_code", "claude_code:a", "0:10", "w", "1/2"))
        self.assertEqual(origin[n2][9], _md5("first text"))
        self.assertEqual(origin[n3][0], ci.episode_key(n3))
        self.assertEqual(origin[n4][0], ci.episode_key(n4))
        self.assertEqual(origin[n4][9], _md5("no raw"))
        self.assertEqual(origin[n5][9], _md5("odd raw"))
        self.assertEqual(origin[d1][0], ci.source_key(dup))
        self.assertEqual(origin[d2][0], ci.episode_key(d2))
        self.assertTrue(all(r[10] is None for r in origin.values()))

        m1 = by_key[ci.source_key(merged_payload)]
        self.assertEqual((m1[1], m1[2], m1[4]), (n2, "merged", "codex:b"))
        self.assertEqual(m1[10], ci.payload_sha256(merged_payload))
        self.assertEqual(m1[9], _md5("second capture"))
        self.assertEqual(m1[8], datetime(2026, 10, 1, 0, 1, tzinfo=timezone.utc))
        m2 = by_key[ci.source_key(gateway_payload)]
        self.assertEqual(m2[0], ci.payload_sha256(gateway_payload))
        self.assertEqual(m2[1], n2)
        # No payload and no receipt: keyed by episode and position, ts unparseable -> NULL.
        m3 = [r for r in merged if r[0] not in (m1[0], m2[0])][0]
        self.assertEqual(m3[0], hashlib.sha256(f"khipu-merged-v1\x1f{n2}\x1f3".encode()).hexdigest())
        self.assertIsNone(m3[8])
        self.assertIsNone(m3[9])

        self._apply()
        self.assertEqual(self._rows(), rows)

    def test_rerun_adds_rows_only_for_episodes_written_without_the_table(self):
        self._apply()
        before = self._rows()
        late = self._episode(summary="landed while the table was missing")
        self._apply()
        after = self._rows()
        self.assertEqual(len(after), len(before) + 1)
        self.assertTrue(set(before) <= set(after))
        self.assertIn(ci.episode_key(late), {r[0] for r in after})

    def test_hard_delete_of_an_episode_removes_its_source_rows(self):
        eid = self._episode(summary="to delete")
        self._apply()
        self.cur.execute("DELETE FROM episodes WHERE id = %s", (eid,))
        self.assertEqual(self._rows(), [])


class DualWriteScratchTest(unittest.TestCase):
    """capture.write_pg against real SQL: one origin row per insert, one merged
    row per merge, and a hub without 0027 still captures."""

    def setUp(self):
        self.ids: list[int] = []
        for p in (mock.patch("khipu.db.connect", side_effect=lambda *a, **k: _direct_connect()),
                  mock.patch("khipu.topic_graph.persist_capture_graph", return_value={}),
                  mock.patch("khipu.config.path_setting", return_value=None),
                  mock.patch("khipu.embed.embed_one", side_effect=RuntimeError("no key"))):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        with _direct_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM episodes WHERE id = ANY(%s)", (self.ids,))
            conn.commit()

    def _payload(self, **kw):
        nonce = os.urandom(6).hex()
        return {"ts": "2026-10-11T12:00:00Z", "summary": f"scratch capture {nonce}",
                "harness": "claude_code", "session_id": f"claude_code:{nonce}",
                "transcript_range": "0:100", "window_id": f"w{nonce}", "part": "1/1", **kw}

    def _write(self, payload):
        from khipu import capture

        stats = capture.write_pg(payload)
        if stats.get("episode_id"):
            self.ids.append(int(stats["episode_id"]))
        return stats

    def _sources(self, episode_id):
        with _direct_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT source_key, role, ts, summary_md5, payload_sha256 "
                            "FROM episode_sources WHERE episode_id = %s ORDER BY role",
                            (episode_id,))
                return cur.fetchall()

    def test_insert_then_merge(self):
        first = self._payload(project="acme/scratch-widget")
        expected = ci.source_identity(dict(first))
        stats = self._write(first)
        self.assertTrue(stats["episode_inserted"])
        eid = stats["episode_id"]
        (row,) = self._sources(eid)
        self.assertEqual(row[0], expected["source_key"])
        self.assertEqual(row[1], "origin")
        self.assertEqual(row[3], _md5(first["summary"]))
        self.assertEqual(row[4], expected["payload_sha256"])

        second = self._payload(project="acme/scratch-widget", ts="2026-10-11T12:01:00Z",
                               summary=first["summary"])
        stats2 = self._write(second)
        self.assertEqual(stats2["dedup"]["action"], "merge")
        rows = self._sources(eid)
        self.assertEqual([r[1] for r in rows], ["merged", "origin"])
        self.assertEqual(rows[0][0], ci.source_key(second))

    def test_hub_without_the_table_still_captures(self):
        from khipu import db as _db

        with _direct_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DROP TABLE episode_sources")
                cur.execute("DELETE FROM schema_migrations WHERE version = %s", (VERSION,))
            conn.commit()
        _db._TABLE_COLUMNS_CACHE.clear()
        stats = self._write(self._payload())
        self.assertTrue(stats["episode_inserted"])
        self.assertIsNotNone(stats["episode_id"])


if __name__ == "__main__":
    unittest.main()
