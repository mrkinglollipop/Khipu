"""Routing truth, phase 6 (docs/plans/2026-10-14-routing-truth.md): the push
at the moment of delegation. A PreToolUse hook matched only to delegation
calls sends what the per-prompt push would send, under its seen marker and
budget, never inside a subagent and never as a deny; a SubagentStart hook
gives a Claude Code subagent the full summary. Hermetic: its own data dir
and capture home, a fake clock, no network.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

from khipu import agent_view
from khipu import integrations as integ
from khipu import recall_prompt, routing, routing_delegation

T0 = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
ACCOUNTS = [{"label": "Claude", "harness": "claude"}, {"label": "Codex", "harness": "codex"}]
ME = {"kind": "conversation", "harness": "claude_code", "project": "project-a", "session": "s-one"}
SESSION = "11111111-2222-3333-4444-555555555555"
FULL = "## Routing at "
CHANGES = "## Routing changes at "
LAUNCHER = Path(__file__).resolve().parents[1] / "bin" / "khipu-delegation-hook"


class Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> datetime:
        self.now = self.now + timedelta(**kw)
        return self.now


@pytest.fixture
def home(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": ACCOUNTS, "routing_prompt_budget_ms": 1000,
                                                  "routing_reset_check": False}))
    capture = Path(tempfile.mkdtemp(prefix="krd-", dir="/tmp"))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(capture))
    monkeypatch.delenv("KHIPU_ISOLATED_DIR", raising=False)
    monkeypatch.delenv("ROUTING_NOW_OFF", raising=False)
    monkeypatch.delenv("KHIPU_HARNESS", raising=False)
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "1")
    monkeypatch.setattr(routing, "ROUTING_NOW_DISABLE_FILE", str(tmp_path / "routing_now_disable"))
    clock = Clock(T0)
    monkeypatch.setattr(routing, "_now", clock)
    routing.report(account="Codex", state="limited", until="1h", author=ME)
    routing.report(account="Claude", state="open", author=ME)
    yield {"data": data, "clock": clock, "capture": capture}
    shutil.rmtree(capture, ignore_errors=True)


def _set(home, **values) -> None:
    path = home["data"] / "config.json"
    config = json.loads(path.read_text())
    config.update(values)
    path.write_text(json.dumps(config))


def _call(tool: str = "Agent", tool_input: dict | None = None, **extra) -> dict:
    out = {"hook_event_name": "PreToolUse", "session_id": SESSION, "cwd": "/work/project-a",
           "tool_name": tool, "tool_input": tool_input if tool_input is not None else {"prompt": "build it"}}
    out.update(extra)
    return out


def _run(payload: dict, monkeypatch, environ: dict | None = None) -> dict:
    """The hook as its launcher runs it: payload on stdin, JSON on stdout,
    the marker moved after the write."""
    import io
    from contextlib import redirect_stdout

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    with redirect_stdout(buf):
        if environ is None:
            routing_delegation.main()
        else:
            with mock.patch.dict(os.environ, environ):
                routing_delegation.main()
    return json.loads(buf.getvalue())


def _ctx(out: dict) -> str:
    return out["hookSpecificOutput"]["additionalContext"] if out else ""


def _marker() -> dict:
    return recall_prompt.routing_marker(SESSION)


def _seen_now() -> dict:
    view = routing.read_view()
    assert recall_prompt.mark_routing_seen(SESSION, version=view["version"], epoch=view["epoch"],
                                           full_at=routing._iso(routing._now()))
    return _marker()


def _never_a_decision(out: dict) -> None:
    """Only additionalContext, ever: no permission decision, no block, no
    changed input, no stop."""
    assert set(out) <= {"hookSpecificOutput"}, out
    if out:
        assert set(out["hookSpecificOutput"]) == {"hookEventName", "additionalContext"}, out


def _log_lines() -> list[str]:
    path = recall_prompt._log_path()
    return path.read_text().splitlines() if path.exists() else []


# ---- which calls are delegations ------------------------------------------------------------

@pytest.mark.parametrize("payload, kind", [
    (_call("Agent"), "agent"),
    (_call("Task"), "agent"),
    (_call("delegate_task", {"target": {}}), "delegate_task"),
    (_call("mcp__t3__delegate_task", {"target": {}}), "delegate_task"),
    (_call("spawn_agent", {"message": "review the diff", "task_name": "review"}), "agent"),
])
def test_delegation_calls_are_matched(payload, kind):
    assert routing_delegation.delegation_kind(payload) == kind


@pytest.mark.parametrize("payload", [
    _call("Read", {"file_path": "/work/a.py"}),
    _call("Edit", {"file_path": "/work/a.py"}),
    _call("TaskCreate", {"subject": "x"}),
    _call("Bash", {"command": "git status"}),
    {"hook_event_name": "PreToolUse", "session_id": SESSION},
    _call("spawn_agents_on_csv", {"csv": "x"}),
    _call("send_input", {"message": "codex exec hi"}),
    # Shell commands are never delegations since the shell half was removed
    # (2026-11-07): a job started from a shell hears routing at its own start.
    _call("Bash", {"command": "claude -p 'build the thing'"}),
    _call("Bash", {"command": "cd /work && timeout 600 codex exec -m m 'fix it'"}),
    _call("Bash", {"command": "grok -p 'audit this'"}),
    _call("Bash", {"command": "nohup muse exec 'second opinion' &"}),
    _call("shell", {"command": ["bash", "-lc", "claude -p hi"]}),
    _call("exec_command", {"cmd": "codex exec hi", "workdir": "/work"}),
    _call("shell_command", {"command": "grok -p 'audit'", "workdir": "/work"}),
    _call("local_shell", {"command": ["codex", "exec", "hi"]}),
])
def test_other_calls_are_not_matched(payload):
    assert routing_delegation.delegation_kind(payload) is None
    assert not routing_delegation.might_delegate(payload)


def test_a_shell_call_reads_nothing(monkeypatch):
    # Even with routing on, a shell call ends at the tool name: no setting,
    # file or routing read, and no brief is noted.
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "1")
    boom = mock.Mock(side_effect=AssertionError("read"))
    monkeypatch.setattr(routing, "read_view", boom)
    monkeypatch.setattr(routing, "settings", boom)
    monkeypatch.setattr(recall_prompt, "_RoutingAtPrompt", boom)
    for command in ("ls -la", "claude -p 'go'", "codex exec hi", "echo " + "claude " * 200_000):
        assert routing_delegation.pre_tool_use(_call("Bash", {"command": command})) == ({}, None, None)
    from khipu import owner_turns

    monkeypatch.setattr(owner_turns, "note_brief", boom)
    routing_delegation.note_brief(_call("Bash", {"command": "claude -p 'a brief long enough to note'"}), os.environ)


# ---- what a delegation call hears ------------------------------------------------------------

def test_a_delegation_call_with_no_last_seen_version_gets_the_full_summary(home, monkeypatch):
    out = _run(_call("Agent"), monkeypatch)
    _never_a_decision(out)
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert _ctx(out) == routing.light_status()["text"]
    view = routing.read_view()
    assert _marker() == {"version": view["version"], "epoch": view["epoch"], "full_at": "2026-10-14T15:00:00Z"}
    assert re.search(r"\[khipu-delegation-hook\] session=" + SESSION
                     + r" event=PreToolUse tool=Agent outcome=pushed call=agent routing=full ms=\d+\.\d$",
                     _log_lines()[-1])


def test_every_tool_call_logs_its_cost_and_outcome(home, monkeypatch):
    """A call the hook was handed but that is no delegation (an older
    install's wide matcher still sends shell calls until repair) reads
    nothing and pushes nothing, yet its cost is still one line."""
    out = _run(_call("Bash", {"command": "ls -la"}), monkeypatch)
    assert out == {}
    assert re.search(r"\[khipu-delegation-hook\] session=" + SESSION
                     + r" event=PreToolUse tool=Bash outcome=pass ms=\d+\.\d$", _log_lines()[-1])
    assert "[khipu-prompt-recall]" not in _log_lines()[-1]  # never counted as a per-prompt outcome


def test_a_failed_call_logs_the_error_and_still_prints_nothing(home, monkeypatch):
    def boom(raw, environ=None):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(routing_delegation, "hook_main", boom)
    assert _run(_call("Bash", {"command": "ls"}), monkeypatch) == {}
    assert re.search(r"outcome=error:RuntimeError ms=\d+\.\d$", _log_lines()[-1])


def test_the_seen_marker_is_shared_with_the_per_prompt_push(home, monkeypatch):
    _seen_now()  # the per-prompt push (or the start) delivered the current view
    out = _run(_call("delegate_task", {"target": {}}), monkeypatch)
    assert out == {}  # nothing changed since the conversation last heard
    assert re.search(r"outcome=delegation call=delegate_task routing=none ms=\d+\.\d$", _log_lines()[-1])
    home["clock"].advance(minutes=10)
    routing.report(account="Claude", state="limited", until="2h", author=ME)
    out = _run(_call("delegate_task", {"target": {}}), monkeypatch)
    _never_a_decision(out)
    lines = _ctx(out).splitlines()
    assert lines[0].startswith(CHANGES) and len(lines) == 2 and lines[1].startswith("Claude: limit reached")
    marker = _marker()
    assert marker["version"] == routing.read_view()["version"]
    assert marker["full_at"] == "2026-10-14T15:00:00Z"
    # The per-prompt push reads the same marker: the change is not repeated at the next prompt.
    agent_view.prepare_routing_table(refresh=True)
    assert routing.prompt_push(marker, budget=1.0)["kind"] == "none"


def test_a_block_the_per_prompt_push_did_not_deliver_is_sent_at_delegation(home, monkeypatch):
    _seen_now()
    home["clock"].advance(minutes=1)
    routing.report(account="Codex", state="open", author=ME)
    # The prompt's block was not delivered: its marker did not move, so delegation sends it.
    out = _run(_call("delegate_task", {"target": {}}), monkeypatch)
    assert "Codex: open" in _ctx(out)


def test_an_unmatched_call_prints_nothing_and_leaves_the_marker(home, monkeypatch):
    before = _marker()
    for call in (_call("Read", {"file_path": "/a"}), _call("Bash", {"command": "ls"})):
        assert _run(call, monkeypatch) == {}
    assert _marker() == before == {}
    # Each call is one timing line, and none names a delegation.
    assert [re.search(r"tool=(\w+) outcome=pass ms=", line).group(1) for line in _log_lines()] == ["Read", "Bash"]
    assert not any(" call=" in line for line in _log_lines())


def test_a_subagents_calls_never_repeat_the_push(home, monkeypatch):
    # No marker at all: a parent call would get the full summary; a subagent's gets nothing.
    for call in (_call("Agent", agent_id="a1", agent_type="probe"),
                 _call("delegate_task", {"target": {}}, agent_id="a1")):
        assert _run(call, monkeypatch) == {}
    assert _marker() == {}
    assert _run(_call("Agent"), monkeypatch) != {}  # the parent still hears it


def test_the_budget_of_a_change_block_is_the_per_prompt_one(home, monkeypatch):
    seen = _seen_now()
    _set(home, routing_prompt_chars=150)
    home["clock"].advance(minutes=1)
    routing.add_hold("keep it for the audit", reason="keep", scope="machine", owner=True, via="app")
    routing.report(account="Claude", state="limited", until="2h", author=ME)
    routing.report(account="Codex", state="open", author=ME)
    lines = _ctx(_run(_call("Agent"), monkeypatch)).splitlines()
    assert lines[0].startswith(CHANGES)
    assert lines[1].startswith("Claude: limit reached")
    assert lines[-1].endswith(f"more changes: `khipu_route` since {seen['version']}")
    assert len(lines) < 5 and sum(len(x) + 1 for x in lines[1:]) <= 150


def test_a_read_past_the_time_budget_sends_nothing_and_keeps_the_marker(home, monkeypatch):
    before = _seen_now()
    home["clock"].advance(minutes=1)
    routing.report(account="Codex", state="open", author=ME)
    _set(home, routing_prompt_budget_ms=25)
    real = routing.prompt_push

    def slow(*a, **k):
        time.sleep(0.4)
        return real(*a, **k)

    monkeypatch.setattr(routing, "prompt_push", slow)
    started = time.monotonic()
    assert _run(_call("Agent"), monkeypatch) == {}
    assert time.monotonic() - started < 0.35
    assert _marker() == before
    assert re.search(r"outcome=delegation call=agent routing=skipped:budget ms=\d+\.\d$", _log_lines()[-1])


def test_the_delegation_switch_turns_only_this_push_off(home, monkeypatch):
    _set(home, routing_push_delegation=False)
    assert _run(_call("Agent"), monkeypatch) == {}
    assert _marker() == {}
    assert routing.push_off("delegation") == "routing_push_delegation off"
    assert routing.push_off("prompt") is None
    _set(home, routing_push_delegation=True)
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "0")
    assert _run(_call("Agent"), monkeypatch) == {}


def test_never_a_deny_whatever_happens(home, monkeypatch):
    calls = [_call("Agent"), _call("Bash", {"command": "claude -p x"}), _call("Read", {"file_path": "/a"}),
             _call("Agent", agent_id="a1"), {"hook_event_name": "SubagentStart", "agent_id": "a1"}]
    for call in calls:
        _never_a_decision(_run(call, monkeypatch))
    monkeypatch.setattr(routing, "prompt_push", lambda *a, **k: 1 / 0)
    _never_a_decision(_run(_call("Agent", session_id="other"), monkeypatch))
    monkeypatch.setattr(routing_delegation, "hook_main", lambda *a, **k: 1 / 0)
    assert _run(_call("Agent"), monkeypatch) == {}


def test_a_subagent_gets_the_full_summary_at_its_start(home, monkeypatch):
    _seen_now()
    before = _marker()
    out = _run({"hook_event_name": "SubagentStart", "session_id": SESSION, "agent_id": "a1",
                "agent_type": "probe"}, monkeypatch)
    _never_a_decision(out)
    assert out["hookSpecificOutput"]["hookEventName"] == "SubagentStart"
    assert _ctx(out).startswith(FULL)
    assert _marker() == before  # the parent never saw it
    _set(home, routing_push_delegation=False)
    assert _run({"hook_event_name": "SubagentStart", "session_id": SESSION, "agent_id": "a2"}, monkeypatch) == {}


@pytest.mark.parametrize("env", [{"GROK_HOOK_EVENT": "pre_tool_use"}, {"GROK_HOOK_NAME": "user:x"},
                                 {"KHIPU_HARNESS": "aegis"}])
def test_it_refuses_inside_aegis(home, monkeypatch, env):
    for call in (_call("Agent"), {"hook_event_name": "SubagentStart", "agent_id": "a1"}):
        assert _run(call, monkeypatch, environ=env) == {}
    assert _marker() == {}


def test_the_launcher_refuses_inside_aegis_by_every_route():
    out = integ._probe_aegis_refusal(shlex.quote(str(LAUNCHER)))
    assert out["ok"], out


@pytest.mark.parametrize("page", ["0", "1"])
def test_the_launcher_delivers_and_never_fails_the_call(tmp_path, page):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": ACCOUNTS, "routing_prompt_budget_ms": 1000,
                                                  "routing_reset_check": False}))
    capture = Path(tempfile.mkdtemp(prefix="krd-", dir="/tmp"))
    # Nothing another test left in this process's environment reaches the launcher.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("KHIPU_", "ROUTING_", "GROK_", "CLAUDE_"))}
    env.update(HOME=str(tmp_path), KHIPU_DATA_DIR=str(data), KHIPU_CAPTURE_HOME=str(capture),
               KHIPU_FEATURE_ROUTING_PUSH="1", KHIPU_PYTHON=sys.executable,
               PYTHONPATH=os.pathsep.join(p for p in sys.path if p),
               # Slice 6: the page (1) or the earlier summary (0) the suite's switch selects.
               KHIPU_ROUTING_PAGE=page)
    try:
        subprocess.run([sys.executable, "-c", "from khipu import routing; routing.report(account='Codex', "
                        "state='limited', until='1h', author={'kind': 'owner'})"], env=env, check=True,
                       capture_output=True, timeout=60)
        for payload, want in ((_call("Agent"), "ROUTING " if page == "1" else FULL), (_call("Read", {"file_path": "/a"}), None),
                              ("not json", None)):
            p = subprocess.run([str(LAUNCHER)], input=payload if isinstance(payload, str) else json.dumps(payload),
                               capture_output=True, text=True, env=env, timeout=60)
            assert p.returncode == 0, p.stderr
            out = json.loads(p.stdout)
            _never_a_decision(out)
            assert (_ctx(out).startswith(want) if want else out == {}), p.stdout
    finally:
        shutil.rmtree(capture, ignore_errors=True)


# ---- the installers --------------------------------------------------------------------------

@pytest.fixture
def fake_home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    patches = [
        mock.patch.object(integ, "HOME", h),
        mock.patch.object(integ, "CLAUDE_JSON", h / ".claude.json"),
        mock.patch.object(integ, "CLAUDE_SETTINGS", h / ".claude" / "settings.json"),
        mock.patch.object(integ, "CODEX_TOML", h / ".codex" / "config.toml"),
        mock.patch.object(integ, "CODEX_HOOKS", h / ".codex" / "hooks.json"),
        mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(h / ".config" / "khipu")}),
    ]
    os.environ.pop("CLAUDE_CONFIG_DIR", None)
    for p in patches:
        p.start()
    yield h
    for p in patches:
        p.stop()


def _ours(entries: list[dict]) -> list[dict]:
    return [e for e in entries if any(integ._is_our_delegation(h.get("command")) for h in e.get("hooks", []))]


def _check_hooks(hooks: dict) -> None:
    pre, start = _ours(hooks["PreToolUse"]), _ours(hooks["SubagentStart"])
    assert len(pre) == 1 and len(start) == 1
    assert pre[0]["matcher"] == integ.DELEGATION_MATCHER
    assert pre[0]["hooks"] == [{"type": "command", "command": integ.delegation_hook(),
                                "timeout": integ.DELEGATION_TIMEOUT}]
    assert "matcher" not in start[0]
    assert start[0]["hooks"] == [{"type": "command", "command": integ.delegation_hook(),
                                  "timeout": integ.SUBAGENT_START_TIMEOUT}]


def test_the_claude_code_installer_adds_both_hooks_once(fake_home):
    (fake_home / ".claude").mkdir()
    other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "/me/guard.sh"}]}
    (fake_home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [other]}}))
    (fake_home / ".claude.json").write_text("{}")
    out = integ.install("claude_code")
    assert any(c.endswith("hooks.PreToolUse += khipu-delegation-hook") for c in out["changes"])
    assert any(c.endswith("hooks.SubagentStart += khipu-delegation-hook") for c in out["changes"])
    hooks = json.loads((fake_home / ".claude" / "settings.json").read_text())["hooks"]
    _check_hooks(hooks)
    assert other in hooks["PreToolUse"]  # someone else's PreToolUse hook is kept as it was
    assert not any("delegation" in c for c in integ.install("claude_code")["changes"])
    assert integ.status("claude_code")["delegation_push"] == "installed"
    integ.uninstall("claude_code")
    hooks = json.loads((fake_home / ".claude" / "settings.json").read_text())["hooks"]
    assert hooks["PreToolUse"] == [other] and hooks["SubagentStart"] == []
    assert integ.status("claude_code")["delegation_push"] == "missing"


def test_the_codex_installer_adds_both_hooks_and_names_the_review_step(fake_home):
    (fake_home / ".codex").mkdir()
    integ.CODEX_TOML.write_text("")
    integ.CODEX_HOOKS.write_text("{}")
    out = integ.install("codex")
    assert out["next_step"] == integ.CODEX_REVIEW_STEP
    _check_hooks(json.loads(integ.CODEX_HOOKS.read_text())["hooks"])
    # Khipu never trusts a hook on the owner's behalf.
    assert "trusted_hash" not in integ.CODEX_TOML.read_text() + integ.CODEX_HOOKS.read_text()
    again = integ.install("codex")
    assert again["changes"] == [] and "next_step" not in again
    # The matcher lets through every delegation tool, Codex's native subagent tool included, and no shell tool.
    for tool in ("Agent", "Task", "spawn_agent", "delegate_task", "mcp__t3__delegate_task"):
        assert re.search(integ.DELEGATION_MATCHER, tool), tool
        assert routing_delegation.might_delegate(_call(tool)), tool
    for tool in ("Bash", "shell", "shell_command", "exec_command", "local_shell", "Shell"):
        assert not re.fullmatch(integ.DELEGATION_MATCHER, tool), tool
        assert not routing_delegation.might_delegate(_call(tool)), tool
    assert integ.status("codex")["delegation_push"] == "installed"
    integ.uninstall("codex")
    hooks = json.loads(integ.CODEX_HOOKS.read_text())["hooks"]
    assert hooks["PreToolUse"] == [] and hooks["SubagentStart"] == []


# The matcher every install wrote before the shell half was removed (2026-11-07).
OLD_MATCHER = "Agent|Task|spawn_agent|Bash|shell|shell_command|exec_command|local_shell|delegate_task"


def _old_install(path: Path, other: dict) -> None:
    """Settings as an older install left them: Khipu's PreToolUse entry on the
    wide matcher that also ran before every shell command, beside someone
    else's hook."""
    old = {"PreToolUse": [other, {"matcher": OLD_MATCHER, "hooks": [
               {"type": "command", "command": integ.delegation_hook(), "timeout": integ.DELEGATION_TIMEOUT}]}],
           "SubagentStart": [{"hooks": [{"type": "command", "command": integ.delegation_hook(),
                                         "timeout": integ.SUBAGENT_START_TIMEOUT}]}]}
    path.write_text(json.dumps({"hooks": old}))


def test_a_claude_code_repair_stops_the_hook_running_before_every_shell_command(fake_home):
    (fake_home / ".claude").mkdir()
    (fake_home / ".claude.json").write_text("{}")
    other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "/me/guard.sh"}]}
    settings = fake_home / ".claude" / "settings.json"
    _old_install(settings, other)
    # Until the repair runs, status says the function needs it, so the app offers Reinstall.
    assert integ.status("claude_code")["delegation_push"] == "missing"
    out = integ.install("claude_code")
    assert any(c.endswith(f"hooks.PreToolUse khipu-delegation-hook matcher -> {integ.DELEGATION_MATCHER}")
               for c in out["changes"])
    hooks = json.loads(settings.read_text())["hooks"]
    _check_hooks(hooks)
    assert other in hooks["PreToolUse"]  # the owner's own Bash hook is untouched
    assert not any(re.fullmatch(e["matcher"], "Bash") for e in _ours(hooks["PreToolUse"]))
    assert integ.status("claude_code")["delegation_push"] == "installed"
    assert not any("delegation" in c for c in integ.install("claude_code")["changes"])


