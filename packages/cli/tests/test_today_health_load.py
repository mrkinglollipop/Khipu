"""Temporary health read failures preserve bounded evidence, never hide faults."""
from datetime import timedelta
import subprocess
import pytest
from khipu import health_five, jobs, launchd_gen, paths, routing_health
from tests.test_health_five import NOW, report


def unread(kind):
    if kind == "delivery":
        block = {"status": "unknown", "missing": ["cross-Mac caught-up cursor"], "reasons": []}
        return report(routing_delivery_ok=routing_health.delivery_ok(block), routing_delivery=block)
    return report(nightly_steps_ok=False, nightly_steps={"notes_reconcile_ok": {"ok": False, "state": "check_failed", "error": "TimeoutExpired: launchctl print"}})


@pytest.mark.parametrize("kind,area", [("delivery", 2), ("notes", 4)])
def test_slow_read_retains_last_answer_then_expires(kind, area):
    good = health_five.build(report(), now=NOW, mac="Test Mac")
    kept = {good["areas"][area]["key"]: {"at": good["checked_at"], "row": good["areas"][area]}}
    once = health_five.build(unread(kind), now=NOW + timedelta(minutes=5), mac="Test Mac", kept=kept)
    assert once["headline"] == "Everything is working."
    assert once["areas"][area] == good["areas"][area]
    late = health_five.build(unread(kind), now=NOW + timedelta(minutes=16), mac="Test Mac", kept=kept)
    assert late["areas"][area]["state"] == "unknown"
    assert "could not" in late["areas"][area]["line"].lower()
    assert "Failing" not in late["headline"] and health_five.DELIVERY_LINE not in late["headline"]


def test_reported_fault_is_immediate_even_with_unread():
    r = unread("notes")
    r.update(jobs_ok=False, job_failures=["notes_watch"])
    d = health_five.build(r, now=NOW, mac="Test Mac")
    assert d["areas"][4]["state"] == "warn"
    assert d["areas"][4]["line"] == "Failing: notes_watch."
    r = unread("delivery")
    r["routing_delivery"].update(status="red", reasons=["cross-mac-stale"])
    assert health_five.build(r, now=NOW, mac="Test Mac")["areas"][2]["line"] == health_five.DELIVERY_LINE


@pytest.mark.parametrize("initial_loaded", [False, True])
def test_launchctl_timeout_is_unread_not_missing(initial_loaded, monkeypatch):
    monkeypatch.setattr(jobs, "_job_entry", lambda _: {"plist_loaded": True if initial_loaded else jobs._plist_loaded(jobs.PLIST_NOTES_WATCH)})
    def timeout(*a, **kw): raise subprocess.TimeoutExpired("launchctl", 3)
    monkeypatch.setattr(jobs.subprocess, "run", timeout)
    row = jobs.notes_watch_health()
    assert row["state"] == "check_failed"
    r = report(nightly_steps_ok=False, nightly_steps={"notes_reconcile_ok": row})
    assert health_five.build(r, now=NOW, mac="Test Mac")["areas"][4]["state"] == "unknown"


def test_first_launchctl_timeout_can_recover_on_state_read(monkeypatch):
    monkeypatch.setattr(jobs, "_job_entry", lambda _: {"plist_loaded": jobs._plist_loaded(jobs.PLIST_NOTES_WATCH)})
    calls = []
    def slow_once(*a, **kw):
        calls.append(a)
        if len(calls) == 1: raise subprocess.TimeoutExpired("launchctl", 3)
        return subprocess.CompletedProcess(a, 0, "last exit code = 0\n", "")
    monkeypatch.setattr(jobs.subprocess, "run", slow_once)
    monkeypatch.setattr(launchd_gen, "watch_list_drift", lambda: {"unwatched": []})
    assert jobs.notes_watch_health()["ok"] is True


@pytest.mark.parametrize("kind,area", [("delivery", 2), ("notes", 4)])
def test_command_persists_evidence_without_renewing_unread_age(kind, area, monkeypatch, tmp_path, capsys):
    import json
    from datetime import datetime
    from khipu import macs
    clock = [NOW]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return clock[0]
    monkeypatch.setattr(health_five, "datetime", Clock)
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(health_five, "mac_label", lambda: "Test Mac")
    monkeypatch.setattr(macs, "others", lambda: ([], "test"))
    monkeypatch.setattr(macs, "routing_version", lambda: "test")
    monkeypatch.setattr(macs, "remember_health", lambda _: None)
    monkeypatch.setattr(macs, "local_row", lambda _: {})
    monkeypatch.setattr(macs, "publish", lambda _: None)
    from types import SimpleNamespace
    from khipu import cli
    doctor_answer = [lambda: (report(), "")]
    def doctor_command(args):
        print(json.dumps(doctor_answer[0]()[0]))
        return 1  # doctor can report an unhealthy semantic exit with valid JSON.
    monkeypatch.setattr(cli, "build_parser", lambda: SimpleNamespace(parse_args=lambda argv: SimpleNamespace(func=doctor_command)))
    def read():
        health_five.cmd_health(None)
        return json.loads(capsys.readouterr().out)
    assert read()["headline"] == "Everything is working."
    def timed_out(*a, **kw): raise subprocess.TimeoutExpired("launchctl", 3)
    monkeypatch.setattr(jobs, "_job_entry", lambda _: {"plist_loaded": True})
    monkeypatch.setattr(jobs.subprocess, "run", timed_out)
    def failed_read():
        r = unread(kind)
        if kind == "notes":
            r["nightly_steps"]["notes_reconcile_ok"] = jobs.notes_watch_health()
        return r, ""
    doctor_answer[0] = failed_read
    for seconds in [300, 600, 900, 901, 1200]:
        clock[0] = NOW + timedelta(seconds=seconds)
        d = read()
        assert d["areas"][area]["state"] == ("ok" if seconds <= 900 else "unknown")
    saved = json.loads((tmp_path / "health-read-evidence.json").read_text())
    assert saved["areas"][health_five.KEYS[area]]["at"] == "2026-11-10T12:00:00Z"
    fault = unread(kind)
    if kind == "notes": fault.update(jobs_ok=False, job_failures=["notes_watch"])
    else: fault["routing_delivery"].update(status="red", reasons=["cross-mac-stale"])
    doctor_answer[0] = lambda: (fault, "")
    assert read()["areas"][area]["state"] == "warn"
    clock[0] += timedelta(minutes=5)
    doctor_answer[0] = failed_read
    assert read()["areas"][area]["state"] == "warn"


@pytest.mark.parametrize("kind,area", [("delivery", 2), ("notes", 4)])
def test_cold_unread_is_unknown_and_fault_beats_kept_good(kind, area):
    r = unread(kind)
    assert health_five.build(r, now=NOW, mac="Test Mac")["areas"][area]["state"] == "unknown"
    good = health_five.build(report(), now=NOW, mac="Test Mac")
    kept = {row["key"]: {"at": good["checked_at"], "row": row} for row in good["areas"]}
    if kind == "notes": r.update(jobs_ok=False, job_failures=["notes_watch"])
    else: r["routing_delivery"].update(status="red", reasons=["cross-mac-stale"])
    assert health_five.build(r, now=NOW, mac="Test Mac", kept=kept)["areas"][area]["state"] == "warn"


@pytest.mark.parametrize("entry", [[], "broken", {"at": "not-a-date", "row": {}}])
def test_corrupt_cache_entry_is_ignored(entry):
    assert health_five.build(unread("notes"), now=NOW, mac="Test Mac", kept={"jobs": entry})["areas"][4]["state"] == "unknown"

