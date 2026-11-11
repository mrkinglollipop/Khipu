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
                 "CLAUDE_CODE_ENTRYPOINT", "KHIPU_HARNESS", "GROK_HOOK_NAME", "GROK_HOOK_EVENT",
                 "KHIPU_JOB", "KHIPU_PARENT_SESSION", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_HOST_SESSION_ID")


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


def run_confirmed(world, action: str, args: dict, yes: str = "yes, go ahead") -> tuple[bool, dict]:
    """A class C change as the owner gets it: ``khipu_owner_act`` holds it, he types
    his yes, ``direction.confirm`` claims the request and its command is replayed
    (the real held-request path; a C command by direction applies no other way)."""
    from khipu import owner_act, owner_background

    world.say("go ahead with that")  # the ask needs a typed turn in this conversation
    ok, held = owner_act.act(action, args)
    assert ok and held["pending"] and held["applied"] is False, held
    world.say(yes)
    ok, done = owner_act.act("direction.confirm", {"request": held["request"]})
    if ok and done.get("started"):
        ok, done = owner_background.run(done["request"])
    return ok, done


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
    assert "prompt hook may not be installed" in str(err.value)


def test_refusal_no_recorded_turn_for_a_dispatched_job(world):
    """``claude -p`` (sdk-cli) is not a front end the owner types in: its
    first turn is another agent's brief."""
    world.say("do the build and pause Codex when done", entry="sdk-cli")
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check()
    assert err.value.code == "no_recorded_turn"


def test_a_desktop_app_session_passes(world):
    """The desktop app's Code tab (claude-desktop) is a front end the owner
    types in (the 2026-10-17 refusal)."""
    world.say("pause Codex until 5pm", entry="claude-desktop")
    assert _check()["auth"] == "direction"


def test_codex_exec_stays_refused(world, tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {"originator": "codex_exec"}}) + "\n")
    owner_turns.record({"session_id": OWNER, "prompt": "pause Codex until 5pm", "cwd": str(tmp_path),
                        "transcript_path": str(rollout)}, {"CODEX_THREAD_ID": OWNER}, now=time.time())
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check()
    assert err.value.code == "no_recorded_turn"
    assert "front end that counts" not in err.value.message


def test_refusal_for_an_untrusted_front_end_names_the_fix(world):
    owner_turns.write_settings({"trusted": ["claude_code:cli"]})
    world.say("pause Codex until 5pm", entry="claude-desktop")
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check()
    assert err.value.code == "no_recorded_turn"
    message = str(err.value)
    assert "claude_code:claude-desktop" in message
    assert "Settings, Owner actions, Front ends that count as you" in message


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


def test_recorded_owner_turn_does_not_expire_at_six_hours(world):
    world.say(ago=7 * 3600)
    assert _check()["auth"] == "direction"
    world.say()
    assert _check()["auth"] == "direction"


def test_the_window_is_a_setting(world):
    world.say(ago=3 * 3600)
    assert _check()["auth"] == "direction"
    owner_turns.write_settings({"latest_window_h": 2.0})
    assert owner_turns.settings()["latest_window_h"] == 2.0
    assert _check()["auth"] == "direction"


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
    # review). Sending this Mac's waiting
    # forgets to the hub has none: a forget is never undone (fact
    # provenance design, section 9), and an agent may forget without
    # direction at all (khipu_forget).
    assert set(owner_direction._NO_UNDO_WHY) == {"forget.send", "forget.here", "forget.not_same"}
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
    world.say()
    rc, out = world.run(["owner-direction", "set", "trust", "claude_code:vscode"], True)
    assert rc == 0 and "claude_code:vscode" in owner_turns.settings()["trusted"]
    world.say()
    rc, out = world.run(["owner-direction", "off"], True)
    assert rc == 0 and owner_turns.settings()["enabled"] is False
    world.say()
    rc, out = world.run(["route", "pause", "Codex"], True)
    assert rc == 2 and out["code"] == "direction_off"
    world.say()
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
    assert owner_direction.check(action)["auth"] == "direction", action
    world.say(session=session)
    stamp = owner_direction.check(action)
    assert stamp["auth"] == "direction" and stamp["session"] == session


def test_every_action_is_a_khipu_owner_act_action():
    from khipu import owner_act

    assert set(owner_act.SPECS) == set(owner_direction.ACTIONS)


# ---- where the request comes from ------------------------------------------------------------


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


# ---- a T3 Code conversation (the 2026-10-31 refusal) -------------------------------------------


@pytest.fixture
def t3_conversation(world, tmp_path, monkeypatch):
    """A fake T3 state database where the owner's conversation started long
    ago: its row is the first, behind more than the newest 1024 rows other
    threads have written since (the 2026-10-31 conversation was row 6 of
    1062)."""
    import sqlite3

    path = tmp_path / "statev2.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE orchestration_v2_projection_provider_threads "
                "(thread_id TEXT, payload_json TEXT, updated_at TEXT)")
    rows = [("t3-owner-thread", OWNER)] + [(f"t3-thread-{i}", f"s-job-{i}") for i in range(1100)]
    con.executemany("INSERT INTO orchestration_v2_projection_provider_threads VALUES (?, ?, ?)",
                    [(t, json.dumps({"nativeThreadRef": {"nativeId": s}}), "2026-10-31") for t, s in rows])
    con.commit()
    con.close()
    monkeypatch.setenv("KHIPU_T3_DB", str(path))
    return world


def _t3_say(session: str = OWNER, *, ago: float = 0.0, **env) -> None:
    """The owner types in a T3 Code conversation: Claude Code under T3's
    Agent SDK runs the prompt hook with entrypoint ``sdk-ts``."""
    owner_turns.record({"session_id": session, "prompt": "set the routing budget to 500 ms", "cwd": "/repo"},
                       {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "sdk-ts", **env}, now=time.time() - ago)


