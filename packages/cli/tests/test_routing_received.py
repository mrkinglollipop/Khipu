"""Hermetic checks for the received-context rig; no installed harness needed."""
import json
from pathlib import Path

import pytest

from tests.acceptance.routing_harness.received import ReceiptError, read, rules_received_in_order
from tests.acceptance.routing_harness.run import Session, TOOL_SENTINEL, OVERSIZED_RULES, Unavailable, environment, execute


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
        rows = [claude("user", [{"type": "text", "text": f"<system-reminder>\n{SENTINEL}\n</system-reminder>"}]),
                claude("assistant", [{"type": "tool_use", "id": "t1", "name": "mcp__khipu__khipu_route",
                                      "input": {"text": "not-received"}}]),
                claude("user", [{"type": "tool_result", "tool_use_id": "t1",
                                 "content": [{"type": "text", "text": '{"routing":"received"}'}]}]),
                claude("assistant", "echo")]
        if format == "messages":
            rows = [row["message"] for row in rows]
            channel = "user"
        else:
            rows[0] = {"type": "attachment", "attachment": {"type": "hook_additional_context",
                       "content": [SENTINEL], "hookName": "SessionStart:startup", "hookEvent": "SessionStart"}}
            channel = "hook"
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
                                     claude("user", SENTINEL), claude("assistant", "consumed")], "claude")
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
        from tests.acceptance.routing_harness.run import Scenario
        return Scenario(None, [], SENTINEL, "child", tmp_path / name)
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
                                              "child_sentinel": "child-hook-sentinel", "pretool_sentinel": "pretool-hook-sentinel",
                                              "tool_sentinel": TOOL_SENTINEL}))
    (root / "exit.json").write_text('{"status":0}')
    parent = [{"type": "session_meta", "payload": {"source": "exec"}},
              codex("message", role="developer", content=[{"type": "input_text", "text": SENTINEL}]),
              codex("function_call", call_id="spawn", name="spawn_agent", arguments="{}"),
              codex("message", role="developer", content=[{"type": "input_text", "text": "pretool-hook-sentinel"}]),
              codex("message", role="assistant", content=[{"type": "output_text", "text": "done"}])]
    child = [{"type": "session_meta", "payload": {"source": {"subagent": {"thread_spawn": {}}}}},
             codex("message", role="user", content=[{"type": "input_text", "text": "environment"}]),
             codex("agent_message", content=[{"type": "encrypted_content", "encrypted_content": "opaque"}]),
             codex("message", role="developer", content=[{"type": "input_text", "text": "child-hook-sentinel"}]),
             codex("custom_tool_call", call_id="r1", name="exec", input="read probe.txt"),
             codex("custom_tool_call_output", call_id="r1", output=[{"type": "input_text", "text": TOOL_SENTINEL}]),
             codex("message", role="assistant", content=[{"type": "output_text", "text": "done"}])]
    unhooked = json.loads(json.dumps(child).replace("child-hook-sentinel", "child-hook-sentinel-NOHOOK"))
    for name, rows in (("parent", parent), ("child", child), ("unhooked", unhooked)):
        (sessions / f"{name}.jsonl").write_text("\n".join(map(json.dumps, rows)))
    def no_launch(*args, **kwargs):
        raise AssertionError("offline replay launched a process")
    monkeypatch.setattr("tests.acceptance.routing_harness.run.execute", no_launch)
    scenario = Session(tmp_path / "unused", replay_root=tmp_path).get("codex")
    assert scenario.children[0].text(channel="tool") == TOOL_SENTINEL
    assert "opaque" not in scenario.children[0].text()
    pretool_only = json.loads(json.dumps(parent).replace(SENTINEL, SENTINEL + "-PRETOOL"))
    (sessions / "parent.jsonl").write_text("\n".join(map(json.dumps, pretool_only)))
    with pytest.raises(ReceiptError, match="known sentinel absent"):
        Session(tmp_path / "pretool-only", replay_root=tmp_path).get("codex")
    (sessions / "parent.jsonl").write_text("\n".join(map(json.dumps, parent)))
    # Tool receipt and even the brief's own sentinel cannot stand in for
    # the independent SubagentStart hook control.
    child = [row for row in child if row.get("payload", {}).get("role") != "developer"]
    child.insert(1, codex("message", role="user", content=[{"type": "input_text", "text": "child-hook-sentinel"}]))
    (sessions / "child.jsonl").write_text("\n".join(map(json.dumps, child)))
    with pytest.raises(ReceiptError, match="hook sentinel absent"):
        Session(tmp_path / "missing-hook", replay_root=tmp_path).get("codex")
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


