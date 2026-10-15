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
from tests.test_capture_identity import SECRET, _SourcesFakeCursor, _WritePgCase
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

    def test_replay_of_a_merged_source_is_skipped_by_its_key_alone(self):
        # A merged source's own columns (session, range, window) differ from
        # the episode it landed in, so only its recorded key can find it.
        cur = _SourcesFakeCursor()
        first = self._write(cur, self._payload())
        merged = self._payload(session_id="codex:elsewhere", harness="codex",
                               transcript_range="40:90", window_id="w9",
                               summary="a capture merged into the first")
        ident = ci.source_identity(dict(merged))
        cur.sources[ident["source_key"]] = {"episode_id": first["episode_id"], "role": "merged"}
        again = self._write(cur, merged)
        self.assertFalse(again["episode_inserted"])
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


    def test_windowed_capture_without_a_part_ignores_the_source_key(self):
        # Historical episodes share one key when they have a window_id but no
        # part; the column match inserts them, so the key must not skip them.
        base = {"harness": "codex", "session_id": "codex:a", "transcript_range": "0:9",
                "window_id": "w1"}
        cur = _LookupCur({ci.source_key({**base, "summary": "one"}): 41})
        with mock.patch("khipu.capture_identity.sources_ready", return_value=True), \
                mock.patch("khipu.db.has_columns", return_value=True):
            first = cap._dedup_exact_window(cur, {**base, "summary": "one"})
            second = cap._dedup_exact_window(cur, {**base, "summary": "two"})
        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertFalse(any("episode_sources" in s for s in cur.calls))

    def test_a_part_or_no_window_keeps_the_source_key(self):
        parted = {**self.PAYLOAD, "window_id": "w1", "part": "1/2"}
        cur = _LookupCur({ci.source_key(parted): 8})
        with mock.patch("khipu.capture_identity.sources_ready", return_value=True):
            self.assertEqual(cap._dedup_exact_window(cur, dict(parted)), 8)
        # No natural key: the key is the payload hash, an exact replay.
        bare = {"summary": "s"}
        cur = _LookupCur({ci.source_key(bare): 9})
        with mock.patch("khipu.capture_identity.sources_ready", return_value=True):
            self.assertEqual(cap._dedup_exact_window(cur, dict(bare)), 9)


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

    def __init__(self, row, raw, sources, holders=()):
        self.row, self.raw, self.sources = row, raw, sources
        # (md5, timestamptz or None, text ts or None) rows the ownership query returns.
        self.holders = list(holders)
        self.shared_queries = 0
        self.held_sql = ""
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
        elif "AS held" in s:
            # One query for every candidate: the holders whose md5 is asked for.
            self.shared_queries += 1
            self.held_sql = s
            self._all = [h for h in self.holders if h[0] in params["md"]]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class ForgetReaderTest(unittest.TestCase):
    TS = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)
    RAW = {"ts": "2026-10-01T09:00:00Z", "summary": "first text",
           "merged_from": [{"source_id": "p2", "ts": "2026-10-01T09:02:00Z"}]}

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

    def test_a_line_another_episode_holds_is_dropped_and_reported(self):
        sources = [("k1", "origin", self.TS, _md5("first text"), None),
                   ("k2", "merged", datetime(2026, 10, 1, 9, 2, tzinfo=timezone.utc),
                    _md5("second capture"), "p2")]
        cur = _ForgetCur((self.TS, "edited text", "claude_code:a"), self.RAW, sources,
                         holders=[(_md5("second capture"),
                                   datetime(2026, 10, 1, 9, 2, tzinfo=timezone.utc), None)])
        out = forget.forget_episode(cur, 3)
        self.assertEqual(cur.shared_queries, 1)
        self.assertEqual([i["summary_md5"] for i in out["shared_identities"]],
                         [_md5("second capture")])
        self.assertEqual({i["summary_md5"] for i in out["legacy_identities"]},
                         {_md5("edited text"), _md5("first text")})
        self.assertEqual(out["unidentified_sources"], 0)

    def test_nothing_shared_by_default_and_probe_false_asks_without_sources(self):
        sources = [("k1", "origin", self.TS, _md5("first text"), None)]
        cur = _ForgetCur((self.TS, "edited text", "claude_code:a"), self.RAW, sources)
        self.assertEqual(forget.forget_episode(cur, 3)["shared_identities"], [])
        self.assertIn("episode_sources", cur.held_sql)
        cur = _ForgetCur((self.TS, "edited text", "claude_code:a"), self.RAW, None)
        out = forget.forget_episode(cur, 3)
        self.assertEqual((cur.shared_queries, out["shared_identities"]), (1, []))
        self.assertNotIn("episode_sources", cur.held_sql)

    def test_source_rows_without_a_timestamp_or_md5_are_counted(self):
        sources = [("k1", "origin", None, _md5("first text"), None),
                   ("k2", "merged", self.TS, None, "p2")]
        cur = _ForgetCur((self.TS, "edited text", "claude_code:a"), self.RAW, sources)
        self.assertEqual(forget.forget_episode(cur, 3)["unidentified_sources"], 2)

    def test_a_merged_source_without_its_row_is_recovered_from_raw(self):
        # The origin row was recorded but the merged one failed open: forget
        # must still erase the merged legacy line and remove its queued copy.
        a = {"ts": "2026-10-01T09:00:00Z", "summary": "original fixture A", "harness": "codex",
             "session_id": "codex:fixtureA", "transcript_range": "0:10"}
        b = {"ts": "2026-10-01T09:02:00Z", "summary": "merged fixture B", "harness": "codex",
             "session_id": "codex:fixtureB", "transcript_range": "0:10"}
        raw = {**a, "merged_from": [{"source_id": ci.payload_sha256(b), "payload": b,
                                     "ts": b["ts"]}]}
        sources = [(ci.source_key(a), "origin", self.TS, _md5(a["summary"]),
                    ci.payload_sha256(a))]
        cur = _ForgetCur((self.TS, "combined fixture A and B", a["session_id"]), raw, sources)
        with tempfile.TemporaryDirectory() as td, \
                mock.patch("khipu.db.connect", return_value=_FakeConn(cur)), \
                mock.patch("khipu.hub_snapshot.forget_episode_in_snapshot",
                           return_value={"ok": True}), \
                mock.patch.dict(os.environ, {"KHIPU_OUTBOX": str(Path(td) / "outbox")}):
            import json

            path = Path(td) / "episodes.jsonl"
            path.write_text(json.dumps(a) + "\n" + json.dumps(b) + "\n")
            job = outbox.enqueue(b)
            out = forget.forget_everywhere(3, memory_root=Path(td))
            left = path.read_text()
        self.assertEqual(left, "")
        self.assertFalse(job.exists())
        self.assertTrue(out["complete"], out.get("partial_errors"))
        self.assertEqual([loc["state"] for loc in out["locations"] if loc["path"] == str(job)],
                         ["removed"])

    def test_a_receipt_without_a_row_or_payload_is_unidentified(self):
        sources = [("k1", "origin", self.TS, _md5("first text"), None)]
        raw = {**self.RAW, "merged_from": [{"source_id": "gone", "ts": "2026-10-01T09:02:00Z"}]}
        cur = _ForgetCur((self.TS, "edited text", "claude_code:a"), raw, sources)
        self.assertEqual(forget.forget_episode(cur, 3)["unidentified_sources"], 1)

    def test_a_receipt_sharing_the_origin_key_with_other_content_is_recovered(self):
        a = {"ts": "2026-10-01T09:00:00Z", "summary": "first text", "harness": "codex",
             "session_id": "codex:a", "transcript_range": "0:10"}
        b = {**a, "ts": "2026-10-01T09:02:00Z", "summary": "a re-synthesis"}
        self.assertEqual(ci.source_key(a), ci.source_key(b))
        raw = {**a, "merged_from": [{"source_id": ci.payload_sha256(b), "payload": b}]}
        sources = [(ci.source_key(a), "origin", self.TS, _md5("first text"),
                    ci.payload_sha256(a))]
        cur = _ForgetCur((self.TS, "edited text", "codex:a"), raw, sources)
        out = forget.forget_episode(cur, 3)
        self.assertIn(_md5("a re-synthesis"), {i["summary_md5"] for i in out["legacy_identities"]})

    def test_malformed_receipts_are_unidentified_not_a_crash(self):
        sources = [("k1", "origin", self.TS, _md5("first text"), None)]
        for merged_from, missing in ((5, 1), ("x", 1), ([{"source_id": ["p"]}], 1),
                                     ([{"source_id": {"a": 1}, "payload": 7}], 1)):
            raw = {**self.RAW, "merged_from": merged_from}
            cur = _ForgetCur((self.TS, "edited text", "claude_code:a"), raw, sources)
            self.assertEqual(forget.forget_episode(cur, 3)["unidentified_sources"], missing,
                             merged_from)

    def test_an_origin_without_its_row_is_recovered_from_raw(self):
        sources = [("k2", "merged", datetime(2026, 10, 1, 9, 2, tzinfo=timezone.utc),
                    _md5("second capture"), "p2")]
        raw = {**self.RAW, "session_id": "claude_code:a", "transcript_range": "0:9"}
        cur = _ForgetCur((self.TS, "edited text", "claude_code:a"), raw, sources)
        out = forget.forget_episode(cur, 3)
        self.assertIn(_md5("first text"), {i["summary_md5"] for i in out["legacy_identities"]})
        origin = [r for r in out["source_rows"] if r["role"] == "origin"]
        self.assertEqual([r["source_key"] for r in origin], [ci.source_key(raw)])
        self.assertEqual(out["unidentified_sources"], 0)

    def _everywhere(self, result):
        cur = mock.MagicMock()
        cur.__enter__.return_value = cur
        with tempfile.TemporaryDirectory() as td, \
                mock.patch("khipu.db.connect", return_value=_FakeConn(cur)), \
                mock.patch.object(forget, "forget_episode", return_value=result), \
                mock.patch("khipu.hub_snapshot.forget_episode_in_snapshot",
                           return_value={"ok": True}), \
                mock.patch.dict(os.environ, {"KHIPU_OUTBOX": str(Path(td) / "outbox")}):
            return forget.forget_everywhere(3, memory_root=Path(td))

    def _result(self, **extra):
        ident = {"ts": "2026-10-01T09:00:00+00:00", "summary_md5": _md5("x")}
        return {"ok": True, "id": 3, "session_id": "claude_code:a", "identity": ident,
                "legacy_identities": [ident], "has_merged_sources": False,
                "source_rows": [], **extra}

    def test_unidentified_sources_make_the_forget_partial(self):
        out = self._everywhere(self._result(unidentified_sources=2, shared_identities=[]))
        self.assertFalse(out["complete"])
        self.assertIn("legacy_file", out["partial"])
        self.assertIn("source rows without a legacy identity: 2",
                      out["partial_errors"]["legacy_file"])

    def test_shared_identities_make_the_forget_partial(self):
        shared = [{"ts": "2026-10-01T09:02:00+00:00", "summary_md5": _md5("y")}]
        out = self._everywhere(self._result(unidentified_sources=0, shared_identities=shared))
        self.assertFalse(out["complete"])
        self.assertEqual(out["partial"].count("legacy_shared"), 1)
        self.assertEqual(out["partial_errors"]["legacy_shared"], shared)

    def test_a_job_unredacted_on_disk_matches_its_redacted_source_row(self):
        queued = {"ts": "2026-10-01T09:02:00Z", "summary": f"key {SECRET}",
                  "session_id": "claude_code:a", "transcript_range": "0:9"}
        from khipu.capture import _redact_capture_values
        import copy

        redacted = _redact_capture_values(copy.deepcopy(queued))
        self.assertNotEqual(redacted, queued)
        rows = [{"source_key": "other", "role": "origin", "ts": None, "summary_md5": None,
                 "payload_sha256": ci.payload_sha256(redacted)}]
        self.assertTrue(forget._source_copy(queued, rows))
        self.assertFalse(forget._source_copy(dict(queued, summary="unrelated"), rows))

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
        self.assertIn(str(mine_path), out["remaining"][0])

    def test_outbox_report_without_sources_is_unchanged(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.dict(os.environ, {"KHIPU_OUTBOX": str(Path(td) / "outbox")}):
            outbox.enqueue({"ts": "2026-10-01T09:30:00Z", "summary": "x",
                            "session_id": "claude_code:a"})
            out = forget._outbox_remainder("claude_code:a", source_rows=None)
        self.assertEqual(out["remaining"], ["outbox: not cleaned (capture identity needed)"])


if __name__ == "__main__":
    unittest.main()
