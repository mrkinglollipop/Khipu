"""Owner direction, slice 2 of docs/plans/2026-10-16-owner-direction.md: the
check (``owner_direction.check``), the direction branch of every owner gate,
the owner-actions log, Undo and the scheduled purge.

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

    def say(self, text: str, *, session: str = OWNER, harness: str = "claude_code", entry: str = "cli",
            ago: float = 0.0) -> str | None:
        """The owner types ``text`` into ``session``: what the prompt hook
        records, and the token it pushes (None for Aegis)."""
        env = {"aegis": {"GROK_HOOK_NAME": "UserPromptSubmit"},
               "claude_code": {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": entry}}[harness]
        payload = {"session_id": session, "prompt": text, "cwd": str(self.tmp)}
        token = owner_turns.record(payload, env, now=time.time() - ago)
        if harness == "aegis":
            return None
        assert token or entry != "cli", "the recorder refused an owner turn"
        return token or owner_turns.turns(session)[-1]["turn"]

    def be(self, session: str | None, *, var: str = "CLAUDE_CODE_SESSION_ID") -> None:
        """The agent's process environment (its session id)."""
        for name in _SESSION_VARS:
            self.mp.delenv(name, raising=False)
        if session:
            self.mp.setenv(var, session)

    def run(self, argv: list[str], words: str | None = None, token: str | None = None) -> tuple[int, dict]:
        from khipu import cli

        if words is not None:
            argv = argv + ["--owner-words", words]
        if token is not None:
            argv = argv + ["--owner-turn", token]
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
    return [line for line in owner_direction.read_log() if line.get("kind") == "action"]


# ---- the words: normalising and matching ---------------------------------------------------


def test_the_quote_is_normalised_and_matched_whole_words_only():
    turn = "Please “pause”   Codex’s lane, now."
    assert owner_direction.quote_in("pause\" codex's lane", turn)
    assert owner_direction.quote_in("“PAUSE” CODEX’S LANE.", turn)
    assert not owner_direction.quote_in("ause codex's lane", turn)
    assert not owner_direction.quote_in("pause codexs lane", turn)
    assert owner_direction.normalise("  A‘b’  C ") == "a'b' c"


# ---- each refusal code, by name -------------------------------------------------------------


def _check(action="route.pause", target="codex|", words="pause Codex for now", **kw):
    return owner_direction.check(action, target, words, **kw)


def test_refusal_direction_off(world):
    token = world.say("pause Codex for now")
    owner_turns.write_settings({"enabled": False})
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check(turn_token=token)
    assert err.value.code == "direction_off"


def test_refusal_no_recorded_turn_without_a_session(world):
    token = world.say("pause Codex for now")
    world.be(None)
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check(turn_token=token)
    assert err.value.code == "no_recorded_turn"


def test_refusal_no_recorded_turn_for_an_unknown_token(world):
    world.say("pause Codex for now")
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check(turn_token="0badc0de")
    assert err.value.code == "no_recorded_turn"


def test_refusal_no_recorded_turn_for_an_ineligible_front_end(world):
    token = world.say("pause Codex for now", entry="sdk-cli")
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check(turn_token=token)
    assert err.value.code == "no_recorded_turn" and "not a trusted front end" in err.value.message


def test_refusal_stale(world):
    token = world.say("pause Codex for now", ago=7 * 3600)
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check(turn_token=token)
    assert err.value.code == "stale"


def test_refusal_not_the_owners_words(world):
    token = world.say("pause Codex for now")
    for words in ("pause Codex for a while", "Codex now", "pause"):
        with pytest.raises(owner_direction.DirectionRefused) as err:
            _check(words=words, turn_token=token)
        assert err.value.code == "not_the_owners_words", words


def test_refusal_words_do_not_ask_for_this(world):
    token = world.say("pause Grok for now, and look at Codex")
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check(words="pause Grok for now", turn_token=token)
    assert err.value.code == "words_do_not_ask_for_this" and "Codex" in err.value.message
    with pytest.raises(owner_direction.DirectionRefused) as err:
        _check(words="look at Codex", turn_token=token)
    assert err.value.code == "words_do_not_ask_for_this" and "pause" in err.value.message


