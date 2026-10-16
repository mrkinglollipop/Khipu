"""Owner direction, slice 3 of docs/plans/2026-10-16-owner-direction.md: the
``khipu_owner_act`` MCP tool, a dispatched job acting on its parent's words
(``owner_session``), and pending requests confirmed by direction.

Every call goes through ``mcp_server.handle_message`` as a client sends it.
The server's process environment is faked per harness (Claude Code, Codex,
Aegis, none): the session is read from there, never from the arguments.
Turns are recorded the way the prompt hook records them. Hermetic: a temp
data dir and capture home; nothing starts a harness or reaches the hub."""
from __future__ import annotations

import json
import sys
import time

import pytest

from khipu import mcp_server as ms
from khipu import owner_direction, owner_turns, routing
from khipu.mcp_server import handle_message

ACCOUNTS = [{"label": "Claude", "harness": "claude"},
            {"label": "Claude second", "harness": "claude", "home": "/logins/claude-second"},
            {"label": "Codex", "harness": "codex"},
            {"label": "Grok", "harness": "grok"}]
_ENV = ("KHIPU_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
        "KHIPU_HARNESS", "GROK_HOOK_NAME", "GROK_HOOK_EVENT", "CODEX_HOME", ms.GATEWAY_ACTIVE_ENV)
_SESSION_VAR = {"claude_code": "CLAUDE_CODE_SESSION_ID", "codex": "CODEX_THREAD_ID", "aegis": "KHIPU_SESSION_ID"}
BRIEF = "Build the docs page for the routing record. The owner said: pause Codex until tonight."


class World:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.mp = monkeypatch

    def say(self, text: str, *, session: str, harness: str = "claude_code", entry: str = "cli") -> str | None:
        """A turn the prompt hook records for ``session``; the token it
        pushes (None for Aegis and for an ineligible turn)."""
        payload = {"session_id": session, "prompt": text, "cwd": str(self.tmp)}
        if harness == "claude_code":
            env = {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": entry}
        elif harness == "codex":
            rollout = self.tmp / f"rollout-{session}.jsonl"
            rollout.write_text(json.dumps({"type": "session_meta", "payload": {"originator": entry}}) + "\n")
            env = {"CODEX_THREAD_ID": session}
            payload["transcript_path"] = str(rollout)
        else:
            env = {"GROK_HOOK_NAME": "UserPromptSubmit"}
        return owner_turns.record(payload, env, now=time.time())

    def server(self, session: str | None, harness: str = "claude_code") -> None:
        """The MCP server's process environment, as the harness started it."""
        for name in _ENV:
            self.mp.delenv(name, raising=False)
        if session:
            self.mp.setenv(_SESSION_VAR[harness], session)


@pytest.fixture
def world(tmp_path, monkeypatch, khipu_app):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": ACCOUNTS}))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setattr(ms, "_GATEWAY_ACTIVE", False)
    khipu_app.as_shell("/opt/harness/claude")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    owner_turns.write_settings({"enabled": True})
    w = World(tmp_path, monkeypatch)
    w.server(None)
    return w


def _act(action: str, args: dict | None = None, words: str | None = None, turn: str | None = None,
         **extra) -> tuple[bool, dict]:
    arguments = {"action": action, "args": args or {}, **extra}
    if words is not None:
        arguments["words"] = words
    if turn is not None:
        arguments["turn"] = turn
    out = handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "khipu_owner_act", "arguments": arguments}})
    result = out["result"]
    return result["isError"], json.loads(result["content"][0]["text"])


def _state(label: str) -> str:
    return next(a for a in routing.read_view()["accounts"] if a["label"] == label)["state"]


def _log() -> list[dict]:
    return [line for line in owner_direction.read_log() if line.get("kind") == "action"]


# ---- the tool, per harness -------------------------------------------------------------------


@pytest.mark.parametrize("harness,entry", [("claude_code", "cli"), ("codex", "codex_cli_rs"), ("aegis", "aegis")])
def test_a_pause_passes_from_each_harness(world, harness, entry):
    token = world.say("pause Codex until tonight please", session="s-1", harness=harness, entry=entry)
    world.server("s-1", harness)
    is_error, out = _act("route.pause", {"account": "Codex"}, "pause Codex until tonight", token)
    assert not is_error and out["ok"] is True, out
    assert _state("Codex") == "paused"
    (line,) = _log()
    assert (line["action"], line["auth"], line["session"], line["harness"]) == ("route.pause", "direction", "s-1",
                                                                              harness)
    assert line["binding"] == ("session" if harness == "aegis" else "token")


