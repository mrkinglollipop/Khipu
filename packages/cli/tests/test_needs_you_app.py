"""Needs you from the app (slice B5): the app reads the one list and answers an
item Yes or No with no terminal step, through the ``--from-app`` owner path.

Hermetic: the fixtures of ``test_t3_owner_confirm`` (temp data dir, capture
home, synthetic T3 database) and ``khipu_app`` (the faked process tree the app
check walks). Hub reads and hub commands are stubbed. One test per kind of
answer: confirm, undo, decline-only, an ask, a sign-in, a permanent item, a
request another Mac holds; plus what must not change: a caller that is not
the app is refused, and the MCP list has none of the app's fields."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest

from khipu import cli, needs_you, needs_you_app, owner_act, owner_direction, routing, usage
from tests.test_owner_needs_you import (OTHER, _held_pause, _priority_request, _rule_added, _rule_changed,
                                        _setting_request)
from tests.test_t3_owner_confirm import _be
from tests.test_owner_priority_pending import _ask_in_t3
from tests.test_t3_owner_confirm import _ENV_VARS, world

assert world  # the fixture, imported for pytest
APP_FIELDS = {"from_words", "mac", "ago", "expect"}
RECEIPT = {"text", "action_id", "undoable", "saved_on", "not_reached"}


def _app(world, khipu_app) -> None:
    """The app's own process: started by Khipu.app, no conversation in its environment."""
    for name in _ENV_VARS:
        world["mp"].delenv(name, raising=False)
    khipu_app.as_app()


def _cli(capsys, *argv: str) -> tuple[int, dict]:
    args = cli.build_parser().parse_args(["needs-you", *argv])
    code = args.func(args)
    return code, json.loads(capsys.readouterr().out)


_READ = object()


def _shown_pin(key: str):
    """The pin the app's read gives the item, as the app would send it back."""
    items = needs_you_app.read(key).get("items") or []
    return items[0]["expect"] if items else None


def _answer(capsys, key: str, said: str, expect=_READ) -> tuple[int, dict]:
    pin = _shown_pin(key) if expect is _READ else expect
    return _cli(capsys, "--from-app", "--json", "--answer", key, said, *([f"--expect={pin}"] if pin is not None else []))


def _request(request_id: str) -> dict:
    return next(r for r in owner_direction._read_requests(owner_direction.requests_path()) if r["id"] == request_id)


# ---- the read ---------------------------------------------------------------------------------


def test_the_app_read_adds_where_each_item_came_from_and_changes_nothing(world, khipu_app, capsys):
    _setting_request(world)
    _app(world, khipu_app)
    before = owner_direction.requests_path().read_bytes() if owner_direction.requests_path().exists() else None
    code, out = _cli(capsys, "--json", "--from-app")
    assert code == 0 and out["ok"] is True
    (item,) = out["items"]
    assert APP_FIELDS <= set(item)
    assert item["mac"] == needs_you_app.this_mac() == out["this_mac"]
    assert item["from_words"].startswith("From a ") and item["from_words"].endswith(f" on {item['mac']}.")
    assert item["ago"] == item["asked_by"]["when"]
    after = owner_direction.requests_path().read_bytes() if owner_direction.requests_path().exists() else None
    assert before == after


def test_the_mcp_list_and_the_plain_cli_list_have_none_of_the_apps_fields(world, capsys):
    _setting_request(world)
    mcp = needs_you.waiting(record_read=False)
    assert mcp["items"] and not APP_FIELDS & set(mcp["items"][0])
    assert "this_mac" not in mcp and "last_answer" not in mcp
    code, out = _cli(capsys, "--json")
    assert code == 0 and not APP_FIELDS & set(out["items"][0])


def test_this_mac_is_the_name_the_owner_gave_it(world):
    from khipu.config import mutate_config

    mine = "0123456789abcdef0123456789abcdef"
    routing.routing_dir().mkdir(parents=True, exist_ok=True)
    (routing.routing_dir() / "mac-id").write_text(mine, encoding="ascii")
    assert needs_you_app.this_mac() == "this Mac"
    with mutate_config() as config:
        config["routing_mac_names"] = {mine: "M5"}
    assert needs_you_app.this_mac() == "M5"


def test_with_nothing_waiting_the_read_carries_the_last_answer(world, khipu_app, capsys):
    _setting_request(world)
    _app(world, khipu_app)
    code, done = _answer(capsys, "setting:routing_replication_live", "yes")
    assert code == 0, done
    code, out = _cli(capsys, "--json", "--from-app")
    assert code == 0 and out["total"] == 0
    last = out["last_answer"]
    assert last["action_id"] == done["receipt"]["action_id"]
    assert last["text"].startswith("You said yes:") and last["undoable"] is True


