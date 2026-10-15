"""Capture identity phase 1 (docs/plans/2026-10-11-capture-identity.md):
the source_key canonical form, the has_table probe, and the episode_sources
dual write from write_pg, without a database. The migration itself runs
against real Postgres in test_episode_sources_scratch.py."""
from __future__ import annotations

import hashlib
import unittest
from unittest import mock

from khipu import capture as cap
from khipu import capture_identity as ci
from khipu import db
from tests.test_capture import _EpisodesFakeCursor, _FakeConn

NATURAL = {"harness": "claude_code", "session_id": "claude_code:abc",
           "transcript_range": "0:100", "window_id": "w1", "part": "1/2"}


class SourceKeyCanonicalFormTest(unittest.TestCase):
    """Fixed hashes: changing the canonical form changes every stored key, so
    these may only change together with a migration that rewrites them."""

    def test_natural_key_pinned(self):
        self.assertEqual(ci.source_key(NATURAL),
                         "2bbeb81f0e4a43df2b5d6fc87ae91115924443ff3b4cd317a084344979a5eed1")

    def test_missing_window_and_part_hash_as_empty(self):
        self.assertEqual(ci.source_key({"session_id": "claude_code:abc", "transcript_range": "0:100"}),
                         "ce038d65a567a60cf58d5b0fb3b46991767bb612751db633c331b12c609bc531")
        self.assertEqual(ci.source_key({"session_id": "claude_code:abc", "transcript_range": "0:100",
                                        "window_id": None, "part": None, "harness": None}),
                         ci.source_key({"session_id": "claude_code:abc", "transcript_range": "0:100"}))

    def test_payload_key_pinned_and_equal_to_merge_receipt(self):
        payload = {"summary": "hello", "ts": "2026-10-11T00:00:00Z", "session_id": "gw:x"}
        self.assertEqual(ci.source_key(payload),
                         "7f2ab5f7d199066351497c7a1955f3fe819cece58047c16d91909163524fca66")
        self.assertEqual(ci.source_key(payload), cap._source_receipt(payload))

    def test_episode_key_pinned(self):
        self.assertEqual(ci.episode_key(42),
                         "b19ebbf453ddbb71f6fe5e4d2913ea4a1ba2bd92763b9ca690faa76169f215dc")

    def test_empty_session_or_range_uses_the_payload_key(self):
        for p in ({**NATURAL, "session_id": ""}, {**NATURAL, "transcript_range": ""},
                  {**NATURAL, "transcript_range": None}):
            self.assertEqual(ci.source_key(p), ci.payload_sha256(p))

    def test_fields_change_the_key_and_the_separator_prevents_shifts(self):
        keys = {ci.source_key({**NATURAL, f: "other"}) for f in ci.SOURCE_FIELDS}
        self.assertEqual(len(keys | {ci.source_key(NATURAL)}), 6)
        a = {"session_id": "s", "transcript_range": "1:2", "window_id": "ab", "part": ""}
        b = {"session_id": "s", "transcript_range": "1:2", "window_id": "a", "part": "b"}
        self.assertNotEqual(ci.source_key(a), ci.source_key(b))


class _ProbeCursor:
    def __init__(self, cols):
        self.cols, self.queries = cols, 0

    def execute(self, sql, params=None):
        self.queries += 1

    def fetchall(self):
        return [(c,) for c in self.cols]


class HasTableTest(unittest.TestCase):
    def setUp(self):
        db._TABLE_COLUMNS_CACHE.clear()
        self.addCleanup(db._TABLE_COLUMNS_CACHE.clear)

    def test_absent_is_not_cached_so_a_later_migration_is_seen(self):
        cur = _ProbeCursor([])
        self.assertFalse(db.has_table(cur, "episode_sources"))
        self.assertFalse(db.has_table(cur, "episode_sources"))
        self.assertEqual(cur.queries, 2)
        cur.cols = ["source_key"]
        self.assertTrue(db.has_table(cur, "episode_sources"))

    def test_present_is_cached(self):
        cur = _ProbeCursor(["source_key"])
        self.assertTrue(db.has_table(cur, "episode_sources"))
        self.assertTrue(db.has_table(cur, "episode_sources"))
        self.assertEqual(cur.queries, 1)


