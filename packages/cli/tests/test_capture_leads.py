"""Capture leads (Grok triage 2026-10-26). Confirmed regressions only."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import session_capture as sc


def _write(path: Path, rows: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


class RewriteSameInodeTest(unittest.TestCase):
    """In-place rewrite to a larger file must reset the byte offset."""

    def test_in_place_rewrite_to_larger_size_resets_offset(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.jsonl"
            line = lambda i: {"type": "user", "message": {"role": "user", "content": f"turn {i} " + "a" * 60}}
            _write(p, [line(i) for i in range(60)])
            size = p.stat().st_size
            offset = size - 40
            while not p.read_bytes()[:offset].endswith(b"\n"):
                offset -= 1
            info = p.stat()
            st = {"transcript_path": str(p), "transcript_identity": [info.st_dev, info.st_ino], "offset": offset}
            prior: dict = {}
            sc.read_window(p, 0, stats=prior, end_offset=offset)
            st["transcript_prefix"] = prior.get("prefix")
            with p.open("w") as f:  # same inode, truncated and rewritten, larger than the offset
                for i in range(200, 330):
                    f.write(json.dumps(line(i)) + "\n")
            self.assertGreater(p.stat().st_size, offset)
            self.assertEqual(p.stat().st_ino, info.st_ino)
            sc._reset_transcript_offset(st, p)
            self.assertEqual(st["offset"], 0, "rewritten head (before old offset) will never be read")


class CodexMixedRepeatTest(unittest.TestCase):
    """A later response_item with the same text as an earlier event_msg is kept."""

    def test_repeated_user_text_in_mixed_file_is_kept(self):
        with tempfile.TemporaryDirectory() as td:
            tp = _write(Path(td) / "r.jsonl", [
                {"type": "event_msg", "payload": {"type": "user_message", "message": "ship the capture fix today"}},
                {"type": "event_msg", "payload": {"type": "agent_message", "message": "done"}},
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": "ship the capture fix today"}]}},
                {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "done again"}]}},
            ])
            msgs, _, _ = sc.read_window(tp, 0)
            self.assertEqual([m for m in msgs if m[0] == "user"],
                             [("user", "ship the capture fix today")] * 2, msgs)


class SubagentLineageTest(unittest.TestCase):
    """parent_session_id may be bare; parent episode is harness:id."""

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
            child = _write(Path(td) / "c.jsonl", [
                {"type": "user", "message": {"role": "user", "content": "inspect " + "x" * 220}}])
            sc.hook_main(json.dumps({"hookEventName": "SubagentStop", "session_id": "parent-1", "agent_id": "a1",
                                     "cwd": td, "agent_transcript_path": str(child)}), "claude_code")
            job = json.loads(sc.queued_jobs()[0].read_text())
            payload = {"session_id": f"claude_code:{job['session_id']}",
                       "parent_session_id": job["parent_session_id"]}
            capture._inherit_project(Cur(), payload)
            self.assertEqual(payload.get("project"), "khipu", payload)