_T3_WHY = ("a T3 conversation can't be told apart from T3's own scheduled or relayed messages; "
           "a change asked for there waits for the owner to confirm it")


def test_a_t3_conversations_hook_turn_alone_never_makes_the_change(t3_conversation):
    from khipu import t3

    assert t3.thread_for_session(OWNER) is None  # behind the newest window
    _t3_say()
    (row,) = owner_turns.turns(OWNER)
    assert row["entrypoint"] == "t3" and row["eligible"] is False and row["why"] == _T3_WHY
    assert not row.get("dispatched")
    # T3's own record of the owner's messages decides (class A, 2026-11-07); here it can't be read.
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check("config.routing_setting")
    assert err.value.code == "t3_unreadable"
    rc, out = t3_conversation.run(["config", "--set", "routing_prompt_budget_ms", "500"], True)
    assert rc != 0, out


def test_the_same_words_from_a_trusted_non_t3_front_end_still_pass(world):
    owner_turns.record({"session_id": OWNER, "prompt": "set the routing budget to 500 ms", "cwd": "/repo"},
                       {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"})
    (row,) = owner_turns.turns(OWNER)
    assert row["entrypoint"] == "cli" and row["eligible"] is True
    assert _check("config.routing_setting")["auth"] == "direction"


def test_a_t3_lookup_that_fails_once_is_tried_again(t3_conversation, monkeypatch):
    from khipu import t3

    real, calls = t3.session_thread_ids_result, []

    def flaky(sid, **kw):
        calls.append(sid)
        return (None, False) if len(calls) == 1 else real(sid, **kw)

    monkeypatch.setattr(t3, "session_thread_ids_result", flaky)
    _t3_say()
    assert owner_turns.turns(OWNER)[-1]["entrypoint"] == "t3"
    # Two failed reads settle nothing: unverified, and it never counts.
    monkeypatch.setattr(t3, "session_thread_ids_result", lambda sid, **kw: (None, False))
    _t3_say()
    assert owner_turns.turns(OWNER)[-1]["entrypoint"] == "t3-unverified"
    assert owner_turns.turns(OWNER)[-1]["eligible"] is False


def test_another_t3_conversations_turn_is_refused(t3_conversation):
    _t3_say(session="s-job-7")
    assert owner_turns.turns("s-job-7")[0]["entrypoint"] == "t3"
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check("config.routing_setting")
    assert err.value.code == "no_recorded_turn"


def test_an_old_t3_turn_is_proved_by_t3s_record_too(t3_conversation):
    _t3_say(ago=7 * 3600)
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check("config.routing_setting")
    # No age limit and no hold: T3's own record decides, and here it can't be read.
    assert err.value.code == "t3_unreadable"


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


def test_undoing_a_routing_setting_leaves_the_view_current(world):
    """config.json is an input of the view: the undo refolds it, as set_setting does,
    so the next prompt finds view.json current instead of stale."""
    routing.set_note("builds go to Codex", via="app")  # a record, so there is a saved view
    routing.read_view()
    owner_direction._restore_setting("routing_stale_after", 6 * 3600, True)
    assert routing.settings()["routing_stale_after"] == 6 * 3600
    assert routing._stale_reason(routing._load_view(), routing._light_inputs()[0], routing._now()) is None
    owner_direction._restore_setting("routing_stale_after", None, False)
    assert routing._stale_reason(routing._load_view(), routing._light_inputs()[0], routing._now()) is None


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
    ok, out = run_confirmed(world, "ledger.purge", {"id": "T-12", "project": "atlas"}, "yes, purge T-12")
    assert ok and out["state"] == "scheduled" and purged == [], out
    (line,) = _log()
    assert line["state"] == "scheduled" and line["runs_at"]
    owner_direction.run_due(now=time.time() + 3600)
    assert purged == []
    owner_direction.run_due(now=time.time() + 25 * 3600)
    assert purged == [("atlas", "T-12")]
    owner_direction.run_due(now=time.time() + 26 * 3600)
    assert purged == [("atlas", "T-12")]

    ok, out = run_confirmed(world, "ledger.purge", {"id": "T-13", "project": "atlas"}, "yes, purge T-13")
    assert ok, out
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



def _named(marker: dict) -> dict:
    """The fixed-words lines naming who made each change after ``marker``
    (``khipu_route since``): the routing page itself names no author."""
    view = routing.read_view()
    got = routing.since(marker["version"], view, epoch=marker["epoch"])
    text = "\n".join(got["directions"])
    return {"kind": "changes" if text else "none", "text": text, "version": view["version"],
            "epoch": view["epoch"]}

def test_the_next_routing_block_names_a_direction_once_in_fixed_words(world, khipu_app):
    marker = _marker()
    turn = world.say("pause Codex until tonight please")
    rc, _ = world.run(["route", "pause", "Codex", "--until", "6h"], True)
    assert rc == 0
    events, _bad = routing.read_events()
    at = routing._clock(routing._parse(events[-1]["written_at"]), routing._now())
    push = _named(marker)
    assert push["kind"] == "changes"
    assert f"Paused Codex by your direction in a Claude Code conversation, {at} (undo in Settings)." in push["text"]
    # Fixed words only: never the session, the turn or the owner's words.
    assert OWNER not in push["text"] and turn not in push["text"] and "tonight" not in push["text"]
    seen = {"version": push["version"], "epoch": push["epoch"], "full_at": marker["full_at"]}
    assert _named(seen)["kind"] == "none"
    # Its undo by direction is named the same way, and so is the app's own change.
    line = _log()[-1]
    world.say("undo that pause on Codex")
    rc, _ = world.run(["owner-direction", "undo", line["id"]], True)
    assert rc == 0
    push = _named(seen)
    assert "Lifted the pause on Codex by your direction in a Claude Code conversation" in push["text"]
    seen = {"version": push["version"], "epoch": push["epoch"], "full_at": marker["full_at"]}
    khipu_app.as_app()
    rc, _ = world.run(["route", "note", "set", "builds go to Codex", "--from-app"])
    assert rc == 0
    push = _named(seen)
    assert push["kind"] == "changes" and "by your direction" not in push["text"]
    assert push["text"].count("Set the note in the app") == 1 and "(undo in Settings)" in push["text"]
    seen = {"version": push["version"], "epoch": push["epoch"], "full_at": marker["full_at"]}
    assert _named(seen)["kind"] == "none"


def test_the_next_routing_block_names_a_terminal_change_once(world, khipu_app, monkeypatch):
    marker = _marker()
    khipu_app.as_shell()
    monkeypatch.setattr(routing, "typed_yes", lambda prompt: True)
    rc, _ = world.run(["route", "pause", "Codex"])
    assert rc == 0
    push = _named(marker)
    assert push["text"].count("Paused Codex on a terminal") == 1 and "(undo in Settings)" in push["text"]
    seen = {"version": push["version"], "epoch": push["epoch"], "full_at": marker["full_at"]}
    assert _named(seen)["kind"] == "none"


def test_a_rule_by_direction_is_named_in_the_block(world):
    marker = _marker()
    world.say("add a rule: never run the fast tier")
    rc, _ = world.run(["route", "rule", "add", "never run the fast tier"], True)
    assert rc == 0
    push = _named(marker)
    assert "Added standing rule 1 by your direction in a Claude Code conversation" in push["text"]


@pytest.mark.parametrize("offline", [False, True], ids=["numbered", "provisional"])
@pytest.mark.parametrize("route,event_kind,verb", [
    ("rule.add", "rule_add", "Added standing rule"),
    ("rule.edit", "rule_edit", "Changed standing rule"),
    ("rule.retire", "rule_retire", "Retired standing rule"),
    ("priority.set", "priority_set", "Set the priority"),
    ("hold.add", "hold", "Added a hold"),
    ("hold.end", "hold_end", "Ended a hold"),
    ("accounts.auto_track", "owner_change", "Changed whether new accounts are tracked"),
])
def test_direction_attribution_and_uuid_undo_survive_event_envelopes(world, monkeypatch, offline,
                                                                  route, event_kind, verb):
    from uuid import UUID

    from khipu import config

    monkeypatch.setattr(routing, "_hub_configured", lambda: offline)
    monkeypatch.setattr(routing, "_replication_live", lambda: offline)
    entity = None
    if route in {"rule.edit", "rule.retire"}:
        entity = routing.add_rule("Original rule", via="app")["entity"]
    if route == "hold.end":
        entity = routing.add_hold("Original hold", reason="keep", scope="all", owner=True, via="app")["entity"]
    auto_before = config.auto_track()
    commands = {
        "rule.add": ["route", "rule", "add", "Directed rule"],
        "rule.edit": ["route", "rule", "edit", entity, "Directed edit"],
        "rule.retire": ["route", "rule", "retire", entity],
        "priority.set": ["route", "priority", "set", "Codex", "--expect", routing.priority_version()],
        "hold.add": ["route", "hold", "Directed hold", "--all", "--reason", "keep"],
        "hold.end": ["route", "hold", "end", entity],
        "accounts.auto_track": ["accounts", "auto-track", "off" if auto_before else "on"],
    }
    marker = _marker()
    turn = world.say("Make this routing change on my direction")
    rc, out = world.run(commands[route], True)
    assert rc == 0 and out["ok"], out
    (line,) = _log()
    assert out["owner_action"] == line["id"]
    assert line["state"] == "applied" and not line.get("no_undo")
    assert (line["action"], line["auth"], line["session"], line["turn"]) == (
        route if route.startswith("accounts.") else "route." + route, "direction", OWNER, turn)
    # (A routing change by direction also writes its receipt, an owner_change line, after it.)
    event = [e for e in routing.read_events()[0] if not e.get("receipt")][-1]
    assert event["type"] == event_kind
    assert event["op"] == line["id"]
    assert event["written_by"] == {"kind": "owner"}
    assert (event["via"], event["auth"], event["harness"]) == (
        "direction", "direction", "claude_code")
    assert {"id", "mac", "seq", "hlc", "op"} <= event.keys()
    assert event["hlc"]["mac"] == event["mac"]
    if event_kind != "owner_change":
        entity = event["entity"]
        UUID(entity.split("-", 1)[1])
        assert "base" in event
    else:
        assert event["target"] == line["id"]
    if route in {"rule.edit", "rule.retire", "hold.end"}:
        assert event["target"] == line["target"] == entity
    if route == "rule.add":
        assert line["after"]["entity"] == owner_direction._added_rule(line) == entity
        assert (out["n"] is None) == offline
    push = _named(marker)
    assert verb in push["text"]
    assert "by your direction in a Claude Code conversation" in push["text"]
    assert "(undo in Settings)" in push["text"]

    world.say("Undo that routing change")
    rc, undone = world.run(["owner-direction", "undo", out["owner_action"]], True)
    assert rc == 0 and undone["ok"] and undone["undid"] == line["id"], undone
    assert owner_direction.line_state(line["id"]) == "undone"
    (undo_line,) = [row for row in owner_direction.read_log() if row.get("kind") == "undo"]
    assert undo_line["undoes"] == line["id"] and undo_line["state"] == "pending"
    assert owner_direction.line_state(undo_line["id"]) == "applied"
    inverse = [e for e in routing.read_events()[0] if not e.get("receipt")][-1]
    assert inverse["written_by"] == {"kind": "owner"} and inverse["via"] == "direction"
    view = routing.read_view()
    if route.startswith("rule."):
        assert inverse["entity"] == inverse["target"] == entity
        assert [r["text"] for r in view["rules"]] == ([] if route == "rule.add" else ["Original rule"])
    elif route == "priority.set":
        assert inverse["entity"] == entity and view["priority"] is None
    elif route == "hold.add":
        assert inverse["target"] == entity and view["holds"] == []
    elif route == "hold.end":
        assert [h["text"] for h in view["holds"]] == ["Original hold"]
        UUID(view["holds"][0]["entity"].split("-", 1)[1])
    else:
        assert config.auto_track() == auto_before
        assert inverse["target"] == undo_line["id"]


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


# ---- the fifth review's probe: every owner change is in the routing push ------------------------


def test_an_owner_change_outside_the_routing_record_is_named_in_the_next_push(world):
    """Fifth review, P2: widening the window (an owner-direction setting) is
    named once in the next routing push, with how it was made, and Undo is
    named too. A routing change is named once, from its own line."""
    world.say()
    baseline = routing.read_view()
    world.say()
    rc, out = world.run(["owner-direction", "set", "latest_window_h", "12"], True)
    assert rc == 0, out
    diff = routing.since(baseline["version"], routing.read_view())
    assert len(diff["directions"]) == 1 and diff["directions"][0].startswith(
        "Changed the owner-direction settings by your direction in "), diff
    assert not [e for e in diff["entities"] if e.startswith("owner:")], diff
    mark = routing.read_view()["version"]
    world.say()
    rc, out = world.run(["route", "pause", "Codex"], True)
    assert rc == 0, out
    diff = routing.since(mark, routing.read_view())
    assert len(diff["directions"]) == 1 and diff["directions"][0].startswith("Paused Codex"), diff
    mark = routing.read_view()["version"]
    line = next(a for a in _log() if a["action"] == "direction.set")
    world.say()
    rc, out = world.run(["owner-direction", "undo", line["id"]], True)
    assert rc == 0 and owner_turns.settings()["latest_window_h"] == 6.0, out
    diff = routing.since(mark, routing.read_view())
    assert [d.split(" by ")[0] for d in diff["directions"]] == ["Undid an owner change"], diff


def test_app_click_operation_retries_the_same_rule_through_the_cli(world, khipu_app):
    khipu_app.as_app()
    argv = ["route", "rule", "--from-app", "--op=synthetic-app-click", "--", "add", "Synthetic rule"]
    rc, first = world.run(argv)
    assert rc == 0 and first["ok"], first
    rc, second = world.run(argv)
    assert rc == 0 and second["ok"], second
    assert second["entity"] == first["entity"] and second["id"] == first["id"]
    assert len(routing.read_view()["rules"]) == 1
    assert routing.read_events()[0][0]["op"] == "synthetic-app-click"


def test_a_native_id_naming_an_old_and_a_recent_thread_never_counts(t3_conversation):
    """One native id on the old row and on a different recent thread is
    ambiguous across the whole table: it fails closed even though the recent
    row alone would have matched inside the newest window (Codex review)."""
    import os
    import sqlite3

    from khipu import t3

    with sqlite3.connect(os.environ["KHIPU_T3_DB"]) as con:
        con.execute("INSERT INTO orchestration_v2_projection_provider_threads VALUES (?, ?, ?)",
                    ("t3-other-thread", json.dumps({"nativeThreadRef": {"nativeId": OWNER}}), "2026-10-31"))
    assert t3.thread_for_session_result(OWNER, whole_table=True) == (None, True)
    _t3_say()
    row = owner_turns.turns(OWNER)[-1]
    # Ambiguous is still a T3 session, never its native entrypoint, and never counts.
    assert row["entrypoint"] == "t3" and row["eligible"] is False


# ---- every dispatch marker, and T3 delegated child threads --------------------------------------


def test_a_turn_with_khipu_job_is_not_eligible(world):
    owner_turns.record({"session_id": OWNER, "prompt": "pause Codex for now please", "cwd": "/repo"},
                       {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "KHIPU_JOB": "1"})
    (row,) = owner_turns.turns(OWNER)
    assert row["dispatched"] is True and row["eligible"] is False
    assert owner_turns.eligible(row)[0] is False
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check()
    assert err.value.code == "no_recorded_turn"


def test_a_host_id_that_is_this_session_is_not_a_job(world, monkeypatch):
    world.say()
    monkeypatch.setenv("CLAUDE_CODE_HOST_SESSION_ID", OWNER)
    assert _check()["auth"] == "direction"


def _add_t3_thread(thread: str, native: str, *, parent: str | None = None) -> None:
    import os
    import sqlite3

    with sqlite3.connect(os.environ["KHIPU_T3_DB"]) as con:
        con.execute("INSERT INTO orchestration_v2_projection_provider_threads VALUES (?, ?, ?)",
                    (thread, json.dumps({"nativeThreadRef": {"nativeId": native}}), "2026-10-31"))
        if parent is not None:
            con.execute("CREATE TABLE IF NOT EXISTS orchestration_v2_projection_threads "
                        "(thread_id TEXT, title TEXT, parent_thread_id TEXT)")
            con.execute("INSERT INTO orchestration_v2_projection_threads VALUES (?, ?, ?)",
                        (thread, "child", parent))


# T3's Agent SDK sets these for the owner's own top-level threads (measured
# 2026-10-31 on the owner's T3 conversation).
_T3_SDK_ENV = {"CLAUDE_CODE_CHILD_SESSION": "1", "CLAUDE_CODE_HOST_SESSION_ID": "s-t3-host"}


def _be_with(world, session, monkeypatch, env):
    world.be(session)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def test_a_t3_top_level_thread_with_the_sdk_child_markers_is_proved_by_t3s_record(t3_conversation, monkeypatch):
    _t3_say(**_T3_SDK_ENV)
    row = owner_turns.turns(OWNER)[-1]
    assert row["entrypoint"] == "t3" and row["eligible"] is False and row["why"] == _T3_WHY
    assert not row.get("dispatched")
    _be_with(t3_conversation, OWNER, monkeypatch, _T3_SDK_ENV)
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check("config.routing_setting")
    assert err.value.code == "t3_unreadable"


def test_a_t3_child_lookup_that_never_completes_fails_closed(t3_conversation, monkeypatch):
    from khipu import t3

    monkeypatch.setattr(t3, "delegated_thread_result", lambda thread: (False, False))
    _t3_say()
    row = owner_turns.turns(OWNER)[-1]
    assert row["entrypoint"] == "t3" and row["eligible"] is False


# ---- the Claude desktop app (the 2026-11-10 refusal) ---------------------------------------------

# The desktop app's environment for a conversation it shows (seen 2026-11-10):
# the hook's and the agent's (its MCP server's) alike.
_DESKTOP_ENV = {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "claude-desktop", "CLAUDE_CODE_CHILD_SESSION": "1",
                "CLAUDE_CODE_HOST_SESSION_ID": "local_852a9695-8a4e-43bc-abbd-14284cf79d21"}
_DESKTOP_ISSUER = {"pid": 4242, "start": 1.0, "exe": "claude", "headless": False, "tty": False, "app": None}


def _desktop_say(session: str = OWNER, text: str = "pause Codex until tonight", *, issuer=None) -> None:
    owner_turns.record({"session_id": session, "prompt": text, "cwd": "/repo"},
                       {**_DESKTOP_ENV, "CLAUDE_CODE_SESSION_ID": session},
                       issuer=_DESKTOP_ISSUER if issuer is None else issuer)


@pytest.fixture
def launched(monkeypatch):
    """The recorder's and the khipu MCP server's process trees read as the
    desktop app's own agent (``owner_turns.desktop_launched``)."""
    monkeypatch.setattr(owner_turns, "desktop_launched", lambda pid=None: True)


def test_a_desktop_conversation_the_owner_types_in_makes_the_change(world, launched, monkeypatch):
    _desktop_say()
    _be_with(world, OWNER, monkeypatch, _DESKTOP_ENV)
    assert owner_turns.dispatched_job(dict(__import__("os").environ), OWNER) is None
    assert _check()["auth"] == "direction"
    rc, out = world.run(["route", "pause", "Codex", "--until", "6h"], True)
    assert rc == 0 and _state("codex|") == "paused", out


def test_claude_p_from_a_desktop_shell_still_cannot(world, launched, monkeypatch):
    _desktop_say("s-child", issuer={**_DESKTOP_ISSUER, "print": True, "headless": True})
    _be_with(world, "s-child", monkeypatch, _DESKTOP_ENV)
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check()
    assert err.value.code == "no_recorded_turn"


def test_a_desktop_job_marker_still_refuses(world, launched, monkeypatch):
    _desktop_say()
    _be_with(world, OWNER, monkeypatch, {**_DESKTOP_ENV, "KHIPU_JOB": "1"})
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check()
    assert err.value.code == "no_recorded_turn" and "dispatched job" in err.value.message


def test_one_unread_t3_lookup_does_not_poison_a_conversation_t3_never_ran(t3_conversation, launched, monkeypatch):
    from khipu import t3

    session = "s-desktop"  # T3's record names no thread for it
    real = t3.session_thread_ids_result
    _desktop_say(session)
    monkeypatch.setattr(t3, "session_thread_ids_result", lambda sid, **kw: (None, False))
    _desktop_say(session)
    assert owner_turns.turns(session)[-1]["entrypoint"] == "t3-unverified"
    assert owner_turns.turns(session)[-1]["eligible"] is False  # that turn itself still never counts
    monkeypatch.setattr(t3, "session_thread_ids_result", real)
    _be_with(t3_conversation, session, monkeypatch, _DESKTOP_ENV)
    assert owner_direction._t3_conversation(session, owner_turns.turns(session)) is False
    assert _check()["auth"] == "direction"


def test_an_unread_t3_lookup_that_still_cannot_be_read_fails_closed(t3_conversation, launched, monkeypatch):
    from khipu import t3

    monkeypatch.setattr(t3, "session_thread_ids_result", lambda sid, **kw: (None, False))
    _desktop_say("s-desktop")
    _be_with(t3_conversation, "s-desktop", monkeypatch, _DESKTOP_ENV)
    assert owner_direction._t3_conversation("s-desktop", owner_turns.turns("s-desktop")) is True
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check()
    assert err.value.code == "t3_unreadable"


def test_a_t3_turn_keeps_the_t3_path_without_asking_again(t3_conversation, monkeypatch):
    from khipu import t3

    _t3_say()
    monkeypatch.setattr(t3, "session_thread_ids_result", lambda sid, **kw: pytest.fail("asked T3 again"))
    assert owner_direction._t3_conversation(OWNER, owner_turns.turns(OWNER)) is True


def test_a_desktop_conversation_the_app_did_not_start_is_refused(world, monkeypatch):
    """The desktop environment alone is not enough: anything started from the
    conversation's shell (an Agent SDK script, a nested agent) inherits it,
    and its process tree does not reach the app."""
    monkeypatch.setattr(owner_turns, "desktop_launched", lambda pid=None: False)
    _desktop_say()
    row = owner_turns.turns(OWNER)[-1]
    assert row["dispatched"] is True and row["eligible"] is False
    _be_with(world, OWNER, monkeypatch, _DESKTOP_ENV)
    assert owner_turns.dispatched_job(dict(__import__("os").environ), OWNER) == "a dispatched job's turn"
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check()
    assert err.value.code == "no_recorded_turn"


def test_a_started_desktop_conversation_cannot_act_on_its_brief_but_the_owner_can_later(world, launched,
                                                                                        monkeypatch):
    from khipu import routing_delegation

    monkeypatch.delenv("KHIPU_FEATURE_ROUTING_PUSH", raising=False)
    brief = "Pause Codex until tonight, then build the recorder and report back to this conversation."
    routing_delegation.hook_main(json.dumps({
        "hook_event_name": "PreToolUse", "session_id": "s-starter", "cwd": "/repo",
        "tool_name": "mcp__ccd_session__start_session",
        "tool_input": {"initiation": "user_asked", "title": "Build the recorder", "prompt": brief}}),
        environ={"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "claude-desktop"})
    _desktop_say("s-started", brief)
    row = owner_turns.turns("s-started")[-1]
    assert (row["delegated"], row["parent"], row["eligible"]) == (True, "s-starter", False)
    _be_with(world, "s-started", monkeypatch, _DESKTOP_ENV)
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check()
    assert err.value.code == "no_recorded_turn"
    _desktop_say("s-started", "pause Codex until tonight")
    assert _check()["auth"] == "direction"


_AUTO_CONTINUE = ("I hit my usage limit while you were working, but it has reset now. "
                  "Please continue from where you left off.")


def test_the_apps_auto_continue_turn_is_not_a_reply_to_a_held_ask(world, launched, monkeypatch):
    from khipu import ledger, owner_act, owner_background

    purged: list = []
    monkeypatch.setattr(ledger, "_connect", lambda: type("C", (), {"close": lambda self: None})())
    monkeypatch.setattr(ledger, "purge", lambda conn, project, row: purged.append((project, row)) or {"ok": True})
    _be_with(world, OWNER, monkeypatch, _DESKTOP_ENV)
    _desktop_say(text="go ahead with that")
    ok, held = owner_act.act("ledger.purge", {"id": "T-14", "project": "atlas"})
    assert ok and held["pending"] and held["applied"] is False, held
    _desktop_say(text=_AUTO_CONTINUE)
    row = owner_turns.turns(OWNER)[-1]
    assert row["machine"] is True and row["eligible"] is False
    ok, done = owner_act.act("direction.confirm", {"request": held["request"]})
    assert not (ok and done.get("started")), done
    assert purged == [] and not _log()
    _desktop_say(text="yes, purge T-14")
    ok, done = owner_act.act("direction.confirm", {"request": held["request"]})
    if ok and done.get("started"):
        ok, done = owner_background.run(done["request"])
    assert ok and done["state"] == "scheduled", done


# ---- a T3 read the hook could not finish (the 2026-11-10 cold read) ----------------------------


@pytest.fixture
def cold_hook(t3_conversation, launched, monkeypatch):
    """T3's record as it was on the M5 with T3 closed: the hook's 50 ms read
    never finishes, a read under the owner budget does. The owner-budget
    reads made are counted."""
    from khipu import t3

    real, owner_reads = t3.session_thread_ids_result, []

    def cold(sid, **kw):
        if not t3.owner_read_active():
            return None, False
        owner_reads.append(sid)
        return real(sid, **kw)

    monkeypatch.setattr(t3, "session_thread_ids_result", cold)
    t3_conversation.owner_reads = owner_reads
    return t3_conversation


def test_a_cold_hook_read_is_judged_again_at_the_check(cold_hook, monkeypatch):
    session = "s-desktop"  # T3's record names no thread for it
    _desktop_say(session)
    (row,) = owner_turns.turns(session)
    assert (row["entrypoint"], row["native"], row["eligible"]) == ("t3-unverified", "claude-desktop", False)
    _be_with(cold_hook, session, monkeypatch, _DESKTOP_ENV)
    stamp = _check()
    assert (stamp["auth"], stamp["session"], stamp["turn"]) == ("direction", session, row["turn"])
    assert cold_hook.owner_reads == [session]  # one read, under the owner budget
    rc, out = cold_hook.run(["route", "pause", "Codex", "--until", "6h"], True)
    assert rc == 0 and _state("codex|") == "paused", out


def test_a_cold_hook_read_in_a_conversation_t3_runs_keeps_the_t3_path(cold_hook, monkeypatch):
    _desktop_say(OWNER)  # T3's record names a thread for OWNER
    assert owner_turns.turns(OWNER)[-1]["entrypoint"] == "t3-unverified"
    _be_with(cold_hook, OWNER, monkeypatch, _DESKTOP_ENV)
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check()
    # T3's own record of the owner's messages decides; here it can't be read (as for any T3 turn).
    assert err.value.code == "t3_unreadable"


def test_a_cold_hook_read_still_unreadable_at_the_check_is_refused(cold_hook, monkeypatch):
    from khipu import t3

    _desktop_say("s-desktop")
    reads: list = []
    monkeypatch.setattr(t3, "session_thread_ids_result", lambda sid, **kw: reads.append(sid) or (None, False))
    _be_with(cold_hook, "s-desktop", monkeypatch, _DESKTOP_ENV)
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check()
    assert err.value.code == "t3_unreadable"
    # One bounded set of tries, not a second set on the T3 path (review of #570).
    assert len(reads) == owner_direction._OWNER_T3_ATTEMPTS


def test_a_cold_hook_read_still_unreadable_at_a_hold_reads_once(cold_hook, monkeypatch):
    from khipu import owner_act, t3

    session = "s-terminal"
    cold_hook.be(session)
    cold_hook.say("go ahead with that", session=session)
    assert owner_turns.turns(session)[-1]["entrypoint"] == "t3-unverified"
    reads: list = []
    monkeypatch.setattr(t3, "session_thread_ids_result", lambda sid, **kw: reads.append(sid) or (None, False))
    ok, out = owner_act.act("ledger.purge", {"id": "T-12", "project": "atlas"})
    assert not ok and "t3_unreadable" in str(out), out
    assert len(reads) == owner_direction._OWNER_T3_ATTEMPTS


def test_a_cold_hook_read_never_lifts_a_job_or_a_machine(cold_hook, monkeypatch):
    owner_turns.record({"session_id": "s-job", "prompt": "pause Codex until tonight", "cwd": "/repo"},
                       {**_DESKTOP_ENV, "CLAUDE_CODE_SESSION_ID": "s-job", "KHIPU_JOB": "1"}, issuer=_DESKTOP_ISSUER)
    owner_turns.record({"session_id": "s-sdk", "prompt": "pause Codex until tonight", "cwd": "/repo"},
                       {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "sdk-ts"}, issuer=_DESKTOP_ISSUER)
    for session in ("s-job", "s-sdk"):
        assert owner_turns.turns(session)[-1]["entrypoint"] == "t3-unverified"
        _be_with(cold_hook, session, monkeypatch, {"CLAUDECODE": "1"})
        with pytest.raises(owner_direction.DirectionRefused) as err:
            _check()
        assert err.value.code == "no_recorded_turn", session


def test_a_cold_hook_read_in_an_untrusted_front_end_says_which(cold_hook, monkeypatch):
    owner_turns.write_settings({"trusted": ["claude_code:cli"]})
    _desktop_say("s-desktop")
    _be_with(cold_hook, "s-desktop", monkeypatch, _DESKTOP_ENV)
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check()
    assert err.value.code == "no_recorded_turn" and "claude_code:claude-desktop" in err.value.message


def test_a_cold_hook_read_holds_and_confirms_a_change(cold_hook, monkeypatch):
    from khipu import ledger, owner_act, owner_background

    purged: list = []
    monkeypatch.setattr(ledger, "_connect", lambda: type("C", (), {"close": lambda self: None})())
    monkeypatch.setattr(ledger, "purge", lambda conn, project, row: purged.append((project, row)) or {"ok": True})
    session = "s-terminal"  # T3's record names no thread for it
    cold_hook.be(session)
    cold_hook.say("go ahead with that", session=session)
    assert owner_turns.turns(session)[-1]["entrypoint"] == "t3-unverified"
    ok, held = owner_act.act("ledger.purge", {"id": "T-12", "project": "atlas"})
    assert ok and held["pending"] and held["applied"] is False, held
    cold_hook.say("yes, purge T-12", session=session)
    ok, done = owner_act.act("direction.confirm", {"request": held["request"]})
    if ok and done.get("started"):
        ok, done = owner_background.run(done["request"])
    assert ok and done["state"] == "scheduled", done


# ---- prompts nobody typed in a desktop conversation (review of #568 and #570, 2026-11-11) ----
#
# Claude Code (2.1.321 and up) marks the origin of every user line in its
# transcript; the hook input does not carry it. Field shapes as a live
# session wrote them (values only, synthetic text).
_HUMAN = {"promptSource": "sdk", "turnOrigin": "human"}
_ORIGINS = {
    "scheduled": {"isMeta": True, "promptSource": "system", "wakeupSource": "schedule_wakeup",
                  "turnOrigin": "scheduled"},
    "peer": {"isMeta": True, "promptSource": "system", "turnOrigin": "peer"},
    "task_notification": {"promptSource": "system", "turnOrigin": "task_notification"},
    "sdk": {"promptSource": "sdk", "turnOrigin": "sdk"},
}


@pytest.fixture
def no_t3(world, launched, monkeypatch):
    monkeypatch.setenv("KHIPU_T3_DB", str(world.tmp / "absent.sqlite"))
    monkeypatch.delenv("CLAUDE_CODE_HOST_SCHEDULED_RUN", raising=False)
    world.transcript = world.tmp / "projects" / "repo" / "s-desk.jsonl"
    world.transcript.parent.mkdir(parents=True)
    return world


def _cc_line(path, text, at, marks):
    from datetime import datetime, timezone

    stamp = datetime.fromtimestamp(at, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    rows = [{"type": "user", "message": {"role": "user", "content": text}, "uuid": f"u-{at}", "timestamp": stamp,
             "sessionId": "s-desk", **marks},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "On it."}]},
             "uuid": f"a-{at}", "timestamp": stamp}]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in rows))


