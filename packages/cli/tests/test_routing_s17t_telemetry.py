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


def runner(mac, name, telemetry=None, lanes=None, events=""):
    home, data, _second = mac
    folder = home.parent / f"runner-{name}"
    (folder / "state").mkdir(parents=True)
    (folder / "config.json").write_text(json.dumps({"lanes": lanes or {}}))
    (folder / "state" / "global.json").write_text('{"limited": {}}')
    (folder / "events.log").write_text(events)
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
    assert total["status"] == "unknown" and total["usd"] is None
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
    assert sum(b["n"] for b in merged["buckets"]) == 1 and merged["active"]["x"]["jobs"] == 1
    newer = {"spend": [], "active": [{"lane": "x", "jobs": 5, "at": 11.0}], "coverage": []}
    assert routing_sources.merge_telemetry(merged, newer)["active"]["x"]["jobs"] == 5


# ---- fix round 1: dropped, truncated and launch-only streams (P1) -----------------------


def test_truncation_keeps_the_newest_records_and_marks_the_stream_incomplete(mac):
    base = NOW - timedelta(seconds=5000)
    records = [spend(f"e{i}", 1.0, base + timedelta(seconds=i)) for i in range(4097)]
    parsed = routing_sources._parse_telemetry({"time": NOW.timestamp(), "spend": records}, [])
    assert parsed["truncated"] is True
    assert len(parsed["spend"]) == routing_sources.TELEMETRY_SPEND_MAX
    assert min(r["at"] for r in parsed["spend"]) == (base + timedelta(seconds=1)).timestamp()
    runner(mac, "a", {"spend": records, "spend_coverage": [coverage()]})
    total = week_total()
    assert total["status"] == "unknown" and total["usd"] is None


