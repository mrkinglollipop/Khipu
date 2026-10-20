"""Hermetic checks for the received-context rig; no installed harness needed."""
import json
from pathlib import Path

import pytest

from tests.acceptance.routing_harness.received import ReceiptError, read
from tests.acceptance.routing_harness.run import Session, TOOL_SENTINEL, Unavailable, environment, execute


SENTINEL = "SYNTHETIC-RECEIVED-SENTINEL"


def transcript(tmp_path, rows, format):
    path = tmp_path / "native.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return read(path, format)


def claude(role, content):
    return {"type": role, "message": {"role": role, "content": content}}


def codex(kind, **fields):
    return {"type": "response_item", "payload": {"type": kind, **fields}}


@pytest.mark.parametrize("format", ["claude", "codex", "messages"])
def test_read_received_context_and_mcp_body_not_assistant_echo(tmp_path, format):
    if format == "codex":
        rows = [codex("message", role="developer", content=[{"type": "input_text", "text": SENTINEL}]),
                codex("function_call", call_id="t1", name="mcp__khipu__khipu_route", arguments="{}"),
                codex("function_call_output", call_id="t1", output='{"routing":"received"}'),
                codex("message", role="assistant", content=[{"type": "output_text", "text": "echo"}])]
        channel = "developer"
    else:
        rows = [claude("user", [{"type": "text", "text": f"<system-reminder>{SENTINEL}</system-reminder>"}]),
                claude("assistant", [{"type": "tool_use", "id": "t1", "name": "mcp__khipu__khipu_route",
                                      "input": {"text": "not-received"}}]),
                claude("user", [{"type": "tool_result", "tool_use_id": "t1",
                                 "content": [{"type": "text", "text": '{"routing":"received"}'}]}]),
                claude("assistant", "echo")]
        if format == "messages":
            rows = [row["message"] for row in rows]
        channel = "user"
    received = transcript(tmp_path, rows, format).calibrate(SENTINEL, channel=channel)
    assert received.text(channel="tool", tool="mcp__khipu__khipu_route") == '{"routing":"received"}'
    assert received.text(before_first=True).count(SENTINEL) == 1
    assert "echo" not in received.text() and "not-received" not in received.text()
    assert received.text(channel="tool", before_first=True) == ""


@pytest.mark.parametrize("decoy", ["assistant", "hook_stdout", "tool_arguments", "acp_update", "exec_json"])
def test_calibration_cannot_be_satisfied_by_non_input_channels(tmp_path, decoy):
    rows = [claude("user", "normal prompt")]
    choices = {
        "assistant": claude("assistant", SENTINEL),
        "hook_stdout": {"type": "hook_response", "hookSpecificOutput": {"additionalContext": SENTINEL}},
        "tool_arguments": claude("assistant", [{"type": "tool_use", "id": "t1", "name": "Bash",
                                               "input": {"command": SENTINEL}}]),
        "acp_update": {"params": {"update": {"sessionUpdate": "agent_message_chunk", "content": {"text": SENTINEL}}}},
        "exec_json": {"type": "item.completed", "item": {"type": "agent_message", "text": SENTINEL}},
    }
    rows.extend([choices[decoy], claude("assistant", "done")])
    received = transcript(tmp_path, rows, "claude")
    with pytest.raises(ReceiptError, match="known sentinel absent"):
        received.calibrate(SENTINEL, channel="user")


def test_a_late_sentinel_does_not_prove_first_step_delivery(tmp_path):
    received = transcript(tmp_path, [claude("user", "prompt"), claude("assistant", "step"),
                                     claude("user", SENTINEL)], "claude")
    with pytest.raises(ReceiptError):
        received.calibrate(SENTINEL, channel="user")
    received.calibrate(SENTINEL, channel="user", before_first=False)


@pytest.mark.parametrize("contents", ["", "not-json\n", "[]\n", '{"type":"response_item","payload":null}\n'])
def test_missing_or_malformed_receipts_fail_the_rig(tmp_path, contents):
    path = tmp_path / "native.jsonl"
    path.write_text(contents)
    with pytest.raises(ReceiptError):
        read(path, "codex")


def test_one_scenario_per_harness_including_failures(tmp_path, monkeypatch):
    rig, calls = Session(tmp_path), []
    failure = ReceiptError("synthetic failure")
    def run(name):
        calls.append(name)
        if name == "codex":
            raise failure
        return object()
    monkeypatch.setattr(rig, "_run", run)
    for _ in range(3):
        with pytest.raises(ReceiptError) as error:
            rig.get("codex")
        assert error.value is failure
    assert rig.get("claude") is rig.get("claude")
    assert calls == ["codex", "claude"]


