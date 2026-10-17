"""Which session a capture-now request lands on. The caller (MCP server, CLI)
inherits Claude's session id or is matched against what each capture hook
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
from khipu import owner_turns as ot
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


def _owner_turn(sid: str, pid: int, start: float | None, *, harness: str = "claude_code",
                ts: float | None = None) -> None:
    path = ot.session_file(sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"session": sid, "harness": harness, "ts": ts,
                             "issuer": {"pid": pid, "start": start, "exe": "claude"}}) + "\n")


class ResolverTest(unittest.TestCase):
    def test_claude_session_env_beats_a_later_prompt_before_any_process_lookup(self):
        with tempfile.TemporaryDirectory() as td, _home(td), \
                mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "mine"}):
            now = sc.time.time()
            _state("claude_code", "mine", cwd=td, age_s=3 * 86400)
            _state("claude_code", "other", cwd=td)
            _owner_turn("mine", 20, 100.25, ts=now - 20 * 60)
            _owner_turn("other", 30, 101.5, ts=now)
            with mock.patch.object(sc, "ancestor_pids") as ancestry, \
                    mock.patch.object(ot, "_proc_info") as proc_info:
                self.assertEqual(sc.resolve_session_ref(cwd=td),
                                 ("claude_code", "mine", "session_env"))
                ancestry.assert_not_called()
                proc_info.assert_not_called()

    def test_claude_session_env_works_without_owner_turn_records(self):
        with tempfile.TemporaryDirectory() as td, _home(td), \
                mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "mine"}):
            _state("claude_code", "mine", cwd=td)
            _state("claude_code", "other", cwd=td)
            self.assertEqual(sc.resolve_session_ref(host_pids=[], cwd=td),
                             ("claude_code", "mine", "session_env"))

    def test_unknown_claude_session_env_falls_through_to_issuer_matching(self):
        with tempfile.TemporaryDirectory() as td, _home(td), \
                mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "unknown"}):
            now = sc.time.time()
            _state("claude_code", "mine", cwd=td)
            _state("claude_code", "other", cwd=td)
            _owner_turn("mine", 20, 100.25, ts=now - 20 * 60)
            _owner_turn("other", 30, 101.5, ts=now)
            with mock.patch.object(ot, "_proc_info", return_value=(99, "/bin/claude", 100.25)):
                self.assertEqual(sc.resolve_session_ref(host_pids=[20], cwd=td),
                                 ("claude_code", "mine", "process"))

    def test_claude_session_env_cannot_select_a_non_claude_candidate(self):
        with tempfile.TemporaryDirectory() as td, _home(td), \
                mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "codex-thread"}):
            now = sc.time.time()
            _state("codex", "codex-thread", cwd=td)
            _state("claude_code", "mine", cwd=td)
            _owner_turn("codex-thread", 30, 101.5, harness="codex", ts=now)
            _owner_turn("mine", 20, 100.25, ts=now - 1)
            with mock.patch.object(ot, "_proc_info", return_value=(99, "/bin/claude", 100.25)):
                self.assertEqual(sc.resolve_session_ref(host_pids=[20], cwd=td),
                                 ("claude_code", "mine", "process"))

    def test_claude_env_successor_requires_the_full_latest_issuer_identity(self):
        for pid, start in ((30, 100.25), (20, 101.5), (20, None), (None, 100.25)):
            with self.subTest(pid=pid, start=start), tempfile.TemporaryDirectory() as td, _home(td), \
                    mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "mine"}):
                now = sc.time.time()
                _state("claude_code", "mine", cwd=td)
                _state("claude_code", "other", cwd=td)
                _owner_turn("mine", 20, 100.25, ts=now - 2)
                _owner_turn("mine", pid, start, ts=now - 1)
                _owner_turn("other", 20, 100.25, ts=now)
                self.assertEqual(sc.resolve_session_ref(host_pids=[], cwd=td),
                                 ("claude_code", "mine", "session_env"))

    def test_claude_env_successor_cannot_be_a_different_harness(self):
        with tempfile.TemporaryDirectory() as td, _home(td), \
                mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "mine"}):
            now = sc.time.time()
            _state("claude_code", "mine", cwd=td)
            _state("codex", "other", cwd=td)
            _owner_turn("mine", 20, 100.25, ts=now - 1)
            _owner_turn("other", 20, 100.25, harness="codex", ts=now)
            self.assertEqual(sc.resolve_session_ref(host_pids=[], cwd=td),
                             ("claude_code", "mine", "session_env"))

    def test_tied_successors_of_a_stale_claude_env_refuse(self):
        with tempfile.TemporaryDirectory() as td, _home(td), \
                mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "before-clear"}):
            now = sc.time.time()
            for sid, ts in (("before-clear", now - 1), ("after-a", now), ("after-b", now)):
                _state("claude_code", sid, cwd=td)
                _owner_turn(sid, 20, 100.25, ts=ts)
            with self.assertRaisesRegex(ValueError, "cannot tell which session is yours"):
                sc.resolve_session_ref(host_pids=[], cwd=td)

    def test_explicit_session_still_overrides_the_claude_env(self):
        with tempfile.TemporaryDirectory() as td, _home(td), \
                mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "mine"}):
            _state("claude_code", "mine", cwd=td)
            self.assertEqual(sc.resolve_session_ref("codex", "chosen", host_pids=[], cwd=td),
                             ("codex", "chosen", "explicit"))

    def test_owner_turn_issuer_resolves_a_session_with_stale_capture_state(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            _state("claude_code", "older", cwd=td, host_pids=[20, 99], age_s=3 * 86400)
            _state("claude_code", "newer", cwd=td, host_pids=[10, 99], age_s=2 * 86400)
            _owner_turn("older", 20, 100.25)
            _owner_turn("newer", 10, 101.5)
            with mock.patch.object(ot, "_proc_info", return_value=(99, "/bin/claude", 100.25)):
                self.assertEqual(sc.resolve_session_ref(host_pids=[20, 99], cwd=td),
                                 ("claude_code", "older", "process"))

    def test_shared_cwd_without_prompt_records_resolves_when_only_one_is_recent(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            _state("claude_code", "older", cwd=td, age_s=3 * 86400)
            _state("claude_code", "newer", cwd=td)
            self.assertEqual(sc.resolve_session_ref(host_pids=[], cwd=td),
                             ("claude_code", "newer", "cwd"))

    def test_shared_codex_issuer_refuses_two_recent_prompts_in_callers_folder(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            now = sc.time.time()
            for sid, folder, ts in (("old", td, now - 20 * 60), ("new", td, now),
                                    ("elsewhere", str(Path(td) / "other"), now + 1)):
                _state("codex", sid, cwd=folder, age_s=3 * 86400)
                _owner_turn(sid, 20, 100.25, harness="codex", ts=ts)
            with mock.patch.object(ot, "_proc_info", return_value=(99, "/bin/codex", 100.25)):
                with self.assertRaises(ValueError) as cm:
                    sc.resolve_session_ref(host_pids=[20], cwd=td)
                self.assertIn("codex:old", str(cm.exception))
                self.assertIn("codex:new", str(cm.exception))
                self.assertIn("session_id='<harness>:<id>'", str(cm.exception))

    def test_shared_codex_issuer_uses_latest_prompt_when_other_is_idle(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            now = sc.time.time()
            for sid, folder, ts in (("old", td, now - 2 * 3600), ("new", td, now),
                                    ("elsewhere", str(Path(td) / "other"), now + 1)):
                _state("codex", sid, cwd=folder, age_s=3 * 86400)
                _owner_turn(sid, 20, 100.25, harness="codex", ts=ts)
            with mock.patch.object(ot, "_proc_info", return_value=(99, "/bin/codex", 100.25)):
                self.assertEqual(sc.resolve_session_ref(host_pids=[20], cwd=td),
                                 ("codex", "new", "process"))

    def test_clear_reuses_process_but_latest_session_prompt_wins(self):
        with tempfile.TemporaryDirectory() as td, _home(td), \
                mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "before-clear"}):
            now = sc.time.time()
            _state("claude_code", "before-clear", cwd=td)
            _state("claude_code", "after-clear", cwd=td, age_s=3 * 86400)
            _owner_turn("before-clear", 20, 100.25, ts=now - 1)
            _owner_turn("after-clear", 20, 100.25, ts=now)
            with mock.patch.object(ot, "_proc_info", return_value=(99, "/bin/claude", 100.25)):
                self.assertEqual(sc.resolve_session_ref(host_pids=[20], cwd=td),
                                 ("claude_code", "after-clear", "session_env"))

    def test_shared_issuer_exact_prompt_tie_refuses_despite_different_state_times(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            now = sc.time.time()
            for sid, age in (("old", 3 * 86400), ("new", 0)):
                _state("codex", sid, cwd=td, age_s=age, host_pids=[20])
                _owner_turn(sid, 20, 100.25, harness="codex", ts=now)
            with mock.patch.object(ot, "_proc_info", return_value=(99, "/bin/codex", 100.25)):
                with self.assertRaisesRegex(ValueError, "cannot tell which session is yours"):
                    sc.resolve_session_ref(host_pids=[20], cwd=td)

    def test_harnesses_without_prompt_records_choose_newest_state_in_busy_folder(self):
        for harness in ("aegis", "cursor"):
            with self.subTest(harness=harness), tempfile.TemporaryDirectory() as td, _home(td):
                for n in range(10):
                    _state(harness, f"old-{n}", cwd=td, age_s=(n + 1) * 86400)
                _state(harness, "new", cwd=td)
                self.assertEqual(sc.resolve_session_ref(host_pids=[], cwd=td),
                                 (harness, "new", "cwd"))

    def test_shared_issuer_without_prompt_times_refuses_instead_of_using_state_age(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            for sid, age in (("old", 3 * 86400), ("new", 0)):
                _state("codex", sid, cwd=td, age_s=age)
                _owner_turn(sid, 20, 100.25, harness="codex")
            with mock.patch.object(ot, "_proc_info", return_value=(99, "/bin/codex", 100.25)):
                with self.assertRaisesRegex(ValueError, "cannot tell which session is yours"):
                    sc.resolve_session_ref(host_pids=[20], cwd=td)

    def test_equal_state_times_without_prompt_records_refuse(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            for sid in ("a", "b"):
                p = _state("aegis", sid, cwd=td)
                os.utime(p, (100, 100))
            with self.assertRaisesRegex(ValueError, "cannot tell which session is yours"):
                sc.resolve_session_ref(host_pids=[], cwd=td)

    def test_folder_ranks_prompt_times_instead_of_capture_state_times(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            now = sc.time.time()
            _state("claude_code", "live", cwd=td, age_s=3 * 86400)
            _state("claude_code", "idle", cwd=td)
            _owner_turn("live", 20, 100.25, ts=now)
            _owner_turn("idle", 30, 101.5, ts=now - 3600)
            self.assertEqual(sc.resolve_session_ref(host_pids=[], cwd=td),
                             ("claude_code", "live", "cwd"))

    def test_near_simultaneous_recent_prompts_without_process_evidence_refuse(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            now = sc.time.time()
            for sid, ts in (("a", now - 1), ("b", now)):
                _state("claude_code", sid, cwd=td, age_s=3 * 86400)
                _owner_turn(sid, 20, 100.25, ts=ts)
            with self.assertRaisesRegex(ValueError, "cannot tell which session is yours"):
                sc.resolve_session_ref(host_pids=[], cwd=td)

    def test_a_long_running_turn_and_a_new_prompt_in_the_same_folder_refuse(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            now = sc.time.time()
            for sid, ts in (("long-turn", now - 20 * 60), ("new-prompt", now)):
                _state("claude_code", sid, cwd=td, age_s=3 * 86400)
                _owner_turn(sid, 20, 100.25, ts=ts)
            with self.assertRaisesRegex(ValueError, "cannot tell which session is yours"):
                sc.resolve_session_ref(host_pids=[], cwd=td)

    def test_fresh_state_beats_a_day_old_prompt_in_a_mixed_folder(self):
        for harness in ("aegis", "cursor"):
            with self.subTest(harness=harness), tempfile.TemporaryDirectory() as td, _home(td):
                now = sc.time.time()
                _state("claude_code", "idle", cwd=td, age_s=86400)
                _owner_turn("idle", 20, 100.25, ts=now - 86400)
                _state(harness, "live", cwd=td)
                self.assertEqual(sc.resolve_session_ref(host_pids=[], cwd=td),
                                 (harness, "live", "cwd"))

    def test_recent_state_and_recent_prompt_in_a_mixed_folder_refuse(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            now = sc.time.time()
            _state("claude_code", "long-turn", cwd=td, age_s=86400)
            _owner_turn("long-turn", 20, 100.25, ts=now - 20 * 60)
            _state("aegis", "live", cwd=td)
            with self.assertRaisesRegex(ValueError, "cannot tell which session is yours"):
                sc.resolve_session_ref(host_pids=[], cwd=td)

    def test_only_active_fallback_uses_prompt_time_with_stale_state(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            now = sc.time.time()
            for sid, ts in (("live", now), ("idle", now - 3600)):
                _state("claude_code", sid, cwd=str(Path(td) / "other"), age_s=3 * 86400)
                _owner_turn(sid, 20, 100.25, ts=ts)
            self.assertEqual(sc.resolve_session_ref(host_pids=[], cwd=td),
                             ("claude_code", "live", "only_active"))

    def test_only_active_state_fallback_still_works_outside_callers_folder(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            _state("aegis", "idle", age_s=3 * 86400)
            _state("aegis", "live")
            self.assertEqual(sc.resolve_session_ref(host_pids=[], cwd=td),
                             ("aegis", "live", "only_active"))

    def test_missing_issuer_start_cannot_match_through_pid_only_state(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            _state("claude_code", "a", cwd=td, host_pids=[20])
            _state("claude_code", "b", cwd=td)
            _owner_turn("a", 20, None)
            with mock.patch.object(ot, "_proc_info", return_value=(99, "/bin/claude", 100.25)):
                with self.assertRaisesRegex(ValueError, "cannot tell which session is yours"):
                    sc.resolve_session_ref(host_pids=[20], cwd=td)

    def test_a_single_session_in_the_cwd_still_resolves_with_stale_state(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            _state("claude_code", "only", cwd=td, age_s=3 * 86400)
            self.assertEqual(sc.resolve_session_ref(host_pids=[], cwd=td),
                             ("claude_code", "only", "cwd"))

    def test_a_reused_pid_does_not_match_even_with_recent_host_pids(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            _state("claude_code", "older", cwd=td, host_pids=[20])
            _state("claude_code", "newer", cwd=td)
            _owner_turn("older", 20, 100.25)
            with mock.patch.object(ot, "_proc_info", return_value=(99, "/bin/claude", 101.5)):
                with self.assertRaisesRegex(ValueError, "cannot tell which session is yours"):
                    sc.resolve_session_ref(host_pids=[20], cwd=td)

    def test_a_historical_issuer_is_not_used_instead_of_the_latest(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            _state("claude_code", "older", cwd=td)
            _state("claude_code", "newer", cwd=td)
            _owner_turn("older", 20, 100.25)
            _owner_turn("older", 30, 101.5)
            with mock.patch.object(ot, "_proc_info", return_value=(99, "/bin/claude", 100.25)):
                with self.assertRaisesRegex(ValueError, "cannot tell which session is yours"):
                    sc.resolve_session_ref(host_pids=[20], cwd=td)

    def test_an_issuer_of_another_harness_is_not_process_evidence(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            _state("claude_code", "older", cwd=td)
            _state("claude_code", "newer", cwd=td)
            _owner_turn("older", 20, 100.25, harness="codex")
            with mock.patch.object(ot, "_proc_info", return_value=(99, "/bin/claude", 100.25)):
                with self.assertRaisesRegex(ValueError, "cannot tell which session is yours"):
                    sc.resolve_session_ref(host_pids=[20], cwd=td)

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
