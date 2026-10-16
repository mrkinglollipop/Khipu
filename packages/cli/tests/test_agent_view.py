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
import random
import re
import time
import unicodedata

from pathlib import Path

import pytest

from khipu import agent_view, cli, db, gateway, routing, usage
from khipu import mcp_server as ms

NAME = "probe-login"  # the login folder's name without its dot
USER = "ProbeUser"
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
    monkeypatch.setattr(agent_view, "_TABLE", {"sig": None, "at": 0.0, "table": None})
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


@pytest.mark.parametrize("chain", [False, True])
@pytest.mark.parametrize("payload", [
    ".ResolvedLogin9", "ResolvedLogin9", ".RESOLVEDLOGIN9", "RESOLVEDLOGIN9",
    "resolvedlogin9", ".LoginPointer9", "LoginPointer9", ".LOGINPOINTER9",
])
def test_symlink_login_metadata_never_reaches_agents(owner, monkeypatch, chain, payload):
    from khipu.config import config_file

    home = Path(owner["home"])
    resolved = home / ".ResolvedLogin9"
    resolved.mkdir()
    pointer = home / ".LoginPointer9"
    if chain:
        middle = home / ".MiddleLogin9"
        middle.symlink_to(resolved.name, target_is_directory=True)
        pointer.symlink_to(middle.name, target_is_directory=True)
    else:
        pointer.symlink_to(resolved, target_is_directory=True)
    config_file().write_text(json.dumps({"usage_accounts": [
        {"label": "Work", "harness": "claude", "home": "~/.LoginPointer9"}]}))
    monkeypatch.setattr(usage, "_command", lambda h: "/tmp/fake-cli")
    metadata = frames()
    metadata[0]["response"]["response"]["claude_code_version"] = payload
    metadata[1]["response"]["response"]["subscription_type"] = payload
    monkeypatch.setattr(usage, "_converse", lambda *a, **kw: metadata)
    raw = usage.report(fresh=True)["accounts"][0]
    assert raw["plan"] == raw["version"] == payload
    err, out = call("khipu_usage")
    assert not err
    assert _survivors(out, [payload]) == []
    if chain:
        for alias in _forms_of(".MiddleLogin9"):
            assert _survivors(agent_view.scrub(f"Provider reports {alias} today"), [alias]) == []


def test_symlink_aliases_follow_parent_links_and_changed_hops(owner):
    from khipu.config import config_file

    home = Path(owner["home"])
    storage = home / "storage"
    storage.mkdir()
    resolved = storage / ".ResolvedLogin9"
    resolved.mkdir()
    middle = storage / ".MiddleLogin9"
    middle.symlink_to(resolved.name, target_is_directory=True)
    pointer = storage / ".LoginPointer9"
    pointer.symlink_to(middle.name, target_is_directory=True)
    parent = home / "ParentPointer9"
    parent.symlink_to(storage, target_is_directory=True)
    configured = "~/ParentPointer9/.LoginPointer9"
    config_file().write_text(json.dumps({"usage_accounts": [
        {"label": "Work", "harness": "claude", "home": configured}]}))
    for spelling in (configured, str(parent / pointer.name), str(pointer), str(middle), str(resolved)):
        assert spelling not in agent_view.scrub(spelling)
        for alias in _forms_of(Path(spelling).name):
            assert _survivors(agent_view.scrub(f"Provider reports {alias} today"), [alias]) == []
    before = agent_view._TABLE["table"]
    changed = storage / ".ChangedHop9"
    changed.symlink_to(resolved.name, target_is_directory=True)
    pointer.unlink()
    pointer.symlink_to(changed.name, target_is_directory=True)
    for alias in _forms_of(changed.name):
        assert _survivors(agent_view.scrub(f"Provider reports {alias} today"), [alias]) == []
    assert agent_view._TABLE["table"] is not before


