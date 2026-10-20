"""Slice 17t, case 17e: runner telemetry (active and spend records,
attribution, dedup, coverage and expiry). Hermetic: fake runner folders
and fake figures only. Spend is money data: a missing or partial stream
reads unknown, never zero, and no figure is invented or extrapolated."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from khipu import config, routing, routing_sources, usage

NOW = datetime(2026, 10, 20, 16, 0, tzinfo=timezone.utc)
MAIN = "claude|"


@pytest.fixture
def mac(tmp_path, monkeypatch):
    home, data = tmp_path / "home", tmp_path / "data"
    home.mkdir()
    data.mkdir()
    second = home / ".claude-second"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setattr(routing, "_now", lambda: NOW)
    monkeypatch.setattr(usage, "_now", lambda: NOW)
    monkeypatch.setattr(usage, "_command", lambda h: None)
    (data / "config.json").write_text(json.dumps({"usage_accounts": [
        {"harness": "claude", "label": "Main"},
        {"harness": "claude", "label": "Second", "home": str(second)}]}))
    return home, data, f"claude|{second}"


def beat(folder, telemetry=None, at=NOW, interval=20):
    doc = {"time": at.timestamp(), "interval": interval}
    doc.update(telemetry or {})
    (folder / "state" / "heartbeat.json").write_text(json.dumps(doc))


def runner(mac, name, telemetry=None, lanes=None):
    home, data, _second = mac
    folder = home.parent / f"runner-{name}"
    (folder / "state").mkdir(parents=True)
    (folder / "config.json").write_text(json.dumps({"lanes": lanes or {}}))
    (folder / "state" / "global.json").write_text('{"limited": {}}')
    (folder / "events.log").write_text("")
    beat(folder, telemetry)
    cfg = json.loads(config.config_file().read_text())
    cfg.setdefault("routing_sources", []).append({"name": name, "kind": "lanes", "path": str(folder)})
    config.config_file().write_text(json.dumps(cfg))
    return folder


def spend(event, usd, at, account=MAIN, **fields):
    return {"event": event, "job": "job-1", "attempt": 0, "provider": "claude",
            "account": account, "usd": usd, "at": at.timestamp(), **fields}


def coverage(account=MAIN, start=None, end=None):
    return {"account": account, "provider": "claude",
            "from": (start or NOW - timedelta(days=7)).timestamp(), "to": (end or NOW).timestamp()}


def week_total(account=MAIN, **kwargs):
    return routing.telemetry_spend(account, days=7, now=NOW, **kwargs)


# ---- attribution, dedup, corrections -------------------------------------------------


def test_duplicate_spend_events_count_once_across_heartbeats(mac):
    at = NOW - timedelta(hours=1)
    folder = runner(mac, "a", {"spend": [spend("e1", 10.0, at), spend("e1", 10.0, at)],
                               "spend_coverage": [coverage()]})
    first = week_total()
    assert first["status"] == "known" and first["usd"] == 10.0 and first["events"] == 1
    beat(folder, {"spend": [spend("e1", 10.0, at)], "spend_coverage": [coverage()]})
    assert week_total()["usd"] == 10.0


def test_two_attempts_of_one_job_count_twice(mac):
    at = NOW - timedelta(hours=1)
    runner(mac, "a", {"spend": [spend("e1", 10.0, at, attempt=0), spend("e2", 10.0, at, attempt=1)],
                      "spend_coverage": [coverage()]})
    total = week_total()
    assert total["status"] == "known" and total["usd"] == 20.0 and total["events"] == 2


def test_a_correction_nets_out(mac):
    at = NOW - timedelta(hours=1)
    runner(mac, "a", {"spend": [spend("e1", 35.10, at), spend("e2", -0.10, at, corrects="e1")],
                      "spend_coverage": [coverage()]})
    total = week_total()
    assert total["status"] == "known" and total["usd"] == 35.0


def test_two_accounts_on_one_provider_never_pool(mac):
    _home, _data, second = mac
    at = NOW - timedelta(hours=1)
    runner(mac, "a", {"spend": [spend("e1", 10.0, at), spend("e2", 5.0, at, account=second)],
                      "spend_coverage": [coverage(), coverage(account=second)]})
    assert week_total()["usd"] == 10.0
    assert week_total(second)["usd"] == 5.0
    assert set(routing.telemetry_spend(days=7, now=NOW)["accounts"]) == {MAIN, second}


def test_a_lanes_spend_with_no_resolvable_account_is_unattributed(mac):
    at = NOW - timedelta(hours=1)
    runner(mac, "a", {"spend": [spend("e1", 7.5, at, account="Nope", provider=None)],
                      "spend_coverage": [coverage(account="Nope")]})
    everything = routing.telemetry_spend(days=7, now=NOW)
    assert everything["unattributed"]["spend_events"] == 1
    assert everything["unattributed"]["spend_usd"] == 7.5
    total = week_total()
    assert total["status"] == "unknown" and total["usd"] is None


# ---- coverage honesty ------------------------------------------------------------------


def test_one_runners_gap_makes_the_total_unknown_even_when_another_covers(mac):
    at = NOW - timedelta(days=3)
    runner(mac, "a", {"spend": [spend("e1", 10.0, at)], "spend_coverage": [coverage()]})
    runner(mac, "b", {"spend": [spend("e2", 4.0, at)],
                      "spend_coverage": [coverage(start=NOW - timedelta(days=1))]})
    total = week_total()
    assert total["status"] == "unknown" and total["usd"] is None
    assert "gap" in total["reason"]


def test_a_missing_stream_reads_unknown_never_zero(mac):
    total = week_total()
    assert total["status"] == "unknown" and total["usd"] is None


def test_a_silent_runners_spend_expires_to_unknown(mac):
    at = NOW - timedelta(hours=1)
    runner(mac, "a", {"spend": [spend("e1", 10.0, at)], "spend_coverage": [coverage(end=NOW + timedelta(hours=1))]})
    assert week_total()["status"] == "known"
    later = routing.telemetry_spend(MAIN, days=7, now=NOW + timedelta(seconds=200))
    assert later["status"] == "unknown" and later["usd"] is None


# ---- active snapshots --------------------------------------------------------------------


def test_a_new_active_snapshot_replaces_the_old(mac):
    folder = runner(mac, "a", {"active": [{"lane": "main-lane", "account": MAIN, "jobs": 2,
                                           "unit": "job", "at": NOW.timestamp()}]})
    assert routing.telemetry_active(MAIN, now=NOW) == {"jobs": 2, "status": "known"}
    beat(folder, {"active": [{"lane": "main-lane", "account": MAIN, "jobs": 1,
                              "unit": "job", "at": NOW.timestamp()}]})
    assert routing.telemetry_active(MAIN, now=NOW) == {"jobs": 1, "status": "known"}


def _active(lane, jobs):
    return {"lane": lane, "account": MAIN, "jobs": jobs, "unit": "job", "at": NOW.timestamp()}


def test_two_runners_active_jobs_sum(mac):
    active = _active
    runner(mac, "a", {"active": [active("lane-a", 1)]})
    runner(mac, "b", {"active": [active("lane-b", 2)]})
    assert routing.telemetry_active(MAIN, now=NOW) == {"jobs": 3, "status": "known"}
    view = routing.read_view(now=NOW)
    assert view["sources"]["telemetry"]["accounts"][MAIN] == {"active_jobs": 3, "active_status": "known"}


def test_a_silent_runners_active_records_expire_to_unknown(mac):
    runner(mac, "a", {"active": [{"lane": "main-lane", "account": MAIN, "jobs": 2,
                                  "unit": "job", "at": NOW.timestamp()}]})
    assert routing.telemetry_active(MAIN, now=NOW)["jobs"] == 2
    expired = routing.telemetry_active(MAIN, now=NOW + timedelta(seconds=200))
    assert expired == {"jobs": None, "status": "unknown"}


# ---- bounded parser: malformed records and secrets never survive -------------------------


def test_malformed_telemetry_records_are_dropped_not_repaired(mac):
    at = NOW - timedelta(hours=1)
    runner(mac, "a", {
        "spend": [spend("e1", 10.0, at), spend("e2", "35", at), spend("bad event", 1.0, at),
                  spend("e3", 1.0, at)],
        "spend_coverage": [coverage(), {"account": MAIN, "from": NOW.timestamp(), "to": at.timestamp()}],
        "active": [{"lane": "main-lane", "account": MAIN, "jobs": -1, "unit": "job", "at": NOW.timestamp()},
                   {"lane": "main-lane", "account": MAIN, "jobs": 1, "unit": "request", "at": NOW.timestamp()},
                   {"lane": "main-lane", "account": MAIN, "jobs": 4, "unit": "job", "at": NOW.timestamp()}]})
    total = week_total()
    assert total["status"] == "known" and total["usd"] == 11.0
    assert routing.telemetry_active(MAIN, now=NOW) == {"jobs": 4, "status": "known"}


def test_telemetry_never_keeps_a_path_or_a_free_text_sentinel(mac):
    sentinel = "SENTINEL-TELEMETRY-SECRET"
    at = NOW - timedelta(hours=1)
    path_account = f"claude|{mac[0]}/private-login"
    runner(mac, "a", {"spend": [spend("e1", 10.0, at, account=path_account)], "note": sentinel,
                      "spend_coverage": [coverage(account=path_account)]})
    view = routing.read_view(now=NOW)
    shown = json.dumps(view) + json.dumps(routing.conversation_view(now=NOW))
    assert sentinel not in shown and "private-login" not in shown
    everything = routing.telemetry_spend(days=7, now=NOW)
    assert everything["unattributed"]["spend_usd"] == 10.0


def test_merge_telemetry_dedups_and_keeps_the_newest_snapshot():
    parsed = {"spend": [{"event": "e1", "usd": 3.0, "at": 10.0}],
              "active": [{"lane": "x", "jobs": 1, "at": 10.0}], "coverage": [], "dropped": 0}
    merged = routing_sources.merge_telemetry(routing_sources.merge_telemetry(None, parsed), parsed)
    assert list(merged["spend"]) == ["e1"] and merged["active"]["x"]["jobs"] == 1
    newer = {"spend": [], "active": [{"lane": "x", "jobs": 5, "at": 9.0}], "coverage": []}
    assert routing_sources.merge_telemetry(merged, newer)["active"]["x"]["jobs"] == 1
