"""Two measures of prompt-time recall that hid a real break (2026-11-09).

1. ``recall_prompt.prompt_recall_outcomes`` (doctor): what the owner is shown
   judges the last day, at most 100 searched calls, and says plainly when half
   or more failed; the promotion gate keeps its own rule (the rate window,
   timeouts alone never refuse). Each consumer is tested: plain doctor and the
   app's row (``prompt_recall_outcomes_ok``), ``doctor --quick`` and
   ``promote.gate_doctor``.
2. ``promote.gate_recall_replay``: speed is judged against the live runs on
   either side of each candidate run, so load arriving between runs does not
   fail code exactly as fast as live.
"""
from __future__ import annotations

import argparse
import io
import json
import subprocess
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

from khipu import cli, integrations
from khipu import promote as pm
from khipu import recall_prompt as rp

NOW = datetime(2026, 11, 9, 18, 0, tzinfo=timezone.utc)


def _line(when: datetime, reason: str, *, ms: float = 900.0, legs: str = "['lexical', 'cosine']",
          degraded: str = "None", offline: bool = False) -> str:
    return (f"{when.strftime('%Y-%m-%dT%H:%M:%SZ')} [khipu-prompt-recall] session=s reason={reason} ms={ms} "
            f"hits=[] legs={legs} degraded={degraded}" + (" offline=1" if offline else ""))


@pytest.fixture
def recall_log(tmp_path, monkeypatch):
    path = tmp_path / "prompt-recall.log"
    monkeypatch.setattr(rp, "_log_path", lambda: path)

    def write(lines: list[str]) -> None:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return write


def _the_2026_11_09_day() -> list[str]:
    """A day like the main Mac's: 261 searched prompts, the last 100 of them
    63 timeouts and 37 answers of which 20 had keyword matches only; nothing
    in the last hour (so the old one-hour window said "quiet")."""
    lines = []
    for i in range(161):  # older in the day, answered
        lines.append(_line(NOW - timedelta(hours=23, minutes=i % 50), "ok"))
    recent = (["timeout>1.2s"] * 63) + (["ok-lexical"] * 20) + (["ok"] * 17)
    for i, kind in enumerate(recent):
        when = NOW - timedelta(hours=20) + timedelta(minutes=10 * i)
        if kind == "ok-lexical":
            lines.append(_line(when, "ok", legs="['lexical']", degraded="embedding late"))
        elif kind == "ok":
            lines.append(_line(when, "ok"))
        else:
            lines.append(_line(when, kind, ms=1200.0, legs="[]", degraded="timeout"))
    assert max(NOW - timedelta(hours=20) + timedelta(minutes=10 * i) for i in range(100)) < NOW - timedelta(hours=1)
    return lines


# ---- 1. the doctor check -----------------------------------------------------


def test_the_owner_is_told_when_half_the_last_day_s_prompts_failed(recall_log):
    recall_log(_the_2026_11_09_day())
    out = rp.prompt_recall_outcomes(now=NOW, window_s=3600)
    assert out["ok"] is False and out["level"] == "red" and out["insufficient"] is False
    assert out["count"] == 100 and out["window_minutes"] == 24 * 60
    assert (out["timeouts"], out["errors"], out["answered"], out["answered_no_semantic"]) == (63, 0, 37, 20)
    assert out["warning"] == (
        "Recall before each prompt is failing: 63 of the last 100 prompts that searched memory in the "
        "last 24 hours got nothing back in time (63 timed out, 0 errored); of the 37 that answered, 20 had "
        "no meaning-based search (keyword matches only).")
    # Only logged prompts are counted; nothing is said about prompts the
    # harness killed before the summary line, because nothing measured them.
    assert "kill" not in out["warning"] and "unlogged" not in out
    # The gate's own rule over the rate window: nothing in the last hour is
    # quiet, and timeouts alone would never refuse anyway.
    assert out["gate_ok"] is True and out["gate"]["quiet"] is True


def test_errors_are_red_for_the_owner_and_for_the_gate(recall_log):
    recall_log([_line(NOW - timedelta(minutes=30 - i), "error:boom" if i % 4 else "ok") for i in range(24)])
    out = rp.prompt_recall_outcomes(now=NOW, window_s=3600)
    assert out["ok"] is False and out["gate_ok"] is False
    assert "18 errored" in out["warning"] and "timeout_only" not in out


