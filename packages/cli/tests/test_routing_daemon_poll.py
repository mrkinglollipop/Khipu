"""Routing truth, phase 3c (docs/plans/2026-10-14-routing-truth.md): the
recall daemon keeps the routing record folded between prompts.

Its routing helper (``recall_prompt.routing_forkserver_main``) forks a poll
child that runs ``routing.poll_refold`` when a source or the log changed
(every ``routing_poll_interval``) and when the clock reaches the view's
``valid_until``; it never sleeps past ``valid_until``, and after the Mac
wakes it runs everything that fell due once. Doctor's per-prompt budget
rate and the stop-condition's replay are here too.

Hermetic: a data dir and a short capture home per test; the schedule's
tests use fake clocks; the helper's tests run the real helper process.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from khipu import recall_daemon, recall_prompt, routing, routing_replay

T0 = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
ACCOUNTS = [{"label": "Claude", "harness": "claude"}, {"label": "Codex", "harness": "codex"}]
ME = {"kind": "conversation", "harness": "claude_code", "project": "project-a", "session": "s-one"}
SESSION = "11111111-2222-3333-4444-555555555555"


class Clocks:
    """A wall clock (routing's ``_now`` and seconds since the epoch) and a
    monotonic clock that stops while the Mac sleeps, as macOS's does."""

    def __init__(self, start: datetime):
        self.now = start
        self.mono_s = 1000.0

    def __call__(self) -> datetime:
        return self.now

    def wall(self) -> float:
        return self.now.timestamp()

    def mono(self) -> float:
        return self.mono_s

    def run(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        self.mono_s += seconds

    def sleep_mac(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)  # the monotonic clock stands still


@pytest.fixture
def home(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": ACCOUNTS, "routing_prompt_budget_ms": 1000}))
    capture = Path(tempfile.mkdtemp(prefix="krd-", dir="/tmp"))  # AF_UNIX paths are short
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(capture))
    monkeypatch.delenv("KHIPU_ISOLATED_DIR", raising=False)
    monkeypatch.delenv("ROUTING_NOW_OFF", raising=False)
    monkeypatch.delenv("KHIPU_FEATURE_ROUTING_PUSH", raising=False)
    monkeypatch.setattr(routing, "ROUTING_NOW_DISABLE_FILE", str(tmp_path / "routing_now_disable"))
    clocks = Clocks(T0)
    monkeypatch.setattr(routing, "_now", clocks)
    yield {"data": data, "clocks": clocks, "capture": capture}
    shutil.rmtree(capture, ignore_errors=True)


def _set(home, **values) -> None:
    path = home["data"] / "config.json"
    config = json.loads(path.read_text())
    config.update(values)
    path.write_text(json.dumps(config))


def _seen_now() -> dict:
    view = routing.read_view()
    assert recall_prompt.mark_routing_seen(SESSION, version=view["version"], epoch=view["epoch"],
                                           full_at=routing._iso(routing._now()))
    return recall_prompt.routing_marker(SESSION)


def _schedule(home, interval: float | None = 10.0) -> recall_prompt.RefoldSchedule:
    return recall_prompt.RefoldSchedule(interval, wall=home["clocks"].wall, mono=home["clocks"].mono)


def _poll(schedule) -> dict:
    """One poll as the helper's child runs it, and the schedule told."""
    out = routing.poll_refold()
    due = routing._parse(out.get("valid_until"))
    schedule.ran(due.timestamp() if due else None, ok=not out["busy"] and out["saved"],
                 interval=out["interval"])
    return out


# ---- the schedule ------------------------------------------------------------------------------


def test_the_sleep_never_passes_valid_until(home):
    clocks = home["clocks"]
    schedule = _schedule(home, interval=10.0)
    assert schedule.due() == ["poll"]  # a start reads the view before it waits
    valid_until = clocks.wall() + 23.5
    schedule.ran(valid_until)
    for _ in range(100):
        if schedule.due():
            break
        wait = schedule.sleep_for(60.0)
        assert clocks.wall() + wait <= valid_until  # never sleeps past it
        assert wait <= 10.0  # nor past the next poll
        clocks.run(wait)
        if "poll" in schedule.due():
            schedule.ran(valid_until)
    assert schedule.due() == ["clock"] and clocks.wall() == valid_until
    # A valid_until nearer than the poll wins; with the poll off it is the only wait.
    off = _schedule(home, interval=None)
    off.ran(clocks.wall() + 3600)
    assert off.sleep_for(7200.0) == 3600 and off.due() == []
    off.ran(None)
    assert off.sleep_for(7200.0) == 7200.0 and off.due() == []


def test_a_failed_or_stale_poll_is_tried_again_soon_not_at_once(home):
    clocks = home["clocks"]
    schedule = _schedule(home, interval=600.0)
    schedule.ran(clocks.wall() - 5)  # a valid_until already passed (an unsaved fold)
    assert schedule.due() == [] and schedule.sleep_for(3600) == recall_prompt._POLL_RETRY_S
    schedule.ran(ok=False)
    assert schedule.sleep_for(3600) == recall_prompt._POLL_RETRY_S


def test_a_wake_after_sleep_runs_due_work_once(home, monkeypatch):
    clocks = home["clocks"]
    routing.report(account="Codex", state="limited", until="1h", author=ME)
    routing.add_hold("keep the second account for the audit", reason="keep", scope="machine", until="2h",
                     author=ME)
    schedule = _schedule(home, interval=10.0)
    first = _poll(schedule)
    assert routing._parse(first["valid_until"]) == T0 + timedelta(hours=1)
    folds = []
    real_fold = routing.fold
    monkeypatch.setattr(routing, "fold", lambda *a, **k: folds.append(1) or real_fold(*a, **k))
    # The Mac sleeps three hours: the limit's end and the hold's end both fell due.
    clocks.sleep_mac(3 * 3600)
    runs = []
    for _ in range(30):  # the helper's ticks after it wakes
        why = schedule.due()
        if why:
            runs.append((why, _poll(schedule)["folded"]))
        clocks.run(min(1.0, schedule.sleep_for(1.0)) or 1.0)
    # Once on waking for everything that fell due, then only the 10 s poll, finding the view fresh.
    assert {"wake", "clock"} <= set(runs[0][0]) and runs[0][1]
    assert runs[1:] and all(why == ["poll"] and not folded for why, folded in runs[1:])
    assert len(folds) == 1  # the record was folded once
    view = routing.read_view()
    codex = next(a for a in view["accounts"] if a["label"] == "Codex")
    assert codex["state"] == routing.PROBABLY_OPEN and not view["holds"]


def test_a_clock_set_backwards_also_counts_as_a_jump(home):
    clocks = home["clocks"]
    schedule = _schedule(home)
    schedule.ran(None)
    clocks.now -= timedelta(hours=2)
    assert "wake" in schedule.due()


def test_a_poll_turned_on_again_is_seen_without_waiting_for_valid_until(home):
    # The helper used to learn the interval only from a poll's answer: after
    # the owner set it to 0 it looked again only at valid_until (hours away).
    clocks = home["clocks"]
    _set(home, routing_poll_interval=0, features={"routing_push": True})
    schedule = _schedule(home, interval=None)
    schedule.ran(clocks.wall() + 7200, interval=routing.poll_interval())
    assert schedule.due() == [] and schedule.next_poll is None
    _set(home, routing_poll_interval="2s")
    recall_prompt._look_at_settings(schedule, routing)
    assert schedule.due() == ["poll"]  # looks at once
    schedule.ran(clocks.wall() + 7200)
    clocks.run(2.0)
    assert schedule.due() == ["poll"]
    # A shorter interval takes effect now, not after the longer wait.
    _set(home, routing_poll_interval="1h")
    recall_prompt._look_at_settings(schedule, routing)
    schedule.ran(clocks.wall() + 7200)
    _set(home, routing_poll_interval="5s")
    recall_prompt._look_at_settings(schedule, routing)
    clocks.run(5.0)
    assert schedule.due() == ["poll"]


def test_with_the_push_off_nothing_is_due_and_on_again_a_poll_is_due_at_once(home, monkeypatch):
    clocks = home["clocks"]
    schedule = _schedule(home, interval=10.0)
    schedule.ran(clocks.wall() + 30)
    recall_prompt._look_at_settings(schedule, routing)  # the feature is off in this data dir
    clocks.run(60)  # past the poll and valid_until both
    assert schedule.due() == [] and schedule.sleep_for(5.0) == 5.0
    _set(home, features={"routing_push": True})
    recall_prompt._look_at_settings(schedule, routing)
    assert schedule.due()
    _set(home, routing_push_prompt=False)  # the prompt's own switch
    recall_prompt._look_at_settings(schedule, routing)
    assert schedule.due() == []
    _set(home, routing_push_prompt=True)
    recall_prompt._look_at_settings(schedule, routing)
    assert schedule.due()
    monkeypatch.setenv("ROUTING_NOW_OFF", "1")  # the old hook's bypass
    recall_prompt._look_at_settings(schedule, routing)
    assert schedule.due() == []


def _helper_poll(schedule) -> dict:
    """A poll as the helper starts it: the view's stat first, then the child's poll."""
    schedule.view_seen = recall_prompt._view_stat(routing)
    return _poll(schedule)


@pytest.mark.parametrize("interval", [0, "1h"])
def test_a_limit_written_after_the_last_poll_is_waited_for_with_no_poll_following(home, monkeypatch, interval):
    # The schedule learned valid_until only from a poll's answer: with the
    # poll interval 0 (or a long one) a limit written after the start-up poll
    # never refolded on the clock, and only a prompt's own fold saw its end.
    clocks = home["clocks"]
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "1")
    _set(home, routing_poll_interval=interval)
    routing.report(account="Claude", state="open", author=ME)
    schedule = _schedule(home, interval=routing.poll_interval())
    recall_prompt._look_at_settings(schedule, routing)
    assert schedule.due() == ["poll"]
    _helper_poll(schedule)  # the start-up poll: the clock changes nothing for hours
    assert schedule.valid_until > clocks.wall() + 3600 and not schedule.due()
    clocks.run(5)
    recall_prompt._look_at_settings(schedule, routing)
    assert not schedule.due()  # nothing changed: no poll
    routing.report(account="Codex", state="limited", until="12s", author=ME)
    recall_prompt._look_at_settings(schedule, routing)  # the helper's look sees view.json changed
    assert schedule.due() == ["poll"]
    out = _helper_poll(schedule)
    assert not out["folded"]  # the write saved the view: the poll only reads its valid_until
    end = routing._parse(routing._load_view()["valid_until"])
    assert schedule.valid_until == end.timestamp() == clocks.wall() + 12
    recall_prompt._look_at_settings(schedule, routing)
    assert not schedule.due() and 11.0 <= schedule.sleep_for(60.0) <= 12.0
    clocks.run(12)
    assert schedule.due() == ["clock"]
    assert _helper_poll(schedule)["folded"]
    codex = next(a for a in routing._load_view()["accounts"] if a["label"] == "Codex")
    assert codex["state"] == routing.PROBABLY_OPEN
    # Its own save is one more look at a fresh view, then nothing.
    recall_prompt._look_at_settings(schedule, routing)
    assert schedule.due() == ["poll"] and not _helper_poll(schedule)["folded"]
    recall_prompt._look_at_settings(schedule, routing)
    assert not schedule.due()


def test_a_changed_view_makes_no_poll_due_with_the_push_off(home):
    _set(home, routing_poll_interval=0)
    schedule = _schedule(home, interval=None)
    recall_prompt._look_at_settings(schedule, routing)  # the feature is off by default
    routing.report(account="Codex", state="limited", until="12s", author=ME)
    recall_prompt._look_at_settings(schedule, routing)
    assert schedule.off and schedule.view_seen is None and not schedule.due()


def test_a_stopping_helper_sleeps_while_only_a_folding_poll_child_is_left():
    # The reap wait slept only while a prompt's child was left, so a folding
    # poll child (it ignores the stop signal) left it spinning on waitpid.
    clock = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    pollers = {123}
    recall_prompt._reap_until(lambda: len(pollers), lambda: None, 0.5, sleep=sleep, clock=lambda: clock[0])
    assert 40 <= len(sleeps) <= 51 and clock[0] >= 0.5
    pollers.clear()
    sleeps.clear()
    recall_prompt._reap_until(lambda: len(pollers), lambda: None, 0.5, sleep=sleep, clock=lambda: clock[0])
    assert sleeps == []


# ---- the poll ------------------------------------------------------------------------------------


def test_a_limits_end_with_no_file_changing_reaches_the_next_prompt_as_a_change(home, monkeypatch):
    clocks = home["clocks"]
    _set(home, routing_poll_interval=0)  # the poll off: only the clock wakes it
    routing.report(account="Codex", state="limited", until="1h", author=ME)
    routing.report(account="Claude", state="open", author=ME)
    marker = _seen_now()
    schedule = _schedule(home, interval=None)
    before = _poll(schedule)
    assert not before["folded"]
    events = routing.events_file().stat().st_mtime_ns
    while not schedule.due():
        clocks.run(schedule.sleep_for(3600.0))
    assert schedule.due() == ["clock"] and clocks.now == T0 + timedelta(hours=1)
    out = _poll(schedule)
    assert out["folded"] and out["saved"] and out["version"] > before["version"]
    assert routing.events_file().stat().st_mtime_ns == events  # no file changed
    # The next prompt reads the saved view: no fold of its own.
    monkeypatch.setattr(routing, "fold", lambda *a, **k: pytest.fail("the prompt refolded"))
    clocks.run(5)
    push = routing.prompt_push(marker, budget=1.0)
    assert push["kind"] == "changes", push
    assert "Codex: probably open: its limit was due to end at 16:00Z" in push["text"]


def test_a_source_change_refolds(home, monkeypatch):
    routing.report(account="Claude", state="open", author=ME)
    first = routing.poll_refold()
    assert not first["folded"] and first["interval"] == 10.0
    # Another writer's line is down, but its own refold never ran.
    with monkeypatch.context() as m:
        m.setattr(routing, "_refold_locked", lambda *a, **k: (_ for _ in ()).throw(OSError("full disk")))
        routing.report(account="Codex", state="limited", until="1h", author=ME)
    assert routing._load_view()["version"] == first["version"]
    out = routing.poll_refold()
    assert out["folded"] and out["saved"] and out["version"] == first["version"] + 1
    codex = next(a for a in routing._load_view()["accounts"] if a["label"] == "Codex")
    assert codex["state"] == routing.LIMITED
    assert not routing.poll_refold()["folded"]  # fresh now: nothing to do
    # A changed setting is a source too.
    home["clocks"].run(2)
    _set(home, routing_poll_interval="30s")
    again = routing.poll_refold()
    assert again["folded"] and again["interval"] == 30.0


def test_the_poll_waits_for_a_prompts_fold_and_never_folds_beside_it(home):
    import fcntl

    routing.report(account="Claude", state="open", author=ME)
    routing.view_file().unlink()
    lock = routing.routing_dir() / routing.PROMPT_FOLD_LOCK
    with open(lock, "a") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)  # a prompt's child is folding
        began = time.monotonic()
        out = routing.poll_refold()
        waited = time.monotonic() - began
    assert out["busy"] and not out["folded"]
    assert routing.LIGHT_LOCK_BUDGET - 0.05 <= waited < routing.LIGHT_LOCK_BUDGET + 1.0
    assert routing.poll_refold()["folded"]


