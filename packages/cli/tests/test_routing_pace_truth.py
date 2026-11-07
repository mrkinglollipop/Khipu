"""Pace truth (routing as one page, slice 1, 2026-11-07). Four things the live
record said that were not true:

* Claude's five-hour window stopped being reported on 11-01, and read "reset
  passed; waiting for a new reading" for as long as its readings were kept.
* An idle GLM reports 0% with no reset time; the fold ignored that, so its
  old reset passed and the window read unknown.
* Idle Kimi and Muse five-hour windows, read about hourly and rolling over
  every five hours, flipped between idle, "last reading 0.7 h old", "only 2
  readings" and "reset passed".
* GLM at 96% used and Kimi at 96.25% read "plenty of time".
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from khipu import routing, routing_advice, routing_pace
from tests.test_routing_acceptance import AUTHOR  # noqa: F401

pytest_plugins = ("tests.test_routing_acceptance",)
NOW = datetime(2026, 11, 7, 20, tzinfo=timezone.utc)


def at(minutes_ago, used=0.0, reset=None, mac="m5", window=None, **extra):
    row = {"t": NOW - timedelta(minutes=minutes_ago), "used": used, "reset": reset, "mac": mac,
           "source": "probe", "t_err": 60, **extra}
    return row


def not_started(minutes_ago, mac="m5"):
    return at(minutes_ago, unstarted=True, mac=mac)


# --- GLM: 0% with no reset is a window not started, not an unknown --------

def event(used, resets_at=None, window="five_hour"):
    row = {"type": "usage_sample", "account": "glm|", "window": window, "witness_at": routing._iso(NOW),
           "used_pct": used, "t_err": 60, "source": "probe", "mac": "m5"}
    if resets_at:
        row["resets_at"] = resets_at
    return row


def test_a_zero_reading_with_no_reset_is_read_only_when_asked():
    assert routing.pace_sample(event(0.0)) is None, "compaction and older readers keep ignoring it"
    key, sample = routing.pace_sample(event(0.0), unstarted=True)
    assert key == ("glm|", "five_hour") and sample["unstarted"] is True and sample["reset"] is None
    # Only nothing used means not started: a level with no reset still can't be placed.
    assert routing.pace_sample(event(12.0), unstarted=True) is None
    assert routing.pace_sample(event(False), unstarted=True) is None
    with_reset = routing.pace_sample(event(0.0, "2026-11-08T00:00:00Z"), unstarted=True)[1]
    assert "unstarted" not in with_reset


def test_an_idle_glm_reads_idle_not_started_instead_of_its_old_reset_passing():
    old_cycle = [at(m, used=83.0, reset=NOW - timedelta(hours=20)) for m in (1500, 1440, 1380)]
    rows = old_cycle + [not_started(m) for m in (170, 115, 60, 5)]
    before = routing_pace.compute_pace([r for r in rows if not r.get("unstarted")], [], NOW, "five_hour")
    assert before["why"].startswith("reset passed")
    pace = routing_pace.compute_pace(rows, [], NOW, "five_hour")
    assert pace["why"] is None and pace["idle"] is True and pace["not_started"] is True
    assert pace["used_pct"] == 0 and pace["cycle_resets_at"] is None and pace["rate_pct_per_h"] == 0
    assert routing_pace.pace_line("GLM", pace, NOW) == "GLM five-hour: idle, not started, nothing used"
    assert not routing_advice._pace_reading_unknown(pace)
    assert routing_pace.forecast(pace, "glm|", "GLM", NOW)["availability"] == "known"


def test_a_not_started_window_still_goes_stale_and_a_started_one_wins_when_newer():
    stale = routing_pace.compute_pace([not_started(100)], [], NOW, "five_hour")
    assert "idle" not in stale and stale["why"] == "last reading 1.7 h old"
    started = [not_started(60), at(30, used=4.0, reset=NOW + timedelta(hours=4)),
               at(20, used=5.0, reset=NOW + timedelta(hours=4)), at(10, used=6.0, reset=NOW + timedelta(hours=4))]
    pace = routing_pace.compute_pace(started, [], NOW, "five_hour")
    assert "not_started" not in pace and pace["used_pct"] == 6.0
    assert pace["cycle_resets_at"] == routing._iso(NOW + timedelta(hours=4))


def test_caps_on_a_window_not_started_stay_unknown():
    caps = [{"kind": "pace_target", "pct": 100.0, "slack": 0.0, "revision": "r1"},
            {"kind": "share", "pct": 50.0, "revision": "r2"}]
    pace = routing_pace.compute_pace([not_started(m) for m in (60, 5)], caps, NOW, "five_hour")
    assert pace["idle"] is True
    assert pace["pace_target"]["verdict"] == "unknown" and pace["pace_target"]["why"] == "no reset time"
    assert pace["share_cap"] is None or pace["share_cap"]["verdict"] == "unknown"


def test_another_mac_reads_the_not_started_window_too():
    """The wire sends it; a group mixing it with started readings never breaks."""
    rows = [dict(r, _id=f"u{i}", window="five_hour") for i, r in enumerate(
        [at(400, used=10.0, reset=NOW - timedelta(hours=2)), at(300, used=20.0, reset=NOW - timedelta(hours=2)),
         not_started(200), not_started(100)])]
    send, hold = routing_pace.wire_choice(rows, NOW)
    assert {"u3"} <= send and not hold
    mine = [at(30, used=1.0, reset=NOW + timedelta(hours=4), mac="m1")]
    assert routing_pace.shared_readings(rows + mine, "m1", NOW, "five_hour") == rows + mine


# --- Idle Kimi and Muse: hourly reads, rolling resets --------------------

def rolling(minutes, used=0.0):
    """Readings about every 55 minutes of a window that resets every five hours."""
    out = []
    for m in minutes:
        t = NOW - timedelta(minutes=m)
        cycle_end = datetime(2026, 11, 7, 3, 23, tzinfo=timezone.utc)
        while cycle_end <= t:
            cycle_end += timedelta(hours=5)
        out.append(at(m, used=used, reset=cycle_end))
    return out


def test_an_idle_window_stays_idle_across_its_own_rollover():
    # 18:23Z is a reset: the newest reading is the only one of its cycle.
    rows = rolling((330, 275, 220, 165, 110, 55, 0))
    for offset in (0, 20, 40, 59, 70, 85):
        now = NOW + timedelta(minutes=offset)
        pace = routing_pace.compute_pace(rows, [], now, "five_hour")
        assert pace.get("idle") is True and pace["why"] is None, (offset, pace["why"])


def test_a_reset_passing_between_two_idle_reads_leaves_it_idle():
    # The newest reading, 10 minutes ago, named a reset 5 minutes ago (Muse at 10:16 for 10:22).
    reset = NOW - timedelta(minutes=5)
    rows = [at(m, reset=reset) for m in (130, 70, 10)]
    pace = routing_pace.compute_pace(rows, [], NOW, "five_hour")
    assert pace["idle"] is True and pace["why"] is None
    # Something used before the reset is no longer known: that stays unknown.
    used = [at(m, used=12.0, reset=reset) for m in (130, 70, 10)]
    assert routing_pace.compute_pace(used, [], NOW, "five_hour")["why"].startswith("reset passed")


def test_the_quiet_tolerance_is_an_hour_and_a_half():
    assert routing_pace.QUIET_READING_S == 5400
    rows = [at(m, used=12.0, reset=NOW + timedelta(hours=3)) for m in (140, 80)]
    pace = routing_pace.compute_pace(rows, [], NOW, "five_hour")
    assert pace["idle"] is True and pace["why"] is None
    late = [at(m, used=12.0, reset=NOW + timedelta(hours=3)) for m in (160, 95)]
    assert routing_pace.compute_pace(late, [], NOW, "five_hour")["why"] == "last reading 1.6 h old"


def test_moving_usage_is_never_called_idle_by_the_rollover_rule():
    reset_a, reset_b = NOW - timedelta(minutes=30), NOW + timedelta(hours=4, minutes=30)
    rows = [at(150, used=0.0, reset=reset_a), at(90, used=0.0, reset=reset_a), at(10, used=3.0, reset=reset_b)]
    assert "idle" not in routing_pace.compute_pace(rows, [], NOW, "five_hour")


# --- Claude's five-hour window: not reported, not unknown for ever --------

def claude_like(now=NOW, fresh_weekly=True):
    ghost_reset = datetime(2026, 11, 1, 3, tzinfo=timezone.utc)
    five = [{"t": datetime(2026, 11, 1, 0, m, tzinfo=timezone.utc), "used": 16.0, "reset": ghost_reset,
             "mac": "m5", "source": "probe", "t_err": 60} for m in (7, 27, 47)]
    newest_weekly = 20 if fresh_weekly else 600
    weekly = [at(m, used=68.0, reset=NOW + timedelta(days=2)) for m in range(newest_weekly + 1440, newest_weekly - 1, -60)]
    return {"five_hour": five, "weekly": weekly}


def test_a_window_its_reader_no_longer_lists_says_not_reported():
    caps = [{"kind": "pace_target", "pct": 100.0, "slack": 5.0, "revision": "r1"}]
    samples = claude_like()
    paces = [routing_pace.compute_pace(samples[w], caps, NOW, w) for w in ("five_hour", "weekly")]
    before = [{f: (p.get(f) or {}).get("verdict") for f in ("plan", "pace_target", "share_cap")} for p in paces]
    assert paces[0]["why"].startswith("reset passed")
    routing_pace.mark_not_reported(paces, samples, NOW)
    five, weekly = paces
    assert five["not_reported"] is True
    assert five["why"] == "not reported since Nov 1: its reader no longer lists this window"
    assert five["pace_target"]["why"] == "no reset time" and five["pace_target"]["verdict"] == "unknown"
    assert "not_reported" not in weekly and weekly["why"] is None
    # Only words change: every verdict stays as computed.
    assert before == [{f: (p.get(f) or {}).get("verdict") for f in ("plan", "pace_target", "share_cap")}
                      for p in paces]
    assert routing_advice._pace_reading_unknown(five)
    assert routing_pace.pace_line("Claude", five, NOW) == (
        "Claude five-hour: not reported since Nov 1: its reader no longer lists this window")
    assert routing.headline_pace({"pace": paces}) is weekly


def test_a_reader_that_stopped_altogether_is_not_called_not_reported():
    samples = claude_like(fresh_weekly=False)
    paces = [routing_pace.compute_pace(samples[w], [], NOW, w) for w in ("five_hour", "weekly")]
    routing_pace.mark_not_reported(paces, samples, NOW)
    assert not any(p.get("not_reported") for p in paces)
    assert paces[0]["why"].startswith("reset passed")


def test_a_window_late_by_less_than_six_hours_is_late_not_unreported():
    samples = {"five_hour": [at(m, used=16.0, reset=NOW - timedelta(minutes=30)) for m in (300, 240, 200)],
               "weekly": claude_like()["weekly"]}
    paces = [routing_pace.compute_pace(samples[w], [], NOW, w) for w in ("five_hour", "weekly")]
    routing_pace.mark_not_reported(paces, samples, NOW)
    assert "not_reported" not in paces[0] and paces[0]["why"].startswith("reset passed")


def _grok(replay, window, minutes_ago, used, reset_in=None):
    out = {"type": "usage_sample", "account": "grok|", "window": window, "t_err": 60, "source": "probe",
           "witness_at": routing._iso(replay.now - timedelta(minutes=minutes_ago)), "used_pct": used,
           "written_by": {"kind": "probe"}, "via": "probe"}
    if reset_in is not None:
        out["resets_at"] = routing._iso(replay.now + timedelta(minutes=reset_in))
    return out


def _paces(replay):
    grok = next(a for a in replay.view()["accounts"] if a["key"] == "grok|")
    return {p["window"]: p for p in grok["pace"]}


def test_the_fold_says_not_reported_for_a_window_its_reader_dropped(replay):
    weekly = [_grok(replay, "weekly", m, 40.0, reset_in=3 * 1440) for m in range(1500, -1, -60)]
    ghost = [_grok(replay, "five_hour", m, 16.0, reset_in=-420) for m in (760, 740, 720)]
    replay.write(*ghost, *weekly)
    paces = _paces(replay)
    assert paces["five_hour"]["not_reported"] is True
    assert paces["five_hour"]["why"].startswith("not reported since ")
    assert paces["weekly"]["why"] is None


def test_the_fold_reads_an_idle_glm_style_window_as_not_started(replay):
    weekly = [_grok(replay, "weekly", m, 40.0, reset_in=3 * 1440) for m in range(1500, -1, -60)]
    old = [_grok(replay, "five_hour", m, 83.0, reset_in=-600) for m in (1000, 980, 960)]
    idle = [_grok(replay, "five_hour", m, 0.0) for m in (170, 115, 60, 5)]
    replay.write(*old, *idle, *weekly)
    five = _paces(replay)["five_hour"]
    assert five["why"] is None and five["idle"] is True and five["not_started"] is True


# --- Nearly out: never "plenty of time" at 96% ----------------------------

def row(used, hours_to_reset=50.0, name="weekly", mins=None):
    window = {"name": name, "used_pct": used, "resets_at": routing._iso(NOW + timedelta(hours=hours_to_reset))}
    if mins:
        window["window_mins"] = mins
    return {"ok": True, "observed_at": routing._iso(NOW), "windows": [window]}


def test_ninety_six_percent_used_is_nearly_out_never_plenty_of_time():
    fact = routing.plan_fact(row(96.0), NOW)
    assert fact["words"] == "nearly out" and fact["left_pct"] == 4.0
    assert routing.pace_words(fact, NOW).endswith("; nearly out")
    assert "plenty of time" not in routing.pace_words(fact, NOW)
    assert routing.plan_fact(row(96.25, 24 * 20, "monthly", 44640), NOW)["words"] == "nearly out"


def test_the_nearly_out_line_is_under_ten_percent_left_and_above_nothing():
    assert routing.plan_fact(row(89.0), NOW)["words"] == "plenty of time"
    assert routing.plan_fact(row(90.5), NOW)["words"] == "nearly out"
    assert routing.plan_fact(row(100.0), NOW)["words"] == "nothing left until it resets"
    assert routing_pace.nearly_out(95) and not routing_pace.nearly_out(90) and not routing_pace.nearly_out(100)
    assert not routing_pace.nearly_out(None)


def test_an_idle_pace_line_at_ninety_six_percent_says_nearly_out():
    rows = [at(60 * h, used=96.0, reset=NOW + timedelta(days=2)) for h in range(30, -1, -1)]
    pace = routing_pace.compute_pace(rows, [], NOW, "weekly")
    assert pace["idle"] is True
    assert routing_pace.pace_line("GLM", pace, NOW) == (
        "GLM weekly: nearly out; idle, no use to forecast; 96% used, 4% left")
    moving = [at(60 * h, used=90.0 + (30 - h) * 0.2, reset=NOW + timedelta(days=2)) for h in range(30, -1, -1)]
    line = routing_pace.pace_line("Kimi", routing_pace.compute_pace(moving, [], NOW, "weekly"), NOW)
    assert line.startswith("Kimi weekly: nearly out, 4% left; ") and len(line) <= 160
    full = [at(60 * h, used=100.0, reset=NOW + timedelta(days=2)) for h in range(30, -1, -1)]
    assert "nearly out" not in routing_pace.pace_line("GLM", routing_pace.compute_pace(full, [], NOW, "weekly"), NOW)
