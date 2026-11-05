"""Both Macs read one allowance from one history: pace readings cross, compactly.

Two disposable Macs and an in-memory hub, never a credential or network read."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from khipu import routing, routing_advice, routing_pace, routing_replication as replication
from tests import test_routing_s11b_replication as _s11b
from tests.test_routing_acceptance import T0

pair = _s11b.pair
sync = _s11b.sync
RESET = T0 + timedelta(days=3)


def sample(at, used, *, account="codex|", window="weekly", reset=RESET, **extra):
    return {"type": "usage_sample", "account": account, "window": window, "witness_at": routing._iso(at),
            "used_pct": used, "t_err": 60, "source": "probe", "resets_at": routing._iso(reset),
            "written_by": {"kind": "probe"}, "via": "probe", **extra}


def day_of_use(hours=30, step_min=30, *, offset_min=0, rate=0.5):
    start = T0 - timedelta(hours=hours)
    return [sample(start + timedelta(minutes=i * step_min + offset_min), 20 + rate * i * step_min / 60)
            for i in range(hours * 60 // step_min)]


def weekly(view, key="codex|"):
    entry = next(a for a in view["accounts"] if a["key"] == key)
    return next((p for p in entry.get("pace") or [] if p["window"] == "weekly"), None), entry


def settle(*macs_and_hub):
    *macs, hub = macs_and_hub
    for mac in (*macs, *macs):
        assert sync(mac, hub)["ok"]


def test_two_macs_with_one_history_give_equal_weekly_advice(pair):
    a, b, hub = pair
    a.activate()
    a.write(*day_of_use())
    routing.set_cap("Codex", window="weekly", kind="share", pct=95, via="app")
    routing.set_cap("Codex", window="weekly", kind="pace_target", pct=100, slack=5, via="app")
    settle(a, b, hub)
    a.activate()
    here, here_entry = weekly(a.view())
    here_advice = routing_advice.account_advice(a.view(), here_entry, a.now)
    b.activate()
    there, there_entry = weekly(b.view())
    assert there is not None, "the other Mac has no pace at all: the readings did not cross"
    assert there["why"] is None and here["why"] is None
    for field in ("cycle_resets_at", "used_pct", "headline"):
        assert there[field] == here[field]
    assert there["rate_pct_per_h"] == pytest.approx(here["rate_pct_per_h"])
    assert there["plan"]["verdict"] == here["plan"]["verdict"]
    assert there["share_cap"]["verdict"] == here["share_cap"]["verdict"]
    assert there["pace_target"]["verdict"] == here["pace_target"]["verdict"]
    there_advice = routing_advice.account_advice(b.view(), there_entry, b.now)
    assert (there_advice["verdict"], there_advice["evidence"]) == (here_advice["verdict"], here_advice["evidence"])
    # And the line a conversation reads about the shared allowance.
    assert routing_pace.pace_line("Codex", there, b.now) == routing_pace.pace_line("Codex", here, a.now)


def test_both_macs_reading_one_allowance_pool_to_the_same_pace(pair):
    a, b, hub = pair
    a.activate()
    a.write(*day_of_use())
    b.activate()
    b.write(*day_of_use(offset_min=10))
    settle(a, b, hub)
    a.activate()
    here = weekly(a.view())[0]
    b.activate()
    there = weekly(b.view())[0]
    assert here["why"] is None and there["why"] is None
    assert here["plan"]["verdict"] == there["plan"]["verdict"]
    assert here["rate_pct_per_h"] == pytest.approx(there["rate_pct_per_h"], abs=0.02)


@pytest.mark.parametrize("window", ["monthly", "weekly_fable", "120_min", "daily", "weekly", "five_hour"])
def test_a_cap_on_any_window_a_cap_can_name_crosses(pair, window):
    a, b, hub = pair
    a.activate()
    routing.set_cap("Codex", window=window, kind="share", pct=50, via="app")
    settle(a, b, hub)
    b.activate()
    caps = weekly(b.view())[1].get("caps") or []
    assert [(c["kind"], c["window"], c["pct"]) for c in caps] == [("share", window, 50)]
    a.activate()
    routing.remove_cap("Codex", window=window, kind="share", via="app")
    settle(a, b, hub)
    b.activate()
    assert not weekly(b.view())[1].get("caps")


def test_a_window_nothing_can_read_still_stays_home():
    bindings = {"codex|": "codex:main"}
    for bad in ("/Users/someone/secret", "weekly_/etc/passwd", "bogus"):
        rows = routing.replication_payload([{"type": "cap_set", "id": "e1", "account": "codex|", "kind": "share",
                                             "window": bad, "pct": 1.0, "revision": "cap-1"}], bindings=bindings)
        assert "window" not in rows[0]


def test_grok_reserve_and_every_ceiling_survive_the_new_readings(pair):
    a, b, hub = pair
    a.activate()
    routing.set_cap("Grok", window="weekly", kind="share", pct=90, via="app")
    routing.set_cap("Codex", window="weekly", kind="share", pct=95, via="app")
    routing.set_cap("Codex", window="monthly", kind="share", pct=70, via="app")
    a.write(*day_of_use(), *[dict(s, account="grok|") for s in day_of_use()])
    settle(a, b, hub)
    b.activate()
    view = b.view()
    assert {(c["window"], c["pct"]) for c in weekly(view)[1]["caps"]} == {("weekly", 95), ("monthly", 70)}
    pace, grok = weekly(view, "grok|")
    assert [(c["window"], c["pct"]) for c in grok["caps"]] == [("weekly", 90)]
    assert pace["share_cap"]["pct"] == 90 and pace["share_cap"]["verdict"] != "unknown"


def test_a_login_that_reads_another_allowance_does_not_poison_this_macs_pace(pair):
    """The real shape on 2026-11-05: one Mac's login is bound to an id whose
    allowance another Mac reads with a different reset. Pooled, both would read
    "reset time disagrees between readings"."""
    a, b, hub = pair
    a.activate()
    a.write(*day_of_use())
    before = weekly(a.view())[0]
    b.activate()
    b.write(*[dict(s, resets_at=routing._iso(RESET + timedelta(days=2)), used_pct=s["used_pct"] + 23)
              for s in day_of_use()])
    settle(a, b, hub)
    a.activate()
    after = weekly(a.view())[0]
    assert after["why"] is None and after["plan"]["verdict"] == before["plan"]["verdict"]
    assert after["used_pct"] == before["used_pct"] and after["cycle_resets_at"] == before["cycle_resets_at"]


def test_with_no_reading_of_its_own_a_mac_shows_what_the_others_disagree_about():
    now = T0
    mine, one, two = "m" * 32, "a" * 32, "b" * 32
    rows = [{"t": now - timedelta(minutes=5), "used": 1.0, "reset": RESET, "mac": one},
            {"t": now - timedelta(minutes=5), "used": 2.0, "reset": RESET + timedelta(days=2), "mac": two}]
    assert routing_pace.shared_readings(rows, mine, now) == rows


def wire(readings, now):
    tagged = [dict(s, _id=i, window="weekly", mac="m", source="probe") for i, s in enumerate(readings)]
    return routing_pace.wire_choice(tagged, now), tagged


def burst_history(now=T0):
    """Real use: a reading at each poll while usage moves, and flat stretches."""
    rows, used, at = [], 10.0, now - timedelta(hours=60)
    while at < now:
        busy = (at.hour % 6) < 3
        used += 0.1 if busy else 0.0
        rows.append({"t": at, "used": used, "reset": RESET, "t_err": 60, "cycle": None})
        at += timedelta(minutes=2 if busy else 60)
    return rows


def pace_of(rows, now):
    return routing_pace.compute_pace([dict(r, mac="m", source="probe") for r in rows], [], now, "weekly")


def test_what_crosses_gives_the_pace_all_the_readings_give():
    """At each quarter hour, the same pace from what crossed as from every reading."""
    rows = burst_history()
    total = len([r for r in rows if r["t"] > T0 - timedelta(days=2)])
    (send, hold), _tagged = wire(rows, T0)
    assert not hold and 0 < len(send) < total * 0.5, f"{len(send)} of {total} sent: that is no thinning"
    for ticks in range(0, 48):
        now = T0 - timedelta(minutes=15 * ticks)
        upto = [r for r in rows if r["t"] < now]
        (send, hold), _tagged = wire(upto, now)
        assert not hold
        thin = [r for i, r in enumerate(upto) if i in send]
        full, kept = pace_of(upto, now), pace_of(thin, now)
        for field in ("why", "used_pct", "cycle_resets_at"):
            assert full[field] == kept[field], (now, field)
        assert full["plan"]["verdict"] == kept["plan"]["verdict"], now
        assert kept["rate_pct_per_h"] == pytest.approx(full["rate_pct_per_h"], abs=0.02), now


def test_a_bucket_with_a_possible_reset_goes_whole_and_an_open_one_waits():
    now = T0
    falling = [{"t": now - timedelta(hours=1, minutes=m), "used": u, "reset": RESET} for m, u in ((8, 40.0), (5, 3.0), (2, 4.0))]
    (send, hold), _tagged = wire(falling, now)
    assert send == {0, 1, 2} and not hold
    moved = [{"t": now - timedelta(hours=1, minutes=8), "used": 4.0, "reset": RESET},
             {"t": now - timedelta(hours=1, minutes=2), "used": 5.0, "reset": RESET + timedelta(days=7)}]
    assert wire(moved, now)[0][0] == {0, 1}
    # Past the last quarter hour: the bucket has not closed by a tick yet.
    late = now + timedelta(minutes=7)
    (send, hold), _tagged = wire([{"t": now + timedelta(minutes=1), "used": 1.0, "reset": RESET}], late)
    assert not send and hold == {0}
    ancient = [{"t": now - timedelta(days=3), "used": 1.0, "reset": RESET}]
    assert wire(ancient, now)[0] == (set(), set())


def _quarter_hour_reset(now=T0):
    """Usage fell to 2 and rose, all under one named reset, 3 of the readings in
    a later bucket than the fall from 50."""
    return [(now - timedelta(minutes=m), used) for m, used in ((80, 50.0), (50, 50.0), (20, 50.0), (14, 2.0), (12, 3.0), (10, 4.0))]


def test_a_reset_confirmed_by_three_readings_stays_confirmed_across_buckets():
    rows = [{"t": at, "used": used, "reset": RESET} for at, used in _quarter_hour_reset()]
    (send, hold), _tagged = wire(rows, T0)
    assert not hold and {3, 4, 5} <= send, f"sent {sorted(send)}"
    full = pace_of(rows, T0)
    kept = pace_of([r for i, r in enumerate(rows) if i in send], T0)
    assert not str(full["why"]).startswith("possible reset")
    assert full["why"] == kept["why"] and full["used_pct"] == kept["used_pct"] == 4.0


def test_both_macs_agree_on_a_reset_confirmed_at_a_quarter_hour(pair):
    a, b, hub = pair
    a.activate()
    assert a.now.timestamp() % 900 == 0
    a.write(*[sample(at, used) for at, used in _quarter_hour_reset(a.now)])
    settle(a, b, hub)
    a.activate()
    here = weekly(a.view())[0]
    b.activate()
    there = weekly(b.view())[0]
    assert not str(there["why"]).startswith("possible reset"), there["why"]
    assert (there["why"], there["used_pct"], there["plan"]["verdict"]) == (here["why"], here["used_pct"], here["plan"]["verdict"])
    assert len([r for r in hub.calls if r["type"] == "usage_sample"]) >= 4


@pytest.mark.parametrize("values,kept", [
    # A dip that comes back is a correction, read from every reading, rebound included.
    ((50, 49, 48, 50, 51, 52), {0, 1, 2, 3, 5}),
    # Far below for three readings: a new cycle, and rising after that thins again.
    ((50, 2, 3, 4, 5, 6, 7), {0, 1, 2, 3, 6}),
    # Far below twice and then back: never settled.
    ((50, 2, 3, 50, 51), {0, 1, 2, 3, 4}),
    # Just rising: only the newest of the bucket goes.
    ((50, 51, 52, 53), {3})])
def test_what_a_fall_needs_crosses_and_nothing_more(values, kept):
    start = T0 - timedelta(hours=1)
    rows = [{"t": start + timedelta(minutes=i), "used": float(v), "reset": RESET} for i, v in enumerate(values)]
    assert wire(rows, T0)[0][0] == kept
    full, thin = pace_of(rows, T0), pace_of([r for i, r in enumerate(rows) if i in kept], T0)
    assert full["why"] == thin["why"] and full["used_pct"] == thin["used_pct"] and full["cycle_resets_at"] == thin["cycle_resets_at"]


def _rewrites(monkeypatch):
    done = []
    real = replication._atomic

    def counted(path, data):
        if path.name == "remote.jsonl":
            done.append(path)
        return real(path, data)
    monkeypatch.setattr(replication, "_atomic", counted)
    return done


def test_a_day_of_readings_costs_one_rewrite_per_pull_not_one_per_row(pair, monkeypatch):
    a, b, hub = pair
    a.activate()
    a.write(*day_of_use(hours=24, step_min=10))
    rewrites = _rewrites(monkeypatch)
    assert sync(a, hub)["ok"]
    assert sync(a, hub)["ok"]
    sent = [r for r in hub.calls if r["type"] == "usage_sample"]
    assert 64 <= len(sent) <= 144
    assert rewrites == [], "this Mac's own readings rewrote its copy of what other Macs sent"
    assert not [e for e in replication.remote_events() if e["type"] == "usage_sample"]
    uploads = len(sent)
    b.activate()
    for _ in range(3):
        assert sync(b, hub)["ok"]
    assert len(rewrites) == 1
    received = [e for e in replication.remote_events() if e["type"] == "usage_sample"]
    assert len(received) == uploads and all(e["account"] == "codex:main" for e in received)
    assert all("op_body" not in e and e.get("written_by", {}).get("kind") != "probe" for e in received)


def test_a_held_reading_goes_when_its_bucket_closes_and_is_not_called_unsupported(pair):
    a, b, hub = pair
    a.activate()
    a.now = T0 + timedelta(minutes=7)
    a.write(sample(a.now - timedelta(minutes=1), 21))
    hub.now = a.now
    out = sync(a, hub)
    assert out["ok"] and out["deferred_kinds"] == [] and replication.status()["deferred_kinds"] == []
    assert not [r for r in hub.calls if r["type"] == "usage_sample"]
    a.now += timedelta(minutes=30)
    hub.now = a.now
    assert sync(a, hub)["ok"]
    assert [r["used_pct"] for r in hub.calls if r["type"] == "usage_sample"] == [21]
    assert json.loads((routing.routing_dir() / "replication.json").read_text()).get("deferred", {}) == {}


def test_readings_an_older_khipu_left_deferred_cross_in_bounded_turns_and_clear(pair):
    a, b, hub = pair
    a.activate()
    a.write(*day_of_use(hours=24, step_min=5))
    ids = [e["id"] for e in routing._read_event_lines()[0] if e["type"] == "usage_sample"]
    assert sync(a, hub)["ok"]
    hub.calls.clear()
    hub.rows.clear()
    state = json.loads((routing.routing_dir() / "replication.json").read_text())
    state["deferred"] = {i: "usage_sample" for i in ids}
    (routing.routing_dir() / "replication.json").write_text(json.dumps(state))
    assert sync(a, hub)["ok"]
    assert len([r for r in hub.calls if r["type"] == "usage_sample"]) == replication.UPLOAD_PER_TURN
    for _ in range(4):
        assert sync(a, hub)["ok"]
    assert json.loads((routing.routing_dir() / "replication.json").read_text()).get("deferred", {}) == {}
    assert 64 < len([r for r in hub.calls if r["type"] == "usage_sample"]) <= 96