def _folding_elsewhere(seconds: float):
    """Another process's fold (the poll's, as it holds prompt-fold.lock):
    a thread that takes the lock on its own descriptor, refolds and saves
    the view after ``seconds``, then lets go. Returns (thread, started, the
    threads that folded)."""
    import fcntl

    folded: list[str] = []
    real_fold = routing.fold

    def counted(*a, **k):
        folded.append(threading.current_thread().name)
        return real_fold(*a, **k)

    started = threading.Event()

    def poll():
        with open(routing.routing_dir() / routing.PROMPT_FOLD_LOCK, "a") as fd:
            fcntl.flock(fd, fcntl.LOCK_EX)
            started.set()
            time.sleep(seconds)
            with routing._lock():
                routing._refold_locked(routing._now())

    thread = threading.Thread(target=poll, name="the-poll")
    return thread, started, folded, counted


@pytest.mark.parametrize("reader", ["read_view", "start_view"])
def test_a_reader_waits_for_the_running_fold_and_never_folds_beside_it(home, monkeypatch, reader):
    routing.report(account="Claude", state="open", author=ME)
    home["clocks"].run(2)
    _set(home, routing_poll_interval="30s")  # a changed setting: the view is out of date
    assert not routing._fresh(routing._load_view(), routing._inputs()[0], routing._now())
    thread, started, folded, counted = _folding_elsewhere(0.4)
    monkeypatch.setattr(routing, "fold", counted)
    thread.start()
    assert started.wait(5)
    if reader == "read_view":
        view = routing.read_view()
    else:
        with routing.lock_budget(5):
            view = routing._start_view(routing._now())
    thread.join(5)
    assert folded == ["the-poll"]  # once, by the fold that was running
    saved = routing._load_view()
    assert view.get("saved", True) is not False and view["digest"] == saved["digest"]
    assert routing._fresh(saved, routing._inputs()[0], routing._now())


