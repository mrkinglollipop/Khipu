"""Owner direction (docs/plans/2026-10-16-owner-direction.md, section 2): the
check (``owner_direction.check``: a conversation the owner typed in within
the window), the direction branch of every owner gate, the owner-actions log,
Undo and the scheduled purge.

Hermetic: a temp data dir and a temp capture home. Turns are recorded the way
the prompt hook records them (``owner_turns.record`` with a fake payload and
a fake hook environment); the agent's command runs with a fake process
environment carrying its session id. Nothing here starts a harness."""
from __future__ import annotations

import io
import json
import sys
import time
from contextlib import redirect_stdout

import pytest

from khipu import owner_direction, owner_turns, routing

ACCOUNTS = [{"label": "Claude", "harness": "claude"},
            {"label": "Claude second", "harness": "claude", "home": "/logins/claude-second"},
            {"label": "Codex", "harness": "codex"},
            {"label": "Grok", "harness": "grok"}]
OWNER = "s-owner"
_SESSION_VARS = ("KHIPU_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CLAUDECODE",
                 "CLAUDE_CODE_ENTRYPOINT", "KHIPU_HARNESS", "GROK_HOOK_NAME", "GROK_HOOK_EVENT")


class World:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.mp = monkeypatch

    def say(self, text: str = "please go ahead with that", *, session: str = OWNER, harness: str = "claude_code",
            entry: str = "cli", ago: float = 0.0) -> str | None:
        """The owner types ``text`` into ``session``: what the prompt hook
        records (its turn id)."""
        env = {"aegis": {"GROK_HOOK_NAME": "UserPromptSubmit"},
               "claude_code": {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": entry}}[harness]
        payload = {"session_id": session, "prompt": text, "cwd": str(self.tmp)}
        return owner_turns.record(payload, env, now=time.time() - ago)

    def be(self, session: str | None, *, var: str = "CLAUDE_CODE_SESSION_ID") -> None:
        """The agent's process environment (its session id)."""
        for name in _SESSION_VARS:
            self.mp.delenv(name, raising=False)
        if session:
            self.mp.setenv(var, session)

    def run(self, argv: list[str], direct: bool = False) -> tuple[int, dict]:
        """A CLI command; ``direct`` adds ``--owner-direction``."""
        from khipu import cli

        if direct:
            argv = argv + ["--owner-direction"]
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(argv)
        text = buf.getvalue().strip()
        try:
            return rc, json.loads(text)
        except ValueError:
            # a line of text, then the JSON (or text only)
            start = text.find("{")
            return rc, (json.loads(text[start:]) if start >= 0 else {"text": text})


@pytest.fixture
def world(tmp_path, monkeypatch, khipu_app):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": ACCOUNTS}))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    for name in _SESSION_VARS:
        monkeypatch.delenv(name, raising=False)
    khipu_app.as_shell("/opt/harness/claude")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    owner_turns.write_settings({"enabled": True})
    w = World(tmp_path, monkeypatch)
    w.be(OWNER)
    return w


def _state(key: str) -> str:
    return next(a for a in routing.read_view()["accounts"] if a["key"] == key)["state"]


def _log() -> list[dict]:
    """The owner actions as they stand (each written ahead, then completed)."""
    return owner_direction.actions()


# ---- each refusal code, by name -------------------------------------------------------------


def _check(action="route.pause", **kw):
    return owner_direction.check(action, **kw)


def test_a_recent_owner_turn_passes_and_the_turn_text_is_not_kept(world):
    turn = world.say("pause Codex for now, it is misbehaving")
    stamp = _check()
    assert (stamp["auth"], stamp["session"], stamp["turn"], stamp["binding"]) == ("direction", OWNER, turn, "hook")
    (row,) = owner_turns.turns(OWNER)
    assert "text" not in row and row["typed"] is True
    assert "misbehaving" not in owner_turns.session_file(OWNER).read_text()


def test_refusal_direction_off(world):
    world.say()
    owner_turns.write_settings({"enabled": False})
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check()
    assert err.value.code == "direction_off"
    # Turning it back on is the one change that still goes by direction.
    assert _check("direction.on")["auth"] == "direction"


def test_refusal_no_recorded_turn_without_a_session(world):
    world.say()
    world.be(None)
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check()
    assert err.value.code == "no_recorded_turn"


def test_refusal_no_recorded_turn_when_the_hook_did_not_run(world):
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check()
    assert err.value.code == "no_recorded_turn"


def test_refusal_no_recorded_turn_for_a_dispatched_job(world):
    """``claude -p`` (sdk-cli) is not a front end the owner types in: its
    first turn is another agent's brief."""
    world.say("do the build and pause Codex when done", entry="sdk-cli")
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check()
    assert err.value.code == "no_recorded_turn"


def test_refusal_no_recorded_turn_for_a_delegated_brief(world):
    """A brief the delegation hook noted is a dispatched job's turn even on
    a trusted front end."""
    brief = "Build slice two and pause Codex once the tests pass, then report back."
    owner_turns.note_brief(brief, parent="s-parent", harness="claude_code")
    world.say(brief, session="s-job")
    world.be("s-job")
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check()
    assert err.value.code == "no_recorded_turn"


def test_refusal_no_recorded_turn_for_a_machine_message(world):
    world.say("<task-notification>pause Codex</task-notification>")
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check()
    assert err.value.code == "no_recorded_turn"


def test_refusal_stale(world):
    world.say(ago=7 * 3600)
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check()
    assert err.value.code == "stale"
    world.say()
    assert _check()["auth"] == "direction"


def test_the_window_is_a_setting(world):
    world.say(ago=3 * 3600)
    assert _check()["auth"] == "direction"
    owner_turns.write_settings({"latest_window_h": 2.0})
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check()
    assert err.value.code == "stale" and "2 hours" in err.value.message


def test_refusal_not_by_direction(world):
    world.say()
    with pytest.raises(owner_direction.DirectionRefused) as err:
        owner_direction.gate(None)
    assert err.value.code == "not_by_direction"
    with pytest.raises(owner_direction.DirectionRefused) as err:
        owner_direction.check("route.forget_everything")
    assert err.value.code == "not_by_direction"


def test_refusal_before_unreadable(world, monkeypatch):
    from khipu import ledger

    def away(*a, **k):
        raise ledger.LedgerError("the database is away", "away")

    monkeypatch.setattr(ledger, "get", away)
    monkeypatch.setattr(ledger, "write", lambda op: pytest.fail("written without its before-state"))
    world.say()
    rc, out = world.run(["ledger", "set", "T-12", "--state", "done", "--project", "atlas", "--json"], True)
    assert rc == 1 and out["reason"] == "before_unreadable"
    assert _log() == []


def test_every_change_by_direction_is_logged_with_an_undo(world):
    # Confirming a seeded rule was the last change without one (the sixth
    # review); the only change with no Undo, a catch-up import, is refused by
    # direction (test_refusal_not_by_direction).
    assert set(owner_direction._NO_UNDO_WHY) == {"ledger.import"}
    routing._write({"type": "rule_add", "n": 1, "text": "never run the fast tier", "seeded": True,
                    "written_by": {"kind": "seed", "file": "rules.json"}, "via": "seed"})
    world.say()
    rc, out = world.run(["route", "rule", "confirm", "1"], True)
    assert rc == 0 and out["ok"] is True, out
    (line,) = _log()
    assert line["action"] == "route.rule.confirm" and line["auth"] == "direction"
    assert not line.get("no_undo") and line["before"] == {"seeded": True}


def test_widening_the_check_and_turning_it_on_are_ordinary_changes(world):
    world.say()
    rc, out = world.run(["owner-direction", "set", "latest_window_h", "12"], True)
    assert rc == 0 and owner_turns.settings()["latest_window_h"] == 12.0
    rc, out = world.run(["owner-direction", "set", "trust", "claude_code:vscode"], True)
    assert rc == 0 and "claude_code:vscode" in owner_turns.settings()["trusted"]
    rc, out = world.run(["owner-direction", "off"], True)
    assert rc == 0 and owner_turns.settings()["enabled"] is False
    rc, out = world.run(["route", "pause", "Codex"], True)
    assert rc == 2 and out["code"] == "direction_off"
    rc, out = world.run(["owner-direction", "on"], True)
    assert rc == 0 and owner_turns.settings()["enabled"] is True
    assert [line["action"] for line in _log()] == ["direction.set", "direction.set", "direction.off", "direction.on"]
    assert {line["auth"] for line in _log()} == {"direction"}


# ---- every owner action goes by direction ---------------------------------------------------


@pytest.mark.parametrize("action", sorted(owner_direction.ACTIONS))
def test_every_action_passes_from_a_recent_owner_turn_and_is_refused_without_one(world, action):
    session = f"s-{action}"
    world.be(session)
    with pytest.raises(owner_direction.DirectionRefused) as err:
        owner_direction.check(action)
    assert err.value.code == "no_recorded_turn", action
    world.say(session=session, ago=7 * 3600)
    with pytest.raises(owner_direction.DirectionRefused) as err:
        owner_direction.check(action)
    assert err.value.code == "stale", action
    world.say(session=session)
    stamp = owner_direction.check(action)
    assert stamp["auth"] == "direction" and stamp["session"] == session


def test_every_action_is_a_khipu_owner_act_action():
    from khipu import owner_act

    assert set(owner_act.SPECS) == set(owner_direction.ACTIONS)


# ---- where the request comes from ------------------------------------------------------------


def test_a_session_id_from_another_conversation_is_refused(world):
    """The turn's hook must be above the call: an agent that sets another
    conversation's session id in its own process does not pass."""
    world.say(session="s-other")
    world.be("s-other")
    world.mp.setattr(owner_direction, "_caller_chain", lambda: [(4242, 1.0, "claude")])
    rc, out = world.run(["route", "pause", "Codex"], True)
    assert rc == 2 and out["code"] == "not_from_the_hook"
    assert not routing.events_file().exists()


def test_a_dispatched_job_that_kept_the_conversations_session_id_is_refused(world):
    """Fifth review, P1: a job started from the owner's conversation (``claude
    -p``, ``codex exec``, a T3 delegate) runs its own harness process. Even
    with the parent's session id, the nearest harness above its call is the
    job's, not the one whose hook recorded the owner's turn."""
    from tests.conftest import FAKE_HOOK_ISSUER

    world.say()
    world.be(OWNER, var="KHIPU_SESSION_ID")
    parent = (FAKE_HOOK_ISSUER["pid"], FAKE_HOOK_ISSUER["start"], "claude")
    world.mp.setattr(owner_direction, "_caller_chain", lambda: [(5000, 2.0, "zsh"), (5001, 2.0, "claude"), parent])
    rc, out = world.run(["route", "pause", "Codex"], True)
    assert rc == 2 and out["code"] == "not_from_the_hook", out
    assert _state("codex|") != "paused"
    # The owner's own conversation: only shells and Khipu's Python between.
    world.mp.setattr(owner_direction, "_caller_chain",
                     lambda: [(5000, 2.0, "sh"), (5002, 2.0, "python3.11"), (5003, 2.0, "zsh"), parent])
    rc, out = world.run(["route", "pause", "Codex"], True)
    assert rc == 0 and _state("codex|") == "paused", out


def test_a_call_naming_two_conversations_is_refused(world):
    """Fifth review, P1: one front end hosts two sessions; a call whose
    environment names the owner's session and another one can't be told
    apart, so it is refused."""
    world.say()
    world.be(OWNER, var="KHIPU_SESSION_ID")
    world.mp.setenv("CODEX_THREAD_ID", "another-conversation")
    rc, out = world.run(["owner-direction", "off"], True)
    assert rc == 2 and out["code"] == "no_recorded_turn", out
    assert owner_turns.settings()["enabled"] is True


def test_session_only_binding_for_aegis(world):
    world.say(session="s-aegis", harness="aegis")
    world.be("s-aegis", var="KHIPU_SESSION_ID")
    rc, out = world.run(["route", "pause", "Codex"], True)
    assert rc == 0 and _state("codex|") == "paused"
    (line,) = _log()
    assert line["binding"] == "session" and line["harness"] == "aegis"
    owner_turns.write_settings({"session_binding": False})
    rc, out = world.run(["route", "lift", "Codex"], True)
    assert rc == 2 and out["code"] == "no_recorded_turn"


def test_a_piped_yes_without_direction_is_still_refused(world, monkeypatch):
    world.say()
    monkeypatch.setattr(sys, "stdin", io.StringIO("yes\n"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    rc, out = world.run(["route", "pause", "Codex"])
    assert rc == 2 and "typed confirmation" in out["error"]
    assert not routing.events_file().exists()


# ---- apply, log, undo --------------------------------------------------------------------------


def test_a_pause_by_direction_is_stamped_logged_and_undone(world):
    turn = world.say("pause Codex until tonight")
    rc, out = world.run(["route", "pause", "Codex", "--until", "6h"], True)
    assert rc == 0 and _state("codex|") == "paused"
    events, _bad = routing.read_events()
    assert events[-1]["via"] == "direction" and events[-1]["auth"] == "direction"
    assert routing.read_view()["unchecked"] == [] and routing.doctor_block()["outside_app"] == []
    (line,) = _log()
    assert (line["action"], line["target"], line["auth"], line["session"], line["turn"], line["binding"]) == \
        ("route.pause", "codex|", "direction", OWNER, turn, "hook")
    assert "words" not in line
    assert line["before"] == {"paused": False, "until": None}
    world.say("undo that pause on Codex")
    rc, out = world.run(["owner-direction", "undo", line["id"]], True)
    assert rc == 0 and out["ok"] is True and _state("codex|") != "paused"
    (undo_line,) = [x for x in owner_direction.read_log() if x.get("kind") == "undo"]
    assert undo_line["undoes"] == line["id"] and owner_direction.line_state(undo_line["id"]) == "applied"
    rc, out = world.run(["owner-direction", "undo", line["id"], "--from-app"])
    assert rc == 2 and "already undone" in out["error"]


def test_the_app_and_a_typed_yes_are_logged_too(world, khipu_app, monkeypatch):
    khipu_app.as_app()
    rc, _ = world.run(["route", "note", "set", "Codex for builds", "--from-app"])
    assert rc == 0
    khipu_app.as_shell()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    rc, _ = world.run(["route", "note", "end"])
    assert rc == 0
    assert [(line["action"], line["auth"]) for line in _log()] == [("route.note.set", "app"),
                                                                    ("route.note.end", "terminal")]


def test_note_rule_and_hold_by_direction_store_the_given_text(world):
    world.say("set the note to builds go to Codex today")
    rc, out = world.run(["route", "note", "set", "builds go to Codex"], True)
    assert rc == 0 and routing.read_view()["note"]["text"] == "builds go to Codex"
    world.say("add a rule: never run the fast tier")
    rc, out = world.run(["route", "rule", "add", "never run the fast tier"], True)
    assert rc == 0 and routing.read_view()["rules"][-1]["text"] == "never run the fast tier"
    world.say("hold Codex while the Mac updates")
    rc, out = world.run(["route", "hold", "while the Mac updates", "--account", "Codex", "--reason", "keep"], True)
    assert rc == 0 and routing.read_view()["holds"][0]["owner"] is True


def test_a_routing_setting_by_direction_and_its_undo(world):
    world.say("set stale after to 6h please")
    rc, out = world.run(["config", "--set", "routing_stale_after", "6h"], True)
    assert rc == 0 and out["ok"] is True and routing.settings()["routing_stale_after"] == 6 * 3600
    (line,) = _log()
    assert line["action"] == "config.routing_setting" and line["before"] == {"value": None, "set": False}
    world.say("undo the stale after change")
    rc, out = world.run(["owner-direction", "undo", line["id"]], True)
    assert rc == 0 and routing.settings()["routing_stale_after"] == 12 * 3600


def test_work_hide_by_direction_goes_through_the_ledger_guard(world, monkeypatch):
    from khipu import work

    monkeypatch.setattr(work, "read_owed", lambda: {"items": [{"id": "42", "text": "x"}], "error": None})
    world.say("hide commitment 42 for now")
    rc, out = world.run(["work", "hide", "42"], True)
    assert rc == 0 and "42" in work.read_hidden()
    (line,) = _log()
    assert line["action"] == "work.hide" and line["before"] == {"hidden": None}


# ---- the scheduled purge ---------------------------------------------------------------------


def test_purge_by_direction_is_scheduled_runs_a_day_later_and_undo_cancels_it(world, monkeypatch):
    from khipu import ledger

    purged: list = []
    monkeypatch.setattr(ledger, "_connect", lambda: type("C", (), {"close": lambda self: None})())
    monkeypatch.setattr(ledger, "purge", lambda conn, project, row: purged.append((project, row)) or {"ok": True})
    world.say("purge T-12 from the atlas ledger")
    rc, out = world.run(["ledger", "purge", "T-12", "--project", "atlas", "--json"], True)
    assert rc == 0 and out["state"] == "scheduled" and purged == []
    (line,) = _log()
    assert line["state"] == "scheduled" and line["runs_at"]
    owner_direction.run_due(now=time.time() + 3600)
    assert purged == []
    owner_direction.run_due(now=time.time() + 25 * 3600)
    assert purged == [("atlas", "T-12")]
    owner_direction.run_due(now=time.time() + 26 * 3600)
    assert purged == [("atlas", "T-12")]

    world.say("purge T-13 from the atlas ledger")
    rc, out = world.run(["ledger", "purge", "T-13", "--project", "atlas", "--json"], True)
    second = _log()[-1]
    world.say("undo the purge of T-13")
    rc, out = world.run(["owner-direction", "undo", second["id"]], True)
    assert rc == 0
    owner_direction.run_due(now=time.time() + 25 * 3600)
    assert purged == [("atlas", "T-12")]
    assert owner_direction.line_state(second["id"]) == "cancelled"


def test_the_actions_list_is_newest_first(world):
    for text, name in (("pause Codex for now", "Codex"), ("pause Grok for now", "Grok")):
        world.say(text)
        world.run(["route", "pause", name], True)
    rc, out = world.run(["owner-direction", "actions", "--json"])
    assert rc == 0 and [a["target"] for a in out["actions"]] == ["grok|", "codex|"]


# ---- the next routing block names it (slice 4) ------------------------------------------------


def _marker() -> dict:
    """A conversation that has just had the full summary of a record."""
    routing.report(account="Claude", state="open",
                   author={"kind": "conversation", "harness": "codex", "project": "p", "session": "s-other"})
    first = routing.prompt_push(None, budget=5.0)
    return {"version": first["version"], "epoch": first["epoch"], "full_at": routing._iso(routing._now())}


def test_the_next_routing_block_names_a_direction_once_in_fixed_words(world, khipu_app):
    marker = _marker()
    turn = world.say("pause Codex until tonight please")
    rc, _ = world.run(["route", "pause", "Codex", "--until", "6h"], True)
    assert rc == 0
    events, _bad = routing.read_events()
    at = routing._clock(routing._parse(events[-1]["written_at"]), routing._now())
    push = routing.prompt_push(marker, budget=5.0)
    assert push["kind"] == "changes"
    assert f"Paused Codex by your direction in a Claude Code conversation, {at} (undo in Settings)." in push["text"]
    # Fixed words only: never the session, the turn or the owner's words.
    assert OWNER not in push["text"] and turn not in push["text"] and "tonight" not in push["text"]
    seen = {"version": push["version"], "epoch": push["epoch"], "full_at": marker["full_at"]}
    assert routing.prompt_push(seen, budget=5.0)["kind"] == "none"
    # Its undo by direction is named the same way, and so is the app's own change.
    line = _log()[-1]
    world.say("undo that pause on Codex")
    rc, _ = world.run(["owner-direction", "undo", line["id"]], True)
    assert rc == 0
    push = routing.prompt_push(seen, budget=5.0)
    assert "Lifted the pause on Codex by your direction in a Claude Code conversation" in push["text"]
    seen = {"version": push["version"], "epoch": push["epoch"], "full_at": marker["full_at"]}
    khipu_app.as_app()
    rc, _ = world.run(["route", "note", "set", "builds go to Codex", "--from-app"])
    assert rc == 0
    push = routing.prompt_push(seen, budget=5.0)
    assert push["kind"] == "changes" and "by your direction" not in push["text"]
    assert push["text"].count("Set the note in the app") == 1 and "(undo in Settings)" in push["text"]
    seen = {"version": push["version"], "epoch": push["epoch"], "full_at": marker["full_at"]}
    assert routing.prompt_push(seen, budget=5.0)["kind"] == "none"


def test_the_next_routing_block_names_a_terminal_change_once(world, khipu_app, monkeypatch):
    marker = _marker()
    khipu_app.as_shell()
    monkeypatch.setattr(routing, "typed_yes", lambda prompt: True)
    rc, _ = world.run(["route", "pause", "Codex"])
    assert rc == 0
    push = routing.prompt_push(marker, budget=5.0)
    assert push["text"].count("Paused Codex on a terminal") == 1 and "(undo in Settings)" in push["text"]
    seen = {"version": push["version"], "epoch": push["epoch"], "full_at": marker["full_at"]}
    assert routing.prompt_push(seen, budget=5.0)["kind"] == "none"


def test_a_rule_by_direction_is_named_in_the_block(world):
    marker = _marker()
    world.say("add a rule: never run the fast tier")
    rc, _ = world.run(["route", "rule", "add", "never run the fast tier"], True)
    assert rc == 0
    push = routing.prompt_push(marker, budget=5.0)
    assert "Added standing rule 1 by your direction in a Claude Code conversation" in push["text"]


def test_the_apps_owner_direction_argv_apply_at_once_and_only_from_the_app(world, khipu_app):
    """Settings, Owner actions: the argv the app's Rust commands build
    (``owner_direction_*_argv`` in lib.rs). The app is the owner, so a
    change applies at once; the same argv from a shell is refused."""
    rc, out = world.run(["owner-direction", "set", "--from-app", "--", "latest_window_h", "12"])
    assert rc == 2 and out["code"] == "owner_only"
    khipu_app.as_app()
    for argv in (["set", "--from-app", "--", "latest_window_h", "12"],
                 ["set", "--from-app", "--", "trust", "claude_code:sdk-cli"],
                 ["set", "--from-app", "--", "session_binding", "off"],
                 ["off", "--from-app"], ["on", "--from-app"]):
        rc, out = world.run(["owner-direction", *argv])
        assert rc == 0 and out["ok"] is True, argv
    conf = owner_turns.settings()
    assert (conf["latest_window_h"], conf["session_binding"], conf["enabled"]) == (12, False, True)
    assert "claude_code:sdk-cli" in conf["trusted"]
    assert {line["auth"] for line in _log()} == {"app"}
    first = _log()[0]
    rc, out = world.run(["owner-direction", "undo", "--from-app", "--", first["id"]])
    assert rc == 0 and out["undid"] == first["id"] and owner_turns.settings()["latest_window_h"] == 6


def test_the_apps_own_change_is_in_the_log_and_can_be_undone_there(world, khipu_app):
    khipu_app.as_app()
    rc, _ = world.run(["route", "note", "set", "builds go to Codex", "--from-app"])
    assert rc == 0 and routing.read_view()["note"]
    (line,) = [x for x in _log() if x.get("kind") == "action"]
    assert (line["action"], line["auth"]) == ("route.note.set", "app")
    rc, out = world.run(["owner-direction", "undo", "--from-app", "--", line["id"]])
    assert rc == 0, out
    assert not routing.read_view()["note"]
    assert owner_direction.verify_log()["ok"] is True


# ---- the fifth review's probe: every owner change is in the routing push ------------------------


def test_an_owner_change_outside_the_routing_record_is_named_in_the_next_push(world):
    """Fifth review, P2: widening the window (an owner-direction setting) is
    named once in the next routing push, with how it was made, and Undo is
    named too. A routing change is named once, from its own line."""
    world.say()
    baseline = routing.read_view()
    rc, out = world.run(["owner-direction", "set", "latest_window_h", "12"], True)
    assert rc == 0, out
    diff = routing.since(baseline["version"], routing.read_view())
    assert len(diff["directions"]) == 1 and diff["directions"][0].startswith(
        "Changed the owner-direction settings by your direction in "), diff
    assert not [e for e in diff["entities"] if e.startswith("owner:")], diff
    mark = routing.read_view()["version"]
    rc, out = world.run(["route", "pause", "Codex"], True)
    assert rc == 0, out
    diff = routing.since(mark, routing.read_view())
    assert len(diff["directions"]) == 1 and diff["directions"][0].startswith("Paused Codex"), diff
    mark = routing.read_view()["version"]
    line = next(a for a in _log() if a["action"] == "direction.set")
    rc, out = world.run(["owner-direction", "undo", line["id"]], True)
    assert rc == 0 and owner_turns.settings()["latest_window_h"] == 6.0, out
    diff = routing.since(mark, routing.read_view())
    assert [d.split(" by ")[0] for d in diff["directions"]] == ["Undid an owner change"], diff
