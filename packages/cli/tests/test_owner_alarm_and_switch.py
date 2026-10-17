"""Owner direction: turning it on is an owner action, and the tamper alarm on the owner-actions log
stays until the owner acknowledges it (docs/plans/2026-10-16-owner-direction.md, sections 2.4 and
2.5).

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


# ---- the tamper alarm --------------------------------------------------------------------------


def _cut_the_tail():
    for number in range(2):
        owner_direction._append({"kind": "action", "id": f"before-{number}", "auth": "app",
                                 "action": "route.lift", "target": "codex|"})
    path = owner_direction.log_path()
    path.write_text("\n".join(path.read_text(encoding="utf-8").splitlines()[:-1]) + "\n", encoding="utf-8")


def _later_append():
    owner_direction._append({"kind": "action", "id": "after-tamper", "auth": "app", "action": "route.lift",
                             "target": "codex|"})


def test_a_later_append_does_not_launder_a_cut_tail(world):
    _cut_the_tail()
    assert owner_direction.verify_log()["ok"] is False
    _later_append()
    chain = owner_direction.verify_log()
    assert chain["ok"] is False and chain["alarm"] and "missing from the end" in chain["first_bad"]["why"]
    assert owner_direction.doctor_block()["ok"] is False


def test_a_cut_tail_raises_the_alarm_even_when_nobody_checked_before_the_append(world):
    _cut_the_tail()
    _later_append()
    assert owner_direction.verify_log()["ok"] is False


def test_the_owner_acknowledges_the_alarm_by_chat(world):
    _cut_the_tail()
    _later_append()
    world.say()
    rc, out = world.run(["owner-direction", "acknowledge"], True)
    assert rc == 0 and out["acknowledged"]["first_bad"], out
    assert owner_direction.verify_log()["ok"] is True
    line = owner_direction.actions()[-1]
    assert line["action"] == "direction.acknowledge" and line["auth"] == "direction"
    _later_append()
    assert owner_direction.verify_log()["ok"] is True
    # Undo brings the alarm back as it was.
    rc, out = world.run(["owner-direction", "undo", line["id"]], True)
    assert rc == 0 and owner_direction.verify_log()["ok"] is False, out


def test_the_owner_acknowledges_the_alarm_in_the_app(world, khipu_app):
    _cut_the_tail()
    _later_append()
    khipu_app.as_app()
    rc, out = world.run(["owner-direction", "acknowledge", "--from-app"])
    assert rc == 0 and owner_direction.verify_log()["ok"] is True, out
    assert owner_direction.actions()[-1]["auth"] == "app"


def test_acknowledging_by_chat_needs_a_recent_owner_turn(world):
    _cut_the_tail()
    rc, out = world.run(["owner-direction", "acknowledge"], True)
    assert rc == 2 and out["code"] == "no_recorded_turn" and owner_direction.verify_log()["ok"] is False, out


def test_with_no_alarm_acknowledge_changes_nothing(world, khipu_app):
    khipu_app.as_app()
    rc, out = world.run(["owner-direction", "acknowledge", "--from-app"])
    assert rc == 0 and out["acknowledged"] is None and owner_direction.actions() == []
