"""Capture spend bounds (2026-10-13): the summariser ran the Google project into
its monthly spending cap. Machine-generated user messages are not user turns,
one capture queues at most MAX_PARTS_PER_CAPTURE parts, and cloud synth calls
have a daily ceiling. Temp dirs and fakes only; no network.
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from khipu import cli
from khipu import embed as em
from khipu import extract
from khipu import session_capture as sc
from tests.test_cli_recall_quality import _green_doctor_patches

_REAL_BUDGET_PATH = em._budget_path


def _home(td):
    return mock.patch.dict(os.environ, {"KHIPU_CAPTURE_HOME": str(Path(td) / "kh")})


def _write(path: Path, rows: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def _user(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant(text: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": text}}


def _notification(i: int) -> str:
    return (f"<task-notification>\n<task-id>t{i}</task-id>\n<status>completed</status>\n"
            f"<result>{'r' * 2000}</result>\n</task-notification>")


TEAMMATE = "please re-run the lexical parity harness on the fresh copy " + "w" * 400


def _cross_session(i: int) -> str:
    return f'<cross-session-message from="aegis-{i}">{TEAMMATE}</cross-session-message>'


class MachineUserMessageTest(unittest.TestCase):
    def _fixture(self, td: str) -> Path:
        rows = [_user("<system-reminder>hook context " + "h" * 500 + "</system-reminder>"
                      "first real question about the drain"),
                _assistant("first answer")]
        for i in range(10):
            rows += [_user(_notification(i)), _assistant(f"noted {i}")]
        rows += [_user(_cross_session(1)), _assistant("on it"),
                 _user("second real question"), _assistant("second answer")]
        for i in range(10, 20):
            rows += [_user(_notification(i)), _assistant(f"noted {i}")]
        rows += [_user(_cross_session(2)), _assistant("on it again"),
                 _user("third real question"), _assistant("third answer")]
        return _write(Path(td) / "s.jsonl", rows)

    def test_read_window_counts_only_real_user_turns(self):
        with tempfile.TemporaryDirectory() as td:
            msgs, _, users = sc.read_window(self._fixture(td), 0)
        text = sc.render(msgs, max_chars=10**9)
        self.assertEqual(users, 3)
        self.assertEqual(text.count("[notification: task-notification"), 20)
        self.assertNotIn("r" * 100, text)
        clipped = [m for _, m in msgs if "[cross-session message" in m]
        self.assertEqual(len(clipped), 2)
        for line in clipped:
            self.assertTrue(line.startswith("please re-run the lexical parity harness"), line)
            self.assertEqual(len(line.split("…[cross-session message")[0]), sc.CROSS_SESSION_KEEP)
            self.assertIn("…[cross-session message, ", line)
        self.assertIn(("user", "second real question"), msgs)
        self.assertIn("USER: first real question about the drain", text)
        self.assertNotIn("system-reminder", text)
        self.assertNotIn("hook context", text)

    def test_stand_in_lines_and_real_text(self):
        self.assertIsNone(sc._machine_user_message("x" * 10))
        self.assertIsNone(sc._machine_user_message("see <task-notification> in docs"))
        raw = "  <task-notification>" + "a" * 2361 + "</task-notification>"
        self.assertEqual(sc._machine_user_message(raw),
                         f"[notification: task-notification, {len(raw):,} chars]")
        self.assertEqual(sc._machine_user_message("<command-name>/clear</command-name>"),
                         "[notification: command-name, 35 chars]")
        self.assertTrue(sc._machine_user_message("<local-command-stdout>ok</local-command-stdout>")
                        .startswith("[notification: local-command-stdout, "))
        short = sc._machine_user_message("<cross-session-idle>idle</cross-session-idle>")
        self.assertEqual(short, "idle [cross-session message, 45 chars]")

    def test_notifications_alone_do_not_reach_min_turns(self):
        rows = []
        for i in range(20):
            rows += [_user(_notification(i)), _assistant(f"noted {i}")]
        with tempfile.TemporaryDirectory() as td:
            msgs, _, users = sc.read_window(_write(Path(td) / "n.jsonl", rows), 0)
        text = sc.render(msgs, max_chars=10**9)
        self.assertEqual(users, 0)
        due, reason = sc.decide("stop", user_turns=users, chars=len(text), elapsed_s=0.0,
                                stop_hook_active=False, window_text=text)
        self.assertFalse(due, reason)
        # The same 20 turns typed by a person would be due.
        due_real, _ = sc.decide("stop", user_turns=20, chars=len(text), elapsed_s=0.0,
                                stop_hook_active=False, window_text=text)
        self.assertTrue(due_real)


class PartCapTest(unittest.TestCase):
    def _enqueue(self, td: str, n_msgs: int) -> tuple[list[dict], dict, list[dict]]:
        # One ~13,000-char message per part: two never fit in MAX_TRANSCRIPT.
        msgs = [("assistant" if i % 2 else "user", f"m{i} " + "x" * 13_000) for i in range(n_msgs)]
        all_parts = sc._window_parts(msgs, sc.MAX_TRANSCRIPT)
        self.assertEqual(len(all_parts), n_msgs)
        st: dict = {"offset": 100}
        tp = Path(td) / "t.jsonl"
        tp.write_text("", encoding="utf-8")
        names = sc._enqueue_window(harness="claude_code", sid="sess-9", track_sid="sess-9", cwd=td,
                                   event="stop", path=tp, st=st, msgs=msgs, new_off=987_654,
                                   turns=n_msgs)
        jobs = [json.loads((sc.queue_dir() / n).read_text(encoding="utf-8")) for n in names]
        jobs.sort(key=lambda j: j.get("part", "1/1"))
        return jobs, st, all_parts

    def test_nine_parts_keep_the_newest_four(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            jobs, st, all_parts = self._enqueue(td, 9)
            log = sc.log_path().read_text(encoding="utf-8")
            st_disk = sc.load_state("claude_code", "sess-9")
        self.assertEqual(sc.MAX_PARTS_PER_CAPTURE, 4)
        self.assertEqual(len(jobs), 4)
        self.assertEqual([j["part"] for j in jobs], ["1/4", "2/4", "3/4", "4/4"])
        self.assertEqual([j["transcript"] for j in jobs], [p["text"] for p in all_parts[-4:]])
        dropped = sum(len(p["text"]) for p in all_parts[:5])
        self.assertEqual(jobs[0]["truncated_chars"], dropped)
        self.assertTrue(all(j["truncated_chars"] == 0 for j in jobs[1:]))
        self.assertEqual(len({j["window_id"] for j in jobs}), 1)
        self.assertEqual(st["offset"], 987_654)
        self.assertEqual(st_disk["offset"], 987_654)
        lines = [ln for ln in log.splitlines() if "clipped to" in ln]
        self.assertEqual(len(lines), 1)
        self.assertIn(f"[claude_code] sess-9 window of 9 parts clipped to 4 (dropped {dropped:,} chars)",
                      lines[0])

    def test_two_parts_are_unchanged(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            jobs, st, all_parts = self._enqueue(td, 2)
            log = sc.log_path()
            log_text = log.read_text(encoding="utf-8") if log.exists() else ""
        self.assertEqual(len(jobs), 2)
        self.assertEqual([j["part"] for j in jobs], ["1/2", "2/2"])
        self.assertTrue(all(j["truncated_chars"] == 0 for j in jobs))
        self.assertEqual([j["transcript"] for j in jobs], [p["text"] for p in all_parts])
        self.assertEqual(st["offset"], 987_654)
        self.assertNotIn("clipped to", log_text)


class _SynthBudget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        p = mock.patch.object(em, "_budget_path",
                              lambda scope="memory": root / f"budget-{scope}.json")
        p.start()
        self.addCleanup(p.stop)


class SynthBudgetTest(_SynthBudget):
    def test_defaults(self):
        self.assertEqual(em.SYNTH_DAILY_CALL_BUDGET, 1000)
        self.assertEqual(em.EMBED_DAILY_CALL_BUDGET, 100000)
        self.assertEqual(em.LIBRARY_DAILY_CALL_BUDGET, 2000)
        self.assertTrue(issubclass(em.BudgetExhausted, RuntimeError))

    def test_budget_file_names(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch("khipu.paths.ensure_data_dir", return_value=Path(td)):
            names = {s: _REAL_BUDGET_PATH(s).name for s in ("memory", "library", "synth")}
        self.assertEqual(names, {"memory": "embed-budget.json", "library": "embed-budget-library.json",
                                 "synth": "embed-budget-synth.json"})

    def test_take_charges_and_refuses_at_the_cap(self):
        with mock.patch.object(em, "SYNTH_DAILY_CALL_BUDGET", 2):
            em._budget_take(scope="synth")
            em._budget_take(scope="synth")
            with self.assertRaises(em.BudgetExhausted) as ctx:
                em._budget_take(scope="synth")
            st = em.budget_status("synth")
        self.assertIn("synth call budget exhausted", str(ctx.exception))
        self.assertIn("KHIPU_SYNTH_DAILY_CALLS", str(ctx.exception))
        self.assertEqual({k: st[k] for k in ("calls", "cap", "remaining", "exhausted")},
                         {"calls": 2, "cap": 2, "remaining": 0, "exhausted": True})
        # The other scopes are untouched.
        self.assertEqual(em.budget_status("memory")["calls"], 0)
        self.assertEqual(em.budget_status("library")["calls"], 0)

    def test_day_rollover(self):
        with mock.patch.object(em, "SYNTH_DAILY_CALL_BUDGET", 1):
            em._budget_take(scope="synth")
            with mock.patch.object(em, "_budget_today", lambda: "2999-01-01"):
                self.assertEqual(em.budget_status("synth")["calls"], 0)
                em._budget_take(scope="synth")

    def test_status_shape(self):
        st = em.budget_status("synth")
        for key in ("day", "calls", "cap", "unlimited", "remaining", "exhausted", "available"):
            self.assertIn(key, st)
        self.assertEqual((st["calls"], st["cap"], st["remaining"], st["exhausted"]), (0, 1000, 1000, False))

    def test_generate_refuses_before_the_network_and_charges_per_attempt(self):
        calls = []

        def fake_urlopen(*_a, **_k):
            calls.append(1)
            raise OSError("down")

        with mock.patch.object(em, "SYNTH_DAILY_CALL_BUDGET", 2), \
                mock.patch("khipu.models.synth_settings", return_value={"provider": "cloud"}), \
                mock.patch.object(extract, "_key", return_value="k"), \
                mock.patch.object(extract.time, "sleep"), \
                mock.patch.object(extract.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(em.BudgetExhausted):
                extract._generate("p", retries=5)
            self.assertEqual(len(calls), 2, "one charge per HTTP attempt")
            calls.clear()
            with self.assertRaises(em.BudgetExhausted):
                extract._generate("p")
            self.assertEqual(calls, [], "network must not be touched once the budget is spent")

    def test_rerank_cloud_call_is_gated_too(self):
        from khipu import rerank

        with mock.patch.object(em, "SYNTH_DAILY_CALL_BUDGET", 1), \
                mock.patch("khipu.models.synth_settings", return_value={"provider": "cloud"}), \
                mock.patch.object(extract, "_key", return_value="k"), \
                mock.patch.object(extract.urllib.request, "urlopen",
                                  side_effect=AssertionError("network touched")):
            em._budget_take(scope="synth")
            with self.assertRaises(em.BudgetExhausted):
                rerank._call_model("p", timeout=1)


class DrainBudgetTest(_SynthBudget):
    def test_drain_stops_at_first_refusal_and_keeps_every_job(self):
        with tempfile.TemporaryDirectory() as td, _home(td):
            q = sc.queue_dir()
            q.mkdir(parents=True)
            for i in range(3):
                (q / f"{i}.json").write_text(json.dumps({
                    "harness": "claude_code", "session_id": f"s{i}", "cwd": "/x", "event": "stop",
                    "ts": sc._mint_ts(), "turns": 5, "transcript": f"USER: question {i} " + "q" * 300,
                    "offset_before": 0, "offset_after": 10, "truncated_chars": 0}))
            before = sorted(p.name for p in sc.queued_jobs())
            with mock.patch.object(em, "SYNTH_DAILY_CALL_BUDGET", 1), \
                    mock.patch("khipu.models.synth_settings", return_value={"provider": "cloud"}), \
                    mock.patch.object(extract, "_key", return_value="k"), \
                    mock.patch.object(extract.urllib.request, "urlopen",
                                      side_effect=AssertionError("network touched")), \
                    mock.patch.object(extract, "extract_memory", wraps=extract.extract_memory) as ex, \
                    mock.patch("khipu.capture.capture") as capture, \
                    mock.patch("khipu.config.capture_mode", return_value="hub"):
                em._budget_take(scope="synth")
                out = sc.drain()
            after = sorted(p.name for p in sc.queued_jobs())
            log = sc.log_path().read_text(encoding="utf-8")
        self.assertEqual(ex.call_count, 1)
        capture.assert_not_called()
        self.assertEqual(after, before)
        self.assertTrue(out["synth_budget_exhausted"])
        self.assertEqual(out["waiting"], 3)
        lines = [ln for ln in log.splitlines() if "budget" in ln]
        self.assertEqual(len(lines), 1, log)
        self.assertIn("drain: synth call budget exhausted for today (1/1); 3 job(s) wait", lines[0])


class DoctorSynthBudgetTest(_SynthBudget):
    def setUp(self):
        super().setUp()
        for p in _green_doctor_patches():
            if getattr(p, "attribute", None) == "budget_status":
                continue
            p.start()
            self.addCleanup(p.stop)

    def _doctor(self) -> dict:
        args = cli.build_parser().parse_args(["doctor"])
        with mock.patch("khipu.probe.status", return_value={"ok": True}), \
                mock.patch("khipu.recall_prompt.prompt_recall_outcomes", return_value={"ok": True}), \
                mock.patch("khipu.drift.recall_quality", return_value={}), \
                redirect_stdout(io.StringIO()) as out:
            cli.cmd_doctor(args)
        return json.loads(out.getvalue())

    def test_synth_budget_reported_and_gates_ok(self):
        with mock.patch.object(em, "SYNTH_DAILY_CALL_BUDGET", 1):
            fresh = self._doctor()
            em._budget_take(scope="synth")
            spent = self._doctor()
        self.assertTrue(fresh["synth_budget_ok"])
        self.assertEqual({k: fresh["synth_budget"][k] for k in ("calls", "cap", "remaining", "exhausted")},
                         {"calls": 0, "cap": 1, "remaining": 1, "exhausted": False})
        self.assertFalse(spent["synth_budget_ok"])
        self.assertTrue(spent["synth_budget"]["exhausted"])
        self.assertFalse(spent["ok"])
        # Nothing else changed between the two runs: the budget alone flips a check.
        flips = [k for k in fresh if k.endswith("_ok") and fresh[k] != spent[k]]
        self.assertEqual(flips, ["synth_budget_ok"])


if __name__ == "__main__":
    unittest.main()
