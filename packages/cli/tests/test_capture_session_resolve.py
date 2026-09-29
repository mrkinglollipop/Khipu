"""Which session a capture-now request lands on. The caller (MCP server, CLI)
has no session id of its own, so it is matched against what each capture hook
recorded about itself: process ancestry and working directory. The old answer,
"the newest file in the state directory", picked the per-prompt recall dedup
file as a session and, with several sessions live, whichever was last active.

Everything runs under a temp KHIPU_CAPTURE_HOME with invented sessions; no hook
process, database or model is involved.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import mcp_server as ms
from khipu import session_capture as sc


def _home(td):
    return mock.patch.dict(os.environ, {"KHIPU_CAPTURE_HOME": str(Path(td) / "kh")})


def _state(harness: str, sid: str, *, age_s: float = 0.0, **fields) -> Path:
    sc.save_state(harness, sid, {"offset": 0, "last_ts": 0.0, "captures": 0, **fields})
    p = sc._state_file(harness, sid)
    if age_s:
        t = p.stat().st_mtime - age_s
        os.utime(p, (t, t))
    return p


def _transcript(td: str, name: str) -> Path:
    p = Path(td) / ".claude" / "projects" / "p" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"type": "user", "message": {"role": "user", "content": "one question"}},
            {"type": "assistant", "message": {"role": "assistant", "content": "one answer"}}]
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return p


class ResolverTest(unittest.TestCase):
    def test_the_recall_dedup_file_is_never_a_session_even_when_newest(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            _state("claude_code", "aaaaaaaa1111", age_s=60)
            (sc.state_dir() / "recall--bbbbbbbb2222.json").write_text('{"batches": []}')
            self.assertEqual(sc.resolve_session_ref(host_pids=[], cwd=td),
                             ("claude_code", "aaaaaaaa1111", "only_active"))

    def test_subagent_tracking_files_are_not_sessions(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            _state("claude_code", "aaaaaaaa1111", age_s=60)
            _state("claude_code", sc._safe("aaaaaaaa1111:agent:x"), subagent=True)
            self.assertEqual(sc.resolve_session_ref(host_pids=[], cwd=td)[1], "aaaaaaaa1111")

    def test_two_live_sessions_resolve_by_process_ancestry(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            _state("claude_code", "aaaaaaaa1111", host_pids=[10, 99], cwd=td)
            _state("claude_code", "bbbbbbbb2222", host_pids=[20, 99], cwd=td, age_s=120)
            # 99 is shared by every session and says nothing; 20 is B's alone.
            self.assertEqual(sc.resolve_session_ref(host_pids=[5, 99, 20], cwd=td),
                             ("claude_code", "bbbbbbbb2222", "process"))

    def test_the_nearest_ancestor_that_tells_sessions_apart_wins(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            _state("claude_code", "aaaaaaaa1111", host_pids=[10, 30, 99])
            _state("cursor", "bbbbbbbb2222", host_pids=[20, 30, 99])
            self.assertEqual(sc.resolve_session_ref(host_pids=[10, 30, 99], cwd=td)[:2],
                             ("claude_code", "aaaaaaaa1111"))

    def test_identical_ancestry_falls_to_the_working_directory(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            here, there = Path(td) / "here", Path(td) / "there"
            here.mkdir()
            there.mkdir()
            _state("claude_code", "aaaaaaaa1111", host_pids=[99], cwd=str(there))
            _state("claude_code", "bbbbbbbb2222", host_pids=[99], cwd=str(here), age_s=120)
            self.assertEqual(sc.resolve_session_ref(host_pids=[99], cwd=str(here)),
                             ("claude_code", "bbbbbbbb2222", "cwd"))

    def test_a_state_file_from_before_the_change_resolves_through_transcript_path(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            mine, other = Path(td) / "mine", Path(td) / "other"
            mine.mkdir()
            other.mkdir()

            def tpath(d: Path, sid: str) -> str:
                enc = re.sub(r"[^A-Za-z0-9]", "-", str(d.resolve()))
                return str(Path(td) / ".claude" / "projects" / enc / f"{sid}.jsonl")

            _state("claude_code", "aaaaaaaa1111", transcript_path=tpath(other, "aaaaaaaa1111"))
            _state("claude_code", "bbbbbbbb2222", age_s=120,
                   transcript_path=tpath(mine, "bbbbbbbb2222"))
            self.assertEqual(sc.resolve_session_ref(host_pids=[], cwd=str(mine)),
                             ("claude_code", "bbbbbbbb2222", "cwd"))

    def test_two_recent_sessions_and_no_match_refuse_and_name_the_candidates(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            _state("claude_code", "aaaaaaaa1111ffff", cwd=str(Path(td) / "x"))
            _state("cursor", "bbbbbbbb2222ffff", cwd=str(Path(td) / "y"))
            with self.assertRaises(ValueError) as cm:
                sc.resolve_session_ref(host_pids=[], cwd=td)
            msg = str(cm.exception)
            self.assertIn("claude_code:aaaaaaaa", msg)
            self.assertIn("cursor:bbbbbbbb", msg)
            self.assertNotIn("ffff", msg)
            self.assertIn("session_id='<harness>:<id>'", msg)

    def test_a_lone_session_untouched_for_hours_is_not_guessed(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            _state("claude_code", "aaaaaaaa1111", age_s=3 * 3600)
            with self.assertRaises(ValueError):
                sc.resolve_session_ref(host_pids=[], cwd=td)

    def test_no_state_at_all_refuses(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            with self.assertRaises(ValueError):
                sc.resolve_session_ref(host_pids=[], cwd=td)

    def test_an_explicit_harness_must_be_a_real_capture_harness(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            with self.assertRaises(ValueError):
                sc.resolve_session_ref("recall", "bbbbbbbb2222")
            self.assertEqual(sc.resolve_session_ref("codex", "s9"), ("codex", "s9", "explicit"))


class AncestorLookupTest(unittest.TestCase):
    def test_a_failing_lookup_returns_nothing_and_never_raises(self):
        for exc in (OSError("no ps"), __import__("subprocess").TimeoutExpired("ps", 1)):
            with mock.patch.object(sc.subprocess, "run", side_effect=exc):
                self.assertEqual(sc.ancestor_pids(), [])

    def test_the_chain_is_nearest_first_and_stops_before_pid_1(self):
        table = "  50  40\n  40  30\n  30   1\n   1   0\n"
        with mock.patch.object(sc.subprocess, "run",
                               return_value=mock.Mock(stdout=table)):
            self.assertEqual(sc.ancestor_pids(50), [40, 30])

    def test_the_hook_still_works_and_records_an_empty_list_when_the_lookup_fails(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            tp = _transcript(td, "s.jsonl")
            env = {"hook_event_name": "Stop", "session_id": "hk1", "cwd": td, "transcript_path": str(tp)}
            with mock.patch.object(sc.subprocess, "run", side_effect=OSError("no ps")):
                out = sc.hook_main(json.dumps(env))
            self.assertNotIn("error", out, out)
            st = sc.load_state("claude_code", "hk1")
            self.assertEqual(st["host_pids"], [])
            self.assertEqual(st["cwd"], td)

    def test_the_hook_records_its_directory_and_ancestry(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            tp = _transcript(td, "s.jsonl")
            env = {"hook_event_name": "Stop", "session_id": "hk2", "cwd": td, "transcript_path": str(tp)}
            with mock.patch.object(sc, "ancestor_pids", return_value=[7, 8]):
                sc.hook_main(json.dumps(env))
            st = sc.load_state("claude_code", "hk2")
            self.assertEqual((st["host_pids"], st["cwd"]), ([7, 8], td))


class CaptureNowEndToEndTest(unittest.TestCase):
    def test_the_flag_the_tool_writes_is_the_one_the_stop_path_consumes(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            tp_a, tp_b = _transcript(td, "a.jsonl"), _transcript(td, "b.jsonl")
            envs = {sid: {"hook_event_name": "Stop", "session_id": sid, "cwd": td,
                          "transcript_path": str(tp)}
                    for sid, tp in (("sessa", tp_a), ("sessb", tp_b))}
            for sid, pids in (("sessa", [10, 99]), ("sessb", [20, 99])):
                with mock.patch.object(sc, "ancestor_pids", return_value=pids):
                    sc.hook_main(json.dumps(envs[sid]))
            # The caller sits under session B's host process, and the recall
            # dedup file is the newest thing in the directory.
            (sc.state_dir() / "recall--sessb.json").write_text('{"batches": []}')
            with mock.patch.object(ms, "_stdio_hook_owns_capture", return_value=True), \
                    mock.patch.object(sc, "ancestor_pids", return_value=[20, 99]):
                res = ms._tool_capture({"summary": "remember the plan"})
            self.assertEqual(res["resolved_by"], "process")
            self.assertEqual((res["harness"], res["session_id"], res["queued"]),
                             ("claude_code", "sessb", True))
            with mock.patch.object(sc, "ancestor_pids", return_value=[10, 99]):
                out_a = sc.hook_main(json.dumps(envs["sessa"]))
            self.assertFalse(out_a["due"], out_a)
            with mock.patch.object(sc, "ancestor_pids", return_value=[20, 99]):
                out_b = sc.hook_main(json.dumps(envs["sessb"]))
            self.assertTrue(out_b["due"], out_b)
            self.assertEqual(out_b["reason"], "requested")
            jobs = [json.loads(p.read_text()) for p in sc.queued_jobs()]
            self.assertEqual([(j["session_id"], j["capture_note"]) for j in jobs],
                             [("sessb", "remember the plan")])

    def test_the_tool_refuses_an_unknown_harness_and_an_ambiguous_caller(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            _state("claude_code", "aaaaaaaa1111", cwd=str(Path(td) / "x"))
            _state("cursor", "bbbbbbbb2222", cwd=str(Path(td) / "y"))
            with mock.patch.object(ms, "_stdio_hook_owns_capture", return_value=True), \
                    mock.patch.object(sc, "ancestor_pids", return_value=[]):
                with self.assertRaises(ValueError):
                    ms._tool_capture({"summary": "x", "session_id": "recall:bbbbbbbb2222"})
                with self.assertRaises(ValueError):
                    ms._tool_capture({"summary": "x"})
                ok = ms._tool_capture({"summary": "x", "session_id": "cursor:bbbbbbbb2222"})
            self.assertEqual(ok["resolved_by"], "explicit")
            self.assertEqual(list(sc.state_dir().glob("recall--*.capture-now.json")), [])


if __name__ == "__main__":
    unittest.main()
