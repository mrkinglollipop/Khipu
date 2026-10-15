"""Capture identity phase 2 (docs/plans/2026-10-11-capture-identity.md):
write_pg reports the episode id, and every identity reader asks
episode_sources first. Each reader also runs with the table probe forced
false, where it must behave exactly as before 0027. The two audit gaps
(merge-then-reconcile, forget after an edit) run against real Postgres in
test_capture_identity_scratch.py."""
from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from khipu import capture as cap
from khipu import capture_identity as ci
from khipu import db, drift, forget, outbox
from tests.test_capture_identity import _SourcesFakeCursor, _WritePgCase
from tests.test_capture import _FakeConn


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _no_sources():
    return mock.patch("khipu.capture_identity.sources_ready", return_value=False)


class WritePgReturnsTheIdTest(_WritePgCase):
    def test_insert_reports_id_and_source_key_without_reselecting(self):
        cur = _SourcesFakeCursor()
        stats = self._write(cur, self._payload())
        self.assertTrue(stats["episode_inserted"])
        self.assertEqual(stats["episode_id"], cur.last_inserted_id)
        self.assertEqual(stats["source_key"], ci.source_key(self._payload()))
        self.assertFalse(any(s.startswith("SELECT id FROM episodes WHERE ts") for s, _ in cur.calls),
                         "the id comes from RETURNING, never from (ts, md5(summary))")

    def test_replay_of_a_recorded_source_is_skipped_by_its_key(self):
        cur = _SourcesFakeCursor()
        first = self._write(cur, self._payload())
        # Same transcript span, re-synthesised: a different summary and ts.
        again = self._write(cur, self._payload(summary="a different synthesis",
                                               ts="2026-10-11T00:05:00Z"))
        self.assertFalse(again["episode_inserted"])
        self.assertEqual(again["dedup"]["matched_via"], "exact_window")
        self.assertEqual(again["episode_id"], first["episode_id"])
        self.assertEqual(len(cur.episodes), 1)

    def test_probe_false_reports_the_id_and_dedups_by_columns(self):
        cur = _SourcesFakeCursor(sources=False)
        with _no_sources():
            first = self._write(cur, self._payload())
            again = self._write(cur, self._payload(summary="a different synthesis"))
        self.assertEqual(first["episode_id"], cur.last_inserted_id)
        self.assertEqual(again["dedup"]["action"], "skip")
        self.assertFalse(any("episode_sources" in s for s, _ in cur.calls))


class _LookupCur:
    def __init__(self, recorded=None):
        self.recorded = recorded or {}
        self.calls: list[str] = []
        self._row = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.calls.append(s)
        eid = self.recorded.get(params[0]) if "FROM episode_sources" in s else None
        self._row = (eid,) if eid else None

    def fetchone(self):
        return self._row


class ExactWindowReaderTest(unittest.TestCase):
    PAYLOAD = {"harness": "codex", "session_id": "codex:a", "transcript_range": "0:9",
               "summary": "s"}

    def test_source_key_answers_first(self):
        cur = _LookupCur({ci.source_key(self.PAYLOAD): 41})
        with mock.patch("khipu.capture_identity.sources_ready", return_value=True):
            self.assertEqual(cap._dedup_exact_window(cur, dict(self.PAYLOAD)), 41)
        self.assertEqual(len(cur.calls), 1)

    def test_probe_false_never_reads_the_table(self):
        cur = _LookupCur({ci.source_key(self.PAYLOAD): 41})
        with _no_sources():
            self.assertIsNone(cap._dedup_exact_window(cur, dict(self.PAYLOAD)))
        self.assertFalse(any("episode_sources" in s for s in cur.calls))
        self.assertTrue(any("raw @>" in s for s in cur.calls), "receipt match still runs")


