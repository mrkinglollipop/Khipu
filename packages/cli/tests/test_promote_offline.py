"""Offline and self-healing, slice 7 (docs/plans/2026-10-16-offline-and-self-healing.md, E):
the promotion gate after an outage. Reds whose evidence is all from a time the
hub was out of reach do not refuse; the hub steps wait for the hub; a refusal
that is only the hub is queued and retried; the owner can have the next update
skip a red check once ("Update anyway")."""
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


def test_a_statement_timeout_from_the_hub_is_not_offline_evidence(home, monkeypatch, tmp_path):
    # A SQL statement timeout is not proof the hub cannot be reached.
    from datetime import datetime, timezone

    from khipu import outbox

    job = {"queued_at": datetime.now(timezone.utc).isoformat(), "payload": {}, "attempts": 0,
           "last_error": "QueryCanceled: canceling statement due to statement timeout"}
    path = tmp_path / "capture.json"
    path.write_text(json.dumps(job))
    monkeypatch.setattr(outbox, "jobs", lambda: [path])
    monkeypatch.setattr(outbox, "dead_jobs", lambda: [])
    monkeypatch.setattr(outbox, "quarantine_dir", lambda: tmp_path / "absent")
    result = outbox.status()
    _doctor_says(monkeypatch, {**_BASE, "outbox_ok": False, "outbox": result})
    gate = pm.gate_doctor(_checkout(home / "c"))
    assert not gate["ok"], gate


# ---- the owner's one-time override ("Update anyway") -------------------------


def _owner(monkeypatch, via: str | None = "app") -> None:
    from khipu import routing

    monkeypatch.setattr(routing, "_owner_via", lambda args, intent=None: via)


def test_the_owner_override_skips_a_check_once_and_the_record_names_it(home, monkeypatch, tmp_path):
    _promotable(home, monkeypatch, tmp_path)
    monkeypatch.setattr(pm, "hub_reachable", lambda **kw: True)
    _doctor_says(monkeypatch, {**_BASE, "degraded_rate_ok": False, "degraded_rate": {"ok": False}})
    for name in ("recall", "capture", "version"):
        monkeypatch.setitem(pm.GATE_STEPS, name, lambda c: {"ok": True, "ms": 1, "detail": "ok"})
    refused = pm.promote("origin/main")
    assert not refused["ok"] and refused["gate"][0]["red_checks"] == ["degraded_rate"]
    assert pm.gate_ignore_status()["blocked_by"] == ["degraded_rate"]

    pm.set_gate_ignore("degraded_rate", by="app")
    assert pm.gate_ignore() == ["degraded_rate"]
    out = pm.promote("origin/main")
    assert out["ok"], out
    assert pm.active_slot() == Path(out["slot"])
    assert out["gate"][0]["ignored_reds"] == ["degraded_rate"]
    logged = pm.records()[-1]
    assert logged["gate"][0]["ignored_reds"] == ["degraded_rate"]
    assert "skipped once" in logged["gate"][0]["detail"] and "owner" in logged["gate"][0]["detail"]
    assert logged["override"]["checks"] == ["degraded_rate"] and logged["override"]["by"] == "app"
    assert logged["override"]["cleared"] is True
    # Used once, then cleared: the next attempt refuses on the same red.
    assert pm.gate_ignore() == []
    assert not pm.promote("origin/main")["ok"]


@pytest.mark.parametrize("failed_step", ["doctor", "recall", "capture", "version"])
def test_a_failed_gate_keeps_the_owner_override(home, monkeypatch, tmp_path, failed_step):
    live = _promotable(home, monkeypatch, tmp_path)
    monkeypatch.setattr(pm, "hub_reachable", lambda **kw: True)
    _doctor_says(monkeypatch, {**_BASE, "nightly_steps_ok": False,
                               "schema_migrations_ok": failed_step != "doctor"})
    for name in ("recall", "capture", "version"):
        monkeypatch.setitem(pm.GATE_STEPS, name,
                            lambda c, name=name: {"ok": name != failed_step, "ms": 1, "detail": name})
    pm.set_gate_ignore("nightly_steps", by="app")
    override = pm.gate_override()

    out = pm.promote("origin/main")

    assert not out["ok"] and out["failed_step"] == failed_step
    assert pm.active_slot() == live
    assert pm.queued() is None
    assert pm.gate_override() == override
    logged = pm.records()[-1]
    assert logged["gate"][0]["override"] == override
    assert logged["override"] == {**override, "skipped": [] if failed_step == "doctor" else ["nightly_steps"],
                                  "cleared": False}
    if failed_step == "doctor":
        assert logged["gate"][0]["red_checks"] == ["schema_migrations"]
    monkeypatch.setattr(pm, "_override_age_s", lambda _: pm.OVERRIDE_TTL_S + 60)
    assert pm.gate_override() is None


