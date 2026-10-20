"""Owner direction: Undo in the same transaction (docs/plans/2026-10-16-owner-direction.md,
sections 2.6 and 2.7). The owner-actions line that carries an action's Undo is written
before the change, so a direction never reports success without a way back: a log Khipu
can't write refuses the direction and changes nothing, a change that fails is marked
failed, and a change whose completion could not be written stays unconfirmed with its
Undo. Undo is logged the same way, and a clear (or a dismissed report) can be undone:
the fold drops the clear and the clear before it counts again.

Hermetic: the ``world`` fixture from tests/test_owner_direction.py (temp data and capture
folders, turns recorded the way the prompt hook records them)."""
from __future__ import annotations

import json

import pytest

from khipu import owner_direction, routing
from tests import test_owner_direction as base

world = base.world  # the fixture, shared
_state = base._state

REPORTER = {"kind": "conversation", "harness": "codex", "project": "p", "session": "reporter"}


def _pause_by_direction(world, words: str = "pause Codex for now") -> tuple[int, dict]:
    world.say(words)
    return world.run(["route", "pause", "Codex"], True)


def _chain_ok() -> None:
    chain = owner_direction.verify_log()
    assert chain["ok"] is True, chain


def _held_grok() -> tuple[dict, dict[str, str]]:
    hold = routing.add_hold("wait for Grok", scope="account", account="Grok", reason="keep", owner=True, via="app")
    return hold, {hold["id"]: "keep"}


# ---- the original probes ----------------------------------------------------------------------


def test_priority_undo_reuses_its_original_hold_choice(world):
    _hold, choices = _held_grok()
    routing.set_priority([{"account": "Grok", "role": "old role"}], via="app",
                         expect=routing.priority_version(), holds=choices)
    world.say("set Grok's new priority")
    rc, out = world.run(["route", "priority", "set", "--entries=" + json.dumps(
        [{"account": "Grok", "role": "new role"}]), "--expect=" + routing.priority_version(),
                         "--holds=" + json.dumps(choices)], True)
    assert rc == 0, out
    (line,) = owner_direction.actions()
    assert line["facts"]["holds"] == choices
    world.say("undo Grok's priority")
    rc, undone = world.run(["owner-direction", "undo", out["owner_action"]], True)
    assert rc == 0 and routing.read_view()["priority"]["entries"][0]["role"] == "old role", undone


def test_rule_edit_undo_reuses_its_original_hold_choice(world):
    _hold, choices = _held_grok()
    routing.add_rule("Grok old rule", via="app", holds=choices)
    world.say("edit Grok's rule")
    rc, out = world.run(["route", "rule", "edit", "1", "Grok new rule", "--holds=" + json.dumps(choices)], True)
    assert rc == 0, out
    (line,) = owner_direction.actions()
    assert line["facts"]["holds"] == choices
    world.say("undo Grok's rule edit")
    rc, undone = world.run(["owner-direction", "undo", out["owner_action"]], True)
    assert rc == 0 and routing.read_view()["rules"][0]["text"] == "Grok old rule", undone


def test_rule_confirm_undo_reuses_its_original_hold_choice(world):
    _hold, choices = _held_grok()
    routing.add_rule("Grok old rule", via="app", holds=choices)
    pending = routing.request_rule("Grok new rule", n=1, owner_request="the owner asked", author=REPORTER, via="mcp")
    assert pending["ok"] is True
    world.say("confirm Grok's edited rule")
    rc, out = world.run(["route", "rule", "confirm", "1", "--holds=" + json.dumps(choices)], True)
    assert rc == 0, out
    (line,) = owner_direction.actions()
    assert line["facts"]["holds"] == choices and line["before"]["pending_kind"] == "changed"
    world.say("undo Grok's confirmed rule")
    rc, undone = world.run(["owner-direction", "undo", out["owner_action"]], True)
    assert rc == 0 and routing.read_view()["rules"][0]["text"] == "Grok old rule", undone


def test_rule_retire_undo_reuses_its_original_hold_choice(world):
    hold, choices = _held_grok()
    routing.add_rule("Grok old rule", via="app", holds=choices)
    world.say("retire Grok's rule")
    rc, out = world.run(["route", "rule", "retire", "1", "--holds=" + json.dumps(choices)], True)
    assert rc == 0, out
    (line,) = owner_direction.actions()
    assert line["facts"]["holds"] == choices
    world.say("undo retiring Grok's rule")
    rc, undone = world.run(["owner-direction", "undo", out["owner_action"]], True)
    assert rc == 0 and routing.read_view()["rules"][0]["text"] == "Grok old rule", undone
    assert routing.read_view()["holds"][0]["id"] == hold["id"]


