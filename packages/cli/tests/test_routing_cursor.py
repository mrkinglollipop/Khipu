"""Cursor transport: shared xAI payer, legacy account removal and model evidence."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from khipu import config, discover, routing, routing_sources, usage

NOW = datetime(2026, 10, 20, 16, 0, tzinfo=timezone.utc)
EMAIL = "cursor-identity-sentinel@example.invalid"


@pytest.fixture
def mac(tmp_path, monkeypatch):
    home, data = tmp_path / "home", tmp_path / "data"
    home.mkdir()
    data.mkdir()
    (home / ".cursor").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setattr(routing, "_now", lambda: NOW)
    monkeypatch.setattr(usage, "_now", lambda: NOW)
    monkeypatch.setattr(usage, "_command", lambda h: None)
    (data / "config.json").write_text(json.dumps({"usage_accounts": [
        {"harness": "cursor", "label": "Cursor"}, {"harness": "grok", "label": "Grok"}]}))
    return home, data


def test_cursor_is_not_a_trackable_or_discovered_account(mac, monkeypatch):
    calls = []
    def command(h):
        calls.append(h)
        return "/fake/cursor-agent" if h == "cursor" else None
    monkeypatch.setattr(usage, "_command", command)
    assert [a["harness"] for a in usage.configured_accounts()] == ["grok"]
    assert [a["harness"] for a in usage.accounts_status()["accounts"]] == ["grok"]
    assert [a["harness"] for a in routing.accounts()] == ["grok"]
    assert "cursor" not in usage.HARNESSES and "cursor" not in discover.CLIS
    assert "cursor" not in discover.cli_states()
    assert not [r for r in discover._gather(NOW.timestamp())[0].values() if r["cli"] == "cursor"]
    discover._save_store({"version": discover.STORE_VERSION, "candidates": {
        "cursor:default": {"id": "cursor:default", "cli": "cursor", "identity": "default", "sources": ["installed"]}}})
    assert discover._view(discover._load_store())["candidates"] == []
    assert "cursor" not in calls
    assert not hasattr(usage, "probe_cursor") and not hasattr(usage, "parse_cursor")
    assert not discover.argv_allowed("cursor", ("/fake/cursor-agent", "about", "--format", "json"))
    with pytest.raises(usage.AccountsError) as exc:
        usage.parse_usage_accounts([{"harness": "cursor", "label": "Cursor"}])
    assert exc.value.code == "unknown_cli"
    with pytest.raises(routing.RouteError):
        routing.report(account="Cursor", state="limited")
    before = config.config_file().read_bytes()
    doctor = usage.doctor_block()
    assert [a["harness"] for a in doctor["accounts"]] == ["grok"]
    assert doctor["warnings"] == ["Ignored a tracked Cursor entry: Cursor is a transport on the xAI account; its usage and limits count against Grok"]
    assert config.config_file().read_bytes() == before


@pytest.mark.parametrize("provider", usage.HARNESSES)
def test_each_provider_matches_only_a_unique_account(provider):
    known = [{"key": provider + "|", "label": "Account", "harness": provider, "home": None}]
    for field in ("kind", "provider"):
        match = routing.resolve_lane("runner", "arbitrary-tier", {}, known, {field: provider})
        assert match["key"] == provider + "|" and match["matched_by"] == "provider"
        second = {"key": provider + "|/second", "label": "Second", "harness": provider, "home": "/second"}
        match = routing.resolve_lane("runner", "arbitrary-tier", {}, [*known, second], {field: provider})
        assert match["key"] is None and len(match["candidates"]) == 2


def test_declaration_conflicts_overrides_and_tier_keys(mac):
    known = routing.accounts() + [{"key": "claude|", "label": "Claude", "harness": "claude", "home": None},
                                  {"key": "claude|/second", "label": "Second", "harness": "claude", "home": "/second"}]
    assert routing.resolve_lane("r", "main-sonnet", {}, known,
        {"kind": "claude", "budget_account": "claude|"})["key"] == "claude|"
    for spec in ({"kind": "cursor", "provider": "claude"},
                 {"kind": "claude", "budget_account": "grok|"},
                 {"account": "claude|", "budget_account": "grok|"},
                 {"kind": "cursor", "budget_account": "missing|"}):
        assert routing.resolve_lane("r", "x", {}, known, spec)["key"] is None
    for spec in ({"kind": "cursor", "provider": "grok"},
                 {"kind": "cursor", "budget_account": "grok|"},
                 {"kind": "cursor", "provider": "grok", "budget_account": "grok|"}):
        assert routing.resolve_lane("r", "x", {}, known, spec)["key"] == "grok|"
    assert routing.resolve_lane("r", "x", {"r": {"x": "grok|"}}, known,
                                {"kind": "cursor"})["key"] == "grok|"


def runner(mac, lanes, events):
    folder = mac[1].parent / "runner"
    (folder / "state").mkdir(parents=True)
    (folder / "config.json").write_text(json.dumps({"lanes": lanes}))
    (folder / "state/global.json").write_text('{"limited": {}}')
    (folder / "state/heartbeat.json").write_text(json.dumps({"time": NOW.timestamp(), "interval": 20}))
    (folder / "events.log").write_text(events)
    cfg = json.loads(config.config_file().read_text())
    cfg["routing_sources"] = [{"name": "runner", "kind": "lanes", "path": str(folder)}]
    config.config_file().write_text(json.dumps(cfg))
    return folder


@pytest.mark.parametrize("phrase", ["usage limit", "rate limit", "quota"])
@pytest.mark.parametrize("separator", [" ", ": "])
def test_cursor_grok_limit_bills_shared_grok_account(mac, phrase, separator):
    runner(mac, {"cursor-grok": {"kind": "cursor", "model": "grok-4.7-high"},
                 "cursor-auto": {"kind": "cursor", "budget_account": "grok|", "model": "auto"}},
           f"2026-10-20T15:59:00Z job ERROR lane=cursor-grok{separator}{phrase} {EMAIL}\n")
    view = routing.read_view()
    rows = {a["harness"]: a for a in view["accounts"]}
    assert "cursor" not in rows and rows["grok"]["state"] == "limited"
    assert (rows["grok"]["until"], rows["grok"]["basis"]) == ("2026-10-20T16:29:00Z", "estimated")
    assert rows["grok"]["witness"]["model"] == "grok-4.7-high"
    assert rows["grok"]["witness"]["family"] == "xAI"
    assert rows["grok"]["witness"]["transport"] == "cursor"
    assert routing_sources.model_family("auto") == "unknown"
    matches = view["sources"]["runners"][0]["lane_matches"]
    assert matches["cursor-grok"]["matched_by"] == "provider"
    assert not view["sources"]["runners"][0]["unmatched_lanes"]
    public = routing.conversation_view()
    assert EMAIL not in json.dumps(public)
    assert all(EMAIL.encode() not in p.read_bytes() for p in mac[1].rglob("*") if p.is_file())
    routing.report(runner="runner", lane="cursor-auto", state="limited", message="quota")
    assert any(e.get("account") == "grok|" and e.get("family") == "unknown" and e.get("transport") == "cursor"
               for e in routing._read_all_events()[0] if e.get("type") == "evidence")


@pytest.mark.parametrize("lane,model,family", [("cursor-grok", "grok-4.7-high", "xAI"),
                                               ("cursor-auto", "auto", "unknown")])
def test_cursor_church_runner_limited_line_uses_until(mac, lane, model, family):
    runner(mac, {lane: {"kind": "cursor", "model": model, "budget_account": "grok|"}},
           f"2026-10-20T15:59:00Z fix-40 LIMITED lane={lane} until=2026-10-20T19:00:00Z "
           "job=fix-40 (account limit reached); the fix step is retried later\n")
    rows = {a["harness"]: a for a in routing.read_view()["accounts"]}
    assert (rows["grok"]["state"], rows["grok"]["until"], rows["grok"]["basis"]) == (
        "limited", "2026-10-20T19:00:00Z", "cli")
    assert "cursor" not in rows
    assert {k: rows["grok"]["witness"][k] for k in ("transport", "model", "family")} == {
        "transport": "cursor", "model": model, "family": family}
    public = routing.conversation_view()
    assert public["accounts"][0]["witness"]["transport"] == "cursor"


def test_native_grok_limit_still_bills_grok(mac):
    runner(mac, {"native": {"kind": "grok", "model": "grok-4.7-high"}},
           "2026-10-20T15:59:00Z job LIMITED lane=native until=2026-10-20T17:00:00Z\n")
    rows = {a["harness"]: a for a in routing.read_view()["accounts"]}
    assert rows["grok"]["state"] == "limited" and "cursor" not in rows


def test_declared_tier_reports_and_ambiguous_health(mac):
    cfg = json.loads(config.config_file().read_text())
    cfg["usage_accounts"] += [{"harness": "claude", "label": "Main"},
                              {"harness": "claude", "label": "Second", "home": "/logins/second"}]
    config.config_file().write_text(json.dumps(cfg))
    runner(mac, {"main-sonnet": {"kind": "claude", "budget_account": "claude|", "model": "sonnet"},
                 "ambiguous": {"kind": "claude"}}, "")
    view = routing.read_view()
    status = view["sources"]["runners"][0]
    assert status["unmatched_lanes"] == ["ambiguous"]
    assert set(status["lane_matches"]["ambiguous"]["candidates"]) == {"Main", "Second"}
    routing.report(runner="runner", lane="main-sonnet", state="limited", message="usage limit")
    assert next(a for a in routing.read_view()["accounts"] if a["key"] == "claude|")["state"] == "limited"
    with pytest.raises(routing.RouteError):
        routing.report(runner="runner", lane="ambiguous", state="limited")


def test_cursor_transport_keeps_multiple_grok_accounts_ambiguous():
    known = [{"key": "grok|", "label": "Grok", "harness": "grok"},
             {"key": "grok|/second", "label": "Second", "harness": "grok"}]
    assert routing.resolve_lane("r", "cursor-auto", {}, known, {"kind": "cursor"})["key"] is None
    assert routing.resolve_lane("r", "cursor-auto", {}, known, {
        "kind": "cursor", "budget_account": "grok|"})["key"] == "grok|"


def test_legacy_cursor_history_does_not_revive_account_or_priority(mac):
    routing._write_many([
        {"type": "evidence", "account": "cursor|", "state": "limited", "source": "report",
         "until": "2026-10-20T19:00:00Z"},
        {"type": "pause", "account": "cursor|", "until": "2026-10-20T19:00:00Z"},
        {"type": "priority_set", "entries": [{"account": "cursor|", "label": "Cursor"},
                                                {"account": "grok|", "label": "Grok"}]}])
    view = routing.read_view()
    assert [a["harness"] for a in view["accounts"]] == ["grok"]
    assert [e["account"] for e in view["priority"]["entries"]] == ["grok|"]
    assert usage.doctor_block()["warnings"]


def test_old_view_with_cursor_account_is_refolded(mac):
    routing.report(account="Grok", state="open")
    view = routing.read_view()
    view["fingerprint"].pop("payer_identity")
    view["accounts"].append({"key": "cursor|", "label": "Cursor", "harness": "cursor", "state": "open"})
    routing._save_view(view)
    assert [a["harness"] for a in routing.read_view()["accounts"]] == ["grok"]


def test_grok_aggregate_usage_includes_cursor_without_another_usage_row(mac, monkeypatch):
    frames = [json.loads(line) for line in (Path(__file__).parent / "fixtures/usage/grok.jsonl").read_text().splitlines()]
    billing = frames[-1]["result"]["config"]
    billing["creditUsagePercent"] = 14
    billing["currentPeriod"]["end"] = "2026-10-26T23:52:00Z"
    row = dict(usage.parse_grok(frames), account="Grok", harness="grok", home=None, key="grok|",
               probed_at=NOW.isoformat(), last_attempt_at=NOW.isoformat())
    monkeypatch.setattr(usage, "_load_cache", lambda: {"grok|": row, "cursor|": dict(row, harness="cursor")})
    report = usage.report()
    assert [r["harness"] for r in report["accounts"]] == ["grok"]
    assert report["accounts"][0]["windows"][0]["used_pct"] == 14
    assert routing.plan_fact(report["accounts"][0], NOW)["used_pct"] == 14
    assert "breakdown" not in report["accounts"][0]