class _SourcesFakeCursor(_EpisodesFakeCursor):
    """The episodes fake plus an episode_sources table (or not, with
    sources=False), answering RETURNING id and the two source-row inserts."""

    def __init__(self, *a, sources: bool = True, fail_source_insert: bool = False,
                 probe_raises: bool = False, **kw):
        super().__init__(*a, **kw)
        self.probe_raises = probe_raises
        self.sources_table = sources
        self.fail_source_insert = fail_source_insert
        self.sources: dict[str, dict] = {}

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("SELECT column_name FROM information_schema.columns") \
                and params == ("episode_sources",):
            self.calls.append((s, params))
            if self.probe_raises:
                raise RuntimeError("statement timeout")
            self._result = [("source_key",)] if self.sources_table else []
            return
        if s.startswith("INSERT INTO episode_sources"):
            self.calls.append((s, params))
            if self.fail_source_insert:
                raise RuntimeError("boom")
            if "FROM episodes WHERE id" in s:
                key, harness, sid, tr, wid, part, psha, eid = params
                e = self.episodes[eid]
                row = {"episode_id": eid, "role": "origin", "ts": e["ts"],
                       "summary_md5": hashlib.md5(e["summary"].encode()).hexdigest()}
            else:
                key, eid, role, harness, sid, tr, wid, part, ts, md5, psha = params
                row = {"episode_id": eid, "role": role, "ts": ts, "summary_md5": md5}
            row.update(harness=harness, session_id=sid, transcript_range=tr,
                       window_id=wid, part=part, payload_sha256=psha)
            if key in self.sources:
                self.rowcount = 0
            else:
                self.sources[key] = row
                self.rowcount = 1
            return
        if s.startswith("SELECT to_jsonb(e) FROM episodes e WHERE id"):
            self.calls.append((s, params))
            e = self.episodes.get(params[0])
            self._result = [(e,)] if e else []
            return
        super().execute(sql, params)
        if s.startswith("INSERT INTO episodes") and self.rowcount:
            self._result = [(self.last_inserted_id,)]


class _WritePgCase(unittest.TestCase):
    def setUp(self):
        db._TABLE_COLUMNS_CACHE.clear()
        self.addCleanup(db._TABLE_COLUMNS_CACHE.clear)
        for p in (mock.patch("khipu.topic_graph.persist_capture_graph", return_value={}),
                  mock.patch("khipu.config.path_setting", return_value=None),
                  mock.patch("khipu.embed.embed_one", side_effect=RuntimeError("no key"))):
            p.start()
            self.addCleanup(p.stop)

    def _write(self, cur, payload):
        with mock.patch("khipu.db.connect", return_value=_FakeConn(cur)):
            return cap.write_pg(payload)

    def _payload(self, **kw):
        return {"ts": "2026-10-11T00:00:00Z", "summary": "built the source table",
                "harness": "claude_code", "session_id": "claude_code:abc",
                "transcript_range": "0:100", "window_id": "w1", "part": "1/1",
                "topics": ["Capture"], **kw}


class DualWriteTest(_WritePgCase):
    def test_insert_records_one_origin_row_keyed_before_ingestion_rewrites(self):
        cur = _SourcesFakeCursor()
        payload = self._payload()
        expected = ci.source_identity(dict(payload))
        stats = self._write(cur, payload)
        self.assertTrue(stats["episode_inserted"])
        self.assertEqual(list(cur.sources), [expected["source_key"]])
        row = cur.sources[expected["source_key"]]
        self.assertEqual(row["role"], "origin")
        self.assertEqual(row["episode_id"], cur.last_inserted_id)
        self.assertEqual(row["payload_sha256"], expected["payload_sha256"])
        self.assertEqual(row["summary_md5"], hashlib.md5(b"built the source table").hexdigest())
        self.assertEqual((row["window_id"], row["part"]), ("w1", "1/1"))

    def test_summary_only_capture_is_keyed_by_payload(self):
        cur = _SourcesFakeCursor()
        payload = {"ts": "2026-10-11T00:00:00Z", "summary": "gateway note", "session_id": "gw:1"}
        expected = ci.source_identity(dict(payload))
        self._write(cur, payload)
        self.assertEqual(list(cur.sources), [expected["source_key"]])
        self.assertEqual(expected["source_key"], expected["payload_sha256"])

    def test_replay_on_the_legacy_identity_writes_no_source_row(self):
        cur = _SourcesFakeCursor()
        self._write(cur, self._payload())
        before = dict(cur.sources)
        stats = self._write(cur, self._payload(session_id="claude_code:other"))
        self.assertFalse(stats["episode_inserted"])
        self.assertEqual(cur.sources, before)

    def test_origin_key_held_by_another_episode_falls_back_to_the_episode_key(self):
        cur = _SourcesFakeCursor()
        payload = self._payload()
        cur.sources[ci.source_key(payload)] = {"episode_id": 999, "role": "origin"}
        self._write(cur, payload)
        self.assertIn(ci.episode_key(cur.last_inserted_id), cur.sources)
        self.assertEqual(cur.sources[ci.episode_key(cur.last_inserted_id)]["episode_id"],
                         cur.last_inserted_id)

    def test_merge_records_a_merged_row_with_the_receipt(self):
        existing = {1: {"ts": "2026-10-11T00:00:00Z", "session_id": "claude_code:first",
                        "summary": "built the episode sources table today", "topics": [],
                        "decisions": [], "preferences": [], "raw": {}, "harness": "claude_code",
                        "repo_root": "/r", "project": "acme/widget", "parent_session_id": None,
                        "transcript_range": "0:50", "tags": []}}
        cur = _SourcesFakeCursor(existing)
        payload = self._payload(ts="2026-10-11T00:01:00Z", project="acme/widget",
                                summary="built the episode sources table today",
                                session_id="claude_code:second")
        stats = self._write(cur, payload)
        self.assertEqual(stats["dedup"]["action"], "merge")
        receipt = cur.episodes[1]["raw"]["merged_from"][0]["source_id"]
        (key, row), = cur.sources.items()
        self.assertEqual(row["role"], "merged")
        self.assertEqual(row["episode_id"], 1)
        self.assertEqual(row["payload_sha256"], receipt)
        self.assertEqual(row["session_id"], "claude_code:second")
        self.assertEqual(row["ts"], "2026-10-11T00:01:00Z")

    def test_hub_without_the_table_writes_exactly_as_before(self):
        cur = _SourcesFakeCursor(sources=False)
        stats = self._write(cur, self._payload())
        self.assertTrue(stats["episode_inserted"])
        self.assertEqual(cur.sources, {})
        self.assertFalse(any("episode_sources" in s and s.startswith("INSERT") for s, _ in cur.calls))

    def test_a_failing_source_insert_never_fails_the_capture(self):
        cur = _SourcesFakeCursor(fail_source_insert=True)
        stats = self._write(cur, self._payload())
        self.assertTrue(stats["episode_inserted"])
        stmts = [s for s, _ in cur.calls]
        self.assertIn("ROLLBACK TO SAVEPOINT capture_source_row", stmts)

    def test_a_failing_table_probe_is_inside_the_savepoint(self):
        cur = _SourcesFakeCursor(probe_raises=True)
        stats = self._write(cur, self._payload())
        self.assertTrue(stats["episode_inserted"])
        stmts = [s for s, _ in cur.calls]
        probe = next(i for i, (s, p) in enumerate(cur.calls) if p == ("episode_sources",))
        self.assertEqual(stmts[probe - 1], "SAVEPOINT capture_source_row")
        self.assertEqual(stmts[probe + 1], "ROLLBACK TO SAVEPOINT capture_source_row")


