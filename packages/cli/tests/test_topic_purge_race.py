"""Embedding catch-up can never write a purged topic back.

``embed_recent_missing`` reads live topics, calls the embedding API, and only
then writes vectors to the hub and the topic plus its vectors to this Mac's
replica. A ``khipu topic purge`` landing in that gap used to leave hub vectors
for a topic that no longer exists and put the topic back into the replica
until the next full refresh. These tests fire the purge inside that gap.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import embed as em
from khipu import hub_snapshot as hs
from tests.test_topic_purge_replica import _CONTROL, _PROFILE, _PURGED, _build

_HUB_BUILT = "2026-10-15T00:00:00+00:00"


class _RaceHub:
    """The hub as catch-up sees it. ``live`` is the topics table; a purge
    removes the slug from it, as ``khipu topic purge`` does."""

    def __init__(self) -> None:
        self.live = {_PURGED, _CONTROL}
        self.inserts: list[tuple] = []
        self._result: list[tuple] = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "FROM topics t WHERE t.deleted_at IS NULL" in s:
            self._result = [(slug, slug, f"{slug} body", {"slug": slug, "title": slug,
                                                         "body": f"{slug} body"})
                            for slug in sorted(self.live)]
        elif s.startswith("SELECT 1 FROM topics") and "FOR SHARE" in s:
            self._result = [(1,)] if params[0] in self.live else []
        elif s.startswith("INSERT INTO memory_embeddings"):
            self.inserts.append(params)
            self._result = [(_HUB_BUILT,)]
        else:
            self._result = []

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None

    def cursor(self):
        return self

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TopicPurgeRaceTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        self.snap = self.data / hs.SNAPSHOT_NAME
        for p in (mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(self.data)}),
                  mock.patch.object(hs, "snapshot_path", return_value=self.snap),
                  mock.patch.object(hs, "meta_path", return_value=self.data / hs.META_NAME)):
            p.start()
            self.addCleanup(p.stop)
        hs._MATRIX_CACHE.clear()
        _build(self.snap)

    def replica(self, sql: str, params=()) -> list[tuple]:
        con = sqlite3.connect(str(self.snap))
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()

    def topic_vectors(self, slug: str) -> int:
        return self.replica("SELECT COUNT(*) FROM memory_embeddings WHERE kind = 'topic' AND ref = ?",
                            (slug,))[0][0]

    def test_a_purge_between_read_and_write_leaves_no_copy_anywhere(self):
        hub = _RaceHub()

        def embed_then_purge(api, profile):
            # The purge commits while catch-up waits on the embedding API.
            if hub.live and _PURGED in hub.live and any(_PURGED in str(a) for a in api):
                hub.live.discard(_PURGED)
                self.assertTrue(hs.purge_topic_in_snapshot(_PURGED)["ok"])
            return [[0.0] * em.DIM for _ in api]

        with mock.patch("khipu.db.connect", return_value=hub), \
                mock.patch.object(em, "_active_profile", return_value=_PROFILE), \
                mock.patch.object(em, "embed_batch", side_effect=embed_then_purge):
            em.embed_recent_missing(limit=10)
        self.assertNotIn(_PURGED, hub.live)
        self.assertEqual([p for p in hub.inserts if p[2] == _PURGED], [], "hub vectors written back")
        self.assertTrue([p for p in hub.inserts if p[2] == _CONTROL], "control not embedded")
        self.assertEqual(self.replica("SELECT slug FROM topics WHERE slug = ?", (_PURGED,)), [])
        self.assertEqual(self.topic_vectors(_PURGED), 0)
        self.assertEqual(self.replica("SELECT slug FROM topics WHERE slug = ?", (_CONTROL,)),
                         [(_CONTROL,)])

    def test_a_replica_write_that_lands_after_the_purge_is_refused(self):
        # The hub write committed before the purge; the replica half of the
        # same catch-up arrives after the purge already cleaned the replica.
        self.assertTrue(hs.purge_topic_in_snapshot(_PURGED)["ok"])
        rows = [{"profile": _PROFILE, "kind": "topic", "ref": slug, "chunk_idx": 0,
                 "chunk_text": "t", "content_hash": "h", "embedding": [0.5, 0.5],
                 "built_at": _HUB_BUILT} for slug in (_PURGED, _CONTROL)]
        sources = [{"slug": slug, "title": slug, "body": "late", "updated_at": _HUB_BUILT}
                   for slug in (_PURGED, _CONTROL)]
        out = hs.upsert_embeddings(rows, topic_rows=sources,
                                   replace_refs=[(_PROFILE, "topic", _PURGED),
                                                 (_PROFILE, "topic", _CONTROL)])
        self.assertTrue(out["ok"])
        self.assertEqual(self.replica("SELECT slug FROM topics WHERE slug = ?", (_PURGED,)), [])
        self.assertEqual(self.topic_vectors(_PURGED), 0)
        self.assertEqual(self.replica("SELECT body FROM topics WHERE slug = ?", (_CONTROL,)),
                         [("late",)])
        self.assertEqual(self.topic_vectors(_CONTROL), 1)

    def test_a_marker_is_kept_when_the_replica_cannot_be_written(self):
        lock = hs._acquire_refresh_lock()
        self.assertIsNotNone(lock)
        try:
            self.assertFalse(hs.purge_topic_in_snapshot(_PURGED)["ok"])
        finally:
            hs._release_refresh_lock(lock)
        self.assertIn(_PURGED, hs._purged_topic_slugs(self.snap))

    def test_a_full_refresh_after_the_purge_clears_the_marker(self):
        self.assertTrue(hs.purge_topic_in_snapshot(_PURGED)["ok"])
        self.assertIn(_PURGED, hs._purged_topic_slugs(self.snap))
        hs._clear_purged_topics(self.snap, dumped_after=hs._purge_marker_time(self.snap, _PURGED))
        self.assertNotIn(_PURGED, hs._purged_topic_slugs(self.snap))


class PurgeLocksTheTopicRowTest(unittest.TestCase):
    def test_purge_locks_the_row_before_deleting_its_vectors(self):
        from tests.test_topic_purge_replica import _HubConn, _HubCur
        from khipu import cli

        seen: list[str] = []
        original = _HubCur.execute

        def record(self, sql, params=None):
            seen.append(" ".join(sql.split()))
            return original(self, sql, params)

        with mock.patch.object(_HubCur, "execute", record), \
                mock.patch("khipu.db.connect", return_value=_HubConn()), \
                mock.patch("khipu.hub_snapshot.purge_topic_in_snapshot",
                           return_value={"ok": True, "applicable": False}), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            args = cli.build_parser().parse_args(["topic", "purge", _PURGED, "--yes"])
            args.func(args)
        first = seen[0]
        self.assertTrue(first.startswith("SELECT deleted_at FROM topics") and first.endswith("FOR UPDATE"),
                        first)
        self.assertLess(0, next(i for i, s in enumerate(seen) if s.startswith("DELETE FROM memory_embeddings")))


if __name__ == "__main__":
    unittest.main()