def test_a_reader_that_cannot_wait_for_the_running_fold_answers_unsaved(home, monkeypatch):
    routing.report(account="Claude", state="open", author=ME)
    home["clocks"].run(2)
    _set(home, routing_poll_interval="30s")
    thread, started, folded, counted = _folding_elsewhere(1.0)
    monkeypatch.setattr(routing, "fold", counted)
    thread.start()
    assert started.wait(5)
    began = time.monotonic()
    with routing.lock_budget(0.2):
        view = routing.read_view()
    waited = time.monotonic() - began
    thread.join(5)
    assert view["saved"] is False and waited < 0.9


def test_no_record_folds_and_writes_nothing(home):
    out = routing.poll_refold()
    assert not out["folded"] and not routing.routing_dir().exists()


# ---- the real helper -------------------------------------------------------------------------------


@pytest.fixture
def helper(home, monkeypatch):
    """The service as ``serve`` runs it, with the push on; the helper's poll
    child uses the real clock."""
    home["clocks"].now = datetime.now(timezone.utc)
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "1")
    svc = recall_daemon.RecallService(home["capture"] / "recall.sock", stamp_fn=lambda: "fixed",
                                      idle_check=False)
    assert svc.bind()
    thread = threading.Thread(target=svc.run, daemon=True)
    thread.start()
    yield svc
    svc.stop()
    thread.join(5)
    svc.close()


