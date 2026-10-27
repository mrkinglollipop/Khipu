"""Undo refuses over a newer change to the same target (Matt, 2026-10-27).

An Undo puts back what its action's snapshot read. When a later change (an
owner action, or another write in the routing record such as a conversation's
proposal) touched the same target since, the Undo is refused with the newer
change named and nothing changed; ``--over-newer`` (the app's second confirm,
``over_newer`` in ``khipu_owner_act``) restores anyway and is logged as its
own owner action. A change to a different target never blocks.

Hermetic: the ``world`` fixture from tests/test_owner_direction.py (temp data
folder, routing record and owner-actions log written fresh per test)."""
from __future__ import annotations

import json

from khipu import owner_act, owner_direction, routing
from tests import test_owner_direction as base

world = base.world  # the fixture, shared

REPORTER = {"kind": "conversation", "harness": "codex", "project": "p", "session": "reporter"}


def _undo(world, line_id: str, *extra: str) -> tuple[int, dict]:
    return world.run(["owner-direction", "undo", line_id, "--from-app", *extra])


def _app(world, khipu_app, argv: list[str]) -> dict:
    khipu_app.as_app()
    rc, out = world.run([*argv, "--from-app"])
    assert rc == 0, out
    return out


def _refused(rc: int, out: dict, newer_id: str | None = None) -> None:
    assert rc == 2 and out["code"] == "newer_change", out
    assert "Not undone" in out["error"] and "nothing was changed" in out["error"], out
    assert out["newer"]["at"] and out["newer"]["at"] in out["error"], out
    if newer_id:
        assert out["newer"]["id"] == newer_id and newer_id in out["error"], out


def _undo_lines() -> list[dict]:
    return [x for x in owner_direction.read_log() if x.get("kind") == "undo" and x.get("state") == "pending"]


# ---- priority ---------------------------------------------------------------------------------


def _set_priority(world, khipu_app, role: str) -> dict:
    return _app(world, khipu_app, ["route", "priority", "set", "--entries=" + json.dumps(
        [{"account": "Codex", "role": role}]), "--expect=" + routing.priority_version()])


def _role() -> str:
    return routing.read_view()["priority"]["entries"][0]["role"]


def test_priority_undo_refuses_over_a_newer_priority_change_then_restores_over_it(world, khipu_app):
    first = _set_priority(world, khipu_app, "first role")
    second = _set_priority(world, khipu_app, "second role")
    events_before = len(routing.read_events()[0])
    rc, out = _undo(world, first["owner_action"])
    _refused(rc, out, second["owner_action"])
    assert "same priority" in out["error"]
    assert _role() == "second role" and len(routing.read_events()[0]) == events_before
    assert not _undo_lines(), "a refused undo writes no undo line"
    rc, out = _undo(world, first["owner_action"], "--over-newer")
    assert rc == 0, out
    assert "priority" not in routing.read_view() or routing.read_view()["priority"] is None
    (line,) = _undo_lines()
    assert line["undoes"] == first["owner_action"]
    assert line["facts"]["over_newer"] is True and line["facts"]["newer"] == second["owner_action"]


def test_priority_undo_refuses_over_a_conversations_later_proposal(world, khipu_app):
    first = _set_priority(world, khipu_app, "owner role")
    shown = routing.conversation_view()
    proposal = routing.request_priority([{"account": "Codex", "role": "proposed role"}], owner_request="the owner asked",
                                        author=dict(REPORTER), via="mcp", expect=routing.priority_version(),
                                        base=shown["priority_owner_revision"])
    assert proposal["ok"] is True, proposal
    rc, out = _undo(world, first["owner_action"])
    _refused(rc, out, proposal["id"])
    assert "a conversation's later priority change" in out["error"]


def test_priority_undo_of_the_newest_change_still_works(world, khipu_app):
    _set_priority(world, khipu_app, "first role")
    second = _set_priority(world, khipu_app, "second role")
    rc, out = _undo(world, second["owner_action"])
    assert rc == 0 and _role() == "first role", out


def test_a_newer_change_that_was_itself_undone_does_not_block(world, khipu_app):
    first = _set_priority(world, khipu_app, "first role")
    second = _set_priority(world, khipu_app, "second role")
    rc, out = _undo(world, second["owner_action"])
    assert rc == 0, out
    rc, out = _undo(world, first["owner_action"])
    assert rc == 0, out
    assert not routing.read_view().get("priority")


# ---- caps -------------------------------------------------------------------------------------


def _cap(world, khipu_app, account: str, pct: str, kind: str = "share") -> dict:
    return _app(world, khipu_app, ["route", "cap", "set", account, "--window", "weekly", "--kind", kind,
                                   "--pct", pct])


