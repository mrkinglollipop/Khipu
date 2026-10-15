"""Every agent-facing output passes through ``khipu.agent_view`` (settings-
clarity plan, slice 9A): no login folder, no ~/ form of one, no folder
name, no home folder and no user name reaches a conversation, whether it came
from config, from a CLI's own reply, from a cache, from a conversation's
words or from an exception. The owner's note and his standing rules are the
one deliberate exception: they reach agents as he wrote them.

Hermetic: HOME and the data dir are under ``tmp_path``; the vendor wire,
the hub and the gateway's network are fakes; nothing writes outside tmp.
"""
from __future__ import annotations

import io
import json
import time

import pytest

from khipu import agent_view, cli, db, gateway, routing, usage
from khipu import mcp_server as ms

NAME = "probe-login"  # the login folder's name without its dot
USER = "probeuser"
FIXED = "PermissionError: Khipu could not read or write one of its files"


@pytest.fixture
def owner(tmp_path, monkeypatch):
    home = tmp_path / "users" / USER
    login = home / f".{NAME}"
    login.mkdir(parents=True)
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": [
        {"label": "Work", "harness": "claude", "home": str(login)}]}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.setenv("KHIPU_TEST", "1")
    monkeypatch.delenv(ms.GATEWAY_ACTIVE_ENV, raising=False)
    monkeypatch.setattr(ms, "_GATEWAY_ACTIVE", False)
    monkeypatch.setattr(ms, "_RELOAD_ON_CHANGE", False)
    monkeypatch.setattr(usage, "_command", lambda h: None)
    monkeypatch.setattr(ms, "_attach_prior_work", lambda *a: None)
    monkeypatch.setattr(ms, "_attach_contract", lambda *a: None)
    monkeypatch.setattr(agent_view, "_OWNER_WORDS", set())
    return {"account": usage.configured_accounts()[0], "login": str(login), "home": str(home)}


def _strings(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)
    elif value is not None:
        yield str(value)


def _leaks(value, owner, *, allow: str | None = None) -> list[str]:
    bad = (owner["login"], f"~/.{NAME}", "~/", NAME, NAME.replace("-", "_"), USER, owner["home"])
    found = []
    for s in _strings(value):
        if allow:
            s = s.replace(allow, "")
        if any(b.lower() in s.lower() for b in bad):
            found.append(s)
    return found


def call(name, **args):
    response = ms.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": name, "arguments": args}})
    result = response["result"]
    return result["isError"], json.loads(result["content"][0]["text"])


def row(account, **updates):
    stamp = usage._iso(time.time())
    value = dict(usage._blank_row(account), ok=True, probed_at=stamp, last_attempt_at=stamp, last_good_at=stamp,
                 source="live", windows=[{"name": "weekly", "used_pct": 95,
                                          "resets_at": usage._iso(time.time() + 86400), "window_mins": 10080}])
    value.update(updates)
    return value


def cache(account, **updates):
    value = row(account, **updates)
    usage._save_cache({usage._key(account): value})
    return value


def frames():
    """A vendor reply whose own fields carry the folder, its ~/ form and the user name."""
    return [
        {"type": "control_response", "response": {"request_id": "init", "response":
            {"claude_code_version": USER}}},
        {"type": "control_response", "response": {"request_id": "usage", "response":
            {"subscription_type": f"~/.{NAME}", "rate_limits": {"model_scoped": [
                # The CLI keeps 64 characters of a name: the folder's tail and the user.
                {"display_name": f"{USER}/.{NAME}", "utilization": 95, "resets_at": usage._iso(time.time() + 86400)}]}}}}]


