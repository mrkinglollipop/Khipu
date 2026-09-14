"""Contract tests for Phase 2 — capture that cannot lose (K1, K2, K3, K4, K5, K7).

Each test targets one gap the phase brief named: on-demand capture (K1), the
high-value-turn trigger (K1), the no-tail-clip window split (K3), the
verbatim tier (K2) and its redaction, merge-keeps-summaries (K5), the
SubagentStop installer (K4), and the transcript-missing doctor reason (K7).

Everything here runs under a temp KHIPU_CAPTURE_HOME / temp $HOME; nothing
touches the real queue, heartbeat, Postgres, or a real harness config.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import capture as cap
from khipu import extract
from khipu import integrations as integ
from khipu import redact
from khipu import session_capture as sc


def _home(td):
    return mock.patch.dict(os.environ, {"KHIPU_CAPTURE_HOME": str(Path(td) / "kh")})


def _write(path: Path, rows: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


# ---- K1: capture now -------------------------------------------------------------

class CaptureNowFlagTest(unittest.TestCase):
    def test_flag_makes_decide_due_regardless_of_cadence_and_is_consumed(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            sc.request_capture_now("claude_code", "s1", note="remember this decision")
            due, reason = sc.decide(
                "stop", user_turns=0, chars=0, elapsed_s=0.0, stop_hook_active=False,
                capture_requested=True,
            )
            self.assertTrue(due)
            self.assertEqual(reason, "requested")
            # Second decide() call with no flag present is back to normal cadence.
            due2, reason2 = sc.decide(
                "stop", user_turns=0, chars=0, elapsed_s=0.0, stop_hook_active=False,
                capture_requested=False,
            )
            self.assertFalse(due2)

    def test_flag_file_is_deleted_on_first_consume(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            sc.request_capture_now("codex", "s2", note="x")
            first = sc._consume_capture_now("codex", "s2")
            self.assertIsNotNone(first)
            self.assertEqual(first["note"], "x")
            second = sc._consume_capture_now("codex", "s2")
            self.assertIsNone(second)

    def test_hook_main_queues_on_capture_now_and_note_lands_on_the_job(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            tp = _write(Path(td) / ".claude" / "p" / "s.jsonl", [
                {"type": "user", "message": {"role": "user", "content": "one question"}},
                {"type": "assistant", "message": {"role": "assistant", "content": "one answer"}}])
            sc.request_capture_now("claude_code", "cn1", note="phase 2 proof")
            env = {"hook_event_name": "Stop", "session_id": "cn1", "cwd": td, "transcript_path": str(tp)}
            out = sc.hook_main(json.dumps(env))
            self.assertTrue(out["due"], out)
            self.assertEqual(out["reason"], "requested")
            jobs = sc.queued_jobs()
            self.assertEqual(len(jobs), 1)
            job = json.loads(jobs[0].read_text())
            self.assertEqual(job["capture_note"], "phase 2 proof")


# ---- K1: high-value turn trigger -------------------------------------------------

class HighValueTriggerTest(unittest.TestCase):
    def test_explicit_ask_positive_and_negative(self):
        self.assertIsNotNone(sc._high_value_reason("USER: please remember this for later"))
        self.assertIsNone(sc._high_value_reason("USER: what time is it"))

    def test_correction_positive_and_negative(self):
        self.assertIsNotNone(sc._high_value_reason("USER: no, that's wrong, try again"))
        self.assertIsNone(sc._high_value_reason("USER: sounds right, thanks"))

    def test_decision_phrase_positive_and_negative(self):
        self.assertIsNotNone(sc._high_value_reason("ASSISTANT: decided to ship the fix now"))
        self.assertIsNone(sc._high_value_reason("ASSISTANT: still investigating the bug"))

    def test_long_assistant_turn_positive_and_negative(self):
        long_turn = "ASSISTANT: " + ("x" * (sc.HIGH_VALUE_ASSISTANT_CHARS + 1))
        short_turn = "ASSISTANT: " + ("x" * 100)
        self.assertIsNotNone(sc._high_value_reason(long_turn))
        self.assertIsNone(sc._high_value_reason(short_turn))

    def test_decide_fires_on_a_high_value_turn_below_the_turn_cadence(self):
        text = "USER: capture this — we just decided the rollout plan"
        due, reason = sc.decide(
            # chars only needs to clear MIN_CHARS to pass the "nothing new"
            # gate ahead of the high-value scan; window_text is scanned as-is.
            "stop", user_turns=1, chars=sc.MIN_CHARS, elapsed_s=0.0, stop_hook_active=False,
            window_text=text,
        )
        self.assertTrue(due)
        self.assertIn("high-value", reason)


# ---- K3: no-tail-clip window split ------------------------------------------------

class WindowSplitTest(unittest.TestCase):
    def test_30000_char_window_splits_into_n_lossless_parts(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            rows = []
            # ~300 chars/turn * 100 turns ~= 30,000 chars of window text.
            for i in range(50):
                rows.append({"type": "user", "message": {"role": "user",
                             "content": f"question {i} " + "x" * 120}})
                rows.append({"type": "assistant", "message": {"role": "assistant",
                             "content": f"answer {i} " + "y" * 120}})
            tp = _write(Path(td) / ".claude" / "p" / "s.jsonl", rows)
            env = {"hook_event_name": "SessionEnd", "session_id": "w1", "cwd": td,
                   "transcript_path": str(tp)}
            out = sc.hook_main(json.dumps(env))
            self.assertTrue(out["due"], out)
            jobs = sc.queued_jobs()
            self.assertGreater(len(jobs), 1, "a 30k window must split, not clip")
            self.assertLessEqual(len(jobs), sc.MAX_WINDOW_PARTS)
            payloads = [json.loads(j.read_text()) for j in jobs]
            self.assertTrue(all(j["truncated_chars"] == 0 for j in payloads))
            window_ids = {j["window_id"] for j in payloads}
            self.assertEqual(len(window_ids), 1, "siblings share one window_id")
            # Every part named, none skipped, none duplicated.
            parts = sorted(j["part"] for j in payloads)
            self.assertEqual(parts, [f"{i}/{len(jobs)}" for i in range(1, len(jobs) + 1)])

    def test_over_the_part_ceiling_merges_the_oldest_and_stamps_truncated_chars(self):
        # Tiny max_chars forces far more than MAX_WINDOW_PARTS groups from a
        # handful of messages — exercises the ceiling directly and fast,
        # rather than needing a 100k+ char fixture through hook_main().
        msgs = [("user", f"turn {i} " + "z" * 20) for i in range(20)]
        parts = sc._window_parts(msgs, max_chars=30)
        groups = sc._split_messages(msgs, 30)
        self.assertGreater(len(groups), sc.MAX_WINDOW_PARTS)
        self.assertEqual(len(parts), sc.MAX_WINDOW_PARTS)
        # Oldest part absorbed the overflow and is the only one with loss recorded.
        self.assertGreater(parts[0]["truncated_chars"], 0)
        self.assertTrue(all(p["truncated_chars"] == 0 for p in parts[1:]))


# ---- K2: verbatim tier -------------------------------------------------------------

class VerbatimExtractionTest(unittest.TestCase):
    FIXTURE = (
        "USER: the build is failing, first message\n\n"
        "ASSISTANT: looking at packages/cli/khipu/updater.py\n"
        "Traceback (most recent call last):\n"
        "ValueError: boom\n\n"
        "USER: run:\n$ pytest tests/test_updater.py -q\n"
        "this is the middle message and it is deliberately the longest of "
        "the three so first/longest/last are three genuinely distinct quotes\n\n"
        "ASSISTANT: 1 test FAILED\n\n"
        "USER: thanks, last message"
    )

    def test_extracts_error_command_path_and_three_quotes(self):
        out = extract.extract_verbatim(self.FIXTURE)
        self.assertTrue(any("Traceback" in e for e in out["errors"]))
        self.assertIn("pytest tests/test_updater.py -q", out["commands"])
        self.assertIn("packages/cli/khipu/updater.py", out["paths"])
        self.assertEqual(len(out["quotes"]), 3)
        self.assertTrue(any("first message" in q for q in out["quotes"]))
        self.assertTrue(any("deliberately the longest" in q for q in out["quotes"]))
        self.assertTrue(any("last message" in q for q in out["quotes"]))

    def test_empty_window_returns_empty_dict(self):
        self.assertEqual(extract.extract_verbatim(""), {})


class VerbatimRedactionTest(unittest.TestCase):
    def test_a_fake_key_inside_a_verbatim_quote_is_redacted(self):
        payload = {
            "summary": "s",
            "verbatim": {"quotes": ["my key is sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"]},
        }
        n = redact.redact_payload(payload)
        self.assertGreater(n, 0)
        self.assertNotIn("sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", payload["verbatim"]["quotes"][0])
        self.assertIn(redact.MASK, payload["verbatim"]["quotes"][0])

    def test_a_fake_key_inside_verbatim_note_is_redacted(self):
        payload = {"summary": "s", "verbatim": {"note": "token: sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"}}
        n = redact.redact_payload(payload)
        self.assertGreater(n, 0)
        self.assertIn(redact.MASK, payload["verbatim"]["note"])


# ---- K5: merge keeps summaries ----------------------------------------------------

class MergeKeepsSummaryTest(unittest.TestCase):
    def test_append_summary_adds_a_new_paragraph(self):
        merged = cap._append_summary("first summary.", "second summary.")
        self.assertEqual(merged, "first summary.\n\nsecond summary.")

    def test_append_summary_dedupes_exact_text(self):
        merged = cap._append_summary("same text.", "same text.")
        self.assertEqual(merged, "same text.")

    def test_append_summary_handles_empty_existing(self):
        self.assertEqual(cap._append_summary("", "only one."), "only one.")


# ---- K4: SubagentStop installer ---------------------------------------------------

class SubagentStopInstallerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._patches = [
            mock.patch.object(integ, "HOME", self.home),
            mock.patch.object(integ, "CLAUDE_JSON", self.home / ".claude.json"),
            mock.patch.object(integ, "CLAUDE_SETTINGS", self.home / ".claude" / "settings.json"),
            mock.patch.object(integ, "CODEX_TOML", self.home / ".codex" / "config.toml"),
            mock.patch.object(integ, "CODEX_HOOKS", self.home / ".codex" / "hooks.json"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_claude_install_adds_subagentstop_and_is_idempotent(self):
        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".claude" / "settings.json").write_text("{}")
        (self.home / ".claude.json").write_text("{}")
        out = integ.install("claude_code")
        self.assertTrue(out["detected"])
        self.assertTrue(any("SubagentStop" in c for c in out["changes"]), out["changes"])
        s = json.loads((self.home / ".claude" / "settings.json").read_text())
        self.assertEqual(len(s["hooks"]["SubagentStop"]), 1)
        # Idempotent: a second install adds nothing more for SubagentStop.
        out2 = integ.install("claude_code")
        self.assertFalse(any("SubagentStop" in c for c in out2["changes"]), out2["changes"])

    def test_codex_install_adds_subagentstop_and_is_idempotent(self):
        (self.home / ".codex").mkdir(parents=True)
        (self.home / ".codex" / "config.toml").write_text('hooks = true\n')
        (self.home / ".codex" / "hooks.json").write_text("{}")
        out = integ.install("codex")
        self.assertTrue(out["detected"])
        self.assertTrue(any("SubagentStop" in c for c in out["changes"]), out["changes"])
        h = json.loads((self.home / ".codex" / "hooks.json").read_text())
        self.assertEqual(len(h["hooks"]["SubagentStop"]), 1)
        out2 = integ.install("codex")
        self.assertFalse(any("SubagentStop" in c for c in out2["changes"]), out2["changes"])


# ---- K7: transcript_missing -> doctor red reason ----------------------------------

class TranscriptMissingLivenessTest(unittest.TestCase):
    def test_transcript_missing_heartbeat_becomes_a_red_reason(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            sc._heartbeat("claude_code", {
                "harness": "claude_code", "event": "sessionend", "session_id": "x",
                "due": False, "at": sc._mint_ts(), "reason": "transcript missing: /nope",
                "transcript_missing": True,
            })
            beat = sc._read_beat("claude_code")
            self.assertEqual(beat["transcript_missing"], 1)
            live = sc.liveness("claude_code")
            self.assertFalse(live["ok"], live)
            self.assertTrue(any("readable transcript" in r for r in live["reasons"]), live["reasons"])

    def test_a_transcript_missing_run_via_hook_main_increments_the_heartbeat(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            env = {"hook_event_name": "SessionEnd", "session_id": "tm1", "cwd": td,
                   "transcript_path": str(Path(td) / "nope.jsonl")}
            out = sc.hook_main(json.dumps(env), "claude_code")
            self.assertFalse(out["due"])
            self.assertIn("transcript missing", out["reason"])
            beat = sc._read_beat("claude_code")
            self.assertEqual(beat.get("transcript_missing"), 1)


if __name__ == "__main__":
    unittest.main()
