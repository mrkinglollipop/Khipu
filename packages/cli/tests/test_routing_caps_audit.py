"""Caps audit regressions: synthetic readers, telemetry and clocks only."""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from khipu import routing, routing_advice, routing_pace, routing_sources as rs, usage

NOW = datetime(2026, 10, 22, 12, 2, tzinfo=timezone.utc)
CAP = dict(kind="pace_target", window="weekly", pct=100, slack=0, revision="fake")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("caps regression attempted external I/O")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(usage, "_command", lambda h: "/fake/cli")
    monkeypatch.setattr(routing, "settings", lambda: {"routing_stale_after": 3600})
    monkeypatch.setattr(routing, "account_id", lambda key: key)
    monkeypatch.setattr(routing, "_local_mac", lambda: "fake-mac")
    monkeypatch.setattr(routing, "_telemetry_streams", blocked)
    monkeypatch.setattr(routing, "read_view", blocked)
    monkeypatch.setattr(usage, "_now", lambda: NOW)
    monkeypatch.setattr(usage, "_converse", blocked)


def entry(**overrides):
    return dict(key="claude|", label="Fake Claude", harness="claude", state="open",
                witness={"at": routing._iso(NOW)}, **overrides)


def sample(minutes, used, *, reset=None, mac="fake", cycle=None):
    return dict(t=NOW + timedelta(minutes=minutes), used=used,
                reset=reset or NOW + timedelta(days=6), mac=mac, source="probe", cycle=cycle, t_err=0)


@pytest.mark.parametrize("inferred", [False, True])
def test_routing_active_model_limit_closes_opus_only(inferred):
    account = {"key": "claude|", "label": "Fake Claude", "harness": "claude"}
    def observation(n, state, scope="all", until=None):
        return dict(id=f"fake-{n}", state=state, scope=scope, until=until,
                    at=NOW - timedelta(minutes=5), seq=n, basis="cli", source="report",
                    written_by={"kind": "owner"}, evidence=None)
    folded = routing._fold_account(account, [observation(1, "open"),
        observation(2, "limited", "Opus", NOW + timedelta(hours=1))],
        {"clears": {}, "dismissed": set(), "pauses": {}, "lifts": {}}, NOW,
        {"routing_stale_after": 3600})
    view = {"accounts": [folded]}
    for tier, wanted in (("sonnet", "go"), ("opus", "closed")):
        view["tier_table"] = {"confirmed": True, "tiers": {"tests": tier}}
        row = routing.advise(view=view, now=NOW, purpose="check", kind="check",
                             tier=None if inferred else tier, environ={})["ranked"][0]
        assert row["verdict"] == wanted


@pytest.mark.parametrize("harness", ["codex", "muse"])
@pytest.mark.parametrize("observed", [None, "invalid", "old", "fresh"])
def test_usage_observation_time_required_for_stored_figures(tmp_path, monkeypatch, harness, observed):
    home = tmp_path / "fake-codex"
    (home / "sessions").mkdir(parents=True)
    stamp = NOW - timedelta(hours=2) if observed == "old" else NOW
    wire_time = None if observed is None else "bad-time" if observed == "invalid" else routing._iso(stamp)
    account = {"harness": harness, "label": "Fake account", "home": str(home)}
    if harness == "codex":
        event = {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
            "primary": {"used_percent": 15, "window_minutes": 10080,
                        "resets_at": int((NOW + timedelta(days=1)).timestamp())}}}}
        if wire_time is not None:
            event["timestamp"] = wire_time
        (home / "sessions" / "rollout-fake.jsonl").write_text(json.dumps(event) + "\n")
        def timeout(*a, **kw):
            raise usage.ProbeError(usage.E_TIMEOUT)
        monkeypatch.setattr(usage, "_converse", timeout)
    else:
        value = {"tier": "pro", "weekly": {"usedPercent": 15,
                 "resetsAtMs": int((NOW + timedelta(days=1)).timestamp() * 1000)}}
        if observed is not None:
            value["observedAtMs"] = "bad-time" if observed == "invalid" else int(stamp.timestamp() * 1000)
        monkeypatch.setattr(usage, "_converse", lambda *a, **kw: [{"id": 2, "result": {"usage": value}}])
    monkeypatch.setattr(usage, "_login_folder_there", lambda home: True)
    row = usage._merge(None, usage.probe_account(account))
    monkeypatch.setattr(usage, "_load_cache", lambda: {usage._key(account): row})
    reported = usage.report(accounts=[account])["accounts"][0]
    fact = routing.plan_fact(row, NOW)
    samples = routing._pace_sample_events([], [{"key": usage._key(account)}], NOW)
    if observed in (None, "invalid"):
        assert reported["stale"] is True
        assert fact["stale"] is True and fact["pace_pct_per_hour"] is None
        assert samples == []
        assert routing._check_evidence(usage._key(account), row) == ([], [], False)
        row["windows"][0]["used_pct"] = 100
        assert routing._check_evidence(usage._key(account), row) == ([], [], False)
    else:
        assert reported["stale"] is (observed == "old")
        assert bool(fact.get("stale")) is (observed == "old")
        assert samples[0]["witness_at"] == routing._iso(stamp)


