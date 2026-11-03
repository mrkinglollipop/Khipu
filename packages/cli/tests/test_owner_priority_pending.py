"""A conversation's priority request waiting for the owner, settled by
talking to a conversation (2026-11-03): an owner amend asked for while it
waits says so at ask time and its one yes confirms the request as shown,
then applies the amendment; route.priority.confirm and route.priority.undo
work end to end through a T3 held request; confirming clears "the owner has
not confirmed"; priority writes answer with a short receipt, and a since=
read that shows the priority counts as shown.

Hermetic: the fixtures of ``test_t3_owner_confirm`` (temp data dir, capture
home, synthetic T3 database, faked process tree). Nothing here reads real
Khipu or T3 state."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest

from khipu import mcp_server, owner_act, owner_direction, routing
from tests.test_t3_owner_confirm import (THREAD, T3_SESSION, _be, _be_front, _confirm_id, _front_end_turn,
                                         _t3_hook_turn, world)

assert world  # the fixture, imported for pytest
FAR = "2030-01-01T00:00:00Z"
OTHER = "s-other-conversation"


def _owner_priority() -> None:
    routing.set_priority([{"account": "Claude", "role": "main work"}, {"account": "Codex", "role": "reviews"}],
                         via="app", expect=routing.priority_version())


def _pending_request(world, *, exception: str = "Codex takes the release work this week") -> str:
    """Another conversation's request, at the owner's request: an exception
    on Codex, live and waiting for the owner. The priority's version."""
    _owner_priority()
    _be(world, OTHER)
    shown = routing.conversation_view()
    out = routing.request_priority_amend("Codex", exception=exception, until=FAR,
                                         owner_request="give Codex the release work this week",
                                         author=routing.conversation_author(), via="mcp",
                                         expect=shown["priority"]["version"], base=shown["priority_owner_revision"])
    assert out["ok"] is True, out
    assert routing.read_view()["priority"]["pending"] is True
    return routing.priority_version()


def _ask_in_t3(world, action: str, args: dict):
    if not world["t3"].count(THREAD):
        world["t3"].message(THREAD, time.time() - 1, text="change the priority")
    _t3_hook_turn()
    _be(world, T3_SESSION)
    return owner_act.act(action, args)


def _owner_says_yes(world) -> str:
    mid = world["t3"].message(THREAD, time.time() + 1, text="Yes confirm")
    _t3_hook_turn(prompt="Yes confirm")
    return mid


def _roles() -> dict:
    return {e["label"]: e["role"] for e in routing.read_view()["priority"]["entries"]}


def _proposal_status() -> str:
    return [p["status"] for p in routing.read_view()["proposals"].values() if p.get("kind") == "priority"][-1]


# ---- 1. an amend asked for while a request waits ------------------------------------------------


def test_an_amend_while_a_request_waits_is_held_with_an_ask_naming_both_and_one_yes_settles_both(world):
    version = _pending_request(world)
    ok, out = _ask_in_t3(world, "route.priority.amend",
                         {"account": "Claude", "append": "and the docs", "expect": version})
    assert ok and out["pending"] is True and out["applied"] is False, out
    ask = out["ask"]
    # The ask names the waiting request, its words in full, its version, and the amendment.
    assert "waiting for you" in ask and version in ask, ask
    assert "Codex exception until" in ask and "Codex takes the release work this week" in ask, ask
    assert "give Codex the release work this week" in ask, ask
    assert "route.priority.amend" in ask and "and the docs" in ask, ask
    (row,) = owner_direction._read_requests(owner_direction.requests_path())
    assert row["args"]["confirm_pending"] == routing.pending_priority_pin(routing.read_view())
    assert row["args"]["confirm_pending"].startswith("pp-") and row["args"]["expect"] == version
    assert routing.read_view()["priority"]["pending"] is True  # nothing changed yet
    _owner_says_yes(world)
    ok, out = _confirm_id(out["request"])
    assert ok, out
    held = routing.read_view()["priority"]
    assert held["pending"] is False and held["confirmed"] is True
    assert _roles() == {"Claude": "main work and the docs", "Codex": "reviews"}
    codex = next(e for e in held["entries"] if e["label"] == "Codex")
    assert [x["text"] for x in codex["exceptions"]] == ["Codex takes the release work this week"]
    statuses = [p["status"] for p in routing.read_view()["proposals"].values() if p.get("kind") == "priority"]
    assert "confirmed" in statuses
    line = routing.priority_line(held, routing._now())
    assert "has not confirmed" not in line and line.startswith("Your priority (your words"), line


