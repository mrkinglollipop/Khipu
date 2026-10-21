"""Owner direction, slice 3 of docs/plans/2026-10-16-owner-direction.md: the
``khipu_owner_act`` MCP tool, a dispatched job refused, and pending requests
confirmed by direction.

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

    def say(self, text: str = "please go ahead with that", *, session: str, harness: str = "claude_code",
            entry: str = "cli", ago: float = 0.0) -> str | None:
        """A turn the prompt hook records for ``session``; its turn id
        (None when nothing was recorded)."""
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
        return owner_turns.record(payload, env, now=time.time() - ago)

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


def _act(action: str, args: dict | None = None, **extra) -> tuple[bool, dict]:
    arguments = {"action": action, "args": args or {}, **extra}
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
    world.say("pause Codex until tonight please", session="s-1", harness=harness, entry=entry)
    world.server("s-1", harness)
    is_error, out = _act("route.pause", {"account": "Codex", "until": "6h"})
    assert not is_error and out["ok"] is True, out
    assert _state("Codex") == "paused"
    (line,) = _log()
    assert (line["action"], line["auth"], line["session"], line["harness"]) == ("route.pause", "direction", "s-1",
                                                                              harness)
    assert line["binding"] == ("session" if harness == "aegis" else "hook")


def test_no_session_in_the_servers_environment_is_refused(world):
    world.say(session="s-1")
    world.server(None)
    is_error, out = _act("route.pause", {"account": "Codex", "until": "6h"})
    assert is_error and out["ok"] is False and out["code"] == "no_recorded_turn"
    assert _state("Codex") != "paused"


def test_a_forged_session_id_is_refused(world):
    world.say(session="s-a")
    world.server("s-b")
    is_error, out = _act("route.pause", {"account": "Codex", "until": "6h"})
    assert is_error and out["code"] == "no_recorded_turn"
    assert _state("Codex") != "paused"


def test_the_session_is_never_taken_from_the_arguments(world):
    world.say(session="s-a")
    world.server("s-b")
    for key in ("session", "session_id"):
        is_error, out = _act("route.pause", {"account": "Codex", "until": "6h"}, **{key: "s-a"})
        assert is_error and out["code"] == "bad_args" and _state("Codex") != "paused"


@pytest.mark.parametrize("extra", [{"words": "pause Codex"}, {"turn": "abcd1234"}, {"owner_session": "s-1"}])
def test_the_old_word_and_token_keys_are_refused(world, extra):
    world.say(session="s-1")
    world.server("s-1")
    is_error, out = _act("route.pause", {"account": "Codex", "until": "6h"}, **extra)
    assert is_error and out["code"] == "bad_args"
    assert _state("Codex") != "paused" and _log() == []


@pytest.mark.parametrize("args", [{"account": "Codex", "from_app": True}, {"account": "Codex", "owner_words": "x"},
                                  {"account": "--from-app"}, {"account": 3}, {}])
def test_arguments_are_checked_strictly(world, args):
    world.say(session="s-1")
    world.server("s-1")
    is_error, out = _act("route.pause", args)
    assert is_error and out["ok"] is False
    assert _state("Codex") != "paused" and _log() == []


def test_an_unknown_action_is_refused(world):
    world.say(session="s-1")
    world.server("s-1")
    is_error, out = _act("route.report", {"account": "Codex"})
    assert is_error and out["code"] == "not_by_direction"
    assert _state("Codex") != "paused"


def test_a_stale_turn_is_refused(world):
    world.say(session="s-1", ago=7 * 3600)
    world.server("s-1")
    is_error, out = _act("route.pause", {"account": "Codex", "until": "6h"})
    assert is_error and out["code"] == "stale"
    assert _state("Codex") != "paused"


def test_a_routing_setting_outside_the_routing_settings_is_refused(world):
    world.say(session="s-1")
    world.server("s-1")
    is_error, out = _act("config.routing_setting", {"key": "cli_path.codex", "value": "/bin/codex"})
    assert is_error and out["ok"] is False


def test_the_tool_takes_only_an_action_and_its_arguments(world):
    tool = next(t for t in ms.TOOLS if t["name"] == "khipu_owner_act")
    text = tool["description"].lower()
    for phrase in ("page", "another conversation", "dispatched"):
        assert phrase in text, phrase
    for gone in ("quote", "paraphrase", "owner turn", "words"):
        assert gone not in text, gone
    assert set(tool["inputSchema"]["properties"]) == {"action", "args"}
    assert tool["annotations"]["readOnlyHint"] is False
    assert " he " not in f" {text} " and " his " not in f" {text} " and " him " not in f" {text} "


# ---- the gateway does not have it -------------------------------------------------------------


def test_the_gateway_does_not_list_or_run_it(world):
    from khipu.request_context import gateway_request

    world.say(session="s-1")
    world.server("s-1")
    with gateway_request("bot"):
        listed = handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {t["name"] for t in listed["result"]["tools"]}
        assert "khipu_owner_act" not in names and "khipu_route" in names
        is_error, out = _act("route.pause", {"account": "Codex", "until": "6h"})
    assert is_error and _state("Codex") != "paused"
    local = handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert "khipu_owner_act" in {t["name"] for t in local["result"]["tools"]}


# ---- dispatched jobs are refused -------------------------------------------------------------


def test_a_job_acting_on_its_delegated_brief_is_refused(world):
    """The delegation hook notes the brief; the job's first turn is it."""
    world.say(session="s-parent")
    owner_turns.note_brief(BRIEF, parent="s-parent", harness="claude_code")
    world.say(BRIEF, session="s-job", entry="sdk-cli")
    (turn,) = owner_turns.turns("s-job")
    assert turn["delegated"] is True and turn["parent"] == "s-parent"
    world.server("s-job")
    is_error, out = _act("route.pause", {"account": "Codex", "until": "6h"})
    assert is_error and out["code"] == "no_recorded_turn"
    assert _state("Codex") != "paused" and _log() == []