def test_a_codex_repair_stops_the_hook_running_before_every_shell_command(fake_home):
    (fake_home / ".codex").mkdir()
    integ.CODEX_TOML.write_text("")
    other = {"matcher": "shell", "hooks": [{"type": "command", "command": "/me/guard.sh"}]}
    _old_install(integ.CODEX_HOOKS, other)
    assert integ.status("codex")["delegation_push"] == "missing"
    out = integ.install("codex")
    assert out["next_step"] == integ.CODEX_REVIEW_STEP  # Codex runs the narrowed hook once it is reviewed
    hooks = json.loads(integ.CODEX_HOOKS.read_text())["hooks"]
    _check_hooks(hooks)
    assert other in hooks["PreToolUse"]
    for tool in ("shell", "shell_command", "exec_command", "local_shell"):
        assert not any(re.fullmatch(e["matcher"], tool) for e in _ours(hooks["PreToolUse"])), tool
    assert integ.status("codex")["delegation_push"] == "installed"


def test_a_dry_run_reports_both_hooks_and_writes_nothing(fake_home):
    (fake_home / ".claude").mkdir()
    (fake_home / ".claude" / "settings.json").write_text("{}")
    (fake_home / ".claude.json").write_text("{}")
    out = integ.install("claude_code", dry_run=True)
    assert sum("khipu-delegation-hook" in c for c in out["changes"]) == 2
    assert (fake_home / ".claude" / "settings.json").read_text() == "{}"