def spend(event, usd, corrects=None):
    value = dict(event=event, usd=usd, at=(NOW - timedelta(hours=1)).timestamp(),
                 account="claude|", provider="claude")
    if corrects is not None:
        value["corrects"] = corrects
    return value


def telemetry(events, arrival=NOW):
    start = NOW - timedelta(days=7)
    parsed = rs._parse_telemetry({"time": arrival.timestamp(), "spend": events,
        "spend_coverage": [{"account": "claude|", "provider": "claude",
                            "from": start.timestamp(), "to": arrival.timestamp()}]},
        [{"key": "claude|", "label": "Fake Claude", "harness": "claude"}])
    return rs.merge_telemetry({"since": (start - timedelta(days=1)).timestamp()}, parsed, now=NOW)


def spend_result(monkeypatch, runners, silent=()):
    e = entry(caps=[dict(kind="spend", usd=35, days=7, revision="fake-spend")])
    view = {"accounts": [e], "source_cache": {"runners": {
        n: {"telemetry": t} for n, t in runners.items()}},
        "sources": {"runners": [{"name": n, "silent": n in silent} for n in runners]}}
    # Restore this pure adapter; the fixture blocks accidental readers elsewhere.
    streams = rs.telemetry_streams(view["source_cache"], lane_map={}, providers=None,
        known=[{"key": "claude|", "label": "Fake Claude", "harness": "claude"}],
        silent={n: n in silent for n in runners})
    monkeypatch.setattr(routing, "_telemetry_streams", lambda view: streams)
    return (rs.spend_total(streams, "claude|", NOW - timedelta(days=7), NOW),
            routing.cap_statuses(view, e, NOW)[0], routing_advice.account_advice(view, e, NOW))


def test_routing_coverage_only_silent_runner_makes_total_unknown(monkeypatch):
    total, cap, advice = spend_result(monkeypatch, {
        "paid": telemetry([spend("paid", 10)]),
        "silent": telemetry([], NOW - timedelta(hours=2))}, silent=("silent",))
    assert total["status"] == "unknown" and total["usd"] is None
    assert cap["verdict"] == "unknown" and advice["evidence"] == "unknown"


@pytest.mark.parametrize("corrections", [
    [spend("self", -30, "self")],
    [spend("cycle-a", -15, "cycle-b"), spend("cycle-b", -15, "cycle-a")],
    [spend("orphan", -30, "missing")],
    [spend("tail", -5, "cycle-a"), spend("cycle-a", -10, "cycle-b"), spend("cycle-b", -15, "cycle-a")],
])
def test_routing_unrooted_corrections_cannot_create_headroom(monkeypatch, corrections):
    total, cap, advice = spend_result(monkeypatch, {"runner": telemetry([spend("paid", 40), *corrections])})
    assert total["status"] == "unknown" and total["usd"] is None
    assert cap["verdict"] == "unknown" and advice["evidence"] == "unknown"
    streams = routing._telemetry_streams({})
    assert rs._stream_spend(streams[0], NOW - timedelta(days=7), NOW) == (40, 1)


def test_routing_valid_correction_chain_keeps_real_spend(monkeypatch):
    total, cap, advice = spend_result(monkeypatch, {"runner": telemetry([
        spend("paid", 40), spend("first", -10, "paid"), spend("second", 8, "first")])})
    assert total["status"] == "known" and total["usd"] == 38
    assert cap["verdict"] == "over" and advice["verdict"] == "hold_back"


@pytest.mark.parametrize("payload", [
    {"limits": [{"unit": 3, "type": "CREDIT_LIMIT", "percentage": -5}]},
    {"windows": [{"window_mins": 300, "currentValue": -5, "usage": 100}]},
    {"windows": [{"window_mins": 300, "used_pct": -5}]},
])
def test_usage_http_quota_rejects_raw_negative_percentages(payload):
    with pytest.raises(usage.ProbeError) as caught:
        usage.parse_http_quota(200, json.dumps(payload).encode())
    assert caught.value.code == usage.E_BAD