def _encode_identity(value, encoding):
    if encoding == "unicode-one":
        return value.replace("o", r"\u006f", 1)
    if encoding == "percent-one":
        return value.replace("o", "%6F", 1)
    if encoding == "unicode-all":
        return "".join(r"\u%04x" % ord(c) for c in value)
    if encoding == "hex-all":
        return "".join(r"\x%02x" % ord(c) for c in value)
    if encoding == "decimal-entities":
        return "".join(f"&#{ord(c)};" for c in value)
    if encoding == "hex-entities":
        return "".join(f"&#x{ord(c):x};" for c in value)
    encoded = "".join(f"%{ord(c):02X}" for c in value)
    if encoding == "percent-five":
        for _ in range(4):
            encoded = encoded.replace("%", "%25")
    return encoded


@pytest.mark.parametrize("kind", ["folder", "user", "full", "tilde"])
@pytest.mark.parametrize("encoding", [
    "unicode-one", "percent-one", "unicode-all", "hex-all", "percent-all",
    "percent-five", "decimal-entities", "hex-entities",
])
def test_encoded_identities_never_reach_mcp(owner, monkeypatch, kind, encoding):
    from khipu.config import config_file

    home = Path(owner["home"])
    resolved = home / ".ResolvedLogin9"
    resolved.mkdir()
    pointer = home / ".LoginPointer9"
    pointer.symlink_to(resolved.name, target_is_directory=True)
    config_file().write_text(json.dumps({"usage_accounts": [
        {"label": "Work", "harness": "claude", "home": "~/.LoginPointer9"}]}))
    original = {"folder": resolved.name, "user": USER, "full": str(resolved),
                "tilde": "~/" + resolved.name}[kind]
    payload = _encode_identity(original, encoding)
    if kind in ("folder", "user") and len(payload) <= 64:
        monkeypatch.setattr(usage, "_command", lambda h: "/tmp/fake-cli")
        metadata = frames()
        metadata[0]["response"]["response"]["claude_code_version"] = payload
        metadata[1]["response"]["response"]["subscription_type"] = payload
        monkeypatch.setattr(usage, "_converse", lambda *a, **kw: metadata)
        raw = usage.report(fresh=True)["accounts"][0]
        assert raw["plan"] == raw["version"] == payload
        err, out = call("khipu_usage")
        assert not err
        assert out["accounts"][0]["plan"] == agent_view.WITHHELD
        assert out["accounts"][0]["version"] == agent_view.WITHHELD
    else:
        def fail(**kw):
            raise ValueError("Provider failed at " + payload)

        monkeypatch.setattr(usage, "report", fail)
        err, out = call("khipu_usage")
        expected = agent_view.WITHHELD
        if encoding in ("unicode-one", "percent-one") and kind in ("full", "tilde"):
            # Literal path syntax is removed before the final containment check.
            replacement = agent_view.FOLDER_WORD if kind == "full" else agent_view.HOME_WORD
            expected = "Provider failed at " + replacement
        assert err and out["error"] == expected


@pytest.mark.parametrize("login,payload", [
    (".Resolved+Login9", ".Res%6Flved+Login9"),
    (".Resolved Login9", ".Res%6Flved+Login9"),
    (".ResolvedLogin9", _encode_identity(r".Res\u006flvedLogin9", "percent-five")),
    (".ResolvedLogin9", r".Res\x26#111;lvedLogin9"),
    (".Maße-Café", r".MASSE-CAFE\u0301"),
])
def test_decoded_variants_cover_plus_mixed_and_normalized_names(tmp_path, monkeypatch, login, payload):
    _setup(tmp_path, monkeypatch, USER, [login])
    assert agent_view.scrub(payload) == agent_view.WITHHELD


def test_decoding_preserves_public_text_owner_words_and_fast_path(owner, monkeypatch):
    note = r"Owner permits .probe\u002dlogin"
    routing.set_note(note, via="app")
    assert agent_view.scrub(note) == note
    assert agent_view.scrub(note + r" and .probe\u002dlogin") == agent_view.WITHHELD + " " + note
    for text in ("100% public", r"literal \uZZZZ and \xQQ", "public &#35; text", "a+b%20c"):
        assert agent_view.scrub(text) == text

    def unexpected_decode(value):
        pytest.fail("ordinary output must skip decoding")

    monkeypatch.setattr(agent_view, "unquote", unexpected_decode)
    monkeypatch.setattr(agent_view, "unquote_plus", unexpected_decode)
    assert agent_view.scrub("ordinary public text + words") == "ordinary public text + words"


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
                                "evidence": f"Login failed at {owner['login']} for /{USER}"},
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