def _caps(label: str) -> list[dict]:
    return next(a for a in routing.read_view()["accounts"] if a["label"] == label).get("caps") or []


def test_cap_undo_refuses_over_a_newer_change_to_the_same_slot(world, khipu_app):
    first = _cap(world, khipu_app, "Codex", "30")
    khipu_app.as_app()
    rc, removed = world.run(["route", "cap", "remove", "Codex", "--window", "weekly", "--kind", "share",
                             "--from-app"])
    assert rc == 0, removed
    rc, out = _undo(world, first["owner_action"])
    _refused(rc, out, removed["owner_action"])
    assert "same cap" in out["error"] and _caps("Codex") == []
    rc, out = _undo(world, first["owner_action"], "--over-newer")
    assert rc == 0 and _caps("Codex") == [], out


def test_cap_undo_ignores_a_cap_on_another_slot_or_account(world, khipu_app):
    first = _cap(world, khipu_app, "Codex", "30")
    _cap(world, khipu_app, "Grok", "40")
    _app(world, khipu_app, ["route", "cap", "set", "Codex", "--window", "five_hour", "--kind", "share",
                            "--pct", "50"])
    rc, out = _undo(world, first["owner_action"])
    assert rc == 0, out
    assert [c.get("window") for c in _caps("Codex")] == ["five_hour"]
    assert len(_caps("Grok")) == 1


# ---- rules ------------------------------------------------------------------------------------


def test_rule_undo_refuses_over_a_newer_edit_of_the_same_rule(world, khipu_app):
    added = _app(world, khipu_app, ["route", "rule", "add", "Codex reviews"])
    edited = _app(world, khipu_app, ["route", "rule", "edit", "1", "Codex reviews twice"])
    rc, out = _undo(world, added["owner_action"])
    _refused(rc, out, edited["owner_action"])
    assert "same rule" in out["error"]
    assert [r["text"] for r in routing.read_view()["rules"]] == ["Codex reviews twice"]
    rc, out = _undo(world, added["owner_action"], "--over-newer")
    assert rc == 0, out
    assert not [r for r in routing.read_view()["rules"] if not r.get("retired")], routing.read_view()["rules"]


def test_rule_edit_undo_refuses_over_a_conversations_later_proposal(world, khipu_app):
    routing.add_rule("Codex reviews", via="app")
    edited = _app(world, khipu_app, ["route", "rule", "edit", "1", "Codex reviews twice"])
    shown = routing.conversation_view()
    asked = routing.request_rule("Codex reviews three times", n=1, owner_request="the owner asked",
                                 author=dict(REPORTER), via="mcp", base=shown["rules"][0]["owner_revision"])
    assert asked["ok"] is True, asked
    rc, out = _undo(world, edited["owner_action"])
    _refused(rc, out)
    assert "a conversation's later rule edit change" in out["error"]


def test_rule_undo_ignores_an_edit_of_another_rule(world, khipu_app):
    routing.add_rule("Codex reviews", via="app")
    routing.add_rule("Grok surveys", via="app")
    edited = _app(world, khipu_app, ["route", "rule", "edit", "1", "Codex reviews twice"])
    _app(world, khipu_app, ["route", "rule", "edit", "2", "Grok surveys twice"])
    rc, out = _undo(world, edited["owner_action"])
    assert rc == 0, out
    assert sorted(r["text"] for r in routing.read_view()["rules"]) == ["Codex reviews", "Grok surveys twice"]


# ---- holds ------------------------------------------------------------------------------------


def test_hold_undo_refuses_once_the_hold_was_ended_since(world, khipu_app):
    added = _app(world, khipu_app, ["route", "hold", "wait for Codex", "--account", "Codex", "--reason", "keep"])
    hold_id = owner_direction._find(added["owner_action"])["after"]["id"]
    ended = _app(world, khipu_app, ["route", "hold", "end", hold_id])
    rc, out = _undo(world, added["owner_action"])
    _refused(rc, out, ended["owner_action"])
    assert "same hold" in out["error"]


def test_hold_undo_ignores_another_hold(world, khipu_app):
    added = _app(world, khipu_app, ["route", "hold", "wait for Codex", "--account", "Codex", "--reason", "keep"])
    other = _app(world, khipu_app, ["route", "hold", "wait for Grok", "--account", "Grok", "--reason", "keep"])
    _app(world, khipu_app, ["route", "hold", "end", owner_direction._find(other["owner_action"])["after"]["id"]])
    rc, out = _undo(world, added["owner_action"])
    assert rc == 0, out
    assert routing.read_view()["holds"] == []


