"""Account plan terms (routing slice 2): ``ends``, ``plan_change``, ``resets_held``
and ``ceiling_with_ok`` on an account definition. They read through to every
reader as written; only ``ends`` acts, and a passed one takes the account out of
the owner's order with nothing written. Scope:
jobs/2026-11-07/scope-routing-one-page.md, slice 2."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from khipu import routing, routing_entries

pytest_plugins = ("tests.test_routing_acceptance",)


def _account(ident, label, **terms):
    return {"id": ident, "label": label, "provider": ident.partition(":")[0], **terms}


# ---- the closed schema ------------------------------------------------------------------------


def test_every_term_is_accepted_and_kept_as_written():
    value = _account("muse:main", "Muse", ends="2026-11-13",
                     plan_change={"to": "Pro 100", "on": "2026-11-08"}, resets_held=2, ceiling_with_ok=90)
    assert routing_entries.checked_definition("account", value) == value
    timed = _account("kimi:main", "Kimi", ends="2026-11-27T12:00:00Z", plan_change={"to": "a smaller plan"},
                     resets_held=0, ceiling_with_ok=87.5)
    assert routing_entries.checked_definition("account", timed) == timed


def test_an_account_without_terms_reads_as_before():
    plain = {"id": "claude:main", "label": "Claude", "provider": "claude", "family": "Anthropic"}
    assert routing_entries.checked_definition("account", plain) == plain


@pytest.mark.parametrize("bad", [
    {"ends": "soon"}, {"ends": 20261113}, {"ends": ""}, {"ends": "2026-13-40"},
    {"plan_change": "Pro 100"}, {"plan_change": {}}, {"plan_change": {"to": ""}},
    {"plan_change": {"to": "Pro 100", "on": "next week"}}, {"plan_change": {"to": "Pro 100", "price": 100}},
    {"plan_change": {"to": "see /private/plan"}}, {"plan_change": {"to": "x" * 121}},
    {"resets_held": -1}, {"resets_held": 100}, {"resets_held": True}, {"resets_held": 1.5}, {"resets_held": "2"},
    {"ceiling_with_ok": 0}, {"ceiling_with_ok": 101}, {"ceiling_with_ok": True}, {"ceiling_with_ok": "90"},
    {"ceiling": 90},
])
def test_a_bad_term_refuses_the_whole_definition(bad):
    assert routing_entries.checked_definition("account", _account("grok:main", "Grok", **bad)) is None


def test_terms_belong_to_accounts_only():
    assert routing_entries.checked_definition(
        "provider", {"id": "muse", "label": "Muse", "family": "Meta", "ends": "2026-11-13"}) is None
    assert routing_entries.checked_definition(
        "lane_map", {"runner": "r", "lane": "l", "account": "muse:main", "ends": "2026-11-13"}) is None


def test_a_bare_date_is_the_whole_of_that_day_in_utc():
    assert routing_entries.term_time("2026-11-13") == datetime(2026, 11, 14, tzinfo=timezone.utc)
    assert routing_entries.term_time("2026-11-13T09:30:00Z") == datetime(2026, 11, 13, 9, 30, tzinfo=timezone.utc)
    assert routing_entries.term_time("not a date") is None


# ---- read-through -----------------------------------------------------------------------------


def _define(**ends_by_id):
    for ident, family in (("muse:main", "Meta"), ("glm:main", "Zhipu"), ("kimi:main", "Moonshot")):
        provider = ident.partition(":")[0]
        routing.set_definition("provider", {"id": provider, "label": provider.title(), "family": family,
                                            "probe": "none", "lane_names": [provider]}, via="app")
        terms = {"ends": ends_by_id[provider]} if provider in ends_by_id else {}
        routing.set_definition("account", _account(ident, provider.title(), **terms), via="app")


def test_terms_read_through_to_the_view_and_the_conversation_view_and_change_nothing_else(replay):
    _define()
    routing.set_definition("account", _account("muse:main", "Muse", ends="2026-11-13",
                                               plan_change={"to": "Pro 100", "on": "2026-11-08"},
                                               resets_held=2, ceiling_with_ok=90), via="app")
    view = routing.read_view()
    stored = view["definitions"]["accounts"]["muse:main"]
    assert stored["ceiling_with_ok"] == 90 and stored["resets_held"] == 2
    entry = next(a for a in view["accounts"] if a["key"] == "muse:main")
    assert entry["terms"] == {"ends": "2026-11-13", "plan_change": {"to": "Pro 100", "on": "2026-11-08"},
                              "resets_held": 2, "ceiling_with_ok": 90}
    # Read-through only: the 90% is not a cap and no hold appears.
    assert not entry.get("caps") and not view["holds"]
    public = next(a for a in routing.conversation_view()["accounts"] if a["label"] == "Muse")
    assert public["terms"]["ceiling_with_ok"] == 90 and public["terms"]["plan_change"]["to"] == "Pro 100"
    assert all("terms" not in a for a in view["accounts"] if a["key"] != "muse:main")


def test_terms_travel_to_the_other_mac_whole(replay):
    routing.set_definition("account", _account("muse:main", "Muse", ends="2026-11-13",
                                               plan_change={"to": "Pro 100"}, resets_held=1,
                                               ceiling_with_ok=90), via="app")
    events = routing.read_events()[0]
    local = [e for e in events if e.get("type") == "definition_set"]
    bindings, _forwards, _aliases = routing.account_identity.state(routing._in_write_order(events),
                                                                   routing.accounts())
    sent = routing.replication_payload(local, bindings=bindings)
    assert sent and sent[-1]["value"]["plan_change"] == {"to": "Pro 100"}
    assert sent[-1]["value"]["ceiling_with_ok"] == 90 and sent[-1]["value"]["ends"] == "2026-11-13"


# ---- a passed ``ends`` leaves the order -------------------------------------------------------


def _order(view):
    return [e["label"] for e in (view.get("priority") or {}).get("entries") or []]


def test_an_account_leaves_the_order_when_its_plan_ends_and_nothing_is_written(replay):
    now = routing._now()
    ends = now + timedelta(days=1)
    _define(muse=routing._iso(ends))
    routing.set_priority([{"account": "Kimi", "role": "builds"}, {"account": "Muse", "role": "second opinions"},
                          {"account": "Glm", "role": "fixes"}], via="app", expect=routing.priority_version())
    written = len(routing.read_events()[0])
    before = routing.read_view(now)
    assert _order(before) == ["Kimi", "Muse", "Glm"]
    # The view refolds by itself at the end.
    assert before["valid_until"] is not None and routing._parse(before["valid_until"]) <= ends
    after_at = ends + timedelta(minutes=1)
    after = routing.read_view(after_at)
    assert _order(after) == ["Kimi", "Glm"]  # the rest keep their places
    assert "Muse" in after["priority_ended"]
    assert "Muse" not in routing.priority_line(after["priority"], after_at)
    from khipu import routing_page
    order = routing_page.page(after, after_at).split("ACCOUNTS", 1)[1].split("Not in the owner's order", 1)[0]
    assert "Muse" not in order
    assert all(e["label"] != "Muse" for e in routing.conversation_view(now=after_at)["priority"]["entries"])
    # The account itself is still tracked and the owner's line keeps it, so a
    # later ends (or its removal) brings it back with no rewrite.
    assert any(a["key"] == "muse:main" for a in after["accounts"])
    assert [e["account"] for e in after["priority_stored"]["entries"]].count("muse:main") == 1
    assert len(routing.read_events()[0]) == written


def test_moving_ends_later_brings_the_account_back(replay):
    now = routing._now()
    _define(muse=routing._iso(now - timedelta(hours=1)))
    routing.set_priority([{"account": "Kimi", "role": "builds"}, {"account": "Muse", "role": "second opinions"}],
                         via="app", expect=routing.priority_version())
    assert _order(routing.read_view(now)) == ["Kimi"]
    routing.set_definition("account", _account("muse:main", "Muse", ends=routing._iso(now + timedelta(days=3))),
                           via="app")
    assert _order(routing.read_view(now)) == ["Kimi", "Muse"]


def test_when_every_account_has_ended_there_is_no_order(replay):
    now = routing._now()
    _define(muse=routing._iso(now + timedelta(hours=1)), glm=routing._iso(now + timedelta(hours=2)))
    routing.set_priority([{"account": "Muse", "role": "second opinions"}, {"account": "Glm", "role": "fixes"}],
                         via="app", expect=routing.priority_version())
    later = now + timedelta(hours=3)
    view = routing.read_view(later)
    assert view["priority"] is None and set(view["priority_ended"]) == {"Muse", "Glm"}


def test_the_three_known_end_dates_leave_in_turn(replay):
    _define(muse="2026-11-13", glm="2026-11-20", kimi="2026-11-27")
    routing.set_priority([{"account": "Muse", "role": "second opinions"}, {"account": "Kimi", "role": "builds"},
                          {"account": "Glm", "role": "fixes"}], via="app", expect=routing.priority_version())
    at = lambda *d: datetime(*d, tzinfo=timezone.utc)  # noqa: E731
    if routing._now() >= at(2026, 11, 14):
        pytest.skip("the fixture dates are behind the real clock")
    assert _order(routing.read_view(at(2026, 11, 13, 23, 59))) == ["Muse", "Kimi", "Glm"]
    assert _order(routing.read_view(at(2026, 11, 14))) == ["Kimi", "Glm"]
    assert _order(routing.read_view(at(2026, 11, 21))) == ["Kimi"]
    assert routing.read_view(at(2026, 11, 28))["priority"] is None


def test_a_definition_line_with_bad_terms_never_reaches_a_reader(replay):
    with pytest.raises(routing.RouteError):
        routing.set_definition("account", _account("muse:main", "Muse", ends="whenever"), via="app")
    # A hand-written line with a bad term is skipped by the fold, as any bad definition is.
    _define()
    view = routing.read_view()
    assert "ends" not in json.dumps(view["definitions"]["accounts"])