# ---- an answer, by kind -----------------------------------------------------------------------


def test_yes_confirms_with_a_receipt_and_its_undo_works(world, khipu_app, capsys):
    _setting_request(world)
    _app(world, khipu_app)
    code, out = _answer(capsys, "setting:routing_replication_live", "yes")
    assert code == 0, out
    receipt = out["receipt"]
    assert set(receipt) == RECEIPT
    assert receipt["undoable"] is True and receipt["saved_on"] == [needs_you_app.this_mac()]
    assert receipt["not_reached"] == [] and receipt["text"].startswith("You said yes.")
    status = routing.settings_status()["routing_replication_live"]
    assert status["value"] is True and not status.get("pending")
    line = owner_direction._find(receipt["action_id"])
    assert line["action"] == "config.answer_request.confirm" and line["state"] == "applied"
    # The receipt's Undo is the app's existing one, on exactly that line.
    args = cli.build_parser().parse_args(["owner-direction", "undo", "--from-app", receipt["action_id"]])
    assert args.func(args) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert routing.settings_status()["routing_replication_live"]["value"] is False


def test_no_undoes_a_conversations_change(world, khipu_app, capsys):
    _setting_request(world)
    _app(world, khipu_app)
    code, out = _answer(capsys, "setting:routing_replication_live", "no")
    assert code == 0, out
    assert out["receipt"]["text"].startswith("You said no.")
    assert owner_direction._find(out["receipt"]["action_id"])["action"] == "config.answer_request.undo"
    status = routing.settings_status()["routing_replication_live"]
    assert status["value"] is False and not status.get("pending")


def test_a_proposed_rule_is_confirmed_and_another_dropped(world, khipu_app, capsys):
    _rule_added(world)
    _rule_added(world, "Docs go to Haiku")
    keys = [i["key"] for i in needs_you.waiting(kind="rule", record_read=False)["items"]]
    _app(world, khipu_app)
    assert _answer(capsys, keys[0], "yes")[0] == 0
    assert _answer(capsys, keys[1], "no")[0] == 0
    rules = {r["text"]: r for r in routing.read_view()["rules"]}
    assert not rules["Never send client work to Grok"].get("pending")
    assert "Docs go to Haiku" not in rules or rules["Docs go to Haiku"].get("pending") is not True


def test_a_conversations_priority_request_is_confirmed(world, khipu_app, capsys):
    _priority_request(world)
    _app(world, khipu_app)
    code, out = _answer(capsys, "priority", "yes")
    assert code == 0, out
    assert not (routing.read_view().get("priority") or {}).get("pending")
    assert out["receipt"]["action_id"] and out["receipt"]["undoable"] is True


def test_a_held_request_is_answered_yes_and_another_no(world, khipu_app, capsys):
    first = _held_pause(world)
    _app(world, khipu_app)
    code, out = _answer(capsys, f"held:{first}", "no")
    assert code == 0, out
    assert out["declined"] is True and _request(first)["state"] == "declined"
    line = owner_direction._find(out["receipt"]["action_id"])
    assert line["action"] == owner_direction.DECLINE_ACTION and line["auth"] == "app"
    assert out["receipt"]["undoable"] is False and "Declined" in out["receipt"]["text"]
    second = _held_pause(world)
    _app(world, khipu_app)
    code, out = _answer(capsys, f"held:{second}", "yes")
    assert code == 0, out
    assert _request(second)["state"] == "applied"
    line = owner_direction._find(out["receipt"]["action_id"])
    assert line["action"] == "route.pause" and line["state"] == "applied"
    assert out["receipt"]["undoable"] is True


def test_a_decline_only_item_refuses_yes_and_takes_no(world, khipu_app, capsys, monkeypatch):
    end = time.time() + 60
    until = datetime.fromtimestamp(end, timezone.utc).isoformat()
    ok, held = _ask_in_t3(world, "direction.request", {"action": "route.priority.set", "args": {
        "entries": ["Codex"], "expect": routing.priority_version(), "until": until}})
    assert ok, held
    monkeypatch.setattr(owner_direction, "_clock", lambda: end + 1)
    _app(world, khipu_app)
    code, out = _answer(capsys, "held:" + held["request"], "yes")
    assert code == 2 and out["code"] == "decline_only"
    assert _request(held["request"])["state"] == "pending"
    code, out = _answer(capsys, "held:" + held["request"], "no")
    assert code == 0, out
    assert _request(held["request"])["state"] == "declined"


