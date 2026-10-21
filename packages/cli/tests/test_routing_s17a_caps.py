"""Slice 17a, case 17b cap parts: pace_target, spend and concurrency caps.

Facts, owner confirm, calculations. A conversation proposes only; a runner
number is data, never a permission. Hermetic: fake samples and fake runner
figures only."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from khipu import config, routing, routing_pace, usage
from tests.test_owner_direction_e2e import world  # noqa: F401  (pytest fixture)

T0 = datetime(2026, 10, 20, 14, tzinfo=timezone.utc)
CONV = {"kind": "conversation", "harness": "codex", "project": "p", "session": "s-other"}


def samp(t, used, *, reset, mac="m1"):
    return {"t": t, "used": float(used), "reset": reset, "mac": mac, "source": "probe",
            "cycle": None, "t_err": 0}


# ---- checked_cap: the three new kinds -------------------------------------


def test_checked_cap_accepts_the_new_kinds():
    assert routing_pace.checked_cap({"kind": "pace_target", "pct": 65, "slack": 5, "revision": "r1"}) == {
        "kind": "pace_target", "pct": 65.0, "slack": 5.0, "revision": "r1"}
    assert routing_pace.checked_cap({"kind": "spend", "usd": 35, "days": 7, "revision": "r1"}) == {
        "kind": "spend", "usd": 35.0, "days": 7.0, "revision": "r1"}
    assert routing_pace.checked_cap({"kind": "concurrency", "n": 1, "revision": "r1"}) == {
        "kind": "concurrency", "n": 1, "revision": "r1"}


@pytest.mark.parametrize("bad", [
    {"kind": "pace_target", "pct": 65, "revision": "r"},          # no slack
    {"kind": "pace_target", "pct": 0, "slack": 5, "revision": "r"},
    {"kind": "pace_target", "pct": 65, "slack": -1, "revision": "r"},
    {"kind": "spend", "usd": 0, "days": 7, "revision": "r"},
    {"kind": "spend", "usd": 35, "days": 0, "revision": "r"},
    {"kind": "spend", "usd": "35", "days": 7, "revision": "r"},
    {"kind": "concurrency", "n": -1, "revision": "r"},
    {"kind": "concurrency", "n": 1.5, "revision": "r"},
    {"kind": "concurrency", "n": True, "revision": "r"},
])
def test_checked_cap_rejects_bad_new_caps(bad):
    assert routing_pace.checked_cap(bad) is None


# ---- pace_target calculation ----------------------------------------------
# 65% target, 5 slack, 11 h into a 168 h week: allowance 65*11/168+5 = 9.26%.


def _week_pace(used, *, at_h=11):
    now = T0 + timedelta(hours=at_h)
    reset = T0 + timedelta(hours=168)
    cap = {"kind": "pace_target", "pct": 65.0, "slack": 5.0, "revision": "r1"}
    return routing_pace.compute_pace([samp(now, used, reset=reset)], [cap], now, "weekly")


def test_pace_target_ahead_at_18_used_11h_in():
    target = _week_pace(18)["pace_target"]
    assert target["allowance_pct"] == pytest.approx(9.26, abs=0.01)
    assert target["used_pct"] == 18
    assert target["verdict"] == "ahead"


def test_pace_target_within_and_recovering_band():
    assert _week_pace(5)["pace_target"]["verdict"] == "within"
    # Inside the 1-point recovery band: no longer ahead, not yet recovered.
    assert _week_pace(8.8)["pace_target"]["verdict"] == "recovering"
    assert _week_pace(8.0)["pace_target"]["verdict"] == "within"


def test_pace_target_unknown_without_a_reading():
    cap = {"kind": "pace_target", "pct": 65.0, "slack": 5.0, "revision": "r1"}
    pace = routing_pace.compute_pace([], [cap], T0, "weekly")
    assert pace["pace_target"]["verdict"] == "unknown"


def test_pace_target_line_names_ahead_within_160_chars():
    pace = _week_pace(18)
    line = routing_pace.pace_line("Claude", pace, T0 + timedelta(hours=11))
    assert "ahead" in line and len(line) <= 160


# ---- owner confirm vs conversation proposal (world fixture) ----------------


def _caps(label="Claude"):
    view = routing.read_view()
    account = next(a for a in view["accounts"] if a["label"] == label)
    return account, view


def test_owner_set_pace_target_cap_is_a_fact_and_reaches_views(world):  # noqa: F811
    from tests.test_owner_direction_e2e import _act, _hook

    routing.report(account="Claude", state="open", author=CONV)
    _hook("keep Claude at an even pace to 65 percent with 5 slack")
    err, out = _act("route.cap.set", {"account": "Claude", "window": "weekly", "kind": "pace_target",
                                      "pct": 65, "slack": 5, "revision": "pt1"})
    assert not err, out
    account, _view = _caps()
    caps = [c for c in account.get("caps") or [] if c["kind"] == "pace_target"]
    assert caps and caps[0]["pct"] == 65 and caps[0]["slack"] == 5
    conv = routing.conversation_view()
    shown = next(a for a in conv["accounts"] if a["label"] == "Claude")
    assert any(c["kind"] == "pace_target" for c in shown.get("caps") or [])


def test_conversation_pace_target_cap_is_only_a_proposal(world):  # noqa: F811
    routing.report(account="Claude", state="open", author=CONV)
    out = routing.request_cap("Claude", window="weekly", kind="pace_target", pct=65, slack=5,
                              revision="conv1", author={"kind": "owner"})
    assert out.get("pending") is True
    account, view = _caps()
    assert not [c for c in account.get("caps") or [] if c["kind"] == "pace_target"]
    proposals = [p for p in view.get("cap_proposals") or [] if p.get("revision") == "conv1"]
    assert proposals and proposals[0]["written_by"]["kind"] == "conversation"
    assert proposals[0]["kind"] == "pace_target" and proposals[0]["slack"] == 5


def test_owner_remove_new_cap_kinds(world):  # noqa: F811
    routing.report(account="Claude", state="open", author=CONV)
    routing.set_cap("Claude", kind="concurrency", n=1, revision="c1", via="app")
    routing.set_cap("Claude", kind="spend", usd=35, days=7, revision="s1", via="app")
    account, _ = _caps()
    kinds = {c["kind"] for c in account.get("caps") or []}
    assert {"concurrency", "spend"} <= kinds
    routing.remove_cap("Claude", kind="concurrency", via="app")
    account, _ = _caps()
    assert not [c for c in account.get("caps") or [] if c["kind"] == "concurrency"]


def test_set_cap_rejects_a_spend_cap_without_usd(world):  # noqa: F811
    routing.report(account="Claude", state="open", author=CONV)
    with pytest.raises(routing.RouteError):
        routing.set_cap("Claude", kind="spend", days=7, via="app")


# ---- spend and concurrency verdicts from runner telemetry (17t) ------------

NOW = datetime(2026, 10, 20, 16, 0, tzinfo=timezone.utc)
MAIN = "claude|"


@pytest.fixture
def mac(tmp_path, monkeypatch):
    home, data = tmp_path / "home", tmp_path / "data"
    home.mkdir()
    data.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setattr(routing, "_now", lambda: NOW)
    monkeypatch.setattr(usage, "_now", lambda: NOW)
    monkeypatch.setattr(usage, "_command", lambda h: None)
    (data / "config.json").write_text(json.dumps({"usage_accounts": [{"harness": "claude", "label": "Main"}]}))
    return home, data


def _runner(mac, telemetry):
    home, data = mac
    folder = home.parent / "runner-a"
    (folder / "state").mkdir(parents=True)
    (folder / "config.json").write_text(json.dumps({"lanes": {}}))
    (folder / "state" / "global.json").write_text('{"limited": {}}')
    (folder / "events.log").write_text("")
    (folder / "state" / "heartbeat.json").write_text(
        json.dumps({"time": (NOW - timedelta(days=8)).timestamp(), "interval": 20}))
    cfg = json.loads(config.config_file().read_text())
    cfg.setdefault("routing_sources", []).append({"name": "a", "kind": "lanes", "path": str(folder)})
    config.config_file().write_text(json.dumps(cfg))
    routing.read_view(now=NOW - timedelta(days=8))
    (folder / "state" / "heartbeat.json").write_text(
        json.dumps({"time": NOW.timestamp(), "interval": 20, **telemetry}))
    return folder


def _spend(event, usd, at):
    return {"event": event, "job": "job-1", "attempt": 0, "provider": "claude",
            "account": MAIN, "usd": usd, "at": at.timestamp()}


def _coverage():
    return {"account": MAIN, "provider": "claude",
            "from": (NOW - timedelta(days=7)).timestamp(), "to": NOW.timestamp()}


def _statuses():
    view = routing.read_view(now=NOW)
    account = next(a for a in view["accounts"] if a["key"] == MAIN)
    return {s["kind"]: s for s in routing.cap_statuses(view, account, NOW)}


def test_spend_cap_over_at_35_10_against_35(mac):
    _runner(mac, {"spend": [_spend("e1", 35.10, NOW - timedelta(hours=1))],
                  "spend_coverage": [_coverage()]})
    routing.set_cap("Main", kind="spend", usd=35, days=7, revision="s1", via="app")
    status = _statuses()["spend"]
    assert status["verdict"] == "over" and status["usd"] == pytest.approx(35.10)
    assert status["cap_usd"] == 35


def test_spend_cap_unknown_when_the_period_is_not_covered(mac):
    _runner(mac, {"spend": [_spend("e1", 3.0, NOW - timedelta(hours=1))]})
    routing.set_cap("Main", kind="spend", usd=35, days=7, revision="s1", via="app")
    status = _statuses()["spend"]
    assert status["verdict"] == "unknown" and status["usd"] is None


def test_concurrency_cap_full_at_one_job(mac):
    _runner(mac, {"active": [{"lane": "main-lane", "account": MAIN, "jobs": 1,
                              "unit": "job", "at": NOW.timestamp()}]})
    routing.set_cap("Main", kind="concurrency", n=1, revision="c1", via="app")
    status = _statuses()["concurrency"]
    assert status["verdict"] == "full" and status["jobs"] == 1 and status["cap_n"] == 1


def test_concurrency_cap_unknown_when_telemetry_expired(mac):
    _runner(mac, {"active": [{"lane": "main-lane", "account": MAIN, "jobs": 1,
                              "unit": "job", "at": NOW.timestamp()}]})
    routing.set_cap("Main", kind="concurrency", n=1, revision="c1", via="app")
    view = routing.read_view(now=NOW + timedelta(seconds=200))
    account = next(a for a in view["accounts"] if a["key"] == MAIN)
    statuses = {s["kind"]: s for s in routing.cap_statuses(view, account, NOW + timedelta(seconds=200))}
    assert statuses["concurrency"]["verdict"] == "unknown"
    assert statuses["concurrency"]["jobs"] is None