class CaptureHandsTheIdOnTest(unittest.TestCase):
    def test_capture_embeds_by_id_and_fills_result(self):
        stats = {"episode_inserted": True, "topics_written": 0, "episode_id": 77,
                 "source_key": "k", "dedup": {"action": "none"}}
        seen, result = [], {}
        with mock.patch.object(cap, "write_pg", return_value=stats), \
                mock.patch("khipu.embed.embed_on_capture",
                           side_effect=lambda p, episode_id=None: seen.append(episode_id)), \
                mock.patch.dict(os.environ, {"KHIPU_HUB_FILE_MIRROR": "0"}), \
                mock.patch("khipu.t3.thread_for_session", return_value=None):
            rc = cap.capture({"ts": "2026-10-15T00:00:00Z", "summary": "s"}, mode="hub",
                             result=result)
        self.assertEqual(rc, 0)
        self.assertEqual(seen, [77])
        self.assertEqual(result["episode_id"], 77)

    def test_mcp_capture_reports_the_written_id_without_a_lookup(self):
        from khipu import mcp_server as ms

        def fake_capture(payload, mode=None, result=None):
            result.update(episode_id=5150, episode_inserted=True)
            return 0

        with mock.patch.object(ms, "_capture_mode", return_value="hub"), \
                mock.patch.object(ms, "_stdio_hook_owns_capture", return_value=False), \
                mock.patch("khipu.capture.capture", side_effect=fake_capture), \
                mock.patch("khipu.db.connect", side_effect=AssertionError("no lookup")):
            out = ms._tool_capture({"summary": "a capture", "session_id": "x:1"})
        self.assertEqual(out["episode_id"], 5150)
        self.assertIsNone(ms._captured_episode_id({}))

    def test_outbox_drain_embeds_by_the_written_id(self):
        seen = []
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.dict(os.environ, {"KHIPU_OUTBOX": str(Path(td) / "outbox")}), \
                mock.patch("khipu.capture.write_pg",
                           return_value={"episode_inserted": True, "episode_id": 9}), \
                mock.patch("khipu.embed.embed_on_capture",
                           side_effect=lambda p, episode_id=None: seen.append(episode_id)):
            outbox.enqueue({"ts": "2026-10-15T00:00:00Z", "summary": "queued"})
            out = outbox.drain()
        self.assertEqual(out["replayed"], 1)
        self.assertEqual(seen, [9])


class EmbedOnCaptureByIdTest(unittest.TestCase):
    def _first_query(self, **kw):
        calls = []
        cur = mock.MagicMock()
        cur.__enter__.return_value = cur
        cur.execute.side_effect = lambda sql, params=None: calls.append((" ".join(sql.split()), params))
        cur.fetchone.return_value = None
        with mock.patch("khipu.db.connect", return_value=_FakeConn(cur)), \
                mock.patch("khipu.embed._active_profile", return_value="p"):
            from khipu.embed import embed_on_capture

            self.assertFalse(embed_on_capture({"ts": "2026-10-15T00:00:00Z", "summary": "s"}, **kw))
        return calls[0]

    def test_an_id_selects_the_row_by_id(self):
        sql, params = self._first_query(episode_id=12)
        self.assertIn("WHERE e.id = %s", sql)
        self.assertEqual(params, (12,))

    def test_no_id_keeps_the_legacy_lookup(self):
        sql, params = self._first_query()
        self.assertIn("md5(e.summary)", sql)
        self.assertEqual(params, ("2026-10-15T00:00:00Z", _md5("s")))


class MissingInPgReaderTest(unittest.TestCase):
    def _sql(self):
        cur = mock.MagicMock()
        cur.fetchall.return_value = []
        drift.episodes_missing_in_pg(cur, [("2026-10-15T00:00:00Z", _md5("s"))])
        return " ".join(cur.execute.call_args.args[0].split())

    def test_sources_are_anti_joined_when_present(self):
        with mock.patch("khipu.capture_identity.sources_ready", return_value=True):
            sql = self._sql()
        self.assertIn("FROM episode_sources s WHERE s.ts = v.ts::timestamptz "
                      "AND s.summary_md5 = v.md", sql)
        self.assertIn("md5(e.summary) = v.md", sql, "the live identity still counts")

    def test_probe_false_is_the_pre_0027_query(self):
        with _no_sources():
            self.assertNotIn("episode_sources", self._sql())