def test_fewer_than_20_in_the_last_day_says_how_many(recall_log):
    week = [_line(NOW - timedelta(days=3, minutes=i), "timeout>1.2s", ms=1200.0) for i in range(30)]
    day = [_line(NOW - timedelta(hours=2, minutes=i), "ok") for i in range(12)]
    recall_log(week + day)
    out = rp.prompt_recall_outcomes(now=NOW, window_s=3600)
    assert out["ok"] is True and out["insufficient"] is True and out["quiet"] is True
    assert out["count"] == 12
    assert out["message"].startswith("not enough prompts yet: 12 prompt(s) searched memory in the last 24 hours")
    assert "warning" not in out


def test_at_most_the_100_most_recent_calls_are_judged(recall_log):
    old_fail = [_line(NOW - timedelta(hours=10, seconds=200 - i), "timeout>1.2s", ms=1200.0) for i in range(150)]
    recent_ok = [_line(NOW - timedelta(hours=3, seconds=100 - i), "ok") for i in range(100)]
    recall_log(old_fail + recent_ok)
    out = rp.prompt_recall_outcomes(now=NOW, window_s=3600)
    assert out["ok"] is True and out["count"] == 100 and out["timeouts"] == 0


def test_offline_calls_never_count_as_failures_for_the_owner(recall_log):
    lines = [_line(NOW - timedelta(hours=5, seconds=i), "timeout>1.2s", ms=1200.0, offline=True) for i in range(60)]
    lines += [_line(NOW - timedelta(hours=4, seconds=i), "ok") for i in range(25)]
    recall_log(lines)
    out = rp.prompt_recall_outcomes(now=NOW, window_s=3600)
    assert out["ok"] is True and out["offline"] == 60 and out["count"] == 25


def test_doctor_warnings_carry_the_owner_s_sentence():
    block = {"ok": False, "gate_ok": True, "level": "red", "warning": "Recall before each prompt is failing: x."}
    assert cli._recall_outcomes_warnings({"prompt_recall_outcomes": block}) == [block["warning"]]
    thin = {"ok": True, "level": "amber", "message": "not enough prompts yet: 3"}
    assert cli._recall_outcomes_warnings({"prompt_recall_outcomes": thin}) == [thin["message"]]


def test_plain_doctor_shows_the_owner_s_verdict():
    """Plain doctor is what the app's health row reads (``*_ok`` false is a
    red row and an attention item built from count, failure_rate and
    window_minutes): timeouts alone on half the day are red there."""
    from tests.test_cli_recall_quality import _green_doctor_patches

    block = {"ok": False, "gate_ok": True, "level": "red", "available": True, "insufficient": False,
             "count": 100, "failure_rate": 0.63, "window_minutes": 1440,
             "warning": "Recall before each prompt is failing: 63 of the last 100 prompts."}
    patches = _green_doctor_patches()
    for p in patches:
        p.start()
    try:
        args = cli.build_parser().parse_args(["doctor"])
        with mock.patch("khipu.probe.status", return_value={"ok": True}), \
                mock.patch("khipu.recall_prompt.prompt_recall_outcomes", return_value=block), \
                mock.patch("khipu.drift.recall_quality", return_value={}), \
                redirect_stdout(io.StringIO()) as stdout:
            rc = cli.cmd_doctor(args)
    finally:
        for p in patches:
            p.stop()
    result = json.loads(stdout.getvalue())
    assert result["prompt_recall_outcomes_ok"] is False
    assert result["ok"] is False and rc == 2
    assert block["warning"] in result["warnings"]
    for field in ("count", "failure_rate", "window_minutes", "insufficient"):
        assert result["prompt_recall_outcomes"][field] == block[field]


def _quick(monkeypatch, block: dict) -> tuple[int, dict]:
    local = {k: True for k in cli.LOCAL_GATES}
    local.update({"secrets": {"dsn": True}, "job_failures": {},
                  "prompt_recall_outcomes": block, "prompt_recall_outcomes_ok": bool(block.get("ok"))})
    monkeypatch.setattr(cli, "_doctor_local_checks", lambda mem: dict(local))
    monkeypatch.setattr("khipu.migrate.doctor_check", lambda: {})
    monkeypatch.setattr(cli, "_owner_writes_warnings", lambda: [])
    with redirect_stdout(io.StringIO()) as stdout:
        rc = cli._cmd_doctor_quick(argparse.Namespace(memory_root=None))
    return rc, json.loads(stdout.getvalue())


