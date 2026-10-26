"""Fixes for the second cross-family review (jobs/2026-10-26/route-exceptions-muse): each test
failed on af8340d3. Hermetic: a temp data dir and fake accounts."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from khipu import accounts, owner_act, routing

pytest_plugins = ("tests.test_routing_acceptance", "tests.test_owner_direction")


@pytest.fixture
def direction_world(world):
    return world


ME = {"kind": "conversation", "harness": "claude_code", "project": "project-a", "session": "s-one"}


def _stored():
    events, _bad = routing.read_events()
    return [e for e in events if e.get("type") in ("priority_set", "priority_confirm")][-1]["entries"]


def _future_level():
    now = routing._now()
    return {"claim": "80% left", "as_of": (now + timedelta(days=1)).isoformat(),
            "until": (now + timedelta(days=2)).isoformat()}


def _with_level():
    routing.set_priority([{"account": "Codex", "role": "reviews"},
                          {"account": "Grok", "role": "audits", "level": _future_level()}],
                         via="app", expect=routing.priority_version())
    return _stored()


def _exception(text="words", id_="xsame", by="owner", hours=2):
    now = routing._now()
    return {"id": id_, "text": text, "until": (now + timedelta(hours=hours)).isoformat(), "at": now.isoformat(),
            "by": by, "source": "app"}


# ---- P1: confirm writes the stored line ----------------------------------------------------------


def test_confirm_keeps_entries_the_view_leaves_out(direction_world):
    grok = _with_level()[1]
    base = routing.conversation_view()["priority_owner_revision"]
    routing.request_priority_amend("Codex", exception="loosen", until="2h", owner_request="Matt said", author=ME,
                                   expect=routing.priority_version(), base=base)
    routing.confirm_priority(via="app", expect=routing.priority_version())
    assert routing.read_view()["priority"]["pending"] is False
    stored = _stored()
    assert stored[1] == grok
    assert [x["text"] for x in stored[0]["exceptions"]] == ["loosen"]


# ---- P1: caller-supplied exceptions --------------------------------------------------------------


def test_a_conversation_cannot_write_an_owners_exception_or_choose_its_id(direction_world):
    routing.set_priority([{"account": "Codex", "role": "reviews"}], via="app", expect=routing.priority_version())
    base = routing.conversation_view()["priority_owner_revision"]
    routing.request_priority([{"account": "Codex", "role": "reviews", "exceptions": [_exception()]}],
                             owner_request="Matt asked", author=ME, expect=routing.priority_version(), base=base)
    made = _stored()[0]["exceptions"][0]
    assert made["by"] != "owner" and made["id"] != "xsame" and made["source"] == "Matt asked"


def test_an_exception_over_the_size_cap_is_refused_for_every_writer(direction_world):
    routing.set_priority([{"account": "Codex", "role": "reviews"}], via="app", expect=routing.priority_version())
    long = [_exception(text="x " * 400)]
    with pytest.raises(routing.RouteError, match="at most 500"):
        routing.set_priority([{"account": "Codex", "exceptions": long}], via="app", expect=routing.priority_version())
    base = routing.conversation_view()["priority_owner_revision"]
    with pytest.raises(routing.RouteError, match="at most 500"):
        routing.request_priority([{"account": "Codex", "exceptions": long}], owner_request="Matt asked", author=ME,
                                 expect=routing.priority_version(), base=base)
    assert "exceptions" not in _stored()[0]


# ---- P2: the stored line stays internal ----------------------------------------------------------


def test_route_json_does_not_carry_the_stored_line(direction_world):
    routing.set_priority([{"account": "Codex", "role": "reviews", "exceptions": [_exception("secret ended words",
                                                                                            hours=1)]}],
                         via="app", expect=routing.priority_version())
    later = routing._now() + timedelta(hours=3)
    view = routing.read_view(later)
    public = routing.public_view(view, later)
    assert "priority_stored" not in json.dumps(public)
    # The live priority leaves the ended exception out; the change history
    # keeps it, as the design says ended exceptions stay readable there.
    assert "secret ended words" not in json.dumps(public["priority"])


# ---- P2: every priority undo restores the stored line --------------------------------------------


def test_undo_of_a_set_is_not_refused_after_an_entrys_end_passed(direction_world, monkeypatch):
    routing.set_priority([{"account": "Codex", "role": "reviews"}, {"account": "Grok", "role": "audits", "until": "1h"}],
                         via="app", expect=routing.priority_version())
    before = _stored()
    direction_world.say("set the priority to Codex alone")
    ok, applied = owner_act.act("route.priority.set", {"entries": [{"account": "Codex", "role": "only"}],
                                                       "expect": routing.priority_version()})
    assert ok, applied
    later = routing._now() + timedelta(hours=2)
    monkeypatch.setattr(routing, "_now", lambda: later)
    direction_world.say("undo that")
    ok, undone = owner_act.act("direction.undo", {"id": applied["owner_action"]})
    assert ok, undone
    assert [(e["account"], e["role"], e["until"]) for e in _stored()] == [
        (e["account"], e["role"], e["until"]) for e in before]


def test_undo_of_a_clear_restores_a_level_not_yet_started(direction_world):
    before = _with_level()
    direction_world.say("clear the priority")
    ok, applied = owner_act.act("route.priority.clear", {"expect": routing.priority_version()})
    assert ok, applied
    direction_world.say("undo that")
    ok, undone = owner_act.act("direction.undo", {"id": applied["owner_action"]})
    assert ok, undone
    assert [e.get("level") for e in _stored()] == [e.get("level") for e in before]


def test_undo_of_a_confirm_restores_the_stored_priority_it_replaced(direction_world):
    before = _with_level()
    base = routing.conversation_view()["priority_owner_revision"]
    routing.request_priority([{"account": "Codex", "role": "something else"}], owner_request="Matt asked",
                             author=ME, expect=routing.priority_version(), base=base)
    direction_world.say("confirm it")
    ok, applied = owner_act.act("route.priority.confirm", {"expect": routing.priority_version()})
    assert ok, applied
    direction_world.say("undo that")
    ok, undone = owner_act.act("direction.undo", {"id": applied["owner_action"]})
    assert ok, undone
    assert [(e["account"], e["role"], e.get("level")) for e in _stored()] == [
        (e["account"], e["role"], e.get("level")) for e in before]


# ---- P3: exception ids are unique ----------------------------------------------------------------


def test_repeated_exception_ids_are_refused(direction_world):
    with pytest.raises(routing.RouteError, match="each id once"):
        routing.set_priority([{"account": "Codex", "exceptions": [_exception("a"), _exception("b")]}],
                             via="app", expect=routing.priority_version())


def test_a_new_exception_id_never_repeats_one_the_entry_holds(direction_world, monkeypatch):
    routing.set_priority([{"account": "Codex", "role": "reviews", "exceptions": [_exception(id_="xaaaaaaaa")]}],
                         via="app", expect=routing.priority_version())
    tokens = iter(["aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(routing.secrets, "token_hex", lambda n: next(tokens))
    out = routing.amend_priority("Codex", exception="second", until="2h", via="app", expect=routing.priority_version())
    assert out["exception_id"] == "xbbbbbbbb"
    assert sorted(x["id"] for x in _stored()[0]["exceptions"]) == ["xaaaaaaaa", "xbbbbbbbb"]


# ---- sweep: Stop tracking rewrites the priority from the stored line -----------------------------


def test_stop_tracking_keeps_the_other_entries_as_stored(direction_world):
    routing.set_priority([{"account": "Codex", "role": "reviews"},
                          {"account": "Grok", "role": "audits", "level": _future_level()},
                          {"account": "Claude", "role": "orchestration"}],
                         via="app", expect=routing.priority_version())
    grok = _stored()[1]
    claude = next(a["key"] for a in routing.read_view()["accounts"] if a["label"] == "Claude")
    events, done = accounts._stop_events(routing.read_view(), [claude], keep_priority=False, keep_pause=False,
                                         end_holds=[])
    assert done["priority"] == "removed"
    written = next(e for e in events if e["type"] == "priority_set")
    assert written["entries"][1] == grok