class _ForgetCur:
    """forget_episode's statements; ``sources`` is None for a pre-0027 hub."""

    def __init__(self, row, raw, sources):
        self.row, self.raw, self.sources = row, raw, sources
        self.rowcount = 0
        self._one = None
        self._all: list = []
        db._TABLE_COLUMNS_CACHE.clear()

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.rowcount = 1 if s.startswith(("UPDATE", "DELETE")) else 0
        self._all = []
        if "information_schema.columns" in s:
            if params[0] == "episode_sources" and self.sources is not None:
                self._all = [("source_key",)]
        elif s.startswith("SELECT ts, summary, session_id FROM episodes"):
            self._one = self.row
        elif s.startswith("SELECT raw FROM episodes"):
            self._one = (self.raw,)
        elif s.startswith("SELECT source_key, role, ts, summary_md5"):
            self._all = list(self.sources or [])

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class ForgetReaderTest(unittest.TestCase):
    TS = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)
    RAW = {"ts": "2026-10-01T09:00:00Z", "summary": "first text",
           "merged_from": [{"source_id": "x", "ts": "2026-10-01T09:02:00Z"}]}

    def test_identities_come_from_every_source_row(self):
        sources = [("k1", "origin", self.TS, _md5("first text"), None),
                   ("k2", "merged", datetime(2026, 10, 1, 9, 2, tzinfo=timezone.utc),
                    _md5("second capture"), "p2")]
        cur = _ForgetCur((self.TS, "edited text", "claude_code:a"), self.RAW, sources)
        out = forget.forget_episode(cur, 3)
        got = {(i["ts"][:19], i["summary_md5"]) for i in out["legacy_identities"]}
        self.assertEqual(got, {("2026-10-01T09:00:00", _md5("edited text")),
                               ("2026-10-01T09:00:00", _md5("first text")),
                               ("2026-10-01T09:02:00", _md5("second capture"))})
        self.assertTrue(out["has_merged_sources"])
        self.assertEqual([r["source_key"] for r in out["source_rows"]], ["k1", "k2"])

    def test_probe_false_keeps_the_raw_path(self):
        cur = _ForgetCur((self.TS, "edited text", "claude_code:a"), self.RAW, None)
        out = forget.forget_episode(cur, 3)
        # A merged episode with no source table: only the live identity, as before.
        self.assertEqual([i["summary_md5"] for i in out["legacy_identities"]], [_md5("edited text")])
        self.assertTrue(out["has_merged_sources"])
        self.assertIsNone(out["source_rows"])

    def test_outbox_report_names_exact_copies_and_ignores_session_siblings(self):
        mine = {"ts": "2026-10-01T09:02:00Z", "summary": "second capture",
                "session_id": "claude_code:a"}
        sibling = {"ts": "2026-10-01T09:30:00Z", "summary": "a later capture",
                   "session_id": "claude_code:a"}
        rows = [{"source_key": ci.source_key(mine), "role": "merged",
                 "ts": "2026-10-01T09:02:00+00:00", "summary_md5": _md5("second capture"),
                 "payload_sha256": None}]
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.dict(os.environ, {"KHIPU_OUTBOX": str(Path(td) / "outbox")}):
            mine_path = outbox.enqueue(mine)
            outbox.enqueue(sibling)
            before = {p: p.read_bytes() for p in mine_path.parent.iterdir()}
            out = forget._outbox_remainder("claude_code:a", has_merged_sources=True,
                                           source_rows=rows)
            self.assertEqual({p: p.read_bytes() for p in mine_path.parent.iterdir()}, before)
        self.assertFalse(out["ok"])
        self.assertEqual(len(out["remaining"]), 1)
        self.assertTrue(out["remaining"][0].endswith(str(mine_path)))

    def test_outbox_report_without_sources_is_unchanged(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.dict(os.environ, {"KHIPU_OUTBOX": str(Path(td) / "outbox")}):
            outbox.enqueue({"ts": "2026-10-01T09:30:00Z", "summary": "x",
                            "session_id": "claude_code:a"})
            out = forget._outbox_remainder("claude_code:a", source_rows=None)
        self.assertEqual(out["remaining"], ["outbox: not cleaned (capture identity needed)"])


if __name__ == "__main__":
    unittest.main()
