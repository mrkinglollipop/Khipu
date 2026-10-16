"""Offline and self-healing, slice 7 (docs/plans/2026-10-16-offline-and-self-healing.md, E):
the promotion gate after an outage. Reds whose evidence is all from a time the
hub was out of reach do not refuse; the hub steps wait for the hub; a refusal
that is only the hub is queued and retried; the owner can pass over a check."""
from __future__ import annotations

import io
import json
import os
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from khipu import integrations
from khipu import promote as pm


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


def _doctor_says(monkeypatch, out: dict, rc: int = 2) -> None:
    monkeypatch.setattr(pm, "_run", lambda argv, **kw: subprocess.CompletedProcess(argv, rc, json.dumps(out), ""))


_BASE = {"ok": False, "imports": {"failed": {}}, "config": {"ok": True}, "dsn_present": True}


# ---- the doctor step ---------------------------------------------------------


def test_a_red_from_an_offline_period_does_not_refuse(home, monkeypatch):
    _doctor_says(monkeypatch, {**_BASE, "outbox_ok": False, "outbox": {"pending": 5, "offline_only": True}})
    out = pm.gate_doctor(_checkout(home / "c"))
    assert out["ok"], out
    assert out["offline_reds"] == ["outbox"]
    assert "out of reach" in out["detail"]


def test_a_red_with_other_evidence_still_refuses(home, monkeypatch):
    _doctor_says(monkeypatch, {**_BASE, "outbox_ok": False, "outbox": {"pending": 5, "offline_only": False},
                               "degraded_rate_ok": True})
    out = pm.gate_doctor(_checkout(home / "c"))
    assert not out["ok"]
    assert "red: outbox" in out["detail"]


def test_an_offline_red_does_not_hide_a_real_one(home, monkeypatch):
    _doctor_says(monkeypatch, {**_BASE, "outbox_ok": False, "outbox": {"offline_only": True},
                               "bundle_seal_ok": False, "bundle_seal": {"ok": False}})
    out = pm.gate_doctor(_checkout(home / "c"))
    assert not out["ok"]
    assert out["detail"] == "red: bundle_seal"


def test_a_failed_import_is_never_excused(home, monkeypatch):
    _doctor_says(monkeypatch, {**_BASE, "imports": {"failed": {"khipu.db": "ImportError: x"}},
                               "outbox_ok": False, "outbox": {"offline_only": True}})
    assert not pm.gate_doctor(_checkout(home / "c"))["ok"]


def test_the_owner_override_passes_over_a_check_and_the_record_names_it(home, monkeypatch, tmp_path):
    pm.set_gate_ignore("degraded_rate")
    assert pm.gate_ignore() == ["degraded_rate"]
    _doctor_says(monkeypatch, {**_BASE, "degraded_rate_ok": False, "degraded_rate": {"ok": False}})
    monkeypatch.setattr(pm, "_repo", lambda: _checkout(tmp_path / "repo"))
    out = pm.gate_only("doctor")
    assert out["ok"], out
    assert out["gate"][0]["ignored_reds"] == ["degraded_rate"]
    logged = pm.records()[-1]
    assert logged["gate"][0]["ignored_reds"] == ["degraded_rate"]
    assert "owner" in logged["gate"][0]["detail"]
    pm.set_gate_ignore(None)
    assert not pm.gate_doctor(_checkout(home / "c"))["ok"]


def test_the_override_takes_only_checks_the_gate_runs(home):
    with pytest.raises(ValueError, match="not a check the gate runs: imports"):
        pm.set_gate_ignore("outbox,imports")
    assert pm.gate_ignore() == []
    assert "outbox" in pm.gate_ignorable() and "jobs" not in pm.gate_ignorable()


def test_the_override_is_a_setting_the_cli_writes_and_shows(home):
    from khipu import cli

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["config", "--set", "promote_gate_ignore", "outbox, degraded_rate"])
    assert rc == 0
    assert json.loads(buf.getvalue())["promote_gate_ignore"]["value"] == ["outbox", "degraded_rate"]
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["config", "--set", "promote_gate_ignore", "nope"])
    assert rc == 2 and "nope" in json.loads(buf.getvalue())["error"]
    with redirect_stdout(io.StringIO()):
        assert cli.main(["config", "--unset", "promote_gate_ignore"]) == 0
    assert pm.gate_ignore() == []


