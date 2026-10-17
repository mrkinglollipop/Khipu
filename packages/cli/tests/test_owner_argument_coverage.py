"""Owner direction: argument coverage (docs/plans/2026-10-16-owner-direction.md, section 2.3 step 4).

An owner direction authorises a structured action only when every argument of that action (the
action itself, what it acts on, the project, the field, the value, the end, the order) is stated
in the owner's own words, in the clause that asks for it. ``owner_direction.arguments`` lists
them for every owner action and ``_read`` checks each one. The third review's probes and their
mirror cases are the table here; selecting one of several waiting widenings by name is tested
after it.

Hermetic: the ``world`` fixture from tests/test_owner_direction.py (temp data and capture
folders, turns recorded the way the prompt hook records them)."""
from __future__ import annotations

import re

import pytest

from khipu import ledger, owner_direction, owner_turns
from tests import test_owner_direction as base

world = base.world  # the fixture, shared
CASES = base.CASES

PASS = "pass"
NOT_ASKED = "words_do_not_ask_for_this"
SECOND = "needs_second_turn"
REFUSED = (NOT_ASKED, "not_the_owners_words", "time_not_the_owners")


def _arguments(action: str, target: str, extra: dict) -> list[dict]:
    keep = {k: v for k, v in extra.items() if k in ("names", "stored", "fields", "project", "timed", "switch")}
    return owner_direction.arguments(action, target, **keep)[0]


# ---- every action, every argument ------------------------------------------------------------


def test_every_action_declares_its_arguments():
    assert set(CASES) == set(owner_direction.ACTIONS)
    for action, (target, _good, _bad, extra) in CASES.items():
        args = _arguments(action, target, extra)
        kinds = [a["kind"] for a in args]
        assert kinds[0] == "action" and set(kinds) <= set(owner_direction.ARGUMENT_KINDS), (action, kinds)
        assert {"target", "value"} & set(kinds), f"{action} acts on nothing the words must name"