def _wait(check, seconds: float = 15.0):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        got = check()
        if got:
            return got
        time.sleep(0.05)
    raise AssertionError("timed out")


def test_the_helper_refolds_at_valid_until_with_no_prompt_and_never_in_the_daemon(home, monkeypatch, helper):
    _set(home, routing_poll_interval="1h")  # only the clock can wake it
    monkeypatch.setattr(routing, "_now", lambda: datetime.now(timezone.utc))
    routing.report(account="Codex", state="limited", until="3s", author=ME)
    version = routing._load_view()["version"]
    # Any fold in this process (the daemon) fails the test; the helper's child has its own.
    monkeypatch.setattr(routing, "fold", lambda *a, **k: pytest.fail("the daemon folded"))
    assert helper.start_routing()
    assert helper.routing_forkserver.wait_ready(10)
    view = _wait(lambda: (v := routing._load_view()) and v["version"] > version and v)
    codex = next(a for a in view["accounts"] if a["label"] == "Codex")
    assert codex["state"] == routing.PROBABLY_OPEN


def test_the_helper_refolds_after_a_source_change(home, monkeypatch, helper):
    _set(home, routing_poll_interval="1s")
    monkeypatch.setattr(routing, "_now", lambda: datetime.now(timezone.utc))
    routing.report(account="Claude", state="open", author=ME)
    assert helper.start_routing()
    assert helper.routing_forkserver.wait_ready(10)
    version = _wait(lambda: routing._load_view())["version"]
    with monkeypatch.context() as m:
        m.setattr(routing, "_refold_locked", lambda *a, **k: (_ for _ in ()).throw(OSError("full disk")))
        routing.report(account="Codex", state="limited", until="1h", author=ME)
    view = _wait(lambda: (v := routing._load_view()) and v["version"] > version and v)
    assert next(a for a in view["accounts"] if a["label"] == "Codex")["state"] == routing.LIMITED


