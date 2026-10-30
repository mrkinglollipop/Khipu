"""Capture identity phase 1 (docs/plans/2026-10-11-capture-identity.md):
the source_key canonical form, the has_table probe, and the episode_sources
dual write from write_pg, without a database. The migration itself runs
against real Postgres in test_episode_sources_scratch.py."""
from __future__ import annotations

import hashlib
import json
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
        if s.startswith("SELECT episode_id FROM episode_sources WHERE source_key"):
            self.calls.append((s, params))
            row = self.sources.get(params[0])
            self._result = [(row["episode_id"],)] if row else []
            return
        if s.startswith("SELECT to_jsonb(e) FROM episodes e WHERE id"):
            self.calls.append((s, params))
            e = self.episodes.get(params[0])
            self._result = [(e,)] if e else []
            return
        super().execute(sql, params)


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

    def test_distinct_session_on_legacy_identity_is_an_explicit_conflict(self):
        cur = _SourcesFakeCursor()
        self._write(cur, self._payload())
        before = dict(cur.sources)
        episodes_before = json.loads(json.dumps(cur.episodes))
        # Same legacy ts/summary with another session is distinct evidence,
        # not a replay: acknowledging it used to delete the incoming outbox.
        with self.assertRaises(cap.CaptureEvidenceConflict):
            self._write(cur, self._payload(session_id="claude_code:other"))
        self.assertEqual(json.loads(json.dumps(cur.episodes)), episodes_before)
        self.assertEqual(cur.sources, before)

    def test_origin_key_held_by_another_episode_falls_back_to_the_episode_key(self):
        # write_pg now skips a recorded key as a replay before inserting
        # (phase 2), so the fallback is reached through the file mirror's
        # insert, which has no dedup.
        from khipu.mirror import _upsert_episode

        cur = _SourcesFakeCursor()
        payload = self._payload()
        cur.sources[ci.source_key(payload)] = {"episode_id": 999, "role": "origin"}
        self.assertTrue(_upsert_episode(cur, payload))
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
        # The last probe is the dual write's; the first is the exact-window
        # dedup's reader probe, which fails closed to the pre-0027 path.
        probe = max(i for i, (s, p) in enumerate(cur.calls) if p == ("episode_sources",))
        self.assertEqual(stmts[probe - 1], "SAVEPOINT capture_source_row")
        self.assertEqual(stmts[probe + 1], "ROLLBACK TO SAVEPOINT capture_source_row")


SECRET = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"


class _WindowsFakeCursor(_SourcesFakeCursor):
    """A hub with the K3 window columns, so the exact-window dedup matches a
    sibling part by its window id and part, as the live hub does."""

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("SELECT column_name FROM information_schema.columns") and params == ("episodes",):
            super().execute(sql, params)
            self._result = [*self._result, ("window_id",), ("truncated_chars",), ("verbatim",)]
            return
        if s.startswith("SELECT id FROM episodes WHERE harness") and "window_id = %s" in s:
            self.calls.append((s, params))
            harness, sid, tr, wid, part = params
            self._result = [(eid,) for eid, e in self.episodes.items()
                            if (e.get("harness"), e.get("session_id"), e.get("transcript_range"),
                                e["raw"].get("window_id"), e["raw"].get("part")) == (harness, sid, tr, wid, part)][:1]
            return
        super().execute(sql, params)


