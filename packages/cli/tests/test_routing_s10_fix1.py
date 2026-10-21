"""Slice 10 fix round 1: date hygiene, the tier table's owner path, and
word-boundary task guesses. Probes copied from the review, not imported."""
from __future__ import annotations

import copy
import json

import pytest

from khipu import routing, routing_advisory, routing_delegation
from tests import test_owner_direction as owner_world
from tests.test_routing_acceptance import AUTHOR, expect

pytest_plugins = ("tests.test_routing_acceptance",)

world = owner_world.world

TIERS = {
    "scope": "opus", "mock": "opus", "ruling": "opus", "hard-security": "opus",
    "failed-fix": "opus", "build": "sonnet", "fix-round": "sonnet", "tests": "sonnet",
    "docs": "sonnet", "synthesis": "sonnet", "mechanical": "haiku",
}
LINE = "Rule 12: fix rounds go to Sonnet; this dispatch is Opus. Say why if intended."


def payload(model="opus", brief="task: fix-round\nRepair the synthetic fixture."):
    return {"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Agent",
            "tool_input": {"model": model, "prompt": brief}}


def hook(call):
    out, _, _ = routing_delegation.hook_main(json.dumps(call))
    assert set(out) <= {"hookSpecificOutput"}, out
    return out.get("hookSpecificOutput", {}).get("additionalContext", "")


def dispatch_lines(text):
    return [line for line in text.splitlines() if line.startswith("Rule 12:")]


def hygiene(replay, entity):
    return [f for f in replay.pull().get("rule_hygiene", []) if entity in f.get("entities", [])]


# ---- date hygiene ------------------------------------------------------------------------


def test_fix1_provenance_date_is_neither_move_nor_expired(replay):
    rule = routing.add_rule("Grok never builds or fixes privacy or safety code (Matt 2026-10-12).", via="app")
    replay.advance(9 * 86400)
    assert hygiene(replay, rule["entity"]) == []


@pytest.mark.parametrize("words", ["Never run Codex until its limit is hit; keep headroom.",
                                   "Codex reviews on Friday builds are cheap."])
def test_fix1_bare_until_and_non_deadline_weekday_raise_no_flags(replay, words):
    rule = routing.add_rule(words, via="app")
    replay.advance(9 * 86400)
    assert hygiene(replay, rule["entity"]) == []


@pytest.mark.parametrize("prep", ["through", "by", "expires"])
def test_fix1_other_deadline_words_still_expire_and_suggest_a_role(replay, prep):
    future = routing.add_rule(f"Use Codex {prep} 2026-10-24.", via="app")
    assert any(f["kind"] == "move_to_role" for f in hygiene(replay, future["entity"]))
    past = routing.add_rule(f"Use Codex {prep} 2026-10-19.", via="app")
    assert any(f["kind"] == "expired" for f in hygiene(replay, past["entity"]))


def test_fix1_weekday_expiry_uses_the_first_weekday_in_the_text(replay):
    # Written Wednesday 2026-10-14; checked Saturday 2026-10-17: Friday (the
    # first weekday named) has passed, Monday has not.
    replay.advance(-6 * 86400)
    rule = routing.add_rule("Use Codex until Friday or Monday.", via="app")
    replay.advance(3 * 86400)
    assert any(f["kind"] == "expired" for f in hygiene(replay, rule["entity"]))


def test_fix1_named_reset_expiry_matches_the_accounts_window_names(replay):
    row = {"ok": True, "observed_at": routing._iso(replay.now), "probed_at": routing._iso(replay.now),
           "last_good_at": routing._iso(replay.now),
           "windows": [{"name": "daily", "window_mins": 1440, "used_pct": 4,
                        "resets_at": routing._iso(replay.now)}]}
    target = replay.data / "state" / "usage.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"version": 2, "rows": {"codex|": row}}))
    rule = routing.add_rule("Use Codex until its daily reset.", via="app")
    other = routing.add_rule("Use Grok until its daily reset.", via="app")
    replay.advance(2 * 3600)
    assert any(f["kind"] == "expired" for f in hygiene(replay, rule["entity"]))
    # Grok has no daily window in scope, so its rule does not borrow Codex's.
    assert not any(f["kind"] == "expired" for f in hygiene(replay, other["entity"]))


# ---- task-class guesses ------------------------------------------------------------------


@pytest.mark.parametrize("brief", ["Summarize why the contest flaked",
                                   "Write a haiku"])
def test_fix1_task_class_guess_uses_word_boundaries(brief):
    assert routing_advisory.task_class(brief) == (None, False)