def post_without_network(message):
    h = gateway.Handler.__new__(gateway.Handler)
    raw = json.dumps(message).encode()
    h.path = "/mcp"
    h.client_address = ("127.0.0.1", 12345)
    h.headers = {"Content-Length": str(len(raw)), "Authorization": "Bearer synthetic-review-token"}
    h.connection = None
    h.rfile = io.BytesIO(raw)
    h.token = "synthetic-review-token"
    h.tokens = {}
    h.rate = gateway._Rate(1000)
    h.token_rate = gateway._Rate(1000)
    h.auth_fail_rate = gateway._Rate(1000)
    sent = []
    h._send = lambda status, body=None, **kw: sent.append((status, json.loads(body) if body else None))
    h.do_POST()
    return sent[-1]


def test_adapter_metadata_never_reaches_agents(owner, monkeypatch):
    monkeypatch.setattr(usage, "_command", lambda h: "/tmp/fake-cli")
    monkeypatch.setattr(usage, "_converse", lambda *a, **kw: frames())
    raw = usage.report(fresh=True)
    assert _leaks(raw, owner), "the owner's report carries them, so a leak would show"
    err, out = call("khipu_usage")
    assert not err and _leaks(out, owner) == []
    assert out["accounts"][0]["windows"][-1]["used_pct"] == 95
    payload: dict = {}
    ms._attach_plan_warnings(payload)
    assert payload["warnings"] and _leaks(payload, owner) == []
    err, routed = call("khipu_route")
    assert not err and _leaks(routed, owner) == []
    assert _leaks(routing.summary(), owner) == []
    assert _leaks(routing.light_status(start=True), owner) == []
    assert _leaks(routing.prompt_push(None, budget=5), owner) == []


def test_a_cached_note_naming_the_user_is_not_passed_on(owner):
    cache(owner["account"], note=f"Previous attempt failed for {USER}")
    err, out = call("khipu_usage")
    assert not err and _leaks(out, owner) == []


def test_routing_report_evidence_names_the_account_by_its_id(owner):
    routing.conversation_write({"state": "limited", "account": "Work", "until": "1h",
                                "evidence": f"Login failed at {owner['login']}"}, author={"harness": "claude-code"})
    err, out = call("khipu_route")
    assert not err and _leaks(out, owner) == []
    ident = routing.account_id(usage._key(owner["account"]))
    assert f"Login failed at {ident}" in json.dumps(out)


def test_the_owners_note_reaches_agents_as_written(owner, capsys):
    # Deliberate exception: the owner's own words to agents pass whole; a
    # conversation's evidence beside them is still scrubbed.
    text = f"Use login {owner['login']} or ~/.{NAME} for {USER}"
    routing.set_note(text, via="app")
    routing.conversation_write({"state": "limited", "account": "Work", "until": "1h",
                                "evidence": f"Login failed at {owner['login']} for {USER}"},
                               author={"harness": "claude-code"})
    err, out = call("khipu_route")
    assert not err and text in json.dumps(out)
    assert _leaks(out, owner, allow=text) == []
    assert cli.main(["route", "--summary"]) == 0
    stdout = capsys.readouterr().out
    assert text in stdout and _leaks(stdout, owner, allow=text) == []
    start = routing.light_status(start=True)
    prompt = routing.prompt_push(None, budget=5)
    assert text in start["text"] and text in prompt["text"]
    assert _leaks(start, owner, allow=text) == [] and _leaks(prompt, owner, allow=text) == []
    light = ms._tool_status_light({"prompt": "review local routing"})
    assert text in light["routing"]["text"]
    err, status = call("khipu_status", prompt="review local routing")
    assert not err and text in status["routing"]["text"]
    assert _leaks(status, owner, allow=text) == []


def test_a_filesystem_error_in_a_tool_is_said_in_fixed_words(owner, monkeypatch):
    def fail(**kw):
        raise PermissionError(13, "Permission denied", owner["login"])

    monkeypatch.setattr(usage, "report", fail)
    err, out = call("khipu_usage")
    assert err and out["error"] == FIXED


