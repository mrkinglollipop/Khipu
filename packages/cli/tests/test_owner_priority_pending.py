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
    assert row["args"]["confirm_pending"] is True and row["args"]["expect"] == version
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
    ok, out = _ask_in_t3(world, "route.priority.amend", {"account": "Claude", "append": "x", "confirm_pending": True,
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