class SiblingPartsShareTheLegacyKeyTest(_WritePgCase):
    """2026-10-27: parts 4/7 and 5/7 of one Cursor window carried the fallback
    summary part 2/7 already held, so all three shared (ts, md5(summary)) and
    the later parts were held as evidence conflicts. A sibling part is not a
    conflict: it lands as its own row, keyed by its own source."""

    FALLBACK = "Session evidence retained without a model summary."

    def _part(self, part, **kw):
        return self._payload(summary=self.FALLBACK, harness="cursor", session_id="cursor:879382d5",
                             transcript_range="0:124618", window_id="e9b8", part=part, topics=[],
                             verbatim={"paths": [f"p{part}"], "quotes": [f"quote of {part}"]}, **kw)

    def test_sibling_parts_with_the_fallback_summary_each_land(self):
        cur = _WindowsFakeCursor()
        self.assertTrue(self._write(cur, self._part("2/7"))["episode_inserted"])
        incoming = {p: self._part(p) for p in ("4/7", "5/7")}
        keys = {p: ci.source_identity(dict(v)) for p, v in incoming.items()}
        for part in ("4/7", "5/7"):
            stats = self._write(cur, incoming[part])
            self.assertTrue(stats["episode_inserted"], part)
            self.assertEqual(stats["source_key"], keys[part]["source_key"])
            row = cur.episodes[stats["episode_id"]]
            self.assertEqual(row["summary"], f"{self.FALLBACK} (part {part})")
            self.assertEqual(row["raw"]["verbatim"], incoming[part]["verbatim"])
            self.assertEqual(cur.sources[keys[part]["source_key"]]["part"], part)
            self.assertEqual(cur.sources[keys[part]["source_key"]]["payload_sha256"],
                             keys[part]["payload_sha256"])
            # The caller's payload (an outbox job's, on a retry) is untouched.
            self.assertEqual(incoming[part]["summary"], self.FALLBACK)
        self.assertEqual(len(cur.episodes), 3)
        first = next(e for e in cur.episodes.values() if e["raw"]["part"] == "2/7")
        self.assertEqual(first["summary"], self.FALLBACK)

    def test_replayed_sibling_part_is_skipped_not_duplicated(self):
        cur = _WindowsFakeCursor()
        self._write(cur, self._part("2/7"))
        landed = self._write(cur, self._part("4/7"))["episode_id"]
        replay = self._write(cur, self._part("4/7"))
        self.assertFalse(replay["episode_inserted"])
        self.assertEqual(replay["episode_id"], landed)
        self.assertEqual(len(cur.episodes), 2)

    def test_held_sibling_part_lands_after_retry_dead(self):
        import os
        import tempfile
        from pathlib import Path

        from khipu import outbox

        cur = _WindowsFakeCursor()
        self._write(cur, self._part("2/7"))
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.dict(os.environ, {"KHIPU_OUTBOX": str(Path(td) / "outbox")}), \
                mock.patch("khipu.db.connect", return_value=_FakeConn(cur)), \
                mock.patch("khipu.embed.embed_on_capture"), \
                mock.patch.object(outbox, "_queued_origin", return_value={"origin": "local:test"}):
            queued = outbox.enqueue(self._part("4/7"), reason="CaptureEvidenceConflict")
            job = json.loads(queued.read_text())
            job.update(attempts=25, evidence_conflict=True,
                       last_error="CaptureEvidenceConflict: legacy episode key conflicts")
            outbox.dead_dir().mkdir(parents=True, exist_ok=True)
            (outbox.dead_dir() / queued.name).write_text(json.dumps(job))
            queued.unlink()
            self.assertEqual(outbox.retry_dead()["moved"], 1)
            result = outbox.drain()
            self.assertEqual(result["replayed"], 1, result)
            self.assertEqual((outbox.jobs(), outbox.dead_jobs()), ([], []))
        landed = [e for e in cur.episodes.values() if e["raw"]["part"] == "4/7"]
        self.assertEqual(len(landed), 1)
        self.assertEqual(landed[0]["raw"]["verbatim"], self._part("4/7")["verbatim"])

    def test_another_window_on_the_key_is_still_a_conflict(self):
        cur = _WindowsFakeCursor()
        self._write(cur, self._part("2/7"))
        before = json.loads(json.dumps(cur.episodes))
        for other in ({"window_id": "other"}, {"transcript_range": "0:999"}):
            with self.subTest(other=other), self.assertRaises(cap.CaptureEvidenceConflict):
                self._write(cur, {**self._part("4/7"), **other})
        self.assertEqual(json.loads(json.dumps(cur.episodes)), before)


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