@pytest.mark.parametrize("payload", [
    {"limits": [{"unit": 3, "type": "TOKENS_LIMIT", "percentage": 100.4}]},
    {"limits": [{"unit": 3, "type": "TOKENS_LIMIT", "currentValue": 1004, "usage": 1000}]},
    {"limits": [{"unit": 3, "type": "CREDIT_LIMIT", "percentage": 250}]},
    {"windows": [{"window_mins": 300, "currentValue": 250, "usage": 100}]},
])
def test_usage_http_quota_clamps_overdrawn_glm_percentages(payload):
    parsed = usage.parse_http_quota(200, json.dumps(payload).encode())
    assert parsed["windows"][0]["used_pct"] == 100


@pytest.mark.parametrize("source", ["rollout", "last_observed"])
def test_usage_bad_window_observation_falls_back_to_valid_row_time(source):
    row = {"source": source, "observed_at": "2026-10-22T12:00:00Z",
           "probed_at": routing._iso(NOW), "windows": [{"name": "weekly", "used_pct": 100,
           "resets_at": "2026-10-23T12:00:00Z", "observed_at": "junk"}]}
    assert usage._figures_time(row, row["windows"][0]) == NOW - timedelta(minutes=2)


@pytest.mark.parametrize("window, readings", [
    ("weekly", [sample(-55, 40), sample(-30, 60), sample(-1, 100)]),
    ("weekly", [sample(-50, 95), sample(-40, 2), sample(-30, 3), sample(-20, 4), sample(-1, 100)]),
    ("weekly", [sample(-m, 100) for m in range(60, -1, -5)]),
    ("weekly", [sample(-50, 100), sample(-40, 90), sample(-1, 100)]),
    ("five_hour", [sample(-55, 40), sample(-30, 60), sample(-1, 100)]),
], ids=["gappy", "rollover", "dense-thin-coverage", "refuted-reset", "gappy-five-hour"])
def test_routing_fresh_reached_level_closes_despite_unknown_rate(window, readings):
    caps = [dict(CAP, window=window),
            dict(kind="share", window=window, pct=95, revision="fake-share")]
    pace = routing_pace.compute_pace(readings, caps, NOW, window)
    assert pace["why"].startswith("readings ")
    assert pace["plan"]["verdict"] == "limit_reached"
    assert pace["share_cap"]["verdict"] == "cap_reached"
    advice = routing_advice.account_advice({}, entry(pace=[pace], caps=caps), NOW)
    assert advice["verdict"] == "closed"


def test_routing_possible_reset_is_unknown_before_reached_checks():
    caps = [CAP, dict(kind="share", window="weekly", pct=95, revision="fake-share")]
    pace = routing_pace.compute_pace([sample(-59, 90), sample(-30, 96), sample(-2, 100), sample(0, 1)], caps, NOW, "weekly")
    assert pace["why"].startswith("possible reset")
    assert pace["plan"]["verdict"] == "unknown"
    assert pace["share_cap"]["verdict"] == "unknown"
    advice = routing_advice.account_advice({}, entry(pace=[pace], caps=caps), NOW)
    assert advice["verdict"] == "go" and advice["evidence"] == "unknown"
    assert "limit reached" not in routing_pace.pace_line("Fake", pace, NOW)


def test_routing_fresh_thin_reached_reading_still_closes():
    caps = [CAP, dict(kind="share", window="weekly", pct=95, revision="fake-share")]
    pace = routing_pace.compute_pace([sample(0, 100)], caps, NOW, "weekly")
    assert pace["plan"]["verdict"] == "limit_reached"
    assert pace["share_cap"]["verdict"] == "cap_reached"
    assert routing_advice.account_advice({}, entry(pace=[pace], caps=caps), NOW)["verdict"] == "closed"


def test_routing_stale_reached_reading_is_unknown():
    caps = [CAP, dict(kind="share", window="weekly", pct=95, revision="fake-share")]
    pace = routing_pace.compute_pace([sample(-80, 100)], caps, NOW, "weekly")
    assert pace["plan"]["verdict"] == "unknown"
    assert pace["share_cap"]["verdict"] == "unknown"


def test_routing_unknown_pace_preserves_independent_spend_hold(monkeypatch):
    total, spend_cap, _ = spend_result(monkeypatch, {"runner": telemetry([spend("paid", 40)])})
    assert total["status"] == "known"
    pace = routing_pace.compute_pace([sample(-3 * 1440, 80)], [CAP], NOW, "weekly")
    monkeypatch.setattr(routing, "cap_statuses", lambda *a: [spend_cap, {
        "kind": "pace_target", "purpose": "job", "window": "weekly", "verdict": "ahead",
        "allowance_pct": 14.3, "used_pct": 80}])
    advice = routing_advice.account_advice({}, entry(pace=[pace]), NOW)
    assert advice["verdict"] == "hold_back" and advice["reason"] == "spend over"
    assert advice["numbers"]["spend_usd"] == 40


