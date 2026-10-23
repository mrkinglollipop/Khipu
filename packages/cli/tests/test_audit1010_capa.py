"""Focused CAPA regression coverage for native capture behavior."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from khipu import activity
from khipu import session_capture as sc


def _write(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


class CaptureCapaTest(unittest.TestCase):
    def test_scheduled_empty_queue_replays_outbox_with_bounds(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"KHIPU_CAPTURE_HOME": td}), \
                mock.patch.object(sc, "sweep_idle", return_value={}), \
                mock.patch("khipu.outbox.drain", return_value={"replayed": 1, "failed": 0}) as replay:
            out = sc.drain(sweep=True, limit=200, time_budget_s=300)
        replay.assert_called_once()
        self.assertLessEqual(replay.call_args.kwargs["limit"], 20)
        self.assertLessEqual(replay.call_args.kwargs["time_budget_s"], 60)
        self.assertEqual(out["outbox"]["replayed"], 1)

    def test_successful_empty_extraction_is_archived_and_unblocks_sibling(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"KHIPU_CAPTURE_HOME": td}), \
                mock.patch("khipu.extract._generate", return_value='{"summary":""}') as generate, \
                mock.patch("khipu.capture.capture") as capture, \
                mock.patch("khipu.config.capture_mode", return_value="hub"):
            for part in ("1/2", "2/2"):
                sc.enqueue({"harness": "codex", "session_id": "s", "ts": sc._mint_ts(),
                            "window_id": "empty-window", "part": part,
                            "transcript": "ASSISTANT: All right then."})
            out = sc.drain()
            again = sc.drain()
            archived = list((sc.khipu_home() / "empty").glob("*.json"))
            self.assertEqual(out["empty"], 2)
            self.assertEqual(out["failed"], 0)
            self.assertEqual(again["jobs"], 0)
            self.assertEqual(generate.call_count, 2)
            self.assertEqual(len(archived), 2)
            self.assertTrue(all(json.loads(p.read_text())["disposition"] == "empty" for p in archived))
            capture.assert_not_called()

    def test_empty_archive_failure_keeps_source_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"KHIPU_CAPTURE_HOME": td}), \
                mock.patch("khipu.extract._generate", return_value='{"summary":""}') as generate, \
                mock.patch("khipu.config.capture_mode", return_value="hub"):
            sc.enqueue({"harness": "codex", "session_id": "s", "ts": sc._mint_ts(),
                        "transcript": "ASSISTANT: All right then."})
            (sc.khipu_home() / "empty").write_text("blocked")
            out = sc.drain()
            self.assertEqual(out["failed"], 1)
            pending = sc.queued_jobs()
            self.assertEqual(len(pending), 1)
            self.assertEqual(json.loads(pending[0].read_text()).get("disposition"), "empty")
            (sc.khipu_home() / "empty").unlink()
            self.assertEqual(sc.drain()["empty"], 1)
            self.assertEqual(generate.call_count, 1)

    def test_oversize_coalesced_message_is_lossless_bounded_fragments(self):
        text = "begin-" + "m" * 70 + "-end"
        parts = sc._window_parts([("assistant", text)], 30)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part["text"]) <= 30 for part in parts))
        self.assertEqual("".join(p["text"].split(": ", 1)[1] for p in parts), text)
        self.assertTrue(all(part["truncated_chars"] == 0 for part in parts))

    def test_fragment_boundaries_preserve_whitespace_and_newlines(self):
        text = "  begin\n" + "m" * 40 + "\nend  "
        parts = sc._window_parts([("assistant", text)], 24)
        self.assertEqual("".join(p["text"].split(": ", 1)[1] for p in parts), text)

    def test_acp_coalesced_overflow_preserves_each_role_exactly(self):
        with tempfile.TemporaryDirectory() as td:
            user, assistant = " user\n" * 30, " assistant\n" * 30
            path = _write(Path(td) / "updates.jsonl", [
                {"params": {"update": {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": user[:90]}}}},
                {"params": {"update": {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": user[90:]}}}},
                {"params": {"update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": assistant[:100]}}}},
                {"params": {"update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": assistant[100:]}}}},
            ])
            msgs, _, users = sc.read_window(path, 0)
        parts = sc._window_parts(msgs, 40)
        recovered = {"user": "", "assistant": ""}
        for part in parts:
            role, text = part["text"].split(": ", 1)
            recovered[role.lower()] += text
        self.assertEqual(users, 1)
        self.assertEqual(recovered, {"user": user, "assistant": assistant})

    def test_subagentstop_flushes_normal_one_prompt_child(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"KHIPU_CAPTURE_HOME": str(Path(td) / "kh")}):
            child = _write(Path(td) / "child.jsonl", [
                {"type": "user", "message": {"role": "user", "content": "please inspect this"}},
                {"type": "assistant", "message": {"role": "assistant", "content": "result " + "x" * 220}},
            ])
            out = sc.hook_main(json.dumps({"hookEventName": "SubagentStop", "session_id": "parent",
                                            "agent_id": "normal", "cwd": td,
                                            "agent_transcript_path": str(child)}), "claude_code")
            self.assertTrue(out["due"], out)
            self.assertEqual(json.loads(sc.queued_jobs()[0].read_text())["parent_session_id"], "parent")

    def test_short_explicit_request_precedes_minimum_character_gate(self):
        text = "USER: save this"
        due, why = sc.decide("stop", user_turns=1, chars=len(text), elapsed_s=0,
                             stop_hook_active=False, window_text=text)
        self.assertTrue(due)
        self.assertIn("explicit", why)

    def test_short_high_value_hook_queues_below_minimum_chars(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"KHIPU_CAPTURE_HOME": str(Path(td) / "kh")}):
            transcript = _write(Path(td) / "short.jsonl", [
                {"type": "user", "message": {"role": "user", "content": "remember this"}},
            ])
            out = sc.hook_main(json.dumps({"hookEventName": "Stop", "session_id": "short", "cwd": td,
                                           "transcript_path": str(transcript)}), "claude_code")
            self.assertTrue(out["due"], out)
            self.assertEqual(len(sc.queued_jobs()), 1)

    def test_subagentstop_flushes_child_without_parent_state_collision(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"KHIPU_CAPTURE_HOME": str(Path(td) / "kh")}):
            child = _write(Path(td) / "child.jsonl", [
                {"type": "assistant", "message": {"role": "assistant", "content": "result " + "x" * 220}},
            ])
            out = sc.hook_main(json.dumps({"hookEventName": "SubagentStop", "session_id": "parent",
                                            "agent_id": "child", "cwd": td,
                                            "agent_transcript_path": str(child)}), "claude_code")
            self.assertTrue(out["due"], out)
            job = json.loads(sc.queued_jobs()[0].read_text())
            self.assertEqual(job["session_id"], "parent:agent:child")
            self.assertEqual(job["parent_session_id"], "parent")
            self.assertTrue(sc._state_file("claude_code", "parent:agent:child").exists())
            self.assertFalse(sc._state_file("claude_code", "parent").exists())

    def test_terminal_assistant_tail_queues_exactly_once(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"KHIPU_CAPTURE_HOME": str(Path(td) / "kh")}):
            transcript = _write(Path(td) / "tail.jsonl", [
                {"type": "assistant", "message": {"role": "assistant", "content": "final " + "x" * 220}},
            ])
            raw = json.dumps({"hookEventName": "SessionEnd", "session_id": "tail", "cwd": td,
                              "transcript_path": str(transcript)})
            first = sc.hook_main(raw, "claude_code")
            second = sc.hook_main(raw, "claude_code")
            self.assertTrue(first["due"], first)
            self.assertFalse(second["due"], second)
            self.assertEqual(len(sc.queued_jobs()), 1)

    def test_first_stop_window_is_later_recovered_by_idle_sweep(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"KHIPU_CAPTURE_HOME": str(Path(td) / "kh")}):
            transcript = _write(Path(td) / "idle.jsonl", [
                {"type": "user", "message": {"role": "user", "content": "question " + "x" * 220}},
            ])
            raw = json.dumps({"hookEventName": "Stop", "session_id": "idle", "cwd": td,
                              "transcript_path": str(transcript)})
            first = sc.hook_main(raw, "claude_code")
            self.assertFalse(first["due"], first)
            old = time.time() - sc.MIN_MINUTES * 60 - 60
            state = sc.load_state("claude_code", "idle")
            state["seen_ts"] = old
            sc.save_state("claude_code", "idle", state)
            os.utime(transcript, (old, old))
            swept = sc.sweep_idle(now=time.time())
            self.assertEqual(swept["swept"], 1)
            self.assertEqual(len(sc.queued_jobs()), 1)

    def test_capture_now_note_becomes_a_durable_note_only_job(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"KHIPU_CAPTURE_HOME": str(Path(td) / "kh")}):
            transcript = _write(Path(td) / "empty.jsonl", [])
            sc.request_capture_now("claude_code", "s", "retain me")
            out = sc.hook_main(json.dumps({"hookEventName": "SessionEnd", "session_id": "s", "cwd": td,
                                            "transcript_path": str(transcript)}), "claude_code")
            self.assertTrue(out["due"])
            self.assertFalse(sc._capture_now_flag_path("claude_code", "s").exists())
            job = json.loads(sc.queued_jobs()[0].read_text())
            self.assertEqual(job["turns"], 0)
            self.assertIn("Capture note: retain me", job["transcript"])

    def test_capture_now_enqueue_failure_keeps_request_flag(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"KHIPU_CAPTURE_HOME": str(Path(td) / "kh")}):
            transcript = _write(Path(td) / "empty.jsonl", [])
            flag = sc.request_capture_now("claude_code", "s", "retry me")
            with mock.patch.object(sc, "_enqueue_window", side_effect=OSError("disk full")):
                out = sc.hook_main(json.dumps({"hookEventName": "SessionEnd", "session_id": "s", "cwd": td,
                                               "transcript_path": str(transcript)}), "claude_code")
            self.assertIn("disk full", out["error"])
            self.assertTrue(flag.exists())

    def test_capture_now_replacement_waits_for_ack_and_remains_pending(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"KHIPU_CAPTURE_HOME": str(Path(td) / "kh")}):
            original = {"note": "old", "requested_at": "old"}
            flag = sc._capture_now_flag_path("codex", "s")
            flag.parent.mkdir(parents=True)
            flag.write_text(json.dumps(original))
            started = threading.Event()

            def replace():
                started.set()
                sc.request_capture_now("codex", "s", "new")

            with sc._capture_now_lock("codex", "s") as acquired:
                self.assertTrue(acquired)
                thread = threading.Thread(target=replace)
                thread.start()
                self.assertTrue(started.wait(1))
                sc._ack_capture_now("codex", "s", original)
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(json.loads(flag.read_text())["note"], "new")

    def test_live_old_claim_is_not_reclaimed_by_mtime(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"KHIPU_CAPTURE_HOME": str(Path(td) / "kh")}):
            q = sc.queue_dir()
            q.mkdir(parents=True)
            claim = q / f"x.claimed.{os.getpid()}"
            claim.write_text("{}")
            old = time.time() - sc.CLAIM_STALE_S - 1
            os.utime(claim, (old, old))
            self.assertEqual(sc._reclaim_stale(), 0)
            self.assertTrue(claim.exists())

    def test_delivery_retry_reuses_durable_extraction(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"KHIPU_CAPTURE_HOME": str(Path(td) / "kh")}):
            sc.enqueue({"harness": "aegis", "session_id": "s", "cwd": "/x", "event": "stop",
                        "ts": sc._mint_ts(), "turns": 1, "transcript": "USER: retained",
                        "extracted_payload": {"summary": "cached", "topics": [], "decisions": [],
                                              "preferences": [], "scope": "", "edges": [], "topic_pages": []}})
            captured = []
            with mock.patch("khipu.extract.extract_memory", side_effect=AssertionError("second model call")), \
                    mock.patch("khipu.capture.capture", lambda payload, mode=None: captured.append(payload) or 0), \
                    mock.patch("khipu.config.capture_mode", lambda: "hub"):
                self.assertEqual(sc.drain()["captured"], 1)
            self.assertEqual(captured[0]["summary"], "cached")

    def test_extraction_checkpoint_temp_is_not_a_claim_candidate(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"KHIPU_CAPTURE_HOME": str(Path(td) / "kh")}):
            q = sc.queue_dir()
            q.mkdir(parents=True)
            claimed = q / f"job.claimed.{os.getpid()}"
            claimed.write_text("{}")
            observed = []
            real_replace = os.replace

            def inspect_temp(src, dst):
                observed.append(Path(src).name)
                self.assertEqual(list(q.glob("*.claimed.*")), [claimed])
                real_replace(src, dst)

            with mock.patch.object(sc.os, "replace", side_effect=inspect_temp):
                sc._save_claimed_job(claimed, {"part": "1/2"})
            self.assertTrue(observed[0].startswith(".extract-"), observed)

    def test_sibling_parts_cannot_overlap_and_drain_in_sequence(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"KHIPU_CAPTURE_HOME": str(Path(td) / "kh")}):
            q = sc.queue_dir()
            q.mkdir(parents=True)
            base = {"harness": "aegis", "session_id": "s", "cwd": "/x", "event": "stop",
                    "ts": sc._mint_ts(), "turns": 1, "window_id": "window", "offset_before": 0,
                    "offset_after": 10, "truncated_chars": 0}
            (q / "a.json").write_text(json.dumps({**base, "part": "2/2", "transcript": "USER: second"}))
            (q / "b.json").write_text(json.dumps({**base, "part": "1/2", "transcript": "USER: first"}))
            effects = []
            first_open = threading.Event()
            close_first = threading.Event()

            def capture(payload, mode=None):
                part = payload["part"]
                effects.append(f"open:{part}")
                if part == "1/2":
                    first_open.set()
                    self.assertTrue(close_first.wait(2))
                effects.append(f"close:{part}")
                return 0

            with mock.patch("khipu.extract.extract_memory", lambda text, *, cwd="", **kw: {
                    "summary": text, "topics": [], "decisions": [], "preferences": [], "scope": "", "edges": [], "topic_pages": []}), \
                    mock.patch("khipu.capture.capture", capture), \
                    mock.patch("khipu.config.capture_mode", lambda: "hub"):
                first = threading.Thread(target=lambda: sc.drain(limit=1))
                first.start()
                self.assertTrue(first_open.wait(2))
                second = threading.Thread(target=lambda: sc.drain(limit=1))
                second.start()
                second.join(2)
                self.assertFalse(second.is_alive())
                self.assertNotIn("open:2/2", effects)
                close_first.set()
                first.join(2)
                self.assertFalse(first.is_alive())
                sc.drain(limit=1)
            self.assertEqual(effects, ["open:1/2", "close:1/2", "open:2/2", "close:2/2"])

    def test_claimed_job_counts_as_stale_queue_work(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"KHIPU_CAPTURE_HOME": str(Path(td) / "kh")}):
            q = sc.queue_dir()
            q.mkdir(parents=True)
            claim = q / f"job.claimed.{os.getpid()}"
            claim.write_text(json.dumps({"harness": "codex", "session_id": "s"}))
            old = time.time() - sc.QUEUE_STALE_S - 1
            os.utime(claim, (old, old))
            sc._heartbeat("codex", {"at": sc._mint_ts(), "event": "stop", "session_id": "s", "due": False})
            self.assertEqual(sc.status("codex")["queue_depth"], 1)
            self.assertEqual(sc.status("codex")["claimed_depth"], 1)
            self.assertFalse(sc.liveness("codex")["ok"])

    def test_hook_and_drain_heartbeat_updates_keep_counters_and_error(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"KHIPU_CAPTURE_HOME": str(Path(td) / "kh")}):
            started = threading.Barrier(3)
            def hook():
                started.wait()
                sc._heartbeat("codex", {"at": sc._mint_ts(), "event": "stop",
                                                        "session_id": "s", "due": False})
            def drain():
                started.wait()
                sc._record_drain("codex", captured=False, error="provider down")
            with sc._beat_lock("codex") as acquired:
                self.assertTrue(acquired)
                threads = [threading.Thread(target=hook), threading.Thread(target=drain)]
                [thread.start() for thread in threads]
                started.wait()
                self.assertTrue(all(thread.is_alive() for thread in threads))
            [thread.join(2) for thread in threads]
            beat = sc.last_dispatch("codex")
            self.assertEqual(beat["dispatches"], 1)
            self.assertEqual(beat["last_drain_error"], "provider down")

    def test_dead_claim_is_visible_at_once_and_unhealthy_once_nothing_reclaims_it(self):
        """CAP-17: claimed work must never vanish from health. A claim whose
        owner just died is counted at once, but it is the normal hand-over to
        the next drain (every hook's drain is killed with its hook), so it is
        red only after DEAD_CLAIM_GRACE_S with no reclaim. Red at once showed
        "stopped recording" in the app many times a day (2026-10-11)."""
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"KHIPU_CAPTURE_HOME": str(Path(td) / "kh")}):
            q = sc.queue_dir()
            q.mkdir(parents=True)
            claim = q / "fresh.claimed.99999999"
            claim.write_text(json.dumps({"harness": "codex", "session_id": "s"}))
            sc._heartbeat("codex", {"at": sc._mint_ts(), "event": "stop", "session_id": "s", "due": False})
            sc._record_drain("codex", captured=True)
            with mock.patch.object(sc, "_claimant_gone", return_value=True):
                fresh = sc.liveness("codex")
                status = sc.status("codex")
                with mock.patch.object(sc, "DEAD_CLAIM_GRACE_S", 0):
                    health = sc.liveness("codex")
            self.assertTrue(fresh["ok"])
            self.assertEqual((status["queue_depth"], status["claimed_depth"], status["dead_claims"]), (1, 1, 1))
            self.assertFalse(health["ok"])
            self.assertTrue(any("nothing is reclaiming" in reason for reason in health["reasons"]))

    def test_scalar_and_malformed_queue_jobs_preserve_bytes_and_do_not_starve(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"KHIPU_CAPTURE_HOME": str(Path(td) / "kh")}):
            q = sc.queue_dir()
            q.mkdir(parents=True)
            scalar = q / "scalar.json"
            scalar.write_text("[]")
            self.assertEqual(sc.status("aegis")["queue_depth"], 1)
            malformed = q / "malformed.json"
            malformed.write_text("{broken")
            healthy = sc.enqueue({"ts": sc._mint_ts(), "harness": "codex", "session_id": "healthy",
                                  "transcript": "USER: retain this synthetic decision"})
            with mock.patch("khipu.config.capture_mode", return_value="hub"), \
                    mock.patch("khipu.extract.extract_memory", return_value={"summary": "healthy synthetic decision"}), \
                    mock.patch("khipu.capture.capture", return_value=0) as capture, \
                    mock.patch("khipu.hub_snapshot.sync_decision_changes", return_value={}):
                reports = [sc.drain(limit=1) for _ in range(3)]
            self.assertEqual(sum(r["failed"] for r in reports), 2)
            self.assertEqual(sum(r["quarantined"] for r in reports if "quarantined" in r), 2)
            self.assertFalse(healthy.exists())
            capture.assert_called_once()
            self.assertEqual(sc.queued_jobs(), [])
            self.assertEqual((sc.empty_dir() / "scalar.json").read_bytes(), b"[]")
            self.assertEqual((sc.empty_dir() / "malformed.json").read_bytes(), b"{broken")
            for name in ("scalar", "malformed"):
                reason = json.loads((sc.empty_dir() / f"{name}.error").read_text())
                self.assertEqual(reason["disposition"], "malformed")
                self.assertTrue(reason["reason"])

    def test_default_hook_drains_and_embeds_unless_queue_only(self):
        repo = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as td:
            transcript = _write(Path(td) / "session.jsonl", [
                {"type": "response_item", "payload": {"type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "retain " + "x" * 220}]}},
            ])
            env = {"HOME": td, "KHIPU_ROOT": str(repo), "KHIPU_PYTHON": sys.executable,
                   "KHIPU_CAPTURE_HOME": str(Path(td) / "real-queue"), "PATH": os.environ["PATH"],
                   "KHIPU_HARNESS": "codex", "KHIPU_CAPTURE_QUEUE_ONLY": "1"}
            payload = json.dumps({"hookEventName": "SessionEnd", "session_id": "real", "cwd": td,
                                  "transcript_path": str(transcript)})
            fixed = subprocess.run(["sh", str(repo / "packages/cli/bin/khipu-stop-hook")], input=payload,
                                   text=True, env=env, capture_output=True, timeout=5, cwd=td)
            self.assertEqual(fixed.returncode, 0)
            jobs = list((Path(env["KHIPU_CAPTURE_HOME"]) / "queue").glob("*.json"))
            self.assertEqual(len(jobs), 1, "the production enqueue path must durably queue")

            root = Path(td) / "fake-root"
            package = root / "packages" / "cli" / "khipu"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("")
            (package / "session_capture.py").write_text(
                "import os, time\n"
                "def hook_main(*args):\n"
                " open(os.environ['ENQUEUE_MARKER'], 'w').write('queued')\n"
                " return {'harness':'codex','event':'stop','due':True,'session_id':'s'}\n"
                "from pathlib import Path\n"
                "def queue_dir(): return Path(os.environ['KHIPU_CAPTURE_HOME']) / 'queue'\n"
                "def queued_jobs(): return [] if os.path.exists(os.environ['DRAIN_MARKER']) else [object()]\n"
                "def drain(): open(os.environ['DRAIN_MARKER'], 'w').write('called'); time.sleep(1); return {'captured': 1}\n"
            )
            # The hook's post-enqueue work lives in khipu.hook_background; the fake
            # root runs the real module against the fake session_capture/embed.
            shutil.copy(repo / "packages/cli/khipu/hook_background.py", package / "hook_background.py")
            (package / "embed.py").write_text(
                "import os\n"
                "def embed_recent_missing(limit):\n"
                " open(os.environ['EMBED_MARKER'], 'w').write(str(limit))\n"
                " return {'embedded': 1}\n"
            )
            env = {"HOME": td, "KHIPU_ROOT": str(root), "KHIPU_PYTHON": sys.executable,
                   "KHIPU_CAPTURE_HOME": str(Path(td) / "queue"), "PATH": os.environ["PATH"],
                   "KHIPU_HARNESS": "codex", "ENQUEUE_MARKER": str(Path(td) / "enqueue-marker"),
                   "DRAIN_MARKER": str(Path(td) / "drain-marker"),
                   "EMBED_MARKER": str(Path(td) / "embed-marker"),
                   "KHIPU_HOOK_INLINE": "1"}
            payload = json.dumps({"hookEventName": "Stop", "session_id": "s"})
            fixed = subprocess.run(["sh", str(repo / "packages/cli/bin/khipu-stop-hook")], input=payload,
                                   text=True, env=env, capture_output=True, timeout=5, cwd=td)
            self.assertEqual(fixed.returncode, 0)
            self.assertTrue(Path(env["ENQUEUE_MARKER"]).exists())
            self.assertTrue(Path(env["DRAIN_MARKER"]).exists(), "default hook must deliver without launchd")
            self.assertEqual(Path(env["EMBED_MARKER"]).read_text(), "5")
            Path(env["DRAIN_MARKER"]).unlink()
            Path(env["EMBED_MARKER"]).unlink()
            env["KHIPU_CAPTURE_QUEUE_ONLY"] = "1"
            queued = subprocess.run(["sh", str(repo / "packages/cli/bin/khipu-stop-hook")], input=payload,
                                    text=True, env=env, capture_output=True, timeout=5, cwd=td)
            self.assertEqual(queued.returncode, 0)
            self.assertFalse(Path(env["DRAIN_MARKER"]).exists())
            self.assertFalse(Path(env["EMBED_MARKER"]).exists())

            (package / "session_capture.py").write_text(
                "import os, time\n"
                "def hook_main(*args): time.sleep(1); open(os.environ['LATE_MARKER'], 'w').write('late')\n"
            )
            env["KHIPU_CAPTURE_ENQUEUE_TIMEOUT_S"] = "0.05"
            env["LATE_MARKER"] = str(Path(td) / "late-marker")
            bounded = subprocess.run(["sh", str(repo / "packages/cli/bin/khipu-stop-hook")], input=payload,
                                     text=True, env=env, capture_output=True, timeout=5, cwd=td)
            self.assertEqual(bounded.returncode, 0)
            self.assertFalse(Path(env["LATE_MARKER"]).exists(), "enqueue exceeded its bounded hook budget")


class _Cursor:
    def __init__(self, row): self.row, self.statements, self.params = row, [], []
    def execute(self, statement, params=()):
        self.statements.append(statement)
        self.params.append(params)
    def fetchone(self): return self.row


class _Conn:
    def __init__(self, cur): self.cur = cur
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def cursor(self): return self.cur


class _Cursor(_Cursor):
    def __enter__(self): return self
    def __exit__(self, *args): return False


class TopicDetailCapaTest(unittest.TestCase):
    def test_topic_body_query_and_response_are_utf8_bounded(self):
        # The database hands back at most max_bytes + 4 bytes from the offset;
        # the page ends on a character boundary and says where the next starts.
        raw = "ééé".encode("utf-8")
        cur = _Cursor(("t", "T", raw[:9], len(raw), "active", None, None, [], "/t.md", None))
        with mock.patch.object(activity, "connect", return_value=_Conn(cur)):
            out = activity.topic_detail("t", max_bytes=5)
        self.assertEqual(out["body"], "éé")
        self.assertEqual((out["body_offset"], out["body_next_offset"]), (0, 4))
        self.assertEqual((out["body_total_bytes"], out["complete"]), (6, False))
        self.assertNotIn("body_truncated", out)
        self.assertIn("convert_to(t.body, 'UTF8')", cur.statements[0])
        self.assertNotIn("LEFT(body", cur.statements[0])
        self.assertEqual(cur.params[0], (1, 9, "t"))