SECRET = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"


class SourceIdentityIsSanitizedAndStableTest(_WritePgCase):
    """Review fixes: the source row holds only what the episode row holds
    (redacted), and its key depends on the incoming capture alone, never on
    hub state that enrichment reads (project inheritance, alias resolution)."""

    def test_mirror_source_row_never_holds_a_secret_the_episode_masks(self):
        from khipu.mirror import _upsert_episode

        cur = _SourcesFakeCursor()
        payload = self._payload(part=f"1/1 {SECRET}")
        self.assertTrue(_upsert_episode(cur, payload))
        (row,) = cur.sources.values()
        self.assertNotIn(SECRET, row["part"])
        self.assertEqual(row["part"], cur.episodes[cur.last_inserted_id]["raw"]["part"])

    def _inherit(self, cur, payload):
        payload["project"] = "acme/inherited"

    def test_summary_only_insert_key_ignores_enrichment(self):
        cur = _SourcesFakeCursor()
        payload = {"ts": "2026-10-11T00:00:00Z", "summary": "gateway note", "session_id": "gw:1"}
        expected = ci.source_identity(dict(payload))
        with mock.patch.object(cap, "_inherit_project", side_effect=self._inherit):
            self._write(cur, payload)
        self.assertEqual(payload["project"], "acme/inherited")
        self.assertEqual(list(cur.sources), [expected["source_key"]])
        self.assertEqual(cur.sources[expected["source_key"]]["payload_sha256"],
                         expected["payload_sha256"])

    def test_summary_only_merge_key_and_receipt_ignore_enrichment(self):
        existing = {1: {"ts": "2026-10-11T00:00:00Z", "session_id": "gw:first",
                        "summary": "built the episode sources table today", "topics": [],
                        "decisions": [], "preferences": [], "raw": {}, "harness": None,
                        "repo_root": None, "project": "acme/inherited",
                        "parent_session_id": None, "transcript_range": None, "tags": []}}
        cur = _SourcesFakeCursor(existing)
        payload = {"ts": "2026-10-11T00:01:00Z", "session_id": "gw:second",
                   "summary": "built the episode sources table today"}
        expected = ci.source_identity(dict(payload))
        with mock.patch.object(cap, "_inherit_project", side_effect=self._inherit):
            stats = self._write(cur, payload)
        self.assertEqual(stats["dedup"]["action"], "merge")
        self.assertEqual(list(cur.sources), [expected["source_key"]])
        receipt = cur.episodes[1]["raw"]["merged_from"][0]["source_id"]
        self.assertEqual(receipt, expected["payload_sha256"])

    def test_replay_of_a_receipt_written_before_the_fix_still_matches(self):
        payload = {"ts": "2026-10-11T00:01:00Z", "session_id": "gw:second", "summary": "note"}
        legacy = ci.payload_sha256({**payload, "project": "acme/inherited"})
        cur = _SourcesFakeCursor({1: {"ts": "2026-10-11T00:00:00Z", "session_id": "gw:first",
                                      "summary": "x", "raw": {"merged_from": [{"source_id": legacy}]}}})
        with mock.patch.object(cap, "_inherit_project", side_effect=self._inherit):
            stats = self._write(cur, payload)
        self.assertEqual(stats["dedup"]["action"], "skip")
        self.assertEqual(stats["dedup"]["matched_episode"], 1)
        self.assertEqual(cur.sources, {})

if __name__ == "__main__":
    unittest.main()
