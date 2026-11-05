"""An idle account is not unknown. Idle windows are stored hourly and a five-hour
pace reads one hour, so it used to say "only 2 readings" or "last reading 0.7 h
old" for ever. The words now say which is true: idle, not enough history, the
reader failed, and what is left when that is known. No more readings, no more polls."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from khipu import routing, routing_advice, routing_pace
from tests.test_routing_acceptance import AUTHOR  # noqa: F401

pytest_plugins = ("tests.test_routing_acceptance",)
NOW = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
RESET = NOW + timedelta(hours=3)


def reading(minutes_ago, used=12.0, reset=RESET):
    return {"t": NOW - timedelta(minutes=minutes_ago), "used": used, "reset": reset, "mac": "m",
            "source": "probe", "t_err": 60}


def five_hour(rows, caps=(), now=NOW):
    return routing_pace.compute_pace(rows, list(caps), now, "five_hour")


HOURLY = [reading(m) for m in (130, 70, 10)]


def test_the_one_hour_idle_fixture_says_idle_and_keeps_the_allowance():
    pace = five_hour(HOURLY)
    assert pace["idle"] is True and pace["why"] is None
    assert pace["rate_pct_per_h"] == 0 and pace["plan"]["verdict"] == "underpacing"
    line = routing_pace.pace_line("Claude", pace, NOW)
    assert line == "Claude five-hour: idle, no use to forecast; 12% used, 88% left"
    assert not routing_advice._pace_reading_unknown(pace)


@pytest.mark.parametrize("age", [0, 5, 21, 40, 59, 74])
def test_idle_holds_all_the_way_through_the_hourly_cadence(age):
    rows = [reading(age + 120), reading(age + 60), reading(age)]
    pace = five_hour(rows)
    assert pace.get("idle") is True and pace["why"] is None, pace["why"]
    assert pace["plan"]["needed_pct_per_h"] is not None


def test_a_reading_older_than_the_hourly_cadence_is_no_longer_idle():
    pace = five_hour([reading(200), reading(140), reading(80)])
    assert "idle" not in pace
    assert pace["why"] == "last reading 1.3 h old"
    line = routing_pace.pace_line("Claude", pace, NOW)
    assert line == ("Claude five-hour: no fresh reading, its reader is failing or has not run: last reading 1.3 h old")
    assert routing_advice._pace_reading_unknown(pace)


def test_usage_that_was_moving_and_stopped_being_read_is_a_late_reader_not_quiet():
    pace = five_hour([reading(75, used=9.0), reading(55, used=10.0), reading(35, used=11.0)])
    assert pace["why"] == "last reading 0.6 h old"
    assert "no fresh reading" in routing_pace.pace_line("Claude", pace, NOW)
    assert "% left" not in routing_pace.pace_line("Claude", pace, NOW)


def drive(start_used=12.0, *, moves=(), minutes=6 * 60, poll=5):
    """What the writer does with the polls that run anyway: one candidate per
    poll through ``should_sample``, the forecast read after each."""
    stored, lines = [], []
    used = start_used
    start = NOW - timedelta(minutes=minutes)
    for minute in range(0, minutes + 1, poll):
        at = start + timedelta(minutes=minute)
        used += sum(by for when, by in moves if when == minute)
        candidate = {"window": "five_hour", "t": at, "used": used, "mac": "m", "source": "probe"}
        if routing_pace.should_sample(stored, candidate):
            stored.append(dict(candidate, reset=at + timedelta(hours=4), t_err=60))
            for row in stored:
                row["reset"] = at + timedelta(hours=4)
        pace = routing_pace.compute_pace([dict(s) for s in stored], [], at, "five_hour")
        lines.append((minute, routing_pace.pace_line("Claude", pace, at), pace))
    return stored, lines


def test_a_quiet_day_stores_hourly_and_reads_idle_after_the_first_hour():
    stored, lines = drive()
    assert len(stored) == 7, "no more readings than an idle window stored before"
    for minute, line, _pace in lines:
        if minute >= 60:
            assert line == "Claude five-hour: idle, no use to forecast; 12% used, 88% left", (minute, line)
        else:
            assert line.startswith("Claude five-hour: not enough history for a pace: only 1 reading in the last 1 h"), (minute, line)
            assert line.endswith("12% used, 88% left"), (minute, line)


def test_changed_usage_gives_a_rate_again_through_honest_words():
    moves = [(300, 3.0), (310, 2.0), (320, 2.0), (330, 2.0), (340, 2.0), (350, 2.0)]
    stored, lines = drive(moves=moves)
    by_minute = {minute: (line, pace) for minute, line, pace in lines}
    assert "idle" in by_minute[295][0]
    just_moved, pace = by_minute[300]
    assert "idle" not in pace and "idle" not in just_moved
    assert "not enough history for a pace: only " in just_moved and "15% used, 85% left" in just_moved
    # The fast cadence the move turned on makes the three readings pace reads.
    settled, pace = by_minute[350]
    assert pace["why"] is None and "idle" not in pace
    assert pace["rate_pct_per_h"] > 0 and pace["plan"]["verdict"] != "unknown"
    assert "at the trailing average" in settled and "idle" not in settled
    assert len([s for s in stored if s["t"] >= NOW - timedelta(minutes=60)]) >= 6


def test_thin_history_says_so_with_what_is_left():
    rows = [{"t": NOW - timedelta(minutes=30 * i), "used": 64.0 - i * 0.2, "reset": NOW + timedelta(days=2),
             "mac": "m", "source": "probe", "t_err": 60} for i in range(16)]
    pace = routing_pace.compute_pace(rows, [], NOW, "weekly")
    assert pace["why"] == "readings cover only 7.5 of the last 24 h"
    assert routing_pace.pace_line("Claude", pace, NOW) == (
        "Claude weekly: not enough history for a pace: readings cover only 7.5 of the last 24 h; 64% used, 36% left")
    assert not routing_advice._pace_reading_unknown(pace)


def test_a_flat_week_with_no_reading_before_its_window_is_thin_history_not_idle():
    rows = [{"t": NOW - timedelta(hours=i), "used": 40.0, "reset": NOW + timedelta(days=2), "mac": "m",
             "source": "probe", "t_err": 60} for i in range(10)]
    pace = routing_pace.compute_pace(rows, [], NOW, "weekly")
    assert "idle" not in pace and pace["why"].startswith("readings cover only")


def test_a_flat_week_reads_idle_and_pace_targets_and_caps_still_answer():
    rows = [{"t": NOW - timedelta(hours=i), "used": 40.0, "reset": NOW + timedelta(days=2), "mac": "m",
             "source": "probe", "t_err": 60} for i in range(30)]
    caps = [{"kind": "pace_target", "pct": 100, "slack": 0, "revision": "r1"},
            {"kind": "share", "pct": 90, "revision": "r2"}]
    pace = routing_pace.compute_pace(rows, caps, NOW, "weekly")
    assert pace["idle"] is True
    assert pace["pace_target"]["verdict"] in ("within", "recovering", "ahead")
    assert pace["share_cap"]["verdict"] == "underpacing"
    assert pace["used_pct"] == 40


def test_idle_never_hides_a_reached_ceiling_or_a_target_it_is_ahead_of():
    full = [reading(m, used=100.0) for m in (130, 70, 10)]
    assert five_hour(full)["plan"]["verdict"] == "limit_reached"
    assert "limit reached" in routing_pace.pace_line("Claude", five_hour(full), NOW)
    capped = five_hour(HOURLY, [{"kind": "share", "pct": 10, "revision": "r"}])
    assert capped["share_cap"]["verdict"] == "cap_reached"
    assert "cap reached" in routing_pace.pace_line("Claude", capped, NOW)
    ahead = five_hour(HOURLY, [{"kind": "pace_target", "pct": 10, "slack": 0, "revision": "r"}])
    assert ahead["pace_target"]["verdict"] == "ahead"
    assert "ahead of its pace target" in routing_pace.pace_line("Claude", ahead, NOW)


def test_a_reset_or_a_disagreement_is_never_called_idle():
    moved = [reading(130), reading(70), reading(10, reset=RESET + timedelta(days=2))]
    assert "idle" not in five_hour(moved)
    fell = [reading(130, used=50.0), reading(70, used=50.0), reading(10, used=2.0)]
    assert "idle" not in five_hour(fell)
    passed = [reading(130, reset=NOW - timedelta(minutes=1)), reading(70, reset=NOW - timedelta(minutes=1))]
    pace = five_hour(passed)
    assert "idle" not in pace and pace["why"].startswith("reset passed")


def test_the_view_is_folded_again_when_an_idle_reading_runs_out():
    at = routing_pace.next_change(HOURLY, NOW, "five_hour")
    assert at is not None and at <= HOURLY[-1]["t"] + timedelta(seconds=routing_pace.IDLE_READING_S)
    later = NOW + timedelta(minutes=55)
    assert routing_pace.next_change(HOURLY, later, "five_hour") == HOURLY[-1]["t"] + timedelta(seconds=routing_pace.IDLE_READING_S)


def test_a_reader_that_failed_says_so_and_caps_keep_their_place(replay):
    routing.set_cap("Grok", window="weekly", kind="share", pct=90, via="app")
    target = replay.data / "state" / "usage.json"
    target.parent.mkdir(exist_ok=True)
    at = routing._iso(replay.now)
    target.write_text(json.dumps({"version": 2, "rows": {"grok|": {
        "ok": False, "error": "not_signed_in", "probed_at": at, "last_attempt_at": at, "windows": []}}}))
    grok = next(a for a in replay.view()["accounts"] if a["key"] == "grok|")
    assert (grok["state"], grok["reason"]) == (routing.DOWN, "not signed in")
    pace = next(p for p in grok["pace"] if p["window"] == "weekly")
    assert pace["why"] == "reader failed: not signed in"
    assert routing_pace.pace_line("Grok", pace, replay.now) == "Grok weekly: reader failed: not signed in"
    assert [c["pct"] for c in grok["caps"]] == [90]
    assert routing_advice._pace_reading_unknown(pace)


def test_no_readings_yet_is_not_called_a_failed_reader():
    pace = routing_pace.compute_pace([], [], NOW, "weekly")
    assert routing_pace.pace_line("Grok", pace, NOW) == (
        "Grok weekly: pace unknown at the trailing average: this account can't be probed")
