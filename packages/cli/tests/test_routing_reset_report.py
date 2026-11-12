"""A reported reset beats figures from before it (2026-11-12).

The owner pressed the second Claude account's weekly reset; a conversation
recorded it with ``khipu_route_report`` (evidence, source report, state
open, account_quota scope). The usage cache still held 99% from four minutes
earlier and every read since had failed (``no_plan_figures``), so the page
said NEARLY OUT, advice held back and the pace plan ran out within the hour.

A. An open report re-reads the account's figures at once: the login's turn
   stamp is dropped and the detached refresh starts without waiting.
B. While the report is newer than every reading, the page and advice do not
   present the older figures as current. Figures read after it win.
C. Kept figures over ten minutes old while reads fail say so on the page.

Hermetic: a data dir per test, a fake clock, no CLI is started.
"""
# ruff: noqa: F811
from __future__ import annotations

import json
from datetime import timedelta

from khipu import routing, routing_advice, routing_page, usage
from tests.test_routing_priority import FIGURES, ME, SECOND, T0, _cache, _row, _week, home  # noqa: F401

RESETS = T0 + timedelta(days=3)
# The real re-read, before conftest stubs it for every other test.
REAL_REREAD = getattr(usage, "reread_reported", None)


def _second(used, at, **extra) -> dict:
    row = _row("Claude second", "claude", "/logins/claude-second", _week(used, RESETS))
    return dict(row, probed_at=routing._iso(at), last_attempt_at=routing._iso(at), last_good_at=routing._iso(at),
                **extra)


def _read(home, used, at) -> None:
    """A good read of the second account at ``at``, sampled as the refresh does."""
    _cache(home, dict(FIGURES, **{SECOND: _second(used, at)}))
    routing.record_pace_samples(at)


def _failing(home, used, good_at, attempt_at) -> None:
    row = dict(_second(used, good_at), ok=False, error=usage.E_NO_FIGURES, last_error=usage.E_NO_FIGURES,
               last_attempt_at=routing._iso(attempt_at))
    _cache(home, dict(FIGURES, **{SECOND: row}))


def _report_open(home) -> dict:
    return routing.conversation_write({"action": "report", "state": "open", "account": "Claude second",
                                       "evidence": "Second account reset button hit"}, author=ME)


def _line(page: str) -> str:
    return next(line for line in page.splitlines() if "Claude second:" in line)


def _entry(view: dict) -> dict:
    return next(e for e in view["accounts"] if e["key"] == SECOND)


# ---- B. the reported reset beats older figures ------------------------------------------------


def test_reset_reported_after_the_last_read_shows_figures_pending_not_nearly_out(home):
    clock = home["clock"]
    _read(home, 99, T0 - timedelta(minutes=4))
    _failing(home, 99, T0 - timedelta(minutes=4), T0)
    before = routing_page.page(routing.read_view(T0), T0)
    assert "Claude second: NEARLY OUT. 1% left" in _line(before)  # what the conversation was told

    _report_open(home)
    now = clock.advance(minutes=6)
    view = routing.read_view(now)
    line = _line(routing_page.page(view, now))
    assert "NEARLY OUT" not in line and "LIMIT REACHED" not in line and "1% left" not in line
    assert "Claude second: OK. Reset reported 14:00Z; figures pending (last read 13:56Z: 99% used)." in line

    entry = _entry(view)
    assert entry["reset_reported"] == {"at": routing._iso(T0), "figures_at": routing._iso(T0 - timedelta(minutes=4)),
                                       "used_pct": 99.0, "windows": ["weekly"]}
    # No run-out or verdict is derived from the older reading.
    assert all(p.get("used_pct") is None and (p.get("plan") or {}).get("runs_out_at") is None
               for p in entry.get("pace") or [])
    assert routing_advice.nearly_out(entry) is None
    advice = routing_advice.account_advice(view, entry, now)
    assert advice["verdict"] != "hold_back"
    fact = routing.plan_facts(view, now)[SECOND]
    assert fact["readable"] is False and fact["reset_reported"] is True
    assert fact["why"].startswith("Reset reported 14:00Z; figures pending")


def test_figures_read_after_the_report_win_as_normal(home):
    clock = home["clock"]
    _read(home, 99, T0 - timedelta(minutes=4))
    _report_open(home)
    now = clock.advance(minutes=3)
    _read(home, 2, now)
    view = routing.read_view(now)
    line = _line(routing_page.page(view, now))
    assert "Reset reported" not in line and "98% left" in line
    assert "reset_reported" not in _entry(view)