def test_routing_stale_thin_readings_are_unknown():
    pace = routing_pace.compute_pace([sample(-80, 30), sample(-70, 31)], [CAP], NOW, "weekly")
    assert pace["why"].startswith("last reading ")
    advice = routing_advice.account_advice({}, entry(pace=[pace], caps=[CAP]), NOW)
    assert advice["evidence"] == "unknown" and advice["numbers"] == {}
    assert advice["verdict"] == "go"


def test_routing_expired_ahead_reading_does_not_hold_work():
    pace = routing_pace.compute_pace([sample(-3 * 1440, 80)], [CAP], NOW, "weekly")
    advice = routing_advice.account_advice({}, entry(pace=[pace], caps=[CAP]), NOW)
    assert advice["evidence"] == "unknown" and advice["numbers"] == {}
    assert advice["verdict"] == "go"
    assert advice["caps"][0]["verdict"] == "unknown"


def test_routing_old_witness_reset_ambiguity_ages_out():
    reset = NOW + timedelta(days=6)
    samples = [sample(-26 * 60, 1, reset=reset - timedelta(hours=1.2), mac="offline"),
               sample(-25 * 60, 2, reset=reset - timedelta(hours=.6))]
    samples += [sample(-m, 20 + (1440 - m) / 120) for m in range(1440, -1, -30)]
    pace = routing_pace.compute_pace(samples, [CAP], NOW, "weekly")
    assert pace["why"] is None
    assert pace["valid_pct"] == 100
    assert pace["plan"]["verdict"] != "unknown"


@pytest.mark.parametrize("evidence", ["unknown", "stale"])
def test_routing_advice_line_qualifies_uncertain_go(evidence):
    advice = routing_advice.account_advice({}, entry(), NOW)
    advice.update(evidence=evidence)
    assert advice["verdict"] == "go"
    assert f"evidence={evidence}" in routing_advice.advice_line(advice)


def test_routing_desktop_withholds_unplaceable_cap_figures(tmp_path):
    """Execute production TS without dependencies; all runtime imports are pure/stubbed."""
    root = Path(__file__).resolve().parents[3]
    node = shutil.which("node")
    if node is None:
        pytest.skip("production TS probe requires Node with native type stripping")
    runner = tmp_path / "pace-probe.cjs"
    runner.write_text(r"""
const fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
const { stripTypeScriptTypes } = require('node:module');
if (!stripTypeScriptTypes) process.exit(77);
const cache = new Map();
function blocked() { throw Error('external operation forbidden'); }
async function load(name) {
  if (cache.has(name)) return cache.get(name);
  let mod;
  if (name === 'react' || name === './settingsCli') {
    const names = name === 'react' ? ['useCallback','useEffect','useRef','useState'] : ['callCli'];
    mod = new vm.SyntheticModule(names, function() { for (const n of names) this.setExport(n, blocked); });
  } else {
    const file = path.join(process.cwd(), 'src', name.replace(/^\.\//, '') + '.ts');
    mod = new vm.SourceTextModule(stripTypeScriptTypes(fs.readFileSync(file, 'utf8')), { identifier: file });
  }
  cache.set(name, mod);
  if (mod instanceof vm.SourceTextModule) await mod.link(load);
  return mod;
}
(async () => {
  const mod = await load('./routingPace'); await mod.evaluate();
  const cap = { kind: 'pace_target', revision: 'fake', window: 'weekly', pct: 100, slack: 0,
                allowance_pct: 14.3, used_pct: 32, verdict: 'ahead' };
  for (const why of ['reset time spread over more than an hour', 'last reading 2 h old',
                     'possible reset; waiting for the next reading']) {
    const pace = { window: 'weekly', buckets: 48, used_pct: 32, why,
                   plan: { verdict: 'unknown' }, pace_target: cap };
    const a = { key: 'fake|', label: 'Fake', harness: 'claude', state: 'open', pace: [pace], cap_statuses: [cap] };
    const row = mod.namespace.paceModel({ accounts: [a] }, null, new Date('2026-10-22T12:02:00Z')).rows[0];
    if (!row.caps[0].state.startsWith('Not known:') || row.caps[0].tag !== null)
      throw Error(JSON.stringify(row.caps[0]));
  }
})().catch(e => { console.error(e); process.exitCode = 1; });
""")
    result = subprocess.run([node, "--experimental-vm-modules", str(runner)],
                            cwd=root / "apps/desktop", capture_output=True, text=True, timeout=30)
    if result.returncode == 77:
        pytest.skip("production TS probe requires Node with native type stripping")
    assert result.returncode == 0, result.stderr
