"""Slice 8b (case 7b): Codex native child.

Hermetic: the bounded child block from SubagentStart, and the advisory
line on the parent's spawn_agent result when Codex's hooks are absent
and the child's brief carries no routing. Fake data, temp HOME only.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from khipu import recall_prompt, routing, routing_delegation, routing_delivery

T0 = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
ACCOUNTS = [{"label": "Claude", "harness": "claude"}, {"label": "Codex", "harness": "codex"}]
ME = {"kind": "conversation", "harness": "codex", "project": "project-a", "session": "s-parent"}
SESSION = "11111111-2222-3333-4444-555555555555"
ADVISORY = "This child got no routing: add `khipu route --brief` to its brief."


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now = self.now + timedelta(**kw)
        return self.now


@pytest.fixture
def home(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({
        "usage_accounts": ACCOUNTS, "routing_prompt_budget_ms": 1000,
        "routing_reset_check": False}))
    capture = Path(tempfile.mkdtemp(prefix="kr8b-", dir="/tmp"))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(capture))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("KHIPU_ISOLATED_DIR", raising=False)
    monkeypatch.delenv("ROUTING_NOW_OFF", raising=False)
    monkeypatch.delenv("KHIPU_HARNESS", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "1")
    monkeypatch.setattr(routing, "ROUTING_NOW_DISABLE_FILE", str(tmp_path / "routing_now_disable"))
    clock = Clock(T0)
    monkeypatch.setattr(routing, "_now", clock)
    routing.report(account="Codex", state="limited", until="1h", author=ME)
    routing.report(account="Claude", state="open", author=ME)
    yield {"data": data, "clock": clock, "capture": capture, "root": tmp_path}
    shutil.rmtree(capture, ignore_errors=True)


def _set(home, **values):
    path = home["data"] / "config.json"
    config = json.loads(path.read_text())
    config.update(values)
    path.write_text(json.dumps(config))


def _utf16(text):
    return len(text.encode("utf-16-le")) // 2


def _oversized_rules(count=4):
    for i in range(count):
        routing.add_rule(f"SYNTHETIC-ROUTING-OPENING-{i} " + "~" * 2750 + f" SYNTHETIC-ROUTING-END-{i}",
                         via="app")


def _spawn(message="Read probe.txt once, then answer OK. Do nothing else.", **extra):
    out = {"hook_event_name": "PreToolUse", "session_id": SESSION, "cwd": "/work/project-a",
           "tool_name": "spawn_agent", "tool_input": {"message": message}}
    out.update(extra)
    return out


def _ctx(out):
    return out["hookSpecificOutput"]["additionalContext"] if out else ""


def _child_text(agent="child-1", session=SESSION, environ=None):
    """The unified child block, through the SubagentStart hook."""
    out, _info, _kind = routing_delegation.hook_main(json.dumps(
        {"hook_event_name": "SubagentStart", "session_id": session, "agent_id": agent}), environ)
    return _ctx(out)


# ---- the bounded child block -------------------------------------------------


def test_child_block_orders_accounts_holds_rules_note_and_fits_small_case(home):
    routing.add_rule("Codex does the builds.", via="app")
    routing.set_note("Owner note words.", via="app")
    routing.add_hold("keep it for the audit", reason="keep", scope="machine", owner=True, via="app")
    block = _child_text()
    assert block.startswith("## Routing at ")
    lines = block.splitlines()
    account_at = next(i for i, line in enumerate(lines) if line.startswith("Codex:"))
    hold_at = next(i for i, line in enumerate(lines) if line.startswith("Hold on"))
    rule_at = next(i for i, line in enumerate(lines) if "Codex does the builds." in line)
    note_at = next(i for i, line in enumerate(lines) if "Owner note words." in line)
    assert account_at < hold_at < rule_at < note_at
    assert _utf16(block) <= 4000


def test_child_block_bounds_oversized_rules_to_openings(home):
    _oversized_rules()
    block = _child_text()
    assert _utf16(block) <= 4000
    # Accounts come before any rule.
    assert block.index("Codex:") < block.index("SYNTHETIC-ROUTING-OPENING-0")
    # The first rule that fits is whole; a later oversized rule is present
    # only as its number, kind, accounts and opening, never whole.
    assert "SYNTHETIC-ROUTING-END-0" in block
    assert "Standing rule 2" in block
    assert "SYNTHETIC-ROUTING-OPENING-1" in block
    assert "SYNTHETIC-ROUTING-END-1" not in block


def test_child_block_respects_the_configured_bound(home):
    _set(home, routing_child_chars=500)
    _oversized_rules(1)
    block = _child_text()
    assert 0 < _utf16(block) <= 500
    assert "Standing rule 1" in block


def test_subagent_start_returns_bounded_block_not_nothing_when_oversized(home, monkeypatch):
    _oversized_rules(2)
    before = recall_prompt.routing_marker(SESSION)
    out, info, kind = routing_delegation.hook_main(json.dumps(
        {"hook_event_name": "SubagentStart", "session_id": SESSION, "agent_id": "child-1"}))
    assert out["hookSpecificOutput"]["hookEventName"] == "SubagentStart"
    text = _ctx(out)
    assert text.startswith("## Routing at ")
    assert _utf16(text) <= 4000
    assert "SYNTHETIC-ROUTING-OPENING-0" in text
    assert "SYNTHETIC-ROUTING-END-0" in text  # the first fitting rule is whole, not a page
    assert recall_prompt.routing_marker(SESSION) == before  # the parent never saw it


def test_codex_subagent_start_is_a_snapshot_and_defers_to_its_own_pull(home, monkeypatch):
    _oversized_rules(2)
    monkeypatch.setenv("CODEX_THREAD_ID", "codex-child-1")
    before = recall_prompt.routing_marker(SESSION)
    out, info, kind = routing_delegation.hook_main(json.dumps(
        {"hook_event_name": "SubagentStart", "session_id": SESSION, "agent_id": "child-1"}),
        {**os.environ})
    text = _ctx(out)
    assert text.startswith("## Routing at ")
    assert _utf16(text) <= 4000
    assert "SYNTHETIC-ROUTING-OPENING-0" in text
    assert "SYNTHETIC-ROUTING-END-0" in text
    # Its own conversation: nothing reserved under the parent, no ack to record.
    assert (info, kind) == (None, None)
    assert routing_delivery.snapshot(SESSION, "agent:child-1") == {}
    assert routing_delivery.snapshot(SESSION, "main") == {}
    assert recall_prompt.routing_marker(SESSION) == before
    # No deferred page on its own (non-Khipu) tool results either: pull only.
    page_out, page_info, page_kind = routing_delegation.pre_tool_use(
        {"hook_event_name": "PreToolUse", "session_id": "codex-child-session", "agent_id": "child-1",
         "tool_name": "Bash", "tool_input": {"command": "cat probe.txt"}}, environ={**os.environ})
    assert (page_out, page_info, page_kind) == ({}, None, None)
    # Deferred routing arrives on the child's own Khipu pull, under its
    # own CODEX_THREAD_ID identity, with the deferred rule named.
    from khipu.mcp_server import handle_message

    result = handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": "khipu_route", "arguments": {}}})["result"]
    assert not result["isError"], result
    body = json.loads(result["content"][0]["text"])
    entities = [item["entity"] for item in body["routing_changes"]["items"]]
    assert any(entity.startswith("rule:") for entity in entities), body["routing_changes"]
    assert routing_delivery.snapshot("codex:codex-child-1", "agent:child-1") == {}


def test_subagent_start_still_off_when_delegation_push_off(home):
    _set(home, routing_push_delegation=False)
    out, _, _ = routing_delegation.hook_main(json.dumps(
        {"hook_event_name": "SubagentStart", "session_id": SESSION, "agent_id": "child-1"}))
    assert out == {}


# ---- brief inspection and the advisory line ----------------------------------


def test_spawn_without_routing_brief_gets_advisory_when_codex_hook_absent(home):
    out, info, kind = routing_delegation.pre_tool_use(_spawn(), environ={**os.environ, "CODEX_HOME": str(home["root"] / ".codex")})
    assert kind == "agent"
    assert set(out) <= {"hookSpecificOutput"}  # never a deny
    assert ADVISORY in _ctx(out)


def test_spawn_with_routing_in_brief_gets_no_advisory(home):
    brief = "## Routing at 15:00Z (Khipu's routing record)\nCodex: open\n\nRead probe.txt once."
    out, _, _ = routing_delegation.pre_tool_use(_spawn(brief), environ={**os.environ, "CODEX_HOME": str(home["root"] / ".codex")})
    assert ADVISORY not in _ctx(out)


def test_spawn_gets_no_advisory_when_codex_subagent_hook_installed(home):
    codex_home = home["root"] / ".codex"
    codex_home.mkdir()
    (codex_home / "hooks.json").write_text(json.dumps({"hooks": {"SubagentStart": [
        {"hooks": [{"type": "command", "command": "khipu-delegation-hook", "timeout": 10}]}]}}))
    out, _, _ = routing_delegation.pre_tool_use(_spawn(), environ={**os.environ, "CODEX_HOME": str(codex_home)})
    assert ADVISORY not in _ctx(out)


def test_advisory_is_codex_spawn_only_and_never_inside_a_subagent(home):
    env = {**os.environ, "CODEX_HOME": str(home["root"] / ".codex")}
    out, _, _ = routing_delegation.pre_tool_use(
        {"hook_event_name": "PreToolUse", "session_id": SESSION, "tool_name": "Agent",
         "tool_input": {"prompt": "build it, no routing here"}}, environ=env)
    assert ADVISORY not in _ctx(out)
    out, info, kind = routing_delegation.pre_tool_use(_spawn(agent_id="child-1"), environ=env)
    assert (out, kind) == ({}, None)


def test_briefs_of_reads_the_spawn_message(home):
    assert routing_delegation.briefs_of(_spawn("a brief with several words in it")) == [
        "a brief with several words in it"]
