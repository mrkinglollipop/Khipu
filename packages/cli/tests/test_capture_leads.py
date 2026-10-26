"""Capture leads (Grok triage 2026-10-26). Confirmed regressions only."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from khipu import session_capture as sc


def _write(path: Path, rows: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def _tail_digest(path: Path, offset: int, n: int = 4096) -> str:
    """Match ``session_capture._prefix_tail_digest`` (head + tail of prefix)."""
    dig = sc._prefix_tail_digest(path, offset, n=n)
    assert dig is not None
    return dig


def _response_user(text: str) -> dict:
    return {"type": "response_item", "payload": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": text}]}}


def _response_assistant(text: str) -> dict:
    return {"type": "response_item", "payload": {
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": text}]}}


def _event_user(text: str) -> dict:
    return {"type": "event_msg", "payload": {"type": "user_message", "message": text}}


def _event_assistant(text: str) -> dict:
    return {"type": "event_msg", "payload": {"type": "agent_message", "message": text}}


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
            st["transcript_prefix_tail"] = _tail_digest(p, offset)
            with p.open("w") as f:  # same inode, truncated and rewritten, larger than the offset
                for i in range(200, 330):
                    f.write(json.dumps(line(i)) + "\n")
            self.assertGreater(p.stat().st_size, offset)
            self.assertEqual(p.stat().st_ino, info.st_ino)
            sc._reset_transcript_offset(st, p)
            self.assertEqual(st["offset"], 0, "rewritten head (before old offset) will never be read")

    def test_unchanged_head_skips_full_prefix_reread(self):
        """P2: append-only growth must not re-parse the whole captured prefix."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.jsonl"
            line = lambda i: {"type": "user", "message": {"role": "user", "content": f"turn {i} " + "a" * 60}}
            _write(p, [line(i) for i in range(40)])
            offset = p.stat().st_size
            info = p.stat()
            prior: dict = {}
            sc.read_window(p, 0, stats=prior, end_offset=offset)
            st = {
                "transcript_path": str(p),
                "transcript_identity": [info.st_dev, info.st_ino],
                "offset": offset,
                "transcript_prefix": prior.get("prefix"),
                "transcript_prefix_tail": _tail_digest(p, offset),
            }
            with p.open("a") as f:
                f.write(json.dumps(line(99)) + "\n")
            with mock.patch.object(sc, "read_window", wraps=sc.read_window) as rw:
                sc._reset_transcript_offset(st, p)
            self.assertEqual(st["offset"], offset)
            self.assertEqual(rw.call_count, 0, "unchanged head must not re-parse the prefix")

    def test_due_advance_refreshes_prefix_tail_so_next_hook_skips(self):
        """P2: after a due capture advances the offset, the next append-only reset
        must not re-parse the whole captured prefix."""
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(
                os.environ, {"KHIPU_CAPTURE_HOME": str(Path(td) / "kh"), "HOME": td,
                             "KHIPU_KEYCHAIN": "0"}), \
                mock.patch.object(sc, "_claim_process_start", return_value="kernel:fake"):
            p = Path(td) / "t.jsonl"
            line = lambda i: {"type": "user", "message": {"role": "user",
                                                          "content": f"turn {i} " + "a" * 80}}
            _write(p, [line(i) for i in range(8)])
            env = json.dumps({"session_id": "adv-1", "hook_event_name": "SessionEnd",
                              "cwd": td, "transcript_path": str(p)})
            out = sc.hook_main(env, "claude_code")
            self.assertTrue(out.get("due"), out)
            st = sc.load_state("claude_code", "adv-1")
            offset = int(st["offset"])
            self.assertGreater(offset, 0)
            self.assertEqual(st.get("transcript_prefix_tail"),
                             sc._prefix_tail_digest(p, offset),
                             "due path must store a tail digest for the new offset")
            with p.open("a") as f:
                f.write(json.dumps(line(99)) + "\n")
            with mock.patch.object(sc, "read_window", wraps=sc.read_window) as rw:
                sc._reset_transcript_offset(st, p)
            prefix_rereads = [
                c for c in rw.call_args_list
                if (c.kwargs.get("end_offset") is not None
                    or (len(c.args) >= 2 and c.args[1] == 0 and "end_offset" in c.kwargs))
            ]
            self.assertEqual(prefix_rereads, [],
                             "stale prefix_tail after advance forces a full prefix re-read")


class CodexMixedRepeatTest(unittest.TestCase):
    """A later response_item with the same text as an earlier event_msg is kept."""

    def test_repeated_user_text_in_mixed_file_is_kept(self):
        with tempfile.TemporaryDirectory() as td:
            tp = _write(Path(td) / "r.jsonl", [
                _event_user("ship the capture fix today"),
                _event_assistant("done"),
                _response_user("ship the capture fix today"),
                _response_assistant("done again"),
            ])
            msgs, _, _ = sc.read_window(tp, 0)
            self.assertEqual([m for m in msgs if m[0] == "user"],
                             [("user", "ship the capture fix today")] * 2, msgs)

    def test_unpaired_response_beside_a_pair_is_kept(self):
        """P3: response-only turn next to a real event/response pair stays."""
        with tempfile.TemporaryDirectory() as td:
            tp = _write(Path(td) / "r.jsonl", [
                _event_user("paired"),
                _response_user("paired"),
                _response_assistant("ack"),
                _response_user("solo later"),
                _response_assistant("solo ack"),
            ])
            msgs, _, _ = sc.read_window(tp, 0)
            users = [m for m in msgs if m[0] == "user"]
            self.assertEqual(users, [("user", "paired"), ("user", "solo later")], msgs)

    def test_long_response_only_window_stays_linear(self):
        """P1: response_item-only Codex windows must not be quadratic."""
        with tempfile.TemporaryDirectory() as td:
            rows = []
            for i in range(2000):
                rows.append(_response_user(f"user turn {i} " + "x" * 40))
                rows.append(_response_assistant(f"assistant turn {i} " + "y" * 40))
            tp = _write(Path(td) / "long.jsonl", rows)
            t0 = time.perf_counter()
            msgs, _, _ = sc.read_window(tp, 0)
            elapsed = time.perf_counter() - t0
            self.assertEqual(len(msgs), 4000)
            self.assertLess(elapsed, 1.5, f"read_window took {elapsed:.2f}s on 4000 response_item lines")


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