def test_the_daemon_starts_a_dead_helper_again_between_prompts(home, monkeypatch, helper):
    monkeypatch.setattr(recall_prompt, "_FORKSERVER_RESPAWN_S", 0.0)
    assert not helper.keep_routing()  # never started (the push was off): stays unstarted
    assert helper.start_routing()
    assert helper.routing_forkserver.wait_ready(10)
    proc = helper.routing_forkserver._proc
    proc.kill()
    proc.wait(5)
    assert helper.keep_routing()
    assert _wait(lambda: helper.routing_forkserver.wait_ready(0.1) and helper.routing_forkserver.spawned == 2)


def test_the_daemon_stops_the_helper_when_the_push_goes_off_and_never_starts_it_again(home, monkeypatch, helper):
    monkeypatch.setattr(recall_prompt, "_FORKSERVER_RESPAWN_S", 0.0)
    monkeypatch.setattr(recall_daemon, "_ROUTING_LOOK_S", 0.0)
    assert helper.start_routing()
    assert helper.routing_forkserver.wait_ready(10)
    proc = helper.routing_forkserver._proc
    _set(home, routing_push_prompt=False)
    assert not helper.keep_routing()
    proc.wait(5)  # stopped: nothing polls
    assert helper.routing_forkserver._proc is None
    assert not helper.keep_routing() and helper.routing_forkserver._proc is None
    _set(home, routing_push_prompt=True)
    assert helper.keep_routing()  # on again: started again
    assert _wait(lambda: helper.routing_forkserver.wait_ready(0.1) and helper.routing_forkserver.spawned == 2)