def test_a_value_error_naming_a_folder_and_user_path_is_scrubbed(owner, monkeypatch):
    def fail(**kw):
        raise ValueError(f"bad folder {owner['login']} for /{USER}")

    monkeypatch.setattr(usage, "report", fail)
    err, out = call("khipu_usage")
    ident = routing.account_id(usage._key(owner["account"]))
    assert err and out["error"] == f"bad folder {ident} for /the user"
    assert _leaks(out, owner) == []


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
                                         evidence=f"Login failed at {owner['login']} (~/.{NAME}) for /{USER}")
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
    assert out[0] == "read the home folder now"


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


@pytest.mark.parametrize("login,payload", [
    ("/srv/FreshAccount/.qz-fresh-login", "/srv/FreshAccount/.qz-fresh-login"),
    ("/srv/FreshAccount/.qz-fresh-login", ".qz-fresh-login"),
    ("/home/freshperson/.qz-fresh-login", "freshperson"),
])
def test_t3_login_added_after_warming_table_never_reaches_usage(owner, monkeypatch, login, payload):
    from khipu import t3

    raw = {"accounts": [row(owner["account"], version=payload)]}
    monkeypatch.setattr(usage, "report", lambda **kw: raw)
    assert agent_view.scrub("safe") == "safe"
    before = agent_view._TABLE["table"]
    settings = t3.settings_path()
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"providerInstances": {"fresh": {
        "driver": "claudeAgent", "config": {
            "homePath": login}}}}))
    err, out = call("khipu_usage")
    assert not err
    assert payload.casefold() not in json.dumps(out).casefold()
    assert agent_view._TABLE["table"] is not before


@pytest.mark.parametrize("source", [
    "config", "record", "record_version", "discovery", "environment", "configured_paths",
    "memory_root", "volumes", "username", "resolved_path", "t3_stamp",
])
def test_each_table_source_invalidates_warm_cache(owner, monkeypatch, tmp_path, source):
    from types import SimpleNamespace

    from khipu import claude_homes, t3
    from khipu.config import config_file

    fresh = "/srv/FreshAccount/.qz-new-login"
    view = {"version": 1, "accounts": []}
    monkeypatch.setattr(agent_view, "_saved_view", lambda: view)
    discovered = []
    if source == "discovery":
        monkeypatch.setattr(claude_homes, "discover", lambda: discovered)
    if source == "resolved_path":
        old = tmp_path / "old-target"
        new = tmp_path / "new-target"
        old.mkdir()
        new.mkdir()
        link = tmp_path / "login-link"
        link.symlink_to(old, target_is_directory=True)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(link))
        fresh = str(new)
    agent_view.scrub("neutral")
    before = agent_view._TABLE["table"]
    if source == "config":
        config_file().write_text(json.dumps({"usage_accounts": [{"harness": "claude", "home": fresh}]}))
    elif source == "record":
        view["accounts"].append({"key": f"claude|{fresh}"})
    elif source == "record_version":
        view["version"] += 1
    elif source == "discovery":
        discovered.append(SimpleNamespace(path=Path(fresh), is_default=False))
    elif source == "environment":
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", fresh)
    elif source == "configured_paths":
        monkeypatch.setattr(routing, "_configured_paths", lambda: [fresh])
    elif source == "memory_root":
        monkeypatch.setenv("KHIPU_MEMORY_ROOT", fresh)
    elif source == "volumes":
        listdir = agent_view.os.listdir
        monkeypatch.setattr(agent_view.os, "listdir",
                            lambda path: ["FreshVolume"] if str(path) == "/Volumes" else listdir(path))
        fresh = "/Volumes/FreshVolume"
    elif source == "username":
        monkeypatch.setattr(agent_view.getpass, "getuser", lambda: "freshperson")
        fresh = "freshperson"
    elif source == "resolved_path":
        link.unlink()
        link.symlink_to(new, target_is_directory=True)
    elif source == "t3_stamp":
        settings = t3.settings_path()
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text("{}")
    agent_view.scrub("neutral")
    assert agent_view._TABLE["table"] is not before, source
    if source not in ("record_version", "t3_stamp"):
        assert fresh not in agent_view.scrub(fresh), source


