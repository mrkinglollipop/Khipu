"""Round 3 of the routing-exceptions review (jobs/2026-10-26/route-exceptions-sol): an undo puts
back exactly the prior state, and never turns a conversation's write into the owner's. Each test
failed on d1b0baba. Hermetic: a temp data dir and fake accounts."""
from __future__ import annotations

from datetime import timedelta

import pytest

from khipu import accounts, owner_act, routing

pytest_plugins = ("tests.test_routing_acceptance", "tests.test_owner_direction")


@pytest.fixture
def direction_world(tmp_path, monkeypatch, khipu_app):
    # test_owner_direction's world, built here by its own parts: a module
    # collected earlier (test_routing_s9a_fix*) registers another ``world``
    # that would win by name in a whole-directory run.
    import json
    import sys

    from khipu import owner_turns
    from tests import test_owner_direction as od

    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": od.ACCOUNTS}))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    for name in od._SESSION_VARS:
        monkeypatch.delenv(name, raising=False)
    khipu_app.as_shell("/opt/harness/claude")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    owner_turns.write_settings({"enabled": True})
    w = od.World(tmp_path, monkeypatch)
    w.be(od.OWNER)
    return w


ME = {"kind": "conversation", "harness": "claude_code", "project": "project-a", "session": "s-one"}


def _owner_priority():
    routing.set_priority([{"account": "Codex", "role": "reviews"}, {"account": "Grok", "role": "audits"}],
                         via="app", expect=routing.priority_version())


def _request():
    base = routing.conversation_view()["priority_owner_revision"]
    routing.request_priority([{"account": "Codex", "role": "CONVERSATION WORDS"}], owner_request="Matt asked",
                             author=ME, expect=routing.priority_version(), base=base)


def _act_and_undo(world, action, args):
    world.say(f"do {action}")
    ok, applied = owner_act.act(action, {"expect": routing.priority_version(), **args})
    assert ok, applied
    world.say("undo that")
    ok, undone = owner_act.act("direction.undo", {"id": applied["owner_action"]})
    assert ok, undone


def _assert_request_back():
    held = routing.read_view()["priority"]
    assert held["pending"] is True
    assert held["pending_author"]["kind"] == "conversation" and held["owner_request"] == "Matt asked"
    assert [e["role"] for e in held["entries"]] == ["CONVERSATION WORDS"]
    assert [(e["label"], e["role"]) for e in held["replaced"]["entries"]] == [("Codex", "reviews"), ("Grok", "audits")]


# ---- P1: undo of an owner change over a pending conversation priority ----------------------------


@pytest.mark.parametrize("action,args", [
    ("route.priority.set", {"entries": [{"account": "Grok", "role": "owner's own"}]}),
    ("route.priority.clear", {}),
    ("route.priority.undo", {}),
    ("route.priority.confirm", {}),
])
def test_undo_puts_a_pending_request_back_pending_and_the_conversations(direction_world, action, args):
    _owner_priority()
    _request()
    _act_and_undo(direction_world, action, args)
    _assert_request_back()


def test_undo_of_a_change_over_a_first_request_leaves_no_owner_priority_beneath(direction_world):
    _request()
    _act_and_undo(direction_world, "route.priority.clear", {})
    held = routing.read_view()["priority"]
    assert held["pending"] is True and held["replaced"] is None


def test_undo_of_an_owner_priority_stays_the_owners(direction_world):
    _owner_priority()
    _act_and_undo(direction_world, "route.priority.clear", {})
    held = routing.read_view()["priority"]
    assert held["pending"] is False and [e["role"] for e in held["entries"]] == ["reviews", "audits"]


# ---- P2: Stop tracking chooses set or clear by the stored rows kept ------------------------------