def test_refusal_already_done_answers_with_the_first_result(world):
    token = world.say("pause Codex for now")
    rc, first = world.run(["route", "pause", "Codex"], "pause Codex for now", token)
    assert rc == 0 and first["ok"] is True
    lines = len(routing.read_events()[0])
    rc, again = world.run(["route", "pause", "Codex"], "pause Codex for now", token)
    assert rc == 0 and again["already_done"] is True and again["id"] == first["id"]
    assert len(routing.read_events()[0]) == lines and len(_log()) == 1


def test_refusal_needs_second_turn_then_a_later_yes_applies_it(world):
    token = world.say("make the latest window 12 hours")
    rc, out = world.run(["owner-direction", "set", "latest_window_h", "12"], "make the latest window 12 hours",
                        token)
    assert rc == 2 and out["code"] == "needs_second_turn"
    assert owner_turns.settings()["latest_window_h"] == 6.0
    yes = world.say("yes do it now")
    rc, out = world.run(["owner-direction", "set", "latest_window_h", "12"], "yes do it now", yes)
    assert rc == 0 and out["ok"] is True
    assert owner_turns.settings()["latest_window_h"] == 12.0


def test_a_widening_confirmed_from_its_own_turn_is_refused(world):
    token = world.say("make the latest window 12 hours, yes do it")
    rc, out = world.run(["owner-direction", "set", "latest_window_h", "12"], "make the latest window 12 hours",
                        token)
    assert out["code"] == "needs_second_turn"
    rc, out = world.run(["owner-direction", "set", "latest_window_h", "12"], "make the latest window 12 hours, "
                        "yes do it", token)
    assert rc == 2 and out["code"] == "needs_second_turn"
    assert owner_turns.settings()["latest_window_h"] == 6.0


def test_tightening_the_check_takes_one_turn(world):
    token = world.say("make the latest window 2 hours")
    rc, out = world.run(["owner-direction", "set", "latest_window_h", "2"], "make the latest window 2 hours", token)
    assert rc == 0 and owner_turns.settings()["latest_window_h"] == 2.0
    token = world.say("turn owner direction off for now")
    rc, out = world.run(["owner-direction", "off"], "turn owner direction off", token)
    assert rc == 0 and owner_turns.settings()["enabled"] is False


def test_a_change_with_no_undo_is_applied_by_direction_and_logged_as_such(world):
    token = world.say("clear Codex it is fine")
    rc, out = world.run(["route", "clear", "Codex"], "clear Codex it is fine", token)
    assert rc == 0 and out["ok"] is True
    (line,) = _log()
    assert line["action"] == "route.clear" and line["auth"] == "direction"
    assert line["undo"] is None and "can't be undone" in line["no_undo"]
    undo = world.say("undo that clear of Codex")
    rc, out = world.run(["owner-direction", "undo", line["id"]], "undo that clear of Codex", undo)
    assert rc == 2 and "can't be undone" in out["error"]


def test_refusal_before_unreadable(world, monkeypatch):
    from khipu import ledger

    def away(*a, **k):
        raise ledger.LedgerError("the database is away", "away")

    monkeypatch.setattr(ledger, "get", away)
    monkeypatch.setattr(ledger, "write", lambda op: pytest.fail("written without its before-state"))
    token = world.say("mark T-12 as done in the ledger")
    rc, out = world.run(["ledger", "set", "T-12", "--state", "done", "--project", "atlas", "--json"],
                        "mark T-12 as done in the ledger", token)
    assert rc == 1 and out["reason"] == "before_unreadable"
    assert _log() == []


def test_a_bare_yes_turn_confirms_a_widening(world):
    token = world.say("make the latest window 12 hours")
    rc, out = world.run(["owner-direction", "set", "latest_window_h", "12"], "make the latest window 12 hours",
                        token)
    assert out["code"] == "needs_second_turn"
    yes = world.say("Yes!")
    rc, out = world.run(["owner-direction", "set", "latest_window_h", "12"], "yes", yes)
    assert rc == 0 and owner_turns.settings()["latest_window_h"] == 12.0


