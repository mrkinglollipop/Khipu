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

from khipu import owner_direction, routing
from tests import test_owner_direction as base

world = base.world  # the fixture, shared
_state = base._state

REPORTER = {"kind": "conversation", "harness": "codex", "project": "p", "session": "reporter"}


def _pause_by_direction(world, words: str = "pause Codex for now") -> tuple[int, dict]:
    token = world.say(words)
    return world.run(["route", "pause", "Codex"], words, token)


def _chain_ok() -> None:
    chain = owner_direction.verify_log()
    assert chain["ok"] is True, chain


# ---- the original probes ----------------------------------------------------------------------


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
    token = world.say(words)
    rc, out = world.run(["route", "clear", "Codex"], words, token)
    assert rc == 0 and _state("codex|") == "open", out
    words = "undo that clear please"
    token = world.say(words)
    rc, out = world.run(["owner-direction", "undo", out["owner_action"]], words, token)
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
    rc, undone = world.run(["owner-direction", "undo", out["owner_action"]], words, world.say(words))
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
    rc, undone = world.run(["owner-direction", "undo", out["owner_action"]], words, world.say(words))
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
    rc, undone = world.run(["owner-direction", "undo", out["owner_action"]], words, world.say(words))
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
    rc, undone = world.run(["owner-direction", "undo", out["owner_action"]], words, world.say(words))
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
    token = world.say(words)
    rc, out = world.run(["route", "clear", "Codex", "--report", report["id"]], words, token)
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
    rc, undone = world.run(["owner-direction", "undo", out["owner_action"]], words, world.say(words))
    assert rc == 0 and _state("codex|") != "paused", undone
    undo_line = next(x for x in owner_direction.read_log() if x.get("kind") == "undo")
    assert owner_direction.line_state(undo_line["id"]) == "unconfirmed"
    # Whether it is undone is not known, so it is neither applied nor undone.
    assert owner_direction.line_state(out["owner_action"]) == "unconfirmed"
    _chain_ok()