# ---- the hub steps and the queue ---------------------------------------------


class _Hub:
    """A fake hub: each probe pops the next state, the last one repeats."""

    def __init__(self, *states: bool | None) -> None:
        self.states = list(states)
        self.probes = 0

    def __call__(self, **_kw) -> bool | None:
        self.probes += 1
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]


def _steps(monkeypatch, recall) -> list[str]:
    ran: list[str] = []

    def ok(name):
        return lambda c: (ran.append(name), {"ok": True, "ms": 1, "detail": "ok"})[1]

    monkeypatch.setattr(pm, "GATE_STEPS", {"doctor": ok("doctor"), "recall": lambda c: (ran.append("recall"), recall())[1],
                                           "capture": ok("capture"), "version": ok("version")})
    return ran


def test_a_hub_step_waits_for_the_hub_and_runs_again(home, monkeypatch):
    answers = [{"ok": False, "ms": 1, "detail": "OperationalError: connection refused"},
               {"ok": True, "ms": 1, "detail": "ok"}]
    ran = _steps(monkeypatch, lambda: answers.pop(0))
    hub = _Hub(False, False, True)
    monkeypatch.setattr(pm, "hub_reachable", hub)
    monkeypatch.setattr(pm.time, "sleep", lambda s: None)
    results = pm.run_gate(_checkout(home / "c"))
    assert [r["ok"] for r in results] == [True, True, True, True]
    assert ran == ["doctor", "recall", "recall", "capture", "version"]
    assert "waited_for_hub_s" in results[1]


def test_a_hub_that_never_answers_marks_the_step_after_a_bounded_wait(home, monkeypatch):
    _steps(monkeypatch, lambda: {"ok": False, "ms": 1, "detail": "connection refused"})
    monkeypatch.setattr(pm, "hub_reachable", _Hub(False))
    monkeypatch.setattr(pm, "hub_wait_s", lambda: 0)
    results = pm.run_gate(_checkout(home / "c"))
    assert results[-1]["step"] == "recall" and results[-1]["hub_unreachable"] is True
    assert "could not be reached" in results[-1]["detail"]


def test_a_real_failure_with_the_hub_up_does_not_wait(home, monkeypatch):
    _steps(monkeypatch, lambda: {"ok": False, "ms": 1, "detail": "empty answer where live had 3 hit(s)"})
    monkeypatch.setattr(pm, "hub_reachable", _Hub(True))
    monkeypatch.setattr(pm, "_wait_for_hub", lambda s: pytest.fail("must not wait with the hub up"))
    results = pm.run_gate(_checkout(home / "c"))
    assert "hub_unreachable" not in results[-1]


def _promotable(home, monkeypatch, tmp_path):
    a, _b = pm.slots()
    _checkout(a)
    assert pm.flip(a)["ok"]
    monkeypatch.setattr(pm, "_repo", lambda: tmp_path)
    monkeypatch.setattr(pm, "_fetch_and_resolve", lambda repo, ref: {"ok": True, "commit": "c0ffee"})
    monkeypatch.setattr(pm, "_prepare_slot", lambda slot, commit, repo: (_checkout(slot), {"ok": True})[1])
    monkeypatch.setattr(pm, "_head", lambda root: "headsha" if root else None)
    monkeypatch.setattr(pm, "precompile", lambda root, **kw: {"ok": True})
    monkeypatch.setattr(pm, "_before_flip", lambda: {"ok": True})
    monkeypatch.setattr(pm, "_after_flip", lambda: {"kicked": []})
    monkeypatch.setattr(pm, "hub_wait_s", lambda: 0)
    return a