@pytest.mark.parametrize("ignored_red", [False, True])
def test_a_flip_failure_consumes_only_an_override_that_passed_the_gate(home, monkeypatch, tmp_path, ignored_red):
    live = _promotable(home, monkeypatch, tmp_path)
    _doctor_says(monkeypatch, {**_BASE, "ok": not ignored_red, "nightly_steps_ok": not ignored_red},
                 rc=0 if not ignored_red else 2)
    for name in ("recall", "capture", "version"):
        monkeypatch.setitem(pm.GATE_STEPS, name, lambda c: {"ok": True, "ms": 1, "detail": "ok"})
    monkeypatch.setattr(pm, "flip", lambda slot: {"ok": False, "error": "cannot replace link"})
    pm.set_gate_ignore("nightly_steps", by="app")
    override = pm.gate_override()

    out = pm.promote("origin/main")

    assert not out["ok"] and out["error"] == "flip: cannot replace link"
    assert pm.active_slot() == live
    assert out["override"]["cleared"] is ignored_red
    assert pm.gate_override() == (None if ignored_red else override)


def test_a_new_override_set_during_the_run_is_never_cleared(home, monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone

    _promotable(home, monkeypatch, tmp_path)
    instant = datetime.now(timezone.utc).replace(microsecond=100)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant

    monkeypatch.setattr(pm, "datetime", Clock)
    _doctor_says(monkeypatch, {**_BASE, "nightly_steps_ok": False})
    for name in ("recall", "capture", "version"):
        monkeypatch.setitem(pm.GATE_STEPS, name, lambda c: {"ok": True, "ms": 1, "detail": "ok"})
    pm.set_gate_ignore("nightly_steps", by="app")
    original = pm.gate_override()
    replacement = None
    flip = pm.flip

    def replace_then_flip(slot):
        nonlocal instant, replacement
        instant += timedelta(microseconds=1)
        pm.set_gate_ignore("nightly_steps", by="app")
        replacement = pm.gate_override()
        return flip(slot)

    monkeypatch.setattr(pm, "flip", replace_then_flip)
    out = pm.promote("origin/main")

    assert out["ok"] and pm.active_slot() == Path(out["slot"])
    assert out["override"] == {**original, "skipped": ["nightly_steps"], "cleared": False}
    assert replacement["set_at"] != original["set_at"]
    assert pm.gate_override() == replacement


def test_an_unused_override_lapses_after_a_day(home, monkeypatch):
    pm.set_gate_ignore("outbox", by="app")
    assert pm.gate_ignore() == ["outbox"]
    later = pm.OVERRIDE_TTL_S + 60
    monkeypatch.setattr(pm, "_override_age_s", lambda stored: later)
    assert pm.gate_ignore() == [] and pm.gate_override() is None
    _doctor_says(monkeypatch, {**_BASE, "outbox_ok": False, "outbox": {"offline_only": False}})
    assert not pm.gate_doctor(_checkout(home / "c"))["ok"]


def test_an_override_kept_while_its_promotion_waits_for_the_hub(home, monkeypatch, tmp_path):
    _promotable(home, monkeypatch, tmp_path)
    monkeypatch.setattr(pm, "hub_reachable", lambda **kw: False)
    _doctor_says(monkeypatch, {**_BASE, "outbox_ok": False, "outbox": {"offline_only": False}})
    monkeypatch.setitem(pm.GATE_STEPS, "recall", lambda c: {"ok": False, "ms": 1, "detail": "connection refused"})
    pm.set_gate_ignore("outbox", by="terminal")
    out = pm.promote("origin/main")
    assert out["queued"] and out["override"]["cleared"] is False
    assert pm.gate_ignore() == ["outbox"]


def test_a_standing_list_left_from_before_is_never_read(home):
    from khipu.config import mutate_config

    with mutate_config() as data:
        data[pm.GATE_IGNORE_KEY] = ["outbox", "degraded_rate"]
    assert pm.gate_ignore() == [] and pm.gate_override() is None


def test_the_override_takes_only_checks_the_gate_runs(home):
    with pytest.raises(ValueError, match="not a check the gate runs: imports"):
        pm.set_gate_ignore("outbox,imports", by="app")
    assert pm.gate_ignore() == []
    assert "outbox" in pm.gate_ignorable() and "jobs" not in pm.gate_ignorable()


def test_the_override_is_a_setting_the_cli_writes_and_shows(home, monkeypatch):
    from khipu import cli

    _owner(monkeypatch, "app")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["config", "--from-app", "--set", "promote_gate_ignore", "outbox, degraded_rate"])
    assert rc == 0
    shown = json.loads(buf.getvalue())["promote_gate_ignore"]
    assert shown["value"] == ["outbox", "degraded_rate"] and shown["by"] == "app" and shown["expires_at"]
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["config", "--from-app", "--set", "promote_gate_ignore", "nope"])
    assert rc == 2 and "nope" in json.loads(buf.getvalue())["error"]
    with redirect_stdout(io.StringIO()):
        assert cli.main(["config", "--from-app", "--unset", "promote_gate_ignore"]) == 0
    assert pm.gate_ignore() == []