def test_direction_never_reports_success_when_its_undo_log_cannot_be_written(world, monkeypatch):
    def full(_line):
        raise OSError("disk full")

    monkeypatch.setattr(owner_direction, "_append", full)
    rc, out = _pause_by_direction(world)
    succeeded = rc == 0 and out.get("ok") is True
    assert not (succeeded and _state("codex|") == "paused" and owner_direction.read_log() == []), (
        "direction changed routing and reported success although no durable undo/audit record exists"
    )
    assert rc == 2 and out["code"] == "log_unwritable" and _state("codex|") != "paused", out


def test_clear_can_restore_the_report_it_replaced(world):
    routing.report(state="limited", account="Codex", until="2h", author=dict(REPORTER))
    words = "clear Codex report please"
    world.say(words)
    rc, out = world.run(["route", "clear", "Codex"], True)
    assert rc == 0 and _state("codex|") == "open", out
    words = "undo that clear please"
    world.say(words)
    rc, out = world.run(["owner-direction", "undo", out["owner_action"]], True)
    assert rc == 0 and _state("codex|") == "limited", out
    _chain_ok()


# ---- write-ahead ------------------------------------------------------------------------------


def test_the_undo_line_is_on_disk_before_the_change_is_made(world, monkeypatch):
    seen: list[list[dict]] = []
    real = routing.pause

    def watched(*args, **kwargs):
        seen.append(owner_direction.read_log())
        return real(*args, **kwargs)

    monkeypatch.setattr(routing, "pause", watched)
    rc, out = _pause_by_direction(world)
    assert rc == 0 and _state("codex|") == "paused", out
    (at_change,) = seen
    (line,) = at_change
    assert (line["kind"], line["state"], line["action"], line["before"]) == (
        "action", "pending", "route.pause", {"paused": False, "until": None})
    assert owner_direction.line_state(out["owner_action"]) == "applied"
    (merged,) = owner_direction.actions()
    assert merged["id"] == out["owner_action"] and merged["state"] == "applied" and merged["after"]["account"] == "codex|"
    _chain_ok()


def test_a_change_that_fails_is_marked_failed_and_has_no_undo(world, monkeypatch):
    def broken(*_args, **_kwargs):
        raise routing.RouteError("the record can't be written")

    monkeypatch.setattr(routing, "pause", broken)
    rc, out = _pause_by_direction(world)
    assert rc == 2 and _state("codex|") != "paused", out
    (line,) = owner_direction.actions()
    assert line["state"] == "failed"
    rc, out = world.run(["owner-direction", "undo", line["id"], "--from-app"])
    assert rc == 2 and "did not happen" in out["error"], out
    _chain_ok()


def test_a_completion_that_cannot_be_written_leaves_an_unconfirmed_line_that_undo_still_reverses(world, monkeypatch):
    real = owner_direction._append

    def no_completion(line):
        if line.get("kind") == "state":
            raise OSError("disk full")
        return real(line)

    monkeypatch.setattr(owner_direction, "_append", no_completion)
    rc, out = _pause_by_direction(world)
    assert rc == 0 and out["ok"] is True and _state("codex|") == "paused", out
    assert out["owner_action"] and "unconfirmed" in out["record_incomplete"], out
    assert owner_direction.line_state(out["owner_action"]) == "unconfirmed"
    monkeypatch.setattr(owner_direction, "_append", real)
    words = "undo that pause on Codex"
    world.say(words)
    rc, undone = world.run(["owner-direction", "undo", out["owner_action"]], True)
    assert rc == 0 and _state("codex|") != "paused", undone
    assert owner_direction.line_state(out["owner_action"]) == "undone"
    _chain_ok()


def test_the_app_never_waits_on_the_log(world, khipu_app, monkeypatch):
    def full(_line):
        raise OSError("disk full")

    monkeypatch.setattr(owner_direction, "_append", full)
    khipu_app.as_app()
    rc, out = world.run(["route", "pause", "Codex", "--from-app"])
    assert rc == 0 and _state("codex|") == "paused", out


def test_the_app_is_written_ahead_too(world, khipu_app, monkeypatch):
    seen: list[list[dict]] = []
    real = routing.pause

    def watched(*args, **kwargs):
        seen.append(owner_direction.read_log())
        return real(*args, **kwargs)

    monkeypatch.setattr(routing, "pause", watched)
    khipu_app.as_app()
    rc, _ = world.run(["route", "pause", "Codex", "--from-app"])
    assert rc == 0
    assert [(x["auth"], x["state"]) for x in seen[0]] == [("app", "pending")]
    assert [x["state"] for x in owner_direction.actions()] == ["applied"]
    _chain_ok()


