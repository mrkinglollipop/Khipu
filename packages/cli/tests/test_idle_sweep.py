"""Idle capture backstop; all state, transcripts and jobs use a throwaway home."""
from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from khipu import cli, jobs, session_capture as sc


class IdleSweepTest(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.home = Path(td.name)
        self.now = time.time()
        self.old = self.now - sc.MIN_MINUTES * 60 - 60
        env = mock.patch.dict(os.environ, {
            "HOME": str(self.home), "KHIPU_CAPTURE_HOME": str(self.home / "kh"),
            "KHIPU_PARENT_SESSION": "", "KHIPU_SWEEP_VERBOSE": "0",
        })
        env.start()
        self.addCleanup(env.stop)
        ident = mock.patch("khipu.identity.resolve_repo_root", return_value={
            "repo_root": "/repo", "project": "acme/project", "is_worktree": False,
        })
        self.identity = ident.start()
        self.addCleanup(ident.stop)

    def session(self, sid="s1", *, harness="codex", rows=None, **state):
        path = self.home / ".codex" / "sessions" / f"rollout-{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        if rows is None:
            rows = []
            for i in range(2):
                for role, text in (("user", f"Question {i} " + "x" * 100),
                                   ("assistant", f"Answer {i} " + "y" * 100)):
                    rows.append({"type": "response_item", "payload": {
                        "type": "message", "role": role,
                        "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
                    }})
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        os.utime(path, (self.old, self.old))
        st = {"offset": 0, "last_ts": self.old - 100, "captures": 0,
              "seen_ts": self.old, "seen_end": path.stat().st_size,
              "transcript_path": str(path), "cwd": str(self.home / "project")}
        st.update(state)
        sc.save_state(harness, sid, st)
        return path, st

    def test_idle_codex_window_queues_once_and_does_not_beat(self):
        path, before = self.session(parent_session_id="codex:parent")
        sc._heartbeat("codex", {"at": "2026-10-01T00:00:00Z", "event": "stop",
                                "session_id": "s1", "due": False, "new_turns": 2})
        beat = sc.dispatch_path("codex").read_bytes()
        out = sc.sweep_idle(now=self.now)
        self.assertEqual((out["swept"], out["jobs"], out["read"]), (1, 1, 1))
        job = json.loads(sc.queued_jobs()[0].read_text())
        self.assertEqual(job["event"], "idle_sweep")
        self.assertEqual(job["turns"], 2)
        self.assertEqual(job["parent_session_id"], "codex:parent")
        self.assertEqual((job["project"], job["repo_root"]), ("acme/project", "/repo"))
        self.identity.assert_called_once_with(before["cwd"])
        after = sc.load_state("codex", "s1")
        self.assertEqual(after["offset"], path.stat().st_size)
        self.assertEqual(after["seen_ts"], before["seen_ts"])
        self.assertEqual(after["seen_end"], before["seen_end"])
        self.assertEqual(after["sweep_checked_mtime"], path.stat().st_mtime)
        self.assertEqual(sc.dispatch_path("codex").read_bytes(), beat)
        with mock.patch.object(sc, "read_window", wraps=sc.read_window) as read:
            self.assertEqual(sc.sweep_idle(now=self.now)["swept"], 0)
            read.assert_not_called()
        self.assertEqual(len(sc.queued_jobs()), 1)
        log = sc.log_path().read_text()
        self.assertIn("sweep codex:s1: queued 1 part(s)", log)
        self.assertEqual(log.count("sweep summary:"), 1)

    def test_fresh_transcript_or_hook_is_not_read(self):
        for fresh in ("transcript", "hook"):
            with self.subTest(fresh=fresh):
                path, _ = self.session(sid=fresh, seen_ts=self.now if fresh == "hook" else self.old)
                if fresh == "transcript":
                    os.utime(path, (self.now, self.now))
        with mock.patch.object(sc, "read_window") as read:
            out = sc.sweep_idle(now=self.now)
        read.assert_not_called()
        self.assertEqual(out["skipped"], {"not_idle": 2})

    def test_nothing_unread_is_not_read(self):
        path, st = self.session()
        for offset in (path.stat().st_size, path.stat().st_size + 1):
            st["offset"] = offset
            sc.save_state("codex", "s1", st)
            with mock.patch.object(sc, "read_window") as read:
                out = sc.sweep_idle(now=self.now)
            read.assert_not_called()
            self.assertEqual(out["skipped"], {"nothing_unread": 1})

    def test_helpers_subagents_flags_recall_and_unknown_harness_are_skipped(self):
        self.session("helper", cwd="/tmp/t3code-claude-title-test")
        self.session("child", subagent=True)
        self.session("s1_agent_x")
        self.session("unknown", harness="other")
        sc.save_state("codex", "missing", {})
        sc.save_state("recall", "s1", {"transcript_path": "unused"})
        flag = sc.request_capture_now("codex", "s1")
        flag_bytes = flag.read_bytes()
        unknown_bytes = sc._state_file("other", "unknown").read_bytes()
        with mock.patch.object(sc, "read_window") as read:
            out = sc.sweep_idle(now=self.now)
        read.assert_not_called()
        self.assertEqual(out["skipped"], {"helper": 1, "subagent": 2, "unknown_harness": 2,
                                          "flag": 1, "no_transcript": 1})
        self.assertEqual(flag.read_bytes(), flag_bytes)
        self.assertEqual(sc._state_file("other", "unknown").read_bytes(), unknown_bytes)

    def test_missing_transcript_and_missing_seen_marker_are_skipped(self):
        self.session("missing", transcript_path=str(self.home / "missing.jsonl"))
        self.session("unseen", seen_ts=None)
        with mock.patch.object(sc, "read_window") as read:
            out = sc.sweep_idle(now=self.now)
        read.assert_not_called()
        self.assertEqual(out["skipped"], {"missing_transcript": 1, "not_idle": 1})

    def test_capture_newer_than_transcript_is_skipped(self):
        self.session(last_ts=self.old)
        with mock.patch.object(sc, "read_window") as read:
            out = sc.sweep_idle(now=self.now)
        read.assert_not_called()
        self.assertEqual(out["skipped"], {"already_captured": 1})

    def test_limit_counts_transcript_reads_and_later_runs_make_progress(self):
        for i in range(3):
            self.session(str(i))
        with mock.patch.object(sc, "read_window", wraps=sc.read_window) as read:
            out = sc.sweep_idle(now=self.now, limit=1)
        self.assertEqual((read.call_count, out["swept"]), (1, 1))
        self.assertEqual(out["skipped"]["limit"], 1)
        self.assertEqual(sc.sweep_idle(now=self.now, limit=1)["swept"], 1)
        self.assertEqual(sc.sweep_idle(now=self.now, limit=1)["swept"], 1)
        self.assertEqual(len(sc.queued_jobs()), 3)
        self.session("zero")
        with mock.patch.object(sc, "read_window") as read:
            self.assertEqual(sc.sweep_idle(now=self.now, limit=0)["swept"], 0)
        read.assert_not_called()

    def test_garbled_state_is_logged_without_stopping_good_sessions(self):
        self.session()
        sc._state_file("codex", "bad").write_text("{broken", encoding="utf-8")
        out = sc.sweep_idle(now=self.now)
        self.assertEqual(out["swept"], 1)
        self.assertEqual(out["skipped"]["error"], 1)
        self.assertIn("sweep: skipped codex--bad.json: JSONDecodeError", sc.log_path().read_text())

    def test_unchanged_small_or_assistant_only_windows_are_not_reread(self):
        for sid, role, text in (("small", "user", "hi"), ("assistant", "assistant", "x" * 300)):
            self.session(sid, rows=[{"type": "event_msg", "payload": {
                "type": "user_message" if role == "user" else "agent_message", "message": text}}])
        self.assertEqual(sc.sweep_idle(now=self.now)["skipped"], {"nothing_new": 2})
        with mock.patch.object(sc, "read_window") as read:
            out = sc.sweep_idle(now=self.now)
        read.assert_not_called()
        self.assertEqual(out["skipped"], {"unchanged": 2})
        path = self.home / ".codex" / "sessions" / "rollout-small.jsonl"
        with path.open("a") as stream:
            stream.write(json.dumps({"type": "event_msg", "payload": {
                "type": "user_message", "message": "x" * 300}}) + "\n")
        os.utime(path, (self.old + 1, self.old + 1))
        self.assertEqual(sc.sweep_idle(now=self.now)["swept"], 1)

    def test_sweep_redacts_and_keeps_full_session_identity(self):
        secret = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"
        self.session("safe", session_id="original:id", rows=[
            {"type": "event_msg", "payload": {"type": "user_message", "message": "x" * 300 + " " + secret}},
        ])
        sc.sweep_idle(now=self.now)
        job = json.loads(sc.queued_jobs()[0].read_text())
        self.assertNotIn(secret, job["transcript"])
        self.assertIn("[REDACTED]", job["transcript"])
        self.assertEqual(job["session_id"], "original:id")

    def test_dry_run_does_not_queue_or_advance_state(self):
        self.session()
        before = sc._state_file("codex", "s1").read_bytes()
        out = sc.sweep_idle(now=self.now, dry_run=True)
        self.assertEqual((out["swept"], out["jobs"]), (1, 0))
        self.assertEqual(sc.queued_jobs(), [])
        self.assertEqual(sc._state_file("codex", "s1").read_bytes(), before)
        self.assertEqual(sc.sweep_idle(now=self.now)["swept"], 1)

    def test_time_budget_stops_without_advancing_or_caching_a_window(self):
        self.session()
        with mock.patch.object(sc.time, "monotonic", side_effect=[0, 0, sc.SWEEP_TIMEOUT_S]):
            out = sc.sweep_idle(now=self.now)
        self.assertEqual(out["skipped"], {"time_budget": 1})
        self.assertEqual(out["swept"], 0)
        self.assertEqual(sc.load_state("codex", "s1")["offset"], 0)
        self.assertNotIn("sweep_checked_mtime", sc.load_state("codex", "s1"))

    def test_concurrent_hook_state_change_is_not_overwritten(self):
        path, st = self.session()
        real_read = sc.read_window

        def read(*args):
            result = real_read(*args)
            st["seen_ts"] = self.now
            sc.save_state("codex", "s1", st)
            return result

        with mock.patch.object(sc, "read_window", side_effect=read):
            out = sc.sweep_idle(now=self.now)
        self.assertEqual(out["skipped"], {"changed": 1})
        self.assertEqual(sc.load_state("codex", "s1"), st)
        self.assertEqual(sc.queued_jobs(), [])

    def test_enqueue_failure_keeps_offset_for_retry(self):
        self.session()
        with mock.patch.object(sc, "enqueue", side_effect=OSError("disk full")):
            out = sc.sweep_idle(now=self.now)
        self.assertEqual(out["skipped"], {"error": 1})
        self.assertEqual(sc.load_state("codex", "s1")["offset"], 0)
        self.assertNotIn("sweep_checked_mtime", sc.load_state("codex", "s1"))
        self.assertEqual(sc.sweep_idle(now=self.now)["skipped"], {"error_cached": 1})
        self.assertEqual(sc.sweep_idle(now=self.now + 3601)["swept"], 1)

    def test_noop_summary_is_logged_only_when_verbose(self):
        with mock.patch.object(sc, "_log") as log:
            sc.sweep_idle(now=self.now)
        log.assert_not_called()
        with mock.patch.dict(os.environ, {"KHIPU_SWEEP_VERBOSE": "1"}), \
                mock.patch.object(sc, "_log") as log:
            sc.sweep_idle(now=self.now)
        self.assertEqual(log.call_count, 1)
        self.assertIn("sweep summary:", log.call_args.args[0])

    def test_cli_sweep_dry_run_prints_json_and_honours_limit(self):
        self.session()
        before = sc._state_file("codex", "s1").read_bytes()
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            rc = cli.main(["sessions", "sweep", "--dry-run", "--limit", "0"])
        out = json.loads(stdout.getvalue())
        self.assertEqual((rc, out["swept"], out["read"]), (0, 0, 0))
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            cli.main(["sessions", "sweep", "--dry-run", "--limit", "1"])
        self.assertEqual(json.loads(stdout.getvalue())["swept"], 1)
        self.assertEqual(sc.queued_jobs(), [])
        self.assertEqual(sc._state_file("codex", "s1").read_bytes(), before)

    def test_drainer_sweeps_before_listing_jobs_and_passes_event_to_capture(self):
        self.session()
        with mock.patch.object(sc, "land_transcript_images", return_value={}), \
                mock.patch("khipu.config.capture_mode", return_value="hub"), \
                mock.patch("khipu.extract.extract_memory", return_value={"summary": "captured"}), \
                mock.patch("khipu.capture.capture", return_value=0) as capture, \
                mock.patch("khipu.hub_snapshot.sync_decision_changes", return_value={"ok": True}), \
                mock.patch.object(sc, "_heartbeat") as beat:
            out = sc.drain(sweep=True)
        self.assertEqual((out["sweep"]["swept"], out["captured"]), (1, 1))
        self.assertEqual(capture.call_args.args[0]["event"], "idle_sweep")
        beat.assert_not_called()
        self.assertEqual(sc.queued_jobs(), [])

    def test_default_hook_drainer_does_not_sweep(self):
        self.session()
        with mock.patch.object(sc, "sweep_idle") as sweep:
            out = sc.drain()
        sweep.assert_not_called()
        self.assertEqual(out["jobs"], 0)

    def test_sweep_does_not_make_a_stale_session_active_for_capture_now(self):
        self.old = self.now - 13 * 3600
        self.session("stale", host_pids=[10])
        sc.sweep_idle(now=self.now)
        candidates = sc._capture_candidates()
        self.assertEqual(candidates[0]["mtime"], self.old)
        with self.assertRaises(ValueError):
            sc.resolve_session_ref(host_pids=[10], cwd="/unrelated")

    def test_cli_drain_and_nightly_opt_in(self):
        out = {"failed": 0}
        with mock.patch.object(sc, "drain", return_value=out) as drain, \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(cli.main(["sessions", "drain"]), 0)
        drain.assert_called_once_with(limit=None, dry_run=False, sweep=True)
        with mock.patch.object(sc, "drain", return_value=out) as drain:
            self.assertTrue(jobs._drain_sessions()["ok"])
        drain.assert_called_once_with(sweep=True, limit=200, time_budget_s=300)
        with mock.patch.object(sc, "drain", side_effect=RuntimeError("offline")):
            self.assertEqual(jobs._drain_sessions(), {"ok": False, "error": "RuntimeError: offline"})

    def test_sweep_preserves_mtime_for_jobs_and_nothing_new(self):
        self.session("job")
        self.session("small", rows=[{"type": "event_msg", "payload": {
            "type": "user_message", "message": "hi"}}])
        for sid in ("job", "small"):
            os.utime(sc._state_file("codex", sid), (self.old, self.old))
        before = {sid: sc._state_file("codex", sid).stat().st_mtime_ns for sid in ("job", "small")}
        sc.sweep_idle(now=self.now)
        for sid, stamp in before.items():
            self.assertEqual(sc._state_file("codex", sid).stat().st_mtime_ns, stamp)

    def test_swept_orphan_does_not_displace_recent_hooks_in_liveness(self):
        path, _ = self.session("orphan", seen_ts=self.now - 8 * 3600, seen_end=0,
                               last_ts=self.now - 9 * 3600)
        os.utime(path, (self.now - 3 * 3600, self.now - 3 * 3600))
        for i in range(sc.ACTIVITY_SCAN_LIMIT):
            self.session(f"live-{i}", seen_ts=self.now)
            os.utime(sc._state_file("codex", f"live-{i}"), (self.old - 1, self.old - 1))
        sc._heartbeat("codex", {"at": sc._mint_ts(), "event": "stop",
                                "session_id": "live-0", "due": False, "new_turns": 0})
        self.assertEqual(sc.sweep_idle(now=self.now)["swept"], 1)
        self.assertIsNone(sc._stopped_hook_evidence("codex")[0])
        self.assertTrue(sc.liveness("codex")["ok"])
        with mock.patch.object(sc, "ACTIVITY_SCAN_LIMIT", sc.ACTIVITY_SCAN_LIMIT + 1):
            self.assertIsNotNone(sc._stopped_hook_evidence("codex")[0])

    def test_locked_state_is_skipped_by_sweep_and_hook(self):
        path, st = self.session()
        before = sc._state_file("codex", "s1").read_bytes()
        with sc._state_lock("codex", "s1") as acquired:
            self.assertTrue(acquired)
            with mock.patch.object(sc, "read_window") as read:
                self.assertEqual(sc.sweep_idle(now=self.now)["skipped"], {"locked": 1})
                out = sc.hook_main(json.dumps({"session_id": "s1", "cwd": st["cwd"],
                    "transcript_path": str(path), "hook_event_name": "SessionEnd"}), "codex")
            read.assert_not_called()
            self.assertEqual((out["due"], out["reason"]), (False, "locked"))
        self.assertEqual(sc._state_file("codex", "s1").read_bytes(), before)
        self.assertFalse(sc._state_file("codex", "s1").with_suffix(".lock").exists())

    def test_state_changed_before_lock_is_not_read_or_swept(self):
        for field, value in (("offset", 1), ("seen_ts", self.now)):
            with self.subTest(field=field):
                _, st = self.session()
                real_lock = sc._state_lock

                @contextmanager
                def changed_lock(harness, sid):
                    st[field] = value
                    sc.save_state(harness, sid, st)
                    with real_lock(harness, sid) as acquired:
                        yield acquired

                with mock.patch.object(sc, "_state_lock", side_effect=changed_lock), \
                        mock.patch.object(sc, "read_window") as read:
                    self.assertEqual(sc.sweep_idle(now=self.now)["skipped"], {"changed": 1})
                read.assert_not_called()
                self.assertEqual(sc.load_state("codex", "s1"), st)
        self.assertEqual(sc.queued_jobs(), [])

    def test_lock_is_released_after_failure_and_stale_lock_is_reclaimed(self):
        self.session()
        lock = sc._state_file("codex", "s1").with_suffix(".lock")
        lock.mkdir()
        os.utime(lock, (self.now - 61, self.now - 61))
        with mock.patch.object(sc, "enqueue", side_effect=OSError("disk full")):
            self.assertEqual(sc.sweep_idle(now=self.now)["skipped"], {"error": 1})
        self.assertFalse(lock.exists())
        with mock.patch.object(sc, "read_window", side_effect=RuntimeError("broken")):
            path = self.home / ".codex" / "sessions" / "rollout-s1.jsonl"
            out = sc.hook_main(json.dumps({"session_id": "s1", "transcript_path": str(path),
                "hook_event_name": "SessionEnd"}), "codex")
        self.assertIn("RuntimeError", out["error"])
        self.assertFalse(lock.exists())

    def test_hook_cannot_overwrite_sweep_during_identity_lookup(self):
        path, st = self.session()
        env = json.dumps({"session_id": "s1", "cwd": st["cwd"],
                          "transcript_path": str(path), "hook_event_name": "SessionEnd"})

        def resolve(cwd):
            with path.open("a") as stream:
                stream.write(json.dumps({"type": "event_msg", "payload": {
                    "type": "user_message", "message": "new turn " + "z" * 300}}) + "\n")
            self.assertEqual(sc.hook_main(env, "codex")["reason"], "locked")
            self.assertEqual(sc.sweep_idle(now=self.now)["swept"], 0)
            return {}

        self.identity.side_effect = resolve
        out = sc.sweep_idle(now=self.now)
        self.assertEqual(out["jobs"], 1)
        self.assertLess(sc.load_state("codex", "s1")["offset"], path.stat().st_size)
        self.identity.side_effect = None
        hook = sc.hook_main(env, "codex")
        self.assertTrue(hook["due"], hook)
        ranges = [json.loads(p.read_text())["transcript_range"] for p in sc.queued_jobs()]
        first, second = sorted(tuple(map(int, r.split(":"))) for r in ranges)
        self.assertEqual(first[1], second[0])

    def test_hook_stores_identity_for_later_sweep(self):
        path, st = self.session("original_id")
        with mock.patch.object(sc, "decide", return_value=(False, "waiting")), \
                mock.patch.dict(os.environ, {"KHIPU_PARENT_SESSION": "codex:parent"}), \
                mock.patch.object(sc.time, "time", return_value=self.old):
            out = sc.hook_main(json.dumps({"session_id": "original:id", "cwd": st["cwd"],
                "transcript_path": str(path), "hook_event_name": "Stop"}), "codex")
        self.assertFalse(out["due"])
        saved = sc.load_state("codex", "original:id")
        self.assertEqual((saved["session_id"], saved["parent_session_id"]),
                         ("original:id", "codex:parent"))
        self.assertEqual(sc.sweep_idle(now=self.now)["swept"], 1)
        job = json.loads(sc.queued_jobs()[0].read_text())
        self.assertEqual((job["session_id"], job["parent_session_id"]),
                         ("original:id", "codex:parent"))

    def test_newest_transcript_is_read_first_and_errors_do_not_starve_others(self):
        self.session("old")
        new_path, _ = self.session("new")
        os.utime(new_path, (self.old + 1, self.old + 1))
        real_read = sc.read_window

        def read(path, offset):
            if path == new_path:
                raise OSError("cannot read")
            return real_read(path, offset)

        with mock.patch.object(sc, "read_window", side_effect=read) as reader:
            self.assertEqual(sc.sweep_idle(now=self.now, limit=1)["skipped"]["error"], 1)
            self.assertEqual(reader.call_args.args[0], new_path)
            stamp = sc._state_file("codex", "new").stat().st_mtime_ns
            self.assertEqual(sc.sweep_idle(now=self.now + 100, limit=1)["swept"], 1)
            self.assertEqual(reader.call_count, 2)
            self.assertEqual(sc.sweep_idle(now=self.now + 3601, limit=1)["skipped"]["error"], 1)
        self.assertEqual(sc._state_file("codex", "new").stat().st_mtime_ns, stamp)

    def test_malformed_state_error_is_cached_and_dry_run_is_read_only(self):
        self.session()
        bad = sc._state_file("codex", "bad")
        bad.write_text("{broken")
        sc.sweep_idle(now=self.now, dry_run=True)
        self.assertFalse(bad.with_suffix(".sweep-error").exists())
        sc.sweep_idle(now=self.now)
        self.assertEqual(sc.sweep_idle(now=self.now + 100)["skipped"]["error_cached"], 1)
        self.assertEqual(sc.sweep_idle(now=self.now + 3601)["skipped"]["error"], 1)
        self.assertEqual(bad.read_text(), "{broken")

    def test_dry_run_lists_sessions_and_unread_counts(self):
        _, st = self.session(session_id="original:id")
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            cli.main(["sessions", "sweep", "--dry-run", "--limit", "1"])
        session = json.loads(stdout.getvalue())["sessions"][0]
        self.assertEqual((session["harness"], session["session_id"], session["unread_turns"]),
                         ("codex", "original:id", 2))
        self.assertGreater(session["unread_chars"], sc.MIN_CHARS)
        self.assertAlmostEqual(session["idle_minutes"], (self.now - st["seen_ts"]) / 60, places=2)

    def test_drain_time_budget_leaves_jobs_unclaimed(self):
        self.session()
        sc.sweep_idle(now=self.now)
        with mock.patch.object(sc, "_claim") as claim:
            out = sc.drain(time_budget_s=0)
        claim.assert_not_called()
        self.assertTrue(out["time_budget_exhausted"])
        self.assertEqual(len(sc.queued_jobs()), 1)

    def test_nightly_drain_reports_failed_or_exhausted_steps(self):
        for result in ({"failed": 1}, {"failed": 0, "time_budget_exhausted": True},
                       {"failed": 0, "sweep": {"skipped": {"error": 1}}}):
            with mock.patch.object(sc, "drain", return_value=result):
                self.assertFalse(jobs._drain_sessions()["ok"])

    def test_drain_budget_stops_between_jobs_and_releases_no_extra_claims(self):
        self.session("one")
        self.session("two")
        sc.sweep_idle(now=self.now)
        clock = [0]

        def extract(*args, **kwargs):
            clock[0] = 301
            return {"summary": "captured"}

        with mock.patch.object(sc.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(sc, "land_transcript_images", return_value={}), \
                mock.patch("khipu.config.capture_mode", return_value="hub"), \
                mock.patch("khipu.extract.extract_memory", side_effect=extract), \
                mock.patch("khipu.capture.capture", return_value=0), \
                mock.patch("khipu.hub_snapshot.sync_decision_changes") as sync:
            out = sc.drain(time_budget_s=300)
        self.assertEqual(out["captured"], 1)
        self.assertTrue(out["time_budget_exhausted"])
        self.assertEqual(len(sc.queued_jobs()), 1)
        self.assertEqual(list(sc.queue_dir().glob("*.working*")), [])
        sync.assert_not_called()

    def test_due_hook_job_bytes_match_original_schema_for_split_windows(self):
        rows = [
            {"type": "event_msg", "payload": {"type": "user_message", "message": "é" * 100}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "a" * 100}},
        ]
        path, st = self.session(rows=rows)
        ts = "2026-10-08T12:00:00Z"
        identifiers = iter(mock.Mock(hex=f"{i:08x}" * 4) for i in range(1, 4))
        with mock.patch.object(sc, "MAX_TRANSCRIPT", 150), \
                mock.patch.object(sc, "_mint_ts", return_value=ts), \
                mock.patch.object(sc.time, "time", return_value=self.now), \
                mock.patch.object(sc.uuid, "uuid4", side_effect=lambda: next(identifiers)), \
                mock.patch.object(sc, "ancestor_pids", return_value=[]), \
                mock.patch.dict(os.environ, {"KHIPU_PARENT_SESSION": "codex:parent"}):
            sc.request_capture_now("codex", "s1", "note é")
            out = sc.hook_main(json.dumps({"session_id": "s1", "cwd": st["cwd"],
                                          "transcript_path": str(path), "hook_event_name": "Stop"}), "codex")
        self.assertTrue(out["due"], out)
        expected = []
        for i, text in enumerate(("USER: " + "é" * 100, "ASSISTANT: " + "a" * 100), 1):
            expected.append(json.dumps({
                "harness": "codex", "session_id": "s1", "cwd": st["cwd"], "event": "stop",
                "ts": ts, "turns": 1, "transcript": text, "transcript_path": str(path),
                "offset_before": 0, "offset_after": path.stat().st_size,
                "repo_root": "/repo", "project": "acme/project", "parent_session_id": "codex:parent",
                "transcript_range": f"0:{path.stat().st_size}", "truncated_chars": 0,
                "window_id": f"{1:08x}" * 4, "part": f"{i}/2", "capture_note": "note é",
            }, ensure_ascii=False).encode("utf-8"))
        self.assertCountEqual([p.read_bytes() for p in sc.queued_jobs()], expected)
        self.assertEqual(sc.load_state("codex", "s1")["offset"], path.stat().st_size)