def test_a_newer_request_landing_before_the_yes_is_refused_never_confirmed_unseen(world):
    version = _pending_request(world)
    ok, out = _ask_in_t3(world, "route.priority.amend",
                         {"account": "Claude", "append": "and the docs", "expect": version})
    assert ok and out["pending"] is True, out
    # A newer request from a third conversation lands before the owner answers.
    _be(world, "s-third")
    shown = routing.conversation_view()
    newer = routing.request_priority_amend("Claude", role="only the hard bugs", owner_request="narrow Claude",
                                           author=routing.conversation_author(), via="mcp",
                                           expect=shown["priority"]["version"], base=shown["priority_owner_revision"])
    assert newer["ok"] is True, newer
    _be(world, T3_SESSION)
    _owner_says_yes(world)
    ok, refused = _confirm_id(out["request"])
    assert not ok, refused
    assert routing.read_view()["priority"]["pending"] is True and _roles()["Claude"] == "only the hard bugs"


def test_an_amend_on_an_older_version_is_refused_at_ask_time(world):
    version = _pending_request(world)
    ok, out = _ask_in_t3(world, "route.priority.amend", {"account": "Claude", "append": "x", "expect": "p0.stale"})
    assert not ok and out["code"] == "version_moved" and version in out["error"], out
    assert not owner_direction.requests_path().exists()


def test_an_owners_instant_amend_while_a_request_waits_is_held_not_applied(world):
    """The owner typed the amend, never a yes to the waiting request's words:
    the amend that would confirm it waits for his answer to the ask."""
    version = _pending_request(world)
    _front_end_turn(world, "claude_code:cli", "s-direct")
    _be_front(world, "claude_code:cli", "s-direct")
    ok, out = owner_act.act("route.priority.amend", {"account": "Claude", "append": "and docs", "expect": version})
    assert ok and out["pending"] is True and out["applied"] is False, out
    assert "Codex takes the release work this week" in out["ask"]
    assert routing.read_view()["priority"]["pending"] is True and _roles()["Claude"] == "main work"


def test_confirm_pending_with_nothing_waiting_is_refused(world):
    _owner_priority()
    ok, out = _ask_in_t3(world, "route.priority.amend", {"account": "Claude", "append": "x", "confirm_pending": "pp-0",
                                                         "expect": routing.priority_version()})
    assert not ok and out["code"] == "bad_args", out


def test_the_owner_write_refusal_names_confirm_and_undo(world):
    version = _pending_request(world)
    with pytest.raises(routing.RouteError) as err:
        routing.amend_priority("Claude", append="x", via="app", expect=version)
    words = str(err.value)
    assert "route.priority.confirm" in words and "route.priority.undo" in words and version in words, words
    assert routing.read_view()["priority"]["pending"] is True


def test_undoing_the_one_step_amend_puts_the_waiting_request_back(world):
    version = _pending_request(world)
    ok, out = _ask_in_t3(world, "route.priority.amend", {"account": "Claude", "append": "and docs",
                                                         "expect": version})
    _owner_says_yes(world)
    ok, done = _confirm_id(out["request"])
    assert ok, done
    assert (owner_direction._find(done["owner_action"]) or {}).get("action") == "route.priority.amend"
    result = owner_direction.undo(done["owner_action"], via="app")
    assert result.get("ok") is not False, result
    assert routing.read_view()["priority"]["pending"] is True and _roles()["Claude"] == "main work"


# ---- 2. confirm and undo from a T3 conversation -----------------------------------------------


def test_route_priority_confirm_works_end_to_end_from_a_t3_held_request(world):
    version = _pending_request(world)
    ok, out = _ask_in_t3(world, "route.priority.confirm", {"expect": version})
    assert ok and out["pending"] is True, out
    assert routing.read_view()["priority"]["pending"] is True
    _owner_says_yes(world)
    ok, done = _confirm_id(out["request"])
    assert ok, done
    assert routing.read_view()["priority"]["pending"] is False and _proposal_status() == "confirmed"


