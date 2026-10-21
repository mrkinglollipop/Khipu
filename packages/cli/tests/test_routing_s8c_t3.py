"""Routing slice 8c (case 7c), hermetic layer: one T3 thread keeps one
routing ledger across a Claude Code -> Codex provider switch, doctor names
T3 providers without Khipu hooks, and an unhooked T3 child's parent gets
the brief advisory. Fake T3 database and temp HOME only; no real harness.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from khipu import routing, routing_delegation, routing_delivery, t3
from tests.test_routing_s7_fix1 import home, _call, _route  # noqa: F401  (fixture + helpers)

THREAD = "thread-synthetic-1"
ADVISORY = "This child got no routing: add `khipu route --brief` to its brief."
BRIEF = "Build the synthetic routing fixture carefully and report back."


@pytest.fixture
def t3_db(tmp_path, monkeypatch):
    path = tmp_path / "statev2.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE orchestration_v2_projection_provider_threads "
                "(thread_id TEXT, payload_json TEXT, updated_at TEXT)")
    for sid in ("claude-one", "codex-two"):
        con.execute("INSERT INTO orchestration_v2_projection_provider_threads VALUES (?, ?, ?)",
                    (THREAD, json.dumps({"nativeThreadRef": {"nativeId": sid}}), "2026-10-20"))
    con.commit()
    con.close()
    monkeypatch.setenv("KHIPU_T3_DB", str(path))
    return path


def _providers_settings(root, instances):
    path = root / ".t3" / "userdata" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"providerInstances": instances}), encoding="utf-8")
    return path


def _hooked_codex_home(root):
    codex_home = root / "codex-home"
    codex_home.mkdir(parents=True, exist_ok=True)
    hooks = {"hooks": {"SessionStart": [{"hooks": [{"type": "command",
                                                     "command": "khipu-recall-hook"}]}],
                       "PreToolUse": [{"hooks": [{"type": "command",
                                                  "command": "khipu-delegation-hook"}]}]}}
    (codex_home / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
    return codex_home


# ---- identity: KHIPU_SESSION_ID=t3:<thread> derived by the T3 integration ----


def test_canonical_identity_maps_both_providers_to_one_t3_root(t3_db, monkeypatch):
    monkeypatch.delenv("KHIPU_SESSION_ID", raising=False)
    claude = routing_delivery.canonical_identity({"CLAUDE_CODE_SESSION_ID": "claude-one"})
    codex = routing_delivery.canonical_identity({"CODEX_THREAD_ID": "codex-two"})
    assert claude["root"] == claude["session"] == f"t3:{THREAD}"
    assert codex["root"] == codex["session"] == f"t3:{THREAD}"
    assert claude["capability"] == codex["capability"] == "hooked"
    plain = routing_delivery.canonical_identity({"CLAUDE_CODE_SESSION_ID": "not-in-t3"})
    assert plain["root"] == "claude:not-in-t3"


def test_explicit_khipu_session_id_still_wins_over_t3_mapping(t3_db):
    identity = routing_delivery.canonical_identity(
        {"KHIPU_SESSION_ID": "t3:explicit", "CLAUDE_CODE_SESSION_ID": "claude-one"})
    assert identity["root"] == "t3:explicit"


def test_hook_session_maps_native_payloads_of_both_providers_to_one_root(t3_db):
    assert routing_delivery.hook_session("claude-one", {"CLAUDE_CODE_SESSION_ID": "claude-one"}) == f"t3:{THREAD}"
    assert routing_delivery.hook_session("codex-two", {"CODEX_THREAD_ID": "codex-two"}) == f"t3:{THREAD}"
    # A payload naming its own native session resolves even before the
    # provider's environment variable is visible to this hook process.
    assert routing_delivery.hook_session("codex-two", {}) == f"t3:{THREAD}"
    assert routing_delivery.hook_session("unrelated", {}) == "unrelated"


def test_provider_switch_keeps_one_mcp_ledger_and_acks_the_first_batch(home, t3_db, monkeypatch):  # noqa: F811
    routing.add_rule("synthetic owner rule", via="app")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-one")
    first = _route()
    batch = first["routing_changes"]["batch"]
    assert batch
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    monkeypatch.setenv("CODEX_THREAD_ID", "codex-two")
    out = _route(ack=batch)
    assert out["delivery"]["acked_through"] is not None
    assert not (out.get("routing_changes") or {}).get("items")
    files = list(routing_delivery._path(f"t3:{THREAD}").parent.glob("*.routing.json"))
    assert len(files) == 1
    assert routing_delivery.snapshot(f"t3:{THREAD}", "mcp").get("acked_through") is not None


# ---- doctor names providers without hooks ------------------------------------


def test_doctor_names_t3_providers_without_hooks(tmp_path, monkeypatch):
    hooked = _hooked_codex_home(tmp_path)
    secret = "SYNTHETIC-T3-SETTINGS-SENTINEL"
    _providers_settings(tmp_path, {
        "codex_hooked": {"driver": "codex", "displayName": "Codex hooked",
                         "config": {"homePath": str(hooked)}, "enabled": True, "apiKey": secret},
        "codex_bare": {"driver": "codex", "displayName": "Codex bare",
                       "config": {"homePath": str(tmp_path / "no-hooks-home")}, "enabled": True},
        "cursor_1": {"driver": "cursor", "displayName": "Cursor", "config": {}, "enabled": True},
        "off_1": {"driver": "codex", "displayName": "Codex disabled",
                  "config": {"homePath": str(tmp_path / "no-hooks-home")}, "enabled": False},
    })
    monkeypatch.setenv("KHIPU_T3_DB", str(tmp_path / "missing.sqlite"))
    out = t3.health(tmp_path)
    assert out is not None
    by_id = {p["id"]: p for p in out["providers"]}
    assert by_id["codex_hooked"]["hooks"] == "installed"
    assert by_id["codex_bare"]["hooks"] == "missing"
    assert by_id["cursor_1"]["hooks"] == "missing"
    assert "off_1" not in by_id
    warnings = "\n".join(out["warnings"])
    assert "Codex bare" in warnings and "hooks" in warnings and "missing" in warnings
    assert "Cursor" in warnings
    assert "Codex hooked" not in warnings and "Codex disabled" not in warnings
    assert secret not in json.dumps(out)


def test_doctor_names_claude_provider_whose_home_lacks_hooks(tmp_path, monkeypatch):
    _providers_settings(tmp_path, {
        "claude_1": {"driver": "claudeAgent", "displayName": "Claude bare",
                     "config": {"homePath": str(tmp_path / "claude-bare")}, "enabled": True},
    })
    monkeypatch.setenv("KHIPU_T3_DB", str(tmp_path / "missing.sqlite"))
    out = t3.health(tmp_path)
    assert out["providers"][0]["hooks"] == "missing"
    assert "Claude bare" in "\n".join(out["warnings"])


# ---- delegate_task advisory for a child whose provider has no hooks ----------


@pytest.fixture
def delegation_home(home, monkeypatch):  # noqa: F811
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "1")
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(home["tmp"] / "capture"))
    return home


def _delegate(fixture, brief, *, hooked=False):
    codex_home = _hooked_codex_home(fixture["tmp"]) if hooked else fixture["tmp"] / "no-hooks-home"
    _providers_settings(fixture["tmp"], {
        "codex_2": {"driver": "codex", "displayName": "Codex",
                    "config": {"homePath": str(codex_home)}, "enabled": True},
    })
    payload = {"hook_event_name": "PreToolUse", "session_id": "parent-1", "cwd": str(fixture["tmp"]),
               "tool_name": "delegate_task",
               "tool_input": {"target": {"providerInstanceId": "codex_2"}, "work": {"text": brief}}}
    out, _info, kind = routing_delegation.hook_main(json.dumps(payload))
    assert kind == "delegate_task"
    return out.get("hookSpecificOutput", {}).get("additionalContext", "") if out else ""


def test_delegate_task_to_unhooked_t3_provider_gets_brief_advisory(delegation_home):
    routing.add_rule("synthetic owner rule", via="app")
    assert ADVISORY in _delegate(delegation_home, BRIEF)


def test_delegate_task_with_routing_in_brief_gets_no_advisory(delegation_home):
    routing.add_rule("synthetic owner rule", via="app")
    brief = "Routing at 15:00Z (Khipu's routing record, version 1; guidance, not a gate)\n" + BRIEF
    assert ADVISORY not in _delegate(delegation_home, brief)


def test_delegate_task_to_hooked_t3_provider_gets_no_advisory(delegation_home):
    routing.add_rule("synthetic owner rule", via="app")
    assert ADVISORY not in _delegate(delegation_home, BRIEF, hooked=True)