def test_a_short_yes_inside_a_longer_turn_does_not_confirm(world):
    token = world.say("make the latest window 12 hours")
    world.run(["owner-direction", "set", "latest_window_h", "12"], "make the latest window 12 hours", token)
    later = world.say("yes but first tell me what that means")
    rc, out = world.run(["owner-direction", "set", "latest_window_h", "12"], "yes", later)
    assert rc == 2 and out["code"] == "not_the_owners_words"
    assert owner_turns.settings()["latest_window_h"] == 6.0


def test_a_short_quote_never_carries_any_other_action(world):
    token = world.say("yes")
    rc, out = world.run(["route", "pause", "Codex"], "yes", token)
    assert rc == 2 and out["code"] == "not_the_owners_words"


def test_refusal_not_by_direction(world):
    token = world.say("turn owner direction on again")
    rc, out = world.run(["owner-direction", "on"], "turn owner direction on", token)
    assert rc == 2 and out["code"] == "not_by_direction"


# ---- one passing and one refused case per action --------------------------------------------

# action: (target, words that ask for it, words that do not, extra check arguments)
CASES: dict[str, tuple] = {
    "route.pause": ("codex|", "pause Codex until five", "pause Claude until five", {}),
    "route.lift": ("codex|", "lift the pause on Codex", "lift the pause on Grok", {}),
    "route.clear": ("codex|", "clear Codex it is fine", "clear Grok it is fine", {}),
    "route.note.set": ("note", "set the note to builds go to Codex", "set the note to reviews go to Codex",
                       {"stored": ["builds go to Codex"]}),
    "route.note.end": ("note", "end the note now please", "end the hold now please", {}),
    "route.rule.add": ("new", "add a rule: never run the fast tier", "add a rule: never run the slow tier",
                       {"stored": ["never run the fast tier"]}),
    "route.rule.edit": ("3", "change rule 3 to say Grok never builds", "change rule 4 to say Grok never builds",
                        {"stored": ["Grok never builds"]}),
    "route.rule.retire": ("3", "retire rule 3 it is done", "retire rule 4 it is done", {}),
    "route.rule.restore": ("3", "restore rule 3 after all", "restore rule 5 after all", {}),
    "route.rule.confirm": ("3", "confirm rule 3 as written", "keep the note as written", {}),
    "route.rule.undo": ("3", "undo the change to rule 3", "undo the change to rule 2", {}),
    "route.rule.dismiss": ("3", "dismiss rule 3 the new one", "dismiss rule 7 the new one", {}),
    "route.hold.add": ("account:codex|", "hold Codex while the Mac updates", "hold Grok while the Mac updates",
                       {"stored": ["while the Mac updates"]}),
    "route.hold.end": ("h-1", "end the hold on Codex now", "end the hold on Grok now",
                       {"names": [["codex", "h-1"]]}),
    "route.priority.set": ("codex|,claude|/logins/claude-second", "priority Codex first then Claude second",
                           "priority Codex first then Grok", {}),
    "route.priority.confirm": ("priority", "confirm the new priority please", "confirm the new rule please", {}),
    "route.priority.undo": ("priority", "undo the priority change please", "undo the rule change please", {}),
    "route.priority.clear": ("priority", "clear the priority for today", "clear the note for today", {}),
    "accounts.track": ("codex|/logins/codex-work", "track the Codex work login",
                       "track the Grok work login", {"names": [["codex"]]}),
    "accounts.rename": ("codex|", "rename Codex to Codex main", "rename Grok to Codex main",
                        {"stored": ["Codex main"]}),
    "accounts.stop": ("grok|", "stop tracking Grok for good", "stop tracking Codex for good", {}),
    "accounts.move": ("codex|", "Codex moved to /logins/codex-two now", "Grok moved to /logins/codex-two now",
                      {"stored": ["/logins/codex-two"]}),
    "accounts.decline": ("codex", "don't offer the Codex login again", "don't offer the Grok login again", {}),
    "accounts.offer_again": ("codex", "offer the Codex login again", "offer the Grok login again", {}),
    "accounts.auto_track": ("on", "turn auto track on please", "turn auto track off please", {}),
    "accounts.request.confirm": ("request", "confirm that account list change", "undo that account list change",
                                 {"names": [["account list", "that"]]}),
    "accounts.request.undo": ("request", "undo that account list change", "confirm that account list change",
                              {"names": [["account list", "that"]]}),
    "config.usage_accounts": ("list", "track Grok in the accounts too", "track Muse in the accounts too",
                              {"names": [["grok"]]}),
    "config.routing_setting": ("routing_stale_after", "set stale after to 6h please",
                               "set stale after to 8h please", {"stored": ["6h"]}),
    "config.answer_request.confirm": ("routing_stale_after", "confirm that stale after change",
                                      "undo that stale after change", {}),
    "config.answer_request.undo": ("routing_stale_after", "undo that stale after change",
                                   "confirm that stale after change", {}),
    "promote.gate_ignore": ("hub_reach", "update anyway and skip hub reach", "update anyway and skip the disk",
                            {}),
    "promote.gate_ignore.cancel": ("update anyway", "cancel the update anyway", "cancel the note for now", {}),
    "ledger.import": ("/trackers/tracker.json", "import the tracker.json file now", "import the board file now",
                      {}),
    "ledger.restore": ("atlas#T-12", "restore T-12 in the ledger", "restore T-13 in the ledger", {}),
    "ledger.purge": ("atlas#T-12", "purge T-12 from the ledger", "purge T-13 from the ledger", {}),
    "ledger.add": ("atlas#T-14", "add T-14 to the ledger as ship the docs", "add T-15 to the ledger as ship the docs",
                   {"stored": ["ship the docs"]}),
    "ledger.set": ("atlas#T-12", "mark T-12 as done in the ledger", "mark T-12 as blocked in the ledger",
                   {"stored": ["done"]}),
    "ledger.remove": ("atlas#T-12", "remove T-12 from the ledger", "remove T-13 from the ledger", {}),
    "work.hide": ("42", "hide commitment 42 for now", "hide commitment 43 for now", {}),
    "work.unhide": ("42", "unhide commitment 42 please", "unhide commitment 43 please", {}),
    "work.same": ("atlas#T-12|42", "T-12 and 42 are the same", "T-12 and 42 are not the same", {}),
    "work.not_same": ("atlas#T-12|42", "T-12 and 42 are not the same", "T-12 and 43 are not the same", {}),
    "direction.off": ("direction", "turn owner direction off", "turn the note off", {}),
    "direction.set": ("latest_window_h", "make the latest window 2 hours", "make the earlier window 2 hours",
                      {"stored": ["2"]}),
    "direction.undo": ("a1b2c3", "undo that pause please", "pause that one please", {"names": [["that"]]}),
}


