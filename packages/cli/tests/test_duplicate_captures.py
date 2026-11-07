"""Duplicate captures (2026-11-02): one Cursor session stacked 20+ near-identical
episodes (same project, same session_id, same ts, a fresh paraphrase each) and
the day's embedding calls passed their cap.

Cause: a hub-only install has no legacy file writer (capture_v2), so the file
leg returned EX_SOFTWARE after the hub row was already durable. The drain read
that as a failed delivery, kept the job and re-ran it on every pass for hours.
A re-run that reached the similarity leg (or any new window id over the same
text) found no identity match, spent a dedup embedding, and inserted another
paraphrase. These tests pin the three fixes: a missing file leg is not a
failed capture, identical window text in one session never becomes a second
episode, and one session's embedding calls are bounded per day.

Temp dirs and fakes only; no database, no network, no provider.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from khipu import capture as cap
from khipu import embed as em
from khipu import session_capture as sc
from tests.test_capture import _EpisodesFakeCursor, _FakeConn

TRANSCRIPT = ("USER: please fix the three owner change losses and run the oracle\n\n"
              "ASSISTANT: " + "I changed the rule write, the crossed notice and the read budget. " * 12)


class _ContentFakeCursor(_EpisodesFakeCursor):
    """The episodes fake, plus the same-content lookup the writer issues."""

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("SELECT id FROM episodes WHERE session_id = %s AND harness = %s "
                        "AND raw->>'transcript_sha256'"):
            sid, harness, digest = params[0], params[1], params[2]
            own_window = params[3] if "raw->>'window_id' IS DISTINCT FROM" in s else None
            self.calls.append((s, tuple(params)))
            hits = [eid for eid, e in sorted(self.episodes.items())
                    if e.get("session_id") == sid and e.get("harness") == harness
                    and (e.get("raw") or {}).get("transcript_sha256") == digest
                    and not e.get("deleted_at")
                    and (own_window is None or (e.get("raw") or {}).get("window_id") != own_window)]
            self._result = [(hits[0],)] if hits else []
            return
        super().execute(sql, params)


def _episode(**raw):
    return {
        "ts": "2026-11-02T18:08:50Z", "session_id": "cursor:agent-1",
        "summary": "Fixed three Khipu defects related to owner changes.",
        "topics": [], "decisions": [], "preferences": [],
        "raw": {"transcript_sha256": cap.transcript_digest(TRANSCRIPT), **raw},
        "harness": "cursor", "repo_root": "/r", "project": "acme/khipu",
        "parent_session_id": None, "transcript_range": "0:128958", "tags": [],
    }


def _payload(**over):
    p = {"ts": "2026-11-02T18:08:50Z", "session_id": "cursor:agent-1", "harness": "cursor",
         "project": "acme/khipu", "transcript_range": "0:99", "window_id": "w1", "part": "1/2",
         "summary": "The session was about owner changes; nothing was pushed.",
         "transcript_sha256": cap.transcript_digest(TRANSCRIPT)}
    p.update(over)
    return p


class SameContentSkipTest(unittest.TestCase):
    def setUp(self):
        for target, kw in (("khipu.topic_graph.persist_capture_graph", {"return_value": {}}),
                           ("khipu.config.path_setting", {"return_value": None}),
                           ("khipu.hygiene.classify_topics", {"return_value": ([], [], False)}),
                           ("khipu.embed.embed_one", {"side_effect": RuntimeError("no key")})):
            patch = mock.patch(target, **kw)
            patch.start()
            self.addCleanup(patch.stop)

    def write(self, cur, payload):
        with mock.patch("khipu.db.connect", return_value=_FakeConn(cur)), \
                mock.patch.object(cap, "_dedup_cosine_best", return_value=None) as cosine:
            return cap.write_pg(payload), cosine

    def test_the_same_window_text_under_a_new_range_is_not_a_second_episode(self):
        # An offset reset re-reads the transcript under a new generation: new
        # range and window id, identical text, a fresh paraphrase.
        cur = _ContentFakeCursor({1: _episode()})
        stats, _ = self.write(cur, _payload(window_id="w-gen2", ts="2026-11-02T20:30:00Z"))
        self.assertFalse(stats["episode_inserted"])
        self.assertEqual(stats["episode_id"], 1)
        self.assertEqual(stats["dedup"]["action"], "skip")
        self.assertEqual(len(cur.episodes), 1)

    def test_a_skip_spends_no_embedding_call(self):
        cur = _ContentFakeCursor({1: _episode()})
        _, cosine = self.write(cur, _payload(window_id="w-gen2"))
        cosine.assert_not_called()

    def test_a_session_that_grew_still_gets_its_own_episode(self):
        cur = _ContentFakeCursor({1: _episode()})
        grown = _payload(window_id="w-grown", transcript_range="128958:200000",
                         transcript_sha256=cap.transcript_digest(TRANSCRIPT + " and then more work"),
                         summary="A second stretch of work on a different subject entirely.")
        stats, _ = self.write(cur, grown)
        self.assertTrue(stats["episode_inserted"])
        self.assertEqual(len(cur.episodes), 2)

    def test_another_session_with_the_same_text_is_not_dropped(self):
        cur = _ContentFakeCursor({1: _episode()})
        other = _payload(session_id="cursor:agent-2", window_id="w-other",
                         summary="Entirely different words about a different run.")
        stats, _ = self.write(cur, other)
        self.assertTrue(stats["episode_inserted"])

    def test_a_sibling_part_of_the_same_window_is_never_a_duplicate(self):
        cur = _ContentFakeCursor({1: _episode(window_id="w1", part="1/2")})
        sibling = _payload(window_id="w1", part="2/2", summary="Words for the second half of it.")
        stats, _ = self.write(cur, sibling)
        self.assertTrue(stats["episode_inserted"])

    def test_an_owner_note_is_never_dropped_as_a_repeat(self):
        cur = _ContentFakeCursor({1: _episode()})
        noted = _payload(window_id="w-note", verbatim={"note": "remember the oracle must be local"},
                         summary="Capture note about the oracle.")
        stats, _ = self.write(cur, noted)
        self.assertTrue(stats["episode_inserted"])


def _drain_env(root: Path) -> dict:
    return {"KHIPU_CAPTURE_HOME": str(root / "capture"), "KHIPU_DATA_DIR": str(root / "data"),
            "KHIPU_OUTBOX": str(root / "outbox")}


def _drain_patches(stack, extract, **extra):
    for patch in (
        mock.patch.object(sc, "_claim_process_start", return_value="kernel:fake"),
        mock.patch("khipu.config.capture_mode", return_value="hub"),
        mock.patch("khipu.extract.extract_memory", extract),
        mock.patch("khipu.hub_snapshot.sync_decision_changes", return_value={"ok": True}),
        mock.patch("khipu.routing_directions.capture_scan"),
        *extra.values(),
    ):
        stack.enter_context(patch)


_JOB = {"harness": "cursor", "session_id": "agent-1", "event": "sessionend",
        "ts": "2026-11-02T18:08:50Z", "offset_before": 0, "offset_after": 9,
        "transcript_range": "0:9", "transcript": TRANSCRIPT}


class DrainCarriesTheDigestTest(unittest.TestCase):
    def drained(self, job):
        seen: list = []
        with tempfile.TemporaryDirectory() as td, ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, _drain_env(Path(td))))
            _drain_patches(stack, mock.Mock(return_value={"summary": "did it", "topics": []}),
                           capture=mock.patch("khipu.capture.capture",
                                              side_effect=lambda p, **kw: seen.append(dict(p)) or 0))
            sc.enqueue(dict(job))
            sc.drain()
        return seen[0]

    def test_the_drain_stamps_the_window_text_digest_on_the_payload(self):
        self.assertEqual(self.drained(_JOB)["transcript_sha256"], cap.transcript_digest(TRANSCRIPT))

    def test_a_window_too_short_to_be_a_replay_carries_no_digest(self):
        self.assertNotIn("transcript_sha256", self.drained({**_JOB, "transcript": "USER: yes"}))


class MissingFileLegTest(unittest.TestCase):
    """No capture_v2 configured: the hub row is the record, not a failure."""

    STATS = {"episode_inserted": False, "topics_written": 0, "episode_id": 5, "source_key": "k",
             "dedup": {"action": "skip"}}
    PAYLOAD = {"ts": "2026-11-02T18:08:50Z", "session_id": "cursor:a", "summary": "s"}

    def run_capture(self, mode):
        outcome: dict = {}
        with mock.patch.object(cap, "write_pg", return_value=dict(self.STATS)), \
                mock.patch.object(cap, "_capture_v2", return_value=None), \
                mock.patch("khipu.t3.thread_for_session", return_value=None), \
                mock.patch("khipu.embed.embed_on_capture"):
            return cap._capture(dict(self.PAYLOAD), mode=mode, result=outcome), outcome

    def test_a_durable_hub_row_with_no_file_leg_succeeds_in_hub_mode(self):
        self.assertEqual(self.run_capture("hub")[0], 0)

    def test_and_in_dual_mode(self):
        self.assertEqual(self.run_capture("dual")[0], 0)

    def test_legacy_mode_has_only_the_file_leg_so_it_still_fails(self):
        rc, outcome = self.run_capture("legacy")
        self.assertEqual(rc, cap.EX_SOFTWARE)
        self.assertEqual(outcome["reason"], "file_leg_not_configured")

    def test_a_hub_write_that_fell_to_the_outbox_is_still_delivered_without_a_file_leg(self):
        with mock.patch.object(cap, "write_pg", side_effect=RuntimeError("hub down")), \
                mock.patch.object(cap, "_capture_v2", return_value=None), \
                mock.patch("khipu.t3.thread_for_session", return_value=None), \
                mock.patch("khipu.outbox.enqueue", return_value=Path("/tmp/queued.json")):
            self.assertEqual(cap._capture(dict(self.PAYLOAD), mode="hub", result={}), 0)

    def test_a_configured_path_that_is_gone_is_a_real_failure(self):
        outcome: dict = {}
        with mock.patch.object(cap, "write_pg", return_value=dict(self.STATS)), \
                mock.patch.object(cap, "_capture_v2", return_value=Path("/Volumes/gone/capture_v2.py")), \
                mock.patch("khipu.t3.thread_for_session", return_value=None), \
                mock.patch("khipu.embed.embed_on_capture"):
            rc = cap._capture(dict(self.PAYLOAD), mode="hub", result=outcome)
        self.assertEqual(rc, cap.EX_SOFTWARE)
        self.assertEqual(outcome["reason"], "file_leg_missing")
        self.assertIn("file_leg_missing", cap.REASON_CODES)

    def test_a_caller_with_no_result_dict_gets_the_same_verdict(self):
        # `khipu capture` and the probe call capture(payload) bare.
        with mock.patch.object(cap, "write_pg", return_value=dict(self.STATS)), \
                mock.patch.object(cap, "_capture_v2", return_value=None), \
                mock.patch("khipu.t3.thread_for_session", return_value=None), \
                mock.patch("khipu.embed.embed_on_capture"):
            self.assertEqual(cap.capture(dict(self.PAYLOAD), mode="hub"), 0)

    def test_a_same_content_skip_never_runs_the_file_leg(self):
        stats = {**self.STATS, "dedup": {"action": "skip", "matched_via": "same_content"}}
        with mock.patch.object(cap, "write_pg", return_value=stats), \
                mock.patch.object(cap, "run_capture_v2") as leg, \
                mock.patch("khipu.t3.thread_for_session", return_value=None), \
                mock.patch("khipu.embed.embed_on_capture"):
            self.assertEqual(cap._capture(dict(self.PAYLOAD), mode="dual", result={}), 0)
        leg.assert_not_called()

    def test_a_file_leg_that_ran_and_failed_still_fails(self):
        with mock.patch.object(cap, "write_pg", return_value=dict(self.STATS)), \
                mock.patch.object(cap, "run_capture_v2", return_value=cap.EX_SOFTWARE), \
                mock.patch("khipu.t3.thread_for_session", return_value=None), \
                mock.patch("khipu.embed.embed_on_capture"):
            self.assertEqual(cap._capture(dict(self.PAYLOAD), mode="hub", result={}), cap.EX_SOFTWARE)


class RetryDoesNotRespendTest(unittest.TestCase):
    """The loop itself: with no file leg, one drain delivers the job and
    later drains find nothing to extract, embed or write again."""

    def test_a_job_is_delivered_once_not_retried_on_every_pass(self):
        extract = mock.Mock(return_value={"summary": "did it", "topics": []})
        write_pg = mock.Mock(return_value=dict(MissingFileLegTest.STATS))
        with tempfile.TemporaryDirectory() as td, ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, _drain_env(Path(td))))
            _drain_patches(
                stack, extract,
                write_pg=mock.patch.object(cap, "write_pg", write_pg),
                v2=mock.patch.object(cap, "_capture_v2", return_value=None),
                t3=mock.patch("khipu.t3.thread_for_session", return_value=None),
                embed=mock.patch("khipu.embed.embed_on_capture"),
            )
            sc.enqueue(dict(_JOB))
            first = sc.drain()
            again = [sc.drain() for _ in range(3)]
            left = sc.queued_jobs()
        self.assertEqual((first["captured"], first["failed"]), (1, 0))
        self.assertEqual(left, [])
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(write_pg.call_count, 1)
        self.assertTrue(all(a["jobs"] == 0 for a in again))


class SessionEmbedBoundTest(unittest.TestCase):
    """One runaway session cannot spend the whole day's embedding calls."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(Path(tmp.name) / "data"),
                                           "KHIPU_EMBED_SESSION_DAILY_CALLS": "3"})
        env.start()
        self.addCleanup(env.stop)

    def test_a_session_is_refused_past_its_daily_ceiling_and_others_are_not(self):
        for _ in range(3):
            em.take_session_budget("cursor:agent-1")
        with self.assertRaises(em.BudgetExhausted) as ctx:
            em.take_session_budget("cursor:agent-1")
        self.assertIn("cursor:agent-1", str(ctx.exception))
        em.take_session_budget("cursor:agent-2")

    def test_the_count_resets_on_a_new_utc_day(self):
        for _ in range(3):
            em.take_session_budget("cursor:agent-1")
        with mock.patch.object(em, "_budget_today", return_value="2099-01-01"):
            em.take_session_budget("cursor:agent-1")

    def test_zero_turns_the_bound_off(self):
        with mock.patch.dict(os.environ, {"KHIPU_EMBED_SESSION_DAILY_CALLS": "0"}):
            for _ in range(10):
                em.take_session_budget("cursor:agent-1")

    def test_no_session_id_is_not_counted(self):
        for _ in range(10):
            em.take_session_budget(None)
            em.take_session_budget("")

    def test_the_capture_embedding_stops_before_touching_the_hub_once_the_session_is_spent(self):
        for _ in range(3):
            em.take_session_budget("cursor:agent-1")
        with mock.patch("khipu.db.connect", side_effect=AssertionError("hub reached")), \
                mock.patch.object(em, "embed_batch", side_effect=AssertionError("provider reached")):
            self.assertFalse(em.embed_on_capture(
                {"ts": "2026-11-02T18:08:50Z", "session_id": "cursor:agent-1", "summary": "s"},
                episode_id=1))

    def test_the_dedup_embedding_is_not_sent_once_the_session_is_spent(self):
        for _ in range(3):
            em.take_session_budget("cursor:agent-1")
        with mock.patch("khipu.embed._active_profile", return_value="p"), \
                mock.patch("khipu.embed.embed_one", side_effect=AssertionError("provider reached")):
            best = cap._dedup_cosine_best(
                mock.Mock(), {"session_id": "cursor:agent-1", "summary": "s"}, [{"id": 1}], 0.9)
        self.assertIsNone(best)

    def test_the_default_ceiling_leaves_room_for_a_heavy_real_session(self):
        self.assertEqual(em.SESSION_DAILY_CALL_BUDGET, 400)

    def test_the_refused_dedup_embedding_is_logged(self):
        for _ in range(3):
            em.take_session_budget("cursor:agent-1")
        with mock.patch.object(cap, "_log") as log:
            cap._dedup_cosine_best(
                mock.Mock(), {"session_id": "cursor:agent-1", "summary": "s"}, [{"id": 1}], 0.9)
        self.assertTrue(any("session embed budget exhausted" in str(c) for c in log.call_args_list))

    def test_status_lists_the_sessions_at_the_ceiling(self):
        for _ in range(3):
            em.take_session_budget("cursor:agent-1")
        em.take_session_budget("cursor:agent-2")
        status = em.session_budget_status()
        self.assertEqual((status["cap"], status["sessions"], status["at_cap"]), (3, 2, 1))
        self.assertEqual(status["at_cap_sessions"], ["cursor:agent-1"])

    def test_a_corrupt_budget_file_does_not_block_a_capture(self):
        path = em._session_budget_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        em.take_session_budget("cursor:agent-1")
        self.assertEqual(json.loads(path.read_text())["sessions"]["cursor:agent-1"], 1)


if __name__ == "__main__":
    unittest.main()