def test_a_value_error_naming_a_folder_is_scrubbed(owner, monkeypatch):
    def fail(**kw):
        raise ValueError(f"bad folder {owner['login']} for {USER}")

    monkeypatch.setattr(usage, "report", fail)
    err, out = call("khipu_usage")
    assert err and out["error"].startswith("bad folder") and _leaks(out, owner) == []


def test_gateway_status_hub_error_is_said_in_fixed_words(owner, monkeypatch):
    monkeypatch.setattr(ms, "_GATEWAY_ACTIVE", True)

    def fail():
        raise PermissionError(13, "Permission denied", owner["login"])

    monkeypatch.setattr(db, "connect", fail)
    status, out = post_without_network({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                        "params": {"name": "khipu_status", "arguments": {"prompt": "review"}}})
    assert status == 200
    body = json.loads(out["result"]["content"][0]["text"])
    assert body["hub_error"] == FIXED and _leaks(out, owner) == []


def test_gateway_dispatch_error_is_said_in_fixed_words(owner, monkeypatch):
    def boom(message):
        raise PermissionError(13, "Permission denied", owner["login"])

    monkeypatch.setattr(gateway, "handle_message", boom)
    status, out = post_without_network({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                        "params": {"name": "khipu_status", "arguments": {}}})
    assert out["error"]["message"] == FIXED


def test_gateway_declines_usage_and_route_without_reading_local_reports(owner, monkeypatch):
    monkeypatch.setattr(ms, "_GATEWAY_ACTIVE", True)

    def forbidden(*a, **kw):
        raise AssertionError("local report must not be touched by gateway")

    monkeypatch.setattr(usage, "report", forbidden)
    monkeypatch.setattr(routing, "conversation_view", forbidden)
    for name in ("khipu_usage", "khipu_route"):
        status, out = post_without_network({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                            "params": {"name": name, "arguments": {}}})
        assert status == 200
        body = json.loads(out["result"]["content"][0]["text"])
        assert body["available"] is False and _leaks(out, owner) == []


def test_the_apps_cli_json_is_unchanged(owner, capsys):
    cache(owner["account"])
    expected = usage.report()
    assert cli.main(["usage", "--json"]) == 0
    actual = json.loads(capsys.readouterr().out)
    actual.pop("generated_at")
    expected.pop("generated_at")
    assert actual == json.loads(json.dumps(expected))
    assert actual["accounts"][0]["home"] == owner["login"]


def test_an_invalid_entry_whose_cli_is_a_path_shows_no_path(owner):
    from khipu.config import config_file

    config_file().write_text(json.dumps({"usage_accounts": [
        {"harness": owner["login"], "home": "relative-folder"}]}))
    err, out = call("khipu_usage")
    assert not err and _leaks(out, owner) == []
    entry = out["accounts"][0]
    assert entry["error"] == usage.E_INVALID and entry["note"] == usage.INVALID_PUBLIC_NOTE
    assert entry["harness"] == "?"


def test_cached_error_fields_and_notes_are_khipus_own(owner):
    from khipu.config import config_file

    config_file().write_text(json.dumps({"usage_accounts": [{"label": "Default", "harness": "claude"}]}))
    default = usage.configured_accounts()[0]
    cache(default, ok=False, error=owner["login"], live_error=f"~/.{NAME}", last_error=USER,
          note=f"Cannot read {owner['login']}")
    err, out = call("khipu_usage")
    assert not err and _leaks(out, owner) == []
    entry = out["accounts"][0]
    assert entry["error"] == entry["live_error"] == entry["last_error"] == usage.E_INTERNAL