def test_a_new_hold_like_the_ended_one_does_not_block_bringing_it_back(world, khipu_app):
    hold = routing.add_hold("conversation hold", scope="account", account="Codex", reason="keep",
                            author=dict(REPORTER), via="mcp")
    ended = _app(world, khipu_app, ["route", "hold", "end", hold["id"]])
    again = routing.add_hold("conversation hold", scope="account", account="Codex", reason="keep",
                             author=dict(REPORTER), via="mcp")
    assert again["ok"] is True
    # A different hold (its own id) is not the one the Undo brings back.
    rc, out = _undo(world, ended["owner_action"])
    assert rc == 0, out
    assert len(routing.read_view()["holds"]) == 2


# ---- accounts ---------------------------------------------------------------------------------


def _rename(world, khipu_app, key: str, name: str) -> dict:
    from khipu import config

    return _app(world, khipu_app, ["accounts", "rename", "--account", key, "--name", name,
                                   "--expect=" + config.accounts_version()])


def test_account_rename_undo_refuses_over_a_newer_rename_of_that_account(world, khipu_app):
    first = _rename(world, khipu_app, "codex|", "Builder")
    second = _rename(world, khipu_app, "codex|", "Reviewer")
    rc, out = _undo(world, first["owner_action"])
    _refused(rc, out, second["owner_action"])
    assert "same account" in out["error"]


def test_account_rename_undo_ignores_a_rename_of_another_account(world, khipu_app):
    # (Renaming back to a label that became an account id is refused by the
    # accounts list itself, so this reads the check directly.)
    first = _rename(world, khipu_app, "codex|", "Builder")
    _rename(world, khipu_app, "grok|", "Surveyor")
    assert owner_direction.newer_change(owner_direction._find(first["owner_action"])) is None
    _rename(world, khipu_app, "codex|", "Maker")
    assert owner_direction.newer_change(owner_direction._find(first["owner_action"]))["target"] == "account"


# ---- owner_act --------------------------------------------------------------------------------


def test_owner_act_passes_the_override_to_the_undo():
    argv, positional = owner_act.argv("direction.undo", {"id": "a1b2c3d4", "over_newer": True})
    assert argv == ["owner-direction", "undo", "--over-newer"] and positional == ["a1b2c3d4"]
    argv, _ = owner_act.argv("direction.undo", {"id": "a1b2c3d4", "over_newer": False})
    assert "--over-newer" not in argv


def test_owner_act_passes_the_owners_words_to_the_undo():
    argv, _ = owner_act.argv("direction.undo", {"id": "a1b2c3d4", "over_newer": True,
                                                "owner_words": "undo it anyway"})
    assert argv == ["owner-direction", "undo", "--over-newer", "--owner-words=undo it anyway"]


# ---- a moved login: the newer change is on its new key ----------------------------------------


SECOND = "claude|/logins/claude-second"


def _move(world, khipu_app, key: str) -> dict:
    from khipu import config

    folder = world.tmp / "moved-login"
    folder.mkdir()
    return _app(world, khipu_app, ["accounts", "move", f"--account={key}", f"--folder={folder}",
                                   "--expect=" + config.accounts_version()])


def _caps_of(key: str) -> list[dict]:
    return next(a for a in routing.read_view()["accounts"] if a["key"] == key).get("caps") or []


def test_cap_undo_refuses_over_a_newer_cap_set_on_the_moved_key(world, khipu_app):
    first = _cap(world, khipu_app, SECOND, "30")
    moved = _move(world, khipu_app, SECOND)
    assert moved["key"] != SECOND
    second = _cap(world, khipu_app, moved["key"], "60")
    rc, out = _undo(world, first["owner_action"])
    _refused(rc, out, second["owner_action"])
    assert "same cap" in out["error"]
    assert [c.get("pct") for c in _caps_of(moved["key"])] == [60]


def test_pause_undo_refuses_over_a_newer_lift_on_the_moved_key(world, khipu_app):
    first = _app(world, khipu_app, ["route", "pause", SECOND])
    moved = _move(world, khipu_app, SECOND)
    second = _app(world, khipu_app, ["route", "lift", moved["key"]])
    rc, out = _undo(world, first["owner_action"])
    _refused(rc, out, second["owner_action"])
    assert "same pause" in out["error"]


# ---- a newer change inside another owner action's batch ---------------------------------------


def _priority(world, khipu_app, entries: list[dict]) -> dict:
    return _app(world, khipu_app, ["route", "priority", "set", "--entries=" + json.dumps(entries),
                                   "--expect=" + routing.priority_version()])


def test_priority_undo_refuses_over_the_priority_line_an_account_stop_wrote(world, khipu_app):
    from khipu import config

    _priority(world, khipu_app, [{"account": "Codex", "role": "a"}, {"account": "Grok", "role": "b"}])
    later = _priority(world, khipu_app, [{"account": "Codex", "role": "a2"}, {"account": "Grok", "role": "b2"}])
    stop = _app(world, khipu_app, ["accounts", "stop", "--account", "grok|", "--expect=" + config.accounts_version()])
    entries = routing.read_view()["priority"]["entries"]
    rc, out = _undo(world, later["owner_action"])
    _refused(rc, out, stop["owner_action"])
    assert "same priority" in out["error"] and "an account no longer tracked" in out["error"]
    assert routing.read_view()["priority"]["entries"] == entries