@pytest.mark.parametrize("content", [SENTINEL, [SENTINEL], [{"type": "text", "text": SENTINEL}]])
def test_claude_native_attachment_calibrates_hook_channel(tmp_path, content):
    received = transcript(tmp_path, [
        {"type": "attachment", "attachment": {"type": "hook_additional_context", "content": content,
                                               "hookName": "SubagentStart", "hookEvent": "SubagentStart"}},
        claude("assistant", "step"),
    ], "claude")
    received.calibrate(SENTINEL, channel="hook")
    assert received.text(before_first=True) == SENTINEL


@pytest.mark.parametrize("event,variable", [("SubagentStart", "ROUTING_RIG_CHILD_SENTINEL"),
                                            ("PreToolUse", "ROUTING_RIG_PRETOOL_SENTINEL")])
def test_hook_control_survives_empty_production_context(tmp_path, monkeypatch, capsys, event, variable):
    import io
    import sys
    from khipu import routing_delegation
    from tests.acceptance.routing_harness.hook import main

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "config.json").write_text("{}")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv(variable, SENTINEL)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"hook_event_name": event})))
    monkeypatch.setattr(routing_delegation, "hook_main", lambda payload: ({}, None, None))
    # The wrapper's connection guards must not escape this hermetic test.
    import socket
    import psycopg
    monkeypatch.setattr(socket.socket, "connect", socket.socket.connect)
    monkeypatch.setattr(socket, "create_connection", socket.create_connection)
    monkeypatch.setattr(psycopg, "connect", psycopg.connect)
    main()
    output = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert output == {"hookEventName": event, "additionalContext": SENTINEL + "\n"}


def test_deferred_rules_reassemble_across_pages_in_order_and_whole():
    rules = OVERSIZED_RULES[1:]
    pages = []
    for number, rule in enumerate(rules):
        pages.extend([f"Rule {number} page 1/3\n" + rule[:1200],
                      f"Rule {number} page 2/3\n" + rule[1200:2400],
                      f"Rule {number} page 3/3\n" + rule[2400:]])
    assert rules_received_in_order(pages, rules)
    assert not rules_received_in_order(pages[3:] + pages[:3], rules)
    assert not rules_received_in_order(pages + pages[:3], rules)
    assert not rules_received_in_order(pages + pages[1:2], rules)
    assert not rules_received_in_order(pages[:1] + pages[2:], rules)
    damaged = list(pages)
    damaged[1] = damaged[1].replace("~", "x", 1)
    assert not rules_received_in_order(damaged, rules)
    bad_header = list(pages)
    bad_header[1] = bad_header[1].replace("page 2/3", "page 1/3")
    assert not rules_received_in_order(bad_header, rules)
    wrong_total = list(pages)
    wrong_total[1] = wrong_total[1].replace("page 2/3", "page 2/4")
    assert not rules_received_in_order(wrong_total, rules)
    assert not rules_received_in_order(["\n".join(pages)], rules)
    assert not rules_received_in_order(["Rule 0 page 1/1\n" + "".join(rules)], rules)
    assert not rules_received_in_order([page.split("\n", 1)[1] for page in pages], rules)
    assert rules_received_in_order(["unrelated result", *pages, "another result"], rules)