def test_absent_binary_has_a_named_skip_reason_without_launch(tmp_path, monkeypatch):
    monkeypatch.setattr("tests.acceptance.routing_harness.run.shutil.which", lambda name: None)
    with pytest.raises(Unavailable, match="t3: harness binary absent"):
        Session(tmp_path).get("t3")


def test_subprocess_environment_cannot_inherit_live_configuration(tmp_path, monkeypatch):
    for key in ("KHIPU_DATABASE_URL", "ALZY_DATABASE_URL", "XAI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "HTTP_PROXY", "KHIPU_LIVE_TESTS"):
        monkeypatch.setenv(key, "SYNTHETIC-SECRET")
    env = environment(tmp_path)
    assert "SYNTHETIC-SECRET" not in json.dumps(env)
    assert env["KHIPU_KEYCHAIN"] == "0"
    for key in ("HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "GROK_HOME", "KHIPU_DATA_DIR"):
        assert Path(env[key]).is_relative_to(tmp_path)


def test_saved_native_child_calibrates_without_decrypting_its_task(tmp_path, monkeypatch):
    root = tmp_path / "codex"
    sessions = root / ".codex" / "sessions"
    sessions.mkdir(parents=True)
    (root / "run.json").write_text(json.dumps({"session_id": "synthetic", "sentinel": SENTINEL,
                                              "child_sentinel": "encrypted-brief-sentinel",
                                              "tool_sentinel": TOOL_SENTINEL}))
    (root / "exit.json").write_text('{"status":0}')
    parent = [{"type": "session_meta", "payload": {"source": "exec"}},
              codex("message", role="developer", content=[{"type": "input_text", "text": SENTINEL}]),
              codex("message", role="assistant", content=[{"type": "output_text", "text": "done"}])]
    child = [{"type": "session_meta", "payload": {"source": {"subagent": {"thread_spawn": {}}}}},
             codex("message", role="user", content=[{"type": "input_text", "text": "environment"}]),
             codex("agent_message", content=[{"type": "encrypted_content", "encrypted_content": "opaque"}]),
             codex("custom_tool_call", call_id="r1", name="exec", input="read probe.txt"),
             codex("custom_tool_call_output", call_id="r1", output=[{"type": "input_text", "text": TOOL_SENTINEL}]),
             codex("message", role="assistant", content=[{"type": "output_text", "text": "done"}])]
    for name, rows in (("parent", parent), ("child", child)):
        (sessions / f"{name}.jsonl").write_text("\n".join(map(json.dumps, rows)))
    def no_launch(*args, **kwargs):
        raise AssertionError("offline replay launched a process")
    monkeypatch.setattr("tests.acceptance.routing_harness.run.execute", no_launch)
    scenario = Session(tmp_path / "unused", replay_root=tmp_path).get("codex")
    assert scenario.children[0].text(channel="tool") == TOOL_SENTINEL
    assert "opaque" not in scenario.children[0].text()
    (root / "exit.json").write_text('{"status":"timeout"}')
    with pytest.raises(ReceiptError, match="did not exit successfully"):
        Session(tmp_path / "unused2", replay_root=tmp_path).get("codex")


def test_timeout_kills_the_child_process_group_and_records_failure(tmp_path, monkeypatch):
    import signal
    import subprocess

    killed, waits = [], []
    class Process:
        pid = 1234567
        def wait(self, timeout=None):
            waits.append(timeout)
            if timeout is not None:
                raise subprocess.TimeoutExpired("synthetic", timeout)
            return -signal.SIGKILL
    monkeypatch.setattr("tests.acceptance.routing_harness.run.subprocess.Popen", lambda *a, **k: Process())
    monkeypatch.setattr("tests.acceptance.routing_harness.run.os.killpg", lambda *args: killed.append(args))
    with pytest.raises(ReceiptError, match="timed out"):
        execute(["synthetic"], root=tmp_path, env={}, timeout=1)
    assert killed == [(1234567, signal.SIGKILL)] and waits == [1, None]
    assert json.loads((tmp_path / "exit.json").read_text()) == {"status": "timeout"}


def test_tool_result_without_a_subsequent_model_step_cannot_calibrate(tmp_path):
    received = transcript(tmp_path, [claude("user", "prompt"),
                                     claude("assistant", [{"type": "tool_use", "id": "r1", "name": "Read"}]),
                                     claude("user", [{"type": "tool_result", "tool_use_id": "r1", "content": SENTINEL}])], "claude")
    with pytest.raises(ReceiptError, match="known sentinel absent"):
        received.calibrate(SENTINEL, channel="tool", before_first=False)