# ---- undo is written ahead too -----------------------------------------------------------------


def test_undo_is_written_before_the_inverse_and_completed_after(world, monkeypatch):
    rc, out = _pause_by_direction(world)
    assert rc == 0
    seen: list[list[dict]] = []
    real = routing.lift

    def watched(*args, **kwargs):
        seen.append(owner_direction.read_log())
        return real(*args, **kwargs)

    monkeypatch.setattr(routing, "lift", watched)
    words = "undo that pause on Codex"
    world.say(words)
    rc, undone = world.run(["owner-direction", "undo", out["owner_action"]], True)
    assert rc == 0 and _state("codex|") != "paused", undone
    pending = seen[0][-1]
    assert (pending["kind"], pending["undoes"], pending["state"]) == ("undo", out["owner_action"], "pending")
    last = owner_direction.read_log()[-1]
    assert (last["kind"], last["target"], last["state"]) == ("state", pending["id"], "applied")
    assert owner_direction.line_state(out["owner_action"]) == "undone"
    _chain_ok()


def test_an_undo_that_fails_leaves_the_action_standing(world, monkeypatch):
    rc, out = _pause_by_direction(world)
    assert rc == 0

    def broken(*_args, **_kwargs):
        raise routing.RouteError("the record can't be written")

    monkeypatch.setattr(routing, "lift", broken)
    words = "undo that pause on Codex"
    world.say(words)
    rc, undone = world.run(["owner-direction", "undo", out["owner_action"]], True)
    assert rc == 2 and _state("codex|") == "paused", undone
    assert owner_direction.line_state(out["owner_action"]) == "applied"
    undo_line = next(x for x in owner_direction.read_log() if x.get("kind") == "undo")
    assert owner_direction.line_state(undo_line["id"]) == "failed"
    _chain_ok()


def test_an_undo_whose_log_cannot_be_written_changes_nothing(world, monkeypatch):
    rc, out = _pause_by_direction(world)
    assert rc == 0

    def full(_line):
        raise OSError("disk full")

    monkeypatch.setattr(owner_direction, "_append", full)
    words = "undo that pause on Codex"
    world.say(words)
    rc, undone = world.run(["owner-direction", "undo", out["owner_action"]], True)
    assert rc == 2 and undone["code"] == "log_unwritable" and _state("codex|") == "paused", undone


# ---- clear and dismiss can be undone -----------------------------------------------------------


def test_a_clear_records_the_clear_it_supersedes(world, khipu_app):
    khipu_app.as_app()
    rc, first = world.run(["route", "clear", "Codex", "--from-app"])
    assert rc == 0
    rc, _ = world.run(["route", "clear", "Codex", "--from-app"])
    assert rc == 0
    lines = owner_direction.actions()
    assert lines[0]["before"] == {"clear": None, "report": None}
    assert lines[1]["before"] == {"clear": first["id"], "report": None}
    assert "no_undo" not in lines[1]


def test_undoing_an_older_clear_keeps_the_newer_one(world, khipu_app):
    routing.report(state="limited", account="Codex", until="2h", author=dict(REPORTER))
    khipu_app.as_app()
    world.run(["route", "clear", "Codex", "--from-app"])
    world.run(["route", "clear", "Codex", "--from-app"])
    older, newer = owner_direction.actions()
    rc, out = world.run(["owner-direction", "undo", older["id"], "--from-app"])
    assert rc == 0 and _state("codex|") == "open", out
    rc, out = world.run(["owner-direction", "undo", newer["id"], "--from-app"])
    assert rc == 0 and _state("codex|") == "limited", out
    kinds = [e["type"] for e in routing.read_events()[0] if e.get("type") in ("clear", "clear_undo")]
    assert kinds == ["clear", "clear", "clear_undo", "clear_undo"]
    _chain_ok()


def test_a_dismissed_report_comes_back_on_undo(world, khipu_app):
    report = routing.report(state="limited", account="Codex", until="2h", author=dict(REPORTER))
    words = "dismiss that Codex report please"
    world.say(words)
    rc, out = world.run(["route", "clear", "Codex", "--report", report["id"]], True)
    assert rc == 0 and _state("codex|") != "limited", out
    khipu_app.as_app()
    rc, undone = world.run(["owner-direction", "undo", out["owner_action"], "--from-app"])
    assert rc == 0 and _state("codex|") == "limited", undone
    assert routing.read_events()[0][-1]["type"] == "undismiss"
    _chain_ok()


