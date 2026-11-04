"""Conversation overrides and the priority walk-through, through the real
owner-direction path: the prompt hook records the owner's turn, the agent's
``khipu_owner_act`` call acts on it (or holds it for one yes), the receipt and
undo come back, and a fictional record is cleaned up one item at a time."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from khipu import cli, conversation_overrides as overrides, priority_cleanup, routing
from tests.test_owner_direction_e2e import SESSION, _act, _hook, world as _world  # noqa: F401

world = _world

pytestmark = pytest.mark.usefixtures("world")


@pytest.fixture(autouse=True)
def _no_t3(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_T3_DB", str(tmp_path / "no-t3.sqlite"))


def _in(hours: float) -> str:
    return routing._iso(routing._now() + timedelta(hours=hours))


def _mine() -> list[dict]:
    return overrides.list_overrides()


def test_one_yes_grants_an_override_with_a_receipt_and_an_undo(world, khipu_app):
    _hook("let this conversation run ahead of pace on Codex for 3 hours")
    bad, out = _act("route.override.grant", {"account": "Codex", "until": _in(3), "request": "run ahead on Codex"})
    assert not bad and out["ok"] is True, out
    (row,) = _mine()
    assert row["subject"]["kind"] == "session" and row["subject"]["id"] == SESSION
    assert out["id"] == row["id"] and out["owner_action"]
    # The receipt names what happened and how to take it back.
    assert out["receipt"].startswith("Let this conversation run ahead of pace on")
    assert f"khipu owner-direction undo {out['owner_action']}" in out["receipt"]
    # This conversation hears it; the read shows only its own.
    bad, mine = _act("route.overrides.mine", {})
    assert not bad and [o["id"] for o in mine["overrides"]] == [row["id"]]
    # Undo (the app's button) takes it back.
    khipu_app.as_app()
    assert cli.main(["owner-direction", "undo", "--from-app", "--", out["owner_action"]]) == 0
    assert _mine() == []


def test_nobody_else_is_told_about_it(world):
    routing.report(account="Claude", state="open",
                   author={"kind": "conversation", "harness": "codex", "project": "p", "session": "s-other"})
    marker = routing.prompt_push(None, budget=5.0)
    seen = {"version": marker["version"], "epoch": marker["epoch"], "full_at": routing._iso(routing._now())}
    _hook("let this conversation run ahead on Codex for 3 hours")
    assert not _act("route.override.grant", {"account": "Codex", "until": _in(3), "request": "x"})[0]
    pushed = routing.prompt_push(seen, budget=5.0)
    assert "override" not in json.dumps(pushed).lower()


def test_ending_early_takes_one_yes_and_can_be_undone(world, khipu_app):
    _hook("let this conversation run ahead on Codex for 3 hours")
    granted = _act("route.override.grant", {"account": "Codex", "until": _in(3), "request": "x"})[1]
    _hook("that's enough, end it")
    bad, ended = _act("route.override.end", {"override": granted["id"]})
    assert not bad and ended["ok"] is True and _mine() == [], ended
    khipu_app.as_app()
    assert cli.main(["owner-direction", "undo", "--from-app", "--", ended["owner_action"]]) == 0
    assert [r["id"] for r in _mine()] == [granted["id"]]


def test_a_second_grant_replaces_and_undo_brings_the_first_back(world, khipu_app):
    _hook("run ahead on Codex for 3 hours")
    first = _act("route.override.grant", {"account": "Codex", "until": _in(3), "request": "x"})[1]
    _hook("make it 5 hours")
    second = _act("route.override.grant", {"account": "Codex", "until": _in(5), "request": "x"})[1]
    assert [r["id"] for r in _mine()] == [second["id"]]
    khipu_app.as_app()
    assert cli.main(["owner-direction", "undo", "--from-app", "--", second["owner_action"]]) == 0
    assert [r["id"] for r in _mine()] == [first["id"]]


def test_the_ask_for_a_held_grant_is_plain_and_retries_are_one_request(world):
    _hook("run ahead on Codex for 3 hours")
    args = {"action": "route.override.grant", "args": {"account": "Codex", "until": _in(3), "request": "x"}}
    first = _act("direction.request", args)[1]
    assert first["pending"] is True
    ask = first["ask"]
    assert ask.startswith("Let this conversation run ahead of pace on Codex until ")
    assert "route.override" not in ask and "{" not in ask
    assert "stopping at the account's saved share cap if it has one, otherwise at its plan limit" in ask
    assert "hold back until pace recovers" in ask
    for _ in range(4):
        again = _act("direction.request", args)[1]
        assert again["request"] == first["request"]
    # The owner's one yes applies it, as the ask showed it.
    _hook("yes")
    bad, done = _act("direction.confirm", {"request": first["request"]})
    assert not bad and done["ok"] is True, done
    assert len(_mine()) == 1


def test_the_conversation_is_named_by_khipu_never_by_the_call(world):
    _hook("run ahead on Codex for 3 hours")
    bad, out = _act("route.override.grant", {"account": "Codex", "until": _in(3), "request": "x",
                                             "subject": {"kind": "session", "id": "someone-else"}})
    assert bad and out["code"] == "bad_args"
    assert _mine() == []


def test_a_job_with_no_owner_turn_can_not_grant(world, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "never-typed")
    bad, out = _act("route.override.grant", {"account": "Codex", "until": _in(3), "request": "x"})
    assert bad and out["code"] == "no_recorded_turn" and _mine() == []


def test_with_no_end_it_asks_again_in_a_week(world):
    _hook("let this run ahead on Codex until I say otherwise")
    out = _act("route.override.grant", {"account": "Codex", "indefinite": True, "request": "x"})[1]
    (row,) = _mine()
    assert row.get("indefinite") is True and out["review_at"] == row["review_at"]
    assert routing._parse(row["review_at"]) - routing._now() > timedelta(days=6)


# ---- the walk-through ------------------------------------------------------------------------


def _exception(i: str, text: str, hours: float, **extra) -> dict:
    return {"id": i, "text": text, "until": _in(hours), "at": routing._iso(routing._now() - timedelta(hours=1)),
            "by": "owner", "source": "test", **extra}


@pytest.fixture
def messy(world):
    """A fictional priority that already holds the mess a retry loop leaves:
    one instruction four times, a near copy, and the same words on two accounts."""
    words = "re-check of PRs #3 and #11"
    entries = [
        {"account": "codex|", "label": "Codex", "role": "builds",
         "exceptions": [_exception("xaa111", words, 5), _exception("xbb222", words.upper() + ".", 6),
                        _exception("xcc333", "re-check of PRs #3 and #11 please", 7),
                        _exception("xdd444", words, 8)]},
        {"account": "claude|", "label": "Claude", "role": "designs",
         "exceptions": [_exception("xee555", words, 5)]},
    ]
    routing._owner_write({"type": "priority_set", "entries": entries, "until": None}, "app")
    return entries


def test_the_walk_goes_one_item_at_a_time_in_plain_words_one_yes_each(messy):
    _hook("walk me through my routing")
    bad, walk = _act("route.walkthrough", {})
    assert not bad and walk["total"] >= 3, walk
    item = walk["item"]
    assert item["code"] == "duplicate" and "twice" in item["words"] and "re-check of PRs #3 and #11" in item["words"]
    assert "route." not in walk["line"] and "{" not in walk["line"]
    codes = []
    for _ in range(10):
        bad, walk = _act("route.walkthrough", {})
        item = walk["item"]
        if item is None:
            break
        codes.append(item["code"])
        confirm = item["settle"]["confirm"]
        if confirm is None:  # twins: the yes keeps the pair
            _act("route.walkthrough", {"keep": item["key"]})
            continue
        _hook("yes")
        bad, done = _act(confirm["action"], confirm["args"])
        assert not bad and done["ok"] is True, (item, done)
    assert walk["item"] is None and walk["line"] == "Nothing to clean up."
    assert "duplicate" in codes and "twin" in codes
    # What was left: one copy of the instruction on Codex, nothing stacked.
    codex = next(e for e in routing.read_view()["priority"]["entries"] if e["account"] == "codex|")
    assert len(codex["exceptions"]) == 1


def test_a_walk_can_be_left_and_resumed(messy):
    _hook("walk me through it")
    first = _act("route.walkthrough", {})[1]["item"]
    after_leave = _act("route.walkthrough", {"leave": first["key"]})[1]
    assert after_leave["item"]["key"] != first["key"] and after_leave["left_for_later"] == 1
    # A new session of the same walk (nothing in memory but the bookmark file) picks up where it was.
    resumed = _act("route.walkthrough", {})[1]
    assert resumed["item"]["key"] == after_leave["item"]["key"]
    assert priority_cleanup._state()["left"].keys() == {first["key"]}
    restarted = _act("route.walkthrough", {"restart": True})[1]
    assert restarted["item"]["key"] == first["key"] and restarted["left_for_later"] == 0


def test_a_conversation_the_owner_is_not_typing_in_sees_no_items(messy, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "never-typed")
    bad, out = _act("route.walkthrough", {})
    assert not bad and out["available"] is False and "item" not in out


def test_the_same_instruction_again_writes_nothing_and_names_the_existing_one(world):
    routing._owner_write({"type": "priority_set", "entries": [{"account": "codex|", "label": "Codex", "role": "r"}],
                          "until": None}, "app")
    ids = []
    for words in ("re-check PRs #3 and #11", "Re-check PRs #3 and #11.", "re-check  PRs #3 and #11", "RE-CHECK PRs #3, and #11"):
        out = routing.amend_priority("Codex", exception=words, until=_in(5), via="app",
                                     expect=routing.priority_version())
        ids.append(out["exception_id"])
    assert len(set(ids)) == 1
    codex = next(e for e in routing.read_view()["priority"]["entries"] if e["account"] == "codex|")
    assert len(codex["exceptions"]) == 1


def test_a_near_copy_is_refused_naming_the_match_but_different_numbers_are_not(world):
    routing._owner_write({"type": "priority_set", "entries": [{"account": "codex|", "label": "Codex", "role": "r"}],
                          "until": None}, "app")
    routing.amend_priority("Codex", exception="re-check of PRs #3 and #11 before the release goes out today",
                           until=_in(5), via="app", expect=routing.priority_version())
    with pytest.raises(routing.RouteError, match="reads almost the same"):
        routing.amend_priority("Codex", exception="re-check of PRs #3 and #11 before the release goes out today please",
                               until=_in(5), via="app", expect=routing.priority_version())
    routing.amend_priority("Codex", exception="re-check of PRs #3 and #12 before the release goes out today",
                           until=_in(5), via="app", expect=routing.priority_version())
    codex = next(e for e in routing.read_view()["priority"]["entries"] if e["account"] == "codex|")
    assert len(codex["exceptions"]) == 2


def test_overrides_that_no_longer_make_sense_are_offered_for_ending(world):
    routing.report(account="Codex", state="down", author={"kind": "conversation", "harness": "codex", "project": "p",
                                                             "session": "s-other"})
    _hook("run ahead on Codex")
    granted = _act("route.override.grant", {"account": "Codex", "until": _in(3), "request": "x"})[1]
    found = [i for i in priority_cleanup.items(routing.read_view(), routing._now()) if i.get("override") == granted["id"]]
    assert [i["code"] for i in found] == ["closed_account"]
    assert found[0]["settle"]["confirm"] == {"action": "route.override.end", "args": {"override": granted["id"]}}


def test_only_the_app_lists_every_override_and_in_plain_words(world, khipu_app, capsys):
    _hook("run ahead on Codex for 3 hours")
    assert not _act("route.override.grant", {"account": "Codex", "until": _in(3), "request": "x"})[0]
    capsys.readouterr()
    assert cli.main(["route", "override", "list", "--from-app"]) == 2  # not started by the app
    assert "Only the Khipu app" in capsys.readouterr().out
    khipu_app.as_app()
    assert cli.main(["route", "override", "list", "--from-app"]) == 0
    (row,) = json.loads(capsys.readouterr().out)["overrides"]
    assert row["label"] == "Codex" and row["who"] == "This conversation" and row["review_due"] is False
    assert row["cap"].startswith("Stopping at the account's saved share cap")
    # The app's own one-click end, with the line Undo works from.
    assert cli.main(["route", "override", "end", row["id"], "--from-app"]) == 0
    ended = json.loads(capsys.readouterr().out)
    assert ended["ok"] is True and ended["owner_action"] and _mine() == []