def test_no_session_in_the_servers_environment_is_refused(world):
    token = world.say("pause Codex until tonight please", session="s-1")
    world.server(None)
    is_error, out = _act("route.pause", {"account": "Codex"}, "pause Codex until tonight", token)
    assert is_error and out["ok"] is False and out["code"] == "no_recorded_turn"
    assert _state("Codex") != "paused"


def test_a_paraphrase_is_refused(world):
    token = world.say("please stop using Codex for a bit", session="s-1")
    world.server("s-1")
    is_error, out = _act("route.pause", {"account": "Codex"}, "pause Codex for a bit", token)
    assert is_error and out["code"] == "not_the_owners_words" and "error" in out
    assert _state("Codex") != "paused"


def test_a_forged_session_id_is_refused(world):
    token = world.say("pause Codex until tonight please", session="s-a")
    world.say("hello there my friend", session="s-b")
    world.server("s-b")
    is_error, out = _act("route.pause", {"account": "Codex"}, "pause Codex until tonight", token)
    assert is_error and out["code"] == "no_recorded_turn"
    assert _state("Codex") != "paused"


def test_the_session_is_never_taken_from_the_arguments(world):
    token = world.say("pause Codex until tonight please", session="s-a")
    world.server("s-b")
    for key in ("session", "session_id"):
        is_error, out = _act("route.pause", {"account": "Codex"}, "pause Codex until tonight", token, **{key: "s-a"})
        assert is_error and _state("Codex") != "paused"


@pytest.mark.parametrize("args", [{"account": "Codex", "from_app": True}, {"account": "Codex", "owner_words": "x"},
                                  {"account": "--from-app"}, {"account": 3}, {}])
def test_arguments_are_checked_strictly(world, args):
    token = world.say("pause Codex until tonight please", session="s-1")
    world.server("s-1")
    is_error, out = _act("route.pause", args, "pause Codex until tonight", token)
    assert is_error and out["ok"] is False
    assert _state("Codex") != "paused" and _log() == []


def test_an_unknown_action_or_a_missing_quote_is_refused(world):
    token = world.say("pause Codex until tonight please", session="s-1")
    world.server("s-1")
    is_error, out = _act("route.report", {"account": "Codex"}, "pause Codex until tonight", token)
    assert is_error and out["code"] == "not_by_direction"
    is_error, out = _act("route.pause", {"account": "Codex"}, None, token)
    assert is_error and out["ok"] is False
    assert _state("Codex") != "paused"


def test_a_routing_setting_outside_the_routing_settings_is_refused(world):
    token = world.say("set the cli path for codex to the new one", session="s-1")
    world.server("s-1")
    is_error, out = _act("config.routing_setting", {"key": "cli_path.codex", "value": "/bin/codex"},
                         "set the cli path for codex", token)
    assert is_error and out["ok"] is False


def test_the_tool_tells_agents_to_quote_and_never_retry(world):
    tool = next(t for t in ms.TOOLS if t["name"] == "khipu_owner_act")
    text = tool["description"].lower()
    for phrase in ("exactly", "paraphrase", "owner turn", "never retry", "page", "another conversation"):
        assert phrase in text, phrase
    assert set(tool["inputSchema"]["properties"]) == {"action", "args", "words", "turn", "owner_session"}
    assert tool["annotations"]["readOnlyHint"] is False
    assert " he " not in f" {text} " and " his " not in f" {text} " and " him " not in f" {text} "


# ---- the gateway does not have it -------------------------------------------------------------


def test_the_gateway_does_not_list_or_run_it(world):
    from khipu.request_context import gateway_request

    token = world.say("pause Codex until tonight please", session="s-1")
    world.server("s-1")
    with gateway_request("bot"):
        listed = handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {t["name"] for t in listed["result"]["tools"]}
        assert "khipu_owner_act" not in names and "khipu_route" in names
        is_error, out = _act("route.pause", {"account": "Codex"}, "pause Codex until tonight", token)
    assert is_error and _state("Codex") != "paused"
    local = handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert "khipu_owner_act" in {t["name"] for t in local["result"]["tools"]}


# ---- dispatched jobs: owner_session -----------------------------------------------------------