def _desk_prompt(world, text, at, *, session="s-desk", transcript=None, **env):
    payload = {"session_id": session, "prompt": text, "cwd": "/repo",
               "transcript_path": str(world.transcript if transcript is None else transcript)}
    turn = owner_turns.record(payload, {**_DESKTOP_ENV, "CLAUDE_CODE_SESSION_ID": session, **env}, now=at,
                              issuer=_DESKTOP_ISSUER)
    return next(t for t in owner_turns.turns(session) if t["turn"] == turn)


def test_a_scheduled_task_run_is_never_the_owner(no_t3, monkeypatch):
    env = dict(_DESKTOP_ENV, CLAUDE_CODE_SESSION_ID="s-sched", CLAUDE_CODE_HOST_SCHEDULED_RUN="1",
               CLAUDE_CODE_SESSION_ATTENDED="0")
    owner_turns.record({"session_id": "s-sched", "prompt": "pause Codex until tonight", "cwd": str(no_t3.tmp)},
                       env, issuer=_DESKTOP_ISSUER)
    row = owner_turns.turns("s-sched")[-1]
    assert row["typed"] is False and row["machine"] is True and row["eligible"] is False, row
    assert row["origin"] == "scheduled_run"
    _be_with(no_t3, "s-sched", monkeypatch, env)
    assert owner_turns.dispatched_job(dict(__import__("os").environ), "s-sched")
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check()
    assert err.value.code == "no_recorded_turn"