def test_a_newer_owner_action_matched_by_name_and_by_its_line_counts_once(world, khipu_app):
    first = _set_priority(world, khipu_app, "first role")
    second = _set_priority(world, khipu_app, "second role")
    newer = owner_direction.newer_change(owner_direction._find(first["owner_action"]))
    assert newer["id"] == second["owner_action"] and newer["count"] == 1


# ---- the check is made again under the lock ---------------------------------------------------


def _missed_once(monkeypatch):
    """The CLI's early check misses the newer change (it lands between that
    check and the Undo); the check under the lock sees it."""
    real, calls = owner_direction.newer_change, []

    def once(line):
        calls.append(1)
        return None if len(calls) == 1 else real(line)

    monkeypatch.setattr(owner_direction, "newer_change", once)


def test_a_plain_undo_refuses_a_newer_change_that_landed_after_the_early_check(world, khipu_app, monkeypatch):
    first = _set_priority(world, khipu_app, "first role")
    second = _set_priority(world, khipu_app, "second role")
    _missed_once(monkeypatch)
    rc, out = _undo(world, first["owner_action"])
    _refused(rc, out, second["owner_action"])
    assert _role() == "second role"
    assert owner_direction._find(first["owner_action"])["state"] == "applied"


def test_an_undo_over_newer_records_a_newer_change_that_landed_after_the_early_check(world, khipu_app, monkeypatch):
    first = _set_priority(world, khipu_app, "first role")
    second = _set_priority(world, khipu_app, "second role")
    _missed_once(monkeypatch)
    rc, out = _undo(world, first["owner_action"], "--over-newer")
    assert rc == 0, out
    (ahead,) = _undo_lines()
    merged = owner_direction._merged(owner_direction.read_log())[ahead["id"]]
    assert merged["facts"]["over_newer"] is True and merged["facts"]["newer"] == second["owner_action"]
    assert owner_direction.line_state(ahead["id"]) == "applied"


# ---- by direction, the override is the owner's own words --------------------------------------


def _direction_undo(world, khipu_app, line_id: str, *extra: str) -> tuple[int, dict]:
    khipu_app.as_shell("/opt/harness/claude")
    return world.run(["owner-direction", "undo", line_id, "--over-newer", *extra], True)


def _two_priorities(world, khipu_app) -> tuple[dict, dict]:
    first = _set_priority(world, khipu_app, "first role")
    return first, _set_priority(world, khipu_app, "second role")


def _not_asked(rc: int, out: dict) -> None:
    assert rc == 2 and out["code"] == "over_newer_not_asked", out
    assert _role() == "second role" and not _undo_lines()


def test_direction_over_newer_without_the_owners_words_is_refused(world, khipu_app):
    first, _second = _two_priorities(world, khipu_app)
    world.say("undo that first priority change anyway")
    _not_asked(*_direction_undo(world, khipu_app, first["owner_action"]))


def test_direction_over_newer_with_words_that_are_not_the_latest_message_is_refused(world, khipu_app):
    first, _second = _two_priorities(world, khipu_app)
    world.say("undo that first priority change anyway")
    world.say("thanks")
    _not_asked(*_direction_undo(world, khipu_app, first["owner_action"],
                                "--owner-words=undo that first priority change anyway"))


def test_direction_over_newer_when_the_owner_only_asked_for_an_undo_is_refused(world, khipu_app):
    first, _second = _two_priorities(world, khipu_app)
    world.say("undo the first priority change")
    _not_asked(*_direction_undo(world, khipu_app, first["owner_action"],
                                "--owner-words=undo the first priority change"))
    world.say("don't undo it anyway, keep the newer one")
    _not_asked(*_direction_undo(world, khipu_app, first["owner_action"],
                                "--owner-words=don't undo it anyway, keep the newer one"))


def test_direction_over_newer_when_the_owner_asked_for_it_restores_and_records_it(world, khipu_app):
    first, second = _two_priorities(world, khipu_app)
    words = "Undo that first priority change anyway,  and lose the newer one."
    world.say(words)
    rc, out = _direction_undo(world, khipu_app, first["owner_action"], "--owner-words=" + words)
    assert rc == 0, out
    assert not routing.read_view().get("priority")
    (line,) = _undo_lines()
    assert line["auth"] == "direction" and line["facts"]["newer"] == second["owner_action"]
    assert "words" not in line
