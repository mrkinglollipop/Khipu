"""Review leads on the Cursor/Antigravity hooks integration (feat/hooks-a-f):
each test reproduces one lead before its fix.

1. Cursor runs the Claude hook it imports beside its own; the single-flight
   backstop must hold the second run even when that run is Claude-shaped and
   carries Claude's session id.
2. An inherited foreign config variable (CODEX_HOME, CODEX_SANDBOX,
   CURSOR_VERSION) must never turn a running Claude Code turn into another
   harness; a Codex job started from Claude (its transcript or
   CODEX_THREAD_ID) is still Codex.
3. An Antigravity PreInvocation that fires before its USER_INPUT is flushed
   must not drop the turn.

Hermetic: temp HOME and capture home, fixture payloads, no harness config.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

from khipu import hook_shape, owner_turns, recall_prompt, recall_rule

SHIM = Path(__file__).resolve().parents[1] / "bin" / "khipu-prompt-recall"
CLAUDE_ENV = {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_SESSION_ID": "cc-1",
              "CLAUDE_PROJECT_DIR": "/work/a"}
CURSOR_ENV = {"CURSOR_VERSION": "3.26.13", "CURSOR_PROJECT_DIR": "/work/khipu", "CLAUDE_PROJECT_DIR": "/work/khipu"}
CURSOR_START = {"conversation_id": "cur-1", "session_id": "cur-1", "cursor_version": "3.26.13",
                "workspace_roots": ["/work/khipu"], "hook_event_name": "sessionStart", "source": "startup",
                "transcript_path": "/Users/x/.cursor/projects/work-khipu/agent-transcripts/cur-1/cur-1.jsonl"}
# The imported Claude entry's run: Claude's own payload shape, Claude's session id.
IMPORTED_START = {"session_id": "0b7e-claude-uuid", "hook_event_name": "SessionStart", "source": "startup",
                  "cwd": "/work/khipu", "transcript_path": "/Users/x/.claude/projects/-work-khipu/0b7e.jsonl"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("KHIPU_T3_DB", str(tmp_path / "no-t3.sqlite"))
    for name in list(os.environ):
        if name.startswith(("CLAUDE", "CODEX", "CURSOR_", "GROK_")) or name in ("KHIPU_HARNESS",):
            monkeypatch.delenv(name, raising=False)
    owner_turns.write_settings({"enabled": True})
    return tmp_path


def _start(payload, env):
    buf = io.StringIO()
    with mock.patch.object(recall_rule, "_session_start_payload", return_value=payload), \
            mock.patch.object(recall_rule, "session_start_context", return_value="# Khipu memory\nkhipu_search"), \
            mock.patch.object(recall_rule, "_RoutingAtStart") as routing, \
            mock.patch.object(recall_rule.sys, "argv", ["khipu-recall-hook"]), \
            mock.patch.object(recall_prompt, "_log"), \
            mock.patch.dict(os.environ, env):
        routing.return_value.begin.return_value.text.return_value = None
        with redirect_stdout(buf):
            recall_rule.session_start_main()
    return json.loads(buf.getvalue())


def _prompt(payload, shape, environ):
    result = {"context": "block", "hits": [], "reason": "ok", "ms": 1.0}
    with mock.patch.object(recall_prompt, "prior_work_for_prompt", return_value=result), \
            mock.patch.object(recall_prompt, "_RoutingAtPrompt") as routing, \
            mock.patch.object(recall_prompt, "_log"):
        routing.return_value.begin.return_value = routing.return_value
        routing.return_value.collect.return_value = None
        routing.return_value.add_to.side_effect = lambda ctx, routed: (ctx, {})
        return recall_prompt.hook_main(json.dumps(payload), shape=shape, environ=environ)


# ---- lead 1: the imported Claude entry is held ------------------------------------

def test_cursors_imported_claude_session_start_is_held_after_cursors_own(home):
    assert "additional_context" in _start(CURSOR_START, CURSOR_ENV)
    assert _start(IMPORTED_START, CURSOR_ENV) == {}


def test_imported_entry_first_then_cursors_own_is_held(home):
    assert "hookSpecificOutput" in _start(IMPORTED_START, CURSOR_ENV)
    assert _start(CURSOR_START, CURSOR_ENV) == {}


def test_cursors_imported_claude_prompt_is_held_after_cursors_own(home):
    own = {**CURSOR_START, "hook_event_name": "beforeSubmitPrompt", "prompt": "fix the build"}
    imported = {**IMPORTED_START, "hook_event_name": "UserPromptSubmit", "prompt": "fix the build"}
    assert _prompt(own, "cursor", CURSOR_ENV) == {"additional_context": "block"}
    assert _prompt(imported, "claude", CURSOR_ENV) == {}
    # Another prompt is its own call.
    assert _prompt({**imported, "prompt": "and the tests"}, "claude", CURSOR_ENV) != {}


def test_the_pair_window_is_short_and_another_workspace_is_its_own(home):
    assert "additional_context" in _start(CURSOR_START, CURSOR_ENV)
    other = dict(CURSOR_ENV, CURSOR_PROJECT_DIR="/work/other")
    assert "hookSpecificOutput" in _start({**IMPORTED_START, "cwd": "/work/other"}, other)
    later = time.time() + hook_shape.CURSOR_PAIR_S + 1
    with mock.patch.object(hook_shape.time, "time", return_value=later):
        assert "hookSpecificOutput" in _start(IMPORTED_START, CURSOR_ENV)


def test_a_claude_code_session_in_cursors_terminal_is_never_held(home):
    env = dict(CLAUDE_ENV, CURSOR_VERSION="3.26.13", CURSOR_PROJECT_DIR="/work/a")
    first = _start({"session_id": "a", "cwd": "/work/a"}, env)
    second = _start({"session_id": "b", "cwd": "/work/a"}, env)
    assert "hookSpecificOutput" in first and "hookSpecificOutput" in second


def test_the_recall_client_forwards_the_hook_identity_to_the_service(home):
    sent = {"CURSOR_VERSION": "3.26.13", "CURSOR_PROJECT_DIR": "/w", "ROUTING_NOW_OFF": "1"}
    with mock.patch.dict(os.environ, {"CLAUDECODE": "1", "CURSOR_VERSION": "daemon-own"}):
        env = recall_prompt.caller_environ(sent)
        assert env["CURSOR_VERSION"] == "3.26.13" and env["ROUTING_NOW_OFF"] == "1"
        assert env.get("CLAUDECODE") is None          # the service's own identity is not the hook's


# ---- lead 2: inherited foreign config never reclassifies a Claude turn -------------

@pytest.mark.parametrize("inherited", [{"CODEX_HOME": "/Users/x/.codex"}, {"CODEX_SANDBOX": "seatbelt"},
                                       {"CURSOR_VERSION": "3.26.13"}])
def test_a_claude_turn_with_inherited_foreign_config_stays_claude(home, inherited):
    env = {**CLAUDE_ENV, **inherited}
    assert hook_shape.detect({"session_id": "s", "prompt": "hi", "cwd": "/w"}, env) == ("claude_code", "claude")
    start = time.time()
    owner_turns.record({"session_id": "s", "prompt": "hi", "cwd": "/w"}, env=env, now=start,
                       issuer={"name": "claude", "pid": 1, "start": "x"})
    turn = owner_turns.turns("s", now=start + 1)[-1]
    assert (turn["harness"], turn["entrypoint"]) == ("claude_code", "cli")
    assert turn["eligible"] is True
    with mock.patch.dict(os.environ, env):
        assert "hookSpecificOutput" in _start({"session_id": "s2", "cwd": "/w"}, {})


def test_cursors_env_alone_without_cursor_payload_keys_keeps_the_claude_envelope(home):
    env = {"CURSOR_VERSION": "3.26.13", "CLAUDE_PROJECT_DIR": "/work/a"}
    assert hook_shape.detect({}, env)[1] == "claude"
    out = _start({}, env)
    assert "hookSpecificOutput" in out and "additional_context" not in out


def test_a_codex_child_of_claude_stays_codex(home):
    codex_payload = {"session_id": "cx-1", "prompt": "hi", "cwd": "/w",
                     "transcript_path": "/Users/x/.codex/sessions/2026/10/27/rollout-cx-1.jsonl"}
    assert hook_shape.detect(codex_payload, CLAUDE_ENV)[0] == "codex"
    assert hook_shape.detect({"session_id": "cx-1"}, dict(CLAUDE_ENV, CODEX_THREAD_ID="cx-1"))[0] == "codex"
    assert owner_turns.harness_of(dict(CLAUDE_ENV, CODEX_HOME="/Users/x/.codex"), codex_payload) == "codex"


# ---- lead 3: an early PreInvocation never drops the Antigravity turn ----------------

def _transcript(root: Path) -> Path:
    p = root / ".gemini" / "antigravity" / "brain" / "ag-1" / ".system_generated" / "logs" / "transcript.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _pre(tp: Path) -> dict:
    return {"conversationId": "ag-1", "workspacePaths": ["/work/khipu"], "transcriptPath": str(tp),
            "artifactDirectoryPath": str(tp.parents[2]), "invocationNum": 1, "initialNumSteps": 0}


def _shim(payload: dict) -> subprocess.CompletedProcess:
    env = {**os.environ, "KHIPU_RECALL_DAEMON": "0", "KHIPU_PYTHON": sys.executable,
           "KHIPU_ROOT": str(Path(__file__).resolve().parents[3])}
    return subprocess.run(["/bin/sh", str(SHIM)], input=json.dumps(payload), capture_output=True, text=True,
                          env=env, timeout=60)


def test_a_user_input_flushed_just_after_the_hook_starts_is_recorded_by_that_one_call(home, tmp_path):
    tp = _transcript(tmp_path)
    tp.write_text("", encoding="utf-8")

    def flush_late():
        time.sleep(0.15)
        with tp.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "USER_INPUT", "step_index": 1, "content": "ship the hooks"}) + "\n")

    writer = threading.Thread(target=flush_late)
    writer.start()
    try:
        out = owner_turns.antigravity_turn(_pre(tp))
    finally:
        writer.join()
    assert out and out["prompt"] == "ship the hooks"


def test_no_user_input_yet_passes_the_payload_on_and_marks_nothing(home, tmp_path):
    tp = _transcript(tmp_path)
    assert owner_turns.antigravity_turn(_pre(tp), wait=0) is None
    assert not list((owner_turns.folder() / "antigravity").glob("*"))


def test_the_shim_does_not_stop_on_an_unflushed_turn_and_the_next_call_records_it(home, tmp_path):
    tp = _transcript(tmp_path)
    with mock.patch.dict(os.environ, {"KHIPU_AG_FLUSH_WAIT_S": "0"}):
        first = _shim(_pre(tp))
        assert first.returncode == 0, first.stderr
        assert not owner_turns.session_file("ag-1").exists()
        with tp.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "USER_INPUT", "step_index": 1, "content": "late line"}) + "\n")
        assert _shim({**_pre(tp), "invocationNum": 2}).returncode == 0
    rows = owner_turns.session_file("ag-1").read_text().splitlines()
    assert len(rows) == 1


def test_the_clients_identity_list_is_hook_shapes(home):
    import ast

    client = Path(__file__).resolve().parents[1] / "bin" / "khipu-recall-client.py"
    tree = ast.parse(client.read_text(encoding="utf-8"))
    sent = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                and any(getattr(t, "id", None) == "HOOK_IDENTITY" for t in node.targets))
    assert set(sent) == set(hook_shape.IDENTITY_ENV)