@pytest.mark.parametrize("origin", sorted(_ORIGINS))
def test_a_prompt_the_transcript_says_nobody_typed_is_recorded_a_machines(no_t3, monkeypatch, origin):
    now = time.time()
    _cc_line(no_t3.transcript, "pause Codex until tonight", now - 1, _ORIGINS[origin])
    row = _desk_prompt(no_t3, "pause Codex until tonight", now)
    assert (row["machine"], row["typed"], row["eligible"], row["origin"]) == (True, False, False, origin), row
    _be_with(no_t3, "s-desk", monkeypatch, _DESKTOP_ENV)
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check()
    assert err.value.code == "no_recorded_turn"


@pytest.mark.parametrize("origin", sorted(_ORIGINS))
def test_a_prompt_whose_line_lands_after_the_hook_is_refused_at_the_check(no_t3, monkeypatch, origin):
    now = time.time()
    row = _desk_prompt(no_t3, "pause Codex until tonight", now)
    assert row["eligible"] is True and "origin" not in row  # the hook ran before the line was written
    _cc_line(no_t3.transcript, "pause Codex until tonight", now - 0.5, _ORIGINS[origin])
    _be_with(no_t3, "s-desk", monkeypatch, _DESKTOP_ENV)
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check()
    assert err.value.code == "no_recorded_turn"