def test_an_ask_is_answered_in_its_conversation_never_by_the_app(world, khipu_app, capsys, monkeypatch):
    ask = needs_you._item("ask", "ask:7", "Should the release wait for Friday?", by={"kind": "conversation"},
                          at=routing._iso(routing._now()), do="The conversation that hears your answer closes this ask.")
    monkeypatch.setattr(routing, "_hub_configured", lambda: True)
    monkeypatch.setattr(needs_you, "_hub_items", lambda: [ask])
    ran = []
    monkeypatch.setattr(owner_act, "settle_from_app", lambda *a: ran.append(a))
    _app(world, khipu_app)
    for said in ("yes", "no"):
        code, out = _answer(capsys, "ask:7", said)
        assert code == 2 and out["code"] == "no_action" and "conversation" in out["error"]
    assert ran == []


def test_a_signin_returns_the_account_and_changes_nothing(world, khipu_app, capsys, monkeypatch):
    monkeypatch.setattr(usage, "_load_cache", lambda: {"claude|": {"ok": False, "error": "not_signed_in"}})
    ran = []
    monkeypatch.setattr(owner_act, "settle_from_app", lambda *a: ran.append(a))
    _app(world, khipu_app)
    code, out = _answer(capsys, "plan:Claude", "yes")
    assert code == 0, out
    assert out["signin_account"] == {"label": "Claude", "harness": "claude", "home": None}
    assert out["applied"] is False
    code, out = _answer(capsys, "plan:Claude", "no")
    assert code == 2 and out["code"] == "no_action"
    assert ran == []


def test_a_permanent_item_is_answered_alone_through_the_hold_and_replay(world, khipu_app, capsys, monkeypatch):
    """Class C: one key per answer, so it is never settled with another item.
    The hub command is stubbed; the hold, the app's proof and the replay are real."""
    monkeypatch.setattr(owner_act, "_forget_list", lambda: {
        "strings": ["forgotten-a"], "ids": [{"id": 41, "summary": "a talk about the roof"}], "at": "2026-11-01T00:00:00Z"})
    ran = []
    monkeypatch.setattr(owner_act, "_run", lambda act, full: (ran.append((act, full)), (True, {"ok": True}))[1])
    _app(world, khipu_app)
    code, shown = _cli(capsys, "--json", "--from-app")
    permanent = [i for i in shown["items"] if i.get("permanent")]
    assert {i["kind"] for i in permanent} == {"forget_send", "forget_id"}
    code, out = _answer(capsys, "forget_send", "yes")
    assert code == 0, out
    # A forget runs in the background (``owner_background``); the receipt says it started.
    assert out["started"] is True and out["receipt"]["text"]
    from khipu import owner_background

    owner_background.run(out["request"])
    assert [a for a, _ in ran] == ["forget.send"] and "--owner-direction" in ran[0][1]
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["needs-you", "--from-app", "--answer", "forget_send", "forget:41", "yes"])


def test_a_request_another_mac_holds_is_sent_there(world, khipu_app, capsys, monkeypatch):
    first = _held_pause(world)
    # Make it a mirror of a request the M1 holds.
    path = owner_direction.requests_path()
    state = json.loads(path.read_text())
    for row in state["requests"]:
        if row["id"] == first:
            row["remote"] = {"mac": "m1-id", "request": "req-remote", "sig": "s" * 16}
    path.write_text(json.dumps(state))
    from khipu import routing_replication

    monkeypatch.setattr(routing_replication, "mac_label", lambda mac: "M1")
    sent = []
    monkeypatch.setattr(routing_replication, "answer_remote", lambda request, stamp, answer: sent.append(answer) or True)
    _app(world, khipu_app)
    code, out = _answer(capsys, f"held:{first}", "yes")
    assert code == 0, out
    assert sent == ["confirm"] and out["sent"] is True
    assert out["receipt"]["not_reached"] == ["M1"] and out["receipt"]["action_id"] is None


# ---- what must not change -----------------------------------------------------------------------


@pytest.mark.parametrize("between", [(), ("/opt/codex",), ("/opt/harness/claude",)])
def test_a_caller_that_is_not_the_app_is_refused_and_nothing_changes(world, khipu_app, capsys, between):
    _setting_request(world)
    for name in _ENV_VARS:
        world["mp"].delenv(name, raising=False)
    khipu_app.as_shell(*between)
    code, out = _answer(capsys, "setting:routing_replication_live", "yes")
    assert code == 2 and out["code"] == "owner_only"
    assert routing.settings_status()["routing_replication_live"]["pending"]


