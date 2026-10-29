"""Pace for every usage window with a known length and reset time.

The five-hour and weekly windows keep their own parameters; any other window
(Kimi's month, a day, a ``<n>_min`` window) is paced at its own length.
Hermetic: synthetic samples and a faked Kimi transport, no network.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from khipu import routing, routing_pace, usage
from tests.test_routing_acceptance import T0, replay  # noqa: F401 - the routing replay fixture
from tests.test_usage_kimi import REPLY, _define_kimi, _reply, kimi  # noqa: F401 - the Kimi transport fixture

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
DAY = 86400


def samp(t, used, *, reset, length=None, mac="m1"):
    out = {"t": NOW + timedelta(seconds=t), "used": float(used), "reset": reset, "mac": mac,
           "source": "probe", "cycle": None, "t_err": 0}
    if length is not None:
        out["window_s"] = length
    return out


def steady(rate, used, *, reset, length=None, span=DAY, step=1800):
    """Readings every ``step`` over the last ``span`` seconds, ending at ``used``."""
    return [samp(t, used - rate * -t / 3600, reset=reset, length=length) for t in range(-span, 1, step)]


def target(pct=100.0, slack=0.0):
    return {"kind": "pace_target", "pct": pct, "slack": slack, "revision": "r1"}


# ---- the windows ---------------------------------------------------------------------


def test_named_windows_keep_their_own_parameters():
    assert routing_pace.window_params("weekly") is routing_pace.WINDOWS["weekly"]
    assert routing_pace.window_params("five_hour") is routing_pace.WINDOWS["five_hour"]
    assert routing_pace.window_params("five_hour", 999) is routing_pace.WINDOWS["five_hour"]


@pytest.mark.parametrize("window, length, reset, expect_s, style", [
    ("monthly", 30 * DAY, None, 30 * DAY, "weekly"),
    ("daily", None, None, DAY, "five_hour"),
    ("120_min", None, None, 7200, "five_hour"),
    ("weekly_opus", None, None, 7 * DAY, "weekly"),
    # A month's length from its reset alone: February 2027 has 28 days, 2028 has 29.
    ("monthly", None, datetime(2027, 3, 1, tzinfo=timezone.utc), 28 * DAY, "weekly"),
    ("monthly", None, datetime(2028, 3, 1, tzinfo=timezone.utc), 29 * DAY, "weekly"),
    ("monthly", None, datetime(2026, 11, 1, tzinfo=timezone.utc), 31 * DAY, "weekly"),
])
def test_any_window_of_known_length_gets_parameters(window, length, reset, expect_s, style):
    params = routing_pace.window_params(window, length, reset)
    named = routing_pace.WINDOWS[style]
    assert params["window_s"] == expect_s
    assert (params["bucket_s"], params["sample_s"], params["gap_s"]) == (
        named["bucket_s"], named["sample_s"], named["gap_s"])
    assert params["trailing_s"] == min(named["trailing_s"], expect_s)


@pytest.mark.parametrize("window, length", [("quota", None), ("refused", None), ("window", None),
                                            ("monthly", None), ("quota", 0), (None, None)])
def test_a_window_with_no_length_cannot_be_paced(window, length):
    assert routing_pace.window_params(window, length) is None


def test_a_window_shorter_than_an_hour_trails_over_its_own_length():
    params = routing_pace.window_params("30_min")
    assert params["trailing_s"] == 1800 and params["trailing_h"] == 0.5


# ---- Kimi's month --------------------------------------------------------------------


def test_mid_month_pace_says_underpacing_unused_and_the_rate_that_lands_on_100():
    month = 30 * DAY
    reset = NOW + timedelta(days=15)
    samples = steady(0.1, 42.4, reset=reset, length=month)
    pace = routing_pace.compute_pace(samples, [target()], NOW, "monthly")
    assert pace["window"] == "monthly" and pace["window_s"] == month
    assert pace["trailing_h"] == 24 and pace["why"] is None
    assert pace["rate_pct_per_h"] == pytest.approx(0.1)
    assert pace["plan"]["verdict"] == "underpacing"
    assert pace["plan"]["needed_pct_per_h"] == pytest.approx(57.6 / 360)
    assert pace["plan"]["unused_at_reset_pct"] == pytest.approx(57.6 - 36)
    # Half the month gone: an even pace to 100% allows 50%.
    assert pace["pace_target"]["allowance_pct"] == pytest.approx(50.0)
    assert pace["pace_target"]["verdict"] == "within"
    line = routing_pace.pace_line("Kimi", routing_pace.at(pace, NOW), NOW)
    assert line == ("Kimi monthly: underpacing at the trailing average (0.1%/h); about 21.6% unused "
                    "at the reset; 0.2%/h lands on 100%")


def test_ahead_of_an_even_monthly_pace_reads_used_against_its_allowance():
    month = 30 * DAY
    samples = steady(0.5, 70, reset=NOW + timedelta(days=15), length=month)
    pace = routing_pace.compute_pace(samples, [target()], NOW, "monthly")
    assert pace["pace_target"]["verdict"] == "ahead"
    assert routing_pace.pace_line("Kimi", routing_pace.at(pace, NOW), NOW) == (
        "Kimi monthly: ahead of its pace target (70% used against a 50.00% allowance)")
    # Six days later with no more use the allowance has caught up with it.
    later = routing_pace.at(pace, NOW + timedelta(days=6))
    assert later["pace_target"]["allowance_pct"] == pytest.approx(70.0)


def test_near_the_monthly_reset_the_unused_share_is_what_the_rate_cannot_reach():
    reset = NOW + timedelta(hours=2)
    pace = routing_pace.compute_pace(steady(1.0, 90, reset=reset, length=30 * DAY), [target()], NOW, "monthly")
    assert pace["plan"]["verdict"] == "underpacing"
    assert pace["plan"]["needed_pct_per_h"] == pytest.approx(5.0)
    assert pace["plan"]["unused_at_reset_pct"] == pytest.approx(8.0)
    assert pace["pace_target"]["allowance_pct"] == pytest.approx(100 * (30 * 24 - 2) / (30 * 24))


def test_outpacing_a_month_runs_out_before_its_reset():
    reset = NOW + timedelta(days=10)
    pace = routing_pace.compute_pace(steady(1.0, 80, reset=reset, length=30 * DAY), [], NOW, "monthly")
    assert pace["plan"]["verdict"] == "outpacing"
    assert pace["plan"]["runs_out_at"] == routing_pace.iso(NOW + timedelta(hours=20))
    line = routing_pace.pace_line("Kimi", pace, NOW)
    assert line.startswith("Kimi monthly: outpacing at the trailing average (1%/h); runs out ")
    assert line.endswith("0.08%/h lands on 100%")


def test_just_after_the_monthly_reset_the_new_cycle_is_the_one_read():
    old_reset = NOW - timedelta(hours=1)
    new_reset = datetime(2026, 11, 20, 11, tzinfo=timezone.utc)
    before = [samp(t, 95 + t / 86400, reset=old_reset, length=30 * DAY) for t in range(-DAY, -3600, 1800)]
    after = [samp(t, 0.5 + (t + 1800) / 3600 * 0.2, reset=new_reset, length=31 * DAY)
             for t in range(-1800, 1, 900)]
    pace = routing_pace.compute_pace(before + after, [target()], NOW, "monthly")
    assert pace["cycle_resets_at"] == "2026-11-20T11:00:00Z"
    assert pace["window_s"] == 31 * DAY
    assert pace["used_pct"] == pytest.approx(0.6)
    assert pace["plan"]["verdict"] != "limit_reached"
    # The month has barely begun: an allowance of a sliver, used is over it.
    assert pace["pace_target"]["allowance_pct"] == pytest.approx(100 * 3600 / (31 * DAY))


@pytest.mark.parametrize("reset, days", [
    (datetime(2027, 3, 1, tzinfo=timezone.utc), 28),
    (datetime(2026, 11, 1, tzinfo=timezone.utc), 31),
])
def test_month_length_comes_from_the_reset_when_a_sample_carries_none(reset, days):
    now = reset - timedelta(days=7)
    samples = [dict(s, t=s["t"] - NOW + now) for s in steady(0.1, 30, reset=reset)]
    pace = routing_pace.compute_pace(samples, [target()], now, "monthly")
    assert pace["window_s"] == days * DAY
    assert pace["pace_target"]["allowance_pct"] == pytest.approx(100 * (days - 7) / days)


def test_kimi_month_length_is_counted_in_the_resets_own_timezone(kimi):  # noqa: F811
    reply = json.loads(json.dumps(REPLY))
    # 1 March 00:00 in UTC+8 is still 28 February in UTC: the month that ends
    # is February (28 days), not January's 31.
    for key in ("limit_month_total", "limit_month_code"):
        reply["usages"][key]["reset_time"] = "2027-03-01T00:00:00+08:00"
    _reply(kimi, reply)
    row = usage.probe_account({"label": "Kimi", "harness": "kimi", "home": None, "provider": "kimi",
                               "quota_key": "kimi:main"})
    month = next(w for w in row["windows"] if w["name"] == "monthly")
    assert month["resets_at"] == "2027-02-28T16:00:00Z"
    assert month["window_mins"] == 28 * 24 * 60


# ---- a generic window ----------------------------------------------------------------


def test_a_two_hour_window_is_paced_at_its_own_length():
    reset = NOW + timedelta(minutes=60)
    samples = [samp(t, 20 + 6 * (t + 3600) / 3600, reset=reset, length=7200) for t in range(-3600, 1, 300)]
    pace = routing_pace.compute_pace(samples, [target()], NOW, "120_min")
    assert pace["window_s"] == 7200 and pace["trailing_h"] == 1
    assert pace["rate_pct_per_h"] == pytest.approx(6.0)
    assert pace["plan"]["verdict"] == "underpacing"
    assert pace["plan"]["unused_at_reset_pct"] == pytest.approx(74 - 6)
    assert pace["pace_target"]["allowance_pct"] == pytest.approx(50.0)
    assert routing_pace.pace_line("Codex", pace, NOW) == (
        "Codex 120-minute: underpacing at the trailing average (6%/h); about 68% unused at the reset; "
        "74%/h lands on 100%")


def test_next_change_and_sampling_cadence_follow_the_windows_kind():
    reset = NOW + timedelta(days=15)
    samples = steady(0.1, 42.4, reset=reset, length=30 * DAY)
    # The oldest reading leaves the 24-hour trailing window next.
    assert routing_pace.next_change(samples, NOW, "monthly") == NOW + timedelta(seconds=1800)
    prior = [{"window": "monthly", "t": NOW - timedelta(minutes=50), "used": 40.0, "mac": "m", "source": "probe"},
             {"window": "monthly", "t": NOW - timedelta(minutes=20), "used": 40.6, "mac": "m", "source": "probe"}]
    candidate = {"window": "monthly", "t": NOW, "used": 40.8, "mac": "m", "source": "probe", "window_s": 30 * DAY}
    assert routing_pace.should_sample(prior, candidate) is False  # 20 min < the 30-minute monthly cadence
    candidate = dict(candidate, window="daily", window_s=None)
    assert routing_pace.should_sample([dict(p, window="daily") for p in prior], candidate) is True


# ---- five-hour and weekly unchanged ----------------------------------------------------


def test_five_hour_and_weekly_pace_carry_no_window_length_field():
    reset = NOW + timedelta(days=3)
    weekly = routing_pace.compute_pace(steady(0.5, 40, reset=reset), [target()], NOW, "weekly")
    assert "window_s" not in weekly
    five = routing_pace.compute_pace(
        [samp(t, 10 + t / 360, reset=NOW + timedelta(hours=2)) for t in range(-3600, 1, 300)], [], NOW, "five_hour")
    assert "window_s" not in five
    assert routing_pace.pace_line("Claude", five, NOW).startswith("Claude five-hour: ")


# ---- end to end: Kimi's month through the routing view ------------------------------------


def test_routing_reads_kimis_month_with_pace(replay, kimi, monkeypatch):  # noqa: F811
    _define_kimi(replay)
    monkeypatch.setattr(usage, "_now", lambda: replay.now)
    reply = json.loads(json.dumps(REPLY))
    reply["usages"]["limit_5h"]["reset_time"] = routing._iso(T0 + timedelta(hours=4))
    month_reset = T0 + timedelta(days=12)
    for i, ratio in enumerate((0.30, 0.31, 0.32, 0.33)):
        for key in ("limit_month_total", "limit_month_code"):
            reply["usages"][key] = {"used_ratio": ratio, "reset_time": routing._iso(month_reset)}
        _reply(kimi, reply)
        assert usage.refresh_due(accounts=usage.http_quota_accounts(), max_age=0)["refreshed"] == 1
        routing.record_pace_samples(replay.now)
        replay.advance(1800)
    events = [json.loads(line) for line in routing.events_file().read_text().splitlines() if line.strip()]
    monthly = [e for e in events if e.get("type") == "usage_sample" and e.get("window") == "monthly"]
    assert monthly and all(e["window_mins"] == usage._kimi_month_mins(routing._iso(month_reset)) for e in monthly)
    five = [e for e in events if e.get("type") == "usage_sample" and e.get("window") == "five_hour"]
    assert five and all("window_mins" not in e for e in five)
    entry = next(e for e in replay.view()["accounts"] if e["key"] == "kimi:main")
    paces = {p["window"]: p for p in entry.get("pace") or []}
    assert set(paces) == {"five_hour", "monthly"}
    month = paces["monthly"]
    assert month["cycle_resets_at"] == routing._iso(month_reset)
    assert month["window_s"] == monthly[0]["window_mins"] * 60
    assert month["used_pct"] == 33.0 and month["rate_pct_per_h"] == pytest.approx(2.0)
    line = routing_pace.pace_line("Kimi", routing_pace.at(month, replay.now), replay.now)
    assert line.startswith("Kimi monthly: ")


def _kimi_month(replay, kimi, monkeypatch, ratios=(0.30, 0.31, 0.32, 0.33)):  # noqa: F811
    _define_kimi(replay)
    monkeypatch.setattr(usage, "_now", lambda: replay.now)
    reply = json.loads(json.dumps(REPLY))
    reply["usages"]["limit_5h"]["reset_time"] = routing._iso(T0 + timedelta(hours=4))
    for ratio in ratios:
        for key in ("limit_month_total", "limit_month_code"):
            reply["usages"][key] = {"used_ratio": ratio, "reset_time": routing._iso(T0 + timedelta(days=12))}
        _reply(kimi, reply)
        usage.refresh_due(accounts=usage.http_quota_accounts(), max_age=0)
        routing.record_pace_samples(replay.now)
        replay.advance(1800)


def _kimi_entry(replay):  # noqa: F811
    return next(e for e in replay.view()["accounts"] if e["key"] == "kimi:main")


def test_the_owner_sets_a_pace_target_on_the_month(replay, kimi, monkeypatch):  # noqa: F811
    _kimi_month(replay, kimi, monkeypatch)
    routing.set_cap("kimi:main", window="monthly", kind="pace_target", pct=100, slack=0, via="app")
    entry = _kimi_entry(replay)
    assert [(c["kind"], c["window"]) for c in entry["caps"]] == [("pace_target", "monthly")]
    target = next(p for p in entry["pace"] if p["window"] == "monthly")["pace_target"]
    assert target["pct"] == 100 and target["verdict"] != "unknown" and target["allowance_pct"] is not None
    routing.set_cap("kimi:main", window="daily", kind="share", pct=50, via="app")
    routing.remove_cap("kimi:main", window="monthly", kind="pace_target", via="app")
    assert [(c["kind"], c["window"]) for c in _kimi_entry(replay)["caps"]] == [("share", "daily")]


@pytest.mark.parametrize("window", ["fortnight", "", "0_min", "monthly; rm"])
def test_a_window_with_no_known_length_takes_no_cap(replay, kimi, monkeypatch, window):  # noqa: F811
    _define_kimi(replay)
    with pytest.raises(routing.RouteError):
        routing.set_cap("kimi:main", window=window, kind="pace_target", pct=100, slack=0, via="app")
    with pytest.raises(routing.RouteError):
        routing.remove_cap("kimi:main", window=window, kind="pace_target", via="app")


def test_the_cli_and_owner_actions_take_the_month(replay, kimi, monkeypatch, khipu_app):  # noqa: F811
    from khipu import cli, owner_act

    _kimi_month(replay, kimi, monkeypatch)
    assert cli.main(["route", "cap", "set", "kimi:main", "--kind", "pace_target", "--window", "monthly",
                     "--pct", "100", "--slack", "5", "--from-app"]) == 0
    assert [c["window"] for c in _kimi_entry(replay)["caps"]] == ["monthly"]
    assert cli.main(["route", "cap", "remove", "kimi:main", "--kind", "pace_target", "--window", "monthly",
                     "--from-app"]) == 0
    assert "caps" not in _kimi_entry(replay)
    values = owner_act._values("route.cap.set", {"account": "kimi:main", "kind": "pace_target",
                                                 "window": "monthly", "pct": 100, "slack": 5})
    head, positionals = owner_act.argv("route.cap.set", values)
    ns = cli.build_parser().parse_args(owner_act.full_argv(head, positionals, ["--owner-direction"]))
    assert ns.window == "monthly"
    with pytest.raises(owner_act.Refused):
        owner_act._values("route.cap.set", {"account": "kimi:main", "kind": "pace_target",
                                            "window": "fortnight", "pct": 100, "slack": 5})