def test_route_priority_undo_works_end_to_end_from_a_t3_held_request(world):
    version = _pending_request(world)
    ok, out = _ask_in_t3(world, "route.priority.undo", {"expect": version})
    assert ok and out["pending"] is True, out
    _owner_says_yes(world)
    ok, done = _confirm_id(out["request"])
    assert ok, done
    held = routing.read_view()["priority"]
    assert held["pending"] is False and _proposal_status() == "undone"
    assert "exceptions" not in next(e for e in held["entries"] if e["label"] == "Codex")


def test_an_unproved_t3_confirm_stays_held(world):
    version = _pending_request(world)
    ok, out = _ask_in_t3(world, "route.priority.confirm", {"expect": version})
    ok, refused = _confirm_id(out["request"])  # no owner message after the ask
    assert not ok and refused["code"] == "not_confirmed", refused
    assert routing.read_view()["priority"]["pending"] is True


# ---- 3. "the owner has not confirmed" ----------------------------------------------------------


def test_confirming_clears_the_not_confirmed_mark_on_a_conversations_exception(world):
    version = _pending_request(world, exception="the owner's own words for Codex")
    view = routing.read_view()
    assert "the owner has not confirmed it yet" in routing.priority_line(view["priority"], routing._now())
    ok, out = _ask_in_t3(world, "route.priority.confirm", {"expect": version})
    _owner_says_yes(world)
    ok, done = _confirm_id(out["request"])
    assert ok, done
    view = routing.read_view()
    line = routing.priority_line(view["priority"], routing._now())
    assert "has not confirmed" not in line and "the owner's own words for Codex" in line, line
    _be(world, OTHER)
    public = routing.conversation_view()["priority"]
    assert public["confirmed"] is True and public["pending"] is False
    assert "has not confirmed" not in public["line"]
    warnings = routing._priority_warnings(view, routing._now(), {"routing_priority_unconfirmed_warn_after": 1})
    assert not any("not confirmed" in w for w in warnings)


# ---- 4. token cost -----------------------------------------------------------------------------


def test_a_priority_write_answers_with_a_short_receipt(world):
    _owner_priority()
    routing.amend_priority("Claude", role="x " * 900, via="app", expect=routing.priority_version())
    _be(world, OTHER)
    shown = mcp_server._tool_route({})
    out = mcp_server._tool_route_priority({"account": "Codex", "exception": "release work", "until": FAR,
                                           "owner_request": "release work for Codex",
                                           "base": shown["priority_owner_revision"],
                                           "expect": shown["priority"]["version"]})
    assert out["ok"] is True and out["priority"]["pending"] is True, out
    blob = json.dumps(out)
    assert "x x x x" not in blob and "line" not in out["priority"] and "replaced" not in out["priority"], blob
    assert len(blob) < 1500, len(blob)
    assert out["priority"]["version"] == routing.priority_version()
    assert {e["label"] for e in out["priority"]["entries"]} == {"Claude", "Codex"}
    assert all(e["revision"].startswith("pe-") for e in out["priority"]["entries"])
    assert any("release work" in c for c in out["priority"]["changes"])
    assert "khipu_route" in out["priority"]["read"]


def test_a_since_read_showing_the_priority_counts_as_shown(world):
    _owner_priority()
    _be(world, OTHER)
    start = mcp_server._tool_route({})["version"]
    routing.amend_priority("Claude", append="and docs", via="app", expect=routing.priority_version())
    # Not shown yet: a request on the new revision is refused.
    revision = routing.read_view()["priority_owner_revision"]
    with pytest.raises(routing.RouteError, match="not shown"):
        mcp_server._tool_route_priority({"account": "Codex", "append": "and tests", "owner_request": "tests too",
                                         "base": revision, "expect": routing.priority_version()})
    answer = mcp_server._tool_route({"since": str(start)})
    assert "priority" in answer["entities"], answer
    assert answer["priority"]["owner_revision"] == revision
    out = mcp_server._tool_route_priority({"account": "Codex", "append": "and tests", "owner_request": "tests too",
                                           "base": answer["priority"]["owner_revision"],
                                           "expect": answer["priority"]["version"]})
    assert out["ok"] is True, out


