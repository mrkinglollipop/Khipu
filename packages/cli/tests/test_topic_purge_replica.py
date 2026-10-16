"""`khipu topic purge` removes the topic from this Mac's sqlite replica too.

The hub delete alone left the replica serving the topic (keyword search, topic
detail, vector candidates) until the next full refresh. These tests drive the
real CLI handler against a stub hub and a real temp replica, with a control
topic that must keep answering.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import struct
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from khipu import cli
from khipu import hub_snapshot as hs

_PROFILE = "test-profile"
_PURGED = "dead-topic"
_CONTROL = "live-topic"


class _HubCur:
    """The hub side of a purge of a tombstoned topic."""

    def __init__(self) -> None:
        self.rowcount = 0
        self._row = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self._row = ("2026-08-01T00:00:00Z",) if s.startswith("SELECT deleted_at FROM topics") else None
        self.rowcount = 1 if s.startswith("DELETE") else 0

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _HubConn:
    def cursor(self):
        return _HubCur()

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _vec(*xs: float) -> bytes:
    return struct.pack(f"{len(xs)}f", *xs)


def _build(path: Path, *, version: int = hs.SNAPSHOT_SCHEMA_VERSION) -> None:
    con = sqlite3.connect(str(path))
    hs._create_schema(con, version=version)
    # The replica was dumped before reconcile tombstoned the topic on the hub,
    # so both topics are live here.
    for slug, body, vec in (
        (_PURGED, "zebrafish migration notes", _vec(1.0, 0.0)),
        (_CONTROL, "zebrafish feeding notes", _vec(0.0, 1.0)),
    ):
        con.execute(
            "INSERT INTO topics (slug, title, body, updated_at) VALUES (?, ?, ?, ?)",
            (slug, slug, body, "2026-10-01T00:00:00+00:00"),
        )
        con.execute(
            "INSERT INTO topic_revisions (slug, revised_at, body) VALUES (?, ?, ?)",
            (slug, "2026-09-01T00:00:00+00:00", body),
        )
        con.execute(
            "INSERT INTO memory_embeddings (profile, kind, ref, chunk_idx, chunk_text, embedding) "
            "VALUES (?, 'topic', ?, 0, ?, ?)",
            (_PROFILE, slug, body, vec),
        )
    con.commit()
    con.close()


class _ReplicaCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        self.snap = self.data / hs.SNAPSHOT_NAME
        self._patches = [
            mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(self.data)}),
            mock.patch.object(hs, "snapshot_path", return_value=self.snap),
            mock.patch.object(hs, "meta_path", return_value=self.data / hs.META_NAME),
        ]
        for p in self._patches:
            p.start()
        hs._MATRIX_CACHE.clear()

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        hs._MATRIX_CACHE.clear()
        self._tmp.cleanup()

    def _purge(self) -> tuple[int, dict, str]:
        args = cli.build_parser().parse_args(["topic", "purge", _PURGED, "--yes"])
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("khipu.db.connect", return_value=_HubConn()), \
                redirect_stdout(out), redirect_stderr(err):
            rc = args.func(args)
        return rc, json.loads(out.getvalue()), err.getvalue()

    def _keyword_slugs(self) -> set[str]:
        return {r["id"] for r in hs.search_snapshot("zebrafish", 20, kind="topic")}

    def _vector_slugs(self) -> set[str]:
        rows = hs.cosine_candidates_snapshot([1.0, 1.0], _PROFILE, limit=10, kind="topic")
        return {r["id"] for r in rows}


class PurgeRemovesTopicFromReplicaTest(_ReplicaCase):
    def test_replica_readers_stop_returning_the_purged_topic(self) -> None:
        _build(self.snap)
        self.assertEqual(self._keyword_slugs(), {_PURGED, _CONTROL})
        self.assertEqual(self._vector_slugs(), {_PURGED, _CONTROL})
        self.assertIsNotNone(hs.topic_detail_snapshot(_PURGED))
        hs._MATRIX_CACHE.clear()

        rc, out, _err = self._purge()

        self.assertEqual(rc, 0, out)
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["replica"]["ok"], out)
        self.assertEqual(self._keyword_slugs(), {_CONTROL})
        self.assertEqual(self._vector_slugs(), {_CONTROL})
        self.assertIsNone(hs.topic_detail_snapshot(_PURGED))
        self.assertIsNotNone(hs.topic_detail_snapshot(_CONTROL))
        con = sqlite3.connect(str(self.snap))
        try:
            for table in ("topics", "topic_revisions", "memory_embeddings"):
                col = "ref" if table == "memory_embeddings" else "slug"
                self.assertEqual(
                    {r[0] for r in con.execute(f"SELECT {col} FROM {table}")}, {_CONTROL}, table,
                )
            # The FTS index holds no entry for the purged row.
            control_rowid = con.execute(
                "SELECT rowid FROM topics WHERE slug = ?", (_CONTROL,)
            ).fetchone()[0]
            self.assertEqual(
                [r[0] for r in con.execute(
                    "SELECT rowid FROM topics_fts WHERE topics_fts MATCH 'zebrafish'"
                )],
                [control_rowid],
            )
            # Raises when the index and its content table disagree.
            con.execute("INSERT INTO topics_fts(topics_fts) VALUES ('integrity-check')")
        finally:
            con.close()

    def test_a_replica_without_fts_tables_is_purged_too(self) -> None:
        _build(self.snap, version=3)
        rc, out, _err = self._purge()
        self.assertEqual(rc, 0, out)
        self.assertTrue(out["replica"]["ok"], out)
        self.assertEqual(self._keyword_slugs(), {_CONTROL})

    def test_an_fts_index_without_its_delete_trigger_is_cleaned_too(self) -> None:
        _build(self.snap)
        con = sqlite3.connect(str(self.snap))
        con.execute("DROP TRIGGER topics_fts_ad")
        con.commit()
        con.close()
        rc, out, _err = self._purge()
        self.assertEqual(rc, 0, out)
        con = sqlite3.connect(str(self.snap))
        try:
            con.execute("INSERT INTO topics_fts(topics_fts) VALUES ('integrity-check')")
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM topics_fts WHERE topics_fts MATCH 'zebrafish'").fetchone()[0],
                1,
            )
        finally:
            con.close()

    def test_no_replica_on_this_mac_is_not_applicable(self) -> None:
        rc, out, err = self._purge()
        self.assertEqual(rc, 0, out)
        self.assertTrue(out["ok"])
        self.assertFalse(out["replica"]["applicable"], out)
        self.assertFalse(self.snap.exists(), "a purge must never create a replica")
        self.assertIn("no local replica", err)


class PurgeReportsAReplicaItCouldNotChangeTest(_ReplicaCase):
    def _assert_not_claimed_gone(self, rc: int, out: dict, err: str) -> None:
        self.assertEqual(rc, 1, out)
        self.assertFalse(out["ok"], out)
        self.assertFalse(out["replica"]["ok"], out)
        self.assertTrue(out["replica"]["reason"], out)
        # The hub delete itself still stands and is reported.
        self.assertTrue(out["hub"]["ok"], out)
        self.assertIn("still", err)
        self.assertEqual(self._keyword_slugs(), {_PURGED, _CONTROL})

    def test_a_busy_refresh_lock_is_reported(self) -> None:
        _build(self.snap)
        lock = hs._acquire_refresh_lock()
        self.assertIsNotNone(lock)
        try:
            rc, out, err = self._purge()
        finally:
            hs._release_refresh_lock(lock)
        self._assert_not_claimed_gone(rc, out, err)
        self.assertIn("refresh", out["replica"]["reason"])

    def test_an_unwritable_replica_is_reported(self) -> None:
        _build(self.snap)
        os.chmod(self.snap, 0o444)
        try:
            rc, out, err = self._purge()
        finally:
            os.chmod(self.snap, 0o644)
        self._assert_not_claimed_gone(rc, out, err)

    def test_a_replica_without_a_topics_table_is_reported(self) -> None:
        con = sqlite3.connect(str(self.snap))
        con.execute("CREATE TABLE episodes (id INTEGER PRIMARY KEY, summary TEXT)")
        con.commit()
        con.close()
        rc, out, _err = self._purge()
        self.assertEqual(rc, 1, out)
        self.assertFalse(out["replica"]["ok"], out)
        self.assertIn("topics", out["replica"]["reason"])


if __name__ == "__main__":
    unittest.main()