def test_a_session_started_by_an_sdk_cli_turn_is_refused_even_with_a_typed_parent(world):
    world.say("pause Codex until tonight please", session="s-parent")
    world.say("pause Codex until tonight please", session="s-job", entry="sdk-cli")
    world.server("s-job")
    is_error, out = _act("route.pause", {"account": "Codex", "until": "6h"})
    assert is_error and out["code"] == "no_recorded_turn"
    assert _state("Codex") != "paused"


# ---- pending requests confirmed by direction (rows 17 to 20) ----------------------------------


def _pending_rule() -> int:
    out = routing.request_rule("Keep browser work read-only.", owner_request="the owner asked",
                               author={"harness": "grok_bot", "session": "bot-1"}, via="mcp")
    return out["n"]


def test_a_pending_rule_confirmed_by_direction(world):
    n = _pending_rule()
    world.say("yes, keep that rule", session="s-1")
    world.server("s-1")
    is_error, out = _act("route.rule.confirm", {"rule": n})
    assert not is_error and out["ok"] is True, out
    (rule,) = routing.read_view()["rules"]
    assert rule["pending"] is False


def test_a_pending_rule_is_not_confirmed_with_no_owner_turn(world):
    n = _pending_rule()
    world.server("s-1")
    is_error, out = _act("route.rule.confirm", {"rule": n})
    assert is_error and out["code"] == "no_recorded_turn"
    assert routing.read_view()["rules"][0]["pending"] is True


def _pending_priority(world) -> str:
    world.server("proposal-author")
    shown = routing.conversation_view()
    out = routing.request_priority([{"account": "Codex"}, {"account": "Claude"}], owner_request="the owner asked",
                                   author={"harness": "grok_bot", "session": "bot-1"}, via="mcp",
                                   expect=routing.priority_version(), base=shown["priority_owner_revision"])
    assert out["ok"] is True
    return routing.priority_version()


def test_a_pending_priority_confirmed_by_direction(world):
    version = _pending_priority(world)
    world.say("yes confirm it", session="s-1")
    world.server("s-1")
    is_error, out = _act("route.priority.confirm", {"expect": version})
    assert not is_error and out["ok"] is True, out
    assert routing.read_view()["priority"]["pending"] is False


def test_owner_act_accepts_hold_choices_for_priority_and_rule_add(world):
    hold = routing.add_hold("wait for Grok", scope="account", account="Grok", reason="keep", owner=True, via="app")
    choices = {hold["id"]: "keep"}
    world.say("set Grok's priority", session="s-priority")
    world.server("s-priority")
    is_error, out = _act("route.priority.set", {"entries": [{"account": "Grok", "role": "build"}],
                                                  "expect": routing.priority_version(), "holds": choices})
    assert not is_error and out["ok"] is True, out
    world.say("add Grok's rule", session="s-rule")
    world.server("s-rule")
    is_error, out = _act("route.rule.add", {"text": "Grok builds", "holds": choices})
    assert not is_error and out["ok"] is True, out
    actions = _log()
    assert [line["facts"]["holds"] for line in actions[-2:]] == [choices, choices]


def _pending_setting(world) -> None:
    from khipu import cli

    world.server(None)
    assert cli.main(["config", "--set", "routing_stale_after", "6h", "--at-owner-request", "the owner asked"]) == 0


def test_a_pending_routing_setting_confirmed_by_direction(world, capsys):
    _pending_setting(world)
    capsys.readouterr()
    world.say("yes, confirm that change", session="s-1")
    world.server("s-1")
    is_error, out = _act("config.answer_request.confirm", {"key": "routing_stale_after"})
    assert not is_error and out["ok"] is True, out
    assert routing._setting_requests(json.loads((world.tmp / "data" / "config.json").read_text())) == {}


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
    world.say("yes, confirm that account list change", session="s-1")
    world.server("s-1")
    is_error, out = _act("accounts.request.confirm", {"expect": config.accounts_version()})
    assert not is_error and out["ok"] is True, out
    assert not config.load_config().get("usage_accounts_request")