def test_a_cached_read_after_the_report_ends_pending_even_without_a_new_sample(home):
    # A report that turns out wrong: the next read still says 99%, too close
    # to the last sample to be stored as a new one. The cache's own time wins.
    clock = home["clock"]
    _read(home, 99, T0 - timedelta(minutes=4))
    _report_open(home)
    now = clock.advance(minutes=2)
    _cache(home, dict(FIGURES, **{SECOND: _second(99, now)}))
    view = routing.read_view(now)
    assert "reset_reported" not in _entry(view)
    assert "NEARLY OUT" in _line(routing_page.page(view, now))


def test_a_report_older_than_the_figures_changes_nothing(home):
    clock = home["clock"]
    _report_open(home)
    now = clock.advance(minutes=2)
    _read(home, 99, now)
    view = routing.read_view(now)
    assert "reset_reported" not in _entry(view)
    assert "NEARLY OUT" in _line(routing_page.page(view, now))


def test_a_limited_report_is_not_a_reset(home):
    clock = home["clock"]
    _read(home, 99, T0 - timedelta(minutes=4))
    routing.conversation_write({"action": "report", "state": "limited", "account": "Claude second",
                                "until": "2h"}, author=ME)
    view = routing.read_view(clock.advance(minutes=1))
    assert "reset_reported" not in _entry(view)


# ---- C. kept figures from failing reads ------------------------------------------------------


def test_kept_figures_over_ten_minutes_old_with_failing_reads_say_so(home):
    clock = home["clock"]
    _read(home, 40, T0 - timedelta(minutes=12))
    _failing(home, 40, T0 - timedelta(minutes=12), T0)
    line = _line(routing_page.page(routing.read_view(T0), T0))
    assert "(figures from 13:48Z; reads failing)" in line

    # Nine minutes old: not yet worth a word.
    _read(home, 40, T0 - timedelta(minutes=9))
    _failing(home, 40, T0 - timedelta(minutes=9), T0 + timedelta(seconds=1))
    now = clock.advance(seconds=1)
    assert "reads failing" not in _line(routing_page.page(routing.read_view(now), now))


def test_good_reads_never_say_failing(home):
    _read(home, 40, T0 - timedelta(minutes=30))
    assert "reads failing" not in _line(routing_page.page(routing.read_view(T0), T0))


def test_the_page_still_fits_with_every_new_line(home):
    _read(home, 99, T0 - timedelta(minutes=30))
    _failing(home, 99, T0 - timedelta(minutes=30), T0)
    routing.conversation_write({"action": "report", "state": "open", "account": "Claude",
                                "evidence": "x" * 300}, author=ME)
    _report_open(home)
    now = home["clock"].advance(minutes=1)
    assert len(routing_page.page(routing.read_view(now), now)) <= routing_page.LIMIT


# ---- A. the open report re-reads the account at once ----------------------------------------


def test_an_open_report_asks_for_a_reread_of_that_account(home, monkeypatch):
    asked = []
    monkeypatch.setattr(usage, "reread_reported", lambda key, **kw: asked.append(key) or True)
    _report_open(home)
    assert asked == [SECOND]
    routing.conversation_write({"action": "report", "state": "limited", "account": "Claude second",
                                "until": "2h"}, author=ME)
    assert asked == [SECOND]


def test_a_reread_that_breaks_never_fails_the_report(home, monkeypatch):
    def boom(key, **kw):
        raise RuntimeError("no")

    monkeypatch.setattr(usage, "reread_reported", boom)
    out = _report_open(home)
    assert out.get("ok", True) is not False
    entries = [json.loads(line) for line in routing.events_file().read_text().splitlines() if line.strip()]
    assert any(e.get("source") == "report" and e.get("state") == "open" for e in entries)


def test_reread_drops_the_logins_turn_stamp_and_starts_the_refresh_now(home, monkeypatch):
    stamp = usage.cache_file().with_name(usage.TURN_STAMP_NAME)
    other = "/logins/other"
    stamp.write_text(json.dumps({"/logins/claude-second": 1e12, other: 1e12}))
    # A refresh kicked a moment ago does not stop this one.
    usage.cache_file().with_name(usage.KICK_STAMP_NAME).touch()
    started = []
    assert REAL_REREAD(SECOND, spawn=started.append) is True
    assert json.loads(stamp.read_text()) == {other: 1e12}
    assert len(started) == 1 and started[0][-3:] == ["--refresh-due", "0", "--json"]
    # With the stamp gone the login's turn is due at once.
    assert usage._turn_due("/logins/claude-second") is True