def test_a_since_read_without_the_priority_records_nothing(world):
    _owner_priority()
    _be(world, OTHER)
    mcp_server._tool_route({})
    routing.amend_priority("Claude", append="and docs", via="app", expect=routing.priority_version())
    after = routing.read_view()["version"]
    revision = routing.read_view()["priority_owner_revision"]
    routing.add_rule("An unrelated rule.", via="app")
    answer = mcp_server._tool_route({"since": str(after)})
    assert "priority" not in answer["entities"] and "priority" not in answer, answer
    with pytest.raises(routing.RouteError, match="not shown"):
        mcp_server._tool_route_priority({"account": "Codex", "append": "and tests", "owner_request": "tests too",
                                         "base": revision, "expect": routing.priority_version()})


# ---- 5. the ask names every change the yes confirms (security review, 2026-11-03) ---------------

LEVEL = {"claim": "Max 20x", "as_of": "2029-01-01T00:00:00Z", "until": "2029-12-01T00:00:00Z"}


def _owner_then_request(world, owner: list, request: list, *, until=None) -> str:
    """The owner's priority, then another conversation's whole-priority
    request over it (at the owner's request). The priority's version."""
    routing.set_priority(owner, via="app", expect=routing.priority_version())
    _be(world, OTHER)
    shown = routing.conversation_view()
    out = routing.request_priority(request, until=until, owner_request="same priority please",
                                   author=routing.conversation_author(), via="mcp",
                                   expect=shown["priority"]["version"], base=shown["priority_owner_revision"])
    assert out["ok"] is True, out
    assert routing.read_view()["priority"]["pending"] is True
    return routing.priority_version()


def test_a_level_the_request_drops_is_named_in_the_ask_never_no_change(world):
    """The reviewer's repro: the owner gave Codex a level; a conversation's
    request resubmits the same entries without it. The amend's ask must say
    the level goes, with its claim and window, and the yes then removes it
    as shown."""
    version = _owner_then_request(world, [{"account": "Claude", "role": "main work"},
                                          {"account": "Codex", "role": "reviews", "level": LEVEL}],
                                  [{"account": "Claude", "role": "main work"}, {"account": "Codex", "role": "reviews"}])
    ok, out = _ask_in_t3(world, "route.priority.amend", {"account": "Claude", "append": "x", "expect": version})
    assert ok and out["pending"] is True, out
    ask = out["ask"]
    assert "no change" not in ask, ask
    assert "Codex level removed" in ask and "Max 20x" in ask, ask
    assert LEVEL["as_of"] in ask and LEVEL["until"] in ask, ask
    _owner_says_yes(world)
    ok, done = _confirm_id(out["request"])
    assert ok, done
    codex = next(e for e in routing.read_view()["priority_stored"]["entries"] if e["label"] == "Codex")
    assert "level" not in codex and routing.read_view()["priority"]["pending"] is False


def test_a_first_priority_requests_end_is_named(world):
    """No priority before the request: its end is still a change the yes
    makes the owner's."""
    _be(world, OTHER)
    shown = routing.conversation_view()
    out = routing.request_priority([{"account": "Claude", "role": "main work"}], until="2029-06-01T00:00:00Z",
                                   owner_request="claude first", author=routing.conversation_author(), via="mcp",
                                   expect=routing.priority_version(), base=shown["priority_owner_revision"])
    assert out["ok"] is True, out
    changes = routing.pending_priority_changes(routing.read_view())
    assert "whole priority until 2029-06-01T00:00:00Z" in changes, changes
    ok, held = _ask_in_t3(world, "route.priority.amend", {"account": "Claude", "append": "x",
                                                          "expect": routing.priority_version()})
    assert ok and "whole priority until 2029-06-01T00:00:00Z" in held["ask"], held


NOW = datetime(2026, 11, 3, tzinfo=timezone.utc)
PAST = "2026-10-01T00:00:00Z"
EXC = {"id": "x1", "text": "release work", "until": "2030-01-01T00:00:00Z", "at": PAST, "by": "a conversation",
       "source": "please"}


def _entry(**fields) -> dict:
    return {"account": "codex|", "label": "Codex", "role": "reviews", "until": None, **fields}


def _diff(old: list, new: list, *, until=None, was_until=None, replaced=True):
    view = {"priority_stored": {"pending": True, "entries": new, "until": until,
                                "replaced": {"entries": old, "until": was_until} if replaced else None}}
    return routing.pending_priority_changes(view, NOW), routing.pending_priority_unnamed(view, NOW)


