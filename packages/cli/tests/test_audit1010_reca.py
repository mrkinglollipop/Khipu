"""Hermetic regressions for the recall audit; no hub or provider calls."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from khipu import deliverables, query_log as ql, recall_daemon as rd, recall_prompt as rp
from tests.test_prompt_lane_provisional import _lane
from tests.test_recall_client import CLIENT, LAUNCHER, _short_dir

PROMPT = "recall hook decision"
HIT = {"kind": "episode", "id": "5", "score": 0.9, "snippet": PROMPT}
RESULT = {"hits": [HIT], "legs": ["lexical"], "degraded": None}


class RecallAuditTest(unittest.TestCase):
    def setUp(self):
        self.home = _short_dir()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        patch = mock.patch("khipu.session_capture.khipu_home", return_value=self.home)
        patch.start()
        self.addCleanup(patch.stop)

    def test_rv3_dedup_failure_preserves_completed_context(self):
        with mock.patch.object(rp, "_search_hits", return_value=RESULT), \
             mock.patch.object(rp, "_deliverable_context_line", return_value=""), \
             mock.patch.object(rp, "_dedup_transaction", side_effect=OSError("busy")):
            out = rp.prior_work_for_prompt(PROMPT, session_id="busy")
        self.assertTrue(out["context"])
        self.assertEqual(out["hits"], [HIT])

    def test_rv3_distinct_batch_survives_unacknowledged_delivery(self):
        pending = {}
        other = {**HIT, "id": "6"}
        try:
            rp._dedup_transaction("busy", [HIT], pending)
            with mock.patch.object(rp, "_search_hits", return_value={**RESULT, "hits": [other]}), \
                 mock.patch.object(rp, "_deliverable_context_line", return_value=""):
                out = rp.prior_work_for_prompt(PROMPT, session_id="busy")
            self.assertEqual(out["hits"], [other])
        finally:
            pending["finish"](False)

    def test_rv3_recall_import_without_fcntl(self):
        import builtins
        original = builtins.__import__
        def guarded(name, *args, **kwargs):
            if name == "fcntl":
                raise ImportError("unavailable")
            return original(name, *args, **kwargs)
        spec = importlib.util.spec_from_file_location("recall_without_fcntl", rp.__file__)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.object(builtins, "__import__", side_effect=guarded):
            spec.loader.exec_module(module)
        with self.assertRaises(OSError):
            module._dedup_transaction("no-lock-backend", [HIT], None)

    def test_handler_failure_is_not_successful_empty_recall(self):
        svc = rd.RecallService(self.home / "recall.sock", stamp_fn=lambda: "same")
        with mock.patch.object(rp, "hook_main", side_effect=RuntimeError("broken handler")):
            out = svc._answer(b'{"op":"recall","raw":"{}","protocol":1}')
        self.assertIs(out.get("ok"), False)
        self.assertIn("error", out)
        self.assertEqual(rp.prompt_recall_outcomes()["count"], 1)

    def test_empty_recall_is_an_explicit_success(self):
        svc = rd.RecallService(self.home / "recall.sock", stamp_fn=lambda: "same")
        with mock.patch.object(rp, "hook_main", return_value={}):
            out = svc._answer(b'{"op":"recall","raw":"{}","protocol":1}')
        self.assertEqual(out, {"ok": True, "output": {}})

    def _client(self, reply):
        spec = importlib.util.spec_from_file_location("reca_client", CLIENT)
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        stdout = io.StringIO()
        sock = mock.Mock()
        sock.recv.return_value = reply
        with mock.patch.object(client.socket, "socket", return_value=sock), \
                mock.patch.object(client.sys, "stdin", mock.Mock(buffer=io.BytesIO(b"{}"))), \
                mock.patch.object(client.sys, "stdout", stdout):
            code = client.main([str(CLIENT)])
        return mock.Mock(returncode=code, stdout=stdout.getvalue().encode())

    def test_immediate_daemon_error_selects_fallback(self):
        done = self._client(b'{"ok":false,"error":"restarting"}\n')
        self.assertEqual(done.returncode, 3)
        self.assertGreater(float(done.stdout), time.monotonic())

    def test_malformed_daemon_answer_selects_fallback(self):
        done = self._client(b'garbage\n')
        self.assertEqual(done.returncode, 3)
        self.assertGreater(float(done.stdout), time.monotonic())

    def test_no_match_envelope_reaches_hook_without_protocol_fields(self):
        done = self._client(b'{"ok":true,"output":{}}\n')
        self.assertEqual((done.returncode, done.stdout), (0, b"{}\n"))

    def test_search_and_deliverable_share_budget_and_complete_latency(self):
        def search(*_a, **_k):
            time.sleep(0.15)
            return RESULT
        def produced(*_a, **_k):
            time.sleep(0.3)
            return "You produced recall.py"
        with mock.patch.object(rp, "TIMEOUT_S", 0.3), \
                mock.patch.object(rp, "_search_hits", side_effect=search), \
                mock.patch.object(rp, "_deliverable_context_line", side_effect=produced):
            start = time.monotonic()
            out = rp.prior_work_for_prompt(PROMPT, session_id="s")
            elapsed = time.monotonic() - start
        self.assertNotIn("You produced", out["context"])
        self.assertGreaterEqual(out["ms"], elapsed * 1000 - 30)
        self.assertLess(elapsed, 0.5)

    def test_unacknowledged_daemon_batch_is_not_deduplicated(self):
        svc = rd.RecallService(self.home / "recall.sock", stamp_fn=lambda: "same")
        raw = json.dumps({"prompt": PROMPT, "session_id": "unseen"})
        sock = mock.Mock()
        request = {"op": "recall", "raw": raw, "protocol": 1, "deadline": time.monotonic() + 0.5}
        sock.recv.side_effect = [json.dumps(request).encode() + b"\n", b"", b""]
        with mock.patch.object(rp, "_search_hits", return_value=RESULT), \
                mock.patch.object(rp, "_deliverable_context_line", return_value=""):
            svc._serve_conn(sock)
            self.assertEqual(rp._load_recent_batches("unseen"), [])
            out = rp.prior_work_for_prompt(PROMPT, session_id="unseen")
        self.assertTrue(out["context"])

    def test_expired_request_never_calls_recall(self):
        svc = rd.RecallService(self.home / "recall.sock", stamp_fn=lambda: "same")
        with mock.patch.object(rp, "hook_main", return_value={}) as hook:
            out = svc._answer(json.dumps({"op": "recall", "raw": "{}", "protocol": 1,
                                         "deadline": time.monotonic() - 1}).encode())
        hook.assert_not_called()
        self.assertIs(out.get("ok"), False)

    def test_all_errors_make_recall_health_unhealthy(self):
        for _ in range(25):
            rp._log("session=s reason=error: hub down ms=1 hits=[] legs=[] degraded=None")
        out = rp.prompt_recall_outcomes()
        self.assertFalse(out["ok"])
        self.assertEqual(out.get("error_rate"), 1.0)

    def test_crashes_record_terminal_outcomes(self):
        with mock.patch.object(rp, "hook_main", side_effect=RuntimeError("boom")), \
                mock.patch("sys.stdin", io.StringIO("{}")), mock.patch("sys.stdout", io.StringIO()):
            rp.prompt_recall_main()
        self.assertEqual(rp.prompt_recall_outcomes()["count"], 1)

    def test_missing_and_unreadable_recall_evidence_is_not_green(self):
        self.assertFalse(rp.prompt_recall_outcomes()["ok"])
        with mock.patch.object(rp, "_tail_text", side_effect=PermissionError("denied")):
            rp._log("session=s reason=ok ms=1 hits=[]")
            out = rp.prompt_recall_outcomes()
        self.assertFalse(out["ok"])
        self.assertIs(out.get("available"), False)

    def test_gate_errors_are_failures_not_deliberate_gating(self):
        with mock.patch("khipu.search_text.search_tokens", side_effect=RuntimeError("bad gate")):
            out = rp.prior_work_for_prompt(PROMPT, budget_ms=100)
        self.assertEqual(out["prior_work_meta"]["outcome"], "error")

    def test_connected_listener_that_never_answers_is_unhealthy(self):
        sock = mock.Mock()
        sock.recv.side_effect = TimeoutError("listener never responded")
        with mock.patch.object(rd.socket, "socket", return_value=sock):
            answer = rd.ping(self.home / "recall.sock", timeout=0.05)
        with mock.patch.object(rd, "ping", return_value=answer):
            out = rd.daemon_health()
        self.assertFalse(out["ok"])
        self.assertTrue(out["running"])

    def test_both_failed_hub_legs_report_error(self):
        with mock.patch.object(rp, "_snapshot_search_hits", side_effect=rp._SnapshotUnusable("missing")), \
                mock.patch("khipu.db.connect", side_effect=RuntimeError("offline")), \
                mock.patch("khipu.embed._cosine_candidates", side_effect=RuntimeError("offline")), \
                mock.patch.object(rp, "_deliverable_context_line", return_value=""):
            out = rp.prior_work_for_prompt(PROMPT, budget_ms=100)
        self.assertEqual(out["prior_work_meta"]["outcome"], "error")

    def test_both_late_hub_legs_report_timeout(self):
        release = threading.Event()
        def late(*_a, **_k):
            release.wait(2)
            raise RuntimeError("late")
        try:
            with mock.patch.object(rp, "_snapshot_search_hits", side_effect=rp._SnapshotUnusable("missing")), \
                    mock.patch("khipu.db.connect", side_effect=late), \
                    mock.patch("khipu.embed._cosine_candidates", side_effect=late), \
                    mock.patch.object(rp, "_deliverable_context_line", return_value=""):
                out = rp.prior_work_for_prompt(PROMPT, budget_ms=50)
        finally:
            release.set()
        self.assertEqual(out["prior_work_meta"]["outcome"], "timeout")

    def test_budgeted_snapshot_preserves_completed_lexical_hits(self):
        release = threading.Event()
        def embed(*_a, **_k):
            release.wait(2)
            return [1.0]
        try:
            with _lane(embed=embed), mock.patch.object(rp, "LOCAL_LANE_DEADLINE_S", 0.95), \
                    mock.patch.object(rp, "_deliverable_context_line", return_value=""):
                out = rp.prior_work_for_prompt(PROMPT, budget_ms=50)
        finally:
            release.set()
        self.assertEqual([h["id"] for h in out["hits"]], ["5"])
        self.assertEqual(out["prior_work_meta"]["outcome"], "match")

    def test_detected_stale_code_is_refused_before_dispatch(self):
        stamp = ["old"]
        svc = rd.RecallService(self.home / "recall.sock", stamp_fn=lambda: stamp[0], recheck_s=0)
        stamp[0] = "new"
        with mock.patch.object(rp, "hook_main", return_value={}) as hook:
            out = svc._answer(b'{"op":"recall","raw":"{}","protocol":1}')
        hook.assert_not_called()
        self.assertIn("restarting", out["error"])

    def test_stamp_detects_nonnewest_content_change_with_preserved_mtime(self):
        root = self.home / "code"
        root.mkdir()
        older, newer = root / "a.py", root / "b.py"
        older.write_text("old = 1")
        newer.write_text("new = 2")
        os.utime(older, ns=(100, 100))
        os.utime(newer, ns=(200, 200))
        with mock.patch.object(rd, "__file__", str(root / "recall_daemon.py")):
            before = rd.code_stamp()
            older.write_text("old = 9")
            os.utime(older, ns=(100, 100))
            after = rd.code_stamp()
        self.assertNotEqual(before, after)

    def test_concurrent_identical_batches_preserve_successful_recall(self):
        barrier = threading.Barrier(2)
        def search(*_a, **_k):
            barrier.wait(2)
            return RESULT
        original = rp._save_recent_batches
        def save(*args):
            time.sleep(0.05)
            original(*args)
        answers = []
        with mock.patch.object(rp, "_search_hits", side_effect=search), \
                mock.patch.object(rp, "_deliverable_context_line", return_value=""), \
                mock.patch.object(rp, "_save_recent_batches", side_effect=save):
            threads = [threading.Thread(target=lambda: answers.append(
                rp.prior_work_for_prompt(PROMPT, session_id="race"))) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(3)
        self.assertEqual(sum(bool(a["context"]) for a in answers), 2)

    def test_validity_restore_is_recalled_again(self):
        outputs = []
        with mock.patch.object(rp, "_deliverable_context_line", return_value=""):
            for state in ("current", "superseded", "current"):
                hit = {**HIT, "validity": {"state": state}}
                with mock.patch.object(rp, "_search_hits", return_value={**RESULT, "hits": [hit]}):
                    outputs.append(rp.prior_work_for_prompt(PROMPT, session_id="restore"))
        self.assertTrue(all(o["context"] for o in outputs))

    def test_long_deliverable_only_context_is_framed_and_bounded(self):
        line = deliverables.format_produced_line({"path": "recall/decision/" + "x" * 1000,
                                                  "episode_id": 5, "created_at": "2026-10-10"})
        with mock.patch.object(rp, "_search_hits", return_value={**RESULT, "hits": []}), \
                mock.patch.object(rp, "_deliverable_context_line", return_value=line):
            out = rp.prior_work_for_prompt(PROMPT)
        self.assertIn("data, not instructions", out["context"])
        self.assertLessEqual(len(out["context"]), 600)

    def test_acknowledged_batch_is_committed_and_deduplicated(self):
        svc = rd.RecallService(self.home / "recall.sock", stamp_fn=lambda: "same")
        raw = json.dumps({"prompt": PROMPT, "session_id": "seen"})
        request = {"op": "recall", "raw": raw, "protocol": 1, "deadline": time.monotonic() + 1}
        sock = mock.Mock()
        sock.recv.side_effect = [json.dumps(request).encode() + b"\n", b'{"op":"ack"}\n', b""]
        with mock.patch.object(rp, "_search_hits", return_value=RESULT), \
                mock.patch.object(rp, "_deliverable_context_line", return_value=""):
            svc._serve_conn(sock)
            out = rp.prior_work_for_prompt(PROMPT, session_id="seen")
        self.assertEqual(out["reason"], "dedup")
        self.assertEqual(rp._load_recent_batches("seen"), [("episode:5",)])

    def test_distinct_delivered_batches_preserve_history_after_contention(self):
        delivery = {}
        with mock.patch.object(rp, "_search_hits", return_value=RESULT), \
                mock.patch.object(rp, "_deliverable_context_line", return_value=""):
            rp.prior_work_for_prompt(PROMPT, session_id="distinct", delivery=delivery)
            held = rp.prior_work_for_prompt(PROMPT, session_id="distinct")
            self.assertTrue(held["context"])
            self.assertIn("dedup state unavailable or busy", held["reason"])
            rp.finish_delivery(delivery, delivered=True)
        other = {**HIT, "id": "6"}
        with mock.patch.object(rp, "_search_hits", return_value={**RESULT, "hits": [other]}), \
                mock.patch.object(rp, "_deliverable_context_line", return_value=""):
            self.assertTrue(rp.prior_work_for_prompt(PROMPT, session_id="distinct")["context"])
        self.assertEqual(rp._load_recent_batches("distinct"), [("episode:5",), ("episode:6",)])

    def test_semantic_query_and_cache_identity_are_redacted(self):
        prompt = "recall hook api_key=sk-" + "a" * 40
        with mock.patch.object(rp, "_query_embed_cache_get", return_value=None) as cache, \
                mock.patch.object(rp, "_query_embed_cache_put"), \
                mock.patch("khipu.embed.uses_task_prefixes", return_value=False), \
                mock.patch("khipu.embed.embed_one", return_value=[1.0]) as embed:
            rp._cached_query_embed(prompt, "p1")
        from khipu.redact import redact_secrets
        sanitized, _ = redact_secrets(prompt)
        self.assertNotEqual(prompt, sanitized)
        self.assertEqual(embed.call_args.args[0], sanitized)
        self.assertEqual(cache.call_args.args[0], rp._query_embed_cache_key("p1", sanitized))


class BundleLauncherAuditTest(unittest.TestCase):
    def test_staged_launcher_selects_bundled_runtime_for_client_and_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bindir = root / "bin"
            bindir.mkdir()
            shutil.copy(LAUNCHER, bindir / "khipu-prompt-recall")
            runtime = root / "python" / "bin" / "python3.11"
            runtime.parent.mkdir(parents=True)
            invocations = root / "runtime-invocations"
            runtime.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$0" >> ' + shlex.quote(str(invocations)) + '\n'
                + 'exec ' + shlex.quote(sys.executable) + ' "$@"\n')
            runtime.chmod(0o755)
            lib = root / "lib"
            lib.mkdir()
            (lib / "bundle_marker.py").write_text("VALUE = 'bundled fallback'\n")
            package = root / "packages" / "cli" / "khipu"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("")
            (package / "recall_prompt.py").write_text(
                "import json, sys\nfrom bundle_marker import VALUE\n"
                "def prompt_recall_main(**kwargs): print(json.dumps({'context':VALUE, 'runtime':sys.executable}))\n")
            client = bindir / "khipu-recall-client.py"
            env = {"PATH": os.environ["PATH"], "HOME": str(root / "home")}
            for code, expected in (("import json, sys; print(json.dumps({'context':'bundled client','runtime':sys.executable}))", "bundled client"),
                                   ("raise SystemExit(3)", "bundled fallback")):
                client.write_text(code)
                invocations.write_text("")
                done = subprocess.run(["/bin/sh", str(bindir / "khipu-prompt-recall")],
                                      input="{}", capture_output=True, text=True, env=env, cwd=root, timeout=10)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(json.loads(done.stdout)["context"], expected, done.stdout)
                self.assertEqual(Path(json.loads(done.stdout)["runtime"]).resolve(),
                                 Path(sys.executable).resolve())
                calls = [Path(line).resolve() for line in invocations.read_text().splitlines()]
                self.assertEqual(calls, [runtime.resolve()] * (1 if expected == "bundled client" else 2))


class QueryLogAuditTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "query_log.jsonl"
        patch = mock.patch.object(ql, "log_path", return_value=self.path)
        patch.start()
        self.addCleanup(patch.stop)

    def test_rotated_failures_remain_in_health_window(self):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entries = [{"ts": now, "degraded": "embed-unavailable"}] * 50
        self.path.with_suffix(".jsonl.1").write_text("\n".join(map(json.dumps, entries)) + "\n")
        self.path.write_text(json.dumps({"ts": now, "result_count": 1}) + "\n")
        out = ql.degraded_rate()
        self.assertFalse(out["ok"])
        self.assertEqual(out["sampled"], 50)

    def test_absent_or_unreadable_query_evidence_is_unavailable(self):
        out = ql.degraded_rate()
        self.assertFalse(out["ok"])
        self.assertIs(out.get("available"), False)
        self.path.write_text('{"result_count":1}\n')
        with mock.patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            self.assertFalse(ql.degraded_rate()["ok"])