def test_the_next_routing_block_names_an_undone_clear(world, khipu_app):
    routing.report(state="limited", account="Codex", until="2h", author=dict(REPORTER))
    khipu_app.as_app()
    world.run(["route", "clear", "Codex", "--from-app"])
    first = routing.prompt_push(None, budget=5.0)
    marker = {"version": first["version"], "epoch": first["epoch"], "full_at": routing._iso(routing._now())}
    (line,) = owner_direction.actions()
    rc, _ = world.run(["owner-direction", "undo", line["id"], "--from-app"])
    assert rc == 0
    push = routing.prompt_push(marker, budget=5.0)
    assert "Undid the clear on Codex in the app" in push["text"], push


def test_an_undo_left_unconfirmed_shows_its_action_as_unconfirmed(world, monkeypatch):
    rc, out = _pause_by_direction(world)
    assert rc == 0
    real = owner_direction._append

    def no_completion(line):
        if line.get("kind") == "state":
            raise OSError("disk full")
        return real(line)

    monkeypatch.setattr(owner_direction, "_append", no_completion)
    words = "undo that pause on Codex"
    world.say(words)
    rc, undone = world.run(["owner-direction", "undo", out["owner_action"]], True)
    assert rc == 0 and _state("codex|") != "paused", undone
    undo_line = next(x for x in owner_direction.read_log() if x.get("kind") == "undo")
    assert owner_direction.line_state(undo_line["id"]) == "unconfirmed"
    # Whether it is undone is not known, so it is neither applied nor undone.
    assert owner_direction.line_state(out["owner_action"]) == "unconfirmed"
    _chain_ok()


# ---- the fifth review's probes: a finish that could not be written ------------------------------


def _no_finish(monkeypatch):
    real = owner_direction._append

    def no_completion(line):
        if line.get("kind") == "state":
            raise OSError("simulated full disk")
        return real(line)

    monkeypatch.setattr(owner_direction, "_append", no_completion)
    return lambda: monkeypatch.setattr(owner_direction, "_append", real)


def test_an_added_rule_whose_finish_was_lost_can_still_be_undone(world, monkeypatch):
    world.say()
    restore = _no_finish(monkeypatch)
    rc, out = world.run(["route", "rule", "add", "Never use fast mode"], True)
    assert rc == 0 and out.get("record_incomplete"), out
    restore()
    assert owner_direction.line_state(out["owner_action"]) == "unconfirmed"
    rc, undone = world.run(["owner-direction", "undo", out["owner_action"]], True)
    assert rc == 0, undone
    assert "Never use fast mode" not in [r["text"] for r in routing.read_view()["rules"]]


def test_an_owner_hold_whose_finish_was_lost_can_still_be_undone(world, monkeypatch):
    world.say()
    restore = _no_finish(monkeypatch)
    rc, out = world.run(["route", "hold", "while the Mac updates", "--account", "Codex", "--reason", "keep"], True)
    assert rc == 0 and out.get("record_incomplete"), out
    restore()
    assert [h for h in routing.read_view()["holds"] if h["owner"]]
    rc, undone = world.run(["owner-direction", "undo", out["owner_action"]], True)
    assert rc == 0, undone
    assert not [h for h in routing.read_view()["holds"] if h["owner"]]


def test_a_purge_whose_finish_was_lost_is_scheduled_and_can_be_cancelled(world, monkeypatch):
    world.say()
    restore = _no_finish(monkeypatch)
    rc, out = world.run(["ledger", "purge", "R1", "--project", "atlas", "--json"], True)
    assert rc == 0, out
    restore()
    (line,) = owner_direction.actions()
    assert line["state"] == "unconfirmed" and line["runs_at_ts"] > owner_direction._clock()
    rc, undone = world.run(["owner-direction", "undo", line["id"]], True)
    assert rc == 0 and undone["state"] == "cancelled", undone
    assert owner_direction.run_due(line["runs_at_ts"] + 100) == []


def test_a_lost_finish_purge_runs_on_its_day_and_then_says_it_ran(world, monkeypatch):
    from khipu import ledger

    world.say()
    restore = _no_finish(monkeypatch)
    rc, out = world.run(["ledger", "purge", "R1", "--project", "atlas", "--json"], True)
    restore()
    (line,) = owner_direction.actions()
    purged = []
    monkeypatch.setattr(ledger, "_connect", lambda: type("Fake", (), {"close": lambda self: None})())
    monkeypatch.setattr(ledger, "purge", lambda c, p, r: purged.append((p, r)) or {"ok": True})
    assert owner_direction.run_due(line["runs_at_ts"] + 1) == [line["id"]] and purged == [("atlas", "R1")]
    rc, undone = world.run(["owner-direction", "undo", line["id"]], True)
    assert rc == 2 and "already ran" in undone["error"], undone