def test_run_reservation_is_shared_across_processes_even_on_failure(tmp_path):
    import os
    import subprocess
    import sys

    script = tmp_path / "reserve.py"
    script.write_text(r"""
import sys
from pathlib import Path
from tests.acceptance.routing_harness.run import Session, ReceiptError
root = Path(sys.argv[1])
rig = Session(root, cache_dir=root / 'shared')
def run(name):
    with (root / 'launches').open('a') as out:
        out.write(name + '\n')
    raise ReceiptError('synthetic launch failure')
rig._run = run
try:
    rig.get('codex')
except ReceiptError as exc:
    assert str(exc) == 'synthetic launch failure'
else:
    raise AssertionError('failure not retained')
""")
    processes = [subprocess.Popen([sys.executable, str(script), str(tmp_path)], env=dict(os.environ),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
    for process in processes:
        out, err = process.communicate(timeout=30)
        assert process.returncode == 0, (out, err)
    assert (tmp_path / "launches").read_text().splitlines() == ["codex"]


@pytest.mark.parametrize("path", ["tests", "tests/acceptance", "tests/acceptance/routing_harness"])
def test_opt_in_options_recognised_at_every_collection_entry(path):
    import subprocess
    import sys

    result = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
                             "--run-routing-harness", "--help", path],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "--run-routing-harness" in result.stdout


@pytest.mark.parametrize("missing", ["session_id", "batch", "ack"])
def test_t3_missing_contract_fields_raise_contract_gap_not_key_error(missing):
    from types import SimpleNamespace
    from tests.test_routing_acceptance import ContractGap
    from tests.acceptance.routing_harness.received import Input
    from tests.acceptance.routing_harness.test_received import test_7c_t3_provider_switch_and_child_doctor

    args = [{"session_id": "t3:synthetic"}, {"session_id": "t3:synthetic", "ack": "batch"}]
    bodies = [{"routing_changes": {"batch": "batch"}}, {"delivery": {"acked_through": 1}}]
    if missing == "batch":
        del bodies[0]["routing_changes"]["batch"]
    elif missing == "session_id":
        del args[0]["session_id"]
    else:
        del args[1]["ack"]
    attachments = [SimpleNamespace(inputs=[Input(0, "tool", json.dumps(body), "khipu_route", arg)])
                   for body, arg in zip(bodies, args)]
    scenario = SimpleNamespace(attachments=attachments, children=[object()])
    with pytest.raises(ContractGap):
        test_7c_t3_provider_switch_and_child_doctor(lambda name: scenario)


def test_codex_advisory_in_developer_context_satisfies_future_contract(tmp_path):
    from tests.acceptance.routing_harness.run import Scenario, ADVISORY, ROUTE_OPENING
    from tests.acceptance.routing_harness.test_received import test_7b_codex_missing_brief_advisory

    def received_text(text):
        return transcript(tmp_path, [codex("message", role="developer", content=text),
                                     codex("message", role="assistant", content="done")], "codex")
    parent = received_text(SENTINEL + "\n" + ROUTE_OPENING + "\n" + ADVISORY)
    child = received_text("child-control\n" + ROUTE_OPENING)
    unhooked = received_text("child-control-NOHOOK")
    scenario = Scenario(parent, [child, unhooked], SENTINEL, "child-control", tmp_path)
    test_7b_codex_missing_brief_advisory(lambda name: scenario)


@pytest.mark.parametrize("tool,arguments,armed", [
    ("exec_command", {"cmd": "cat routing-absent-probe.txt"}, True),
    ("spawn_agent", {"message": "Run cat routing-absent-probe.txt before dispatch"}, False),
    ("exec_command", {"cmd": "echo routing-absent-probe.txt"}, False),
    ("exec_command", {"cmd": "cat other.txt", "message": "routing-absent-probe.txt"}, False),
])
def test_parent_tool_control_arms_absent_routing_hook_before_second_child(tmp_path, monkeypatch, capsys, tool, arguments, armed):
    import io
    import sys
    import socket
    import psycopg
    from khipu import routing_delegation
    from tests.acceptance.routing_harness.hook import main

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "config.json").write_text("{}")
    home = tmp_path / ".codex"
    home.mkdir()
    hooks_file = home / "hooks.json"
    hooks_file.write_text(json.dumps({"hooks": {"SubagentStart": [
        {"hooks": [{"type": "command", "command": "python probe.py"}]}]}}))
    for name, value in {"HOME": str(tmp_path), "KHIPU_DATA_DIR": str(tmp_path / "data"),
                        "ROUTING_RIG_PRETOOL_SENTINEL": "parent-control",
                        "ROUTING_RIG_CHILD_SENTINEL": "child-control"}.items():
        monkeypatch.setenv(name, value)
    calls = []
    monkeypatch.setattr(routing_delegation, "hook_main", lambda payload: (calls.append(payload) or {}, None, None))
    monkeypatch.setattr(socket.socket, "connect", socket.socket.connect)
    monkeypatch.setattr(socket, "create_connection", socket.create_connection)
    monkeypatch.setattr(psycopg, "connect", psycopg.connect)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "hook_event_name": "PreToolUse", "tool_name": tool,
        "tool_input": arguments})))
    main()
    pretool = json.loads(capsys.readouterr().out)
    assert pretool["hookSpecificOutput"]["additionalContext"] == "parent-control\n"
    assert (tmp_path / "child-routing-hook-absent").is_file() is armed
    command = json.loads(hooks_file.read_text())["hooks"]["SubagentStart"][0]["hooks"][0]["command"]
    assert command.endswith(" --calibrator-only") is armed
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"hook_event_name":"SubagentStart"}'))
    main()
    child = json.loads(capsys.readouterr().out)
    assert child["hookSpecificOutput"]["additionalContext"] == ("child-control-NOHOOK\n" if armed else "child-control\n")
    assert len(calls) == (1 if armed else 2), "child routing hook control called wrong path"