@pytest.mark.parametrize("login,user,prose", [
    (".home", "probeuser", "open a folder after login"),
    (".folder", "probeuser", "go home after login"),
    (".zq-private", "home", "go home after the login and open a folder"),
])
def test_plain_words_survive_but_private_path_components_do_not(tmp_path, monkeypatch, login, user, prose):
    _, accounts = _setup(tmp_path, monkeypatch, user, [login])
    monkeypatch.setattr(agent_view.getpass, "getuser", lambda: user)
    monkeypatch.setattr(usage, "report", lambda **kw: {"accounts": [row(accounts[0], plan=prose)]})
    err, out = call("khipu_usage")
    assert not err and out["accounts"][0]["plan"] == prose
    for path in ("/Users/home/x", "~/.home", login, f"/{user}/x", f"{user}/x"):
        assert path.casefold() not in agent_view.scrub(path).casefold(), path


@pytest.mark.parametrize("source", ["folder", "user"])
@pytest.mark.parametrize("name", [
    ".home", ".folder", ".jqx", ".z", "Work", "work9", "two words", "café", "Å", "q-z", "q_x", "123",
])
def test_distinctive_names_and_their_aliases_are_removed_in_prose(tmp_path, monkeypatch, source, name):
    user = name if source == "user" else "probeuser"
    _setup(tmp_path, monkeypatch, user, [name] if source == "folder" else [".qz-private"])
    monkeypatch.setattr(agent_view.getpass, "getuser", lambda: user)
    forms = _forms_of(name) if source == "folder" else [
        name, name.upper(), name.swapcase(), unicodedata.normalize("NFD", name), f"weekly_{name}"]
    for form in forms:
        # An underscore is a token separator even without a path slash.
        text = f"Provider reports {form.replace('weekly_/', 'weekly_')} today"
        assert _survivors(agent_view.scrub(text), [name]) == [], text
    assert agent_view.scrub("ordinary harmless prose") == "ordinary harmless prose"


# ---- round 2: names matched exactly, invalid entries, owner words now -------------