def test_a_runner_that_launched_but_emitted_no_telemetry_is_a_participating_stream(mac):
    at = NOW - timedelta(hours=1)
    runner(mac, "a", {"spend": [spend("e1", 5.0, at)], "spend_coverage": [coverage()]})
    stamp = (NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    runner(mac, "b", None, lanes={"main-lane": {"budget_account": MAIN}},
           events=f"{stamp} pipe LAUNCH lane=main-lane\n")
    total = week_total()
    assert total["status"] == "unknown" and total["usd"] is None


def test_dedup_keys_are_scoped_per_runner(mac):
    at = NOW - timedelta(hours=1)
    runner(mac, "a", {"spend": [spend("e1", 10.0, at)], "spend_coverage": [coverage()]})
    runner(mac, "b", {"spend": [spend("e1", 4.0, at)], "spend_coverage": [coverage()]})
    total = week_total()
    assert total["status"] == "known" and total["usd"] == 14.0 and total["events"] == 2


def test_dedup_survives_a_cache_reload(mac):
    at = NOW - timedelta(hours=1)
    folder = runner(mac, "a", {"spend": [spend("e1", 10.0, at)], "spend_coverage": [coverage()]})
    assert week_total()["usd"] == 10.0
    beat(folder, {"spend": [spend("e1", 10.0, at)], "spend_coverage": [coverage()]})
    assert week_total()["usd"] == 10.0


# ---- fix round 1: snapshots, corrections, bounds and event ids (P2) ---------------------


def test_an_empty_or_missing_active_snapshot_replaces_the_old(mac):
    folder = runner(mac, "a", {"active": [_active("main-lane", 3)]})
    assert routing.telemetry_active(MAIN, now=NOW) == {"jobs": 3, "status": "known"}
    beat(folder, {"active": []})
    assert routing.telemetry_active(MAIN, now=NOW) == {"jobs": None, "status": "unknown"}
    beat(folder, {"active": [_active("main-lane", 3)]})
    assert routing.telemetry_active(MAIN, now=NOW)["jobs"] == 3
    beat(folder, {"spend": [spend("e1", 1.0, NOW - timedelta(hours=1))]})
    assert routing.telemetry_active(MAIN, now=NOW) == {"jobs": None, "status": "unknown"}


def test_a_future_dated_active_at_is_bounded_to_arrival_and_cannot_wedge_the_lane(mac):
    folder = runner(mac, "a", {"active": [{"lane": "main-lane", "account": MAIN, "jobs": 9,
                                           "unit": "job", "at": (NOW + timedelta(days=3)).timestamp()}]})
    assert routing.telemetry_active(MAIN, now=NOW) == {"jobs": 9, "status": "known"}
    beat(folder, {"active": [_active("main-lane", 1)]})
    assert routing.telemetry_active(MAIN, now=NOW) == {"jobs": 1, "status": "known"}


def test_an_orphan_correction_does_not_count(mac):
    at = NOW - timedelta(hours=1)
    runner(mac, "a", {"spend": [spend("e1", 10.0, at), spend("e2", -5.0, at, corrects="missing-1")],
                      "spend_coverage": [coverage()]})
    total = week_total()
    assert total["status"] == "known" and total["usd"] == 10.0


def test_a_plain_negative_without_corrects_is_dropped_and_marks_incomplete(mac):
    at = NOW - timedelta(hours=1)
    runner(mac, "a", {"spend": [spend("e1", 10.0, at), spend("e2", -5.0, at)],
                      "spend_coverage": [coverage()]})
    total = week_total()
    assert total["status"] == "unknown" and total["usd"] is None


def test_a_correction_of_an_out_of_period_target_does_not_count(mac):
    runner(mac, "a", {"spend": [spend("e1", 30.0, NOW - timedelta(days=8)),
                                spend("e2", 1.0, NOW - timedelta(days=2)),
                                spend("e3", -0.10, NOW - timedelta(days=1), corrects="e1")],
                      "spend_coverage": [coverage(start=NOW - timedelta(days=14))]})
    total = week_total()
    assert total["status"] == "known" and total["usd"] == 1.0


def test_a_correction_of_another_accounts_target_does_not_count(mac):
    _home, _data, second = mac
    at = NOW - timedelta(hours=1)
    runner(mac, "a", {"spend": [spend("e1", 10.0, at), spend("e2", 30.0, at, account=second),
                                spend("e3", -5.0, at, corrects="e2")],
                      "spend_coverage": [coverage(), coverage(account=second)]})
    assert week_total()["usd"] == 10.0
    assert week_total(second)["usd"] == 30.0


def test_absurd_or_non_finite_usd_is_dropped_and_marks_incomplete(mac):
    at = NOW - timedelta(hours=1)
    runner(mac, "a", {"spend": [spend("e1", 10.0, at), spend("e2", 1e308, at)],
                      "spend_coverage": [coverage()]})
    total = week_total()
    assert total["status"] == "unknown" and total["usd"] is None


def test_a_key_like_event_id_is_withheld_and_a_uuid_event_id_counts(mac):
    at = NOW - timedelta(hours=1)
    runner(mac, "a", {"spend": [spend("tok-AAAA1111BBBB2222CCCC", 5.0, at)],
                      "spend_coverage": [coverage()]})
    view = routing.read_view(now=NOW)
    assert "tok-AAAA1111BBBB2222CCCC" not in json.dumps(view)
    assert week_total()["status"] == "unknown"
    _home, _data, second = mac
    runner(mac, "b", {"spend": [spend("123e4567-e89b-12d3-a456-426614174000", 2.0, at, account=second)],
                      "spend_coverage": [coverage(account=second)]})
    assert week_total(second)["usd"] == 2.0


def test_coverage_for_one_account_does_not_cover_another(mac):
    _home, _data, second = mac
    at = NOW - timedelta(hours=1)
    runner(mac, "a", {"spend": [spend("e1", 5.0, at, account=second)], "spend_coverage": [coverage()]})
    total = week_total(second)
    assert total["status"] == "unknown" and total["usd"] is None


def test_coverage_to_is_capped_at_the_heartbeat_time():
    parsed = routing_sources._parse_telemetry(
        {"time": NOW.timestamp(),
         "spend_coverage": [{"account": MAIN, "from": (NOW - timedelta(days=7)).timestamp(),
                             "to": (NOW + timedelta(days=30)).timestamp()}]}, [])
    assert parsed["coverage"][0]["to"] == NOW.timestamp()


# ---- fix round 1: unattributed, participation and persisted cost (P3) -------------------


def test_unattributed_summary_respects_period_silence_and_missing_streams(mac):
    runner(mac, "a", {"spend": [spend("e1", 7.5, NOW - timedelta(hours=1), account="Nope", provider=None),
                                spend("e2", 3.0, NOW - timedelta(days=8), account="Nope", provider=None)],
                      "spend_coverage": [coverage(account="Nope", start=NOW - timedelta(days=14))]})
    everything = routing.telemetry_spend(days=7, now=NOW)
    assert everything["unattributed"]["spend_events"] == 1
    assert everything["unattributed"]["spend_usd"] == 7.5
    assert everything["unattributed"]["active_jobs"] is None
    later = routing.telemetry_spend(days=7, now=NOW + timedelta(seconds=200))
    assert later["unattributed"]["spend_usd"] is None
    assert later["unattributed"]["active_jobs"] is None


def test_participation_starts_at_the_first_launch_or_coverage_not_the_first_event(mac):
    at = NOW - timedelta(hours=1)
    runner(mac, "a", {"spend": [spend("e1", 5.0, at)], "spend_coverage": [coverage()]})
    stamp = (NOW - timedelta(hours=36)).strftime("%Y-%m-%dT%H:%M:%SZ")
    runner(mac, "b", {"spend": [spend("e2", 5.0, at)],
                      "spend_coverage": [coverage(start=NOW - timedelta(hours=2))]},
           lanes={"main-lane": {"budget_account": MAIN}},
           events=f"{stamp} pipe LAUNCH lane=main-lane\n")
    assert week_total()["status"] == "unknown"


def test_participation_starts_at_the_first_coverage_interval(mac):
    at = NOW - timedelta(hours=1)
    runner(mac, "a", {"spend": [spend("e1", 5.0, at)], "spend_coverage": [coverage()]})
    runner(mac, "c", {"spend": [spend("e3", 5.0, at)],
                      "spend_coverage": [coverage(start=NOW - timedelta(days=3), end=NOW - timedelta(days=2)),
                                         coverage(start=NOW - timedelta(hours=2))]})
    assert week_total()["status"] == "unknown"


def test_the_4096_event_runner_persists_aggregates_not_every_event(mac):
    records = [spend(f"e{i}", 1.0, NOW - timedelta(minutes=i % 500)) for i in range(4096)]
    runner(mac, "a", {"spend": records, "spend_coverage": [coverage()],
                      "active": [_active("main-lane", 1)]})
    total = week_total()
    assert total["status"] == "known" and total["usd"] == 4096.0 and total["events"] == 4096
    view = routing.read_view(now=NOW)
    tele = view["source_cache"]["runners"]["a"]["telemetry"]
    persisted = len(tele["buckets"]) + len(tele["corrections"])
    assert persisted <= 500
    assert len(json.dumps(tele)) < 200_000