def test_doctor_quick_reports_the_gate_s_rule(monkeypatch, tmp_path):
    """`doctor --quick` is the promotion gate's doctor: its flag is the
    gate's rule, and the owner's sentence still rides in its warnings."""
    monkeypatch.setenv("HOME", str(tmp_path))
    busy = {"ok": False, "gate_ok": True, "level": "red", "warning": "Recall before each prompt is failing."}
    rc, out = _quick(monkeypatch, busy)
    assert out["prompt_recall_outcomes_ok"] is True
    assert out["prompt_recall_outcomes"]["ok"] is False
    assert busy["warning"] in out["warnings"]
    broken = {"ok": False, "gate_ok": False, "level": "red", "warning": "Recall before each prompt is failing."}
    rc, out = _quick(monkeypatch, broken)
    assert out["prompt_recall_outcomes_ok"] is False and out["ok"] is False and rc == 2
    # A check that itself failed carries no gate_ok: it stays red.
    rc, out = _quick(monkeypatch, {"ok": False, "error": "RuntimeError: boom"})
    assert out["prompt_recall_outcomes_ok"] is False


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(integrations, "HOME", h)
    monkeypatch.setenv("KHIPU_DATA_DIR", str(h / ".config" / "khipu"))
    return h


def _checkout(path: Path) -> Path:
    (path / "packages" / "cli" / "khipu").mkdir(parents=True, exist_ok=True)
    return path


_BASE = {"ok": False, "imports": {"failed": {}}, "config": {"ok": True}, "dsn_present": True}


def _doctor_says(monkeypatch, out: dict, rc: int = 2) -> None:
    monkeypatch.setattr(pm, "_run", lambda argv, **kw: subprocess.CompletedProcess(argv, rc, json.dumps(out), ""))


def test_the_promote_gate_keeps_its_rule_when_the_owner_s_row_is_red(home, monkeypatch):
    # A doctor whose flag is the owner's verdict (red from timeouts alone)
    # but whose block says the gate's rule passes: the promote goes on.
    _doctor_says(monkeypatch, {**_BASE, "prompt_recall_outcomes_ok": False,
                               "prompt_recall_outcomes": {"ok": False, "gate_ok": True}})
    out = pm.gate_doctor(_checkout(home / "c"))
    assert out["ok"], out
    assert out["owner_only_reds"] == ["prompt_recall_outcomes"]
    # The gate's rule failing (real errors) still refuses.
    _doctor_says(monkeypatch, {**_BASE, "prompt_recall_outcomes_ok": False,
                               "prompt_recall_outcomes": {"ok": False, "gate_ok": False}})
    out = pm.gate_doctor(_checkout(home / "c"))
    assert not out["ok"] and "red: prompt_recall_outcomes" in out["detail"]
    # An owner-only red beside a real one still refuses, naming the real one.
    _doctor_says(monkeypatch, {**_BASE, "prompt_recall_outcomes_ok": False, "outbox_ok": False,
                               "prompt_recall_outcomes": {"ok": False, "gate_ok": True}})
    out = pm.gate_doctor(_checkout(home / "c"))
    assert not out["ok"] and out["detail"] == "red: outbox"


# ---- 2. the replay gate ------------------------------------------------------


def _summary(reason: str, ms: float, on: bool) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (f"{stamp} [khipu-prompt-recall] session=s1 reason={reason} ms={ms} hits=['episode:3'] "
            f"legs=['lexical', 'cosine'] degraded=None" + (" routing=full" if on else ""))


def _timed(runs: list, timing):
    """A fake hook whose answer for each run is ``timing(who, on, runs)``
    (``runs`` is every hook run so far, this one last, as ``(who, on)``)."""
    def fake_run(argv, *, env=None, input=None, cwd=None, timeout=120):
        if argv[1:2] == ["-c"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"push": True}) + "\n", "")
        on = env.get("KHIPU_FEATURE_ROUTING_PUSH") == "1"
        who = "cand" if "/candidate/packages/" in argv[0] else "live"
        runs.append((who, on))
        reason, ms = timing(who, on, runs)
        log = Path(env["KHIPU_CAPTURE_HOME"]) / "logs" / "prompt-recall.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(_summary(reason, ms, on) + "\n")
        return subprocess.CompletedProcess(argv, 0, '{"hookSpecificOutput": {}}\n', "")
    return fake_run


def _queries(home, n: int) -> Path:
    from tests.test_promote import _many_queries
    return _many_queries(home, n)


def test_i_equally_slow_under_load_that_arrives_between_runs_passes(home, monkeypatch):
    # The first live run of each prompt is taken before the load arrives;
    # from then on live and the candidate are equally slow, both over the
    # 1.2 s budget. The old order (live's earlier run as the only reference)
    # failed this on every prompt: 1500 ms against 800 ms.
    candidate = _queries(home, 3)
    runs: list = []

    def timing(who, on, runs):
        first_of_prompt = len(runs) == 1 or runs[-2] == ("live", True)
        return "ok", 800.0 if first_of_prompt else 1500.0

    monkeypatch.setattr(pm, "_run", _timed(runs, timing))
    out = pm.gate_recall_replay(candidate)
    assert out["ok"], out
    assert "shared slowness, not the candidate" in out["detail"]
    assert "median 1500 ms, live 1500 ms" in out["detail"]
    # Five hook runs a prompt against four before: well inside half again.
    assert len(runs) == 15
    assert runs[:5] == [("live", False), ("cand", False), ("live", False), ("cand", True), ("live", True)]