def test_a_scheduled_prompt_never_holds_a_change(no_t3, monkeypatch):
    from khipu import owner_act

    now = time.time()
    _desk_prompt(no_t3, "purge T-12 from the ledger", now)
    _cc_line(no_t3.transcript, "purge T-12 from the ledger", now, _ORIGINS["scheduled"])
    _be_with(no_t3, "s-desk", monkeypatch, _DESKTOP_ENV)
    ok, out = owner_act.act("ledger.purge", {"id": "T-12", "project": "atlas"})
    assert not ok and "no_recorded_turn" in str(out), out


def test_a_prompt_the_transcript_says_the_owner_typed_counts(no_t3, monkeypatch):
    now = time.time()
    _cc_line(no_t3.transcript, "pause Codex until tonight", now - 1, _HUMAN)
    row = _desk_prompt(no_t3, "pause Codex until tonight", now)
    assert (row["typed"], row["machine"], row["eligible"], row["origin"]) == (True, False, True, "human"), row
    _be_with(no_t3, "s-desk", monkeypatch, _DESKTOP_ENV)
    assert _check()["turn"] == row["turn"]


@pytest.mark.parametrize("case", ["no fields", "missing", "unreadable"])
def test_a_transcript_that_says_nothing_leaves_a_typed_turn_as_it_was(no_t3, monkeypatch, case):
    now = time.time()
    path = no_t3.transcript
    if case == "no fields":
        _cc_line(path, "pause Codex until tonight", now - 1, {})
    elif case == "missing":
        path = no_t3.tmp / "nowhere.jsonl"
    else:
        path.mkdir()  # a folder where the file should be: can't be read
    row = _desk_prompt(no_t3, "pause Codex until tonight", now, transcript=path)
    assert (row["typed"], row["machine"], row["eligible"]) == (True, False, True) and "origin" not in row, row
    _be_with(no_t3, "s-desk", monkeypatch, _DESKTOP_ENV)
    assert _check()["turn"] == row["turn"]


