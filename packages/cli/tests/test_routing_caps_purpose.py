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
                    purpose="check", tier="sonnet", sensitive_only=True, via="app")
    assert routing.advise("Claude", purpose="check", tier="sonnet", sensitive=True, privacy=True,
                          now=replay.now, environ={})["verdict"] == expected
    assert routing.advise("Claude", purpose="job", now=replay.now, environ={})["verdict"] == "hold_back"
    assert routing.advise("Claude", purpose="orchestration", now=replay.now, environ={})["verdict"] == "go"


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



@pytest.mark.parametrize("kind,fields", [
    ("share", {"window": "weekly", "pct": 95}),
    ("per_day", {"window": "weekly", "pct": 10}),
    ("spend", {"usd": 35, "days": 7}), ("concurrency", {"n": 2}),
])
def test_check_purpose_is_refused_for_non_pace_caps(world, khipu_app, capsys, kind, fields):
    from tests.test_owner_direction_e2e import _act, _hook

    values = {"kind": kind, "purpose": "check", **fields}
    error_words = "check purpose is only supported for pace_target caps"
    assert routing_pace.checked_cap({"revision": "r", **{k: v for k, v in values.items() if k != "window"}}) is None
    with pytest.raises(routing.RouteError, match=error_words):
        routing.set_cap("Codex", **values, via="app")
    with pytest.raises(routing.RouteError, match=error_words):
        routing.request_cap("Codex", **values, revision="p", author={"kind": "conversation"})
    with pytest.raises(routing.RouteError, match=error_words):
        routing.remove_cap("Codex", **{k: v for k, v in values.items() if k in ("kind", "window", "purpose")}, via="app")
    _hook("set a check cap")
    error, out = _act("route.cap.set", {"account": "Codex", **values})
    assert error and error_words in json.dumps(out), out
    khipu_app.as_app()
    args = ["route", "cap", "set", "Codex", "--from-app"]
    for k, v in values.items():
        args += ["--" + k.replace("_", "-"), str(v)]
    assert cli.main(args) == 2
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": error_words}


@pytest.mark.parametrize("tier,sensitive,expected", [
    ("sonnet", True, "go"), ("sonnet", False, "hold_back"),
    ("opus", True, "hold_back"), ("haiku", True, "hold_back"),
    (None, True, "hold_back"), (None, False, "hold_back"),
])
def test_qualified_check_cap_matches_both_declarations(replay, tier, sensitive, expected):
    _codex(replay, 40, checks=False)
    routing.set_cap("Codex", window="weekly", kind="pace_target", pct=100, slack=20,
                    purpose="check", tier="sonnet", sensitive_only=True, via="app")
    out = routing.advise("Codex", purpose="check", tier=tier, sensitive=sensitive,
                         now=replay.now, environ={})
    assert out["verdict"] == expected
    assert out["numbers"]["allowance_pct"] == pytest.approx(41.96 if expected == "go" else 21.96, abs=.001)
    selected, = [c for c in out["caps"] if c["applies"]]
    assert selected["purpose"] == ("check" if expected == "go" else "job")
    cap = next(c for c in out["caps"] if c["purpose"] == "check")
    assert cap["tier"] == "sonnet" and cap["sensitive_only"] is True


@pytest.mark.parametrize("qualifiers", [{"tier": "sonnet"}, {"sensitive_only": True}, {"sensitive_only": False}])
def test_check_qualifiers_are_optional_independently(replay, qualifiers):
    _codex(replay, 24, checks=False)
    routing.set_cap("Codex", window="weekly", kind="pace_target", pct=100, slack=5,
                    purpose="check", **qualifiers, via="app")
    assert routing.advise("Codex", purpose="check", tier="sonnet", sensitive=True,
                          now=replay.now, environ={})["verdict"] == "go"


@pytest.mark.parametrize("qualifiers", [{"tier": "bogus"}, {"tier": 5}, {"sensitive_only": "true"}, {"sensitive_only": 1}])
def test_cap_qualifiers_are_strict(qualifiers):
    assert routing_pace.checked_cap({"kind": "pace_target", "pct": 100, "slack": 20,
                                     "purpose": "check", "revision": "r", **qualifiers}) is None


def test_job_caps_refuse_check_qualifiers(replay):
    with pytest.raises(routing.RouteError, match="qualifiers.*check"):
        routing.set_cap("Codex", kind="pace_target", window="weekly", pct=100, slack=20,
                        tier="sonnet", via="app")