def test_no_grok_build_or_aegis_pack_gets_the_hook(fake_home):
    # Cursor has it since slice B (postToolUse, never preToolUse); Aegis and Grok Build never do.
    assert "khipu-delegation-hook" not in integ._PACK_LAUNCHERS["aegis"]
    assert "khipu-delegation-hook" not in integ._PACK_LAUNCHERS["grok_build"]
    assert "khipu-delegation-hook" in integ._PACK_LAUNCHERS["cursor"]


# ---- Cursor: postToolUse is its delegation event (slice B) ----------------------------------

def _cursor_call(tool: str = "Task", tool_input: dict | None = None, event: str = "postToolUse", **extra) -> dict:
    """What Cursor's SDK 1.0.35 sends a tool hook: its own tool names and event
    casing, cursor_version, workspace_roots, and session_id beside conversation_id."""
    out = {"hook_event_name": event, "conversation_id": SESSION, "session_id": SESSION,
           "cursor_version": "3.26.13", "workspace_roots": ["/work/project-a"], "cwd": "/work/project-a",
           "tool_name": tool, "tool_input": tool_input if tool_input is not None else {"prompt": "build it"},
           "tool_output": "{}"}
    out.update(extra)
    return out


@pytest.mark.parametrize("tool, tool_input, kind", [
    ("Task", {"prompt": "build it"}, "agent"),
    ("Shell", {"command": "codex exec -m m 'fix it'"}, None),
    ("Shell", {"command": "ls -la"}, None),
    ("Read", {"path": "/x"}, None),
])
def test_cursor_tool_names_are_delegations(tool, tool_input, kind):
    assert routing_delegation.delegation_kind(_cursor_call(tool, tool_input)) == kind


