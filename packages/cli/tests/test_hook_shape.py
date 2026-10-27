"""khipu.hook_shape: which harness is calling a hook and how it wants its answer
(docs/plans/2026-10-27-cursor-antigravity-hooks.md section 5.1, slice A).
Hermetic: fixture payloads copied from the shapes in that doc's section 3,
a temp capture home, no real harness config.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import time
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

from khipu import hook_shape, owner_turns, recall_prompt, recall_rule, routing_delegation, session_capture

BIN = Path(__file__).resolve().parents[1] / "bin"

CURSOR_COMMON = {
    "conversation_id": "9f1c0000-aaaa-bbbb-cccc-000000000001", "generation_id": "g-1", "model": "composer",
    "cursor_version": "3.26.13", "workspace_roots": ["/work/khipu"], "user_email": "x@example.com",
    "transcript_path": "/Users/x/.cursor/projects/work-khipu/agent-transcripts/s/s.jsonl",
    "session_id": "9f1c0000-aaaa-bbbb-cccc-000000000001",
}
CURSOR_PAYLOADS = {
    "sessionStart": {**CURSOR_COMMON, "hook_event_name": "sessionStart", "source": "startup"},
    "beforeSubmitPrompt": {**CURSOR_COMMON, "hook_event_name": "beforeSubmitPrompt", "prompt": "fix the build"},
    "stop": {**CURSOR_COMMON, "hook_event_name": "stop", "status": "completed"},
    "sessionEnd": {**CURSOR_COMMON, "hook_event_name": "sessionEnd", "reason": "completed"},
    "postToolUse": {**CURSOR_COMMON, "hook_event_name": "postToolUse", "tool_name": "Task", "tool_input": {}},
}
AG_COMMON = {
    "conversationId": "ag-conv-0001", "workspacePaths": ["/work/khipu"], "modelName": "gemini-3.8-flash",
    "transcriptPath": "/Users/x/.gemini/antigravity/brain/ag-conv-0001/.system_generated/logs/transcript.jsonl",
    "artifactDirectoryPath": "/Users/x/.gemini/antigravity/brain/ag-conv-0001",
}
AG_PAYLOADS = {
    "SessionStart": dict(AG_COMMON),
    "PreInvocation": {**AG_COMMON, "invocationNum": 1, "initialNumSteps": 0},
    "PostInvocation": {**AG_COMMON, "invocationNum": 1, "initialNumSteps": 0},
    "Stop": {**AG_COMMON, "executionNum": 1, "terminationReason": "session_end", "fullyIdle": True},
}
AG_ACP = {**AG_COMMON, "transcriptPath": "/Users/x/.t3/userdata/providers/antigravity/ab12/antigravity-acp/brain/"
                                          "ag-conv-0001/.system_generated/logs/transcript.jsonl"}
CLAUDE_PAYLOAD = {"hook_event_name": "UserPromptSubmit", "session_id": "cc-1", "prompt": "hi", "cwd": "/work/a",
                  "transcript_path": "/Users/x/.claude/projects/p/cc-1.jsonl"}
CODEX_PAYLOAD = {"hook_event_name": "UserPromptSubmit", "session_id": "cx-1", "prompt": "hi", "cwd": "/work/a",
                 "transcript_path": "/Users/x/.codex/sessions/2026/10/27/rollout-cx-1.jsonl"}
# What a Claude Code session's shell exports, and so what a job started from it inherits.
CLAUDE_ENV = {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_SESSION_ID": "cc-1",
              "CLAUDE_PROJECT_DIR": "/work/a"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("KHIPU_T3_DB", str(tmp_path / "no-t3.sqlite"))
    for name in list(os.environ):
        if name.startswith(("CLAUDE", "CODEX", "CURSOR_", "GROK_")) or name in ("KHIPU_HARNESS", "CLAUDECODE"):
            monkeypatch.delenv(name, raising=False)
    owner_turns.write_settings({"enabled": True})
    return tmp_path


# ---- detection: harness, shape, session id --------------------------------------

@pytest.mark.parametrize("event", sorted(CURSOR_PAYLOADS))
def test_every_cursor_event_is_cursor_whatever_claude_env_it_carries(home, event):
    payload = CURSOR_PAYLOADS[event]
    # Cursor sets CLAUDE_PROJECT_DIR (and a Claude import runs under it).
    for env in ({}, {"CLAUDE_PROJECT_DIR": "/work/khipu"}, {"CURSOR_VERSION": "3.26.13", "CLAUDE_PROJECT_DIR": "/x"}):
        assert hook_shape.detect(payload, env) == ("cursor", "cursor")
        assert session_capture.infer_harness(payload, env) == "cursor"
        assert owner_turns.harness_of(env, payload) == "cursor"
    assert hook_shape.session_id(payload) == "9f1c0000-aaaa-bbbb-cccc-000000000001"
    assert hook_shape.workspace(payload) == "/work/khipu"
    assert session_capture.session_id(payload) == hook_shape.session_id(payload)
    assert session_capture.session_cwd(payload) == "/work/khipu"
    assert hook_shape.cursor_entrypoint(payload) == "ide"


def test_cursor_older_payload_without_a_version_and_a_cli_version(home):
    older = {"conversation_id": "c-1", "workspace_roots": ["/w"], "hook_event_name": "stop"}
    assert hook_shape.detect(older, {}) == ("cursor", "cursor")
    assert hook_shape.session_id(older) == "c-1"
    assert hook_shape.cursor_entrypoint(older) == "unknown"
    assert hook_shape.cursor_entrypoint({"cursor_version": "2026.10.22-27dcf5f"}) == "cli"


@pytest.mark.parametrize("event", sorted(AG_PAYLOADS))
def test_every_antigravity_event_is_antigravity_whatever_env_it_carries(home, event):
    payload = AG_PAYLOADS[event]
    for env in ({}, CLAUDE_ENV, {"CODEX_THREAD_ID": "x"}):
        assert hook_shape.detect(payload, env) == ("antigravity", "antigravity")
        assert session_capture.infer_harness(payload, env) == "antigravity"
        assert owner_turns.harness_of(env, payload) == "antigravity"
    assert hook_shape.session_id(payload) == "ag-conv-0001"
    assert hook_shape.workspace(payload) == "/work/khipu"
    assert session_capture.session_id(payload) == "ag-conv-0001"
    assert session_capture.session_cwd(payload) == "/work/khipu"


def test_antigravity_surface_comes_from_the_transcripts_app_data_dir(home):
    surface = hook_shape.antigravity_entrypoint
    assert surface(AG_PAYLOADS["Stop"]) == "app"
    assert surface(AG_ACP) == "t3"
    assert hook_shape.detect(AG_ACP, {}) == ("antigravity", "antigravity")
    cli = {**AG_COMMON, "transcriptPath": "/Users/x/.gemini/antigravity-cli/brain/c/.system_generated/logs/t.jsonl"}
    ide = {**AG_COMMON, "transcriptPath": "/Users/x/.gemini/antigravity-ide/brain/c/.system_generated/logs/t.jsonl"}
    assert (surface(cli), surface(ide)) == ("cli", "ide")
    assert surface({"conversationId": "c"}) == "unknown"


def test_antigravity_needs_both_its_markers(home):
    # A bare camelCase id is not enough to call a hook Antigravity's.
    assert hook_shape.detect({"conversationId": "c"}, {})[0] == "unknown"
    assert hook_shape.detect({"sessionId": "s", "cwd": "/w"}, {})[0] == "unknown"


def test_claude_and_codex_keep_their_harness_and_the_claude_shape(home):
    assert hook_shape.detect(CLAUDE_PAYLOAD, CLAUDE_ENV) == ("claude_code", "claude")
    assert hook_shape.detect(CODEX_PAYLOAD, {"CODEX_THREAD_ID": "cx-1"}) == ("codex", "claude")
    assert hook_shape.detect({}, {}) == ("unknown", "claude")
    assert hook_shape.detect({}, {"KHIPU_HARNESS": "Aegis"}) == ("aegis", "claude")
    assert hook_shape.detect({}, {"GROK_HOOK_EVENT": "stop"}) == ("aegis", "claude")
    assert hook_shape.session_id(CLAUDE_PAYLOAD) == "cc-1"
    assert hook_shape.session_id({"sessionId": "a", "session_id": "b"}) == "a"
    assert hook_shape.session_id({}) == ""


# ---- the harness's own signals beat what it inherited ----------------------------

def test_a_codex_job_started_inside_claude_code_is_codex_not_claude(home):
    inherited = dict(CLAUDE_ENV, CODEX_THREAD_ID="cx-1", CODEX_HOME="/Users/x/.codex")
    # By its transcript, and by its own env with no transcript in the payload.
    assert owner_turns.harness_of(inherited, CODEX_PAYLOAD) == "codex"
    assert owner_turns.harness_of(inherited, {"session_id": "cx-1"}) == "codex"
    assert owner_turns.harness_of(dict(CLAUDE_ENV, CODEX_THREAD_ID="cx-1"), {}) == "codex"
    assert session_capture.infer_harness(CODEX_PAYLOAD, inherited) == "codex"
    # The plain Claude session is still Claude.
    assert owner_turns.harness_of(CLAUDE_ENV, CLAUDE_PAYLOAD) == "claude_code"
    assert owner_turns.harness_of(CLAUDE_ENV, {}) == "claude_code"


def test_a_claude_session_started_inside_codex_stays_claude_by_its_transcript(home):
    inherited = dict(CLAUDE_ENV, CODEX_THREAD_ID="cx-1", CODEX_HOME="/Users/x/.codex")
    assert owner_turns.harness_of(inherited, CLAUDE_PAYLOAD) == "claude_code"
    assert session_capture.infer_harness(CLAUDE_PAYLOAD, inherited) == "claude_code"


def test_cursors_env_alone_never_makes_a_cursor_answer(home):
    # Cursor exports CURSOR_VERSION and also CLAUDE_PROJECT_DIR to every hook,
    # the Claude ones it imports included; a real Cursor hook names itself in
    # its payload. Env alone keeps Claude's envelope (review lead 2).
    env = {"CURSOR_VERSION": "3.26.13", "CLAUDE_PROJECT_DIR": "/work/a"}
    assert hook_shape.detect({}, env) == ("claude_code", "claude")
    assert hook_shape.detect({}, {"CURSOR_VERSION": "3.26.13"}) == ("cursor", "claude")
    assert hook_shape.detect(CURSOR_PAYLOADS["stop"], env) == ("cursor", "cursor")
    # A Codex job started from Cursor's shell is Codex.
    assert hook_shape.detect({}, dict(env, CODEX_THREAD_ID="cx-1")) == ("codex", "claude")


def test_a_second_claude_home_is_still_claude_code(home):
    custom = home / "cfg"
    tp = custom / "projects" / "-x" / "s.jsonl"
    payload = {"session_id": "s", "transcript_path": str(tp)}
    assert hook_shape.detect(payload, {"CLAUDE_CONFIG_DIR": str(custom), "HOME": str(home)})[0] == "claude_code"
    assert hook_shape.detect(payload, {"HOME": str(home)})[0] == "unknown"


def test_the_resolved_shape_takes_the_payload_over_an_old_cursor_argument(home):
    resolve = hook_shape.resolve_shape
    assert resolve(CLAUDE_PAYLOAD, {}, ["--cursor"]) == "cursor"      # an install not yet repointed
    assert resolve(CLAUDE_PAYLOAD, {}, []) == "claude"
    assert resolve(AG_PAYLOADS["PreInvocation"], {}, ["--cursor"]) == "antigravity"
    assert resolve(CURSOR_PAYLOADS["sessionStart"], {}, []) == "cursor"


# ---- the answer's envelope --------------------------------------------------------

def test_the_claude_envelope_is_exactly_what_it_was(home):
    assert hook_shape.render("claude", "SessionStart", "ctx") == {
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "ctx"}}
    assert hook_shape.render("claude", "UserPromptSubmit", "ctx") == {
        "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "ctx"}}
    assert hook_shape.render("claude", "PreToolUse", "") == {}
    nested = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "c"}}
    assert hook_shape.reshape(nested, "claude") is nested
    assert hook_shape.reshape({}, "cursor") == {}


def test_cursor_and_antigravity_envelopes(home):
    assert hook_shape.render("cursor", "UserPromptSubmit", "ctx") == {"additional_context": "ctx"}
    assert hook_shape.render("antigravity", "SessionStart", "ctx") == {"injectSteps": [{"systemMessage": "ctx"}]}
    assert hook_shape.render("antigravity", "UserPromptSubmit", "ctx") == {"injectSteps": [{"ephemeralMessage": "ctx"}]}
    nested = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "c"}}
    assert hook_shape.reshape(nested, "cursor") == {"additional_context": "c"}
    assert hook_shape.reshape(nested, "antigravity") == {"injectSteps": [{"ephemeralMessage": "c"}]}
    # A decision without context is not context.
    assert hook_shape.reshape({"decision": "allow"}, "cursor") == {"decision": "allow"}


def test_budgets_per_shape(home):
    assert hook_shape.budget("claude") == 9800 == recall_rule._HOOK_CONTEXT_BUDGET
    assert hook_shape.budget("cursor") == 9800 < 10_000
    assert hook_shape.budget("antigravity") == 9800
    assert hook_shape.utf16_len("a\U0001F600") == 3                # JavaScript counts an emoji as two
    assert recall_rule._utf16_len("a\U0001F600") == 3


# ---- single flight ---------------------------------------------------------------

def test_single_flight_lets_the_first_run_through_and_holds_a_duplicate(home):
    d = home / "sf"
    assert hook_shape.claim("cursor", "s1", "SessionStart", "startup", directory=d, now=1000.0) is True
    assert hook_shape.claim("cursor", "s1", "SessionStart", "startup", directory=d, now=1030.0) is False
    # Another session, event, source, harness: each its own slot.
    assert hook_shape.claim("cursor", "s2", "SessionStart", "startup", directory=d, now=1030.0) is True
    assert hook_shape.claim("cursor", "s1", "UserPromptSubmit", "startup", directory=d, now=1030.0) is True
    assert hook_shape.claim("cursor", "s1", "SessionStart", "resume", directory=d, now=1030.0) is True
    assert hook_shape.claim("claude_code", "s1", "SessionStart", "startup", directory=d, now=1030.0) is True
    # The window is a minute.
    assert hook_shape.claim("cursor", "s1", "SessionStart", "startup", directory=d, now=1061.0) is True
    assert hook_shape.claim("cursor", "s1", "SessionStart", "startup", directory=d, now=1062.0) is False


def test_single_flight_never_loses_a_hook_to_its_own_marker(home):
    blocked = home / "file-not-a-dir"
    blocked.write_text("x")
    assert hook_shape.claim("cursor", "s", "e", directory=blocked / "sub") is True


# ---- session start: the payload decides, Claude is unchanged ---------------------

def _start(payload, shape=None, argv=("khipu-recall-hook",), env=None):
    buf = io.StringIO()
    with mock.patch.object(recall_rule, "_session_start_payload", return_value=payload), \
            mock.patch.object(recall_rule, "session_start_context", return_value="# Khipu memory\nkhipu_search"), \
            mock.patch.object(recall_rule, "_RoutingAtStart") as routing, \
            mock.patch.object(recall_rule.sys, "argv", list(argv)), \
            mock.patch.object(recall_prompt, "_log"), \
            mock.patch.dict(os.environ, env or {}):
        routing.return_value.begin.return_value.text.return_value = None
        with redirect_stdout(buf):
            recall_rule.session_start_main(shape=shape)
    return json.loads(buf.getvalue())


def test_session_start_claude_and_codex_output_is_unchanged(home):
    want = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "# Khipu memory\nkhipu_search"}}
    assert _start(CLAUDE_PAYLOAD) == want
    assert _start(CODEX_PAYLOAD) == want
    assert _start({}) == want
    assert _start(CLAUDE_PAYLOAD, shape="claude") == want


def test_session_start_cursor_payload_gets_flat_context_without_an_argument(home):
    start = CURSOR_PAYLOADS["sessionStart"]
    assert _start(start) == {"additional_context": "# Khipu memory\nkhipu_search"}


def test_session_start_runs_once_for_cursor_when_both_entries_fire(home):
    start = CURSOR_PAYLOADS["sessionStart"]
    first = _start(start)
    second = _start(start)   # the imported Claude entry, a different command string
    assert "additional_context" in first and second == {}
    # A resume is another start, and Claude's own are never held.
    assert "additional_context" in _start({**start, "source": "resume"})
    assert _start(CLAUDE_PAYLOAD) == _start(CLAUDE_PAYLOAD)


def test_session_start_antigravity_gets_a_system_message(home):
    out = _start(AG_PAYLOADS["SessionStart"])
    assert out == {"injectSteps": [{"systemMessage": "# Khipu memory\nkhipu_search"}]}


def test_session_start_old_cursor_install_still_works_by_argument(home):
    out = _start(CLAUDE_PAYLOAD, argv=("khipu-recall-hook", "--cursor"))
    assert out == {"additional_context": "# Khipu memory\nkhipu_search"}


def test_session_start_log_names_the_harness_and_the_session(home, tmp_path):
    seen = []
    buf = io.StringIO()
    with mock.patch.object(recall_rule, "_session_start_payload", return_value=AG_PAYLOADS["SessionStart"]), \
            mock.patch.object(recall_rule, "session_start_context", return_value="m"), \
            mock.patch.object(recall_rule, "_RoutingAtStart") as routing, \
            mock.patch.object(recall_prompt, "_log", side_effect=lambda msg, tag="": seen.append(msg)):
        routing.return_value.begin.return_value.text.return_value = None
        with redirect_stdout(buf):
            recall_rule.session_start_main()
    assert "session=ag-conv-0001 harness=antigravity" in seen[0]


# ---- per-prompt recall -----------------------------------------------------------

def _prompt(payload, shape):
    result = {"context": "block", "hits": [], "reason": "ok", "ms": 1.0}
    with mock.patch.object(recall_prompt, "prior_work_for_prompt", return_value=result), \
            mock.patch.object(recall_prompt, "_RoutingAtPrompt") as routing, \
            mock.patch.object(recall_prompt, "_log"):
        routing.return_value.begin.return_value = routing.return_value
        routing.return_value.collect.return_value = None
        routing.return_value.add_to.side_effect = lambda ctx, routed: (ctx, {})
        return recall_prompt.hook_main(json.dumps(payload), shape=shape)


def test_prompt_recall_envelopes_per_shape_and_claude_unchanged(home):
    assert _prompt(CLAUDE_PAYLOAD, "claude") == {
        "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "block"}}
    assert _prompt({"prompt": "x"}, "cursor") == {"additional_context": "block"}
    assert _prompt({"prompt": "x"}, "antigravity") == {"injectSteps": [{"ephemeralMessage": "block"}]}


def test_prompt_recall_holds_a_duplicate_prompt_for_cursor_only(home):
    one = CURSOR_PAYLOADS["beforeSubmitPrompt"]
    assert _prompt(one, "cursor") == {"additional_context": "block"}
    assert _prompt(one, "cursor") == {}
    # A different prompt in the same session is a different call.
    assert _prompt({**one, "prompt": "something else"}, "cursor") == {"additional_context": "block"}
    # Claude and Codex are never held, even for an identical prompt.
    assert _prompt(CLAUDE_PAYLOAD, "claude") == _prompt(CLAUDE_PAYLOAD, "claude")


def test_prompt_recall_main_detects_the_shape_from_the_payload(home):
    out = []

    def run(payload, argv=("khipu-prompt-recall",)):
        buf = io.StringIO()
        with mock.patch.object(recall_prompt, "hook_main", side_effect=lambda raw, shape, **kw: out.append(shape) or {}), \
                mock.patch.object(recall_prompt.wire, "read_stdin", return_value=json.dumps(payload)), \
                mock.patch.object(recall_prompt.sys, "argv", list(argv)), \
                mock.patch.object(recall_prompt, "finish_delivery"), redirect_stdout(buf):
            recall_prompt.prompt_recall_main()

    run(CLAUDE_PAYLOAD)
    run(CODEX_PAYLOAD)
    run(CURSOR_PAYLOADS["beforeSubmitPrompt"])
    run(AG_PAYLOADS["PreInvocation"])
    run(CLAUDE_PAYLOAD, argv=("khipu-prompt-recall", "--cursor"))
    assert out == ["claude", "claude", "cursor", "antigravity", "cursor"]


# ---- routing at delegation ---------------------------------------------------------

def test_delegation_answer_is_reshaped_for_cursor_and_untouched_for_claude(home):
    nested = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "routing news"}}

    def run(payload):
        buf = io.StringIO()
        with mock.patch.object(routing_delegation, "hook_main", return_value=(nested, None, None)), \
                mock.patch.object(routing_delegation.wire, "read_stdin", return_value=json.dumps(payload)), \
                redirect_stdout(buf):
            routing_delegation.main()
        return json.loads(buf.getvalue())

    assert run({"session_id": "cc-1", "tool_name": "Task", "transcript_path": "/u/.claude/p/s.jsonl"}) == nested
    assert run({"session_id": "cx-1", "tool_name": "spawn_agent", "transcript_path": "/u/.codex/s.jsonl"}) == nested
    assert run({**CURSOR_COMMON, "tool_name": "Task"}) == {"additional_context": "routing news"}


# ---- the stdlib recall client --------------------------------------------------------

def _client():
    spec = importlib.util.spec_from_file_location("khipu_recall_client_t", BIN / "khipu-recall-client.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_recall_client_sends_the_payloads_shape(home):
    client = _client()
    shape = client.output_shape
    assert shape(json.dumps(CLAUDE_PAYLOAD), ["c", "claude"]) == "claude"
    assert shape(json.dumps(CURSOR_PAYLOADS["beforeSubmitPrompt"]), ["c", "claude"]) == "cursor"
    assert shape(json.dumps(AG_PAYLOADS["PreInvocation"]), ["c", "claude"]) == "antigravity"
    assert shape(json.dumps(CLAUDE_PAYLOAD), ["c", "cursor"]) == "cursor"        # an old install's hint
    assert shape("not json", ["c", "cursor"]) == "cursor"
    assert shape("not json", ["c", "claude"]) == "claude"
    answer = json.dumps({"ok": True, "output": {"injectSteps": [{"ephemeralMessage": "x"}]}}).encode()
    assert client.as_hook_output(answer) == {"injectSteps": [{"ephemeralMessage": "x"}]}


def test_the_client_has_no_package_dependency(home):
    text = (Path(hook_shape.__file__)).read_text()
    # hook_shape is loaded by file path under python -S -E -B: stdlib at import time.
    top = [line for line in text.splitlines() if line.startswith(("import ", "from "))]
    assert not [line for line in top if "khipu" in line]


# ---- capture lists and the heartbeat ---------------------------------------------------

def test_antigravity_is_a_capture_harness_and_an_unused_one_is_not_red(home):
    assert "antigravity" in session_capture.HARNESSES
    assert session_capture.liveness("antigravity")["ok"] is True
    assert session_capture.liveness_all()["ok"] is True
    assert session_capture.unknown_harness_heartbeats() == {"warnings": []}
    assert session_capture.canonical_session_ref("ag-1", "antigravity") == "antigravity:ag-1"
    assert session_capture.canonical_session_ref("antigravity:ag-1", "antigravity") == "antigravity:ag-1"


# ---- owner turns: both harnesses record, neither is trusted yet --------------------------

def test_a_cursor_prompt_is_recorded_under_cursor_and_not_trusted(home):
    start = time.time()
    payload = CURSOR_PAYLOADS["beforeSubmitPrompt"]
    assert owner_turns.record(payload, env={"CLAUDE_PROJECT_DIR": "/work/khipu"}, now=start,
                              issuer={"name": "Cursor", "pid": 1, "start": "x"}) is not None
    turn = owner_turns.turns(hook_shape.session_id(payload), now=start + 1)[-1]
    assert (turn["harness"], turn["entrypoint"], turn["session"]) == ("cursor", "ide", payload["session_id"])
    assert turn["eligible"] is False       # decision 6.2: recorded now, trusted only on Matt's word
    assert "cursor:ide" not in owner_turns.DEFAULT_TRUSTED


def test_an_antigravity_prompt_is_recorded_under_antigravity_and_not_trusted(home):
    start = time.time()
    payload = {**AG_ACP, "prompt": "pause Grok please"}
    assert owner_turns.record(payload, env={}, now=start, issuer={"name": "agy", "pid": 1, "start": "x"}) is not None
    turn = owner_turns.turns("ag-conv-0001", now=start + 1)[-1]
    assert (turn["harness"], turn["entrypoint"], turn["cwd"]) == ("antigravity", "t3", "")
    assert turn["eligible"] is False
    assert not [t for t in owner_turns.DEFAULT_TRUSTED if t.startswith(("cursor", "antigravity"))]


def test_a_codex_job_launched_from_claude_code_is_recorded_as_codex(home):
    start = time.time()
    inherited = dict(CLAUDE_ENV, CODEX_THREAD_ID="cx-1", CODEX_HOME="/Users/x/.codex")
    owner_turns.record(CODEX_PAYLOAD, env=inherited, now=start, issuer={"name": "codex", "pid": 1, "start": "x"})
    turn = owner_turns.turns("cx-1", now=start + 1)[-1]
    assert turn["harness"] == "codex"
    # The same turn without the fix would have read claude_code and a Claude entrypoint ("cli").
    assert turn["entrypoint"] != "cli"
