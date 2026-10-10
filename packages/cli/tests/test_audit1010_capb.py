"""Hermetic capture regressions; PostgreSQL semantics are modeled, never contacted."""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from khipu import capture as cap, deliverables, embed, extract, hub_snapshot, session_capture
from tests.test_capture import _EpisodesFakeCursor, _FakeConn


def episode(**overrides):
    row = dict(id=1, ts="2026-10-10T00:00:00Z", summary="Captured the recovery command",
               session_id="codex:one", harness="codex", project="old/app", repo_root="/repo",
               parent_session_id=None, transcript_range="0:100", topics=[], decisions=[],
               preferences=[], people=[], tags=[], raw={}, verbatim={})
    row.update(overrides)
    return row


class Cursor(_EpisodesFakeCursor):
    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("SELECT to_jsonb("):
            row = self.episodes.get(params[0])
            self._result = [(copy.deepcopy(row),)] if row else []
        elif s.startswith("SELECT project FROM project_aliases"):
            self._result = [("old/app",)] if params[0] == "new/app" else []
        elif s.startswith("SELECT topics, decisions, preferences, people, raw, ts, summary"):
            names = s.split("SELECT ", 1)[1].split(" FROM ", 1)[0].split(", ")
            row = self.episodes.get(params[0])
            self._result = [tuple(copy.deepcopy(row.get(n)) for n in names)] if row else []
            self.calls.append((s, params))
        elif s.startswith("UPDATE episodes SET topics"):
            names = [part.split(" = ")[0] for part in s.split(" SET ")[1].split(" WHERE ")[0].split(", ")]
            for name, value in zip(names, params):
                self.episodes[params[-1]][name] = value if name == "summary" else json.loads(value)
        else:
            super().execute(sql, params)
            if s.startswith("SELECT column_name") and params[0] == "episodes":
                self._result.extend([("verbatim",), ("window_id",), ("truncated_chars",)])


class CaptureTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in ("open_from_episode", "auto_close", "close_session_plan"):
            self.stack.enter_context(mock.patch("khipu.commitments." + name, return_value=0))
        for name in ("insert_decisions_from_episode", "detect_reversals_from_episode"):
            self.stack.enter_context(mock.patch("khipu.decisions." + name, return_value=0))
        self.stack.enter_context(mock.patch("khipu.deliverables.insert_deliverables_from_episode", return_value=0))
        self.stack.enter_context(mock.patch("khipu.topic_graph.persist_capture_graph", return_value={}))
        self.stack.enter_context(mock.patch("khipu.config.path_setting", return_value=None))
        self.stack.enter_context(mock.patch("khipu.hygiene.classify_topics", side_effect=lambda c, ts: (ts, [], False)))
        self.stack.enter_context(mock.patch("khipu.embed.embed_one", side_effect=RuntimeError("offline")))
        self.stack.enter_context(mock.patch("khipu.t3.thread_for_session", return_value=None))

    def test_merge_preserves_evidence_notes_and_source_identity(self):
        old = episode(verbatim={"commands": ["old command"], "note": "first note"})
        cur = Cursor({1: old})
        incoming = episode(ts="2026-10-10T00:01:00Z", session_id="codex:two",
                           transcript_range="100:200", truncated_chars=15,
                           verbatim={"commands": ["new command"], "quotes": ["keep this"], "note": "second note"})
        with mock.patch.object(cap, "_reembed_merged_episode"):
            self.assertTrue(cap._merge_into_episode(cur, 1, incoming, matched_via="jaccard", score=.9))
        self.assertEqual(cur.episodes[1]["verbatim"]["commands"], ["old command", "new command"])
        self.assertIn("first note", cur.episodes[1]["verbatim"]["note"])
        self.assertIn("second note", cur.episodes[1]["verbatim"]["note"])
        source = cur.episodes[1]["raw"]["merged_from"][0]["payload"]
        self.assertEqual(source["verbatim"], incoming["verbatim"])
        self.assertEqual(source["transcript_range"], "100:200")
        self.assertEqual(source["truncated_chars"], 15)
        self.assertEqual(cur.episodes[1]["raw"]["ts"], old["ts"])
        self.assertEqual(cur.episodes[1]["raw"]["summary"], old["summary"])

    def test_concurrent_merges_re_read_under_lock(self):
        rows = {1: episode()}
        lock, barrier = threading.Lock(), threading.Barrier(2)

        class RacingCursor(Cursor):
            locked = False

            def execute(self, sql, params=None):
                if sql.startswith("SELECT topics,"):
                    self.locked = "FOR UPDATE" in sql
                    if self.locked:
                        lock.acquire()
                    super().execute(sql, params)
                    if not self.locked:
                        barrier.wait(timeout=5)
                    return
                super().execute(sql, params)

        def merge(text):
            cur = RacingCursor(rows)
            try:
                cap._merge_into_episode(cur, 1, episode(summary=text, decisions=[text]),
                                        matched_via="jaccard", score=.9)
            finally:
                if cur.locked:
                    lock.release()

        with mock.patch.object(cap, "_reembed_merged_episode"), ThreadPoolExecutor(2) as pool:
            list(pool.map(merge, ["first addition", "second addition"]))
        self.assertEqual(set(rows[1]["decisions"]), {"first addition", "second addition"})
        self.assertIn("first addition", rows[1]["summary"])
        self.assertIn("second addition", rows[1]["summary"])
        self.assertEqual(len(rows[1]["raw"]["merged_from"]), 2)

    def _merge_and_embed(self, same_summary):
        cur = Cursor({1: episode()})
        committed = copy.deepcopy(cur.episodes)

        class Writer(_FakeConn):
            def commit(self):
                super().commit()
                committed.clear()
                committed.update(copy.deepcopy(cur.episodes))

        writer = Writer(cur)
        chunks = []
        reader = _FakeConn(Cursor(committed))
        incoming = episode(summary=cur.episodes[1]["summary"] if same_summary else "Additional recovery evidence",
                           session_id="codex:two", ts="2026-10-10T00:01:00Z", people=["new person"])
        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / "snapshot.sqlite"
            with sqlite3.connect(snap) as con:
                hub_snapshot._create_schema(con)
            with mock.patch("khipu.db.connect", side_effect=[writer, reader]), \
                    mock.patch.object(cap, "_dedup_cosine_best", return_value=(1, .99, "cosine")), \
                    mock.patch.object(embed, "_active_profile", return_value="test"), \
                    mock.patch.object(embed, "_api_texts", side_effect=lambda p, pairs: [t for _, t in pairs]), \
                    mock.patch.object(embed, "embed_batch", side_effect=lambda texts, **kw: [[1.] for _ in texts]), \
                    mock.patch.object(embed, "_upsert_chunks", side_effect=lambda c, p, rows: chunks.extend(rows)), \
                    mock.patch.object(deliverables, "deliverables_for_episode", return_value=[]), \
                    mock.patch.object(hub_snapshot, "snapshot_path", return_value=snap):
                cap.write_pg(incoming)
                found = hub_snapshot.search_snapshot("recovery", 10, project="old/app", session_id="codex:one")
            self.assertTrue(chunks, "committed merged content must become an embedding chunk")
            self.assertIn(incoming["summary"], " ".join(c[3] for c in chunks))
            self.assertTrue(found, "the real SQLite replica must retain target session/project filters")

    def test_changed_summary_embedding_reads_committed_target(self):
        self._merge_and_embed(False)

    def test_equal_summary_embedding_preserves_replica_identity(self):
        self._merge_and_embed(True)

    def test_hub_outbox_handoff_does_not_require_disabled_mirror(self):
        with mock.patch.object(cap, "write_pg", side_effect=RuntimeError("offline")), \
                mock.patch("khipu.outbox.enqueue") as enqueue, \
                mock.patch.object(cap, "run_capture_v2", return_value=70) as mirror, \
                mock.patch.dict(os.environ, {"KHIPU_HUB_FILE_MIRROR": "0"}):
            self.assertEqual(cap.capture(episode(), mode="hub"), 0)
        enqueue.assert_called_once()
        mirror.assert_not_called()

    def test_hub_optional_mirror_failure_does_not_negate_commit(self):
        with mock.patch.object(cap, "write_pg", return_value={"episode_inserted": False, "topics_written": 0}), \
                mock.patch.object(cap, "run_capture_v2", return_value=70):
            self.assertEqual(cap.capture(episode(), mode="hub"), 0)

    def test_alias_is_canonicalized_before_episode_insert(self):
        cur = Cursor({})
        with mock.patch("khipu.db.connect", return_value=_FakeConn(cur)):
            cap.write_pg(episode(project="new/app"))
        self.assertEqual(next(iter(cur.episodes.values()))["project"], "old/app")

    def test_collision_with_other_source_is_not_acknowledged(self):
        old = episode()
        cur = Cursor({1: old})
        with mock.patch("khipu.db.connect", return_value=_FakeConn(cur)), \
                mock.patch.object(cap, "dedup_before_insert", return_value={"action": "none"}), \
                mock.patch.object(cap, "run_capture_v2", return_value=0), \
                mock.patch("khipu.outbox.enqueue") as enqueue:
            rc = cap.capture(episode(session_id="codex:other", project="different/app",
                                     verbatim={"note": "do not lose this"}), mode="hub")
        self.assertNotEqual(rc, 0)
        enqueue.assert_not_called()

    def test_nested_secrets_redacted_before_sql_and_outbox(self):
        secret = "sk-test" + "a" * 32
        payload = episode(decision_details=[{"text": "retain rollback", "rationale": secret, "reverses": secret}],
                          raw={"nested": [{"evidence": secret}]})
        cur = Cursor({})
        with mock.patch("khipu.db.connect", return_value=_FakeConn(cur)):
            cap.write_pg(copy.deepcopy(payload))
        self.assertNotIn(secret, json.dumps(cur.episodes))
        with mock.patch.object(cap, "write_pg", side_effect=RuntimeError("offline")), \
                mock.patch("khipu.outbox.enqueue") as enqueue, \
                mock.patch.object(cap, "run_capture_v2", return_value=0):
            cap.capture(payload, mode="hub")
        self.assertNotIn(secret, json.dumps(enqueue.call_args.args[0]))

    def test_topic_import_rejects_traversal_and_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "topics").mkdir()
            outside = root / "outside.md"
            outside.write_text("private content")
            (root / "topics" / "escape.md").symlink_to(outside)
            cur = Cursor({})
            with mock.patch("khipu.db.connect", return_value=_FakeConn(cur)), \
                    mock.patch("khipu.config.path_setting", return_value=root), \
                    mock.patch("khipu.mirror.parse_topic_file", return_value=None) as parse:
                cap.write_pg(episode(topic_pages=[{"slug": "../outside"}, {"slug": "escape"}]))
            parse.assert_not_called()

    def test_topic_import_redacts_disk_content(self):
        secret = "sk-test" + "a" * 32
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "topics").mkdir()
            (root / "topics" / "safe.md").write_text("# Topic\n" + secret)
            with mock.patch("khipu.db.connect", return_value=_FakeConn(Cursor({}))), \
                    mock.patch("khipu.config.path_setting", return_value=root), \
                    mock.patch("khipu.mirror._upsert_topic", return_value=True) as upsert:
                cap.write_pg(episode(topic_pages=[{"slug": "safe"}]))
            self.assertNotIn(secret, json.dumps(upsert.call_args.args[1]))

    def test_invalid_timestamp_rejected_before_any_side_effect(self):
        for ts in ("../../escape", "/tmp/escape", "2026-99-99T00:00:00Z", {"bad": 1}):
            with self.subTest(ts=ts), mock.patch.object(cap, "write_pg") as write, \
                    mock.patch("khipu.outbox.enqueue") as enqueue:
                self.assertEqual(cap.capture(episode(ts=ts), mode="hub"), cap.EX_DATAERR)
                write.assert_not_called()
                enqueue.assert_not_called()
            with self.assertRaises(SystemExit) as caught:
                cap.load_payload(json.dumps(episode(ts=ts)))
            self.assertEqual(caught.exception.code, cap.EX_DATAERR)