def test_reread_of_an_unknown_account_does_nothing(home):
    started = []
    assert REAL_REREAD("claude|/logins/nobody", spawn=started.append) is False
    assert started == []


# ---- review round 1 (Codex) -----------------------------------------------------------------


def test_a_reset_report_beats_a_limit_read_from_older_figures(home):
    # 100% read at 13:56, reset reported 14:00, every read since fails: the
    # probe's limit came from figures older than the report.
    clock = home["clock"]
    _read(home, 100, T0 - timedelta(minutes=4))
    _failing(home, 100, T0 - timedelta(minutes=4), T0)
    assert "LIMIT REACHED" in _line(routing_page.page(routing.read_view(T0), T0))

    _report_open(home)
    now = clock.advance(minutes=3)
    _failing(home, 100, T0 - timedelta(minutes=4), now)
    view = routing.read_view(now)
    entry = _entry(view)
    assert entry["state"] != routing.LIMITED
    assert entry["reset_reported"]["figures_at"] == routing._iso(T0 - timedelta(minutes=4))
    line = _line(routing_page.page(view, now))
    assert "LIMIT REACHED" not in line and "Reset reported 14:00Z; figures pending" in line
    assert routing_advice.account_advice(view, entry, now)["verdict"] not in ("closed", "hold_back")
    assert all(p.get("used_pct") is None for p in entry.get("pace") or [])


def test_a_limit_read_after_the_reset_report_still_wins(home):
    clock = home["clock"]
    _read(home, 100, T0 - timedelta(minutes=4))
    _report_open(home)
    now = clock.advance(minutes=3)
    _read(home, 100, now)
    view = routing.read_view(now)
    entry = _entry(view)
    assert entry["state"] == routing.LIMITED and "reset_reported" not in entry
    assert "LIMIT REACHED" in _line(routing_page.page(view, now))


def test_a_newer_retrieval_time_over_older_window_figures_keeps_the_reset_pending(home):
    # Window observed 13:56, report 14:00, row retrieved 14:02 with no row
    # observed_at: the figures are still from before the report.
    clock = home["clock"]
    _read(home, 99, T0 - timedelta(minutes=4))
    _report_open(home)
    now = clock.advance(minutes=2)
    row = _row("Claude second", "claude", "/logins/claude-second",
               _week(99, RESETS, observed_at=routing._iso(T0 - timedelta(minutes=4))))
    row.update(probed_at=routing._iso(now), last_attempt_at=routing._iso(now), last_good_at=routing._iso(now))
    _cache(home, dict(FIGURES, **{SECOND: row}))
    view = routing.read_view(now)
    entry = _entry(view)
    assert entry.get("reset_reported", {}).get("figures_at") == routing._iso(T0 - timedelta(minutes=4))
    assert "NEARLY OUT" not in _line(routing_page.page(view, now))


def test_forget_turn_skips_when_the_turn_lock_is_held(home):
    import fcntl
    import os
    import threading
    import time as _time

    stamp = usage.cache_file().with_name(usage.TURN_STAMP_NAME)
    stamp.write_text(json.dumps({"/logins/claude-second": 1e12}))
    fd = usage._open_lock(usage.TURN_LOCK_NAME, directory=stamp.parent)
    fcntl.flock(fd, fcntl.LOCK_EX)
    out = []
    worker = threading.Thread(target=lambda: out.append(usage._forget_turn("/logins/claude-second")), daemon=True)
    started = _time.monotonic()
    try:
        worker.start()
        worker.join(timeout=3)
        assert not worker.is_alive(), "forget_turn waited on a held lock"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        worker.join(timeout=3)
    assert out == [False] and _time.monotonic() - started < 3
    assert json.loads(stamp.read_text()) == {"/logins/claude-second": 1e12}


