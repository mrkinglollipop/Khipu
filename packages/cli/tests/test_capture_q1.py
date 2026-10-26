"""Capture q1 leads (grokq-triage-1 item 5, 2026-10-26). Confirmed regressions only."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from khipu import session_capture as sc


def _write(path: Path, rows: list, *, final_newline: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(r) for r in rows)
    path.write_text(body + ("\n" if final_newline else ""), encoding="utf-8")
    return path


def _user(text, **extra):
    return {"type": "user", "message": {"role": "user", "content": text}, **extra}


def _assistant(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


class HarnessPrecedenceTest(unittest.TestCase):
    """CODEX_HOME must not override a Claude transcript home."""

    def test_claude_transcript_under_claude_config_dir_stays_claude_with_codex_home_set(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / ".claude-t3-second"
            tp = home / "projects" / "-x" / "s.jsonl"
            tp.parent.mkdir(parents=True)
            tp.write_text("")
            osenv = {"CLAUDE_CONFIG_DIR": str(home), "HOME": td, "CODEX_HOME": str(Path(td) / ".codex")}
            self.assertEqual(sc.infer_harness({"transcript_path": str(tp)}, osenv), "claude_code")

    def test_default_claude_transcript_stays_claude_with_codex_home_set(self):
        osenv = {"CODEX_HOME": "/somewhere/.codex"}
        tp = "/Users/x/.claude/projects/-x/s.jsonl"
        self.assertEqual(sc.infer_harness({"transcript_path": tp}, osenv), "claude_code")


class CompactSummaryFingerprintTest(unittest.TestCase):
    """A carried compaction summary is fingerprinted once flushed into a capture."""

    def test_summary_is_fingerprinted_once_it_has_been_captured(self):
        summary = "This session is being continued from a previous conversation: the plan was chosen."
        old = "An earlier long question that was already captured in this session before compaction"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tp = root / "t.jsonl"
            _write(tp, [_user(summary, isCompactSummary=True), _user(old)])
            env = {"KHIPU_CAPTURE_HOME": str(root / "kh"), "KHIPU_DATA_DIR": str(root / "data"),
                   "KHIPU_OUTBOX": str(root / "out")}
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(sc, "_claim_process_start", return_value="kernel:fake"):
                sc.save_state("claude_code", "s1", {
                    "offset": 0, "last_ts": time.time(), "window_started_ts": time.time(), "captures": 1,
                    "transcript_path": str(tp), "seen_text": [sc.text_fingerprint("user", old)]})
                ev = json.dumps({"hookEventName": "Stop", "session_id": "s1",
                                 "transcript_path": str(tp), "cwd": td})
                first = sc.hook_main(ev, "claude_code")
                self.assertFalse(first["due"], first)
                with tp.open("a") as f:
                    for i in range(6):
                        f.write(json.dumps(_user(f"brand new real question number {i} " + "q" * 60)) + "\n")
                        f.write(json.dumps(_assistant(f"an answer to question number {i} " + "a" * 60)) + "\n")
                second = sc.hook_main(ev, "claude_code")
                self.assertTrue(second["due"], second)
                st = sc.load_state("claude_code", "s1")
            self.assertIn(sc.text_fingerprint("user", summary), st.get("seen_text") or [],
                          "summary was queued via carry but never remembered")


class ShortCopiesAfterToolLineTest(unittest.TestCase):
    """Re-emitted short user copies after a text-free tool line are not new turns."""

    def test_replayed_short_user_lines_are_not_new_turns(self):
        long_u = "A long already captured user question that is clearly over forty characters"
        tool = {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/x"}}]}}
        tail = []
        for i in range(5):
            tail += [_user(f"short copy {i} xxxxxxxxxxxxx"), _assistant(f"short reply {i} yyyyyyyyyy")]
        with tempfile.TemporaryDirectory() as td:
            tp = Path(td) / "t.jsonl"
            _write(tp, [_user(long_u), tool] + tail)
            _, offset, _ = sc.read_window(tp, 0)
            seen = [sc.text_fingerprint("user", long_u)]
            _write(tp, [_user(long_u), tool] + tail + [_user(long_u), tool] + tail)
            _, _, turns = sc.read_window(tp, offset, seen=seen, stats={})
            self.assertEqual(turns, 0, f"the re-emitted copy counted {turns} new user turns")


class SubagentLineageTest(unittest.TestCase):
    """parent_session_id may be bare; the parent episode is harness:id."""

    def test_inherit_project_can_find_the_prefixed_parent_episode(self):
        from khipu import capture

        class Cur:
            rows = [{"session_id": "claude_code:parent-1", "parent_session_id": None, "project": "khipu"}]
            hit = None

            def execute(self, sql, params=()):
                want = set(params)
                self.hit = next(((r["project"], None) for r in self.rows
                                 if r["session_id"] in want or r["parent_session_id"] in want), None)

            def fetchone(self):
                return self.hit

        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"KHIPU_CAPTURE_HOME": str(Path(td) / "kh")}), \
                mock.patch.object(sc, "_claim_process_start", return_value="kernel:fake"):
            child = _write(Path(td) / "c.jsonl", [_user("inspect " + "x" * 220)])
            sc.hook_main(json.dumps({"hookEventName": "SubagentStop", "session_id": "parent-1", "agent_id": "a1",
                                     "cwd": td, "agent_transcript_path": str(child)}), "claude_code")
            job = json.loads(sc.queued_jobs()[0].read_text())
            payload = {"session_id": f"claude_code:{job['session_id']}",
                       "parent_session_id": job["parent_session_id"]}
            capture._inherit_project(Cur(), payload)
            self.assertEqual(payload.get("project"), "khipu", payload)


if __name__ == "__main__":
    unittest.main()