def test_every_action_has_a_case():
    assert set(CASES) == set(owner_direction.ACTIONS)


@pytest.mark.parametrize("action", sorted(CASES))
def test_each_action_passes_its_words_and_refuses_others(world, action):
    target, good, bad, extra = CASES[action]
    session = f"s-{action}"
    world.be(session)
    token = world.say(f"{good}. Then {bad}.", session=session)
    stamp = owner_direction.check(action, target, good, turn_token=token, **extra)
    assert stamp["auth"] == "direction" and stamp["session"] == session and stamp["binding"] == "token"
    with pytest.raises(owner_direction.DirectionRefused) as err:
        owner_direction.check(action, target, bad, turn_token=token, **extra)
    assert err.value.code == "words_do_not_ask_for_this", action


# ---- the failure modes of scope section 2.6 --------------------------------------------------


def test_forged_session_id_with_a_token_from_another_session(world):
    token = world.say("pause Codex for now", session="s-other")
    world.say("hello there my friend")
    world.be(OWNER)
    rc, out = world.run(["route", "pause", "Codex"], "pause Codex for now", token)
    assert rc == 2 and out["code"] == "no_recorded_turn"
    assert not routing.events_file().exists()


def test_forged_session_without_a_token_on_a_claude_code_session(world):
    world.say("pause Codex for now")
    rc, out = world.run(["route", "pause", "Codex"], "pause Codex for now")
    assert rc == 2 and out["code"] == "no_recorded_turn" and "Owner turn" in out["error"]
    assert not routing.events_file().exists()


