"""Capture spend bounds (2026-10-13): the summariser ran the Google project into
its monthly spending cap. Machine-generated user messages are not user turns
(their content stays), a capture queues every part unless the owner sets
MAX_PARTS_PER_CAPTURE, cloud synth calls have a daily ceiling the owner can
lift, and every call's tokens are recorded. Temp dirs and fakes only; no network.
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
_CAP_ENV = ("KHIPU_SYNTH_DAILY_CALLS", "KHIPU_EMBED_DAILY_CALLS", "KHIPU_LIBRARY_EMBED_DAILY_CALLS")


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
                 _user("third real question"), _assistant("third answer"),
                 _user("<task-notification>\n<task-id>t99</task-id>\n"
                       "<summary>Agent \"parity run\" completed</summary>\n"
                       "<result>" + "r" * 2000 + "</result>\n</task-notification>"),
                 _assistant("read it")]
        return _write(Path(td) / "s.jsonl", rows)

    def test_read_window_counts_only_real_user_turns(self):
        with tempfile.TemporaryDirectory() as td:
            msgs, _, users = sc.read_window(self._fixture(td), 0)
        text = sc.render(msgs, max_chars=10**9)
        self.assertEqual(users, 3)
        self.assertEqual(text.count("[notification: task-notification, "), 20)
        self.assertEqual(text.count("[notification: task-notification] "), 1)
        self.assertNotIn("r" * 100, text)
        relayed = [m for _, m in msgs if m.startswith("please re-run")]
        self.assertEqual(relayed, [TEAMMATE, TEAMMATE], "whole body, outer tag stripped")
        self.assertNotIn("cross-session-message", text)
        self.assertFalse(hasattr(sc, "CROSS_SESSION_KEEP"))
        self.assertIn('[notification: task-notification] Agent "parity run" completed', text)
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
        self.assertEqual(short, "idle")
        long_body = "line one\n" + "w" * 5000
        self.assertEqual(sc._machine_user_message(
            f'<cross-session-delivery to="x">{long_body}</cross-session-delivery>'), long_body)
        self.assertEqual(sc._machine_user_message("<cross-session-idle></cross-session-idle>"),
                         "[notification: cross-session-idle, 41 chars]")
        self.assertEqual(sc._machine_user_message(
            "<task-notification><summary> Build green </summary><result>x</result></task-notification>"),
            "[notification: task-notification] Build green")
        empty = "<task-notification><summary> </summary></task-notification>"
        self.assertEqual(sc._machine_user_message(empty),
                         f"[notification: task-notification, {len(empty):,} chars]")

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

    def test_default_keeps_every_part(self):
        self.assertEqual(sc.MAX_PARTS_PER_CAPTURE, 0)
        with tempfile.TemporaryDirectory() as td, _home(td):
            jobs, st, all_parts = self._enqueue(td, 9)
            log = sc.log_path()
            log_text = log.read_text(encoding="utf-8") if log.exists() else ""
        self.assertEqual(len(jobs), 9)
        self.assertEqual([j["transcript"] for j in jobs], [p["text"] for p in all_parts])
        self.assertTrue(all(j["truncated_chars"] == 0 for j in jobs))
        self.assertEqual(st["offset"], 987_654)
        self.assertNotIn("clipped to", log_text)

    def test_owner_cap_keeps_the_newest_parts(self):
        with tempfile.TemporaryDirectory() as td, _home(td), \
                mock.patch.object(sc, "MAX_PARTS_PER_CAPTURE", 4):
            jobs, st, all_parts = self._enqueue(td, 9)
            log = sc.log_path().read_text(encoding="utf-8")
            st_disk = sc.load_state("claude_code", "sess-9")
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

    def test_two_parts_under_an_owner_cap_are_unchanged(self):
        with tempfile.TemporaryDirectory() as td, _home(td), \
                mock.patch.object(sc, "MAX_PARTS_PER_CAPTURE", 4):
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
        root = self.root = Path(self.tmp.name)
        env = {k: v for k, v in os.environ.items() if k not in _CAP_ENV}
        env["KHIPU_DATA_DIR"] = str(root / "data")
        env["KHIPU_CAPTURE_HOME"] = str(root / "kh")
        for p in (mock.patch.object(em, "_budget_path",
                                    lambda scope="memory": root / f"budget-{scope}.json"),
                  mock.patch.dict(os.environ, env, clear=True)):
            p.start()
            self.addCleanup(p.stop)


class SynthBudgetTest(_SynthBudget):
    def test_defaults(self):
        self.assertEqual(em.SYNTH_DAILY_CALL_BUDGET, 5000)
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
        self.assertIn("khipu budget lift synth", str(ctx.exception))
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
        for key in ("day", "calls", "cap", "cap_source", "unlimited", "remaining", "exhausted",
                    "warn", "available"):
            self.assertIn(key, st)
        self.assertEqual((st["calls"], st["cap"], st["remaining"], st["exhausted"], st["warn"],
                          st["cap_source"]), (0, 5000, 5000, False, False, "default"))

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


    def test_warn_at_eighty_percent(self):
        with mock.patch.object(em, "SYNTH_DAILY_CALL_BUDGET", 5):
            for _ in range(3):
                em._budget_take(scope="synth")
            below = self._doctor()
            em._budget_take(scope="synth")
            at = self._doctor()
        self.assertFalse(below["synth_budget"]["warn"])
        self.assertTrue(at["synth_budget"]["warn"])
        self.assertTrue(at["synth_budget_ok"], "warn never gates")
        self.assertEqual(at["synth_budget"]["cap_source"], "default")
        self.assertEqual(at["synth_budget"]["tokens_today"],
                         {"prompt_tokens": 0, "output_tokens": 0, "thought_tokens": 0})
        self.assertEqual(at["synth_budget"]["estimated_usd_today"], 0.0)

    def test_unlimited_never_warns(self):
        with mock.patch.object(em, "SYNTH_DAILY_CALL_BUDGET", 0):
            for _ in range(3):
                em._budget_take(scope="synth")
            st = em.budget_status("synth")
        self.assertEqual((st["unlimited"], st["warn"], st["exhausted"]), (True, False, False))


def _run(argv: list[str]) -> tuple[int, dict]:
    args = cli.build_parser().parse_args(argv)
    with redirect_stdout(io.StringIO()) as out:
        rc = args.func(args)
    return rc, json.loads(out.getvalue())


class BudgetCapPrecedenceTest(_SynthBudget):
    def test_config_over_env_over_default(self):
        from khipu.config import set_int_setting

        self.assertEqual(em._budget_cap("synth"), (5000, "default"))
        with mock.patch.dict(os.environ, {"KHIPU_SYNTH_DAILY_CALLS": "30"}):
            self.assertEqual(em._budget_cap("synth"), (30, "env"))
            set_int_setting("synth_daily_calls", 7)
            self.assertEqual(em._budget_cap("synth"), (7, "config"))
            set_int_setting("synth_daily_calls", None)
            self.assertEqual(em._budget_cap("synth"), (30, "env"))
        set_int_setting("synth_daily_calls", 0)
        self.assertEqual(em._budget_cap("synth"), (0, "config"))
        for _ in range(3):
            em._budget_take(scope="synth")   # 0 = unlimited
        self.assertTrue(em.budget_status("synth")["unlimited"])
        # The other scopes read their own keys.
        set_int_setting("embed_daily_calls", 11)
        set_int_setting("library_embed_daily_calls", 12)
        self.assertEqual(em._budget_cap("memory"), (11, "config"))
        self.assertEqual(em._budget_cap("library"), (12, "config"))

    def test_config_set_round_trips_and_refuses_bad_values(self):
        from khipu.config import config_file, int_setting

        rc, out = _run(["config", "--set", "synth_daily_calls", "7"])
        self.assertEqual(rc, 0)
        self.assertEqual(out["synth_daily_calls"], 7)
        self.assertEqual(out["budget"]["cap"], 7)
        self.assertEqual(out["budget"]["cap_source"], "config")
        self.assertEqual(int_setting("synth_daily_calls"), 7)
        self.assertEqual(json.loads(config_file().read_text())["synth_daily_calls"], 7)
        self.assertEqual(em._budget_cap("synth"), (7, "config"))
        for bad in ("seven", "-1", "2.5", ""):
            rc, out = _run(["config", "--set", "synth_daily_calls", bad])
            self.assertEqual(rc, 2, bad)
            self.assertFalse(out["ok"])
            self.assertIn("whole number >= 0", out["error"])
        self.assertEqual(int_setting("synth_daily_calls"), 7, "a refused value changes nothing")
        for key in ("embed_daily_calls", "library_embed_daily_calls"):
            self.assertEqual(_run(["config", "--set", key, "0"])[0], 0)
            self.assertEqual(int_setting(key), 0)
        rc, out = _run(["config", "--unset", "synth_daily_calls"])
        self.assertEqual(rc, 0)
        self.assertIsNone(int_setting("synth_daily_calls"))
        self.assertEqual(em._budget_cap("synth"), (5000, "default"))


class BudgetCommandTest(_SynthBudget):
    def test_lift_today_resets_the_counter(self):
        with mock.patch.object(em, "SYNTH_DAILY_CALL_BUDGET", 2):
            em._budget_take(scope="synth")
            em._budget_take(scope="synth")
            with self.assertRaises(em.BudgetExhausted):
                em._budget_take(scope="synth")
            rc, out = _run(["budget", "lift", "synth", "--today"])
            self.assertEqual(rc, 0)
            self.assertEqual(out["calls_cleared"], 2)
            self.assertEqual(out["budget"]["calls"], 0)
            em._budget_take(scope="synth")
            self.assertEqual(em.budget_status("synth")["calls"], 1)
        rec = json.loads((self.root / "budget-synth.json").read_text())
        self.assertEqual(rec, {"day": em._budget_today(), "calls": 1})
        self.assertEqual(em.budget_status("memory")["calls"], 0, "other scopes untouched")
        log = sc.log_path().read_text(encoding="utf-8")
        self.assertEqual(len([ln for ln in log.splitlines() if "budget lift:" in ln]), 1)
        self.assertIn("reset today's synth counter (2 calls cleared)", log)

    def test_lift_cap_and_unlimited_persist(self):
        from khipu.config import int_setting

        rc, out = _run(["budget", "lift", "memory", "--cap", "500"])
        self.assertEqual(rc, 0)
        self.assertEqual(int_setting("embed_daily_calls"), 500)
        self.assertEqual(out["budget"]["cap"], 500)
        self.assertEqual(out["budget"]["cap_source"], "config")
        rc, out = _run(["budget", "lift", "library", "--unlimited"])
        self.assertEqual(rc, 0)
        self.assertEqual(int_setting("library_embed_daily_calls"), 0)
        self.assertTrue(out["budget"]["unlimited"])
        rc, out = _run(["budget", "lift", "synth", "--cap", "-3"])
        self.assertEqual(rc, 2)
        self.assertIsNone(int_setting("synth_daily_calls"))
        log = sc.log_path().read_text(encoding="utf-8")
        self.assertEqual(len([ln for ln in log.splitlines() if "budget lift:" in ln]), 2)
        self.assertIn("memory ceiling set to 500", log)
        self.assertIn("library ceiling set to unlimited", log)

    def test_lift_needs_exactly_one_mode(self):
        for argv in (["budget", "lift", "synth"],
                     ["budget", "lift", "synth", "--today", "--unlimited"],
                     ["budget", "lift", "bogus", "--today"]):
            with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), \
                    mock.patch("sys.stderr", io.StringIO()):
                cli.build_parser().parse_args(argv)

    def test_status_shape(self):
        with mock.patch.dict(os.environ, {"KHIPU_EMBED_DAILY_CALLS": "50"}):
            _run(["budget", "lift", "synth", "--cap", "9"])
            em._budget_take(scope="synth")
            rc, out = _run(["budget", "status"])
        self.assertEqual(rc, 0)
        self.assertEqual(set(out), {"synth", "memory", "library"})
        for scope in out.values():
            for key in ("day", "calls", "cap", "remaining", "exhausted", "cap_source"):
                self.assertIn(key, scope)
        self.assertEqual((out["synth"]["calls"], out["synth"]["cap"], out["synth"]["remaining"],
                          out["synth"]["cap_source"]), (1, 9, 8, "config"))
        self.assertEqual((out["memory"]["cap"], out["memory"]["cap_source"]), (50, "env"))
        self.assertEqual((out["library"]["cap"], out["library"]["cap_source"]), (2000, "default"))
        self.assertEqual(out["synth"]["tokens_today"],
                         {"prompt_tokens": 0, "output_tokens": 0, "thought_tokens": 0})
        self.assertEqual(out["synth"]["estimated_usd_today"], 0.0)


class _Resp:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _answer(usage: dict | None) -> dict:
    out = {"candidates": [{"content": {"parts": [{"text": '{"summary": "ok"}'}]}}]}
    if usage is not None:
        out["usageMetadata"] = usage
    return out


class SynthUsageTest(_SynthBudget):
    def _cloud(self, payload: dict, **kw) -> str:
        with mock.patch.object(extract, "_key", return_value="k"), \
                mock.patch.object(extract.urllib.request, "urlopen", return_value=_Resp(payload)):
            return extract._generate_cloud("p", timeout=1, retries=0, **kw)

    def _rows(self) -> list[dict]:
        path = extract.synth_usage_path()
        if not path.exists():
            return []
        return [json.loads(ln) for ln in path.read_text().splitlines()]

    def test_usage_line_written_per_call(self):
        out = self._cloud(_answer({"promptTokenCount": 1000, "candidatesTokenCount": 200,
                                   "thoughtsTokenCount": 50}),
                          model_id="gemini-2.5-flash", caller="capture")
        self.assertEqual(out, '{"summary": "ok"}')
        self._cloud(_answer({"promptTokenCount": 10, "candidatesTokenCount": 2}),
                    model_id="gemini-2.5-flash")
        rows = self._rows()
        self.assertEqual(extract.synth_usage_path(), self.root / "data" / "state" / "synth-usage.jsonl")
        self.assertEqual(len(rows), 2)
        self.assertEqual({k: rows[0][k] for k in rows[0] if k != "ts"},
                         {"provider": "gemini", "cached_tokens": 0, "model": "gemini-2.5-flash", "prompt_tokens": 1000, "output_tokens": 200,
                          "thought_tokens": 50, "caller": "capture"})
        self.assertEqual((rows[1]["caller"], rows[1]["thought_tokens"]), ("synth", 0))
        self.assertRegex(rows[0]["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_missing_metadata_writes_nothing_and_the_call_returns(self):
        self.assertEqual(self._cloud(_answer(None), model_id="gemini-2.5-flash"), '{"summary": "ok"}')
        self.assertEqual(self._cloud(_answer({"totalTokenCount": 5}), model_id="gemini-2.5-flash"),
                         '{"summary": "ok"}')
        self.assertEqual(self._rows(), [])

    def test_unwritable_usage_file_never_fails_the_call(self):
        with mock.patch.object(extract, "synth_usage_path",
                               return_value=self.root / "nope" / "\0bad"):
            self.assertEqual(self._cloud(_answer({"promptTokenCount": 1}), model_id="m"),
                             '{"summary": "ok"}')

    def test_callers_pass_their_name(self):
        seen = []
        with mock.patch.object(extract, "_generate", side_effect=lambda *_a, **k: seen.append(k)
                               or '{"summary": "s"}'):
            extract.extract_memory("USER: hello " + "x" * 300)
        self.assertEqual(seen[0].get("caller"), "capture")

    def test_today_totals_and_cost(self):
        today = em._budget_today()
        path = extract.synth_usage_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {"ts": "2000-01-01T00:00:00Z", "model": "gemini-2.5-flash", "prompt_tokens": 10**9,
             "output_tokens": 10**9, "thought_tokens": 0, "caller": "capture"},
            {"ts": f"{today}T01:00:00Z", "model": "gemini-2.5-flash", "prompt_tokens": 1_000_000,
             "output_tokens": 100_000, "thought_tokens": 100_000, "caller": "capture"},
            {"ts": f"{today}T02:00:00Z", "model": "gemini-2.5-flash-lite", "prompt_tokens": 2_000_000,
             "output_tokens": 500_000, "thought_tokens": 0, "caller": "rerank"},
        ]
        path.write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n")
        u = extract.synth_usage_today()
        self.assertEqual((u["calls"], u["prompt_tokens"], u["output_tokens"], u["thought_tokens"]),
                         (2, 3_000_000, 600_000, 100_000))
        # flash: 1M in * 0.30 + 0.2M out * 2.50 = 0.80; lite: 2M * 0.10 + 0.5M * 0.40 = 0.40
        self.assertAlmostEqual(u["by_model"]["gemini-2.5-flash"]["estimated_usd"], 0.80)
        self.assertAlmostEqual(u["by_model"]["gemini-2.5-flash-lite"]["estimated_usd"], 0.40)
        self.assertAlmostEqual(u["estimated_usd"], 1.20)
        rc, out = _run(["budget", "status"])
        self.assertAlmostEqual(out["synth"]["estimated_usd_today"], 1.20)
        self.assertEqual(out["synth"]["tokens_today"]["prompt_tokens"], 3_000_000)
        # An unknown model shows its tokens and makes the total unknown.
        with path.open("a") as fh:
            fh.write(json.dumps({"ts": f"{today}T03:00:00Z", "model": "acme-1", "prompt_tokens": 7,
                                 "output_tokens": 1, "thought_tokens": 0, "caller": "brief"}) + "\n")
        u = extract.synth_usage_today()
        self.assertEqual(u["by_model"]["acme-1"]["prompt_tokens"], 7)
        self.assertIsNone(u["by_model"]["acme-1"]["estimated_usd"])
        self.assertIsNone(u["estimated_usd"])

    def test_backward_read_stops_at_older_days(self):
        today = em._budget_today()
        path = extract.synth_usage_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        old = json.dumps({"ts": "2000-01-01T00:00:00Z", "model": "m", "prompt_tokens": 1,
                          "output_tokens": 0, "thought_tokens": 0, "caller": "capture"}) + "\n"
        new = json.dumps({"ts": f"{today}T00:00:00Z", "model": "m", "prompt_tokens": 2,
                          "output_tokens": 0, "thought_tokens": 0, "caller": "capture"}) + "\n"
        path.write_text(old * 5000 + new * 300)
        lines = extract._usage_lines_since(path, today, block=4096)
        self.assertLess(len(lines), 5000 + 300)
        self.assertEqual(extract.synth_usage_today()["prompt_tokens"], 600)


if __name__ == "__main__":
    unittest.main()
