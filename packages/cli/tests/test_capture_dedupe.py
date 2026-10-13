"""Re-emitted messages (2026-10-13): Claude Code writes the retained conversation
back into the transcript after every compaction, with new uuids at new offsets,
and the capture read every copy again. A message whose exact rendered text was
already captured in the same session is dropped and is not a user turn. Temp
dirs and fakes only; no network.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from khipu import session_capture as sc


def _home(td):
    return mock.patch.dict(os.environ, {"KHIPU_CAPTURE_HOME": str(Path(td) / "kh")})


def _append(path: Path, rows: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write("".join(json.dumps(r) + "\n" for r in rows))
    return path


def _user(text: str) -> dict:
    return {"type": "user", "uuid": str(uuid.uuid4()), "message": {"role": "user", "content": text}}


def _assistant(text: str, mid: str) -> dict:
    return {"type": "assistant", "uuid": str(uuid.uuid4()),
            "message": {"id": mid, "role": "assistant", "content": [{"type": "text", "text": text}]}}


def _conversation() -> list[dict]:
    rows = []
    for i in range(3):
        rows.append(_user(f"question {i} " + "q" * 120))
        rows.append(_assistant(f"answer {i} " + "a" * 120, f"msg_{i}"))
    return rows


def _reemitted() -> list[dict]:
    # The same messages, new uuids (fresh dicts from the same text).
    return _conversation()


COMPACT = {"type": "user", "uuid": "c1", "isCompactSummary": True,
           "message": {"role": "user", "content": "This session is being continued. " + "s" * 80}}
NEW_TURN = "a brand new question after the compaction " + "n" * 80


def _window(tp: Path, offset: int, seen) -> tuple[list, int, int, dict]:
    stats: dict = {}
    msgs, off, users = sc.read_window(tp, offset, seen=seen, stats=stats)
    return msgs, off, users, stats


class ReadWindowDedupeTest(unittest.TestCase):
    def test_reemitted_history_is_dropped_and_only_the_new_turn_remains(self):
        with tempfile.TemporaryDirectory() as td:
            tp = _append(Path(td) / "s.jsonl", _conversation())
            msgs, off, users, stats = _window(tp, 0, ())
            self.assertEqual(users, 3)
            self.assertEqual(len(msgs), 6)
            self.assertEqual(stats["deduped_msgs"], 0)
            self.assertEqual(len(stats["fresh"]), 6)
            st: dict = {}
            sc.remember_seen(st, stats["fresh"])

            _append(tp, [COMPACT] + _reemitted() + [_user(NEW_TURN)])
            msgs2, _, users2, stats2 = _window(tp, off, st["seen_text"])
            self.assertEqual(stats2["deduped_msgs"], 6)
            self.assertEqual(stats2["deduped_chars"], sum(len(t) for _, t in msgs))
            self.assertEqual(users2, 1)
            # The compaction summary is new text; with every copy between them
            # dropped it coalesces with the new turn into one user message.
            self.assertEqual(len(msgs2), 1, msgs2)
            role, text = msgs2[0]
            self.assertEqual(role, "user")
            self.assertIn(NEW_TURN, text)
            self.assertNotIn("question 0", text)
            self.assertNotIn("answer", text)

    def test_repeat_inside_one_window_is_kept_once(self):
        with tempfile.TemporaryDirectory() as td:
            tp = _append(Path(td) / "s.jsonl", _conversation() + _reemitted())
            msgs, _, users, stats = _window(tp, 0, ())
            self.assertEqual((len(msgs), users), (6, 3))
            self.assertEqual(stats["deduped_msgs"], 6)
            self.assertEqual(len(stats["fresh"]), 6)

    def test_without_seen_nothing_is_deduplicated(self):
        with tempfile.TemporaryDirectory() as td:
            tp = _append(Path(td) / "s.jsonl", _conversation() + _reemitted())
            msgs, _, users = sc.read_window(tp, 0)
            self.assertEqual((len(msgs), users), (12, 6))

    def test_tool_markers_are_not_deduplicated(self):
        tool = {"type": "assistant", "message": {"id": "t", "role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}}
        with tempfile.TemporaryDirectory() as td:
            tp = _append(Path(td) / "s.jsonl", [_user("go " + "g" * 80), tool, tool])
            msgs, _, _, stats = _window(tp, 0, ())
            self.assertEqual([r for r, _ in msgs].count("tool"), 2, msgs)
            self.assertEqual(stats["deduped_msgs"], 0)

    def test_reemitted_task_notification_is_dropped_not_rerendered(self):
        note = ("<task-notification>\n<task-id>t1</task-id>\n<summary>build finished green</summary>\n"
                "<result>" + "r" * 2000 + "</result>\n</task-notification>")
        with tempfile.TemporaryDirectory() as td:
            tp = _append(Path(td) / "s.jsonl", [_user("start the build " + "b" * 80), _user(note),
                                                _assistant("ok " + "o" * 80, "m1")])
            msgs, off, users, stats = _window(tp, 0, ())
            self.assertEqual(users, 1)
            self.assertIn("[notification: task-notification] build finished green", msgs[0][1])
            _append(tp, [_user(note), _user(NEW_TURN)])
            msgs2, _, users2, stats2 = _window(tp, off, stats["fresh"])
            self.assertEqual(stats2["deduped_msgs"], 1)
            self.assertEqual(users2, 1)
            self.assertEqual(msgs2, [("user", NEW_TURN)])

    def test_codex_rollout_is_unaffected(self):
        rows = [
            {"type": "session_meta", "payload": {"id": "x"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "audit the UI"}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "shell"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "13 findings"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "fix the first one"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "fixed"}},
        ]
        with tempfile.TemporaryDirectory() as td:
            tp = _append(Path(td) / "r.jsonl", rows)
            plain = sc.read_window(tp, 0)
            msgs, off, users, stats = _window(tp, 0, ())
            self.assertEqual((msgs, off, users), plain)
            self.assertEqual(stats["deduped_msgs"], 0)

    def test_acp_stream_chunks_are_not_fingerprinted(self):
        def chunk(kind, text):
            return {"jsonrpc": "2.0", "method": "session/update", "params": {"update": {
                "sessionUpdate": kind, "content": {"type": "text", "text": text}}}}
        rows = [chunk("user_message_chunk", "hello there"),
                chunk("agent_message_chunk", "the"), chunk("agent_message_chunk", " cat"),
                chunk("agent_message_chunk", " the"), chunk("agent_message_chunk", " end")]
        with tempfile.TemporaryDirectory() as td:
            tp = _append(Path(td) / "u.jsonl", rows)
            plain = sc.read_window(tp, 0)
            msgs, _, _, stats = _window(tp, 0, ())
            self.assertEqual(msgs, plain[0])
            self.assertEqual(stats["deduped_msgs"], 0)


class SeenStateTest(unittest.TestCase):
    def test_seen_text_is_bounded_newest_last_and_round_trips(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            st: dict = {"offset": 0}
            sc.remember_seen(st, [f"{i:016x}" for i in range(sc.SEEN_TEXT_MAX)])
            sc.remember_seen(st, ["newest0000000000"])
            self.assertEqual(len(st["seen_text"]), sc.SEEN_TEXT_MAX)
            self.assertEqual(st["seen_text"][0], f"{1:016x}")
            self.assertEqual(st["seen_text"][-1], "newest0000000000")
            sc.save_state("claude_code", "s1", st)
            self.assertEqual(sc.load_state("claude_code", "s1")["seen_text"], st["seen_text"])


class HookDedupeTest(unittest.TestCase):
    def _env(self, td, tp):
        return json.dumps({"hook_event_name": "PreCompact", "session_id": "s1", "cwd": td,
                           "transcript_path": str(tp)})

    def test_log_line_and_heartbeat_report_only_when_something_was_dropped(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            tp = _append(Path(td) / ".claude" / "projects" / "p" / "s.jsonl", _conversation())
            with mock.patch.object(sc, "save_state", wraps=sc.save_state) as save:
                out = sc.hook_main(self._env(td, tp))
            self.assertTrue(out["due"], out)
            self.assertNotIn("deduped_msgs", out)
            # First sight writes the initial state; the run itself writes once.
            self.assertEqual(save.call_count, 2)
            beat = sc._read_beat("claude_code")
            self.assertNotIn("deduped_msgs", beat)
            self.assertNotIn("deduped=", sc.log_path().read_text(encoding="utf-8"))
            self.assertEqual(len(sc.load_state("claude_code", "s1")["seen_text"]), 6)

            _append(tp, [COMPACT] + _reemitted() + [_user(NEW_TURN)])
            with mock.patch.object(sc, "save_state", wraps=sc.save_state) as save:
                out = sc.hook_main(self._env(td, tp))
            self.assertEqual(save.call_count, 1)
            self.assertTrue(out["due"], out)
            self.assertEqual(out["new_turns"], 1)
            self.assertEqual(out["deduped_msgs"], 6)
            beat = sc._read_beat("claude_code")
            self.assertEqual(beat["deduped_msgs"], 6)
            self.assertGreater(beat["deduped_chars"], 0)
            self.assertIn(f"deduped=6 msgs/{beat['deduped_chars']} chars",
                          sc.log_path().read_text(encoding="utf-8"))
            jobs = [json.loads(p.read_text()) for p in sc.queued_jobs()]
            second = [j for j in jobs if NEW_TURN in j["transcript"]]
            self.assertEqual(len(second), 1)
            self.assertNotIn("question 0", second[0]["transcript"])

            # A run with nothing dropped clears the per-run fields.
            _append(tp, [_user("one more fresh question " + "f" * 120)])
            out = sc.hook_main(self._env(td, tp))
            self.assertNotIn("deduped_msgs", out)
            self.assertNotIn("deduped_msgs", sc._read_beat("claude_code"))

    def test_not_due_run_does_not_remember_its_window(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            tp = _append(Path(td) / ".claude" / "projects" / "p" / "s.jsonl", _conversation()[:2])
            env = json.loads(self._env(td, tp))
            env["hook_event_name"] = "Stop"
            self.assertFalse(sc.hook_main(json.dumps(env))["due"])
            self.assertNotIn("seen_text", sc.load_state("claude_code", "s1"))
            # The same unread window is still whole on the next run.
            env["hook_event_name"] = "SessionEnd"
            out = sc.hook_main(json.dumps(env))
            self.assertTrue(out["due"], out)
            self.assertEqual(out["new_turns"], 1)
            self.assertNotIn("deduped_msgs", out)


    def test_not_due_run_steps_past_leading_copies_but_keeps_the_fresh_tail(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            tp = _append(Path(td) / ".claude" / "projects" / "p" / "s.jsonl", _conversation())
            self.assertTrue(sc.hook_main(self._env(td, tp))["due"])
            captured_end = sc.load_state("claude_code", "s1")["offset"]
            # Compaction re-emits everything, then one fresh (not yet due) turn.
            _append(tp, [COMPACT] + _reemitted())
            copies_end = tp.stat().st_size
            _append(tp, [_user(NEW_TURN)])
            env = json.loads(self._env(td, tp))
            env["hook_event_name"] = "Stop"
            out = sc.hook_main(json.dumps(env))
            self.assertFalse(out["due"], out)
            self.assertEqual(out["deduped_msgs"], 6)
            st = sc.load_state("claude_code", "s1")
            self.assertGreater(st["offset"], captured_end)
            self.assertLessEqual(st["offset"], copies_end)
            self.assertEqual(len(st["seen_text"]), 6)  # nothing new remembered
            env["hook_event_name"] = "SessionEnd"
            out = sc.hook_main(json.dumps(env))
            self.assertTrue(out["due"], out)
            self.assertEqual(out["new_turns"], 1)
            self.assertNotIn("deduped_msgs", out)  # the copies were not re-read
            job = [json.loads(p.read_text()) for p in sc.queued_jobs()
                   if NEW_TURN in json.loads(p.read_text())["transcript"]][0]
            self.assertNotIn("question 0", job["transcript"])
            # The compaction summary ahead of the copies was carried, not lost.
            self.assertIn("This session is being continued", job["transcript"])


class ShortMessagesTest(unittest.TestCase):
    def test_short_repeats_are_kept_and_count_as_turns(self):
        with tempfile.TemporaryDirectory() as td:
            long_answer = _assistant("a long answer " + "a" * 80, "m")
            tp = _append(Path(td) / "s.jsonl", [_user("ok"), long_answer, _user("ok"), long_answer])
            msgs, _, users, stats = _window(tp, 0, ())
            self.assertEqual(users, 2)
            self.assertEqual([r for r, _ in msgs], ["user", "assistant", "user"])
            self.assertEqual(stats["deduped_msgs"], 1)
            self.assertEqual(stats["skip_to"], 0)


if __name__ == "__main__":
    unittest.main()
