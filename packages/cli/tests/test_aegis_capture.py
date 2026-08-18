"""Tests for khipu.aegis_capture — Aegis's sandboxed capture hook + the drain.

No model calls: these cover the ACP transcript reader, the cadence, per-session
offset state, the queue, the heartbeat, and — most importantly — the sandbox
contract. Aegis runs hooks in a macOS sandbox where ~/Library, ~/.config and the
legacy Memory tree are denied; the first version of this module wrote to all
three and failed silently inside real sessions. `SandboxContractTest` is the
regression guard for that: it runs the shipped script with those paths made
unwritable and requires the hook to still do its job.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import aegis_capture as ac

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "bin" / "khipu-aegis-capture"


def _row(kind: str, text: str | None = None, **extra) -> str:
    u: dict = {"sessionUpdate": kind, **extra}
    if text is not None:
        u["content"] = {"type": "text", "text": text}
    return json.dumps({"method": "session/update", "params": {"sessionId": "s", "update": u}}) + "\n"


def _transcript(dirpath: Path, turns: int = 1) -> Path:
    p = dirpath / "updates.jsonl"
    body = ""
    for i in range(turns):
        body += _row("user_message_chunk", f"question {i} " + "x" * 200)
        body += _row("agent_message_chunk", f"answer {i} " + "y" * 200)
    p.write_text(body)
    return p


class ReaderTest(unittest.TestCase):
    def test_coalesces_chunks_marks_tools_drops_thoughts_and_counts_users(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "updates.jsonl"
            p.write_text(
                _row("user_message_chunk", "hello ")
                + _row("user_message_chunk", "world")
                + _row("agent_thought_chunk", "thinking...")
                + _row("agent_message_chunk", "hi ")
                + _row("agent_message_chunk", "there")
                + _row("tool_call", title="grep foo", toolCallId="c1")
                + _row("tool_call_update", toolCallId="c1", status="completed")
                + _row("user_message_chunk", "second turn")
            )
            msgs, off, users = ac.read_window(p, 0)
            self.assertEqual(msgs, [("user", "hello world"), ("assistant", "hi there"),
                                    ("tool", "grep foo"), ("user", "second turn")])
            self.assertEqual(users, 2)
            self.assertEqual(off, p.stat().st_size)
            self.assertEqual(ac.read_window(p, off)[0], [])          # incremental
            with p.open("a") as f:
                f.write(_row("user_message_chunk", "third").rstrip("\n"))
            msgs2, off2, _ = ac.read_window(p, off)                  # partial line deferred
            self.assertEqual((msgs2, off2), ([], off))
            r = ac.render(msgs)
            self.assertIn("USER: hello world", r)
            self.assertIn("[tool] grep foo", r)

    def test_truncated_transcript_restarts_instead_of_seeking_past_eof(self):
        with tempfile.TemporaryDirectory() as td:
            p = _transcript(Path(td), turns=1)
            _, off, _ = ac.read_window(p, 0)
            p.write_text(_row("user_message_chunk", "fresh " + "z" * 200))
            msgs, _, users = ac.read_window(p, off)
            self.assertEqual(users, 1)
            self.assertTrue(msgs)


class CadenceTest(unittest.TestCase):
    def test_rules(self):
        d = ac.decide
        self.assertFalse(d("stop", user_turns=1, chars=5000, elapsed_s=0, stop_hook_active=True)[0])
        self.assertFalse(d("stop", user_turns=0, chars=5000, elapsed_s=99999, stop_hook_active=False)[0])
        self.assertFalse(d("stop", user_turns=3, chars=50, elapsed_s=99999, stop_hook_active=False)[0])
        self.assertFalse(d("stop", user_turns=1, chars=5000, elapsed_s=60, stop_hook_active=False)[0])
        self.assertTrue(d("stop", user_turns=ac.MIN_TURNS, chars=5000, elapsed_s=0, stop_hook_active=False)[0])
        self.assertTrue(d("stop", user_turns=1, chars=5000, elapsed_s=ac.MIN_MINUTES * 60 + 1,
                          stop_hook_active=False)[0])
        self.assertTrue(d("precompact", user_turns=1, chars=5000, elapsed_s=0, stop_hook_active=False)[0])
        self.assertTrue(d("sessionend", user_turns=1, chars=5000, elapsed_s=0, stop_hook_active=False)[0])
        self.assertFalse(d("posttooluse", user_turns=9, chars=5000, elapsed_s=99999, stop_hook_active=False)[0])
        self.assertEqual(ac.norm_event("pre_compact"), "precompact")
        self.assertEqual(ac.norm_event("SessionEnd"), "sessionend")


class HookQueueTest(unittest.TestCase):
    def _home(self, td):
        return mock.patch.dict(os.environ, {"KHIPU_AEGIS_HOME": str(Path(td) / "kh")})

    def test_first_sight_starts_the_clock_then_precompact_queues_once(self):
        with tempfile.TemporaryDirectory() as td, self._home(td):
            tp = _transcript(Path(td))
            env = {"hookEventName": "stop", "sessionId": "abc", "cwd": td, "transcriptPath": str(tp)}
            out = ac.hook_main(json.dumps(env))
            self.assertFalse(out["due"], out)                 # clock just started
            self.assertEqual(ac.queued_jobs(), [])
            env["hookEventName"] = "pre_compact"
            out = ac.hook_main(json.dumps(env))
            self.assertTrue(out["due"], out)
            jobs = ac.queued_jobs()
            self.assertEqual(len(jobs), 1)
            job = json.loads(jobs[0].read_text())
            self.assertEqual(job["session_id"], "abc")
            self.assertIn("question 0", job["transcript"])
            self.assertTrue(job["ts"].endswith("Z"))
            # Window consumed: a second PreCompact with no new turns queues nothing.
            out2 = ac.hook_main(json.dumps(env))
            self.assertFalse(out2["due"], out2)
            self.assertEqual(len(ac.queued_jobs()), 1)

    def test_heartbeat_records_every_invocation_even_when_not_due(self):
        with tempfile.TemporaryDirectory() as td, self._home(td):
            tp = _transcript(Path(td))
            ac.hook_main(json.dumps({"hookEventName": "stop", "sessionId": "hb",
                                     "cwd": td, "transcriptPath": str(tp)}), "aegis")
            beat = ac.last_dispatch("aegis")
            self.assertIsNotNone(beat)
            self.assertEqual(beat["event"], "stop")
            self.assertFalse(beat["due"])
            self.assertIn("at", beat)
            self.assertNotIn("transcript", json.dumps(beat))       # never log the content
            self.assertEqual(ac.status("aegis")["queue_depth"], 0)

    def test_hook_never_raises_on_garbage_or_missing_transcript(self):
        with tempfile.TemporaryDirectory() as td, self._home(td):
            for raw in ("", "not json", "[]", '{"hookEventName":"stop"}',
                        '{"hookEventName":"stop","sessionId":"x","transcriptPath":"/nope"}'):
                out = ac.hook_main(raw)
                self.assertFalse(out.get("due"))


class SandboxContractTest(unittest.TestCase):
    """Aegis runs hooks sandboxed: ~/Library, ~/.config and the Memory tree are
    denied. The shipped script must still queue. Emulated by pointing HOME at a
    directory whose Library/.config are read-only — the exact failure that made
    the first version a silent no-op in every real Aegis session."""

    def test_launcher_queues_with_library_and_config_unwritable(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            for sub in ("Library/Logs", ".config"):
                (home / sub).mkdir(parents=True)
            khome = home / ".grok" / "khipu"
            for sub in ("Library", ".config"):
                os.chmod(home / sub, 0o500)                        # deny writes, like the sandbox
            try:
                tp = _transcript(Path(td))
                env = json.dumps({"hookEventName": "session_end", "sessionId": "sbx",
                                  "cwd": td, "transcriptPath": str(tp)})
                r = subprocess.run([str(LAUNCHER)], input=env, capture_output=True, text=True,
                                   timeout=60,
                                   env=dict(os.environ, HOME=str(home), KHIPU_AEGIS_PROBE="1",
                                            KHIPU_AEGIS_HOME=str(khome), KHIPU_HARNESS="aegis"))
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertTrue(json.loads(r.stdout)["due"], r.stdout)
                self.assertEqual(len(list((khome / "queue").glob("*.json"))), 1)
                self.assertTrue((khome / "dispatch" / "aegis.json").is_file())
                self.assertFalse((home / "Library" / "Logs" / "khipu").exists())
            finally:
                for sub in ("Library", ".config"):
                    os.chmod(home / sub, 0o700)

    def test_isolation_native_runs_compat_refuses(self):
        """Aegis is its own harness: the pack marks handlers KHIPU_HARNESS=aegis;
        reached via a vendor-compat import of another harness's config, refuse."""
        with tempfile.TemporaryDirectory() as td:
            tp = _transcript(Path(td))
            env = json.dumps({"hookEventName": "session_end", "sessionId": "iso",
                              "cwd": td, "transcriptPath": str(tp)})
            base = {k: v for k, v in os.environ.items() if k != "KHIPU_HARNESS"}
            base.update(GROK_HOOK_NAME="global/settings:stop[0].hooks[0]", GROK_HOOK_EVENT="stop",
                        KHIPU_AEGIS_PROBE="1")
            for label, extra in (("native", {"KHIPU_HARNESS": "aegis"}), ("compat", {})):
                khome = Path(td) / f"kh-{label}"
                r = subprocess.run([str(LAUNCHER)], input=env, capture_output=True, text=True,
                                   timeout=60, env=dict(base, KHIPU_AEGIS_HOME=str(khome), **extra))
                self.assertEqual(r.returncode, 0)
                queued = list((khome / "queue").glob("*.json")) if khome.is_dir() else []
                if label == "native":
                    self.assertEqual(len(queued), 1, "pack invocation must capture")
                else:
                    self.assertEqual(queued, [], "compat invocation must do nothing")


