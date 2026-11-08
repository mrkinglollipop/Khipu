"""Owner direction: turning it on is an owner action, logged with an Undo
(docs/plans/2026-10-16-owner-direction.md, sections 2.4 and 2.5).

Hermetic: the ``world`` fixture from tests/test_owner_direction.py (temp data and capture
folders, turns recorded the way the prompt hook records them)."""
from __future__ import annotations

from khipu import owner_direction, owner_turns
from tests import test_owner_direction as base

world = base.world  # the fixture, shared


# ---- turning owner direction on is an owner action ------------------------------------------------


def test_turning_it_on_in_the_app_is_logged_with_an_undo(world, khipu_app):
    owner_turns.write_settings({"enabled": False})
    khipu_app.as_app()
    rc, out = world.run(["owner-direction", "on", "--from-app"])
    assert rc == 0 and owner_turns.settings()["enabled"] is True, out
    line = owner_direction.actions()[-1]
    assert (line["action"], line["auth"], line["before"]) == ("direction.on", "app", {"enabled": False})
    rc, out = world.run(["owner-direction", "undo", line["id"], "--from-app"])
    assert rc == 0 and owner_turns.settings()["enabled"] is False, out


def test_turning_it_back_on_by_chat_is_one_request(world):
    world.say()
    rc, out = world.run(["owner-direction", "off"], True)
    assert rc == 0 and owner_turns.settings()["enabled"] is False, out
    # Off: turn times are still recorded (never their text), so a request
    # to turn it back on can pass the check.
    world.say("turn owner direction back on")
    rc, out = world.run(["owner-direction", "on"], True)
    assert rc == 0 and owner_turns.settings()["enabled"] is True, out
    line = owner_direction.actions()[-1]
    assert line["action"] == "direction.on" and line["auth"] == "direction" and line["before"] == {"enabled": False}


def test_while_off_nothing_but_turning_it_on_goes_by_direction(world):
    owner_turns.write_settings({"enabled": False})
    world.say()
    rc, out = world.run(["route", "pause", "Codex"], True)
    assert rc == 2 and out["code"] == "direction_off", out