def test_a_pending_reset_with_failing_reads_says_so(home):
    clock = home["clock"]
    _read(home, 99, T0 - timedelta(minutes=12))
    _failing(home, 99, T0 - timedelta(minutes=12), T0)
    _report_open(home)
    now = clock.advance(minutes=2)
    _failing(home, 99, T0 - timedelta(minutes=12), now)
    view = routing.read_view(now)
    assert "reset_reported" in _entry(view)
    line = _line(routing_page.page(view, now))
    assert "Reset reported 14:00Z; figures pending" in line
    assert "(figures from 13:48Z; reads failing)" in line


# ---- review round 2 (Codex) -----------------------------------------------------------------


def test_a_fresh_window_does_not_end_pending_for_a_stale_one(home):
    # Weekly 99% observed 13:56, reset reported 14:00, a five-hour figure read
    # 14:02 while the weekly figures are unchanged (still from 13:56).
    clock = home["clock"]
    _read(home, 99, T0 - timedelta(minutes=4))
    _report_open(home)
    now = clock.advance(minutes=2)
    five = {"name": "five_hour", "used_pct": 10, "resets_at": routing._iso(T0 + timedelta(hours=3)),
            "window_mins": 300, "observed_at": routing._iso(now)}
    row = _row("Claude second", "claude", "/logins/claude-second",
               _week(99, RESETS, observed_at=routing._iso(T0 - timedelta(minutes=4))), five)
    row.update(probed_at=routing._iso(now), last_attempt_at=routing._iso(now), last_good_at=routing._iso(now))
    _cache(home, dict(FIGURES, **{SECOND: row}))
    routing.record_pace_samples(now)
    view = routing.read_view(now)
    entry = _entry(view)
    # The week stays pending; the five-hour window is used as normal.
    assert entry.get("reset_reported", {}).get("windows") == ["weekly"]
    pace = {p["window"]: p for p in entry.get("pace") or []}
    assert pace["weekly"].get("used_pct") is None
    assert pace["five_hour"].get("used_pct") == 10
    line = _line(routing_page.page(view, now))
    assert "NEARLY OUT" not in line and "1% left" not in line, line
    assert "90% " in line and "Reset reported 14:00Z; figures pending" in line, line
    assert routing_advice.nearly_out(entry) is None
    assert routing_advice.account_advice(view, entry, now)["verdict"] != "hold_back"


def test_a_model_limit_survives_a_reported_account_reset_on_the_page(home):
    # Whole account and its Opus window both at 100% at 13:56, resets ahead;
    # the account's reset reported 14:00 ends the account limit, not Opus's.
    clock = home["clock"]
    at = T0 - timedelta(minutes=4)
    row = _row("Claude second", "claude", "/logins/claude-second", _week(100, RESETS),
               _week(100, RESETS, model="Opus", name="weekly_opus"))
    row.update(probed_at=routing._iso(at), last_attempt_at=routing._iso(at), last_good_at=routing._iso(at))
    _cache(home, dict(FIGURES, **{SECOND: row}))
    routing.record_pace_samples(at)
    _report_open(home)
    now = clock.advance(minutes=3)
    view = routing.read_view(now)
    entry = _entry(view)
    assert entry["state"] != routing.LIMITED and "reset_reported" in entry
    assert [m["model"].casefold() for m in entry.get("models") or []] == ["opus"]
    line = _line(routing_page.page(view, now))
    assert "Reset reported 14:00Z; figures pending" in line, line
    assert "limit reached, back" in line and "opus" in line.casefold(), line


# ---- review round 3 (Codex) -----------------------------------------------------------------


def _two_windows(week_used, week_at, five_used, five_at, now) -> dict:
    five = {"name": "five_hour", "used_pct": five_used, "resets_at": routing._iso(T0 + timedelta(hours=3)),
            "window_mins": 300, "observed_at": routing._iso(five_at)}
    row = _row("Claude second", "claude", "/logins/claude-second",
               _week(week_used, RESETS, observed_at=routing._iso(week_at)), five)
    row.update(probed_at=routing._iso(now), last_attempt_at=routing._iso(now), last_good_at=routing._iso(now))
    return row


