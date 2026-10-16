"""Owner direction: what the owner's words ask for (docs/plans/2026-10-16-owner-direction.md,
section 2.3 step 4). One parser reads the action, its target and whether the owner asked for it
or against it: negation never authorises, a conjunction acts on each named pair exactly, the
longest phrase wins ("stop trusting" is not "trust"), a priority keeps the owner's order, a
timed pause keeps its time, and a bare yes confirms only the widening it was asked about.

Hermetic: the ``world`` fixture from tests/test_owner_direction.py (temp data and capture
folders, turns recorded the way the prompt hook records them)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from khipu import owner_act, owner_direction, owner_turns, routing
from tests import test_owner_direction as base

world = base.world  # the fixture, shared
_state = base._state

PASS = "pass"
NOT_ASKED = "words_do_not_ask_for_this"
TIME = "time_not_the_owners"
TRUST = "trust claude_code:sdk-cli"
UNTRUST = "untrust claude_code:sdk-cli"


def _next_local(hour: int, minute: int = 0, *, shift_h: float = 0) -> str:
    now = datetime.now().astimezone()
    then = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if then <= now:
        then += timedelta(days=1)
    return (then + timedelta(hours=shift_h)).isoformat()


# (words, action, target, extra check arguments, expected: PASS or a refusal code)
TABLE = [
    # negation never authorises
    ("do not pause Codex for now", "route.pause", "codex|", {}, NOT_ASKED),
    ("don't pause Codex please", "route.pause", "codex|", {}, NOT_ASKED),
    ("never pause Codex again", "route.pause", "codex|", {}, NOT_ASKED),
    ("no need to pause Codex today", "route.pause", "codex|", {}, NOT_ASKED),
    ("pause Codex for now", "route.pause", "codex|", {}, PASS),
    ("pause Codex but not Grok", "route.pause", "codex|", {}, PASS),
    ("pause Codex but not Grok", "route.pause", "grok|", {}, NOT_ASKED),
    # conjunctions act on each named pair exactly
    ("pause Grok and lift Codex please", "route.pause", "codex|", {}, NOT_ASKED),
    ("pause Grok and lift Codex please", "route.pause", "grok|", {}, PASS),
    ("pause Grok and lift Codex please", "route.lift", "codex|", {}, PASS),
    ("pause Grok and lift Codex please", "route.lift", "grok|", {}, NOT_ASKED),
    ("lift Codex pause Grok now", "route.lift", "codex|", {}, PASS),
    ("lift Codex pause Grok now", "route.pause", "codex|", {}, NOT_ASKED),
    ("lift Codex pause Grok now", "route.lift", "grok|", {}, NOT_ASKED),
    # a statement with no verb inherits the one before it
    ("pause Codex and Grok please", "route.pause", "grok|", {}, PASS),
    ("pause Codex, then Grok as well", "route.pause", "grok|", {}, PASS),
    ("pause Codex and Grok please", "route.pause", "claude|", {}, NOT_ASKED),
    # a verb used as a noun ("the pause") is not asked for
    ("lift the pause on Codex", "route.lift", "codex|", {}, PASS),
    ("lift the pause on Codex", "route.pause", "codex|", {}, NOT_ASKED),
    # longest match: "stop trusting" never reads as trust, "unpause" never as pause
    ("stop trusting claude_code:sdk-cli please", "direction.set", TRUST, {}, NOT_ASKED),
    ("stop trusting claude_code:sdk-cli please", "direction.set", UNTRUST, {}, PASS),
    ("don't trust claude_code:sdk-cli anymore", "direction.set", TRUST, {}, NOT_ASKED),
    ("don't trust claude_code:sdk-cli anymore", "direction.set", UNTRUST, {}, PASS),
    ("start trusting claude_code:sdk-cli now", "direction.set", TRUST, {}, PASS),
    ("start trusting claude_code:sdk-cli now", "direction.set", UNTRUST, {}, NOT_ASKED),
    ("unpause Codex right now", "route.pause", "codex|", {}, NOT_ASKED),
    ("unpause Codex right now", "route.lift", "codex|", {}, PASS),
    ("don't use Codex today", "route.pause", "codex|", {}, PASS),
    ("don't use Codex today", "route.lift", "codex|", {}, NOT_ASKED),
    # every polarity pair, both ways
    ("stop using Codex today", "route.pause", "codex|", {}, PASS),
    ("start using Codex again", "route.lift", "codex|", {}, PASS),
    ("start using Codex again", "route.pause", "codex|", {}, NOT_ASKED),
    ("T-12 and 42 are not the same", "work.not_same", "atlas#T-12|42", {}, PASS),
    ("T-12 and 42 are not the same", "work.same", "atlas#T-12|42", {}, NOT_ASKED),
    ("T-12 and 42 are the same", "work.same", "atlas#T-12|42", {}, PASS),
    ("T-12 and 42 are the same", "work.not_same", "atlas#T-12|42", {}, NOT_ASKED),
    ("don't offer the Codex login again", "accounts.decline", "codex", {}, PASS),
    ("don't offer the Codex login again", "accounts.offer_again", "codex", {}, NOT_ASKED),
    ("offer the Codex login again", "accounts.offer_again", "codex", {}, PASS),
    ("offer the Codex login again", "accounts.decline", "codex", {}, NOT_ASKED),
    ("unhide commitment 42 please", "work.unhide", "42", {}, PASS),
    ("unhide commitment 42 please", "work.hide", "42", {}, NOT_ASKED),
    ("stop tracking Grok for good", "accounts.stop", "grok|", {}, PASS),
    ("stop tracking Grok for good", "accounts.track", "grok|", {"names": [["grok"]]}, NOT_ASKED),
    ("turn off owner direction please", "direction.off", "direction", {}, PASS),
    ("do not turn off owner direction", "direction.off", "direction", {}, NOT_ASKED),
    # priority keeps the owner's order
    ("put Codex before Grok please", "route.priority.set", "codex|,grok|", {}, PASS),
    ("put Codex before Grok please", "route.priority.set", "grok|,codex|", {}, NOT_ASKED),
    ("put Grok after Codex please", "route.priority.set", "codex|,grok|", {}, PASS),
    ("put Grok after Codex please", "route.priority.set", "grok|,codex|", {}, NOT_ASKED),
    ("priority Codex first then Claude second", "route.priority.set", "codex|,claude|/logins/claude-second", {},
     PASS),
    ("priority Codex first then Claude second", "route.priority.set", "claude|/logins/claude-second,codex|", {},
     NOT_ASKED),
    # a timed pause keeps its time, and an untimed one gets none
    ("pause Codex for 5 minutes", "route.pause", "codex|", {"timed": True}, TIME),
    ("pause Codex for 5 minutes", "route.pause", "codex|", {"timed": True, "until": "5m"}, PASS),
    ("pause Codex for 5 minutes", "route.pause", "codex|", {"timed": True, "until": "2h"}, TIME),
    ("pause Codex for 2 hours", "route.pause", "codex|", {"timed": True, "until": "2h"}, PASS),
    ("pause Codex for 2 hours", "route.pause", "codex|", {"timed": True, "until": "20m"}, TIME),
    ("pause Codex for an hour", "route.pause", "codex|", {"timed": True, "until": "1h"}, PASS),
    ("pause Codex for half an hour", "route.pause", "codex|", {"timed": True, "until": "30m"}, PASS),
    ("pause Codex for a day", "route.pause", "codex|", {"timed": True, "until": "1d"}, PASS),
    ("pause Codex until tomorrow", "route.pause", "codex|", {"timed": True}, TIME),
    ("pause Codex until tomorrow", "route.pause", "codex|", {"timed": True, "until": "1d"}, PASS),
    ("pause Codex until 17:30", "route.pause", "codex|", {"timed": True, "until": _next_local(17, 30)}, PASS),
    ("pause Codex till 9am", "route.pause", "codex|", {"timed": True, "until": _next_local(9)}, PASS),
    ("pause Codex until 5pm", "route.pause", "codex|", {"timed": True, "until": _next_local(17)}, PASS),
    ("pause Codex until 5pm", "route.pause", "codex|", {"timed": True, "until": _next_local(17, shift_h=2)}, TIME),
    ("pause Codex for now please", "route.pause", "codex|", {"timed": True, "until": "2h"}, TIME),
    ("pause Codex for now please", "route.pause", "codex|", {"timed": True}, PASS),
    ("pause Grok for 2 hours and lift Codex", "route.lift", "codex|", {}, PASS),
    # a time Khipu can't resolve still needs an end, and any end is taken
    ("pause Codex until tonight", "route.pause", "codex|", {"timed": True}, TIME),
    ("pause Codex until tonight", "route.pause", "codex|", {"timed": True, "until": "6h"}, PASS),
    ("pause Codex until five", "route.pause", "codex|", {"timed": True}, TIME),
    ("pause Codex until five", "route.pause", "codex|", {"timed": True, "until": "3h"}, PASS),
    ("pause Codex for a bit", "route.pause", "codex|", {"timed": True}, TIME),
    ("pause Codex for a bit", "route.pause", "codex|", {"timed": True, "until": "1h"}, PASS),
    ("pause Codex for now", "route.pause", "codex|", {"timed": True}, PASS),
    ("pause Codex for good", "route.pause", "codex|", {"timed": True}, PASS),
]


@pytest.mark.parametrize("words,action,target,extra,expected", TABLE,
                         ids=[f"{r[1]}:{r[2]}:{r[0]}:{r[3].get('until')}" for r in TABLE])
def test_the_words_decide_action_target_and_polarity(world, words, action, target, extra, expected):
    token = world.say(words)
    if expected == PASS:
        stamp = owner_direction.check(action, target, words, turn_token=token, **extra)
        assert stamp["auth"] == "direction"
        return
    with pytest.raises(owner_direction.DirectionRefused) as err:
        owner_direction.check(action, target, words, turn_token=token, **extra)
    assert err.value.code == expected, err.value.message


def test_a_time_refusal_tells_the_agent_to_pass_the_owners_time(world):
    token = world.say("pause Codex for 5 minutes")
    with pytest.raises(owner_direction.DirectionRefused) as err:
        owner_direction.check("route.pause", "codex|", "pause Codex for 5 minutes", turn_token=token, timed=True)
    assert "--until" in err.value.message


# ---- the review's probes (scope findings), same assertions ------------------------------------


@pytest.mark.parametrize("words", ["do not pause Codex for now", "pause Grok and lift Codex please"])
def test_non_direction_cannot_pause_codex(world, words):
    token = world.say(words)
    rc, out = world.run(["route", "pause", "Codex"], words, token)
    assert rc == 2 and _state("codex|") != "paused", out


def test_pause_expiry_cannot_be_omitted(world):
    words = "pause Codex for 5 minutes"
    token = world.say(words)
    rc, out = world.run(["route", "pause", "Codex"], words, token)
    entry = next(a for a in routing.read_view()["accounts"] if a["key"] == "codex|")
    assert rc == 2 or entry.get("until") is not None, out


def test_priority_order_cannot_reverse_direction(world):
    words = "put Codex before Grok please"
    token = world.say(words)
    ok, out = owner_act.act("route.priority.set", {"entries": [{"account": "Grok"}, {"account": "Codex"}],
                                                   "expect": routing.priority_version()}, words, token)
    priority = routing.read_view().get("priority")
    assert not ok or priority["entries"][0]["account"] == "codex|", out


def test_unrelated_yes_cannot_confirm_widening(world):
    words = "make the latest window 12 hours"
    token = world.say(words)
    rc, first = world.run(["owner-direction", "set", "latest_window_h", "12"], words, token)
    assert rc == 2 and first["code"] == "needs_second_turn"
    words = "yes, run those tests please"
    token = world.say(words)
    rc, out = world.run(["owner-direction", "set", "latest_window_h", "12"], words, token)
    assert rc == 2 and owner_turns.settings()["latest_window_h"] == 6, out


def test_untrust_words_cannot_schedule_trust(world):
    words = "stop trusting claude_code:sdk-cli please"
    token = world.say(words)
    rc, out = world.run(["owner-direction", "set", "trust", "claude_code:sdk-cli"], words, token)
    assert rc == 2 and out["code"] == "words_do_not_ask_for_this", out


@pytest.mark.parametrize("argv,words,key,value", [
    (["owner-direction", "off"], "turn owner direction off please", "enabled", False),
    (["owner-direction", "set", "latest_window_h", "2"], "make latest window 2 hours", "latest_window_h", 2),
    (["owner-direction", "set", "untrust", "claude_code:cli"], "stop trusting claude_code:cli please", "trusted",
     None),
])
def test_disable_and_tighten_one_turn(world, argv, words, key, value):
    token = world.say(words)
    rc, out = world.run(argv, words, token)
    assert rc == 0, out
    if key == "trusted":
        assert "claude_code:cli" not in owner_turns.settings()[key]
    else:
        assert owner_turns.settings()[key] == value


# ---- a bare yes confirms only the widening it was asked about ------------------------------------


def _widen(world, words="make the latest window 12 hours"):
    token = world.say(words)
    rc, out = world.run(["owner-direction", "set", "latest_window_h", "12"], words, token)
    assert rc == 2 and out["code"] == "needs_second_turn", out


def test_a_yes_after_another_owner_turn_does_not_confirm(world):
    _widen(world)
    world.say("what does that window mean")
    yes = world.say("yes")
    rc, out = world.run(["owner-direction", "set", "latest_window_h", "12"], "yes", yes)
    assert rc == 2 and owner_turns.settings()["latest_window_h"] == 6, out


def test_a_yes_with_two_widenings_waiting_is_refused(world):
    token = world.say("make the latest window 12 hours and the earlier window 60")
    for argv in (["latest_window_h", "12"], ["earlier_window_min", "60"]):
        rc, out = world.run(["owner-direction", "set", *argv], "make the latest window 12 hours and the earlier "
                            "window 60", token)
        assert out["code"] == "needs_second_turn", out
    yes = world.say("yes")
    rc, out = world.run(["owner-direction", "set", "latest_window_h", "12"], "yes", yes)
    assert rc == 2 and owner_turns.settings()["latest_window_h"] == 6, out


def test_a_yes_that_names_the_widening_confirms_it(world):
    _widen(world)
    words = "yes, make the latest window 12 hours"
    yes = world.say(words)
    rc, out = world.run(["owner-direction", "set", "latest_window_h", "12"], words, yes)
    assert rc == 0 and owner_turns.settings()["latest_window_h"] == 12, out
