"""Class B by the list (owner decision 2026-11-07): after ``khipu_needs_you``
shows a list, one typed reply ("yes", "yes except 2", "no to 3") settles every
B item shown, item by item through ``khipu_owner_act``, in T3 and outside it.
A class C item never settles from a list: it needs its own ask with its own
words, and the owner's yes to that ask.

Hermetic: the fixtures of ``test_t3_owner_confirm`` through
``test_owner_needs_you``'s builders."""
from __future__ import annotations

import time

import pytest

from khipu import needs_you, owner_act, routing, usage
from tests.test_owner_needs_you import (_accounts_request, _owner_types, _priority_request, _rule_added,
                                        _setting, _setting_request, _tier_request)
from tests.test_t3_owner_confirm import _be_front, _front_end_turn, world

assert world  # the fixture, imported for pytest

TERMINAL = "s-terminal"


def _read() -> list[dict]:
    return needs_you.waiting()["items"]


def _by_kind(items: list[dict]) -> dict[str, dict]:
    return {item["kind"]: item for item in items}


def _held_grok_pause(world) -> None:
    """A change held for the owner (direction.request), on an account the priority doesn't name."""
    _owner_types(world, "pause Grok")
    ok, out = owner_act.act("direction.request", {"action": "route.pause", "args": {"account": "Grok"}})
    assert ok and out["pending"] is True, out


def _grok_paused() -> bool:
    return next(a for a in routing.read_view()["accounts"] if a["label"] == "Grok")["state"] == "paused"


def _build_list(world) -> None:
    _held_grok_pause(world)
    _priority_request(world)
    _rule_added(world)
    _setting_request(world)
    _accounts_request(world)
    _tier_request(world)


def _in_t3(world, text: str) -> None:
    _owner_types(world, text)


def _in_claude_code(world, text: str) -> None:
    time.sleep(0.01)
    _front_end_turn(world, "claude_code:cli", TERMINAL)
    _be_front(world, "claude_code:cli", TERMINAL)


FRONTS = {"t3": _in_t3, "claude_code": _in_claude_code}


def _settled(world) -> dict[str, bool]:
    view = routing.read_view()
    rule = next(r for r in view["rules"] if r.get("text") == "Never send client work to Grok")
    return {"priority": not view["priority"]["pending"], "rule": not rule.get("pending"),
            "setting": _setting()["pending"] is False, "accounts": usage.accounts_status().get("request") is None,
            "tier_table": routing.pending_tier_table() is None}


@pytest.mark.parametrize("front", sorted(FRONTS))
def test_one_yes_to_the_list_settles_every_item_it_showed(world, front):
    _build_list(world)
    FRONTS[front](world, "what needs my confirmation?")
    items = _by_kind(_read())
    assert {"held", "priority", "rule", "setting", "accounts", "tier_table"} <= set(items)
    FRONTS[front](world, "yes")
    for kind in ("held", "priority", "rule", "setting", "accounts", "tier_table"):
        settle = items[kind]["settle"]["confirm"]
        ok, out = owner_act.act(settle["action"], settle["args"])
        assert ok and out.get("pending") is not True, (kind, out)
    assert _settled(world) == dict.fromkeys(("priority", "rule", "setting", "accounts", "tier_table"), True)
    assert _setting()["value"] is True and _grok_paused()


@pytest.mark.parametrize("front", sorted(FRONTS))
def test_yes_except_one_settles_the_rest_and_the_no_undoes_that_one(world, front):
    _build_list(world)
    FRONTS[front](world, "what needs my confirmation?")
    items = _by_kind(_read())
    FRONTS[front](world, "yes except the routing copy")
    for kind, side in (("priority", "confirm"), ("rule", "confirm"), ("setting", "undo"), ("tier_table", "confirm")):
        settle = items[kind]["settle"][side]
        ok, out = owner_act.act(settle["action"], settle["args"])
        assert ok and out.get("pending") is not True, (kind, out)
    assert _setting()["pending"] is False and _setting()["value"] is False
    assert not routing.read_view()["priority"]["pending"]


def _confirm(request: str) -> tuple[bool, dict]:
    """direction.confirm, and the background run a long change starts."""
    ok, done = owner_act.act("direction.confirm", {"request": request})
    if ok and done.get("started"):
        from khipu import owner_background

        return owner_background.run(done["request"])
    return ok, done


def _forget_waits(world) -> list:
    from khipu import forget

    world["mp"].setattr(forget, "unsent_forgets", lambda: {"ok": True, "strings": [], "count": 1,
                                                           "ids": [{"id": 9, "summary": "the roof"}]})
    ran: list = []
    world["mp"].setattr(owner_act, "_run", lambda act, full: (ran.append((act, full)), (True, {"ok": True}))[1])
    return ran


@pytest.mark.parametrize("front", sorted(FRONTS))
def test_a_yes_to_a_list_never_settles_what_cant_be_taken_back(world, front):
    ran = _forget_waits(world)
    _rule_added(world)
    FRONTS[front](world, "what needs my confirmation?")
    items = _by_kind(_read())
    assert {"rule", "forget_id"} <= set(items)
    FRONTS[front](world, "yes")
    settle = items["forget_id"]["settle"]["confirm"]
    ok, held = owner_act.act(settle["action"], settle["args"])
    assert ok and held["pending"] is True and held["applied"] is False, held
    assert "can't be undone" in held["ask"] and "roof" in held["ask"]
    assert not ran
    # Its own ask, then the owner's yes to it.
    FRONTS[front](world, "yes, forget it")
    ok, done = _confirm(held["request"])
    assert ok, done
    assert ran and ran[0][0] == "forget.here"


@pytest.mark.parametrize("front", sorted(FRONTS))
def test_a_yes_to_a_list_never_confirms_a_held_request_that_cant_be_taken_back(world, front):
    ran = _forget_waits(world)
    FRONTS[front](world, "forget the roof one on the hub too")
    ok, held = owner_act.act("forget.here", {"id": "9"})
    assert ok and held["pending"] is True, held
    _rule_added(world)
    FRONTS[front](world, "what needs my confirmation?")
    items = _read()
    listed = [i for i in items if i["kind"] == "held"]
    assert listed and listed[0]["settle"]["confirm"] == {"action": "direction.confirm",
                                                          "args": {"request": held["request"]}}
    assert len(items) > 1
    FRONTS[front](world, "yes")
    ok, refused = _confirm(held["request"])
    assert not ok and refused["code"] == "not_confirmed", refused
    assert "roof" in refused["error"] and "list" in refused["error"]
    assert not ran
    FRONTS[front](world, "yes, forget it")
    ok, done = _confirm(held["request"])
    assert ok, done
    assert ran and ran[0][0] == "forget.here"


@pytest.mark.parametrize("front", sorted(FRONTS))
def test_what_cant_be_taken_back_still_settles_on_its_own_ask(world, front):
    ran = _forget_waits(world)
    FRONTS[front](world, "forget the roof one on the hub too")
    ok, held = owner_act.act("forget.here", {"id": "9"})
    assert ok and held["pending"] is True, held
    FRONTS[front](world, "yes")
    ok, done = _confirm(held["request"])
    assert ok, done
    assert ran and ran[0][0] == "forget.here"