def _dispatch(world, parent: str, job: str = "s-job") -> None:
    """The delegation hook notes the brief; the job's first turn is it."""
    owner_turns.note_brief(BRIEF, parent=parent, harness="claude_code")
    world.say(BRIEF, session=job, entry="sdk-cli")
    (turn,) = owner_turns.turns(job)
    assert turn["delegated"] is True and turn["parent"] == parent


def test_a_job_with_its_parent_link_and_the_parents_token_passes(world):
    token = world.say("pause Codex until tonight please", session="s-parent")
    _dispatch(world, "s-parent")
    world.server("s-job")
    is_error, out = _act("route.pause", {"account": "Codex"}, "pause Codex until tonight", token,
                         owner_session="s-parent")
    assert not is_error and out["ok"] is True, out
    assert _state("Codex") == "paused"
    (line,) = _log()
    assert (line["session"], line["via_session"], line["turn"]) == ("s-parent", "s-job", token)


def test_a_job_acting_on_its_own_brief_is_refused(world):
    world.say("pause Codex until tonight please", session="s-parent")
    _dispatch(world, "s-parent")
    world.server("s-job")
    is_error, out = _act("route.pause", {"account": "Codex"}, "pause Codex until tonight")
    assert is_error and out["code"] == "no_recorded_turn"
    assert _state("Codex") != "paused"


def test_a_job_naming_a_parent_that_never_typed_the_words_is_refused(world):
    token = world.say("build the docs page for routing", session="s-parent")
    _dispatch(world, "s-parent")
    world.server("s-job")
    is_error, out = _act("route.pause", {"account": "Codex"}, "pause Codex until tonight", token,
                         owner_session="s-parent")
    assert is_error and out["code"] == "not_the_owners_words"
    assert _state("Codex") != "paused"


def test_a_job_naming_a_session_it_was_not_started_from_is_refused(world):
    world.say("hello from the real parent", session="s-parent")
    token = world.say("pause Codex until tonight please", session="s-other")
    _dispatch(world, "s-parent")
    world.server("s-job")
    is_error, out = _act("route.pause", {"account": "Codex"}, "pause Codex until tonight", token,
                         owner_session="s-other")
    assert is_error and out["code"] == "no_recorded_turn" and "not started from" in out["error"]
    assert _state("Codex") != "paused"


def test_a_session_with_no_brief_cannot_name_another(world):
    token = world.say("pause Codex until tonight please", session="s-parent")
    world.say("just a normal turn here", session="s-other")
    world.server("s-other")
    is_error, out = _act("route.pause", {"account": "Codex"}, "pause Codex until tonight", token,
                         owner_session="s-parent")
    assert is_error and out["code"] == "no_recorded_turn"
    assert _state("Codex") != "paused"


def test_a_claude_code_parent_still_needs_its_token(world):
    world.say("pause Codex until tonight please", session="s-parent")
    _dispatch(world, "s-parent")
    world.server("s-job")
    is_error, out = _act("route.pause", {"account": "Codex"}, "pause Codex until tonight", owner_session="s-parent")
    assert is_error and out["code"] == "no_recorded_turn"
    assert _state("Codex") != "paused"


def test_the_cli_takes_owner_session_too(world, capsys):
    from khipu import cli

    token = world.say("pause Codex until tonight please", session="s-parent")
    _dispatch(world, "s-parent")
    world.server("s-job")
    rc = cli.main(["route", "pause", "Codex", "--owner-words", "pause Codex until tonight", "--owner-turn", token,
                   "--owner-session", "s-parent"])
    assert rc == 0, capsys.readouterr().out
    assert _state("Codex") == "paused"
    assert _log()[0]["via_session"] == "s-job"


# ---- pending requests confirmed by direction (rows 17 to 20) ----------------------------------


def _pending_rule() -> int:
    out = routing.request_rule("Keep browser work read-only.", owner_request="the owner asked",
                               author={"harness": "grok_bot", "session": "bot-1"}, via="mcp")
    return out["n"]


def test_a_pending_rule_confirmed_by_direction(world):
    n = _pending_rule()
    token = world.say("yes, keep that rule", session="s-1")
    world.server("s-1")
    is_error, out = _act("route.rule.confirm", {"rule": n}, "yes, keep that rule", token)
    assert not is_error and out["ok"] is True, out
    (rule,) = routing.read_view()["rules"]
    assert rule["pending"] is False