@pytest.mark.parametrize("old, new, words", [
    (_entry(), _entry(role="everything"), "Codex's role: \"everything\""),
    (_entry(), _entry(until="2030-02-01T00:00:00Z"), "Codex until 2030-02-01T00:00:00Z"),
    (_entry(until="2030-02-01T00:00:00Z"), _entry(), "Codex until no end"),
    (_entry(label="Old name"), _entry(), "Old name now named Codex"),
    (_entry(level=LEVEL), _entry(), "Codex level removed (was \"Max 20x\" from 2029-01-01T00:00:00Z until "
                                    "2029-12-01T00:00:00Z)"),
    (_entry(), _entry(level=LEVEL), "Codex level: \"Max 20x\" from 2029-01-01T00:00:00Z until 2029-12-01T00:00:00Z"),
    (_entry(level=LEVEL), _entry(level=dict(LEVEL, claim="Pro")), "Codex level: \"Pro\" from"),
    (_entry(level=LEVEL), _entry(level=dict(LEVEL, as_of="2029-03-01T00:00:00Z")),
     "from 2029-03-01T00:00:00Z until 2029-12-01T00:00:00Z (was \"Max 20x\" from 2029-01-01T00:00:00Z"),
    (_entry(level=LEVEL), _entry(level=dict(LEVEL, until="2029-06-01T00:00:00Z")),
     "until 2029-06-01T00:00:00Z (was \"Max 20x\""),
    (_entry(), _entry(exceptions=[dict(EXC, share_pct=40)]),
     "Codex exception until 2030-01-01T00:00:00Z, share 40%: \"release work\""),
    (_entry(exceptions=[EXC]), _entry(exceptions=[dict(EXC, until="2031-01-01T00:00:00Z")]),
     "Codex exception \"release work\" changed: until 2031-01-01T00:00:00Z (was 2030-01-01T00:00:00Z)"),
    (_entry(exceptions=[dict(EXC, share_pct=40)]), _entry(exceptions=[dict(EXC, share_pct=50)]),
     "share 50% (was 40%)"),
    (_entry(exceptions=[dict(EXC, share_pct=40)]), _entry(exceptions=[EXC]), "share none (was 40%)"),
    (_entry(exceptions=[EXC]), _entry(exceptions=[dict(EXC, text="all the work")]),
     "words \"all the work\" (was \"release work\")"),
    (_entry(exceptions=[EXC]), _entry(exceptions=[dict(EXC, by="the owner")]), "by \"the owner\""),
    (_entry(exceptions=[EXC]), _entry(), "Codex exception ended: \"release work\""),
    (_entry(exceptions=[dict(EXC, until=PAST)]), _entry(), "ended (it had already lapsed)"),
])
def test_every_field_the_confirm_writes_is_named(old, new, words):
    changes, unnamed = _diff([old], [new])
    assert any(words in c for c in changes), changes
    assert unnamed == [] and "no change to the priority it replaced" not in changes


def test_an_ended_entry_dropped_and_the_order_are_named():
    claude = {"account": "claude|", "label": "Claude", "role": "main work", "until": None}
    gone = {"account": "gemini|", "label": "Gemini", "role": "old", "until": PAST}
    changes, unnamed = _diff([claude, gone, _entry()], [_entry(), claude])
    assert "remove Gemini" in changes and "order: Codex, Claude" in changes, changes
    assert unnamed == []


def test_the_whole_priority_end_is_named_with_or_without_a_priority_replaced():
    assert "whole priority until no end" in _diff([_entry()], [_entry()], was_until=PAST)[0]
    changes, _ = _diff([], [_entry()], until="2030-01-01T00:00:00Z", replaced=False)
    assert "add Codex: \"reviews\"" in changes and "whole priority until 2030-01-01T00:00:00Z" in changes


def test_an_identical_request_is_no_change():
    entry = _entry(level=LEVEL, exceptions=[EXC])
    assert _diff([entry], [dict(entry)]) == (["no change to the priority it replaced"], [])