class VerbatimOnlyCaptureTest(unittest.TestCase):
    """2026-10-30: an empty summary used to be stored under one fixed sentence
    that 229 episodes then shared. It is now a verbatim-only record: the
    evidence stays, the summary stays empty and the payload says so."""

    WINDOW = "USER: please keep going on the rollout\n\nASSISTANT: wrote /tmp/work/rollout.md\n" * 20
    LEGACY = "Session evidence retained without a model summary."

    def _extract(self, replies, *, applies=False, transcript=None):
        from khipu import extract

        queue = iter(json.dumps(r) for r in replies)
        with mock.patch.object(extract, "_generate", side_effect=lambda *a, **k: next(queue)) as gen, \
                mock.patch.object(extract, "_fallback_applies", return_value=applies), \
                mock.patch.object(extract, "_generate_gemini", side_effect=lambda *a, **k: next(queue)) as gem:
            payload = extract.extract_memory(transcript or self.WINDOW)
        return payload, gen.call_count + gem.call_count

    def test_empty_summary_and_verbatim_only_is_a_verbatim_only_record(self):
        payload, calls = self._extract([{"summary": ""}, {"summary": ""}])
        self.assertIsNotNone(payload)
        self.assertEqual(payload["summary"], "")
        self.assertEqual(payload["summary_kind"], "verbatim_only")
        self.assertNotIn(self.LEGACY, json.dumps(payload))
        self.assertEqual(calls, 2)  # one retry, never a loop

    def test_empty_summary_with_decisions_is_retried_once(self):
        _payload, calls = self._extract([{"summary": "", "decisions": ["Use port 5433"]},
                                         {"summary": "", "decisions": ["Use port 5433"]}],
                                        transcript="ASSISTANT: decided on database configuration")
        self.assertEqual(calls, 2)

    def test_retry_with_a_real_summary_is_used(self):
        payload, calls = self._extract([{"summary": ""}, {"summary": "Rolled out the change."}])
        self.assertEqual(payload["summary"], "Rolled out the change.")
        self.assertNotIn("summary_kind", payload)
        self.assertEqual(calls, 2)

    def test_retry_with_gemini_fallback_is_used_too(self):
        payload, _calls = self._extract([{"summary": ""}, {"summary": "Rolled out the change."}], applies=True)
        self.assertEqual(payload["summary"], "Rolled out the change.")

    def test_a_window_under_the_floor_without_items_is_not_retried(self):
        payload, calls = self._extract([{"summary": ""}], transcript="USER: hi\nError: boom")
        self.assertEqual(calls, 1)
        self.assertEqual(payload["summary_kind"], "verbatim_only")

    def test_nothing_retained_is_still_none(self):
        payload, _calls = self._extract([{"summary": ""}, {"summary": ""}], transcript="ok " * 400)
        self.assertIsNone(payload)

    def test_a_failed_retry_keeps_the_first_reply(self):
        from khipu import extract

        with mock.patch.object(extract, "_generate", side_effect=[json.dumps({"summary": ""}),
                                                                  RuntimeError("limit")]), \
                mock.patch.object(extract, "_fallback_applies", return_value=False):
            payload = extract.extract_memory(self.WINDOW)
        self.assertEqual(payload["summary_kind"], "verbatim_only")

    def test_capture_accepts_an_empty_summary_only_with_the_marker(self):
        import io
        import sys

        for marker, ok in (("verbatim_only", True), (None, False)):
            body = json.dumps({"summary": "", **({"summary_kind": marker} if marker else {})})
            with mock.patch.object(sys, "stdin", io.StringIO(body)):
                if ok:
                    self.assertEqual(cap.load_payload(body)["summary_kind"], "verbatim_only")
                else:
                    with self.assertRaises(SystemExit):
                        cap.load_payload(body)

    def test_push_paths_skip_verbatim_only_rows(self):
        from khipu import summary_kind as sk

        rows = [{"kind": "episode", "id": "1", "snippet": self.LEGACY},
                {"kind": "episode", "id": "2", "snippet": "", "summary_kind": "verbatim_only"},
                {"kind": "episode", "id": "3", "snippet": "Rolled out the change."},
                {"kind": "topic", "id": "t", "snippet": ""}]
        self.assertEqual([r["id"] for r in sk.without_verbatim_only(rows)], ["3", "t"])
        self.assertTrue(sk.is_verbatim_only("anything", {"summary_kind": "verbatim_only"}))
        self.assertTrue(sk.is_verbatim_only("(part 4/7)"))

    def test_session_start_fallback_skips_verbatim_only_rows(self):
        import sqlite3

        from khipu import hub_snapshot, recall_rule

        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE episodes (id INTEGER, summary TEXT, raw TEXT, deleted_at TEXT, "
                    "ts TEXT, ingested_at TEXT)")
        rows = [(1, self.LEGACY, {"project": "p"}), (2, "", {"project": "p", "summary_kind": "verbatim_only"}),
                (3, "Real work.", {"project": "p"})]
        for eid, summary, raw in rows:
            con.execute("INSERT INTO episodes VALUES (?, ?, ?, NULL, '2026-10-30', '2026-10-30')",
                        (eid, summary, json.dumps(raw)))
        with mock.patch.object(hub_snapshot, "open_snapshot", return_value=con), \
                mock.patch.object(hub_snapshot, "snapshot_project_group", return_value=("p", ["p"])):
            text = recall_rule._stale_project_slice("p", None)
        self.assertIn("episode `3`", text)
        self.assertNotIn("episode `1`", text)
        self.assertNotIn("episode `2`", text)

    def test_backfill_marks_placeholder_rows_once(self):
        from khipu import hygiene

        class Cur:
            def __init__(self):
                self.rows = {1: {"summary": VerbatimOnlyCaptureTest.LEGACY, "raw": {"a": 1}},
                             2: {"summary": VerbatimOnlyCaptureTest.LEGACY,
                                 "raw": {"summary_kind": "verbatim_only"}},
                             3: {"summary": "Real.", "raw": {}}}
                self.result, self.rowcount = [], 0

            def _todo(self):
                return [i for i, r in self.rows.items()
                        if r["summary"].startswith(VerbatimOnlyCaptureTest.LEGACY)
                        and not (isinstance(r["raw"], dict) and r["raw"].get("summary_kind") == "verbatim_only")]

            def execute(self, sql, params=()):
                if "information_schema" in sql or "pg_attribute" in sql:
                    self.result = [(1,)]
                elif sql.startswith("SELECT id"):
                    self.result = [(i,) for i in self._todo()[:params[-1]]]
                elif sql.startswith("UPDATE episodes SET raw"):
                    marker, ids = json.loads(params[0]), params[-1]
                    for i in ids:
                        self.rows[i]["raw"].update(marker)
                    self.rowcount = len(ids)
                elif sql.startswith("SELECT COUNT"):
                    self.result = [(len(self._todo()),)]

            def fetchall(self):
                return self.result

            def fetchone(self):
                return self.result[0]

        cur = Cur()
        with mock.patch("khipu.db.has_columns", return_value=True):
            first = hygiene.apply_backfill_summary_kind(cur)
            second = hygiene.apply_backfill_summary_kind(cur)
        self.assertEqual((first["updated"], first["remaining"]), (1, 0))
        self.assertEqual(cur.rows[1]["raw"], {"a": 1, "summary_kind": "verbatim_only"})
        self.assertEqual(cur.rows[1]["summary"], self.LEGACY)  # evidence untouched
        self.assertEqual((second["scanned"], second["updated"]), (0, 0))
        self.assertEqual(cur.rows[3]["raw"], {})

    def _backfill_cursor(self, raws):
        legacy = self.LEGACY

        class Cur:
            def __init__(self):
                self.rows = {i: {"summary": legacy, "raw": r} for i, r in raws.items()}
                self.result, self.rowcount = [], 0

            def _todo(self):
                return [i for i, r in self.rows.items()
                        if not (isinstance(r["raw"], dict) and r["raw"].get("summary_kind") == "verbatim_only")]

            def execute(self, sql, params=()):
                if sql.startswith("SELECT id"):
                    self.result = [(i,) for i in self._todo()[:params[-1]]]
                elif sql.startswith("UPDATE episodes SET raw"):
                    marker, ids = json.loads(params[0]), params[-1]
                    for i in ids:
                        old = self.rows[i]["raw"]
                        if isinstance(old, dict):
                            old.update(marker)
                        elif old is None:
                            self.rows[i]["raw"] = dict(marker)
                        else:
                            self.rows[i]["raw"] = {**marker, "raw_value": old}
                    self.rowcount = len(ids)
                elif sql.startswith("SELECT COUNT"):
                    self.result = [(len(self._todo()),)]

            def fetchall(self):
                return self.result

            def fetchone(self):
                return self.result[0]

        return Cur()

    def test_backfill_wraps_non_object_raw_so_a_second_pass_updates_nothing(self):
        from khipu import hygiene

        cur = self._backfill_cursor({1: [1, 2], 2: None, 3: "text", 4: {"a": 1}})
        with mock.patch("khipu.db.has_columns", return_value=True):
            first = hygiene.apply_backfill_summary_kind(cur)
            second = hygiene.apply_backfill_summary_kind(cur)
        self.assertEqual((first["updated"], first["remaining"]), (4, 0))
        self.assertEqual(cur.rows[1]["raw"], {"summary_kind": "verbatim_only", "raw_value": [1, 2]})
        self.assertEqual(cur.rows[2]["raw"], {"summary_kind": "verbatim_only"})
        self.assertEqual(cur.rows[3]["raw"], {"summary_kind": "verbatim_only", "raw_value": "text"})
        self.assertEqual((second["scanned"], second["updated"]), (0, 0))

    def test_gemini_retry_failure_keeps_the_first_reply(self):
        from khipu import extract

        first = json.dumps({"summary": "", "decisions": ["Use port 5433"]})
        with mock.patch.object(extract, "_generate", return_value=first), \
                mock.patch.object(extract, "_fallback_applies", return_value=True), \
                mock.patch.object(extract, "_generate_gemini", side_effect=RuntimeError("gemini down")):
            payload = extract.extract_memory(self.WINDOW)
        self.assertEqual(payload["decisions"], ["Use port 5433"])
        self.assertEqual(payload["summary_kind"], "verbatim_only")

    def test_a_blank_retry_keeps_the_first_replys_items(self):
        for applies in (True, False):
            first = {"summary": "", "decisions": ["Use port 5433"], "preferences": ["Terse"]}
            retry = {"summary": "", "decisions": ["Ship Friday"]}
            payload, _calls = self._extract([first, retry], applies=applies)
            self.assertEqual(payload["decisions"], ["Use port 5433", "Ship Friday"], applies)
            self.assertEqual(payload["preferences"], ["Terse"], applies)
            self.assertEqual(payload["summary_kind"], "verbatim_only")

    def test_hub_fallback_drops_verbatim_only_rows(self):
        from khipu import recall_prompt as rp

        rows = [{"kind": "episode", "id": "99", "score": 0.9, "snippet": self.LEGACY},
                {"kind": "episode", "id": "98", "score": 0.9, "snippet": "", "summary_kind": "verbatim_only"},
                {"kind": "episode", "id": "7", "score": 0.9, "snippet": "Rolled out the change."}]
        with mock.patch.object(rp, "_snapshot_search_hits", side_effect=rp._SnapshotUnusable("missing")), \
                mock.patch("khipu.embed.hybrid_search", return_value={"results": rows}):
            out = rp._search_hits("a topical prompt", cwd=None)
        self.assertEqual([h["id"] for h in out["hits"]], ["7"])


if __name__ == "__main__":
    unittest.main()