def test_a_pending_rule_is_not_confirmed_by_words_about_something_else(world):
    n = _pending_rule()
    token = world.say("yes, keep the note as it is", session="s-1")
    world.server("s-1")
    is_error, out = _act("route.rule.confirm", {"rule": n}, "yes, keep the note", token)
    assert is_error and out["code"] == "words_do_not_ask_for_this"
    assert routing.read_view()["rules"][0]["pending"] is True


def test_that_rule_needs_exactly_one_pending(world):
    n = _pending_rule()
    routing.request_rule("Never run the fast tier.", owner_request="the owner asked",
                         author={"harness": "grok_bot", "session": "bot-1"}, via="mcp")
    token = world.say("yes, keep that rule", session="s-1")
    world.server("s-1")
    is_error, out = _act("route.rule.confirm", {"rule": n}, "yes, keep that rule", token)
    assert is_error and out["code"] == "words_do_not_ask_for_this"


def _pending_priority() -> str:
    out = routing.request_priority([{"account": "Codex"}, {"account": "Claude"}], owner_request="the owner asked",
                                   author={"harness": "grok_bot", "session": "bot-1"}, via="mcp",
                                   expect=routing.priority_version())
    assert out["ok"] is True
    return routing.priority_version()


def test_a_pending_priority_confirmed_by_direction(world):
    version = _pending_priority()
    token = world.say("yes confirm it", session="s-1")
    world.server("s-1")
    is_error, out = _act("route.priority.confirm", {"expect": version}, "yes confirm it", token)
    assert not is_error and out["ok"] is True, out
    assert routing.read_view()["priority"]["pending"] is False


def test_a_pending_priority_is_not_confirmed_by_other_words(world):
    version = _pending_priority()
    token = world.say("yes confirm the rule", session="s-1")
    world.server("s-1")
    is_error, out = _act("route.priority.confirm", {"expect": version}, "yes confirm the rule", token)
    assert is_error and out["code"] == "words_do_not_ask_for_this"
    assert routing.read_view()["priority"]["pending"] is True


def _pending_setting(world) -> None:
    from khipu import cli

    world.server(None)
    assert cli.main(["config", "--set", "routing_stale_after", "6h", "--at-owner-request", "the owner asked"]) == 0


def test_a_pending_routing_setting_confirmed_by_direction(world, capsys):
    _pending_setting(world)
    capsys.readouterr()
    token = world.say("yes, confirm that change", session="s-1")
    world.server("s-1")
    is_error, out = _act("config.answer_request.confirm", {"key": "routing_stale_after"}, "yes, confirm that change",
                         token)
    assert not is_error and out["ok"] is True, out
    assert routing._setting_requests(json.loads((world.tmp / "data" / "config.json").read_text())) == {}


def test_a_pending_routing_setting_is_not_confirmed_by_an_undo(world, capsys):
    _pending_setting(world)
    capsys.readouterr()
    token = world.say("undo that change please", session="s-1")
    world.server("s-1")
    is_error, out = _act("config.answer_request.confirm", {"key": "routing_stale_after"}, "undo that change please",
                         token)
    assert is_error and out["code"] == "words_do_not_ask_for_this"


def _pending_accounts(world) -> None:
    from khipu import cli, config

    world.server(None)
    new = ACCOUNTS + [{"label": "Codex work", "harness": "codex", "home": "/logins/codex-work"}]
    assert cli.main(["config", "--set", "usage_accounts", json.dumps(new), "--at-owner-request",
                     "the owner asked", "--expect", config.accounts_version()]) == 0


def test_a_pending_account_list_change_confirmed_by_direction(world, capsys):
    from khipu import config

    _pending_accounts(world)
    capsys.readouterr()
    token = world.say("yes, confirm that account list change", session="s-1")
    world.server("s-1")
    is_error, out = _act("accounts.request.confirm", {"expect": config.accounts_version()},
                         "yes, confirm that account list change", token)
    assert not is_error and out["ok"] is True, out
    assert not config.load_config().get("usage_accounts_request")


def test_a_pending_account_list_change_is_not_confirmed_by_other_words(world, capsys):
    from khipu import config

    _pending_accounts(world)
    capsys.readouterr()
    token = world.say("yes, confirm the new rule", session="s-1")
    world.server("s-1")
    is_error, out = _act("accounts.request.confirm", {"expect": config.accounts_version()},
                         "yes, confirm the new rule", token)
    assert is_error and out["code"] == "words_do_not_ask_for_this"
    assert config.load_config().get("usage_accounts_request")
