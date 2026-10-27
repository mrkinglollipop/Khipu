"""Routing slice 8c (case 7c), hermetic layer: one T3 thread keeps one
routing ledger across a Claude Code -> Codex provider switch, doctor names
T3 providers without Khipu hooks, and an unhooked T3 child's parent gets
the brief advisory. Fake T3 database and temp HOME only; no real harness.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

import pytest

from khipu import routing, routing_delegation, routing_delivery, t3
from tests.test_routing_s7_fix1 import home, _call, _route  # noqa: F401  (fixture + helpers)

THREAD = "thread-synthetic-1"
ADVISORY = "This child got no routing: add `khipu route --brief` to its brief."
BRIEF = "Build the synthetic routing fixture carefully and report back."


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    # Identity pinning writes under Khipu's state dir; keep it in the
    # test's temp HOME so no real state is touched or leaks between tests.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))


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
    assert "Codex bare (codex) has no Khipu hooks" in warnings
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


@pytest.mark.parametrize("brief", [
    "Routing at the edge of the network, the build host moves first. " + BRIEF,
    "## Routing table for this job\n" + BRIEF,
    "Before starting, see Khipu's routing record docs for background. " + BRIEF,
])
def test_delegate_task_with_prose_mentioning_routing_still_gets_advisory(delegation_home, brief):
    routing.add_rule("synthetic owner rule", via="app")
    assert ADVISORY in _delegate(delegation_home, brief)


def test_delegate_task_to_hooked_t3_provider_gets_no_advisory(delegation_home):
    routing.add_rule("synthetic owner rule", via="app")
    assert ADVISORY not in _delegate(delegation_home, BRIEF, hooked=True)


# ---- fix round 1: mapped children, ambiguous ids, lookup shape, pinning -----


@pytest.mark.parametrize("prefix", ["claude", "codex"])
def test_mapped_parent_accepts_native_prefixed_children(t3_db, prefix):
    env = {"CLAUDE_CODE_SESSION_ID": "claude-one"}
    child = routing_delivery.canonical_identity(env, explicit=f"{prefix}:claude-one/c")
    assert child is not None
    assert child["root"] == child["session"] == f"t3:{THREAD}/c"
    assert child["child"] is True and child["capability"] == "pull_only"
    deep = routing_delivery.canonical_identity(env, explicit=f"{prefix}:claude-one/a/b")
    assert deep["root"] == f"t3:{THREAD}/a/b"
    assert routing_delivery.hook_session(f"{prefix}:claude-one/c", env) == f"t3:{THREAD}/c"
    # The mapped spelling still works, and an invalid child still fails closed.
    assert routing_delivery.canonical_identity(env, explicit=f"t3:{THREAD}/c")["root"] == f"t3:{THREAD}/c"
    assert routing_delivery.canonical_identity(env, explicit=f"{prefix}:claude-one/../x") is None
    assert routing_delivery.canonical_identity(env, explicit=f"{prefix}:someone-else/c") is None


def test_mapped_child_of_a_bystander_payload_resolves_to_its_own_thread(t3_db):
    env = {"CLAUDE_CODE_SESSION_ID": "claude-one"}
    assert routing_delivery.hook_session("codex:codex-two/kid", env) == f"t3:{THREAD}/kid"
    assert routing_delivery.hook_session("codex:codex-two/kid", {}) == f"t3:{THREAD}/kid"
    assert routing_delivery.hook_session("claude:absent/kid", {}) == "claude:absent/kid"


def _raw_db(tmp_path, monkeypatch, rows):
    path = tmp_path / "statev2.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE orchestration_v2_projection_provider_threads "
                "(thread_id TEXT, payload_json TEXT, updated_at TEXT)")
    con.executemany("INSERT INTO orchestration_v2_projection_provider_threads VALUES (?, ?, ?)", rows)
    con.commit()
    con.close()
    monkeypatch.setenv("KHIPU_T3_DB", str(path))
    return path


def _row(thread, sid, at="2026-10-20"):
    return (thread, json.dumps({"nativeThreadRef": {"nativeId": sid}}), at)


def test_native_id_in_two_distinct_threads_resolves_to_none(tmp_path, monkeypatch):
    _raw_db(tmp_path, monkeypatch, [
        _row("OLD", "abc", "2026-10-21"), _row("NEW", "abc", "2026-10-20")])
    assert t3.thread_for_session("abc") is None
    identity = routing_delivery.canonical_identity({"CLAUDE_CODE_SESSION_ID": "abc"})
    assert identity["root"] == "claude:abc"


def test_native_id_repeated_for_one_thread_still_resolves(tmp_path, monkeypatch):
    _raw_db(tmp_path, monkeypatch, [
        _row(THREAD, "abc", "2026-10-20"), _row(THREAD, "abc", "2026-10-21"),
        _row("other-thread", "xyz", "2026-10-21")])
    assert t3.thread_for_session("abc") == THREAD


def test_lookup_on_a_large_db_finds_the_current_session_within_its_deadline(tmp_path, monkeypatch):
    blob = "x" * 6000
    rows = [(f"old-{i}", json.dumps({"nativeThreadRef": {"nativeId": f"n{i}"}, "blob": blob}),
             "2026-10-07") for i in range(15000)]
    rows.append(_row(THREAD, "current-session", "2026-10-21"))
    _raw_db(tmp_path, monkeypatch, rows)
    start = time.monotonic()
    assert t3.thread_for_session("current-session") == THREAD
    assert time.monotonic() - start < 5


def test_first_identity_answer_is_pinned_for_the_session(tmp_path, monkeypatch):
    path = _raw_db(tmp_path, monkeypatch, [])
    env = {"CLAUDE_CODE_SESSION_ID": "late-row"}
    assert routing_delivery.canonical_identity(env)["root"] == "claude:late-row"
    with sqlite3.connect(path) as con:
        con.execute("INSERT INTO orchestration_v2_projection_provider_threads VALUES (?, ?, ?)",
                    _row(THREAD, "late-row", "2026-10-21"))
        con.execute("INSERT INTO orchestration_v2_projection_provider_threads VALUES (?, ?, ?)",
                    _row(THREAD, "early-row", "2026-10-21"))
    # The row landing mid-session must not flip this session's ledger.
    assert routing_delivery.canonical_identity(env)["root"] == "claude:late-row"
    assert routing_delivery.hook_session("late-row", env) == "claude:late-row"
    # ... and a session first seen mapped stays mapped if the row goes away.
    mapped_env = {"CODEX_THREAD_ID": "early-row"}
    assert routing_delivery.canonical_identity(mapped_env)["root"] == f"t3:{THREAD}"
    with sqlite3.connect(path) as con:
        con.execute("DELETE FROM orchestration_v2_projection_provider_threads "
                    "WHERE thread_id = ?", (THREAD,))
    assert routing_delivery.canonical_identity(mapped_env)["root"] == f"t3:{THREAD}"


# ---- fix round 2: N1 definitive vs failure, N2 rowid fallback, N3 pins ----


def _pin_files():
    return list(routing_delivery._t3_pin_path("probe").parent.glob("t3-pin--*.json"))


def test_n1_transient_failure_is_not_pinned_and_a_later_hook_retries(tmp_path, monkeypatch):
    missing = tmp_path / "missing.sqlite"
    monkeypatch.setenv("KHIPU_T3_DB", str(missing))
    env = {"CLAUDE_CODE_SESSION_ID": "f1"}
    # DB unavailable at the first hook: a failure, not a settled miss.
    assert t3.thread_for_session_result("f1") == (None, False)
    assert t3.thread_for_session("f1") is None
    assert routing_delivery.canonical_identity(env)["root"] == "claude:f1"
    assert _pin_files() == []
    # The DB is back, with a row: the next hook maps the session instead
    # of staying stranded on the pin a failure never wrote.
    _raw_db(tmp_path, monkeypatch, [_row(THREAD, "f1", "2026-10-21")])
    assert t3.thread_for_session_result("f1") == (THREAD, True)
    assert routing_delivery.canonical_identity(env)["root"] == f"t3:{THREAD}"


def test_n1_query_error_is_a_failure_and_empty_db_is_a_definitive_miss(tmp_path, monkeypatch):
    _raw_db(tmp_path, monkeypatch, [])
    assert t3.thread_for_session_result("nobody") == (None, True)
    broken = tmp_path / "broken.sqlite"
    with sqlite3.connect(broken) as con:
        con.execute("CREATE TABLE renamed (id TEXT)")
    monkeypatch.setenv("KHIPU_T3_DB", str(broken))
    assert t3.thread_for_session_result("nobody") == (None, False)
    assert t3.thread_for_session("nobody") is None


def test_n2_without_rowid_table_falls_back_to_a_bounded_query(tmp_path, monkeypatch):
    path = tmp_path / "statev2.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE orchestration_v2_projection_provider_threads "
                "(thread_id TEXT PRIMARY KEY, payload_json TEXT, updated_at TEXT) WITHOUT ROWID")
    con.execute("INSERT INTO orchestration_v2_projection_provider_threads VALUES (?, ?, ?)",
                _row(THREAD, "r1", "2026-10-21"))
    con.commit()
    con.close()
    monkeypatch.setenv("KHIPU_T3_DB", str(path))
    assert t3.thread_for_session("r1") == THREAD
    assert t3.thread_for_session_result("r1") == (THREAD, True)
    assert t3.thread_for_session_result("absent") == (None, True)


def test_n3_no_pin_files_on_a_mac_with_no_t3_database(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_T3_DB", str(tmp_path / "missing.sqlite"))
    for i in range(5):
        identity = routing_delivery.canonical_identity({"CLAUDE_CODE_SESSION_ID": f"no-t3-{i}"})
        assert identity["root"] == f"claude:no-t3-{i}"
    assert _pin_files() == []


def test_n3_pin_files_are_capped_and_expire(tmp_path, monkeypatch):
    _raw_db(tmp_path, monkeypatch, [])
    for i in range(routing_delivery._T3_PIN_MAX + 12):
        routing_delivery.canonical_identity({"CLAUDE_CODE_SESSION_ID": f"capped-{i}"})
    pins = _pin_files()
    assert 0 < len(pins) <= routing_delivery._T3_PIN_MAX
    # An expired pin no longer settles the session: the row that landed
    # after the (now stale) native pin is honoured on the next lookup.
    env = {"CLAUDE_CODE_SESSION_ID": "old-session"}
    assert routing_delivery.canonical_identity(env)["root"] == "claude:old-session"
    pin = routing_delivery._t3_pin_path("old-session")
    assert pin.is_file()
    stale = time.time() - routing_delivery._T3_PIN_TTL_S - 60
    os.utime(pin, (stale, stale))
    path = tmp_path / "statev2.sqlite"
    with sqlite3.connect(path) as con:
        con.execute("INSERT INTO orchestration_v2_projection_provider_threads VALUES (?, ?, ?)",
                    _row(THREAD, "old-session", "2026-10-21"))
    assert routing_delivery.canonical_identity(env)["root"] == f"t3:{THREAD}"


def _provider_with_command(tmp_path, command):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(parents=True, exist_ok=True)
    hooks = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}}
    (codex_home / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
    return {"driver": "codex", "home_path": str(codex_home)}


@pytest.mark.parametrize("command,expected", [
    ("echo not-a-khipu-hook", "missing"),
    ("echo khipu-recall-hook", "missing"),
    ("echo /opt/khipu/bin/khipu-stop-hook", "missing"),
    ("khipu-recall-hook-extra", "missing"),
    ("khipu-recall-hook", "installed"),
    ("/opt/khipu/bin/khipu-prompt-recall", "installed"),
    ("env KHIPU_CAPTURE_QUEUE_ONLY=1 /opt/khipu/bin/khipu-stop-hook", "installed"),
    # N4: a quoted or unquoted absolute path with spaces, standing alone.
    ('"/work/Shared Projects/Khipu/bin/khipu-stop-hook"', "installed"),
    ("/work/Shared Projects/Khipu/bin/khipu-stop-hook", "installed"),
    ("'/work/Shared Projects/Khipu/bin/khipu-recall-hook'", "installed"),
])
def test_hook_detection_matches_actual_hook_command_names(tmp_path, command, expected):
    assert t3.provider_hooks(_provider_with_command(tmp_path, command), tmp_path) == expected