def _pre_reset_100_then_99(home, week_at_after: bool):
    # Weekly 99% at 13:56; five-hour 100% at 14:01; reset reported 14:02;
    # five-hour 99% at 14:03, same reset time (Codex's inputs). The 14:01 and
    # 14:03 samples share a five-minute bucket.
    clock = home["clock"]
    week_at = T0 - timedelta(minutes=4)
    at = clock.advance(minutes=1)
    _cache(home, dict(FIGURES, **{SECOND: _two_windows(99, week_at, 100, at, at)}))
    routing.record_pace_samples(at)
    clock.advance(minutes=1)
    _report_open(home)
    now = clock.advance(minutes=1)
    _cache(home, dict(FIGURES, **{SECOND: _two_windows(5 if week_at_after else 99, now if week_at_after else week_at,
                                                       99, now, now)}))
    routing.record_pace_samples(now)
    return now


def test_a_pre_reset_100_does_not_close_a_fresh_window_while_another_is_pending(home):
    now = _pre_reset_100_then_99(home, week_at_after=False)
    view = routing.read_view(now)
    entry = _entry(view)
    assert entry.get("reset_reported", {}).get("windows") == ["weekly"]
    pace = {p["window"]: p for p in entry.get("pace") or []}
    assert (pace["five_hour"].get("plan") or {}).get("verdict") != "limit_reached", pace["five_hour"]
    advice = routing_advice.account_advice(view, entry, now)
    assert advice["verdict"] != "closed", advice


def test_a_pre_reset_100_does_not_close_once_every_window_is_fresh(home):
    now = _pre_reset_100_then_99(home, week_at_after=True)
    view = routing.read_view(now)
    entry = _entry(view)
    assert "reset_reported" not in entry
    pace = {p["window"]: p for p in entry.get("pace") or []}
    assert (pace["five_hour"].get("plan") or {}).get("verdict") != "limit_reached", pace["five_hour"]
    assert routing_advice.account_advice(view, entry, now)["verdict"] != "closed"


# ---- full-suite round: an open report is a reset only when it contradicts the figures -----------


def test_an_open_report_that_agrees_with_the_figures_is_no_reset(home):
    # 40% read 13:56, a conversation reports the account open at 14:00: the
    # figures stand, nothing is pending and the pace keeps its reading.
    clock = home["clock"]
    _read(home, 40, T0 - timedelta(minutes=4))
    _report_open(home)
    now = clock.advance(minutes=6)
    view = routing.read_view(now)
    entry = _entry(view)
    assert "reset_reported" not in entry
    pace = {p["window"]: p for p in entry.get("pace") or []}
    assert pace["weekly"].get("used_pct") == 40
    assert "Reset reported" not in _line(routing_page.page(view, now))


# ---- review round 4 (Codex) -----------------------------------------------------------------


def test_cached_figures_with_no_sample_still_mark_the_reset(home):
    # Cache holds weekly 100% at 13:56, never sampled; reset reported 14:00.
    clock = home["clock"]
    _cache(home, dict(FIGURES, **{SECOND: _second(100, T0 - timedelta(minutes=4))}))
    _report_open(home)
    now = clock.advance(minutes=3)
    view = routing.read_view(now)
    entry = _entry(view)
    assert entry["state"] != routing.LIMITED
    assert entry.get("reset_reported", {}).get("windows") == ["weekly"], entry.get("reset_reported")
    line = _line(routing_page.page(view, now))
    assert "LIMIT REACHED" not in line and "Reset reported 14:00Z; figures pending" in line, line
    assert routing_advice.account_advice(view, entry, now)["verdict"] != "closed"


def test_a_model_limit_alone_does_not_make_an_open_report_a_reset(home):
    # Whole account 40%, its Opus window 100%, both at 13:56; an ordinary
    # open report at 14:00: no reset, the account's figures and Opus's limit stand.
    clock = home["clock"]
    at = T0 - timedelta(minutes=4)
    row = _row("Claude second", "claude", "/logins/claude-second", _week(40, RESETS),
               _week(100, RESETS, model="Opus", name="weekly_opus"))
    row.update(probed_at=routing._iso(at), last_attempt_at=routing._iso(at), last_good_at=routing._iso(at))
    _cache(home, dict(FIGURES, **{SECOND: row}))
    routing.record_pace_samples(at)
    _report_open(home)
    now = clock.advance(minutes=3)
    view = routing.read_view(now)
    entry = _entry(view)
    assert "reset_reported" not in entry
    pace = {p["window"]: p for p in entry.get("pace") or []}
    assert pace["weekly"].get("used_pct") == 40
    assert [m["model"].casefold() for m in entry.get("models") or []] == ["opus"]
    assert "Reset reported" not in _line(routing_page.page(view, now))