def test_the_helper_picks_up_a_poll_interval_turned_on_again(home, monkeypatch, helper):
    monkeypatch.setattr(recall_prompt, "_SETTINGS_LOOK_S", 0.5)
    _set(home, routing_poll_interval=0)
    monkeypatch.setattr(routing, "_now", lambda: datetime.now(timezone.utc))
    routing.report(account="Codex", state="limited", until="2h", author=ME)
    assert helper.start_routing()
    assert helper.routing_forkserver.wait_ready(10)
    version = _wait(lambda: routing._load_view())["version"]
    _set(home, routing_poll_interval="1s")
    with monkeypatch.context() as m:
        m.setattr(routing, "_refold_locked", lambda *a, **k: (_ for _ in ()).throw(OSError("full disk")))
        routing.report(account="Claude", state="limited", until="1h", author=ME)
    view = _wait(lambda: (v := routing._load_view()) and v["version"] > version and v, seconds=10)
    assert next(a for a in view["accounts"] if a["label"] == "Claude")["state"] == routing.LIMITED


def test_the_helper_refolds_at_a_limits_end_written_after_its_start_with_the_poll_off(home, monkeypatch, helper):
    _set(home, routing_poll_interval=0)
    monkeypatch.setattr(routing, "_now", lambda: datetime.now(timezone.utc))
    routing.report(account="Claude", state="open", author=ME)
    assert helper.start_routing()
    assert helper.routing_forkserver.wait_ready(10)
    time.sleep(1.5)  # its start-up poll has read the view; no poll follows
    routing.report(account="Codex", state="limited", until="3s", author=ME)
    view = routing._load_view()
    version, end = view["version"], routing._parse(view["valid_until"])
    # No prompt and no poll: only the helper's look at view.json and its clock.
    view = _wait(lambda: (v := routing._load_view()) and v["version"] > version and v, seconds=15)
    assert routing._parse(view["folded_at"]) >= end
    assert next(a for a in view["accounts"] if a["label"] == "Codex")["state"] == routing.PROBABLY_OPEN


