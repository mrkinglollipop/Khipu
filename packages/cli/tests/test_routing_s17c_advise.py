"""Slice 17c, case 17b: khipu route advise, the advice field, purpose,
ranking and pushes. Advisory only: advice never blocks, refuses or permits
anything, and a dispatched job claiming orchestration still gets job advice.
Hermetic: fake samples and fake runner figures only."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from khipu import config, routing, usage
from tests.test_routing_acceptance import AUTHOR  # noqa: F401

pytest_plugins = ("tests.test_routing_acceptance",)

T0 = datetime(2026, 10, 20, 14, tzinfo=timezone.utc)


_SAMPLE_N = [0]


def _sample(replay, account="claude|", used=18, *, at=None, reset=None, window="weekly"):
    _SAMPLE_N[0] += 1
    at = at or replay.now
    reset = reset or (at + timedelta(hours=157))
    return replay.event("usage_sample", _SAMPLE_N[0], account=account, window=window,
                        witness_at=routing._iso(at), used_pct=used,
                        resets_at=routing._iso(reset), mac="m1", source="probe", t_err=0)


def _pace_target(replay, used=18):
    routing.report(account="Claude", state="open", author=AUTHOR)
    replay.write(_sample(replay, used=used))
    routing.set_cap("Claude", window="weekly", kind="pace_target", pct=65, slack=5,
                    revision="pt1", via="app")


# ---- case 17b: pace_target purpose split -----------------------------------


def test_pace_target_ahead_holds_job_but_not_orchestration(replay):
    _pace_target(replay, used=18)
    job = routing.advise("Claude", purpose="job", now=replay.now)
    orch = routing.advise("Claude", purpose="orchestration", now=replay.now)
    assert job["verdict"] == "hold_back" and job["purpose"] == "job"
    assert job["numbers"]["allowance_pct"] == pytest.approx(9.26, abs=0.01)
    assert orch["verdict"] == "go" and orch["purpose"] == "orchestration"


def test_dispatched_job_claiming_orchestration_gets_job_advice(replay, monkeypatch):
    _pace_target(replay, used=18)
    monkeypatch.setenv("KHIPU_JOB", "1")
    out = routing.advise("Claude", purpose="orchestration", now=replay.now)
    assert out["verdict"] == "hold_back"
    assert out["purpose"] == "job" and out["requested_purpose"] == "orchestration"
    assert out["dispatched"] is True


def test_zero_burn_is_still_hold_back_while_ahead(replay):
    _pace_target(replay, used=18)
    out = routing.advise("Claude", purpose="job", now=replay.now)
    assert out["verdict"] == "hold_back"


def test_recovery_needs_a_point_under_the_allowance(replay):
    routing.report(account="Claude", state="open", author=AUTHOR)
    # Ahead earlier in the cycle, now inside the 1-point band: still held.
    replay.write(_sample(replay, used=10, at=replay.now - timedelta(hours=6),
                         reset=replay.now + timedelta(hours=157)))
    replay.write(_sample(replay, used=8.8))
    routing.set_cap("Claude", window="weekly", kind="pace_target", pct=65, slack=5,
                    revision="pt1", via="app")
    assert routing.advise("Claude", purpose="job", now=replay.now)["verdict"] == "hold_back"
    # At allowance - 1 (8.26) or below it recovers.
    replay.advance(16 * 60)
    replay.write(_sample(replay, used=8.0, reset=replay.now + timedelta(hours=157)))
    out = routing.advise("Claude", purpose="job", now=replay.now)
    assert out["verdict"] == "go"


def test_pause_is_closed_for_both_purposes(replay):
    routing.report(account="Claude", state="open", author=AUTHOR)
    routing.pause("Claude", via="app")
    for purpose in ("job", "orchestration"):
        out = routing.advise("Claude", purpose=purpose, now=replay.now)
        assert out["verdict"] == "closed", purpose


def test_share_cap_holds_orchestration_too(replay):
    routing.report(account="Claude", state="open", author=AUTHOR)
    replay.write(_sample(replay, used=95))
    routing.set_cap("Claude", window="weekly", kind="share", pct=91, revision="sh1", via="app")
    assert routing.advise("Claude", purpose="orchestration", now=replay.now)["verdict"] == "hold_back"
    assert routing.advise("Claude", purpose="job", now=replay.now)["verdict"] == "hold_back"


def test_advice_field_is_on_each_khipu_route_account(replay):
    _pace_target(replay, used=18)
    body = replay.pull()
    claude = next(a for a in body["accounts"] if a["label"] == "Claude")
    assert claude["advice"]["verdict"] == "hold_back"
    assert claude["advice"]["evidence"] in ("fresh", "stale", "unknown")
    assert "advisory" in claude["advice"] or claude["advice"].get("gate") is not True


def test_ranking_follows_the_owner_priority_and_says_why(replay):
    routing.report(account="Claude", state="open", author=AUTHOR)
    routing.report(account="Codex", state="open", author=AUTHOR)
    routing.set_priority([{"account": "Codex", "role": "first"},
                          {"account": "Claude", "role": "second"}],
                         via="app", expect=routing.priority_version())
    out = routing.advise(now=replay.now)
    ranked = [r for r in out["ranked"] if r["verdict"] == "go"]
    assert [r["label"] for r in ranked[:2]] == ["Codex", "Claude"]
    assert "priority" in ranked[0]["why_ranked"]


def test_cli_advise_and_mcp_advise_agree(replay, capsys):
    from khipu import cli

    _pace_target(replay, used=18)
    assert cli.main(["route", "advise", "Claude", "--purpose", "job", "--json"]) == 0
    cli_out = json.loads(capsys.readouterr().out)
    error, body = replay.call("khipu_route_advise", account="Claude", purpose="job")
    assert not error, body
    assert cli_out["verdict"] == body["verdict"] == "hold_back"


def test_advice_change_for_a_priority_account_pushes(replay):
    routing.report(account="Claude", state="open", author=AUTHOR)
    routing.set_priority([{"account": "Claude", "role": "first"}],
                         via="app", expect=routing.priority_version())
    first, _, info = replay.offer()
    replay.ack(info)
    _pace_target(replay, used=18)
    out, context, info = replay.offer()
    assert out["kind"] != "none"
    assert "hold_back" in context or "hold back" in context


# ---- spend and concurrency (fake runner telemetry, as in 17a) --------------

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


def test_spend_over_holds_both_purposes(mac):
    _runner(mac, {"spend": [_spend("e1", 35.10, NOW - timedelta(hours=1))],
                  "spend_coverage": [_coverage()]})
    routing.set_cap("Main", kind="spend", usd=35, days=7, revision="s1", via="app")
    for purpose in ("job", "orchestration"):
        out = routing.advise("Main", purpose=purpose, now=NOW)
        assert out["verdict"] == "hold_back", purpose
        assert out["numbers"]["spend_usd"] == pytest.approx(35.10)


def test_concurrency_full_holds_job_but_not_orchestration(mac):
    _runner(mac, {"active": [{"lane": "main-lane", "account": MAIN, "jobs": 1,
                              "unit": "job", "at": NOW.timestamp()}]})
    routing.set_cap("Main", kind="concurrency", n=1, revision="c1", via="app")
    assert routing.advise("Main", purpose="job", now=NOW)["verdict"] == "hold_back"
    assert routing.advise("Main", purpose="orchestration", now=NOW)["verdict"] == "go"


def test_expired_telemetry_answers_go_with_unknown_evidence(mac):
    _runner(mac, {"active": [{"lane": "main-lane", "account": MAIN, "jobs": 1,
                              "unit": "job", "at": NOW.timestamp()}]})
    routing.set_cap("Main", kind="concurrency", n=1, revision="c1", via="app")
    later = NOW + timedelta(seconds=200)
    out = routing.advise("Main", purpose="job", now=later)
    assert out["verdict"] == "go" and out["evidence"] == "unknown"