def test_override_refuses_non_owner(home, monkeypatch):
    from khipu import cli

    _owner(monkeypatch, None)
    with redirect_stdout(io.StringIO()):
        rc = cli.main(["config", "--set", "promote_gate_ignore", "outbox"])
    assert rc == 2 and pm.gate_ignore() == [], f"rc={rc}, ignore={pm.gate_ignore()}"
    pm.set_gate_ignore("outbox", by="app")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["config", "--unset", "promote_gate_ignore"])
    assert rc == 2 and json.loads(buf.getvalue())["code"] == "owner_only"
    assert pm.gate_ignore() == ["outbox"]


def test_every_writer_of_the_override_goes_through_the_owner_gate():
    """The override's key is written by promote.py alone (set, and the clear
    after a use), and set_gate_ignore has one caller outside it: the config
    command's override branch, which asks the owner first."""
    import ast

    package = Path(pm.__file__).resolve().parent
    writers, callers = [], []
    for path in sorted(package.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        if "promote_gate_ignore" in source and path.name != "promote.py":
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value == "promote_gate_ignore":
                    writers.append(path.name)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, (ast.Attribute, ast.Name))
                        and getattr(node.func, "attr", getattr(node.func, "id", None))
                        in ("set_gate_ignore", "clear_gate_override")):
                    callers.append((path.name, fn.name))
    # cli.py names the key only to route `config --set/--unset` to its branch;
    # owner_act.py only to build that same command for khipu_owner_act.
    assert set(writers) <= {"cli.py", "owner_act.py"}, writers
    outside = {c for c in callers if c[0] != "promote.py"}
    # Owner direction's Undo puts the override back as it was; it runs only
    # from ``owner-direction undo``, after the owner gate.
    assert outside == {("cli.py", "_config_gate_override"), ("owner_direction.py", "_undo_gate_ignore")}, outside
    import inspect

    from khipu import cli, owner_direction

    body = inspect.getsource(cli._config_gate_override)
    assert body.index("_owner_only(") < body.index("set_gate_ignore(")
    body = inspect.getsource(owner_direction.run_cli)
    assert body.index("routing._owner_via(args, want)") < body.index("out = undo(args.id, via)")


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


def test_queued_promotion_survives_retry_crash(home, monkeypatch):
    entry = pm._queue("original-ref", "fake-commit", "hub unreachable", None)
    monkeypatch.setattr(pm, "hub_reachable", lambda **kw: True)

    def crash(*a):
        raise SystemExit("simulated death during candidate preparation")

    monkeypatch.setattr(pm, "_promote_locked", crash)
    with pytest.raises(SystemExit):
        pm.retry_queued()
    assert pm.queued() == entry, pm.queued()


def test_the_queue_is_removed_at_one_point_after_the_run():
    """``_clear_queue`` has one caller, in ``promote``, after
    ``_promote_locked`` returned."""
    import inspect
    import re

    src = inspect.getsource(pm)
    assert len(re.findall(r"(?<!def )\b_clear_queue\(", src)) == 1
    body = inspect.getsource(pm.promote)
    assert body.index("_promote_locked(") < body.index("_clear_queue()")


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


# ---- crash safety and the override held by a queued promotion ----------------