# ---- doctor: the per-prompt budget rate -----------------------------------------------------------


def _hook_log(statuses: list[str | None], *, when: datetime | None = None) -> None:
    when = when or datetime.now(timezone.utc)
    path = recall_prompt._log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    with path.open("a") as fh:
        for i, status in enumerate(statuses):
            routed = f" routing={status}" if status else ""
            fh.write(f"{stamp} [khipu-prompt-recall] session=s{i} reason=ok ms=300.0 hits=[] legs=['lexical'] "
                     f"degraded=None{routed}\n")
        fh.write(f"{stamp} [khipu-prompt-recall] snapshot unusable (no replica)\n")


def test_the_budget_rate_counts_only_prompts_with_the_push_on(home):
    _hook_log(["full", "none", "skipped:budget", None, None, "changes:2", "unsent:full"])
    rate = recall_prompt.prompt_routing_budget_rate(warn_pct=1.0)
    assert rate["available"] and rate["count"] == 5 and rate["skipped_budget"] == 1
    assert rate["rate_pct"] == 20.0 and not rate["warn"]  # too few prompts to judge


def test_the_budget_rate_counts_every_prompt_that_went_without_routing_for_lack_of_time(home):
    # skipped:fold-busy waited for the poll's fold past its budget; skipped:busy
    # found the helper full: both went without their routing lines for lack of time.
    _hook_log(["none"] * 15 + ["skipped:fold-busy"] * 3 + ["skipped:budget", "skipped:busy", "skipped:unsaved"])
    rate = recall_prompt.prompt_routing_budget_rate(warn_pct=10.0)
    assert rate["count"] == 21 and rate["skipped"] == 5
    assert rate["skipped_budget"] == 1 and rate["skipped_fold_busy"] == 3 and rate["skipped_busy"] == 1
    assert rate["rate_pct"] == round(500 / 21, 2) and rate["warn"]


def test_doctor_warns_past_the_slow_read_threshold(home):
    routing.report(account="Claude", state="open", author=ME)
    home["clocks"].now = datetime.now(timezone.utc)
    _hook_log(["none"] * 97 + ["skipped:budget"] * 3)
    block = routing.doctor_block()
    assert block["prompt_budget"]["rate_pct"] == 3.0 and block["prompt_budget"]["warn"]
    assert any("ran out of time on 3% of the last 100 prompts" in w for w in block["warnings"])
    _set(home, routing_slow_read_pct=5)
    block = routing.doctor_block()
    assert not block["prompt_budget"]["warn"]
    assert not [w for w in block["warnings"] if "ran out of time" in w]
    _set(home, routing_slow_read_pct=0)  # 0: never warn
    assert not routing.doctor_block()["prompt_budget"]["warn"]


def test_old_lines_are_left_out_of_the_budget_rate(home):
    _hook_log(["skipped:budget"] * 30, when=datetime.now(timezone.utc) - timedelta(days=8))
    _hook_log(["none"] * 25)
    rate = recall_prompt.prompt_routing_budget_rate()
    assert rate["count"] == 25 and rate["skipped_budget"] == 0 and not rate["warn"]


# ---- the stop-condition's replay ------------------------------------------------------------------