def test_round2_parent_pretool_token_cannot_calibrate_session_start(tmp_path):
    received = transcript(tmp_path, [codex("message", role="developer", content=SENTINEL + "-PRETOOL"),
                                     codex("message", role="assistant", content="done")], "codex")
    with pytest.raises(ReceiptError, match="known sentinel absent"):
        received.calibrate(SENTINEL, channel="developer")


@pytest.mark.parametrize("hooked_routing", [True, False])
def test_round2_unhooked_child_routing_is_control_failure(tmp_path, hooked_routing):
    from tests.acceptance.routing_harness.run import Scenario, ADVISORY, ROUTE_OPENING
    from tests.acceptance.routing_harness.test_received import test_7b_codex_missing_brief_advisory

    def context(text):
        return transcript(tmp_path, [codex("message", role="developer", content=text),
                                     codex("message", role="assistant", content="done")], "codex")
    scenario = Scenario(context(ROUTE_OPENING + "\n" + ADVISORY),
                        [context("child\n" + (ROUTE_OPENING if hooked_routing else "")),
                         context("child-NOHOOK\n" + ROUTE_OPENING)],
                        SENTINEL, "child", tmp_path)
    with pytest.raises(ReceiptError, match="absent routing hook unexpectedly delivered routing"):
        test_7b_codex_missing_brief_advisory(lambda name: scenario)


@pytest.mark.parametrize("stage", ["metadata", "copy"])
def test_round2_setup_failure_removes_dummy_auth_and_reservation_blocks_retry(tmp_path, monkeypatch, stage):
    from tests.acceptance.routing_harness import run

    auth = tmp_path / "dummy-auth" / "codex"
    auth.mkdir(parents=True)
    (auth / "auth.json").write_bytes(b"dummy-not-a-credential")
    evidence = tmp_path / "evidence"
    rig = Session(evidence, auth_root=auth.parent)
    monkeypatch.setattr(run.shutil, "which", lambda name: "/synthetic/codex")
    monkeypatch.setattr(run, "execute", lambda *a, **k: pytest.fail("must not launch"))
    original = Path.write_text
    def fail_metadata(path, *args, **kwargs):
        if path.name == "run.json":
            assert (evidence / "codex" / ".codex" / "auth.json").is_file()
            raise OSError("synthetic metadata write failure")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "write_text", fail_metadata)
    if stage == "copy":
        def fail_copy(src, dst):
            dst.write(b"dummy-partial-copy")
            raise OSError("synthetic metadata write failure")
        monkeypatch.setattr(run.shutil, "copyfileobj", fail_copy)
    with pytest.raises(OSError, match="synthetic metadata write failure"):
        rig.get("codex")
    assert not (evidence / "codex" / ".codex" / "auth.json").exists()
    with pytest.raises(ReceiptError, match="synthetic metadata write failure"):
        Session(evidence, auth_root=auth.parent).get("codex")


def test_round2_one_t3_transcript_can_preserve_one_ledger():
    from types import SimpleNamespace
    from tests.acceptance.routing_harness.received import Input
    from tests.acceptance.routing_harness.test_received import test_7c_t3_provider_switch_and_child_doctor

    attachment = SimpleNamespace(inputs=[
        Input(0, "tool", json.dumps({"routing_changes": {"batch": "batch"}}), "khipu_route",
              {"session_id": "t3:synthetic"}),
        Input(1, "tool", json.dumps({"delivery": {"acked_through": 1}}), "khipu_route",
              {"session_id": "t3:synthetic", "ack": "batch"}),
        Input(2, "tool", "codex hooks missing", "exec_command", {"cmd": "khipu doctor"}),
    ])
    test_7c_t3_provider_switch_and_child_doctor(
        lambda name: SimpleNamespace(attachments=[attachment], children=[object()]))


def test_round2_unpaged_dump_does_not_prove_deferred_pages():
    assert not rules_received_in_order(["\n".join(OVERSIZED_RULES[1:])], OVERSIZED_RULES[1:])