class DrainTest(unittest.TestCase):
    def test_drain_captures_drops_empty_and_keeps_failures(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.dict(os.environ, {"KHIPU_AEGIS_HOME": str(Path(td) / "kh")}):
            for sid in ("good", "empty", "boom"):
                ac.enqueue({"session_id": sid, "cwd": "/x", "event": "stop", "ts": ac._mint_ts(),
                            "turns": 1, "transcript": f"USER: {sid}", "offset_after": 0})
            self.assertEqual(len(ac.queued_jobs()), 3)

            def fake_extract(transcript, *, cwd=""):
                if "empty" in transcript:
                    return None
                if "boom" in transcript:
                    raise RuntimeError("model down")
                return {"summary": "s", "topics": [], "decisions": [], "preferences": [],
                        "scope": "", "edges": [], "topic_pages": []}

            captured = []
            with mock.patch("khipu.extract.extract_memory", fake_extract), \
                    mock.patch("khipu.capture.capture", lambda p, mode=None: captured.append(p) or 0), \
                    mock.patch("khipu.config.capture_mode", lambda: "dual"):
                out = ac.drain()
            self.assertEqual((out["captured"], out["empty"], out["failed"]), (1, 1, 1))
            self.assertEqual(len(ac.queued_jobs()), 1)             # the failure is retried later
            self.assertEqual(captured[0]["session_id"], "aegis:good")
            self.assertTrue(captured[0]["ts"].endswith("Z"))

    def test_concurrent_drains_capture_a_job_exactly_once(self):
        """Every unsandboxed Stop hook, the nightly, the app and the CLI can drain
        at once, and each runs its OWN model extraction — two winners would be two
        different summaries, i.e. two episodes no identity upsert can dedup
        (audit 2026-08-17: reproduced before the atomic claim)."""
        import threading
        import time as _t
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.dict(os.environ, {"KHIPU_AEGIS_HOME": str(Path(td) / "kh")}):
            ac.enqueue({"session_id": "c", "cwd": "/x", "event": "stop", "ts": ac._mint_ts(),
                        "turns": 1, "transcript": "USER: durable", "offset_after": 0})
            captured, results = [], []

            def slow(t, *, cwd=""):
                _t.sleep(0.4)
                return {"summary": f"s{_t.time()}", "topics": [], "decisions": [],
                        "preferences": [], "scope": "", "edges": [], "topic_pages": []}

            with mock.patch("khipu.extract.extract_memory", slow), \
                    mock.patch("khipu.capture.capture", lambda p, mode=None: captured.append(p) or 0), \
                    mock.patch("khipu.config.capture_mode", lambda: "dual"):
                threads = [threading.Thread(target=lambda: results.append(ac.drain())) for _ in range(3)]
                [t.start() for t in threads]
                [t.join() for t in threads]
            self.assertEqual(len(captured), 1, "one job must produce exactly one episode")
            self.assertEqual(ac.queued_jobs(), [])
            # The losers either lost the claim race or found the queue already
            # empty; either way none of them captured.
            self.assertEqual(sum(r["captured"] for r in results), 1)

    def test_a_failed_drain_releases_the_claim_for_retry(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.dict(os.environ, {"KHIPU_AEGIS_HOME": str(Path(td) / "kh")}):
            ac.enqueue({"session_id": "r", "cwd": "/x", "event": "stop", "ts": ac._mint_ts(),
                        "turns": 1, "transcript": "USER: durable", "offset_after": 0})
            with mock.patch("khipu.extract.extract_memory", side_effect=RuntimeError("model down")):
                out = ac.drain()
                self.assertEqual(out["failed"], 1)
                self.assertEqual(len(ac.queued_jobs()), 1)      # back in the queue, not stuck claimed
                self.assertEqual(ac.drain()["jobs"], 1)         # a later drain still sees it
            # NB: every drain in a test stays inside the mock — one outside it
            # made a real Gemini call and wrote a real episode (audit 2026-08-17).

    def test_drain_is_a_noop_with_an_empty_queue(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.dict(os.environ, {"KHIPU_AEGIS_HOME": str(Path(td) / "kh")}):
            self.assertEqual(ac.drain(), {"jobs": 0, "captured": 0, "empty": 0, "failed": 0,
                                          "skipped_claimed": 0})


class ExtractParseTest(unittest.TestCase):
    def test_model_json_tolerance_and_slugs(self):
        from khipu.extract import parse_model_json, slugify

        self.assertEqual(parse_model_json('```json\n{"summary": "x"}\n```')["summary"], "x")
        self.assertIsNone(parse_model_json("no json here"))
        self.assertEqual(slugify("Stapler History!"), "stapler-history")
        self.assertEqual(slugify("  Khipu -- P3 "), "khipu-p3")


if __name__ == "__main__":
    unittest.main()
