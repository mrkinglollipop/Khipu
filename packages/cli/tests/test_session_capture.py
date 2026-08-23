"""Tests for the harness-native parts of khipu.session_capture — the readers for
Claude Code / Cursor / Codex transcripts, harness inference, and liveness (the
red/green "is this harness actually being recorded?" answer). The Aegis-shaped
engine tests (queue, claim, drain, sandbox) live in test_aegis_capture.py and
run against the same module through its compatibility name.

Everything runs under a temp KHIPU_CAPTURE_HOME; nothing here touches the real
queue, heartbeat, Postgres or the model.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from khipu import session_capture as sc

REPO = Path(__file__).resolve().parents[3]
STOP_HOOK = REPO / "packages" / "cli" / "bin" / "khipu-stop-hook"


def _home(td):
    return mock.patch.dict(os.environ, {"KHIPU_CAPTURE_HOME": str(Path(td) / "kh")})


def _write(path: Path, rows: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


class ClaudeCodeReaderTest(unittest.TestCase):
    def test_reads_claude_jsonl_strips_reminders_and_skips_tool_results(self):
        with tempfile.TemporaryDirectory() as td:
            tp = _write(Path(td) / "s.jsonl", [
                {"type": "user", "message": {"role": "user", "content":
                    "<system-reminder>\nhuge hook context\n</system-reminder>\nFix the updater"}},
                {"type": "assistant", "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "Looking."},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
                # A tool_result carried in a "user" line is NOT a user turn.
                {"type": "user", "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "x", "content": "file list"}]}},
                {"type": "assistant", "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "Done."}]}},
                {"type": "attachment", "attachment": {"foo": 1}},          # no message: dropped
                {"type": "user", "isMeta": True, "message": {"role": "user", "content": "meta"}},
                {"type": "user", "message": {"role": "user", "content": "thanks"}},
            ])
            msgs, off, users = sc.read_window(tp, 0)
            self.assertEqual(users, 2, msgs)
            self.assertEqual(msgs[0][1].strip(), "Fix the updater")
            self.assertIn(("tool", "Bash"), msgs)
            self.assertNotIn("system-reminder", sc.render(msgs))
            self.assertNotIn("file list", sc.render(msgs))
            self.assertEqual(off, tp.stat().st_size)


class CursorReaderTest(unittest.TestCase):
    def test_reads_cursor_jsonl_and_unwraps_user_query(self):
        with tempfile.TemporaryDirectory() as td:
            tp = _write(Path(td) / "c.jsonl", [
                {"role": "user", "message": {"content": [{"type": "text", "text":
                    "<timestamp>Mon</timestamp>\n<user_query>\nIs Khipu working here?\n</user_query>"}]}},
                {"role": "assistant", "message": {"content": [
                    {"type": "text", "text": "Checking."},
                    {"type": "tool_use", "name": "Shell", "input": {"command": "git status"}}]}},
            ])
            msgs, _, users = sc.read_window(tp, 0)
            self.assertEqual(users, 1)
            self.assertIn("Is Khipu working here?", msgs[0][1])
            self.assertNotIn("<user_query>", msgs[0][1])
            self.assertIn(("tool", "Shell"), msgs)


class CodexReaderTest(unittest.TestCase):
    def test_reads_codex_rollout_event_msgs_and_ignores_injected_response_items(self):
        with tempfile.TemporaryDirectory() as td:
            tp = _write(Path(td) / "r.jsonl", [
                {"type": "session_meta", "payload": {"id": "x"}},
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": "# AGENTS.md instructions ..."}]}},   # not a user turn
                {"type": "event_msg", "payload": {"type": "user_message", "message": "audit the UI"}},
                {"type": "response_item", "payload": {"type": "function_call", "name": "shell"}},
                {"type": "response_item", "payload": {"type": "reasoning", "encrypted_content": "..."}},
                {"type": "event_msg", "payload": {"type": "agent_message", "message": "13 findings"}},
                {"type": "event_msg", "payload": {"type": "token_count", "info": {}}},
            ])
            msgs, _, users = sc.read_window(tp, 0)
            self.assertEqual(users, 1, msgs)
            self.assertEqual(msgs, [("user", "audit the UI"), ("tool", "shell"), ("assistant", "13 findings")])

    def test_reads_response_item_only_rollouts_and_drops_injected_context(self):
        # Codex desktop/app-server rollouts (2026-08) carry no event_msg turns at all.
        with tempfile.TemporaryDirectory() as td:
            tp = _write(Path(td) / "r.jsonl", [
                {"type": "session_meta", "payload": {"id": "x"}},
                {"type": "response_item", "payload": {"type": "message", "role": "developer", "content": [
                    {"type": "input_text", "text": "<app-context>\n# Codex desktop context"}]}},
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": "<recommended_plugins>\n- A"},
                    {"type": "input_text", "text": "# AGENTS.md instructions for /x\n\n<INSTRUCTIONS>..."},
                    {"type": "input_text", "text": "<environment_context>\n  <cwd>/x</cwd>\n</environment_context>"}]}},
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": "<image name=[Image #1] path=\"/s.png\">"},
                    {"type": "input_image", "image_url": "data:..."},
                    {"type": "input_text", "text": "</image>"},
                    {"type": "input_text", "text": "make the composer wider"}]}},
                {"type": "response_item", "payload": {"type": "function_call", "name": "shell"}},
                {"type": "response_item", "payload": {"type": "reasoning", "encrypted_content": "..."}},
                {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "Shipped in PR #626."}]}},
            ])
            msgs, _, users = sc.read_window(tp, 0)
            self.assertEqual(users, 1, msgs)
            self.assertEqual(msgs, [("user", "make the composer wider"), ("tool", "shell"),
                                    ("assistant", "Shipped in PR #626.")])

    def test_rollout_with_both_shapes_counts_each_turn_once(self):
        with tempfile.TemporaryDirectory() as td:
            tp = _write(Path(td) / "r.jsonl", [
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": "audit the UI"}]}},
                {"type": "event_msg", "payload": {"type": "user_message", "message": "audit the UI"}},
                {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "13 findings"}]}},
                {"type": "event_msg", "payload": {"type": "agent_message", "message": "13 findings"}},
            ])
            msgs, _, users = sc.read_window(tp, 0)
            self.assertEqual(users, 1, msgs)
            self.assertEqual(msgs, [("user", "audit the UI"), ("assistant", "13 findings")])


class HarnessInferenceTest(unittest.TestCase):
    def test_infers_each_harness_from_payload_and_env(self):
        self.assertEqual(sc.infer_harness({"transcript_path": "/u/.claude/projects/x/s.jsonl"}, {}), "claude_code")
        self.assertEqual(sc.infer_harness({}, {"CLAUDE_PROJECT_DIR": "/x"}), "claude_code")
        self.assertEqual(sc.infer_harness({"conversation_id": "c", "workspace_roots": ["/x"]}, {}), "cursor")
        self.assertEqual(sc.infer_harness({"transcript_path": "/u/.codex/sessions/r.jsonl"}, {}), "codex")
        self.assertEqual(sc.infer_harness({}, {"GROK_HOOK_EVENT": "stop"}), "aegis")
        self.assertEqual(sc.infer_harness({}, {"KHIPU_HARNESS": "aegis", "CLAUDE_PROJECT_DIR": "/x"}), "aegis")
        self.assertEqual(sc.infer_harness({}, {}), "unknown")

    def test_cursor_and_claude_payload_shapes_drive_the_hook(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            tp = _write(Path(td) / ".cursor" / "projects" / "p" / "agent-transcripts" / "cid" / "cid.jsonl", [
                {"role": "user", "message": {"content": [{"type": "text", "text": "q " * 200}]}},
                {"role": "assistant", "message": {"content": [{"type": "text", "text": "a " * 50}]}},
            ])
            # Cursor's own envelope: conversation_id + workspace_roots, camel event name.
            out = sc.hook_main(json.dumps({"hook_event_name": "preCompact", "conversation_id": "cid",
                                           "workspace_roots": [td], "transcript_path": str(tp)}))
            self.assertEqual((out["harness"], out["event"], out["due"]), ("cursor", "precompact", True), out)
            job = json.loads(sc.queued_jobs()[0].read_text())
            self.assertEqual((job["harness"], job["session_id"], job["cwd"]), ("cursor", "cid", td))
            # A Cursor stop that omits transcript_path is located by conversation id.
            with mock.patch.object(Path, "home", return_value=Path(td)):
                self.assertEqual(sc.transcript_path({"conversation_id": "cid"}, "cursor"), tp)


class CadenceTest(unittest.TestCase):
    def test_stop_captures_at_five_turns_and_at_twenty_minutes(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            tp = Path(td) / ".claude" / "projects" / "p" / "s.jsonl"
            rows = []

            def turn(i):
                rows.append({"type": "user", "message": {"role": "user", "content": f"question {i} " + "x" * 60}})
                rows.append({"type": "assistant", "message": {"role": "assistant", "content": f"answer {i} " + "y" * 60}})
                _write(tp, rows)
                return sc.hook_main(json.dumps({"hook_event_name": "Stop", "session_id": "s1", "cwd": td,
                                                "transcript_path": str(tp)}))
            outs = [turn(i) for i in range(6)]
            self.assertTrue(all(o["harness"] == "claude_code" for o in outs))
            self.assertEqual([o["due"] for o in outs], [False, False, False, False, True, False], outs)
            self.assertEqual(len(sc.queued_jobs()), 1)
            job = json.loads(sc.queued_jobs()[0].read_text())
            self.assertEqual(job["turns"], 5)
            self.assertIn("question 4", job["transcript"])
            self.assertNotIn("question 5", job["transcript"])           # window advanced past the job
            # Time-based: one more turn 20 minutes later is due even though turns < 5.
            st = sc.load_state("claude_code", "s1")
            st["last_ts"] = time.time() - sc.MIN_MINUTES * 60 - 1
            sc.save_state("claude_code", "s1", st)
            out = turn(6)
            self.assertTrue(out["due"], out)
            self.assertIn("elapsed", out["reason"])
            self.assertEqual(len(sc.queued_jobs()), 2)

    def test_session_end_captures_whatever_is_pending(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            tp = _write(Path(td) / ".claude" / "p" / "s.jsonl", [
                {"type": "user", "message": {"role": "user", "content": "one question " + "x" * 200}},
                {"type": "assistant", "message": {"role": "assistant", "content": "one answer " + "y" * 50}}])
            env = {"hook_event_name": "Stop", "session_id": "s2", "cwd": td, "transcript_path": str(tp)}
            self.assertFalse(sc.hook_main(json.dumps(env))["due"])
            env["hook_event_name"] = "SessionEnd"
            self.assertTrue(sc.hook_main(json.dumps(env))["due"])
            self.assertEqual(len(sc.queued_jobs()), 1)


class LivenessTest(unittest.TestCase):
    def _beat(self, harness, **kw):
        sc._write_beat(harness, {"harness": harness, "at": sc._mint_ts(), **kw})

    def test_unseen_harness_is_green_with_a_note(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            lv = sc.liveness("codex")
            self.assertTrue(lv["ok"])
            self.assertFalse(lv["seen"])
            self.assertIn("no real session", lv["note"])
            self.assertTrue(sc.liveness_all()["ok"])

    def test_hook_error_and_drain_error_and_stale_queue_and_stuck_cadence_are_red(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            now = sc._mint_ts()
            self._beat("claude_code", last_error="RuntimeError: boom", last_error_at=now)
            self.assertIn("hook error", " ".join(sc.liveness("claude_code")["reasons"]))
            self._beat("cursor", last_drain_error="extract: HTTPError 429", last_drain_error_at=now)
            self.assertIn("capture attempt failed", " ".join(sc.liveness("cursor")["reasons"]))
            # Stale queue: a job for codex older than the threshold.
            self._beat("codex")
            p = sc.enqueue({"harness": "codex", "session_id": "q", "ts": now, "transcript": "x", "event": "stop"})
            os.utime(p, (time.time() - sc.QUEUE_STALE_S - 5,) * 2)
            self.assertIn("nothing is draining", " ".join(sc.liveness("codex")["reasons"]))
            # Stuck cadence: many turns seen, never due.
            self._beat("aegis", pending_turns=sc.STUCK_TURNS, pending_since=now)
            self.assertIn("cadence not firing", " ".join(sc.liveness("aegis")["reasons"]))
            all_ = sc.liveness_all()
            self.assertFalse(all_["ok"])
            self.assertEqual(sorted(all_["red"]), ["aegis", "claude_code", "codex", "cursor"])

    def test_a_capture_clears_a_prior_drain_error_and_resets_pending_turns(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            self._beat("claude_code", last_drain_error="capture rc=1", last_drain_error_at=sc._mint_ts(),
                       pending_turns=4, pending_since=sc._mint_ts())
            self.assertFalse(sc.liveness("claude_code")["ok"])
            time.sleep(1.1)                                   # second-resolution timestamps
            sc._record_drain("claude_code", captured=True)
            lv = sc.liveness("claude_code")
            self.assertTrue(lv["ok"], lv)
            self.assertEqual(lv["captures"], 1)
            # And a due+queued heartbeat resets the pending-turn counter.
            sc._heartbeat("claude_code", {"at": sc._mint_ts(), "due": True, "queued": "j.json", "new_turns": 5})
            self.assertEqual(sc.liveness("claude_code")["pending_turns"], 0)

    def test_stopped_hook_is_red_when_the_transcript_kept_growing(self):
        """The B15 shape one level up: a hook that dies right after a capture
        leaves no error, no queue and pending_turns=0, so every other check
        passes. Only the transcript's own mtime can betray it."""
        with tempfile.TemporaryDirectory() as td, _home(td):
            tr = Path(td) / "session.jsonl"
            tr.write_text('{"type":"user","message":{"role":"user","content":"hi"}}\n')
            sc.save_state("claude_code", "s1", {"offset": 0, "last_ts": 0, "transcript_path": str(tr)})
            # Hook last ran two hours before the transcript was last written to.
            hook_at = time.time() - sc.HOOK_SILENT_S - 600
            self._beat("claude_code", at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(hook_at)),
                       pending_turns=0, captures=3)
            lv = sc.liveness("claude_code")
            self.assertFalse(lv["ok"], lv)
            self.assertTrue(any("stopped firing" in r for r in lv["reasons"]), lv["reasons"])
            self.assertGreater(lv["transcript_newer_than_hook_s"], sc.HOOK_SILENT_S)

    def test_a_long_turn_in_flight_is_not_mistaken_for_a_stopped_hook(self):
        """A single agentic turn can run a long time with no Stop; that is
        idleness of the hook, not failure, and must stay green."""
        with tempfile.TemporaryDirectory() as td, _home(td):
            tr = Path(td) / "session.jsonl"
            tr.write_text('{"type":"user","message":{"role":"user","content":"hi"}}\n')
            sc.save_state("cursor", "s1", {"offset": 0, "last_ts": 0, "transcript_path": str(tr)})
            hook_at = time.time() - (sc.HOOK_SILENT_S // 2)
            self._beat("cursor", at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(hook_at)))
            self.assertTrue(sc.liveness("cursor")["ok"])

    def test_a_missing_or_unrecorded_transcript_never_invents_a_reason(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            sc.save_state("codex", "s1", {"offset": 0, "last_ts": 0,
                                          "transcript_path": str(Path(td) / "gone.jsonl")})
            sc.save_state("codex", "s2", {"offset": 0, "last_ts": 0})   # no path recorded
            self._beat("codex", at="2026-01-01T00:00:00Z")
            lv = sc.liveness("codex")
            self.assertTrue(lv["ok"], lv["reasons"])
            self.assertIsNone(lv["transcript_newer_than_hook_s"])

    def test_the_hook_records_the_transcript_path_for_the_detector(self):
        """The detector is only as good as the path the hook writes down."""
        with tempfile.TemporaryDirectory() as td, _home(td):
            tr = Path(td) / "t.jsonl"
            tr.write_text('{"type":"user","message":{"role":"user","content":"hello there"}}\n')
            env = {"session_id": "sx", "transcript_path": str(tr), "hook_event_name": "PreCompact"}
            sc.hook_main(json.dumps(env), "claude_code")
            st = sc.load_state("claude_code", "sx")
            self.assertEqual(st["transcript_path"], str(tr))

    def test_activity_scan_is_bounded_and_still_finds_the_live_session(self):
        """State files are never pruned and this runs inside every doctor."""
        with tempfile.TemporaryDirectory() as td, _home(td):
            old = Path(td) / "old.jsonl"
            old.write_text("x\n")
            os.utime(old, (time.time() - 86400,) * 2)
            for i in range(sc.ACTIVITY_SCAN_LIMIT + 15):
                sc.save_state("claude_code", f"old{i}", {"offset": 0, "transcript_path": str(old)})
                os.utime(sc._state_file("claude_code", f"old{i}"), (time.time() - 86400 - i,) * 2)
            live = Path(td) / "live.jsonl"
            live.write_text("y\n")
            sc.save_state("claude_code", "live", {"offset": 0, "transcript_path": str(live)})
            newest, which = sc.transcript_activity("claude_code")
            self.assertEqual(which, str(live))
            self.assertIsNotNone(newest)

    def test_hook_error_older_than_a_later_successful_queue_is_not_red(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            self._beat("claude_code", last_error="old", last_error_at="2026-01-01T00:00:00Z",
                       last_queued_at=sc._mint_ts())
            self.assertTrue(sc.liveness("claude_code")["ok"])


class PendingTurnsAccountingTest(unittest.TestCase):
    """pending_turns is the current session's uncaptured window, not a running
    sum: summing the cumulative new_turns of every not-due Stop (1+2+3+4) made
    one ordinary four-turn session look like a stuck cadence (2026-08-22)."""

    def test_not_due_stops_do_not_accumulate_across_runs(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            started = "2026-08-22T10:00:00Z"
            for n in (1, 2, 3, 4):
                sc._heartbeat("claude_code", {"at": sc._mint_ts(), "event": "stop", "session_id": "s1",
                                              "due": False, "new_turns": n, "pending_since": started})
            beat = sc._read_beat("claude_code")
            self.assertEqual(beat["pending_turns"], 4)
            self.assertEqual(beat["pending_since"], started)
            # A second session replaces the level; it does not inherit s1's turns.
            sc._heartbeat("claude_code", {"at": sc._mint_ts(), "event": "stop", "session_id": "s2",
                                          "due": False, "new_turns": 1, "pending_since": "2026-08-22T11:00:00Z"})
            beat = sc._read_beat("claude_code")
            self.assertEqual(beat["pending_turns"], 1)
            self.assertEqual(beat["pending_since"], "2026-08-22T11:00:00Z")


class StopHookIntegrationTest(unittest.TestCase):
    """The shipped khipu-stop-hook, run the way a harness runs it (sh, stdin
    JSON), must capture natively: parse a Claude Code transcript, queue on
    PreCompact, and heartbeat as claude_code — under a throwaway home."""

    def test_stop_hook_queues_and_heartbeats_for_claude_code(self):
        with tempfile.TemporaryDirectory() as td:
            khome = Path(td) / "kh"
            tp = _write(Path(td) / ".claude" / "projects" / "p" / "s.jsonl", [
                {"type": "user", "message": {"role": "user", "content": "probe " + "x" * 300}},
                {"type": "assistant", "message": {"role": "assistant", "content": "ack " * 40}}])
            env = {k: v for k, v in os.environ.items() if k != "KHIPU_HARNESS"}
            env.update(KHIPU_CAPTURE_HOME=str(khome), KHIPU_CAPTURE_NO_DRAIN="1",
                       KHIPU_MEMORY_ROOT=str(Path(td) / "mem"))
            r = subprocess.run(["sh", str(STOP_HOOK)], input=json.dumps(
                {"hook_event_name": "PreCompact", "session_id": "it", "cwd": td, "transcript_path": str(tp)}),
                capture_output=True, text=True, timeout=120, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            jobs = list((khome / "queue").glob("*.json"))
            self.assertEqual(len(jobs), 1, r.stderr)
            beat = json.loads((khome / "dispatch" / "claude_code.json").read_text())
            self.assertTrue(beat["due"], beat)
            self.assertEqual(beat["harness"], "claude_code")


class CadenceIdlenessTest(unittest.TestCase):
    """A harness nobody is using must not go red just because leftover pending
    turns keep aging. pending_since only clears on a due capture, so before
    2026-08-18 an idle harness reddened purely on wall-clock: Cursor showed
    "5 turn(s) over 121 min — cadence not firing" after 17 h of not being
    opened. Red belongs on evidence of failure, never on idleness."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = mock.patch.dict(os.environ, {"KHIPU_AEGIS_HOME": self.tmp.name})
        self.home.start()

    def tearDown(self):
        self.home.stop()
        self.tmp.cleanup()

    def _beat(self, *, dispatch_age_s: int, pend: int, pending_age_s: int) -> None:
        now = time.time()

        def iso(age):
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - age))

        d = Path(self.tmp.name) / "dispatch"
        d.mkdir(parents=True, exist_ok=True)
        (d / "cursor.json").write_text(json.dumps({
            "harness": "cursor", "at": iso(dispatch_age_s), "dispatches": 4, "captures": 1,
            "pending_turns": pend, "pending_since": iso(pending_age_s),
            "last_captured_at": iso(pending_age_s),
        }), encoding="utf-8")

    def _reasons(self):
        return sc.liveness("cursor").get("reasons") or []

    def test_idle_harness_with_stale_pending_turns_is_not_red(self):
        # hook last ran 17 h ago: nobody is failing to decide anything
        self._beat(dispatch_age_s=17 * 3600, pend=5, pending_age_s=17 * 3600)
        self.assertEqual(self._reasons(), [])
        self.assertTrue(sc.liveness("cursor")["ok"])

    def test_an_active_hook_that_never_fires_is_still_red(self):
        # hook ran a minute ago and has been sitting on pending turns for hours
        self._beat(dispatch_age_s=60, pend=5, pending_age_s=6 * 3600)
        reasons = self._reasons()
        self.assertTrue(any("cadence not firing" in r for r in reasons), reasons)
        self.assertFalse(sc.liveness("cursor")["ok"])

    def test_an_active_hook_over_the_turn_cap_is_red(self):
        self._beat(dispatch_age_s=60, pend=sc.STUCK_TURNS, pending_age_s=120)
        self.assertTrue(any("cadence not firing" in r for r in self._reasons()))


class ConversationImageLandTest(unittest.TestCase):
    """Land PNG/JPEG from JSONL when conversation_memory.embed_media is on.
    Hook text parsing still drops attachment-only rows."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._env = mock.patch.dict(
            os.environ,
            {
                "KHIPU_CAPTURE_HOME": str(self.dir / "kh"),
                "KHIPU_DATA_DIR": str(self.dir / "data"),
            },
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    def _png_b64(self) -> str:
        import struct
        import zlib

        def chunk(tag: bytes, data: bytes) -> bytes:
            crc = zlib.crc32(tag + data) & 0xFFFFFFFF
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

        raw = b"\x00" + bytes([255, 220, 0])
        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
        png = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b"")
        )
        return base64.b64encode(png).decode("ascii")

    def test_text_parser_still_skips_attachment_only_rows(self):
        tp = _write(
            self.dir / "s.jsonl",
            [
                {"type": "user", "message": {"role": "user", "content": "hi there"}},
                {"type": "attachment", "attachment": {"foo": 1}},
            ],
        )
        msgs, _, users = sc.read_window(tp, 0)
        self.assertEqual(users, 1)
        self.assertEqual(msgs[0][1].strip(), "hi there")

    def test_land_skips_when_embed_media_off(self):
        from khipu import sources

        b64 = self._png_b64()
        tp = _write(
            self.dir / "img.jsonl",
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": b64,
                                },
                            }
                        ],
                    },
                }
            ],
        )
        self.assertFalse(sources.embed_media_enabled("conversation_memory"))
        stats = sc.land_transcript_images(tp)
        self.assertEqual(stats["landed"], 0)
        root = sources.conversation_media_root()
        self.assertFalse(any(root.glob("*.png")))

    def test_land_claude_image_block_when_opted_in(self):
        from khipu import sources

        sources.set_embed_media("conversation_memory", True)
        b64 = self._png_b64()
        tp = _write(
            self.dir / "img.jsonl",
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": b64,
                                },
                            }
                        ],
                    },
                }
            ],
        )
        stats = sc.land_transcript_images(tp)
        self.assertEqual(stats["landed"], 1)
        pngs = list(sources.conversation_media_root().glob("*.png"))
        self.assertEqual(len(pngs), 1)
        again = sc.land_transcript_images(tp)
        self.assertEqual(again["landed"], 0)
        self.assertGreaterEqual(again["skipped_existing"], 1)

    def test_land_rejects_absolute_png_outside_allowlist(self):
        from khipu import sources

        sources.set_embed_media("conversation_memory", True)
        png = base64.b64decode(self._png_b64())
        with tempfile.TemporaryDirectory() as outside:
            outsider = Path(outside) / "secret.png"
            outsider.write_bytes(png)
            tp = _write(
                self.dir / "outside.jsonl",
                [
                    {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "path": str(outsider.resolve()),
                                    },
                                }
                            ],
                        },
                    }
                ],
            )
            stats = sc.land_transcript_images(tp)
            self.assertEqual(stats["landed"], 0)
            self.assertFalse(any(sources.conversation_media_root().glob("*.png")))

    def test_land_absolute_png_under_jsonl_parent(self):
        from khipu import sources

        sources.set_embed_media("conversation_memory", True)
        png = base64.b64decode(self._png_b64())
        allowed = self.dir / "shot.png"
        allowed.write_bytes(png)
        tp = _write(
            self.dir / "local.jsonl",
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "path": str(allowed.resolve()),
                                },
                            }
                        ],
                    },
                }
            ],
        )
        stats = sc.land_transcript_images(tp)
        self.assertEqual(stats["landed"], 1)
        self.assertEqual(len(list(sources.conversation_media_root().glob("*.png"))), 1)

    def test_land_skips_webp(self):
        from khipu import sources

        sources.set_embed_media("conversation_memory", True)
        tp = _write(
            self.dir / "webp.jsonl",
            [
                {
                    "type": "attachment",
                    "attachment": {
                        "media_type": "image/webp",
                        "data": base64.b64encode(b"RIFF....WEBP").decode("ascii"),
                    },
                }
            ],
        )
        stats = sc.land_transcript_images(tp)
        self.assertEqual(stats["landed"], 0)


if __name__ == "__main__":
    unittest.main()