class ExtractionTest(unittest.TestCase):
    def test_durable_hub_handoff_is_not_extracted_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = root / "job.json"
            job.write_text(json.dumps(dict(harness="codex", session_id="test", event="Stop",
                                           transcript="USER: retain the rollback instruction")))
            with mock.patch.object(session_capture, "queued_jobs", side_effect=lambda: [job] if job.exists() else []), \
                    mock.patch.object(session_capture, "_claim", side_effect=lambda p: p), \
                    mock.patch.object(session_capture, "_reclaim_stale", return_value=0), \
                    mock.patch.object(session_capture, "_record_drain"), \
                    mock.patch.object(hub_snapshot, "sync_decision_changes", return_value={}), \
                    mock.patch.object(extract, "_generate", return_value='{"summary":"Retain rollback"}') as generate, \
                    mock.patch.object(cap, "write_pg", side_effect=RuntimeError("offline")), \
                    mock.patch.object(cap, "run_capture_v2", return_value=70) as mirror, \
                    mock.patch("khipu.t3.thread_for_session", return_value=None), \
                    mock.patch.dict(os.environ, {"KHIPU_CAPTURE_MODE": "hub", "KHIPU_HUB_FILE_MIRROR": "0",
                                                 "KHIPU_OUTBOX": str(root / "outbox")}):
                self.assertEqual(session_capture.drain()["captured"], 1)
                self.assertEqual(session_capture.drain()["jobs"], 0)
                from khipu.outbox import status

                self.assertEqual(status()["pending"], 1)
            generate.assert_called_once()
            mirror.assert_not_called()

    def test_malformed_summary_is_retryable(self):
        for response in ({}, {"summary": None}, {"summary": 1}, {"summary": {}}, {"summary": [{}]}):
            with self.subTest(response=response), mock.patch.object(extract, "_generate", return_value=json.dumps(response)):
                with self.assertRaises(RuntimeError):
                    extract.extract_memory("USER: retain this")

    def test_empty_summary_keeps_structured_memory(self):
        with mock.patch.object(extract, "_generate", return_value=json.dumps({"summary": "", "decisions": ["Use port 5433"]})):
            payload = extract.extract_memory("ASSISTANT: decided on database configuration")
        self.assertIsNotNone(payload)
        self.assertEqual(payload["decisions"], ["Use port 5433"])

    def test_drain_preserves_evidence_and_note_on_empty_summary(self):
        transcript = "USER: Remember the rollback command\n\nASSISTANT:\n```sh\nrestore --previous\n```\nError: failed rollout\nhttps://github.com/example/app/pull/12"
        expected = extract.extract_verbatim(transcript)
        self.assertTrue(expected.get("commands"))
        self.assertTrue(expected.get("errors"))
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "job.json"
            job.write_text(json.dumps(dict(harness="codex", session_id="test", transcript=transcript,
                                           capture_note="remember this rollback", event="Stop")))
            with mock.patch.object(session_capture, "queued_jobs", return_value=[job]), \
                    mock.patch.object(session_capture, "_claim", side_effect=lambda p: p), \
                    mock.patch.object(session_capture, "_reclaim_stale", return_value=0), \
                    mock.patch.object(session_capture, "_record_drain"), \
                    mock.patch.object(hub_snapshot, "sync_decision_changes", return_value={}), \
                    mock.patch.object(extract, "_generate", return_value='{"summary":""}'), \
                    mock.patch.object(cap, "capture", return_value=0) as capture:
                result = session_capture.drain()
            self.assertEqual(result["captured"], 1)
            payload = capture.call_args.args[0]
            for key, value in expected.items():
                self.assertEqual(payload["verbatim"][key], value)
            self.assertEqual(payload["verbatim"]["note"], "remember this rollback")
            self.assertEqual(payload["deliverables"][0]["url"], "https://github.com/example/app/pull/12")
            self.assertFalse(job.exists())


class DeliverableTest(unittest.TestCase):
    def test_distinct_title_only_releases_survive_replay(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.execute("CREATE TABLE deliverables (id INTEGER PRIMARY KEY, project, kind, path, url, title, episode_id)")

        class SQLiteCursor:
            def execute(self, sql, params):
                self.cur = con.execute(sql.replace("%s", "?"), params)
                self.rowcount = self.cur.rowcount

            def fetchone(self):
                return self.cur.fetchone()

        payload = {"project": "app", "deliverables": extract.extract_deliverables("Released 0.4.10\nReleased 0.4.11")}
        with mock.patch.object(deliverables, "_deliverables_ready", return_value=True):
            self.assertEqual(deliverables.insert_deliverables_from_episode(SQLiteCursor(), payload, 1), 2)
            self.assertEqual(deliverables.insert_deliverables_from_episode(SQLiteCursor(), payload, 1), 0)
        self.assertEqual(con.execute("SELECT title FROM deliverables ORDER BY id").fetchall(), [("0.4.10",), ("0.4.11",)])