# ---- the sixth review's probes: a lost finish, another writer, a seeded rule ------------------


def test_a_lost_finish_rule_undo_retires_its_own_rule_not_one_added_meanwhile(world, monkeypatch):
    real_add = routing.add_rule

    def interleaved(*args, **kwargs):
        real_add("Independent concurrent rule", via="app")
        return real_add(*args, **kwargs)

    monkeypatch.setattr(routing, "add_rule", interleaved)
    world.say()
    restore = _no_finish(monkeypatch)
    rc, out = world.run(["route", "rule", "add", "Rule requested by this action"], True)
    assert rc == 0 and out.get("record_incomplete"), out
    restore()
    rc, undone = world.run(["owner-direction", "undo", out["owner_action"]], True)
    assert rc == 0, undone
    assert [r["text"] for r in routing.read_view()["rules"]] == ["Independent concurrent rule"]


def test_a_lost_finish_hold_undo_ends_its_own_hold_not_one_added_meanwhile(world, monkeypatch):
    real_add = routing.add_hold

    def interleaved(*args, **kwargs):
        real_add("another owner hold", reason="keep", scope="machine", owner=True, via="app")
        return real_add(*args, **kwargs)

    monkeypatch.setattr(routing, "add_hold", interleaved)
    world.say()
    restore = _no_finish(monkeypatch)
    rc, out = world.run(["route", "hold", "while the Mac updates", "--account", "Codex", "--reason", "keep"], True)
    assert rc == 0 and out.get("record_incomplete"), out
    restore()
    rc, undone = world.run(["owner-direction", "undo", out["owner_action"]], True)
    assert rc == 0, undone
    assert [h["text"] for h in routing.read_view()["holds"] if h["owner"]] == ["another owner hold"]


def test_an_undo_whose_rule_was_never_written_refuses_instead_of_guessing(world, monkeypatch):
    real_add = routing.add_rule

    def lost(*args, **kwargs):
        real_add("Independent concurrent rule", via="app")
        raise routing.RouteError("the rule's own line was never written")

    world.say()
    monkeypatch.setattr(routing, "add_rule", lost)
    world.run(["route", "rule", "add", "Rule requested by this action"], True)
    (line,) = [x for x in owner_direction.read_log() if x.get("kind") == "action"]
    with pytest.raises(ValueError):
        owner_direction._added_rule(line)
    assert [r["text"] for r in routing.read_view()["rules"]] == ["Independent concurrent rule"]


def _seed_rule() -> None:
    routing._write({"type": "rule_add", "n": 1, "text": "Fake seeded rule", "seeded": True,
                    "written_by": {"kind": "seed", "file": "rules.json"}, "via": "seed"})


def test_confirming_a_seeded_rule_by_direction_can_be_undone(world):
    _seed_rule()
    (rule,) = routing.read_view()["rules"]
    assert rule["seeded"] and not rule["confirmed"]
    world.say()
    rc, out = world.run(["route", "rule", "confirm", "1"], True)
    assert rc == 0 and routing.read_view()["rules"][0]["confirmed"], out
    (line,) = owner_direction.actions()
    assert not line.get("no_undo") and line["before"] == {"seeded": True}
    rc, undone = world.run(["owner-direction", "undo", line["id"]], True)
    assert rc == 0, undone
    (rule,) = routing.read_view()["rules"]
    assert (rule["text"], rule["seeded"], rule["confirmed"]) == ("Fake seeded rule", True, False)
    # Confirming it again works as it did the first time.
    rc, out = world.run(["route", "rule", "confirm", "1"], True)
    assert rc == 0 and routing.read_view()["rules"][0]["confirmed"], out


def test_confirming_a_seeded_rule_in_the_app_can_be_undone(world, khipu_app):
    _seed_rule()
    khipu_app.as_app()
    rc, out = world.run(["route", "rule", "confirm", "1", "--from-app"])
    assert rc == 0, out
    (line,) = owner_direction.actions()
    rc, undone = world.run(["owner-direction", "undo", "--from-app", line["id"]])
    assert rc == 0, undone
    assert routing.read_view()["rules"][0]["confirmed"] is False