def test_fix1_task_class_never_guesses_inside_a_word():
    # "telescope" is not scope and "mockup" is not mock; the whole word
    # "build" is the only guess this brief supports.
    assert routing_advisory.task_class("Review the build output for the telescope mockup") == ("build", True)


def test_fix1_task_class_guess_still_finds_a_whole_word():
    assert routing_advisory.task_class("Write tests for the telescope mockup") == ("tests", True)


# ---- the tier table's owner path ---------------------------------------------------------


def test_fix1_owner_set_tier_table_governs_and_conversation_proposal_does_not(replay):
    proposed = dict(TIERS, **{"fix-round": "opus"})
    out = routing.request_tier_table(proposed, owner_request="use the other table", author=AUTHOR, via="mcp")
    assert out["ok"] is True and out["pending"] is True
    assert replay.view().get("tier_table") is None
    assert dispatch_lines(hook(payload())) == []

    routing.set_tier_table(TIERS, via="app")
    expect((replay.view().get("tier_table") or {}).get("tiers"), TIERS,
           "fix1: owner-written tier table does not govern")
    assert replay.view()["tier_table"]["confirmed"] is True
    expect(dispatch_lines(hook(payload())), [LINE], "fix1: owner tier table did not govern dispatch")


def test_fix1_owner_set_tier_table_refuses_an_incomplete_table(replay):
    with pytest.raises(routing.RouteError):
        routing.set_tier_table({"fix-round": "sonnet"}, via="app")
    assert replay.view().get("tier_table") is None


def test_fix1_confirm_adopts_a_conversations_proposal(replay):
    proposed = dict(TIERS, **{"fix-round": "opus"})
    routing.request_tier_table(proposed, owner_request="use the other table", author=AUTHOR, via="mcp")
    routing.confirm_tier_table(via="app")
    expect((replay.view().get("tier_table") or {}).get("tiers"), proposed,
           "fix1: confirming a proposal did not adopt its tiers")
    assert dispatch_lines(hook(payload())) == []


def test_fix1_conversation_write_refuses_the_tier_table(replay):
    with pytest.raises(routing.RouteError):
        routing.conversation_write({"action": "tier_table_set", "tiers": TIERS},
                                   author=dict(AUTHOR))
    assert replay.view().get("tier_table") is None


def test_fix1_tier_table_set_by_owner_direction_and_undo_restores_the_prior(world):
    world.say("set the tier table")
    rc, out = world.run(["route", "tier-table", "set", "--tiers=" + json.dumps(TIERS)], True)
    assert rc == 0 and out["ok"] is True, out
    expect((routing.read_view().get("tier_table") or {}).get("tiers"), TIERS,
           "fix1: tier table set by direction does not govern")

    custom = dict(TIERS, **{"fix-round": "opus"})
    world.say("change the tier table")
    rc, out = world.run(["route", "tier-table", "set", "--tiers=" + json.dumps(custom)], True)
    assert rc == 0 and out["ok"] is True, out
    expect((routing.read_view().get("tier_table") or {}).get("tiers"), custom,
           "fix1: second tier table does not govern")

    world.say("undo that tier table change")
    rc, undone = world.run(["owner-direction", "undo", out["owner_action"]], True)
    assert rc == 0, undone
    expect((routing.read_view().get("tier_table") or {}).get("tiers"), TIERS,
           "fix1: Undo did not restore the prior tier table")


def test_fix1_tier_table_undo_to_no_table_clears_it(world):
    world.say("set the tier table")
    rc, out = world.run(["route", "tier-table", "set", "--tiers=" + json.dumps(TIERS)], True)
    assert rc == 0 and out["ok"] is True, out
    world.say("undo that tier table")
    rc, undone = world.run(["owner-direction", "undo", out["owner_action"]], True)
    assert rc == 0, undone
    assert routing.read_view().get("tier_table") is None


def test_fix1_owner_act_sets_the_tier_table_by_direction(world):
    from khipu import owner_act

    world.say("set the tier table")
    world.be(owner_world.OWNER)
    ok, out = owner_act.act("route.tier_table.set", {"tiers": copy.deepcopy(TIERS)})
    assert ok and out["ok"] is True, out
    expect((routing.read_view().get("tier_table") or {}).get("tiers"), TIERS,
           "fix1: khipu_owner_act tier table does not govern")


def test_fix1_owner_act_refuses_bad_tiers(world):
    from khipu import owner_act

    world.say("set the tier table")
    world.be(owner_world.OWNER)
    ok, out = owner_act.act("route.tier_table.set", {"tiers": {"fix-round": "sonnet"}})
    assert not ok and routing.read_view().get("tier_table") is None
