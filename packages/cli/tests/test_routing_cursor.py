"""Slice 9d: fake Cursor status, payer resolution and model provenance only."""
from __future__ import annotations

import builtins
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from khipu import accounts, config, discover, routing, routing_sources, usage

NOW = datetime(2026, 10, 20, 16, 0, tzinfo=timezone.utc)
EMAIL = "cursor-identity-sentinel@example.invalid"
WHY = "Cursor reports no usage figures; its limits come from runner and conversation reports"


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


def fake_cursor(tmp_path, monkeypatch, reply=None, *, stderr="", code=0, delay=0):
    script = tmp_path / "cursor-agent"
    record = tmp_path / "calls.jsonl"
    script.write_text(f'''#!{sys.executable}
import json, os, sys, time
with open({str(record)!r}, "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "env": list(os.environ), "cwd": os.listdir(".")}}) + "\\n")
time.sleep({delay})
if sys.argv[1:] == ["--version"]:
    print("2026.10.19")
else:
    print({json.dumps(reply)!r})
    sys.stderr.write({stderr!r})
    sys.exit({code})
''')
    script.chmod(0o755)
    monkeypatch.setattr(usage, "_command", lambda h: str(script) if h == "cursor" else None)
    return script, record


def test_cursor_probe_private_identity_plan_and_cache(mac, tmp_path, monkeypatch, capsys):
    script, record = fake_cursor(tmp_path, monkeypatch, {
        "authenticated": True, "user": {"email": EMAIL}, "subscription": {"tier": "ultra"},
        "untrusted": EMAIL, "usedPercent": 0})
    monkeypatch.setenv("CURSOR_API_KEY", "secret-sentinel")
    original = builtins.open
    def guarded(path, *args, **kwargs):
        assert "API Keys" not in str(path)
        assert ".cursor/" not in str(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", guarded)
    row = usage.probe_account({"label": "Cursor", "harness": "cursor", "home": None})
    assert row["ok"] and row["plan"] == "Ultra" and row["windows"] == []
    assert len(row["identity_hash"]) == 64 and EMAIL not in json.dumps(row)
    again = usage.probe_cursor()
    assert again["identity_hash"] == row["identity_hash"]
    assert routing.plan_fact(row, NOW) == {"readable": False, "why": WHY, "tier": "Ultra"}
    usage.report(fresh=True, accounts=[{"label": "Cursor", "harness": "cursor", "home": None}])
    public = usage.public_row(row)
    assert public["note"] == WHY and "identity_hash" not in public
    found = discover._check_one({"cli": "cursor", "default": True}, run=usage._Run(), end=time.monotonic() + 10)
    assert found == ("signed_in", None, "Ultra")
    for call in map(json.loads, record.read_text().splitlines()):
        assert call["argv"] == ["about", "--format", "json"]
        assert "CURSOR_API_KEY" not in call["env"] and call["cwd"] == []
    assert EMAIL not in json.dumps(routing.conversation_view())
    assert EMAIL not in capsys.readouterr().out
    assert all(EMAIL.encode() not in p.read_bytes() for p in mac[1].rglob("*") if p.is_file())


@pytest.mark.parametrize("reply,stderr,code,error", [
    ({"authenticated": False, "user": {"email": EMAIL}}, "", 0, "not_signed_in"),
    (None, "Not logged in " + EMAIL, 1, "not_signed_in"),
    ({"unexpected": EMAIL}, "", 0, "bad_reply"),
    (None, "failed " + EMAIL, 7, "exit_7"),
])
def test_cursor_failures_never_return_cli_text(mac, tmp_path, monkeypatch, reply, stderr, code, error):
    fake_cursor(tmp_path, monkeypatch, reply, stderr=stderr, code=code)
    row = usage.probe_cursor()
    assert row == {"ok": False, "error": error}
    assert EMAIL not in json.dumps(row)


def test_cursor_timeout_and_ten_second_ceiling(mac, tmp_path, monkeypatch):
    fake_cursor(tmp_path, monkeypatch, {}, delay=2)
    began = time.monotonic()
    assert usage.probe_cursor(timeout=0.05) == {"ok": False, "error": "timeout"}
    assert time.monotonic() - began < 1
    captured = []
    def run(*args, **kw):
        captured.append(kw["timeout"])
        return {"outcome": "timeout"}
    monkeypatch.setattr(discover, "run_once", run)
    usage.probe_cursor(timeout=100)
    assert captured == [10]


def test_cursor_hash_is_salted_and_plan_is_not_free_text(mac, monkeypatch):
    data = {"user": {"email": EMAIL}, "plan": EMAIL}
    one = usage.parse_cursor(data)
    monkeypatch.setattr(routing, "_id_salt", lambda: b"another-install-salt")
    two = usage.parse_cursor(data)
    assert one["identity_hash"] != two["identity_hash"]
    assert one["plan"] is None and EMAIL not in json.dumps(one)


def test_cursor_discovery_and_decline_are_single_login(mac, tmp_path, monkeypatch, khipu_app):
    fake_cursor(tmp_path, monkeypatch, {"user": {"email": EMAIL}, "subscription": "Ultra"})
    config.config_file().write_text(json.dumps({"usage_accounts": [{"harness": "grok", "label": "Grok"}]}))
    found, _, _ = discover._gather(NOW.timestamp())
    cursor = [r for r in found.values() if r["cli"] == "cursor"]
    assert len(cursor) == 1 and cursor[0]["default"] and cursor[0]["abs"] is None
    accounts.decline("cursor")
    assert not [r for r in discover._gather(NOW.timestamp())[0].values() if r["cli"] == "cursor"]
    accounts.offer_again("cursor")
    assert [r for r in discover._gather(NOW.timestamp())[0].values() if r["cli"] == "cursor"]
    accounts.track([{"harness": "cursor", "label": "Cursor"}], read=False, expect=accounts.status()["version"])
    assert any(a["key"] == "cursor|" for a in routing.accounts())
    assert not [r for r in discover._gather(NOW.timestamp())[0].values() if r["cli"] == "cursor"]
    assert routing.read_view()["priority"] is None


def test_cursor_cli_choice_and_binary_name(mac, tmp_path, monkeypatch):
    from khipu import synth_cli
    from khipu.cli import main
    # main's parser accepts Cursor for both discovery reads and decline.
    assert main(["usage", "--try", "--harness", "cursor", "--json"]) == 0
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "cursor-agent"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    assert synth_cli.binary("cursor") == str(fake)
    assert "cli_path.cursor" in config.CLI_SETTINGS
    assert discover.argv_allowed("cursor", (str(fake), "about", "--format", "json"))
    assert not discover.argv_allowed("cursor", (str(fake), "--api-key", "secret", "about"))
    assert not discover.argv_allowed("cursor", (str(fake), "a prompt"))


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
    for spec in ({"kind": "cursor", "provider": "grok"},
                 {"kind": "cursor", "budget_account": "grok|"},
                 {"account": "cursor|", "budget_account": "grok|"},
                 {"kind": "cursor", "budget_account": "missing|"}):
        assert routing.resolve_lane("r", "x", {}, known, spec)["key"] is None
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
def test_cursor_grok_limit_bills_cursor_and_keeps_grok_open(mac, phrase):
    runner(mac, {"cursor-grok": {"kind": "cursor", "model": "grok-4.7-high"},
                 "cursor-auto": {"provider": "cursor", "model": "auto"}},
           f"2026-10-20T15:59:00Z job ERROR lane=cursor-grok {phrase} {EMAIL}\n")
    routing.report(account="Grok", state="open")
    view = routing.read_view()
    rows = {a["harness"]: a for a in view["accounts"]}
    assert rows["cursor"]["state"] == "limited" and rows["grok"]["state"] == "open"
    assert rows["cursor"]["witness"]["model"] == "grok-4.7-high"
    assert rows["cursor"]["witness"]["family"] == "xAI"
    assert routing_sources.model_family("auto") == "unknown"
    matches = view["sources"]["runners"][0]["lane_matches"]
    assert matches["cursor-grok"]["matched_by"] == "provider"
    assert not view["sources"]["runners"][0]["unmatched_lanes"]
    public = routing.conversation_view()
    assert EMAIL not in json.dumps(public)
    assert all(EMAIL.encode() not in p.read_bytes() for p in mac[1].rglob("*") if p.is_file())
    routing.report(runner="runner", lane="cursor-auto", state="limited", message="quota")
    assert any(e.get("account") == "cursor|" and e.get("family") == "unknown"
               for e in routing._read_all_events()[0] if e.get("type") == "evidence")


def test_native_grok_limit_still_bills_grok(mac):
    runner(mac, {"native": {"kind": "grok", "model": "grok-4.7-high"}},
           "2026-10-20T15:59:00Z job LIMITED lane=native until=2026-10-20T17:00:00Z\n")
    routing.report(account="Cursor", state="open")
    rows = {a["harness"]: a for a in routing.read_view()["accounts"]}
    assert rows["grok"]["state"] == "limited" and rows["cursor"]["state"] == "open"


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