def test_the_stop_condition_is_more_than_two_timeouts_in_a_hundred():
    assert not routing_replay.verdict(4.0, 2.0)
    assert routing_replay.verdict(4.5, 2.0)
    assert not routing_replay.verdict(0.0, 3.0)


def test_the_replay_stops_when_the_helper_never_refolds(tmp_path, monkeypatch):
    # A helper that never polls (its push off) would leave the poll's cost
    # out of the measure: the replay refuses to measure without it.
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "live"))
    monkeypatch.delenv("KHIPU_ISOLATED_DIR", raising=False)
    monkeypatch.setattr(routing, "ROUTING_NOW_DISABLE_FILE", str(tmp_path / "routing_now_disable"))
    monkeypatch.setattr(routing_replay, "_CHECK_WAIT_S", 2.0)
    real_seed = routing_replay._seed

    def seed(directory, limit_ends_in):
        real_seed(directory, limit_ends_in)
        config = json.loads((directory / "config.json").read_text())
        (directory / "config.json").write_text(json.dumps(dict(config, routing_push_prompt=False)))

    monkeypatch.setattr(routing_replay, "_seed", seed)
    work = Path(tempfile.mkdtemp(prefix="krr-", dir="/tmp"))
    try:
        with pytest.raises(RuntimeError, match="did not refold.*its push is off: routing_push_prompt off"):
            routing_replay.replay(2, search="synthetic", workdir=work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_the_replay_reports_timeouts_per_100_each_way_and_writes_only_its_own_dir(tmp_path, monkeypatch):
    live = tmp_path / "live"
    live.mkdir()
    monkeypatch.setenv("KHIPU_DATA_DIR", str(live))
    monkeypatch.delenv("KHIPU_ISOLATED_DIR", raising=False)
    monkeypatch.delenv("KHIPU_CAPTURE_HOME", raising=False)
    monkeypatch.setattr(routing, "ROUTING_NOW_DISABLE_FILE", str(tmp_path / "routing_now_disable"))
    monkeypatch.setattr(routing_replay, "_SYNTH_MEDIAN_S", 0.02)
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "0")  # an owner's shell with the push off
    monkeypatch.setenv("ROUTING_NOW_OFF", "1")
    folds: list[str] = []
    real_fold = routing.fold
    monkeypatch.setattr(routing, "fold", lambda *a, **k: folds.append("replay") or real_fold(*a, **k))
    seen: dict = {}
    real_helper_refolds = routing_replay._helper_refolds

    def helper_refolds(*a, **k):
        seen["push_off"] = routing.push_off("prompt")
        seen["folds_before"] = len(folds)
        seen["took"] = real_helper_refolds(*a, **k)
        seen["folds_after"] = len(folds)
        return seen["took"]

    monkeypatch.setattr(routing_replay, "_helper_refolds", helper_refolds)
    work = Path(tempfile.mkdtemp(prefix="krr-", dir="/tmp"))
    try:
        out = routing_replay.replay(8, search="synthetic", workdir=work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    # The helper ran with the push on and refolded at the first limit's end
    # with no prompt, and no fold in this process did it for it.
    assert seen["push_off"] is None
    assert seen["folds_after"] == seen["folds_before"]
    assert out["helper_refold_s"] == seen["took"] and 0 <= seen["took"] < routing_replay._CHECK_WAIT_S
    assert os.environ["KHIPU_FEATURE_ROUTING_PUSH"] == "0" and os.environ["ROUTING_NOW_OFF"] == "1"
    assert out["on"]["prompts"] == out["off"]["prompts"] == 8
    assert out["on"]["reasons"] == {"ok": 8} and out["off"]["reasons"] == {"ok": 8}
    assert out["on"]["per_100"] == out["off"]["per_100"] == 0.0 and not out["stop"] and not out["enough"]
    assert sum(out["on"]["routing"].values()) == 8 and out["off"]["routing"] == {}
    assert list(live.iterdir()) == []  # nothing in the configured data dir
    assert "KHIPU_ISOLATED_DIR" not in os.environ and "KHIPU_CAPTURE_HOME" not in os.environ