def test_promotion_retry_survives_crash_while_replacing_queue(home, monkeypatch):
    pm._queue("original-ref", "fake-commit", "hub unreachable", None)
    monkeypatch.setattr(pm, "hub_reachable", lambda **kw: True)
    monkeypatch.setattr(pm, "_promote_locked", lambda *a: {"ok": False, "gate": [
        {"step": "recall", "ok": False, "hub_unreachable": True}], "commit": "fake-commit"})

    def die(*a):
        raise SystemExit("crash before replacement queue is written")

    monkeypatch.setattr(pm, "_queue", die)
    with pytest.raises(SystemExit):
        pm.retry_queued()
    assert pm.queued() is not None, "queued promotion was deleted before its replacement existed"


def _waiting_on_hub(monkeypatch, override) -> None:
    monkeypatch.setattr(pm, "_promote_locked", lambda *a: {"ok": False, "gate": [
        {"step": "doctor", "ok": True, "override": override, "ignored_reds": ["outbox"]},
        {"step": "recall", "ok": False, "hub_unreachable": True}], "commit": "fake-commit"})


def test_queued_override_survives_a_day_outage(home, monkeypatch):
    pm.set_gate_ignore("outbox", by="app")
    override = pm.gate_override()
    _waiting_on_hub(monkeypatch, override)
    out = pm.promote("original-ref")
    assert out["queued"] and not out["override"]["cleared"]
    monkeypatch.setattr(pm, "_override_age_s", lambda _: pm.OVERRIDE_TTL_S + 60)
    assert pm.gate_ignore() == ["outbox"], "queued retry lost its owner approval after 24h"
    held = pm.gate_ignore_status()
    assert held["held_by_queue"] is True and held["expires_at"] is None


def test_an_old_override_with_nothing_queued_still_lapses(home, monkeypatch):
    pm.set_gate_ignore("outbox", by="app")
    monkeypatch.setattr(pm, "_override_age_s", lambda _: pm.OVERRIDE_TTL_S + 60)
    assert pm.queued() is None
    assert pm.gate_ignore() == []
    assert pm.gate_ignore_status()["held_by_queue"] is False


def test_a_queue_holds_only_the_override_it_was_queued_with(home, monkeypatch):
    pm.set_gate_ignore("outbox", by="app")
    _waiting_on_hub(monkeypatch, pm.gate_override())
    assert pm.promote("original-ref")["queued"]
    # A newer choice made meanwhile carries a different stamp and lapses on its own clock.
    pm.set_gate_ignore("outbox", by="terminal")
    from khipu.config import mutate_config

    with mutate_config() as data:
        data[pm.GATE_IGNORE_KEY]["set_at"] = "2020-01-01T00:00:00Z"
    assert pm.gate_ignore() == []


def test_a_queued_override_is_cleared_when_the_retry_finishes(home, monkeypatch):
    pm.set_gate_ignore("outbox", by="app")
    override = pm.gate_override()
    _waiting_on_hub(monkeypatch, override)
    assert pm.promote("original-ref")["queued"]
    monkeypatch.setattr(pm, "hub_reachable", lambda **kw: True)
    monkeypatch.setattr(pm, "_promote_locked", lambda *a: {"ok": True, "gate": [
        {"step": "doctor", "ok": True, "override": override, "ignored_reds": ["outbox"]}], "commit": "fake-commit"})
    done = pm.retry_queued()
    assert done["ok"] and done["override"]["cleared"] is True
    assert pm.queued() is None and pm.gate_ignore() == []


def test_a_doctor_outage_before_recall_is_queued(home, monkeypatch, tmp_path):
    """A doctor red whose evidence is all from an outage passes the doctor
    step, and the hub steps then wait and queue the promotion."""
    _doctor_says(monkeypatch, {**_BASE, "outbox_ok": False, "outbox": {"pending": 5, "offline_only": True}})
    monkeypatch.setattr(pm, "GATE_STEPS", {
        "doctor": pm.gate_doctor,
        "recall": lambda c: {"ok": False, "ms": 1, "detail": "connection refused"}})
    monkeypatch.setattr(pm, "hub_reachable", lambda **_: False)
    monkeypatch.setattr(pm, "hub_wait_s", lambda: 0)
    monkeypatch.setattr(pm, "_promote_locked", lambda ref, t: {
        "ok": False, "gate": pm.run_gate(_checkout(tmp_path / "c")), "commit": "fake"})
    out = pm.promote("fake")
    assert out.get("queued"), out
    doctor = out["gate"][0]
    assert doctor["step"] == "doctor" and doctor["ok"] and doctor["offline_reds"] == ["outbox"]
    assert out["gate"][1]["hub_unreachable"] is True
