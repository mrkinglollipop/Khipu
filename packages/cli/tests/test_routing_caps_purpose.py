"""Purpose caps use fake samples and temporary owner/runner worlds only."""
import json
from datetime import timedelta

import pytest

from khipu import cli, routing, routing_advice, routing_pace
from tests.test_routing_acceptance import AUTHOR
from tests.test_routing_s17c_advise import _sample
from tests.test_owner_direction_e2e import world  # noqa: F401

pytest_plugins = ("tests.test_routing_acceptance",)


def _codex(replay, used, *, checks=True):
    routing.report(account="Codex", state="open", author=AUTHOR)
    replay.write(_sample(replay, account="codex|", used=used,
                         reset=replay.now + timedelta(seconds=604800 * (1 - .2196))))
    routing.set_cap("Codex", window="weekly", kind="pace_target", pct=100, slack=0,
                    revision="jobs", via="app")
    if checks:
        routing.set_cap("Codex", window="weekly", kind="pace_target", pct=100, slack=5,
                        purpose="check", revision="checks", via="app")


@pytest.mark.parametrize("used,expected", [(24, "go"), (26.96, "go"), (26.97, "hold_back"),
                                           (50, "hold_back")])
def test_codex_check_allowance_is_five_points_above_even(replay, used, expected):
    _codex(replay, used)
    job = routing.advise("Codex", purpose="job", now=replay.now, environ={})
    check = routing.advise("Codex", purpose="check", now=replay.now, environ={})
    assert job["verdict"] == "hold_back"
    assert job["numbers"]["allowance_pct"] == pytest.approx(21.96, abs=.001)
    assert check["verdict"] == expected
    assert check["numbers"]["allowance_pct"] == pytest.approx(26.96, abs=.001)
    assert [c["purpose"] for c in check["caps"] if c["applies"]] == ["check"]


@pytest.mark.parametrize("used", [10, 24, 50])
def test_no_check_cap_means_checks_equal_jobs(replay, used):
    _codex(replay, used, checks=False)
    job = routing.advise("Codex", now=replay.now, environ={})
    check = routing.advise("Codex", purpose="check", now=replay.now, environ={})
    assert check["verdict"] == job["verdict"]
    assert check["numbers"] == job["numbers"]


@pytest.mark.parametrize("purpose", ["job", "check", "orchestration"])
def test_claude_share_ceiling_closes_every_purpose_at_91(replay, purpose):
    routing.report(account="Claude", state="open", author=AUTHOR)
    replay.write(_sample(replay, used=91))
    routing.set_cap("Claude", window="weekly", kind="share", pct=91, via="app")
    assert routing.advise("Claude", purpose=purpose, now=replay.now, environ={})["verdict"] == "closed"


def test_check_cap_removal_keeps_job_cap_and_restores_fallback(replay):
    _codex(replay, 24)
    routing.remove_cap("Codex", window="weekly", kind="pace_target", purpose="check", via="app")
    check = routing.advise("Codex", purpose="check", now=replay.now, environ={})
    assert check["verdict"] == "hold_back"
    assert check["numbers"]["allowance_pct"] == pytest.approx(21.96, abs=.001)
    caps = next(a for a in replay.view()["accounts"] if a["label"] == "Codex")["caps"]
    assert len(caps) == 1 and caps[0]["revision"] == "jobs"


