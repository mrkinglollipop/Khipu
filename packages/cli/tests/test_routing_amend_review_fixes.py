"""Fixes for the cross-family review of route.priority.amend and route.cap.set --until
(jobs/2026-10-26/route-amend-muse): each test failed on c57280fd. Hermetic: a temp data dir
and fake accounts."""
from __future__ import annotations

from datetime import timedelta

import pytest

from khipu import mcp_server, owner_act, routing

pytest_plugins = ("tests.test_routing_acceptance", "tests.test_owner_direction")


@pytest.fixture
def direction_world(world):
    return world


ME = {"kind": "conversation", "harness": "claude_code", "project": "project-a", "session": "s-one"}


def _stored():
    events, _bad = routing.read_events()
    return [e for e in events if e.get("type") == "priority_set"][-1]


def _caps(now=None, label="Codex"):
    return next(a for a in routing.read_view(now)["accounts"] if a["label"] == label).get("caps") or []


# ---- P1: an owner amend never confirms a conversation's pending request -------------------


def test_an_owner_amend_is_refused_while_a_request_waits(direction_world):
    routing.set_priority([{"account": "Codex", "role": "reviews"}, {"account": "Grok", "role": "audits"}],
                         via="app", expect=routing.priority_version())
    base = routing.conversation_view()["priority_owner_revision"]
    routing.request_priority([{"account": "Codex", "role": "reviews"},
                              {"account": "Grok", "role": "INJECTED BY CONVERSATION"}],
                             owner_request="Matt asked", author=ME, expect=routing.priority_version(), base=base)
    with pytest.raises(routing.RouteError, match="confirm or undo"):
        routing.amend_priority("Codex", append="extra", via="app", expect=routing.priority_version())
    held = routing.read_view()["priority"]
    assert held["pending"] is True and held["replaced"]["entries"][1]["role"] == "audits"
    direction_world.say("add to Codex")
    ok, out = owner_act.act("route.priority.amend", {"account": "Codex", "append": "x",
                                                     "expect": routing.priority_version()})
    assert not ok and routing.read_view()["priority"]["pending"] is True


# ---- P1: every other entry is carried over byte for byte, and undo restores the stored line --


def test_amend_keeps_a_future_level_and_other_entries_exactly_as_stored(direction_world):
    now = routing._now()
    level = {"claim": "80% left", "as_of": (now + timedelta(days=1)).isoformat(),
             "until": (now + timedelta(days=2)).isoformat()}
    routing.set_priority([{"account": "Codex", "role": "reviews"},
                          {"account": "Grok", "role": "audits", "level": level, "until": "5d"}], via="app",
                         expect=routing.priority_version())
    before = _stored()["entries"]
    assert before[1].get("level")
    routing.amend_priority("Codex", append="extra", via="app", expect=routing.priority_version())
    after = _stored()["entries"]
    assert after[1] == before[1]
    assert after[0] == dict(before[0], role="reviews extra")


def test_amend_keeps_an_entry_the_view_does_not_show(direction_world):
    routing.set_priority([{"account": "Codex", "role": "reviews"}, {"account": "Grok", "role": "audits", "until": "1h"}],
                         via="app", expect=routing.priority_version())
    later = routing._now() + timedelta(hours=2)
    stored = _stored()["entries"]
    assert [e["label"] for e in routing.read_view(later)["priority"]["entries"]] == ["Codex"]
    routing.amend_priority("Codex", append="extra", via="app", expect=routing.priority_version(), now=later)
    assert _stored()["entries"][1] == stored[1]


def test_undo_of_an_amend_restores_the_stored_line(direction_world):
    now = routing._now()
    level = {"claim": "80% left", "as_of": (now + timedelta(days=1)).isoformat(),
             "until": (now + timedelta(days=2)).isoformat()}
    routing.set_priority([{"account": "Codex", "role": "reviews"},
                          {"account": "Grok", "role": "audits", "level": level}], via="app",
                         expect=routing.priority_version())
    before = _stored()["entries"]
    direction_world.say("add to Codex")
    ok, applied = owner_act.act("route.priority.amend", {"account": "Codex", "append": "extra",
                                                         "expect": routing.priority_version()})
    assert ok, applied
    direction_world.say("undo that")
    ok, undone = owner_act.act("direction.undo", {"id": applied["owner_action"]})
    assert ok, undone
    assert [(e["account"], e["role"], e.get("level")) for e in _stored()["entries"]] == [
        (e["account"], e["role"], e.get("level")) for e in before]