@pytest.mark.parametrize("action", ["set", "remove"])
def test_qualifiers_cross_cli_mcp_proposals_and_undo(world, khipu_app, action):
    from tests.test_owner_direction_e2e import _act, _hook, _log

    values = {"account": "Claude", "kind": "pace_target", "window": "weekly", "pct": 100,
              "slack": 20, "purpose": "check", "tier": "sonnet", "sensitive_only": True}
    _hook("allow Sonnet checks of sensitive code twenty points over even")
    error, out = _act("route.cap.set", values)
    assert not error, out
    line = next(x for x in _log() if x.get("kind") == "action")
    assert line["facts"]["tier"] == "sonnet" and line["facts"]["sensitive_only"] is True
    cap, = next(a for a in routing.read_view()["accounts"] if a["label"] == "Claude")["caps"]
    assert cap["tier"] == "sonnet" and cap["sensitive_only"] is True
    routing.request_cap(**values, revision="p", author={"kind": "conversation"})
    proposal, = routing.read_view()["cap_proposals"]
    assert proposal["tier"] == "sonnet" and proposal["sensitive_only"] is True
    _hook("change the Sonnet check allowance")
    changed_values = ({**values, "slack": 10, "sensitive_only": False} if action == "set"
                      else {k: v for k, v in values.items() if k in ("account", "kind", "window", "purpose")})
    error, out = _act("route.cap." + action, changed_values)
    assert not error, out
    changed = [x for x in _log() if x.get("kind") == "action"][-1]
    caps = next(a for a in routing.read_view()["accounts"] if a["label"] == "Claude").get("caps", [])
    if action == "set":
        assert caps[0]["sensitive_only"] is False
    else:
        assert caps == []
    khipu_app.as_app()
    assert cli.main(["owner-direction", "undo", "--from-app", "--", changed["id"]]) == 0
    cap, = next(a for a in routing.read_view()["accounts"] if a["label"] == "Claude")["caps"]
    assert (cap["tier"], cap["sensitive_only"], cap["slack"]) == ("sonnet", True, 20)


def test_caller_declares_sensitive_in_cli_and_mcp(replay, capsys):
    _codex(replay, 40, checks=False)
    routing.set_cap("Codex", window="weekly", kind="pace_target", pct=100, slack=20,
                    purpose="check", tier="sonnet", sensitive_only=True, via="app")
    assert cli.main(["route", "advise", "Codex", "--purpose", "check", "--tier", "sonnet", "--sensitive", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "go"
    error, out = replay.call("khipu_route_advise", account="Codex", purpose="check", tier="sonnet", sensitive=True)
    assert not error and out["verdict"] == "go", out
    error, out = replay.call("khipu_route_advise", account="Codex", purpose="check", tier="sonnet")
    assert not error and out["verdict"] == "hold_back", out


def test_check_advice_is_omitted_without_a_check_cap(replay):
    _codex(replay, 24, checks=False)
    view = replay.view()
    entry = next(a for a in view["accounts"] if a["label"] == "Codex")
    assert "check_advice" not in next(a for a in routing.public_view(view, replay.now)["accounts"] if a["label"] == "Codex")
    assert "check_advice" not in routing._public_account(entry, replay.now, view=view)


@pytest.mark.parametrize("tier,sensitive", [(None, False), ("opus", True), ("haiku", False), ("sonnet", True)])
def test_unqualified_codex_check_cap_matches_any_code(replay, tier, sensitive):
    _codex(replay, 24)
    assert routing.advise("Codex", purpose="check", tier=tier, sensitive=sensitive,
                          now=replay.now, environ={})["verdict"] == "go"


def test_a_tier_table_inference_is_not_a_declared_check_tier(replay):
    _codex(replay, 40, checks=False)
    routing.set_cap("Codex", window="weekly", kind="pace_target", pct=100, slack=20,
                    purpose="check", tier="sonnet", sensitive_only=True, via="app")
    view = replay.view()
    view["tier_table"] = {"confirmed": True, "tiers": {"tests": "sonnet"}}
    out = routing.advise("Codex", purpose="check", kind="check", sensitive=True,
                         now=replay.now, view=view, environ={})
    assert out["verdict"] == "hold_back"
    assert out["numbers"]["allowance_pct"] == pytest.approx(21.96, abs=.001)


def test_runner_declarations_preserve_and_compare_check_qualifiers():
    from khipu import routing_scanner, routing_sources

    check = {"kind": "pace_target", "window": "weekly", "pct": 100, "slack": 20,
             "purpose": "check", "tier": "sonnet", "sensitive_only": True}
    assert routing_sources._budget_declaration(check) == check
    accounts = [{"key": "codex|", "label": "Codex", "caps": [{**check, "revision": "r"}]}]
    assert not routing_scanner.compare_runner_caps([{**check, "account": "Codex"}], accounts)
    for field in ("tier", "sensitive_only"):
        declaration = {k: v for k, v in check.items() if k != field}
        flag, = routing_scanner.compare_runner_caps([{**declaration, "account": "Codex"}], accounts)
        assert flag["status"] == "disagrees"
        assert flag["fact"][field] == check[field]


def test_invalid_stored_check_share_fact_is_not_folded(replay):
    _codex(replay, 24, checks=False)
    replay.write(replay.event("cap_set", 100, account="codex|", kind="share", window="weekly",
                              purpose="check", pct=10, revision="invalid"))
    caps = next(a for a in replay.view()["accounts"] if a["label"] == "Codex")["caps"]
    assert all(c["kind"] == "pace_target" for c in caps)