def test_cursor_post_tool_use_delivers_routing_in_cursors_flat_shape(home, monkeypatch):
    out = _run(_cursor_call("Task"), monkeypatch)
    assert set(out) == {"additional_context"}, out  # context alone: no permission, no decision
    assert out["additional_context"].startswith(FULL)
    # The same conversation heard it: a second delegation adds nothing new.
    assert _run(_cursor_call("Task"), monkeypatch) == {}


def test_cursor_post_tool_use_ignores_a_call_that_is_not_a_delegation(home, monkeypatch):
    assert _run(_cursor_call("Shell", {"command": "ls -la"}), monkeypatch) == {}
    assert _run(_cursor_call("Read", {"path": "/x"}), monkeypatch) == {}


def test_a_claude_shaped_post_tool_use_still_does_nothing(home, monkeypatch):
    """Only Cursor's own postToolUse carries a delegation; a payload without
    Cursor's markers keeps the old rule (PreToolUse and SubagentStart only)."""
    claude = _call("Task", hook_event_name="postToolUse")
    assert _run(claude, monkeypatch) == {}


def test_cursor_pre_tool_use_is_still_not_acted_on(home, monkeypatch):
    """No preToolUse entry is installed, and an imported Claude one arrives as
    Cursor's camelCase event, which the hook leaves alone until a real run
    proves it (slice C)."""
    assert _run(_cursor_call("Task", event="preToolUse"), monkeypatch) == {}