def test_session_only_binding_for_aegis(world):
    world.say("pause Codex for now", session="s-aegis", harness="aegis")
    world.be("s-aegis", var="KHIPU_SESSION_ID")
    rc, out = world.run(["route", "pause", "Codex"], "pause Codex for now")
    assert rc == 0 and _state("codex|") == "paused"
    (line,) = _log()
    assert line["binding"] == "session" and line["harness"] == "aegis"


def test_stale_prompt_replay_is_refused(world):
    pause = world.say("pause Codex until I say", ago=50 * 60)
    world.say("lift Codex it is fine again", ago=10 * 60)
    rc, out = world.run(["route", "pause", "Codex"], "pause Codex until I say", pause)
    assert rc == 2 and out["code"] == "stale"
    assert not routing.events_file().exists()


def test_paraphrase_is_refused(world):
    token = world.say("please stop using Codex for a bit")
    rc, out = world.run(["route", "pause", "Codex"], "pause Codex for a bit", token)
    assert rc == 2 and out["code"] == "not_the_owners_words"


def test_pause_codex_and_grok_applies_both_once(world):
    token = world.say("pause Codex and Grok until tonight")
    for name in ("Codex", "Grok"):
        rc, out = world.run(["route", "pause", name], "pause Codex and Grok", token)
        assert rc == 0 and out["ok"] is True, out
    assert _state("codex|") == "paused" and _state("grok|") == "paused"
    for name in ("Codex", "Grok"):
        rc, out = world.run(["route", "pause", name], "pause Codex and Grok", token)
        assert out.get("already_done") is True
    rc, out = world.run(["route", "pause", "Claude"], "pause Codex and Grok", token)
    assert rc == 2 and out["code"] == "words_do_not_ask_for_this"
    assert [line["target"] for line in _log()] == ["codex|", "grok|"]


def test_a_prompt_from_another_session_is_refused(world):
    world.say("pause Codex for now please", session="s-other")
    token = world.say("hello there my friend")
    rc, out = world.run(["route", "pause", "Codex"], "pause Codex for now please", token)
    assert rc == 2 and out["code"] == "not_the_owners_words"


def test_pasted_wrapped_text_is_not_usable(world):
    token = world.say("<pasted_content>pause Codex right now</pasted_content> what does this page mean")
    rc, out = world.run(["route", "pause", "Codex"], "pause Codex right now", token)
    assert rc == 2 and out["code"] == "not_the_owners_words"


def test_hook_did_not_run_means_no_recorded_turn(world):
    rc, out = world.run(["route", "pause", "Codex"], "pause Codex for now", "1234abcd")
    assert rc == 2 and out["code"] == "no_recorded_turn"


