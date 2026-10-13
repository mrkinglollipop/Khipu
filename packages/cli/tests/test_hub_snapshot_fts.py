"""FTS5 keyword search on the replica: same rows and order as the LIKE path,
LIKE as the fallback, triggers keep the index current (lexical index phase 1)."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from khipu import hub_snapshot as hs

_NOW = datetime.now(timezone.utc)


def _ts(days: float) -> str:
    return (_NOW - timedelta(days=days)).isoformat()


# (id, days ago, session_id, project, harness, summary, topics, decisions, preferences, people)
_EPISODES = [
    (1, 1, "claude_code:a1", "mrkinglollipop/Khipu", "claude_code",
     "The nightly wiped the biblical library and killed semantic recall", '["khipu", "nightly"]',
     '["never re-embed the library"]', "[]", '["Matt"]'),
    (2, 2, "claude_code:a2", "mrkinglollipop/Khipu", "claude_code",
     "We changed the library scan so it skips imported sources", '["library-scan"]',
     '["library scan reads the import marker"]', "[]", "[]"),
    (3, 3, "codex:b1", "mrkinglollipop/Khipu", "codex",
     "search_snapshot falls back to LIKE when the replica lacks an index", '["replica"]',
     "[]", '["keep the fallback until two nightlies"]', "[]"),
    (4, 4, "codex:b2", "other/repo", "codex",
     "Gateway deploy ran stale code while main was green", '["gateway", "deploy"]',
     '["deploy the gateway after every merge"]', "[]", "[]"),
    (5, 5, "aegis:c1", "other/repo", "",
     "Per-prompt recall lane timed out at the keyword stage under load", '["recall", "prompt"]',
     "[]", "[]", "[]"),
    (6, 30, "claude_code:a3", "mrkinglollipop/Khipu", "claude_code",
     "khipu-embed backfill ok after the nightly; library vectors restored", '["khipu-embed"]',
     "[]", "[]", '["Matt"]'),
    (7, 40, "claude_code:a4", "mrkinglollipop/Khipu", "claude_code",
     "Changes to the replica schema need a version bump", '["replica", "schema"]',
     '["bump the schema version"]', "[]", "[]"),
    (8, 2, "claude_code:a5", "mrkinglollipop/Khipu", "claude_code",
     "Nightly library scan wiped nothing this time", '["nightly"]', "[]", "[]", "[]"),
    (9, 2, "claude_code:a6", "mrkinglollipop/Khipu", "claude_code",
     "Recall prompt timing measured on a copy of the replica", '["recall"]', "[]", "[]", "[]"),
    (10, 400, "claude_code:a7", "mrkinglollipop/Khipu", "claude_code",
     "Old note about the library and the nightly", "[]", "[]", "[]", "[]"),
]

# (slug, title, body, days ago)
_TOPICS = [
    ("library-scan", "Library scan", "The library scan skips imported sources; changed 2026-10-12.", 1),
    ("nightly", "Nightly", "The nightly refreshes the replica and backfills vectors.", 2),
    ("replica", "Replica", "The sqlite replica answers keyword search when the hub is down.", 3),
    ("gateway", "Gateway", "Deploy the gateway after a merge.", 4),
    ("biblical-library", "Biblical library", "Imported, never re-embedded. The nightly must not wipe it.", 50),
]

_QUERIES = [
    "why did the nightly wipe the biblical library and what did we change in library scan",
    "change",  # substring: finds "changed" and "changes"
    "brary",  # substring inside a longer word
    "pr 36 library",  # a two-digit token sends the query to LIKE
    "search_snapshot fallback (LIKE)",  # punctuation splits the token
    "khipu-embed backfill",
    "gateway deploy stale",
    "recall prompt timeout keyword replica",
    "library",
    "zzqq nothing matches",
]


def _build(path: Path, *, version: int) -> Path:
    con = sqlite3.connect(str(path))
    hs._create_schema(con, version=version)
    for eid, days, sid, project, harness, summary, topics, decisions, prefs, people in _EPISODES:
        con.execute(
            "INSERT INTO episodes (id, ts, session_id, project, harness, summary, topics, "
            "decisions, preferences, people) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (eid, _ts(days), sid, project, harness, summary, topics, decisions, prefs, people),
        )
    for slug, title, body, days in _TOPICS:
        con.execute(
            "INSERT INTO topics (slug, title, body, updated_at) VALUES (?, ?, ?, ?)",
            (slug, title, body, _ts(days)),
        )
    con.execute("INSERT INTO nodes (id, type, name) VALUES ('module:library', 'module', 'library scan')")
    con.commit()
    con.close()
    return path


def _search(snap: Path, data: Path, query: str, limit: int = 12, **kw):
    with (
        mock.patch.object(hs, "snapshot_path", return_value=snap),
        mock.patch.object(hs, "meta_path", return_value=data / "meta.json"),
    ):
        return hs.search_snapshot(query, limit, **kw)


def _keys(rows):
    return [(r["kind"], r["id"]) for r in rows]


class FtsParityTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "fts").mkdir()
        (root / "like").mkdir()
        self.fts_dir, self.like_dir = root / "fts", root / "like"
        self.fts = _build(self.fts_dir / "hub_snapshot.sqlite", version=hs.SNAPSHOT_SCHEMA_VERSION)
        self.like = _build(self.like_dir / "hub_snapshot.sqlite", version=3)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _both(self, query: str, limit: int = 12, **kw):
        with mock.patch.object(hs, "_fts_match_terms", wraps=hs._fts_match_terms) as spy:
            fts = _search(self.fts, self.fts_dir, query, limit, **kw)
            used_fts = spy.called
        with mock.patch.object(hs, "_fts_match_terms", wraps=hs._fts_match_terms) as spy:
            like = _search(self.like, self.like_dir, query, limit, **kw)
            self.assertFalse(spy.called, "a replica without FTS tables must stay on LIKE")
        return fts, like, used_fts

    def test_schema_carries_the_fts_tables(self) -> None:
        con = sqlite3.connect(str(self.fts))
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
        con.close()
        self.assertLessEqual(
            {"episodes_fts", "topics_fts", "episodes_fts_ai", "episodes_fts_ad", "episodes_fts_au",
             "topics_fts_ai", "topics_fts_ad", "topics_fts_au"},
            names,
        )

    def test_same_rows_same_order(self) -> None:
        for query in _QUERIES:
            for limit in (12, 4):
                with self.subTest(query=query, limit=limit):
                    fts, like, used_fts = self._both(query, limit)
                    self.assertEqual(_keys(fts), _keys(like))
                    self.assertEqual(fts, like)
                    self.assertEqual(used_fts, query != "pr 36 library", "wrong path taken")

    def test_substring_inside_a_longer_word_matches_on_both_paths(self) -> None:
        for query, expected in (("change", {"2", "7"}), ("brary", {"1", "2", "6", "8", "10"})):
            with self.subTest(query=query):
                fts, like, used_fts = self._both(query, kind="episode")
                self.assertTrue(used_fts)
                self.assertEqual(fts, like)
                self.assertLessEqual(expected, {r["id"] for r in fts})

    def test_short_token_sends_the_whole_query_to_like(self) -> None:
        fts, like, used_fts = self._both("pr 36 library")
        self.assertFalse(used_fts)
        self.assertEqual(fts, like)

    def test_filters_behave_identically(self) -> None:
        cases = [
            {"kind": "episode"},
            {"kind": "topic"},
            {"since": "7d"},
            {"until": _ts(10)},
            {"project": "khipu"},
            {"session_id": "codex:"},
            {"harness": "aegis"},
            {"harness": "claude_code", "since": "3d"},
            {"kind": "topic", "project": "khipu"},
        ]
        for kw in cases:
            for query in ("nightly library recall replica deploy", "library"):
                with self.subTest(kw=kw, query=query):
                    fts, like, _ = self._both(query, **kw)
                    self.assertEqual(fts, like)

    def test_id_shaped_query_still_reaches_nodes(self) -> None:
        fts, like, _ = self._both("module:library")
        self.assertIn(("node", "module:library"), _keys(fts))
        self.assertEqual(fts, like)

    def test_token_less_query_stays_on_like(self) -> None:
        fts, like, used_fts = self._both("the and")
        self.assertFalse(used_fts)
        self.assertEqual(fts, like)

    def test_fts_error_falls_back_to_like(self) -> None:
        with mock.patch.object(hs, "_fts_match_terms", return_value=['"unterminated']):
            fts = _search(self.fts, self.fts_dir, _QUERIES[0])
        like = _search(self.like, self.like_dir, _QUERIES[0])
        self.assertTrue(like)
        self.assertEqual(fts, like)

    def test_detection_is_one_lookup_per_replica_generation(self) -> None:
        hs._FTS_STATE.update(key=None, ready=False)
        con = sqlite3.connect(str(self.fts))
        try:
            with mock.patch.object(hs, "snapshot_path", return_value=self.fts):
                traced: list[str] = []
                con.set_trace_callback(traced.append)
                self.assertTrue(hs._fts_ready(con))
                self.assertTrue(hs._fts_ready(con))
                con.set_trace_callback(None)
        finally:
            con.close()
        self.assertEqual(sum("sqlite_master" in s for s in traced), 1)


def _fts_ids(snap: Path, table: str, term: str) -> set:
    con = sqlite3.connect(str(snap))
    try:
        con.execute(f"INSERT INTO {table}({table}, rank) VALUES ('integrity-check', 1)")
        return {r[0] for r in con.execute(f"SELECT rowid FROM {table} WHERE {table} MATCH ?", (term,))}
    finally:
        con.close()


class FtsTriggerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        self.snap = _build(self.data / "hub_snapshot.sqlite", version=hs.SNAPSHOT_SCHEMA_VERSION)
        self._patches = [
            mock.patch.object(hs, "snapshot_path", return_value=self.snap),
            mock.patch.object(hs, "meta_path", return_value=self.data / "meta.json"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_upsert_episode_new_and_replaced(self) -> None:
        out = hs.upsert_episode(
            {"id": 99, "ts": _ts(0), "summary": "quokka sighting logged", "topics": ["wildlife"]}, [],
        )
        self.assertTrue(out["ok"])
        self.assertEqual(_fts_ids(self.snap, "episodes_fts", "quokka"), {99})
        self.assertEqual(_keys(hs.search_snapshot("quokka", 5)), [("episode", "99")])
        hs.upsert_episode({"id": 99, "ts": _ts(0), "summary": "wombat sighting logged"}, [])
        self.assertEqual(_fts_ids(self.snap, "episodes_fts", "quokka"), set())
        self.assertEqual(_fts_ids(self.snap, "episodes_fts", "wombat"), {99})
        self.assertEqual(_keys(hs.search_snapshot("wombat", 5)), [("episode", "99")])

    def test_topic_insert_and_update(self) -> None:
        out = hs.upsert_embeddings([], topic_rows=[
            {"slug": "quokka-notes", "title": "Quokka notes", "body": "marsupial facts", "updated_at": _ts(0)},
        ])
        self.assertTrue(out["ok"])
        con = sqlite3.connect(str(self.snap))
        rowid = con.execute("SELECT rowid FROM topics WHERE slug = 'quokka-notes'").fetchone()[0]
        con.close()
        self.assertEqual(_fts_ids(self.snap, "topics_fts", "marsupial"), {rowid})
        hs.upsert_embeddings([], topic_rows=[
            {"slug": "quokka-notes", "title": "Quokka notes", "body": "island habitat", "updated_at": _ts(0)},
        ])
        self.assertEqual(_fts_ids(self.snap, "topics_fts", "marsupial"), set())
        self.assertEqual(_fts_ids(self.snap, "topics_fts", "habitat"), {rowid})
        self.assertEqual(_keys(hs.search_snapshot("habitat", 5, kind="topic")), [("topic", "quokka-notes")])

    def test_pending_erasure_keeps_index_consistent_and_hides_the_row(self) -> None:
        self.assertIn(("episode", "1"), _keys(hs.search_snapshot("biblical", 12)))
        pending = hs._pending_erasure_dir(self.snap)
        pending.mkdir()
        (pending / "1").touch()
        con = sqlite3.connect(str(self.snap))
        hs._apply_pending_erasures(con, self.snap)
        con.execute("DELETE FROM episodes WHERE id = 6")
        con.commit()
        con.close()
        self.assertEqual(_fts_ids(self.snap, "episodes_fts", "backfill"), set())
        keys = _keys(hs.search_snapshot("biblical backfill", 12))
        self.assertNotIn(("episode", "1"), keys)
        self.assertNotIn(("episode", "6"), keys)


if __name__ == "__main__":
    unittest.main()