def test_check_cap_reaches_cli_mcp_json_and_advice_line(replay, capsys):
    _codex(replay, 24)
    assert cli.main(["route", "advise", "Codex", "--purpose", "check", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    error, body = replay.call("khipu_route_advise", account="Codex", purpose="check")
    assert not error and out["verdict"] == body["verdict"] == "go"
    assert routing_advice.advice_line(out).startswith("Advice for checks: Codex: go")
    assert cli.main(["route", "--json"]) == 0
    view = json.loads(capsys.readouterr().out)
    codex = next(a for a in view["accounts"] if a["label"] == "Codex")
    assert {c["purpose"] for c in codex["cap_statuses"]} == {"job", "check"}
    assert codex["check_advice"]["verdict"] == "go"
    assert cli.main(["route", "advise", "Codex", "--purpose", "check"]) == 0
    assert "Advice for checks: Codex: go" in capsys.readouterr().out


@pytest.mark.parametrize("purpose", ["orchestration", "checks", "", 1, None])
def test_checked_cap_rejects_invalid_explicit_purpose(purpose):
    assert routing_pace.checked_cap({"kind": "pace_target", "pct": 100, "slack": 5,
                                     "purpose": purpose, "revision": "r"}) is None


def test_check_only_cap_never_becomes_job_cap(replay):
    routing.report(account="Codex", state="open", author=AUTHOR)
    replay.write(_sample(replay, account="codex|", used=50))
    routing.set_cap("Codex", window="weekly", kind="pace_target", pct=100, slack=5,
                    purpose="check", via="app")
    assert routing.advise("Codex", now=replay.now, environ={})["verdict"] == "go"
    assert routing.advise("Codex", purpose="check", now=replay.now, environ={})["verdict"] == "hold_back"


def test_owner_direction_and_app_undo_keep_the_two_purposes_separate(world, khipu_app):
    from tests.test_owner_direction_e2e import _act, _hook, _log

    routing.set_cap("Codex", window="weekly", kind="pace_target", pct=100, slack=0,
                    revision="job", via="app")
    _hook("allow Codex checks up to five points over even weekly pace")
    error, out = _act("route.cap.set", {"account": "Codex", "kind": "pace_target", "window": "weekly",
                                       "pct": 100, "slack": 5, "purpose": "check"})
    assert not error, out
    line = next(x for x in _log() if x.get("kind") == "action")
    assert line["facts"]["purpose"] == "check" and line["before"]["cap"] is None
    khipu_app.as_app()
    assert cli.main(["owner-direction", "undo", "--from-app", "--", line["id"]]) == 0
    caps = next(a for a in routing.read_view()["accounts"] if a["label"] == "Codex")["caps"]
    assert len(caps) == 1 and caps[0]["revision"] == "job"
    assert cli.main(["route", "cap", "set", "Codex", "--kind", "pace_target", "--window", "weekly",
                     "--pct", "100", "--slack", "5", "--purpose", "check", "--from-app"]) == 0
    assert cli.main(["route", "cap", "remove", "Codex", "--kind", "pace_target", "--window", "weekly",
                     "--purpose", "check", "--from-app"]) == 0
    caps = next(a for a in routing.read_view()["accounts"] if a["label"] == "Codex")["caps"]
    assert len(caps) == 1 and caps[0]["revision"] == "job"


def test_a_dispatched_job_cannot_write_a_check_cap(world):
    from khipu import owner_turns
    from tests.test_owner_direction_e2e import _act, _hook

    _hook("allow Codex checks five points of slack")
    from tests.test_owner_direction_e2e import SESSION
    path = owner_turns.session_file(SESSION)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[-1]["delegated"] = True
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    error, out = _act("route.cap.set", {"account": "Codex", "kind": "pace_target", "window": "weekly",
                                       "pct": 100, "slack": 5, "purpose": "check"})
    assert error and not out.get("ok"), out
    assert not next(a for a in routing.read_view()["accounts"] if a["label"] == "Codex").get("caps")


def test_conversation_check_cap_stays_a_proposal(world):
    routing.request_cap("Codex", window="weekly", kind="pace_target", pct=100, slack=5,
                        purpose="check", revision="proposal", author={"kind": "owner"})
    view = routing.read_view()
    assert not next(a for a in view["accounts"] if a["label"] == "Codex").get("caps")
    assert view["cap_proposals"][0]["purpose"] == "check"


def test_check_advice_changes_are_pushed_and_acknowledged(replay):
    _codex(replay, 24, checks=False)
    routing.set_priority([{"account": "Codex"}], via="app", expect=routing.priority_version())
    _, _, info = replay.offer()
    replay.ack(info)
    routing.set_cap("Codex", window="weekly", kind="pace_target", pct=100, slack=5,
                    purpose="check", via="app")
    _, context, info = replay.offer()
    assert "Advice for checks: Codex: go" in context
    replay.ack(info)
    routing.remove_cap("Codex", window="weekly", kind="pace_target", purpose="check", via="app")
    _, context, _ = replay.offer()
    assert "Advice for checks: Codex: hold back" in context


@pytest.mark.parametrize("used,expected", [(40, "go"), (42, "hold_back")])
def test_claude_sonnet_check_allowance_uses_twenty_points(replay, used, expected):
    routing.report(account="Claude", state="open", author=AUTHOR)
    replay.write(_sample(replay, used=used,
                         reset=replay.now + timedelta(seconds=604800 * (1 - .2196))))
    routing.set_cap("Claude", window="weekly", kind="pace_target", pct=100, slack=5, via="app")
    routing.set_cap("Claude", window="weekly", kind="pace_target", pct=100, slack=20,
                    purpose="check", via="app")
    assert routing.advise("Claude", purpose="check", tier="sonnet", privacy=True,
                          now=replay.now, environ={})["verdict"] == expected
    assert routing.advise("Claude", purpose="job", now=replay.now, environ={})["verdict"] == "hold_back"
    assert routing.advise("Claude", purpose="orchestration", now=replay.now, environ={})["verdict"] == "go"


def test_a_check_tag_never_limits_a_share_ceiling_to_checks(replay):
    _codex(replay, 95)
    routing.set_cap("Codex", kind="share", window="weekly", pct=95, purpose="check", via="app")
    for purpose in routing_advice.PURPOSES:
        assert routing.advise("Codex", purpose=purpose, now=replay.now, environ={})["verdict"] == "closed"


def test_runner_declarations_match_only_the_same_purpose():
    from khipu import routing_scanner, routing_sources

    check = {"kind": "pace_target", "window": "weekly", "pct": 100, "slack": 5, "purpose": "check"}
    assert routing_sources._budget_declaration(check) == check
    accounts = [{"key": "codex|", "label": "Codex", "caps": [{**check, "revision": "r"}]}]
    assert not routing_scanner.compare_runner_caps([{**check, "account": "Codex"}], accounts)
    job = {k: v for k, v in check.items() if k != "purpose"}
    flags = routing_scanner.compare_runner_caps([{**job, "account": "Codex"}], accounts)
    assert len(flags) == 1 and flags[0]["status"] == "no_fact"


def test_typed_terminal_check_cap_uses_the_existing_confirmation(world, khipu_app, monkeypatch):
    import sys

    for name in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    khipu_app.as_shell()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    command = ["route", "cap", "set", "Codex", "--kind", "pace_target", "--window", "weekly",
               "--pct", "100", "--slack", "5", "--purpose", "check"]
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")
    assert cli.main(command) == 2
    assert not next(a for a in routing.read_view()["accounts"] if a["label"] == "Codex").get("caps")
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    assert cli.main(command) == 0
    cap, = next(a for a in routing.read_view()["accounts"] if a["label"] == "Codex")["caps"]
    assert cap["purpose"] == "check" and cap["slack"] == 5


def test_legacy_cap_fact_and_explicit_job_share_the_same_slot(replay):
    _codex(replay, 24, checks=False)
    legacy = replay.event("cap_set", 100, account="codex|", kind="pace_target", window="weekly",
                          pct=100, slack=2, revision="legacy")
    replay.write(legacy)
    job = routing.advise("Codex", now=replay.now, environ={})
    check = routing.advise("Codex", purpose="check", now=replay.now, environ={})
    assert check["verdict"] == job["verdict"] == "hold_back"
    caps = next(a for a in replay.view()["accounts"] if a["label"] == "Codex")["caps"]
    assert len(caps) == 1 and caps[0]["revision"] == "legacy"


def test_check_fallback_is_per_window_and_unknown_readings_keep_no_figures(replay):
    _codex(replay, 24)
    replay.write(_sample(replay, account="codex|", used=50, window="five_hour",
                         reset=replay.now + timedelta(hours=4)))
    routing.set_cap("Codex", window="five_hour", kind="pace_target", pct=100, slack=0, via="app")
    advice = routing.advise("Codex", purpose="check", now=replay.now, environ={})
    assert advice["verdict"] == "hold_back"
    selected = {(c["window"], c["purpose"]) for c in advice["caps"] if c["applies"]}
    assert selected == {("weekly", "check"), ("five_hour", "job")}
    replay.advance(3 * 86400)
    advice = routing.advise("Codex", purpose="check", now=replay.now, environ={})
    assert advice["evidence"] == "unknown" and advice["numbers"] == {}
    assert all(c.get("allowance_pct") is None and c.get("used_pct") is None for c in advice["caps"])


@pytest.mark.parametrize("action", ["set", "remove"])
def test_undo_restores_an_existing_check_cap_without_changing_jobs(world, khipu_app, action):
    from tests.test_owner_direction_e2e import _act, _hook, _log

    routing.set_cap("Codex", window="weekly", kind="pace_target", pct=100, slack=0,
                    revision="job", via="app")
    routing.set_cap("Codex", window="weekly", kind="pace_target", pct=100, slack=5,
                    purpose="check", revision="check", via="app")
    _hook("change the Codex check allowance")
    args = {"account": "Codex", "kind": "pace_target", "window": "weekly", "purpose": "check"}
    if action == "set":
        args.update(pct=100, slack=20)
    error, out = _act("route.cap." + action, args)
    assert not error, out
    line = next(x for x in _log() if x.get("kind") == "action")
    assert line["before"]["cap"]["revision"] == "check"
    khipu_app.as_app()
    assert cli.main(["owner-direction", "undo", "--from-app", "--", line["id"]]) == 0
    caps = next(a for a in routing.read_view()["accounts"] if a["label"] == "Codex")["caps"]
    assert {(c.get("purpose", "job"), c["slack"], c["revision"]) for c in caps} == {
        ("job", 0, "job"), ("check", 5, "check")}


def test_each_share_fact_keeps_its_own_verdict_with_two_purposes(replay):
    _codex(replay, 93)
    routing.set_cap("Codex", window="weekly", kind="share", pct=95, via="app")
    routing.set_cap("Codex", window="weekly", kind="share", pct=91, purpose="check", via="app")
    entry = next(a for a in replay.view()["accounts"] if a["label"] == "Codex")
    shares = {s["purpose"]: s for s in routing.cap_statuses(replay.view(), entry, replay.now) if s["kind"] == "share"}
    assert shares["job"]["verdict"] == "unknown"
    assert shares["check"]["verdict"] == "cap_reached"
    assert entry["pace"][0]["share_cap"]["pct"] == 91
    for purpose in routing_advice.PURPOSES:
        assert routing.advise("Codex", purpose=purpose, now=replay.now, environ={})["verdict"] == "closed"


def test_each_daily_fact_keeps_its_own_verdict():
    from tests.test_routing_s17a_caps import T0, samp

    caps = [{"kind": "per_day", "pct": 10, "revision": "job"},
            {"kind": "per_day", "pct": 30, "purpose": "check", "revision": "check"}]
    samples = [samp(T0 - timedelta(hours=24 - hour), hour,
                    reset=T0 + timedelta(days=4)) for hour in range(25)]
    pace = routing_pace.compute_pace(samples, caps, T0, "weekly")
    assert pace["day_cap"]["pct"] == 10
    assert {s["purpose"]: s["verdict"] for s in pace["ceiling_verdicts"]} == {
        "job": "over", "check": "under"}