def test_the_check_picks_the_typed_turn_between_scheduled_ones(no_t3, monkeypatch):
    now = time.time()
    first = _desk_prompt(no_t3, "lane check: pause Codex", now - 120)  # its line lands after the hook
    _cc_line(no_t3.transcript, "lane check: pause Codex", now - 120, _ORIGINS["scheduled"])
    _cc_line(no_t3.transcript, "pause Codex until tonight", now - 60, _HUMAN)  # written before the hook ran
    typed = _desk_prompt(no_t3, "pause Codex until tonight", now - 60)
    _cc_line(no_t3.transcript, "lane check: pause Codex", now - 1, _ORIGINS["scheduled"])
    last = _desk_prompt(no_t3, "lane check: pause Codex", now - 1)
    assert first["eligible"] is True and "origin" not in first
    assert typed["eligible"] is True and typed["origin"] == "human"
    assert last["eligible"] is False and last["origin"] == "scheduled"
    judged = {t["turn"]: t for t in owner_turns.with_origins(owner_turns.turns("s-desk"))}
    assert judged[first["turn"]]["origin"] == "scheduled"
    assert not owner_turns.eligible(judged[first["turn"]])[0] and owner_turns.eligible(judged[typed["turn"]])[0]
    _be_with(no_t3, "s-desk", monkeypatch, _DESKTOP_ENV)
    assert _check()["turn"] == typed["turn"]
