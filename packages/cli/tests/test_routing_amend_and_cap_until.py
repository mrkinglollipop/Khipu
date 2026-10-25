"""``route.priority.amend`` (one account's priority entry) and ``route.cap.set --until``
(a cap that ends by itself). Hermetic: a temp data dir and fake accounts."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from khipu import cli, mcp_server, owner_act, owner_direction, routing

pytest_plugins = ("tests.test_routing_acceptance", "tests.test_owner_direction")


@pytest.fixture
def direction_world(world):
    return world


ME = {"kind": "conversation", "harness": "claude_code", "project": "project-a", "session": "s-one"}


def _entries():
    return [{"account": "Codex", "role": "reviews and checks"},
            {"account": "Grok", "role": "audits", "until": "3d"},
            {"account": "Claude", "role": "orchestration"}]


def _shown():
    return [(e["label"], e["role"], e["until"]) for e in routing.read_view()["priority"]["entries"]]


def _start(direction_world):
    routing.set_priority(_entries(), via="app", expect=routing.priority_version())
    return _shown()


# ---- route.priority.amend ---------------------------------------------------------------------


def test_amend_append_changes_one_entry_and_leaves_the_others_identical(direction_world):
    before = _start(direction_world)
    direction_world.say("loosen Codex's pace until tomorrow")
    ok, out = owner_act.act("route.priority.amend", {"account": "Codex", "append": "15 points over even until 20:30Z.",
                                                     "expect": routing.priority_version()})
    assert ok, out
    after = _shown()
    assert after[0] == ("Codex", "reviews and checks 15 points over even until 20:30Z.", before[0][2])
    assert after[1:] == before[1:]
    held = routing.read_view()["priority"]
    assert held["pending"] is False  # the owner's direction applies at once


def test_amend_role_replaces_that_role_and_can_move_its_end(direction_world):
    before = _start(direction_world)
    direction_world.say("Grok only does audits for a day")
    ok, out = owner_act.act("route.priority.amend", {"account": "Grok", "role": "audits only", "until": "1d",
                                                     "expect": routing.priority_version()})
    assert ok, out
    after = _shown()
    assert after[1][:2] == ("Grok", "audits only")
    assert after[1][2] != before[1][2]
    assert after[0] == before[0] and after[2] == before[2]


def test_amend_is_refused_on_a_stale_expect(direction_world):
    _start(direction_world)
    stale = routing.priority_version()
    routing.set_priority([{"account": "Codex", "role": "moved"}], via="app", expect=stale)
    direction_world.say("add to Codex")
    ok, out = owner_act.act("route.priority.amend", {"account": "Codex", "append": "more", "expect": stale})
    assert not ok
    assert _shown() == [("Codex", "moved", None)]


def test_amend_is_refused_for_an_account_not_in_the_priority(direction_world):
    before = _start(direction_world)
    direction_world.say("add to Claude second")
    ok, out = owner_act.act("route.priority.amend", {"account": "Claude second", "append": "more",
                                                     "expect": routing.priority_version()})
    assert not ok and "not in the priority" in json.dumps(out)
    assert _shown() == before


def test_amend_needs_exactly_one_of_append_and_role(direction_world):
    before = _start(direction_world)
    direction_world.say("change Codex")
    for args in ({}, {"append": "a", "role": "b"}):
        ok, out = owner_act.act("route.priority.amend", {"account": "Codex", "expect": routing.priority_version(), **args})
        assert not ok and out["code"] == "bad_args"
    assert _shown() == before


def test_amend_without_a_priority_is_refused(direction_world):
    direction_world.say("change Codex")
    ok, out = owner_act.act("route.priority.amend", {"account": "Codex", "append": "a",
                                                     "expect": routing.priority_version()})
    assert not ok


def test_a_conversation_amend_stays_unconfirmed_and_keeps_the_others(direction_world):
    before = _start(direction_world)
    base = routing.conversation_view()["priority_owner_revision"]
    out = routing.request_priority_amend("Codex", append="until 20:30Z loosen", owner_request="Matt said loosen Codex",
                                         author=ME, expect=routing.priority_version(), base=base)
    assert out["ok"] is True, out
    held = routing.read_view()["priority"]
    assert held["pending"] is True
    after = _shown()
    assert after[0][1] == "reviews and checks until 20:30Z loosen"
    assert after[1:] == before[1:]
    assert held["replaced"]["entries"][0]["role"] == "reviews and checks"


def test_the_cli_request_path_reaches_the_same_code(direction_world, capsys):
    before = _start(direction_world)
    direction_world.be("s-conv")
    version = routing.priority_version()
    base = routing.conversation_view()["priority_owner_revision"]
    assert cli.main(["route", "priority", "amend", "Grok", "--role", "audits, paused", "--at-owner-request",
                     "Matt asked", "--expect", version, "--base", base]) == 0
    capsys.readouterr()
    held = routing.read_view()["priority"]
    assert held["pending"] is True
    after = _shown()
    assert after[1][1] == "audits, paused" and after[0] == before[0] and after[2] == before[2]


def test_undo_of_an_amend_restores_the_previous_priority(direction_world):
    before = _start(direction_world)
    direction_world.say("add to Codex")
    ok, applied = owner_act.act("route.priority.amend", {"account": "Codex", "append": "extra",
                                                         "expect": routing.priority_version()})
    assert ok, applied
    assert _shown() != before
    direction_world.say("undo that")
    ok, undone = owner_act.act("direction.undo", {"id": applied["owner_action"]})
    assert ok, undone
    assert _shown() == before


def test_the_cli_and_mcp_surfaces_both_reach_amend(direction_world):
    _start(direction_world)
    direction_world.say("add to Codex")
    code, out = direction_world.run(["route", "priority", "amend", "Codex", "--append", "via cli",
                                     "--expect", routing.priority_version()], True)
    assert code == 0, out
    assert _shown()[0][1] == "reviews and checks via cli"
    direction_world.be("s-owner")
    reply = mcp_server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "khipu_owner_act", "arguments": {"action": "route.priority.amend", "args": {
            "account": "Codex", "append": "via mcp", "expect": routing.priority_version()}}}})["result"]
    assert not reply["isError"], reply
    assert _shown()[0][1] == "reviews and checks via cli via mcp"


# ---- route.cap.set --until --------------------------------------------------------------------


def _caps(now=None, label="Codex"):
    return next(a for a in routing.read_view(now)["accounts"] if a["label"] == label).get("caps") or []


def test_a_cap_with_an_end_applies_until_then_and_the_cap_it_replaced_returns(direction_world):
    routing.set_cap("Codex", window="weekly", kind="pace_target", pct=65, slack=5, revision="base", via="app")
    now = routing._now()
    out = routing.set_cap("Codex", window="weekly", kind="pace_target", pct=65, slack=20, revision="loose",
                          until=(now + timedelta(hours=5)).isoformat(), via="app", now=now)
    assert out["ok"] is True or out.get("account")
    during = _caps(now + timedelta(hours=1))
    assert [(c["slack"], c["until"] is not None) for c in during] == [(20.0, True)]
    status = next(a for a in routing.read_view(now + timedelta(hours=1))["accounts"] if a["label"] == "Codex")
    assert routing.cap_statuses(routing.read_view(now + timedelta(hours=1)), status,
                                now + timedelta(hours=1))[0]["until"] == during[0]["until"]
    after = _caps(now + timedelta(hours=6))
    assert [(c["slack"], "until" in c) for c in after] == [(5.0, False)]


def test_an_ended_cap_with_nothing_beneath_it_is_absent(direction_world):
    now = routing._now()
    routing.set_cap("Codex", window="weekly", kind="share", pct=40, revision="s1",
                    until=(now + timedelta(hours=2)).isoformat(), via="app", now=now)
    assert [c["pct"] for c in _caps(now + timedelta(minutes=30))] == [40.0]
    assert _caps(now + timedelta(hours=3)) == []
    view = routing.read_view(now + timedelta(hours=3))
    assert not routing.cap_statuses(view, next(a for a in view["accounts"] if a["label"] == "Codex"),
                                    now + timedelta(hours=3))


def test_the_view_refolds_at_a_caps_end(direction_world):
    now = routing._now()
    routing.set_cap("Codex", window="weekly", kind="share", pct=40, revision="s1",
                    until=(now + timedelta(hours=2)).isoformat(), via="app", now=now)
    stamp = routing.read_view(now)["valid_until"]
    assert stamp is not None and stamp <= (now + timedelta(hours=2)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def test_caps_without_an_end_are_as_before(direction_world):
    routing.set_cap("Codex", window="weekly", kind="share", pct=40, revision="s1", via="app")
    far = routing._now() + timedelta(days=300)
    caps = _caps(far)
    assert [c["pct"] for c in caps] == [40.0] and "until" not in caps[0]


def test_an_end_in_the_past_is_refused(direction_world):
    with pytest.raises(routing.RouteError):
        routing.set_cap("Codex", window="weekly", kind="share", pct=40, revision="s1",
                        until=(routing._now() - timedelta(hours=1)).isoformat(), via="app")
    assert _caps() == []


def test_the_cli_and_owner_act_set_a_cap_with_an_end(direction_world):
    direction_world.say("loosen Codex until tomorrow")
    code, out = direction_world.run(["route", "cap", "set", "Codex", "--window", "weekly", "--kind", "pace_target",
                                     "--pct", "65", "--slack", "20", "--until", "1d"], True)
    assert code == 0, out
    assert _caps()[0]["until"]
    ok, out = owner_act.act("route.cap.set", {"account": "Codex", "window": "weekly", "kind": "share", "pct": 30,
                                              "until": "2d"})
    assert ok, out
    assert {c["kind"] for c in _caps() if c.get("until")} == {"pace_target", "share"}


def test_a_cap_remove_takes_no_end(direction_world):
    direction_world.say("drop the cap")
    ok, out = owner_act.act("route.cap.remove", {"account": "Codex", "kind": "share", "window": "weekly",
                                                 "until": "1d"})
    assert not ok and out["code"] == "bad_args"


def test_undo_of_a_capped_end_returns_the_cap_that_was_there(direction_world):
    routing.set_cap("Codex", window="weekly", kind="share", pct=50, revision="base", via="app")
    direction_world.say("loosen the cap for a day")
    ok, applied = owner_act.act("route.cap.set", {"account": "Codex", "window": "weekly", "kind": "share", "pct": 70,
                                                  "until": "1d"})
    assert ok, applied
    direction_world.say("undo that")
    ok, undone = owner_act.act("direction.undo", {"id": applied["owner_action"]})
    assert ok, undone
    assert [(c["pct"], "until" in c) for c in _caps()] == [(50.0, False)]


def test_the_owner_direction_gate_is_the_same_for_amend(direction_world):
    assert "route.priority.amend" in owner_direction.ACTIONS
    assert "route.priority.amend" in owner_act.SPECS