def test_a_dispatched_job_started_from_a_shell_is_refused(world, khipu_app, capsys):
    _setting_request(world)
    khipu_app.as_shell("/opt/harness/claude")
    world["mp"].setenv("CLAUDE_DISPATCHED_LANE", "lane-1")
    code, out = _answer(capsys, "setting:routing_replication_live", "yes")
    assert code == 2 and out["code"] == "owner_only"
    assert routing.settings_status()["routing_replication_live"]["pending"]


def test_answer_without_from_app_is_refused_even_from_the_app(world, khipu_app, capsys):
    _setting_request(world)
    _app(world, khipu_app)
    code, out = _cli(capsys, "--json", "--answer", "setting:routing_replication_live", "yes")
    assert code == 2 and out["code"] == "owner_only"
    assert routing.settings_status()["routing_replication_live"]["pending"]


def test_the_app_never_answers_through_the_terminal_flags(world, khipu_app, capsys):
    _setting_request(world)
    _app(world, khipu_app)
    code, out = _cli(capsys, "--json", "--from-app", "--yes", "setting:routing_replication_live")
    assert code == 2 and out["code"] == "bad_args"
    assert routing.settings_status()["routing_replication_live"]["pending"]


def test_an_item_that_is_gone_is_refused(world, khipu_app, capsys):
    _app(world, khipu_app)
    code, out = _answer(capsys, "setting:routing_replication_live", "yes")
    assert code == 2 and out["code"] == "not_found"
    code, out = _answer(capsys, "priority", "maybe")
    assert code == 2 and out["code"] == "bad_args"


# ---- the pin the app read ---------------------------------------------------------------------


def test_the_read_gives_each_item_the_pin_its_own_actions_carry(world, khipu_app, capsys, monkeypatch):
    _setting_request(world)
    first = _held_pause(world)
    monkeypatch.setattr(usage, "_load_cache", lambda: {"claude|": {"ok": False, "error": "not_signed_in"}})
    _app(world, khipu_app)
    code, out = _cli(capsys, "--json", "--from-app")
    by = {i["key"]: i for i in out["items"]}
    setting = by["setting:routing_replication_live"]
    assert setting["expect"] == setting["settle"]["confirm"]["args"]["expect"]
    assert setting["expect"].startswith("sr-")
    assert by[f"held:{first}"]["expect"] is None and by["plan:Claude"]["expect"] is None


def test_a_matching_pin_settles(world, khipu_app, capsys):
    _setting_request(world)
    pin = _shown_pin("setting:routing_replication_live")
    _app(world, khipu_app)
    code, out = _answer(capsys, "setting:routing_replication_live", "yes", expect=pin)
    assert code == 0, out
    assert routing.settings_status()["routing_replication_live"]["value"] is True


def test_a_stale_pin_changes_nothing_and_says_changed(world, khipu_app, capsys):
    n = _rule_changed(world)
    key = next(i["key"] for i in needs_you.waiting(kind="rule", record_read=False)["items"])
    shown = _shown_pin(key)
    # A conversation proposes newer words after the app showed the first proposal.
    _be(world, OTHER)
    rule = next(r for r in routing.conversation_view()["rules"] if r["n"] == n)
    routing.request_rule("Docs go to Opus", n=n, owner_request="again", author=routing.conversation_author(), via="mcp",
                         base=rule["owner_revision"])
    assert _shown_pin(key) != shown
    log = len(owner_direction.actions())
    _app(world, khipu_app)
    for said in ("yes", "no"):
        code, out = _answer(capsys, key, said, expect=shown)
        assert code == 2 and out["code"] == "changed" and "changed since you saw it" in out["error"]
    rule = next(r for r in routing.read_view()["rules"] if r["n"] == n)
    assert rule["pending"] is True and rule["text"] == "Docs go to Opus"
    assert len(owner_direction.actions()) == log


def test_a_missing_pin_changes_nothing_and_says_changed(world, khipu_app, capsys):
    _rule_added(world)
    (key,) = [i["key"] for i in needs_you.waiting(kind="rule", record_read=False)["items"]]
    _app(world, khipu_app)
    code, out = _answer(capsys, key, "yes", expect=None)
    assert code == 2 and out["code"] == "changed"
    assert any(r.get("pending") for r in routing.read_view()["rules"])


def test_an_item_with_no_pin_still_answers_without_one(world, khipu_app, capsys):
    first = _held_pause(world)
    assert _shown_pin(f"held:{first}") is None
    _app(world, khipu_app)
    code, out = _answer(capsys, f"held:{first}", "no", expect=None)
    assert code == 0, out
    assert _request(first)["state"] == "declined"


def test_the_terminal_path_and_the_mcp_list_have_no_pin_field(world):
    _setting_request(world)
    item = needs_you.waiting(record_read=False)["items"][0]
    assert "expect" not in item