@pytest.mark.parametrize("old, new, what", [
    (_entry(), _entry(colour="red"), "Codex's colour"),
    (_entry(level=LEVEL), _entry(level=dict(LEVEL, tier=2)), "Codex's level"),
    (_entry(exceptions=[EXC]), _entry(exceptions=[dict(EXC, weight=3)]), "Codex's exceptions"),
])
def test_a_difference_with_no_words_is_named_as_such_never_dropped(old, new, what):
    changes, unnamed = _diff([old], [new])
    assert unnamed == [what], unnamed
    assert any(what in c and "can't be put in words" in c for c in changes), changes


def test_an_amend_whose_request_has_a_change_with_no_words_is_refused_at_ask_time(world, monkeypatch):
    version = _pending_request(world)
    real = routing.read_view

    def odd(*a, **k):
        view = real(*a, **k)
        stored = view.get("priority_stored")
        if stored and stored.get("pending"):
            stored["entries"] = [dict(e, colour="red") for e in stored["entries"]]
        return view

    monkeypatch.setattr(routing, "read_view", odd)
    ok, out = _ask_in_t3(world, "route.priority.amend", {"account": "Claude", "append": "x", "expect": version})
    assert not ok and out["code"] == "priority_pending", out
    assert "route.priority.confirm" in out["error"] and "route.priority.undo" in out["error"], out
    assert "colour" in out["error"], out
    assert not owner_direction.requests_path().exists()


# ---- 6. the pin covers the priority the request replaced ---------------------------------------


def test_the_priority_replaced_changing_under_an_unmoved_version_is_refused_at_the_confirm(world):
    """An owner write from another Mac that sorts before a waiting request
    (here one written without a base, as an older Khipu writes it) changes
    the priority it replaced, and so the ask's changes, without moving the
    version: the yes is refused, never applied to what the ask didn't show."""
    from khipu import routing_events

    _pending_request(world)
    path = routing.events_file()
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    owner, waiting = rows[0], rows[1]
    waiting.pop("base")
    waiting["hlc"] = dict(waiting["hlc"], wall=owner["hlc"]["wall"] + 10, counter=0)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    version = routing.priority_version()
    ok, out = _ask_in_t3(world, "route.priority.amend", {"account": "Claude", "append": "and docs",
                                                         "expect": version})
    assert ok and out["pending"] is True, out
    assert "Codex takes the release work this week" in out["ask"]
    other = dict(owner, id=routing_events.uuid7(), op=routing_events.uuid7(), mac="c" * 32, seq=1, base=owner["id"],
                 hlc={"wall": owner["hlc"]["wall"] + 5, "counter": 0, "mac": "c" * 32},
                 entries=[{"account": "codex|", "label": "Codex", "role": "everything", "until": None}])
    with path.open("a") as f:
        f.write(json.dumps(other) + "\n")
    view = routing.read_view()
    assert view["priority_version"] == version and view["priority"]["pending"] is True
    assert view["priority_stored"]["replaced"]["entries"] == other["entries"]  # what the ask showed is gone
    _be(world, T3_SESSION)
    _owner_says_yes(world)
    ok, refused = _confirm_id(out["request"])
    assert not ok, refused
    assert routing.read_view()["priority"]["pending"] is True and _roles()["Claude"] == "main work"


# ---- 7. confirm_pending only from a confirmed hold's replay ------------------------------------


def test_confirm_pending_on_an_owner_turn_without_an_ask_is_refused(world):
    """A conversation on a turn that passes the owner check runs the amend
    itself with confirm_pending: refused, the waiting request untouched."""
    version = _pending_request(world)
    pin = routing.pending_priority_pin(routing.read_view())
    _front_end_turn(world, "claude_code:cli", "s-direct")
    _be_front(world, "claude_code:cli", "s-direct")
    owner_direction.check("route.priority.amend", instant=True)  # the turn is the owner's
    head, positionals = owner_act.argv("route.priority.amend", {"account": "Claude", "append": "x",
                                                                "expect": version, "confirm_pending": pin})
    ok, out = owner_act._run("route.priority.amend", owner_act.full_argv(head, positionals, ["--owner-direction"]))
    assert not ok, out
    assert "confirm_pending only comes from" in json.dumps(out), out
    with pytest.raises(routing.RouteError, match="confirm_pending only comes from"):
        routing.amend_priority("Claude", append="x", via="app", expect=version, confirm_pending=pin)
    assert routing.read_view()["priority"]["pending"] is True and _roles()["Claude"] == "main work"