def test_a_refusal_that_is_only_the_hub_queues_and_retries_once_the_hub_answers(home, monkeypatch, tmp_path):
    a = _promotable(home, monkeypatch, tmp_path)
    hub_up = {"now": False}
    monkeypatch.setattr(pm, "hub_reachable", lambda **kw: hub_up["now"])

    def recall():
        return ({"ok": True, "ms": 1, "detail": "ok"} if hub_up["now"]
                else {"ok": False, "ms": 1, "detail": "connection refused"})

    _steps(monkeypatch, recall)
    out = pm.promote("origin/main")
    assert not out["ok"] and out["queued"]["ref"] == "origin/main"
    assert out["queued"]["commit"] == "c0ffee"
    assert os.readlink(pm.live_link()) == str(a)
    assert pm.queued()["attempts"] == 1
    assert "runs again by itself" in pm.format_result(out)

    still = pm.retry_queued()
    assert still["waiting"] is True and pm.queued() is not None

    hub_up["now"] = True
    done = pm.retry_queued()
    assert done["ok"], done
    assert pm.queued() is None
    assert done["retry_of"] == out["queued"]["queued_at"]
    assert done["ref"] == "c0ffee"
    assert pm.records()[-1]["ok"] is True
    assert pm.status()["queued"] is None


def test_a_retry_refused_for_another_reason_leaves_nothing_queued(home, monkeypatch, tmp_path):
    _promotable(home, monkeypatch, tmp_path)
    pm._queue("origin/main", "c0ffee", "recall: connection refused", None)
    monkeypatch.setattr(pm, "hub_reachable", lambda **kw: True)
    _steps(monkeypatch, lambda: {"ok": False, "ms": 1, "detail": "empty answer where live had 3 hit(s)"})
    out = pm.retry_queued()
    assert not out["ok"] and "queued" not in out
    assert pm.queued() is None


def test_nothing_queued_is_a_quiet_no_op(home):
    assert pm.retry_queued() == {"ok": True, "action": "retry-queued", "queued": None}
    assert pm.kick_queued_retry() is None


def test_the_drain_kicks_a_queued_retry_in_its_own_session_once(home, monkeypatch):
    pm._queue("origin/main", "c0ffee", "recall: connection refused", None)
    started = []

    class _Proc:
        pid = 4242

    def fake_popen(argv, **kw):
        started.append((argv, kw))
        return _Proc()

    monkeypatch.setattr(pm.subprocess, "Popen", fake_popen)
    first = pm.kick_queued_retry()
    assert first == {"kicked": True, "pid": 4242}
    argv, kw = started[0]
    assert argv[-3:] == ["promote", "--retry-queued", "--json"]
    assert kw["start_new_session"] is True
    assert pm.kick_queued_retry() == {"kicked": False, "reason": "kicked recently"}
    assert len(started) == 1


def test_the_cli_runs_the_retry(home, monkeypatch):
    from khipu import cli

    monkeypatch.setattr(pm, "retry_queued", lambda: {"ok": True, "action": "retry-queued", "queued": None})
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["promote", "--retry-queued"])
    assert rc == 0 and "nothing is queued" in buf.getvalue()


def test_doctor_shows_a_waiting_promotion(home):
    from khipu import cli

    assert cli._promotion_block() == {"queued": None}
    pm._queue("origin/main", "c0ffee", "recall: connection refused", None)
    assert cli._promotion_block()["queued"]["ref"] == "origin/main"


# ---- the outbox evidence -----------------------------------------------------


def test_the_outbox_says_when_every_waiting_capture_is_waiting_on_the_hub(home):
    from khipu import outbox

    outbox.enqueue({"session_id": "test:a", "summary": "one", "ts": "2026-10-16T12:00:00Z"},
                   reason="OperationalError")
    st = outbox.status()
    assert st["pending"] == 1 and st["offline_only"] is True
    outbox.enqueue({"session_id": "test:b", "summary": "two", "ts": "2026-10-16T12:01:00Z"},
                   reason="ValueError")
    st = outbox.status()
    assert st["pending"] == 2 and st["offline"] == 1 and st["offline_only"] is False


def test_an_empty_outbox_is_not_offline_only(home):
    from khipu import outbox

    assert outbox.status()["offline_only"] is False