def test_stopping_the_last_live_account_keeps_another_accounts_stored_row(direction_world):
    now = routing._now()
    level = {"claim": "80% left", "as_of": (now + timedelta(days=1)).isoformat(),
             "until": (now + timedelta(days=2)).isoformat()}
    routing.set_priority([{"account": "Codex", "role": "reviews"},
                          {"account": "Grok", "role": "audits", "until": "1h", "level": level}],
                         via="app", expect=routing.priority_version())
    later = now + timedelta(hours=2)
    view = routing.read_view(later)
    assert [e["label"] for e in view["priority"]["entries"]] == ["Codex"]
    codex = next(a["key"] for a in view["accounts"] if a["label"] == "Codex")
    events, _done = accounts._stop_events(view, [codex], keep_priority=False, keep_pause=False, end_holds=[])
    written = [e for e in events if e["type"] in ("priority_set", "priority_clear")]
    assert [e["type"] for e in written] == ["priority_set"]
    assert [e["label"] for e in written[0]["entries"]] == ["Grok"] and written[0]["entries"][0]["level"]


# ---- sweep: holds ---------------------------------------------------------------------------------


def test_undo_of_ending_a_conversations_hold_brings_it_back_as_the_conversations(direction_world):
    hold = routing.add_hold("wait for Grok", reason="keep", scope="account", account="Grok", author=ME, via="mcp")
    direction_world.say("end that hold")
    ok, applied = owner_act.act("route.hold.end", {"hold": hold["entity"]})
    assert ok, applied
    direction_world.say("undo that")
    ok, undone = owner_act.act("direction.undo", {"id": applied["owner_action"]})
    assert ok, undone
    (back,) = [h for h in routing.read_view()["holds"] if h["text"] == "wait for Grok"]
    assert back["owner"] is False and back["written_by"]["kind"] == "conversation"


# ---- sweep: rules ---------------------------------------------------------------------------------


def _pending_edit(world):
    routing.add_rule("Owner's rule words", via="app")
    shown = routing.conversation_view()
    routing.request_rule("CONVERSATION RULE WORDS", n=1, owner_request="the owner asked", author=ME, via="mcp",
                         base=shown["rules"][0]["owner_revision"])
    rule = routing.read_view()["rules"][0]
    assert rule["pending"] is True
    return rule


def _assert_rule_request_back():
    rule = next(r for r in routing.read_view()["rules"] if r["n"] == 1)
    assert rule["pending"] is True and rule["pending_kind"] == "changed"
    assert rule["text"] == "CONVERSATION RULE WORDS" and rule["prior_text"] == "Owner's rule words"
    assert rule["pending_author"]["kind"] == "conversation"


@pytest.mark.parametrize("action,args", [
    ("route.rule.undo", {"rule": "1"}),
    ("route.rule.edit", {"rule": "1", "text": "Owner's newer words"}),
    ("route.rule.retire", {"rule": "1"}),
    ("route.rule.dismiss", {"rule": "1"}),
])
def test_undo_over_a_pending_rule_edit_puts_it_back_as_the_conversations(direction_world, action, args):
    _pending_edit(direction_world)
    direction_world.say(f"do {action}")
    ok, applied = owner_act.act(action, args)
    assert ok, applied
    direction_world.say("undo that")
    ok, undone = owner_act.act("direction.undo", {"id": applied["owner_action"]})
    assert ok, undone
    _assert_rule_request_back()


def test_undo_never_brings_back_a_dismissed_proposed_rule_as_the_owners(direction_world):
    routing.request_rule("A CONVERSATION'S NEW RULE", owner_request="the owner asked", author=ME, via="mcp")
    direction_world.say("dismiss it")
    ok, applied = owner_act.act("route.rule.dismiss", {"rule": "1"})
    assert ok, applied
    direction_world.say("undo that")
    ok, undone = owner_act.act("direction.undo", {"id": applied["owner_action"]})
    assert not ok
    assert not any(r["text"] == "A CONVERSATION'S NEW RULE" for r in routing.read_view()["rules"])