def test_every_agent_surface_is_clean(owner, monkeypatch, tmp_path, capsys):
    from khipu import drift
    from khipu.config import config_file

    second = tmp_path / "users" / USER / ".codex-zwork"
    second.mkdir()
    config_file().write_text(json.dumps({"usage_accounts": [
        {"label": "Work", "harness": "claude", "home": owner["login"]},
        {"label": "Second", "harness": "codex", "home": str(second)}]}))
    # Fake provider metadata for Claude; Codex reads the same reply and fails it.
    monkeypatch.setattr(usage, "_command", lambda h: "/tmp/fake-cli")
    monkeypatch.setattr(usage, "_converse", lambda *a, **kw: frames())
    monkeypatch.setattr(usage, "codex_rollout", lambda *a, **kw: None)
    raw = usage.report(fresh=True)
    assert any(not r.get("ok") for r in raw["accounts"])
    outputs: dict = {}
    outputs["khipu_usage"] = call("khipu_usage")
    outputs["khipu_route_report"] = call("khipu_route_report", state="limited", account="Work", until="1h",
                                         evidence=f"Login failed at {owner['login']} (~/.{NAME}) for {USER}")
    outputs["khipu_route"] = call("khipu_route")
    outputs["khipu_route since"] = call("khipu_route", since="0")
    outputs["summary"] = routing.summary()
    outputs["start push"] = routing.light_status(start=True)
    outputs["prompt push"] = routing.prompt_push(None, budget=5)
    assert cli.main(["route", "--summary"]) == 0
    outputs["route --summary"] = capsys.readouterr().out

    def fs_fail(*a, **kw):
        raise PermissionError(13, "Permission denied", owner["login"])

    monkeypatch.setattr(db, "connect", fs_fail)
    monkeypatch.setattr(drift, "status_payload", lambda *a, **kw: {"db_path": owner["login"],
                                                                     "latest_ingested_at": None})
    outputs["khipu_status light"] = call("khipu_status", prompt="review")
    outputs["khipu_status full"] = call("khipu_status", full=True, prompt="review")
    outputs["khipu_status plain"] = call("khipu_status")
    monkeypatch.setattr(usage, "report", fs_fail)
    outputs["tool error"] = call("khipu_usage")
    monkeypatch.setattr(ms, "_GATEWAY_ACTIVE", True)
    outputs["gateway khipu_status"] = post_without_network(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "khipu_status",
                                                                        "arguments": {"prompt": "review"}}})
    assert outputs["tool error"][0] is True
    for name, out in outputs.items():
        assert _leaks(out, owner) == [], name


def test_known_roots_with_spaces_are_scrubbed_whole(tmp_path, monkeypatch):
    # A path the scrub doesn't know, under a known root whose name holds a
    # space (the home folder, the memory root, a configured folder), is
    # taken out whole: a generic path ends at the first space and would
    # leave "Storage/..." behind.
    home = tmp_path / "Home Dir" / "someone"
    memory = tmp_path / "Mem Root"
    runner = tmp_path / "Run Ners"
    for d in (home, memory, runner):
        d.mkdir(parents=True)
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({
        "memory_root": str(memory),
        "routing_sources": [{"name": "lanes", "kind": "lanes", "path": str(runner)}]}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    texts = [f"read {home}/Library/Application Support/khipu/x.json now",
             f"memory at {memory}/topics/plan.md, then {memory}/Project Notes/a.md",
             f"runner {runner}/state/Lane One/s.json failed"]
    out = agent_view.scrub(texts)
    for text in out:
        for word in ("Home Dir", "Mem Root", "Run Ners", "Application Support", "Project Notes", "Lane One",
                     "someone", str(tmp_path)):
            assert word not in text, (word, text)
    assert out[0] == "read a folder now"


def test_the_table_is_built_once_per_record_version(owner, monkeypatch):
    builds = []
    real = agent_view._rules
    monkeypatch.setattr(agent_view, "_rules", lambda *a, **k: builds.append(1) or real(*a, **k))
    monkeypatch.setattr(agent_view, "_TABLE", {"sig": None, "at": 0.0, "table": None})
    for _ in range(5):
        agent_view.scrub(["plain words", "more plain words"])
    assert builds == [1]
    agent_view.scrub("x", extra_keys=["claude|/elsewhere/.claude-z"])
    assert builds == [1, 1], "another set of accounts is another table"