def _setup(tmp_path, monkeypatch, user: str, names: list[str], home=None):
    """Accounts whose login folders are ``names`` under ``home``
    (``tmp_path/Users/<user>`` by default), HOME set to that folder."""
    home = home or tmp_path / "Users" / user
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    accounts = []
    for i, name in enumerate(names):
        (home / name).mkdir(parents=True, exist_ok=True)
        accounts.append({"label": f"Acct {i}", "harness": "claude", "home": str(home / name)})
    (data / "config.json").write_text(json.dumps({"usage_accounts": accounts}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.delenv(ms.GATEWAY_ACTIVE_ENV, raising=False)
    monkeypatch.setattr(ms, "_GATEWAY_ACTIVE", False)
    monkeypatch.setattr(ms, "_RELOAD_ON_CHANGE", False)
    monkeypatch.setattr(usage, "_command", lambda h: None)
    monkeypatch.setattr(agent_view, "_TABLE", {"sig": None, "at": 0.0, "table": None})
    return home, usage.configured_accounts()


def _forms_of(name: str) -> list[str]:
    bare = name.lstrip(".")
    return [name, bare, bare.upper(), bare.swapcase(), unicodedata.normalize("NFD", name), f"weekly_/{bare}"]


def _survivors(value, words: list[str]) -> list[tuple[str, str]]:
    """(word, text) for each whole-word appearance of a word, in any case or
    normal form: no letter or digit either side, in any script."""
    found = []
    for text in _strings(value):
        folded = unicodedata.normalize("NFC", text).casefold()
        for word in words:
            w = unicodedata.normalize("NFC", word.lstrip(".")).casefold()
            if re.search(rf"(?<![^\W_]){re.escape(w)}(?![^\W_])", folded):
                found.append((word, text))
    return found


def _seed(home, accounts, user: str):
    """Every account's name in a cached row's vendor fields and a failed
    read, and in a conversation's report about it."""
    rows = {}
    for i, account in enumerate(accounts):
        name = Path(account["home"]).name
        forms = _forms_of(name)
        if i % 2:
            rows[usage._key(account)] = row(account, plan=forms[i % len(forms)], version=user.upper(),
                                            windows=[{"name": f"weekly_/{name.lstrip('.')}", "used_pct": 95,
                                                      "model": forms[2], "window_mins": 10080,
                                                      "resets_at": usage._iso(time.time() + 86400)}])
        else:
            rows[usage._key(account)] = row(account, ok=False, error=forms[1], live_error=forms[3],
                                            note=f"/{user} could not read {forms[4]}", windows=[])
        routing.conversation_write({"action": "report", "state": "limited", "account": account["label"],
                                    "until": "1h", "evidence": " and ".join(f"/{f}" for f in forms) + f" for /{user}"},
                                   author={"harness": "claude-code"})
    usage._save_cache(rows)


def _agent_surfaces(capsys) -> dict:
    out = {"khipu_usage": call("khipu_usage")[1], "khipu_route": call("khipu_route")[1],
           "summary": routing.summary(), "start push": routing.light_status(start=True),
           "prompt push": routing.prompt_push(None, budget=5)}
    payload: dict = {}
    ms._attach_plan_warnings(payload)
    out["khipu_status warnings"] = payload
    assert cli.main(["route", "--summary"]) == 0
    out["route --summary"] = capsys.readouterr().out
    return out


def test_hard_folder_names_and_a_short_user_name_never_reach_an_agent(tmp_path, monkeypatch, capsys):
    names = [".Team Red", ".café-login", ".abc", ".xy"]
    home, accounts = _setup(tmp_path, monkeypatch, "li", names)
    _seed(home, accounts, "li")
    for surface, out in _agent_surfaces(capsys).items():
        assert _survivors(out, [*names, "li"]) == [], surface


def test_an_invalid_entry_never_shows_any_part_of_its_path(tmp_path, monkeypatch):
    # Khipu lowercases an entry's CLI, so HOME is given in lowercase too (the
    # Mac's folders ignore case): the entry's path then starts with HOME
    # exactly as written, the way it does under /tmp.
    home, _ = _setup(tmp_path, monkeypatch, "probeuser", [])
    home = Path(str(home).lower())
    monkeypatch.setenv("HOME", str(home))
    from khipu.config import config_file

    config_file().write_text(json.dumps({"usage_accounts": [
        {"harness": str(home / ".probe-login"), "home": "relative-folder"},
        {"harness": "claude", "home": "~/Some Login/.zq-login"}]}))
    err, out = call("khipu_usage")
    assert not err
    assert _survivors(out, ["probe-login", "relative-folder", "Some Login", "zq-login", "probeuser"]) == []
    assert str(tmp_path).lower() not in str(out).lower()


def test_an_ended_note_is_no_longer_kept(owner, monkeypatch):
    routing.set_note(owner["login"], via="app")
    assert owner["login"] in json.dumps(call("khipu_route")[1])
    routing.end_note(via="app")
    call("khipu_route")

    def fail(**_kw):
        raise ValueError(f"Provider failed at {owner['login']}")
    monkeypatch.setattr(usage, "report", fail)
    err, out = call("khipu_usage")
    assert err and owner["login"] not in out["error"] and NAME not in out["error"], out


def _random_name(rng: random.Random) -> str:
    # Letters that never appear in an account id (hex) or Khipu's own words.
    alphabet = "qxzjQXZJéøÅÉ"
    word = lambda n: "".join(rng.choice(alphabet) for _ in range(n))  # noqa: E731
    shape = rng.choice(["short", "space", "dots", "long"])
    if shape == "short":
        name = word(rng.randint(1, 3))
    elif shape == "space":
        name = f"{word(rng.randint(1, 4))} {word(rng.randint(1, 4))}"
    elif shape == "dots":
        name = f"{word(2)}.{word(rng.randint(1, 3))}"
    else:
        name = word(rng.randint(5, 12))
    return ("." if rng.random() < 0.6 else "") + name


@pytest.mark.parametrize("login,user,payload", [
    ("/srv/Maße-Straße/.claude", "probeuser", "/srv/Maße-Straße/.claude"),
    (".login", "probeuser", ".login"),
    (".home", "probeuser", ".home"),
    (".folder", "probeuser", ".folder"),
    (".zq-private", "Home", "home"),
    (".zq-private", "Login", "login"),
    (".zq-private", "User", "user"),
])
def test_casefold_and_reserved_secrets_never_reach_agents(
        tmp_path, monkeypatch, capsys, login, user, payload):
    from khipu.config import config_file

    home, _ = _setup(tmp_path, monkeypatch, user, [])
    monkeypatch.setattr(agent_view.getpass, "getuser", lambda: user)
    login = str(home / login) if not login.startswith("/") else login
    config_file().write_text(json.dumps({"usage_accounts": [
        {"label": "Work", "harness": "claude", "home": login}]}))
    monkeypatch.setattr(usage, "_command", lambda h: "/tmp/fake-cli")
    # The login folder need not exist: the fake CLI answers either way.
    monkeypatch.setattr(usage, "_login_folder_there", lambda home: True)
    monkeypatch.setattr(usage, "_converse", lambda *a, **kw: [
        {"type": "control_response", "response": {"request_id": "init", "response":
            {"claude_code_version": payload}}},
        {"type": "control_response", "response": {"request_id": "usage", "response":
            {"subscription_type": payload, "rate_limits": {"model_scoped": [
                {"display_name": payload, "utilization": 95,
                 "resets_at": usage._iso(time.time() + 86400)}]}}}}])
    assert usage.report(fresh=True)["accounts"][0]["plan"] == payload
    if "Maße-Straße" in payload:
        assert agent_view.scrub(payload + "/private-child") == "[redacted]"
    for surface, out in _agent_surfaces(capsys).items():
        assert all(agent_view._fold(payload) not in agent_view._fold(s)
                   for s in _strings(out)), surface


def test_name_matching_uses_dot_tokens_and_plain_path_components(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "login", [".home", ".folder", "plainroot"])
    monkeypatch.setattr(agent_view.getpass, "getuser", lambda: "login")
    table = agent_view._rules()
    for text in ("read .home now", "read .folder now", "read home now", "read folder now", "read /login/file",
                 "read ~/plainroot/file", "plainroot/file", "login/file"):
        assert table.names.search(text), text
    for text in ("go to login", "a plainroot in prose", ".homework", "plainrooted/file"):
        assert not table.names.search(text), text


def test_final_containment_guard_covers_fast_path_and_owner_exemption(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "probeuser", [".Maße-Straße"])
    note = "Owner permits .Maße-Straße"
    routing.set_note(note, via="app")
    routing.add_rule(note, via="app")
    secret = "prefixMASSE-Straßesuffix"
    assert agent_view.scrub(secret) == agent_view.scrub("other " + secret)
    assert "masse-strasse" not in agent_view._fold(agent_view.scrub(secret))
    assert agent_view.scrub(note) == note
    mixed = agent_view.scrub(note + " plus " + secret)
    assert note in mixed
    assert "masse-strasse" not in agent_view._fold(mixed.replace(note, ""))


@pytest.mark.parametrize("seed", range(25))
def test_generated_folder_and_user_names_never_reach_an_agent(tmp_path, monkeypatch, capsys, seed):
    rng = random.Random(seed)
    names: list[str] = []
    while len(names) < 3:
        name = _random_name(rng)
        if all(name.lstrip(".").casefold() != n.lstrip(".").casefold() for n in names):
            names.append(name)
    user = _random_name(rng).lstrip(".").replace(" ", "") or "qz"
    home, accounts = _setup(tmp_path, monkeypatch, user, names)
    _seed(home, accounts, user)
    for surface, out in _agent_surfaces(capsys).items():
        assert _survivors(out, [*names, user]) == [], (seed, surface, names, user)