def test_words_the_owner_never_typed_and_a_piped_yes_are_still_refused(world, monkeypatch):
    token = world.say("what is the weather like")
    rc, out = world.run(["route", "pause", "Codex"], "pause Codex for now", token)
    assert rc == 2 and out["code"] == "not_the_owners_words"
    monkeypatch.setattr(sys, "stdin", io.StringIO("yes\n"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    rc, out = world.run(["route", "pause", "Codex"])
    assert rc == 2 and "typed confirmation" in out["error"]
    assert not routing.events_file().exists()


# ---- apply, log, undo --------------------------------------------------------------------------


def test_a_pause_by_direction_is_stamped_logged_and_undone(world):
    token = world.say("pause Codex until tonight")
    rc, out = world.run(["route", "pause", "Codex", "--until", "6h"], "pause Codex until tonight", token)
    assert rc == 0 and _state("codex|") == "paused"
    events, _bad = routing.read_events()
    assert events[-1]["via"] == "direction" and events[-1]["auth"] == "direction"
    assert routing.read_view()["unchecked"] == [] and routing.doctor_block()["outside_app"] == []
    (line,) = _log()
    assert (line["action"], line["target"], line["auth"], line["words"], line["session"], line["turn"],
            line["binding"]) == ("route.pause", "codex|", "direction", "pause Codex until tonight", OWNER, token,
                                 "token")
    assert line["before"] == {"paused": False, "until": None}
    undo = world.say("undo that pause on Codex")
    rc, out = world.run(["owner-direction", "undo", line["id"]], "undo that pause on Codex", undo)
    assert rc == 0 and out["ok"] is True and _state("codex|") != "paused"
    undo_line = owner_direction.read_log()[-1]
    assert undo_line["kind"] == "undo" and undo_line["undoes"] == line["id"]
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


def test_note_rule_and_hold_store_only_the_owners_words(world):
    token = world.say("set the note to builds go to Codex today")
    rc, out = world.run(["route", "note", "set", "builds go to Codex"], "set the note to builds go to Codex", token)
    assert rc == 0 and routing.read_view()["note"]["text"] == "builds go to Codex"
    rc, out = world.run(["route", "note", "set", "builds go to Codex only"], "set the note to builds go to Codex",
                        token)
    assert rc == 2 and out["code"] == "words_do_not_ask_for_this"
    token = world.say("add a rule: never run the fast tier")
    rc, out = world.run(["route", "rule", "add", "never run the fast tier"], "add a rule: never run the fast tier",
                        token)
    assert rc == 0 and routing.read_view()["rules"][-1]["text"] == "never run the fast tier"
    token = world.say("hold Codex while the Mac updates")
    rc, out = world.run(["route", "hold", "while the Mac updates", "--account", "Codex", "--reason", "keep"],
                        "hold Codex while the Mac updates", token)
    assert rc == 0 and routing.read_view()["holds"][0]["owner"] is True


def test_a_routing_setting_by_direction_and_its_undo(world):
    token = world.say("set stale after to 6h please")
    rc, out = world.run(["config", "--set", "routing_stale_after", "6h"], "set stale after to 6h", token)
    assert rc == 0 and out["ok"] is True and routing.settings()["routing_stale_after"] == 6 * 3600
    (line,) = _log()
    assert line["action"] == "config.routing_setting" and line["before"] == {"value": None, "set": False}
    undo = world.say("undo the stale after change")
    rc, out = world.run(["owner-direction", "undo", line["id"]], "undo the stale after change", undo)
    assert rc == 0 and routing.settings()["routing_stale_after"] == 12 * 3600


def test_work_hide_by_direction_goes_through_the_ledger_guard(world, monkeypatch):
    from khipu import work

    monkeypatch.setattr(work, "read_owed", lambda: {"items": [{"id": "42", "text": "x"}], "error": None})
    token = world.say("hide commitment 42 for now")
    rc, out = world.run(["work", "hide", "42"], "hide commitment 42 for now", token)
    assert rc == 0 and "42" in work.read_hidden()
    (line,) = _log()
    assert line["action"] == "work.hide" and line["before"] == {"hidden": None}
    rc, out = world.run(["work", "hide", "43"], "hide commitment 42 for now", token)
    assert rc == 1 and out["reason"] == "words_do_not_ask_for_this"


# ---- the scheduled purge ---------------------------------------------------------------------


def test_purge_by_direction_is_scheduled_runs_a_day_later_and_undo_cancels_it(world, monkeypatch):
    from khipu import ledger

    purged: list = []
    monkeypatch.setattr(ledger, "_connect", lambda: type("C", (), {"close": lambda self: None})())
    monkeypatch.setattr(ledger, "purge", lambda conn, project, row: purged.append((project, row)) or {"ok": True})
    token = world.say("purge T-12 from the ledger")
    rc, out = world.run(["ledger", "purge", "T-12", "--project", "atlas", "--json"], "purge T-12 from the ledger",
                        token)
    assert rc == 0 and out["state"] == "scheduled" and purged == []
    (line,) = _log()
    assert line["state"] == "scheduled" and line["runs_at"]
    owner_direction.run_due(now=time.time() + 3600)
    assert purged == []
    owner_direction.run_due(now=time.time() + 25 * 3600)
    assert purged == [("atlas", "T-12")]
    owner_direction.run_due(now=time.time() + 26 * 3600)
    assert purged == [("atlas", "T-12")]

    token = world.say("purge T-13 from the ledger")
    rc, out = world.run(["ledger", "purge", "T-13", "--project", "atlas", "--json"], "purge T-13 from the ledger",
                        token)
    second = _log()[-1]
    undo = world.say("undo the purge of T-13")
    rc, out = world.run(["owner-direction", "undo", second["id"]], "undo the purge of T-13", undo)
    assert rc == 0
    owner_direction.run_due(now=time.time() + 25 * 3600)
    assert purged == [("atlas", "T-12")]
    assert owner_direction.line_state(second["id"]) == "cancelled"


def test_the_actions_list_is_newest_first(world):
    for text, name in (("pause Codex for now", "Codex"), ("pause Grok for now", "Grok")):
        token = world.say(text)
        world.run(["route", "pause", name], text, token)
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
    token = world.say("pause Codex until tonight please")
    rc, _ = world.run(["route", "pause", "Codex", "--until", "6h"], "pause Codex until tonight", token)
    assert rc == 0
    events, _bad = routing.read_events()
    at = routing._clock(routing._parse(events[-1]["written_at"]), routing._now())
    push = routing.prompt_push(marker, budget=5.0)
    assert push["kind"] == "changes"
    assert f"Paused Codex by your direction in a Claude Code conversation, {at} (undo in Settings)." in push["text"]
    # Fixed words only: never the session, the turn or the owner's words.
    assert OWNER not in push["text"] and token not in push["text"] and "tonight" not in push["text"]
    seen = {"version": push["version"], "epoch": push["epoch"], "full_at": marker["full_at"]}
    assert routing.prompt_push(seen, budget=5.0)["kind"] == "none"
    # Its undo by direction is named the same way, and so is the app's own change.
    line = _log()[-1]
    undo = world.say("undo that pause on Codex")
    rc, _ = world.run(["owner-direction", "undo", line["id"]], "undo that pause on Codex", undo)
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
    token = world.say("add a rule: never run the fast tier")
    rc, _ = world.run(["route", "rule", "add", "never run the fast tier"], "add a rule: never run the fast tier",
                      token)
    assert rc == 0
    push = routing.prompt_push(marker, budget=5.0)
    assert "Added standing rule 1 by your direction in a Claude Code conversation" in push["text"]


def test_the_apps_owner_direction_argv_apply_at_once_and_only_from_the_app(world, khipu_app):
    """Settings, Owner actions: the argv the app's Rust commands build
    (``owner_direction_*_argv`` in lib.rs). The app is the owner, so a
    widening needs no second turn; the same argv from a shell is refused."""
    rc, out = world.run(["owner-direction", "set", "--from-app", "--", "latest_window_h", "12"])
    assert rc == 2 and out["code"] == "owner_only"
    khipu_app.as_app()
    for argv in (["set", "--from-app", "--", "latest_window_h", "12"],
                 ["set", "--from-app", "--", "earlier_window_min", "45"],
                 ["set", "--from-app", "--", "trust", "claude_code:sdk-cli"],
                 ["set", "--from-app", "--", "session_binding", "off"],
                 ["off", "--from-app"], ["on", "--from-app"]):
        rc, out = world.run(["owner-direction", *argv])
        assert rc == 0 and out["ok"] is True, argv
    conf = owner_turns.settings()
    assert (conf["latest_window_h"], conf["earlier_window_min"], conf["session_binding"], conf["enabled"]) == \
        (12, 45, False, True)
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