# ---- P2: a cap undo clears the slot and puts back every layer ----------------------------------


def test_undo_of_a_timed_cap_does_not_leave_it_beneath(direction_world):
    now = routing._now()
    routing.set_cap("Codex", window="weekly", kind="share", pct=40, revision="g", via="app", now=now)
    routing.set_cap("Codex", window="weekly", kind="share", pct=50, revision="b",
                    until=(now + timedelta(days=1)).isoformat(), via="app", now=now)
    direction_world.say("loosen the cap for two days")
    ok, applied = owner_act.act("route.cap.set", {"account": "Codex", "window": "weekly",
                                                  "kind": "share", "pct": 70, "until": "2d"})
    assert ok, applied
    direction_world.say("undo that")
    ok, undone = owner_act.act("direction.undo", {"id": applied["owner_action"]})
    assert ok, undone
    assert [c["pct"] for c in _caps(now + timedelta(hours=1))] == [50.0]
    assert [c["pct"] for c in _caps(now + timedelta(days=1, hours=12))] == [40.0]


def test_undo_after_the_previous_caps_end_has_passed_still_works(direction_world, monkeypatch):
    now = routing._now()
    routing.set_cap("Codex", window="weekly", kind="share", pct=40, revision="g", via="app", now=now)
    routing.set_cap("Codex", window="weekly", kind="share", pct=50, revision="b",
                    until=(now + timedelta(hours=1)).isoformat(), via="app", now=now)
    direction_world.say("set the cap to 70")
    ok, applied = owner_act.act("route.cap.set", {"account": "Codex", "window": "weekly", "kind": "share", "pct": 70})
    assert ok, applied
    later = now + timedelta(hours=2)
    monkeypatch.setattr(routing, "_now", lambda: later)
    direction_world.say("undo that")
    ok, undone = owner_act.act("direction.undo", {"id": applied["owner_action"]})
    assert ok, undone
    assert [c["pct"] for c in _caps(later)] == [40.0]


def test_undo_of_a_cap_remove_restores_every_layer(direction_world):
    now = routing._now()
    routing.set_cap("Codex", window="weekly", kind="share", pct=40, revision="g", via="app", now=now)
    routing.set_cap("Codex", window="weekly", kind="share", pct=50, revision="b",
                    until=(now + timedelta(days=1)).isoformat(), via="app", now=now)
    direction_world.say("drop the cap")
    ok, applied = owner_act.act("route.cap.remove", {"account": "Codex", "window": "weekly", "kind": "share"})
    assert ok, applied
    assert _caps(now + timedelta(hours=1)) == []
    direction_world.say("undo that")
    ok, undone = owner_act.act("direction.undo", {"id": applied["owner_action"]})
    assert ok, undone
    assert [c["pct"] for c in _caps(now + timedelta(hours=1))] == [50.0]
    assert [c["pct"] for c in _caps(now + timedelta(days=2))] == [40.0]


# ---- P3: khipu_route_priority reaches request_priority_amend ------------------------------------


def test_khipu_route_priority_takes_one_accounts_amend(direction_world):
    routing.set_priority([{"account": "Codex", "role": "reviews"}, {"account": "Grok", "role": "audits"}],
                         via="app", expect=routing.priority_version())
    before = _stored()["entries"]
    base = routing.conversation_view()["priority_owner_revision"]
    out = mcp_server._tool_route_priority({"account": "Codex", "append": "loosened", "owner_request": "Matt said",
                                           "expect": routing.priority_version(), "base": base})
    assert out["ok"] is True, out
    held = routing.read_view()["priority"]
    assert held["pending"] is True
    assert [e["role"] for e in held["entries"]] == ["reviews loosened", "audits"]
    assert _stored()["entries"][1] == before[1]
    with pytest.raises(ValueError):
        mcp_server._tool_route_priority({"account": "Codex", "append": "x", "entries": [{"account": "Codex"}],
                                         "owner_request": "y", "expect": "p0", "base": base})
