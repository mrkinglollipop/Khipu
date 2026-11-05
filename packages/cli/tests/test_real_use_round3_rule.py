"""One typed message settles a rule the owner asked for in a T3 conversation (round 3)."""
from __future__ import annotations

import time

from khipu import needs_you, owner_act, owner_direction, owner_turns, routing
from tests.test_owner_needs_you import _owner_types, _rule_added
from tests.test_t3_owner_confirm import T3_SESSION, world  # noqa: F401


def _own_rule(text="Never send client work to Grok"):
    return routing.request_rule(text, owner_request="add that rule", author=routing.conversation_author(), via="mcp")


def _numbered_by_the_hub(monkeypatch):
    """Routing is copied between Macs: a new rule has no number until a sync brings one."""
    monkeypatch.setattr(routing, "_pending_numbering", lambda: True)


def test_a_rule_asked_for_in_a_t3_turn_the_hook_could_not_verify_applies_at_once(world, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(owner_turns, "_t3_membership", lambda session: "unverified")
        _owner_types(world, "add a rule: never send client work to Grok")
    assert [t["entrypoint"] for t in owner_turns.turns(T3_SESSION, time.time())] == ["t3-unverified"]
    done = owner_act.apply_request("rule", _own_rule())
    assert done["applied"] is True and done["pending"] is False
    assert done["instant"]["undo"] == f"khipu owner-direction undo {done['instant']['owner_action']}"


def test_a_rule_the_hub_has_not_numbered_yet_applies_at_once_with_a_receipt_and_undo(world, monkeypatch):
    _numbered_by_the_hub(monkeypatch)
    _owner_types(world, "add a rule: never send client work to Grok")
    out = _own_rule()
    assert out["n"] is None and out["entity"]
    done = owner_act.apply_request("rule", out)
    assert done["applied"] is True and done["instant"]["owner_action"]
    (rule,) = routing.read_view()["rules"]
    assert rule["confirmed"] is True and not rule.get("pending")
    line = next(x for x in owner_direction.read_log() if x.get("id") == done["instant"]["owner_action"])
    assert line["action"] == "route.rule.confirm" and line["instant"]["source"] == "t3"
    assert done["instant"]["undo"] == f"khipu owner-direction undo {done['instant']['owner_action']}"


def test_a_just_added_rule_is_named_never_null_in_what_needs_you_offers(world, monkeypatch):
    _numbered_by_the_hub(monkeypatch)
    _rule_added(world)
    (item,) = needs_you.waiting()["items"]
    for side in ("confirm", "undo"):
        ref = item["settle"][side]["args"]["rule"]
        assert ref is not None and ref.startswith("rule-"), item["settle"]
    assert item["key"].startswith("rule:rule-")


def test_a_yes_after_a_shortened_read_settles_the_rule_in_one_message(world, monkeypatch):
    _numbered_by_the_hub(monkeypatch)
    _rule_added(world, "the words " * 50 + "THE TAIL")
    _owner_types(world, "what is waiting on me?")
    (item,) = needs_you.waiting()["items"]
    assert item["cut"] is True
    _owner_types(world, "Yes")
    ok, out = owner_act.act(item["settle"]["confirm"]["action"], item["settle"]["confirm"]["args"])
    assert ok and out.get("applied") is not False and not out.get("pending"), out
    (rule,) = routing.read_view()["rules"]
    assert rule["confirmed"] is True


def test_a_yes_typed_before_the_read_still_does_not_settle_it(world, monkeypatch):
    _rule_added(world, "the words " * 50 + "THE TAIL")
    _owner_types(world, "Yes")
    (item,) = needs_you.waiting()["items"]
    ok, out = owner_act.act(item["settle"]["confirm"]["action"], item["settle"]["confirm"]["args"])
    assert not (ok and not out.get("pending")), out
    assert routing.read_view()["rules"][0].get("pending")


def test_a_bad_rule_reference_is_still_refused():
    for bad in ("rule-xyz", "2.5", "", "../rule"):
        ok, out = owner_act.act("route.rule.confirm", {"rule": bad})
        assert not ok and out["code"] == "bad_args", (bad, out)