def test_ii_a_candidate_400_ms_slower_than_live_fails_and_says_against_which_run(home, monkeypatch):
    candidate = _queries(home, 3)

    def timing(who, on, runs):
        if who == "cand":
            return "ok", 1400.0
        # Live just after a candidate run is the slower of the two beside it.
        return "ok", 1000.0 if len(runs) > 1 and runs[-2][0] == "cand" else 950.0

    monkeypatch.setattr(pm, "_run", _timed([], timing))
    out = pm.gate_recall_replay(candidate)
    assert not out["ok"], out
    for i in (1, 2, 3):
        assert (f"prompt {i}: 1400 ms > 1200 ms, 400 ms slower than live's run just after it (1000 ms)"
                in out["detail"])


def test_ii_a_routing_on_run_400_ms_slower_than_live_fails(home, monkeypatch):
    candidate = _queries(home, 1)

    def timing(who, on, runs):
        return "ok", (1400.0 if who == "cand" and on else 1000.0)

    monkeypatch.setattr(pm, "_run", _timed([], timing))
    out = pm.gate_recall_replay(candidate)
    assert not out["ok"], out
    assert "with routing on, 1400 ms > 1200 ms, 400 ms slower than" in out["detail"]


def test_iii_a_candidate_timeout_beside_two_quick_live_runs_fails(home, monkeypatch):
    candidate = _queries(home, 1)

    def timing(who, on, runs):
        return ("timeout>1.2s", 1250.0) if who == "cand" and not on else ("ok", 400.0)

    monkeypatch.setattr(pm, "_run", _timed([], timing))
    out = pm.gate_recall_replay(candidate)
    assert not out["ok"], out
    assert "prompt 1: timeout>1.2s" in out["detail"]


def test_iii_a_candidate_timeout_beside_a_live_run_near_the_budget_is_forgiven(home, monkeypatch):
    candidate = _queries(home, 1)

    def timing(who, on, runs):
        if who == "cand":
            return ("timeout>1.2s", 1210.0) if not on else ("ok", 900.0)
        # The live run just after the candidate's answered within the slack.
        return "ok", 1050.0 if len(runs) > 1 and runs[-2][0] == "cand" else 400.0

    monkeypatch.setattr(pm, "_run", _timed([], timing))
    out = pm.gate_recall_replay(candidate)
    assert out["ok"], out


def test_iv_without_a_live_hook_the_budget_alone_applies(home, monkeypatch):
    candidate = _queries(home, 1)
    (home / ".config" / "khipu" / "bin" / "khipu-prompt-recall").unlink()
    monkeypatch.setattr(pm, "_live_code_root", lambda: None)
    runs: list = []
    monkeypatch.setattr(pm, "_run", _timed(runs, lambda who, on, runs: ("ok", 1300.0)))
    out = pm.gate_recall_replay(candidate)
    assert not out["ok"], out
    assert "prompt 1: 1300 ms > 1200 ms, with no live run to compare with" in out["detail"]
    assert {who for who, _on in runs} == {"cand"}
    monkeypatch.setattr(pm, "_run", _timed([], lambda who, on, runs: ("ok", 1100.0)))
    assert pm.gate_recall_replay(candidate)["ok"]


def test_iv_a_live_run_that_logged_no_time_leaves_the_budget_alone(home, monkeypatch):
    candidate = _queries(home, 1)

    def fake_run(argv, *, env=None, input=None, cwd=None, timeout=120):
        if argv[1:2] == ["-c"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"push": True}) + "\n", "")
        on = env.get("KHIPU_FEATURE_ROUTING_PUSH") == "1"
        if "/candidate/packages/" in argv[0]:
            log = Path(env["KHIPU_CAPTURE_HOME"]) / "logs" / "prompt-recall.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(_summary("ok", 1300.0, on) + "\n")
        return subprocess.CompletedProcess(argv, 0, '{"hookSpecificOutput": {}}\n', "")

    monkeypatch.setattr(pm, "_run", fake_run)
    out = pm.gate_recall_replay(candidate)
    assert not out["ok"], out
    assert "1300 ms > 1200 ms, with no live run to compare with" in out["detail"]