def _drop(words: str, phrases) -> str:
    for phrase in sorted({p for p in phrases if p}, key=len, reverse=True):
        words = re.sub(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", " ", words, flags=re.IGNORECASE)
    return " ".join(words.split())


DROPPED = ("action", "target", "project", "field", "value")


def _mutations():
    # Which arguments to drop is known here; their names are read inside the
    # test, where the world's accounts are.
    for action, (target, _good, _bad, extra) in sorted(CASES.items()):
        for i, arg in enumerate(_arguments(action, target, extra)):
            if arg["kind"] in DROPPED:
                yield pytest.param(action, i, id=f"{action}:{i}:{arg['kind']}")


@pytest.mark.parametrize("action,i", list(_mutations()))
def test_dropping_any_stated_argument_is_refused(world, action, i):
    """The enumerating test: for every owner action, its passing words with
    one argument taken out are refused."""
    target, good, _bad, extra = CASES[action]
    arg = _arguments(action, target, extra)[i]
    if arg["kind"] == "value":
        phrases = [arg["value"]]
    else:
        phrases = [p.replace("+", " ") for p in arg["names"]] + [p.split("+")[0] for p in arg["names"]]
    words = _drop(good, phrases)
    assert words != _drop(good, []), f"{action}: the passing words never stated {phrases}"
    session = f"s-{action}-drop"
    world.be(session)
    token = world.say(words + " please", session=session)
    with pytest.raises(owner_direction.DirectionRefused) as err:
        owner_direction.check(action, target, words + " please", turn_token=token, **extra)
    assert err.value.code in REFUSED, err.value.message


# ---- the third review's probes, with mirror cases ----------------------------------------------

STATE = {"stored": ["done"], "fields": {"state": "done"}}
TITLE = {"stored": ["done"], "fields": {"title": "done"}}

# (words, action, target, extra check arguments, expected: PASS or a refusal code)
TABLE = [
    # P1 wrong project: words naming atlas never reach another project's same-numbered row
    ("remove T-12 from atlas ledger please", "ledger.remove", "other-project#T-12", {}, NOT_ASKED),
    ("restore T-12 in atlas ledger please", "ledger.restore", "other-project#T-12", {}, NOT_ASKED),
    ("purge T-12 from atlas ledger please", "ledger.purge", "other-project#T-12", {}, NOT_ASKED),
    ("mark T-12 done in atlas ledger please", "ledger.set", "other-project#T-12", STATE, NOT_ASKED),
    ("remove T-12 from atlas ledger please", "ledger.remove", "atlas#T-12", {}, PASS),
    ("restore T-12 in atlas ledger please", "ledger.restore", "atlas#T-12", {}, PASS),
    ("purge T-12 from atlas ledger please", "ledger.purge", "atlas#T-12", {}, PASS),
    ("mark T-12 done in atlas ledger please", "ledger.set", "atlas#T-12", STATE, PASS),
    ("remove T-12 from the ledger please", "ledger.remove", "atlas#T-12", {}, NOT_ASKED),
    ("in the atlas ledger, remove T-12 please", "ledger.remove", "atlas#T-12", {}, PASS),
    ("atlas T-12 and 42 are the same", "work.same", "other-project#T-12|42", {}, NOT_ASKED),
    ("import tasks.md into atlas please", "ledger.import", "tasks.md", {"project": "atlas"}, PASS),
    ("import tasks.md into atlas please", "ledger.import", "tasks.md", {"project": "other-project"}, NOT_ASKED),
    # P1 wrong field: "set title to done" never changes the state
    ("set T-12 title to done in atlas please", "ledger.set", "atlas#T-12", STATE, NOT_ASKED),
    ("set T-12 title to done in atlas please", "ledger.set", "atlas#T-12", TITLE, PASS),
    ("set the T-12 status to done in atlas", "ledger.set", "atlas#T-12", STATE, PASS),
    ("mark T-12 done in atlas with the title ship it", "ledger.set", "atlas#T-12",
     {"stored": ["done", "ship it"], "fields": {"state": "done", "title": "ship it"}}, PASS),
    ("mark T-12 done in atlas please", "ledger.set", "atlas#T-12",
     {"stored": ["done", "ship it"], "fields": {"state": "done", "title": "ship it"}}, NOT_ASKED),
    # P2 two settings sharing a verb: each with its own value, never the other's
    ("make the latest window 12 hours and the earlier window 60 minutes", "direction.set", "latest_window_h",
     {"stored": ["12"], "widening": True}, SECOND),
    ("make the latest window 12 hours and the earlier window 60 minutes", "direction.set", "earlier_window_min",
     {"stored": ["60"], "widening": True}, SECOND),
    ("make the latest window 60 minutes and the earlier window 12 hours", "direction.set", "latest_window_h",
     {"stored": ["12"], "widening": True}, NOT_ASKED),
    ("make the latest window 12 hours and the earlier window 60 minutes", "direction.set", "earlier_window_min",
     {"stored": ["12"], "widening": True}, NOT_ASKED),
    ("make the latest window 2 hours and look at the earlier window", "direction.set", "earlier_window_min",
     {"stored": ["2"]}, NOT_ASKED),
    # a front end is two arguments, its harness and its entry point
    ("start trusting claude_code:sdk-cli now", "direction.set", "trust claude_code:sdk-cli", {"widening": True},
     SECOND),
    ("start trusting sdk-cli now please", "direction.set", "trust claude_code:sdk-cli", {"widening": True},
     NOT_ASKED),
    ("start trusting T3 in Claude Code now", "direction.set", "trust claude_code:t3", {"widening": True}, SECOND),
    ("start trusting T3 in Claude Code now", "direction.set", "trust codex:t3", {"widening": True}, NOT_ASKED),
    # the duration and the value stay arguments too
    ("pause Codex for 2 hours please", "route.pause", "codex|", {"timed": True, "until": "2h"}, PASS),
    ("pause Codex for 2 hours please", "route.pause", "codex|", {"timed": True}, "time_not_the_owners"),
    ("set stale after to 6h please", "config.routing_setting", "routing_stale_after", {"stored": ["8h"]}, NOT_ASKED),
]


@pytest.mark.parametrize("words,action,target,extra,expected", TABLE,
                         ids=[f"{r[1]}:{r[2]}:{r[0]}" for r in TABLE])
def test_probes_and_mirrors(world, words, action, target, extra, expected):
    token = world.say(words)
    if expected == PASS:
        assert owner_direction.check(action, target, words, turn_token=token, **extra)["auth"] == "direction"
        return
    with pytest.raises(owner_direction.DirectionRefused) as err:
        owner_direction.check(action, target, words, turn_token=token, **extra)
    assert err.value.code == expected, err.value.message


def _fake_row(monkeypatch) -> list:
    monkeypatch.setattr(ledger, "get", lambda p, r: {"row": {"state": "open", "owner": "old", "title": "old",
                                                             "evidence": "", "link": ""}})
    writes: list = []
    monkeypatch.setattr(ledger, "write", lambda op: writes.append(op) or {"ok": True, "line": "updated"})
    return writes


def test_wrong_project_never_reaches_the_ledger_write(world, monkeypatch):
    writes = _fake_row(monkeypatch)
    words = "mark T-12 done in atlas ledger please"
    rc, out = world.run(["ledger", "set", "T-12", "--project", "other-project", "--state", "done", "--json"], words,
                        world.say(words))
    assert rc != 0 and not writes, (out, writes)
    assert "which project (other-project)" in out.get("error", "")
    rc, out = world.run(["ledger", "set", "T-12", "--project", "atlas", "--state", "done", "--json"], words,
                        world.say(words))
    assert rc == 0 and [w["project"] for w in writes] == ["atlas"], (out, writes)


def test_set_title_to_done_never_changes_the_state(world, monkeypatch):
    writes = _fake_row(monkeypatch)
    words = "set T-12 title to done in atlas please"
    rc, out = world.run(["ledger", "set", "T-12", "--project", "atlas", "--state", "done", "--json"], words,
                        world.say(words))
    assert rc != 0 and not writes, (out, writes)
    assert "which field to change (state)" in out.get("error", "")
    rc, out = world.run(["ledger", "set", "T-12", "--project", "atlas", "--title", "done", "--json"], words,
                        world.say(words))
    assert rc == 0 and writes[-1]["fields"]["title"] == "done" and writes[-1]["fields"].get("state") is None, out


# ---- several waiting widenings: a confirmation that names one selects it -----------------------


def _two_waiting(world, words="make the latest window 12 hours and make the earlier window 60 minutes"):
    token = world.say(words)
    for key, value in (("latest_window_h", "12"), ("earlier_window_min", "60")):
        rc, out = world.run(["owner-direction", "set", key, value], words, token)
        assert rc == 2 and out["code"] == SECOND, out


def test_inherited_verb_asks_for_both_settings(world):
    _two_waiting(world, "make the latest window 12 hours and the earlier window 60 minutes")


def test_a_named_confirmation_selects_one_waiting_widening(world):
    _two_waiting(world)
    words = "yes, make the earlier window 60 minutes"
    rc, out = world.run(["owner-direction", "set", "earlier_window_min", "60"], words, world.say(words))
    assert rc == 0 and owner_turns.settings()["earlier_window_min"] == 60, out
    assert owner_turns.settings()["latest_window_h"] == 6


def test_a_named_confirmation_does_not_confirm_the_other(world):
    _two_waiting(world)
    words = "yes, make the earlier window 60 minutes"
    rc, out = world.run(["owner-direction", "set", "latest_window_h", "12"], words, world.say(words))
    assert rc == 2 and owner_turns.settings()["latest_window_h"] == 6, out


def test_a_bare_yes_with_two_waiting_asks_which(world):
    _two_waiting(world)
    rc, out = world.run(["owner-direction", "set", "earlier_window_min", "60"], "yes", world.say("yes"))
    assert rc == 2 and out["code"] == SECOND and "which one" in out["error"], out
    assert owner_turns.settings()["earlier_window_min"] == 30


def test_yes_to_trusting_t3_selects_that_widening(world):
    owner_turns.write_settings({"trusted": [t for t in owner_turns.settings()["trusted"] if t != "claude_code:t3"]})
    words = "trust claude_code:t3 and make the latest window 12 hours"
    token = world.say(words)
    for argv in (["trust", "claude_code:t3"], ["latest_window_h", "12"]):
        rc, out = world.run(["owner-direction", "set", *argv], words, token)
        assert rc == 2 and out["code"] == SECOND, out
    words = "yes to trusting T3"
    rc, out = world.run(["owner-direction", "set", "trust", "claude_code:t3"], words, world.say(words))
    assert rc == 0 and "claude_code:t3" in owner_turns.settings()["trusted"], out
    assert owner_turns.settings()["latest_window_h"] == 6


def test_a_named_confirmation_with_another_number_is_refused(world):
    _two_waiting(world)
    words = "yes, make the earlier window 90 minutes"
    rc, out = world.run(["owner-direction", "set", "earlier_window_min", "60"], words, world.say(words))
    assert rc == 2 and owner_turns.settings()["earlier_window_min"] == 30, out
