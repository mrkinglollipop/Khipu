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

    monkeypatch.setattr(routing_delegation.sys, "stdin", io.StringIO(json.dumps(payload)))
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
    (_call("Bash", {"command": "claude -p 'build the thing'"}), "claude -p"),
    (_call("Bash", {"command": "CLAUDE_CONFIG_DIR=~/.claude-b claude --model x -p 'go'"}), "claude -p"),
    (_call("Bash", {"command": "cd /work && timeout 600 codex exec -m m 'fix it'"}), "codex exec"),
    (_call("Bash", {"command": "/opt/tools/bin/codex exec --json 'review'"}), "codex exec"),
    (_call("Bash", {"command": "grok -p 'audit this'"}), "grok"),
    (_call("Bash", {"command": "nohup muse exec 'second opinion' &"}), "muse exec"),
    (_call("Bash", {"command": "echo start\nclaude -p 'multi\nline prompt'"}), "claude -p"),
    (_call("Bash", {"command": "bash -lc 'codex exec \"do it\"'"}), "codex exec"),
    (_call("shell", {"command": ["bash", "-lc", "claude -p hi"]}), "claude -p"),
    (_call("exec_command", {"command": ["codex", "exec", "hi"]}), "codex exec"),
])
def test_delegation_calls_are_matched(payload, kind):
    assert routing_delegation.delegation_kind(payload, frozenset()) == kind


@pytest.mark.parametrize("payload", [
    _call("Read", {"file_path": "/work/a.py"}),
    _call("Edit", {"file_path": "/work/a.py"}),
    _call("TaskCreate", {"subject": "x"}),
    _call("Bash", {"command": "git status"}),
    _call("Bash", {"command": "claude --version"}),
    _call("Bash", {"command": "codex login"}),
    _call("Bash", {"command": "muse status"}),
    _call("Bash", {"command": "echo 'claude -p is how jobs start'"}),
    _call("Bash", {"command": "grep -rn grok src/"}),
    _call("Bash", {"command": "python3 run_tests.py"}),
    _call("Bash", {"command": ""}),
    {"hook_event_name": "PreToolUse", "session_id": SESSION},
])
def test_other_calls_are_not_matched(payload):
    assert routing_delegation.delegation_kind(payload, frozenset()) is None


def test_a_configured_dispatch_script_is_matched_by_file_name(home):
    call = _call("Bash", {"command": "python3 ~/scripts/dispatch.py --lane build 'brief'"})
    assert routing_delegation.delegation_kind(call) is None  # none configured
    _set(home, routing_dispatch_scripts=["dispatch.py"])
    assert routing_delegation.delegation_kind(call) == "dispatch script"
    assert routing_delegation.delegation_kind(_call("Bash", {"command": "./dispatch.py go"})) == "dispatch script"
    assert routing_delegation.delegation_kind(_call("Bash", {"command": "python3 other.py"})) is None


def test_the_dispatch_scripts_setting_takes_file_names_only(home):
    routing.set_setting("routing_dispatch_scripts", "dispatch.py, run-job.sh,dispatch.py")
    assert routing.settings()["routing_dispatch_scripts"] == ["dispatch.py", "run-job.sh"]
    routing.set_setting("routing_dispatch_scripts", "[]")
    assert routing.settings()["routing_dispatch_scripts"] == []
    for bad in ("/bin/dispatch.py", "a b", "..", '["x/y"]', json.dumps([f"s{i}.py" for i in range(17)])):
        with pytest.raises(ValueError):
            routing.set_setting("routing_dispatch_scripts", bad)


# ---- what a delegation call hears ------------------------------------------------------------

def test_a_delegation_call_with_no_last_seen_version_gets_the_full_summary(home, monkeypatch):
    out = _run(_call("Agent"), monkeypatch)
    _never_a_decision(out)
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert _ctx(out) == routing.light_status()["text"]
    view = routing.read_view()
    assert _marker() == {"version": view["version"], "epoch": view["epoch"], "full_at": "2026-10-14T15:00:00Z"}
    assert _log_lines()[-1].endswith("delegation session=" + SESSION + " call=agent routing=full")


def test_the_seen_marker_is_shared_with_the_per_prompt_push(home, monkeypatch):
    _seen_now()  # the per-prompt push (or the start) delivered the current view
    out = _run(_call("Bash", {"command": "codex exec 'fix it'"}), monkeypatch)
    assert out == {}  # nothing changed since the conversation last heard
    assert _log_lines()[-1].endswith("call=codex_exec routing=none")
    home["clock"].advance(minutes=10)
    routing.report(account="Claude", state="limited", until="2h", author=ME)
    out = _run(_call("Bash", {"command": "codex exec 'fix it'"}), monkeypatch)
    _never_a_decision(out)
    lines = _ctx(out).splitlines()
    assert lines[0].startswith(CHANGES) and len(lines) == 2 and lines[1].startswith("Claude: limit reached")
    marker = _marker()
    assert marker["version"] == routing.read_view()["version"]
    assert marker["full_at"] == "2026-10-14T15:00:00Z"
    # The per-prompt push reads the same marker: the change is not repeated at the next prompt.
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
    assert not any("delegation" in line for line in _log_lines())


def test_a_subagents_calls_never_repeat_the_push(home, monkeypatch):
    # No marker at all: a parent call would get the full summary; a subagent's gets nothing.
    for call in (_call("Agent", agent_id="a1", agent_type="probe"),
                 _call("Bash", {"command": "claude -p hi"}, agent_id="a1")):
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
    assert _log_lines()[-1].endswith("routing=skipped:budget")


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


def test_the_launcher_delivers_and_never_fails_the_call(tmp_path):
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
               PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    try:
        subprocess.run([sys.executable, "-c", "from khipu import routing; routing.report(account='Codex', "
                        "state='limited', until='1h', author={'kind': 'owner'})"], env=env, check=True,
                       capture_output=True, timeout=60)
        for payload, want in ((_call("Agent"), FULL), (_call("Read", {"file_path": "/a"}), None),
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
    assert integ.status("codex")["delegation_push"] == "installed"
    integ.uninstall("codex")
    hooks = json.loads(integ.CODEX_HOOKS.read_text())["hooks"]
    assert hooks["PreToolUse"] == [] and hooks["SubagentStart"] == []


def test_a_dry_run_reports_both_hooks_and_writes_nothing(fake_home):
    (fake_home / ".claude").mkdir()
    (fake_home / ".claude" / "settings.json").write_text("{}")
    (fake_home / ".claude.json").write_text("{}")
    out = integ.install("claude_code", dry_run=True)
    assert sum("khipu-delegation-hook" in c for c in out["changes"]) == 2
    assert (fake_home / ".claude" / "settings.json").read_text() == "{}"


def test_no_grok_build_or_aegis_pack_gets_the_hook(fake_home):
    assert "khipu-delegation-hook" not in integ._PACK_LAUNCHERS["aegis"]
    assert "khipu-delegation-hook" not in integ._PACK_LAUNCHERS["cursor"]
