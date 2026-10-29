"""The Antigravity pack (docs/plans/2026-10-27-cursor-antigravity-hooks.md
sections 5.3 and 6, slice E): install, uninstall, status and verify for
``~/.gemini/config`` and a project's ``.agents``, the per-prompt gate, and the
shim end to end. Hermetic: a fake HOME, ``KHIPU_KEYCHAIN=0``, nothing real.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from khipu import integrations as integ
from khipu import hook_shape, owner_turns, recall_rule

SHIM = Path(__file__).resolve().parents[1] / "bin" / "khipu-prompt-recall"
FOREIGN_HOOKS = {
    "other-tool": {"enabled": True, "PreToolUse": [{"matcher": "run_command", "hooks": [
        {"type": "command", "command": "/opt/other/guard.sh", "timeout": 5}]}],
        "SessionStart": [{"type": "command", "command": "/opt/other/start.sh"}]},
}
FOREIGN_MCP = {"mcpServers": {"other": {"command": "/opt/other/mcp", "args": ["--x"]}}}


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(integ, "HOME", h)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(h / ".config" / "khipu"))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    for name in list(os.environ):
        if name.startswith(("CLAUDE", "CODEX", "CURSOR_", "GROK_")) or name in ("KHIPU_HARNESS", "CLAUDECODE"):
            monkeypatch.delenv(name, raising=False)
    return h


def _config(home: Path) -> Path:
    return home / ".gemini" / "config"


def _seed(home: Path, *, foreign: bool = True) -> Path:
    cfg = _config(home)
    cfg.mkdir(parents=True)
    if foreign:
        (cfg / "hooks.json").write_text(json.dumps(FOREIGN_HOOKS, indent=2) + "\n")
        (cfg / "mcp_config.json").write_text(json.dumps(FOREIGN_MCP, indent=2) + "\n")
        (cfg / "rules").mkdir()
        (cfg / "rules" / "mine.md").write_text("---\ntrigger: always_on\n---\nmy own rule\n")
        (home / ".gemini" / "GEMINI.md").write_text("# my notes\n")
    return cfg


def _commands(entry: dict) -> dict[str, list[str]]:
    return {ev: [h["command"] for h in v] for ev, v in entry.items() if ev != "enabled"}


# ---- command matching ----------------------------------------------------------

def test_launcher_match_accepts_a_command_with_its_arguments(home):
    stop = shlex.quote(integ.stop_hook())
    assert integ._is_launcher_command(f"{stop} --event PostInvocation", "khipu-stop-hook", "--event", "PostInvocation")
    assert integ._is_launcher_command(f"{stop} --event Stop", "khipu-stop-hook", "--event", "Stop")
    assert not integ._is_launcher_command(f"{stop} --event Stop", "khipu-stop-hook", "--event", "PostInvocation")
    assert not integ._is_launcher_command(f"{stop} --event Stop --extra", "khipu-stop-hook", "--event", "Stop")
    assert not integ._is_launcher_command("/bin/echo khipu-stop-hook --event Stop", "khipu-stop-hook",
                                          "--event", "Stop")


# ---- install / reinstall / uninstall ------------------------------------------------

def test_install_writes_hooks_mcp_and_rule_and_leaves_foreign_parts(home):
    cfg = _seed(home)
    out = integ.install("antigravity")
    assert out["detected"] and out.get("ok", True) and len(out["changes"]) == 3, out
    hooks = json.loads((cfg / "hooks.json").read_text())
    assert hooks["other-tool"] == FOREIGN_HOOKS["other-tool"]                      # foreign key untouched
    entry = hooks["khipu"]
    assert entry["enabled"] is True and set(entry) == {"enabled", "SessionStart", "PreInvocation",
                                                       "PostInvocation", "Stop"}
    cmds = _commands(entry)
    stop = shlex.quote(integ.stop_hook())
    assert cmds["SessionStart"] == [integ.recall_hook()] and cmds["PreInvocation"] == [integ.prompt_recall_hook()]
    assert cmds["PostInvocation"] == [f"{stop} --event PostInvocation"]
    assert cmds["Stop"] == [f"{stop} --event Stop"]
    assert [shlex.split(c)[0] for c in cmds["Stop"]] == [integ.stop_hook()]
    # PostInvocation fires after every model call, so it never drains: the hook script treats it as queue-only.
    assert "--event PostInvocation" in cmds["PostInvocation"][0]
    assert entry["PreInvocation"][0]["timeout"] == integ.PROMPT_RECALL_TIMEOUT
    # No PreToolUse / PostToolUse of ours, anywhere in the file.
    assert "PreToolUse" not in entry and "PostToolUse" not in entry
    assert json.loads((cfg / "mcp_config.json").read_text())["mcpServers"] == {
        **FOREIGN_MCP["mcpServers"], "khipu": {"command": integ.mcp_launcher()}}
    rule = (cfg / "rules" / "khipu.md").read_text()
    assert rule == recall_rule.antigravity_rule()
    assert "trigger: always_on" in rule and "khipu_route" in rule and "khipu_status" in rule
    assert len(rule.encode()) < 24_000                                            # Antigravity truncates past this
    assert (cfg / "rules" / "mine.md").read_text().endswith("my own rule\n")
    assert (home / ".gemini" / "GEMINI.md").read_text() == "# my notes\n"           # never edited


def test_reinstall_is_a_no_op_and_status_is_truthful(home):
    _seed(home)
    integ.install("antigravity")
    before = {p: p.read_bytes() for p in _config(home).rglob("*") if p.is_file()}
    again = integ.install("antigravity")
    assert again["changes"] == [] and "backups" not in again
    assert {p: p.read_bytes() for p in _config(home).rglob("*") if p.is_file()} == before
    st = integ.status("antigravity")
    assert st["installed"] is True and st["detected"] is True
    assert st["mcp"] and st["hook_sessionstart"] and st["hook_prompt_recall"] and st["hook_postinvocation"]
    assert st["hook_stop"] and st["extract"] == "installed" and st["recall_rule"] == "installed"
    assert st["hook_pretooluse"] is False and st["launcher_ok"] is True
    # Nothing here has been run by Antigravity itself, and status says so.
    assert st["proof"]["start_block"] == "unproven" and st["proof"]["prompt_recall"] == "unproven"


def test_install_backs_up_what_it_rewrites(home):
    cfg = _seed(home)
    out = integ.install("antigravity")
    assert any(b and Path(b).name.startswith("hooks.json.bak-khipu-") for b in out["backups"])
    assert any(b and Path(b).name.startswith("mcp_config.json.bak-khipu-") for b in out["backups"])
    assert json.loads(Path(next(b for b in out["backups"] if "hooks.json" in b)).read_text()) == FOREIGN_HOOKS
    assert (cfg / "hooks.json").is_file()


def test_uninstall_removes_only_khipu_and_foreign_stays_identical(home):
    cfg = _seed(home)
    integ.install("antigravity")
    out = integ.uninstall("antigravity")
    assert len(out["changes"]) == 3, out
    assert json.loads((cfg / "hooks.json").read_text()) == FOREIGN_HOOKS
    assert json.loads((cfg / "mcp_config.json").read_text()) == FOREIGN_MCP
    assert not (cfg / "rules" / "khipu.md").exists()
    assert (cfg / "rules" / "mine.md").is_file() and (home / ".gemini" / "GEMINI.md").is_file()
    assert integ.uninstall("antigravity")["changes"] == []
    st = integ.status("antigravity")
    assert st["installed"] is False and st["mcp"] is False and st["extract"] == "missing"


def test_a_user_added_handler_under_our_key_survives_an_uninstall(home):
    cfg = _seed(home, foreign=False)
    integ.install("antigravity")
    hooks = json.loads((cfg / "hooks.json").read_text())
    hooks["khipu"]["Stop"].append({"type": "command", "command": "/opt/me/extra.sh"})
    (cfg / "hooks.json").write_text(json.dumps(hooks))
    integ.uninstall("antigravity")
    left = json.loads((cfg / "hooks.json").read_text())
    assert left == {"khipu": {"enabled": True, "Stop": [{"type": "command", "command": "/opt/me/extra.sh"}]}}


def test_a_stray_khipu_pretooluse_is_dropped_never_kept(home):
    cfg = _seed(home, foreign=False)
    stray = {"khipu": {"enabled": True, "PreToolUse": [{"matcher": "*", "hooks": [
        {"type": "command", "command": str(integ._shim_dir() / "khipu-delegation-hook"), "timeout": 3}]}]}}
    (cfg / "hooks.json").write_text(json.dumps(stray))
    integ.install("antigravity")
    entry = json.loads((cfg / "hooks.json").read_text())["khipu"]
    assert "PreToolUse" not in entry and "PostToolUse" not in entry


def test_a_khipu_key_that_is_not_ours_is_refused_and_left_alone(home):
    cfg = _seed(home, foreign=False)
    theirs = {"khipu": {"enabled": True, "Stop": [{"type": "command", "command": "/opt/other/khipu-thing.sh"}]}}
    (cfg / "hooks.json").write_text(json.dumps(theirs))
    before = (cfg / "hooks.json").read_bytes()
    out = integ.install("antigravity")
    assert out["ok"] is False and "does not own" in out["error"]
    assert (cfg / "hooks.json").read_bytes() == before and not (cfg / "mcp_config.json").exists()
    assert integ.uninstall("antigravity")["changes"] == []
    assert (cfg / "hooks.json").read_bytes() == before


def test_a_foreign_mcp_khipu_entry_and_a_foreign_rule_file_are_refused(home):
    cfg = _seed(home, foreign=False)
    (cfg / "mcp_config.json").write_text(json.dumps({"mcpServers": {"khipu": {"command": "/opt/else/mcp"}}}))
    out = integ.install("antigravity")
    assert out["ok"] is False and "mcpServers.khipu" in out["error"]
    (cfg / "mcp_config.json").unlink()
    (cfg / "rules").mkdir()
    (cfg / "rules" / "khipu.md").write_text("somebody else's rule\n")
    out = integ.install("antigravity")
    assert out["ok"] is False and "not Khipu-owned" in out["error"]
    assert (cfg / "rules" / "khipu.md").read_text() == "somebody else's rule\n"


def test_unparseable_hooks_json_is_reported_not_overwritten(home):
    cfg = _seed(home, foreign=False)
    (cfg / "hooks.json").write_text("{ not json")
    out = integ.install("antigravity")
    assert out["ok"] is False and out["aborted"] is True
    assert (cfg / "hooks.json").read_text() == "{ not json"


def test_dry_run_writes_nothing(home):
    cfg = _seed(home, foreign=False)
    out = integ.install("antigravity", dry_run=True)
    assert len(out["changes"]) == 3
    assert sorted(p.name for p in cfg.rglob("*")) == []
    assert not (home / ".config" / "khipu" / "bin").exists()


def test_a_mac_without_antigravity_is_reported_and_nothing_is_created(home):
    assert integ.install("antigravity") == {"harness": "antigravity", "detected": False, "changes": []}
    assert not (home / ".gemini").exists()
    st = integ.status("antigravity")
    assert st["detected"] is False and st["installed"] is False


def test_an_antigravity_app_data_dir_alone_counts_as_detected(home):
    (home / ".gemini" / "antigravity").mkdir(parents=True)
    out = integ.install("antigravity")
    assert out["detected"] and (_config(home) / "hooks.json").is_file()


def test_the_global_mcp_symlink_stays_a_symlink(home):
    cfg = _seed(home, foreign=False)
    (cfg / "mcp_config.json").write_text("{}\n")
    link = home / ".gemini" / "antigravity" / "mcp_config.json"
    link.parent.mkdir()
    link.symlink_to(cfg / "mcp_config.json")
    integ.install("antigravity")
    assert link.is_symlink() and json.loads(link.read_text())["mcpServers"]["khipu"]["command"] == integ.mcp_launcher()


# ---- per project (decision 6.1) -----------------------------------------------------

def test_project_install_writes_workspace_files_without_session_start(home, tmp_path):
    _seed(home)
    project = tmp_path / "repo"
    project.mkdir()
    out = integ.install("antigravity", project=str(project))
    assert out["detected"] and out.get("ok", True)
    agents = project / ".agents"
    entry = json.loads((agents / "hooks.json").read_text())["khipu"]
    assert set(entry) == {"enabled", "PreInvocation", "PostInvocation", "Stop"}          # T3 wires no SessionStart
    assert "PreToolUse" not in entry
    assert json.loads((agents / "mcp_config.json").read_text())["mcpServers"]["khipu"] == {
        "command": integ.mcp_launcher()}
    assert (agents / "rules" / "khipu.md").read_text() == recall_rule.antigravity_rule()
    st = integ.status("antigravity", project=str(project))
    assert st["project"]["installed"] is True and st["installed"] is True
    assert integ.install("antigravity", project=str(project))["changes"] == []
    removed = integ.uninstall("antigravity", project=str(project))
    assert len(removed["changes"]) == 3 and not agents.exists()                       # all ours, so the folder goes too
    assert integ.status("antigravity")["installed"] is True                           # global stays


def test_project_only_install_on_a_mac_without_global_antigravity_writes_no_global_files(home, tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    (project / ".agents").mkdir()
    (project / ".agents" / "hooks.json").write_text(json.dumps({"mine": {"enabled": False}}))
    out = integ.install("antigravity", project=str(project))
    assert out["detected"] is False and out["project_only"] is True and len(out["changes"]) == 3
    assert not (home / ".gemini").exists()
    integ.uninstall("antigravity", project=str(project))
    assert json.loads((project / ".agents" / "hooks.json").read_text()) == {"mine": {"enabled": False}}
    assert not (project / ".agents" / "mcp_config.json").exists()


def test_project_must_exist(home, tmp_path):
    out = integ.install("antigravity", project=str(tmp_path / "nope" / "repo"))
    assert out["ok"] is False and "existing folder" in out["error"]


# ---- launcher health and the harness list --------------------------------------------

def test_antigravity_is_a_known_harness_with_its_launchers(home):
    assert "antigravity" in integ.HARNESSES
    assert [s["harness"] for s in integ.status_all()].count("antigravity") == 1
    _seed(home, foreign=False)
    integ.install("antigravity")
    names = [s["name"] for s in integ.status("antigravity")["launchers"]]
    assert names == ["khipu-mcp", "khipu-stop-hook", "khipu-recall-hook", "khipu-prompt-recall"]
    assert integ.launcher_health()["ok"] is True


# ---- the per-prompt gate -----------------------------------------------------------------

def _transcript(tmp_path: Path, conv: str = "ag-1") -> Path:
    p = tmp_path / ".gemini" / "antigravity" / "brain" / conv / ".system_generated" / "logs" / "transcript.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _step(p: Path, kind: str, index: int, content) -> None:
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": kind, "step_index": index, "content": content}) + "\n")


def _pre(tp: Path, conv: str = "ag-1", **extra) -> dict:
    return {"conversationId": conv, "workspacePaths": ["/work/khipu"], "transcriptPath": str(tp),
            "artifactDirectoryPath": str(tp.parents[2]), "invocationNum": 1, "initialNumSteps": 0, **extra}


def test_gate_passes_the_first_call_of_a_turn_and_holds_the_rest(home, tmp_path):
    tp = _transcript(tmp_path)
    _step(tp, "USER_INPUT", 1, "<USER_REQUEST>fix the build</USER_REQUEST>")
    first = owner_turns.antigravity_turn(_pre(tp))
    assert first["prompt"] == "fix the build" and first["cwd"] == "/work/khipu"
    assert first["conversationId"] == "ag-1"
    # More model calls in the same turn (a tool ran, the planner answered): nothing.
    _step(tp, "PLANNER_RESPONSE", 2, "on it")
    _step(tp, "AGENCY_TOOL_CALL", 3, {"name": "run_command"})
    assert owner_turns.antigravity_turn(_pre(tp, invocationNum=2)) == {}
    assert owner_turns.antigravity_turn(_pre(tp, invocationNum=3)) == {}
    # A new USER_INPUT is a new turn, whatever invocationNum says (docs: per turn; ACP: per session).
    _step(tp, "USER_INPUT", 4, "now the tests")
    second = owner_turns.antigravity_turn(_pre(tp, invocationNum=1))
    assert second["prompt"] == "now the tests"
    assert owner_turns.antigravity_turn(_pre(tp, invocationNum=2)) == {}
    # The same step in another conversation is its own turn.
    other = _transcript(tmp_path, "ag-2")
    _step(other, "USER_INPUT", 4, "now the tests")
    assert owner_turns.antigravity_turn(_pre(other, "ag-2"))["prompt"] == "now the tests"


def test_gate_leaves_other_harnesses_and_prompted_payloads_alone(home, tmp_path):
    assert owner_turns.antigravity_turn({"session_id": "cc", "prompt": "hi", "cwd": "/w",
                                         "transcript_path": "/u/.claude/p/s.jsonl"}) is None
    assert owner_turns.antigravity_turn({"conversation_id": "c", "workspace_roots": ["/w"],
                                         "cursor_version": "3.26.13", "prompt": "hi"}) is None
    tp = _transcript(tmp_path)
    assert owner_turns.antigravity_turn(_pre(tp, prompt="typed here")) is None
    assert owner_turns.antigravity_turn("not a dict") is None


def test_gate_with_no_user_turn_yet_says_nothing_and_marks_nothing(home, tmp_path):
    tp = _transcript(tmp_path)
    assert owner_turns.antigravity_turn(_pre(tp), wait=0) is None                      # no transcript file at all
    _step(tp, "PLANNER_RESPONSE", 1, "hello")
    assert owner_turns.antigravity_turn(_pre(tp), wait=0) is None
    _step(tp, "USER_INPUT", 2, "late arrival")                                         # appears on a later invocation
    assert owner_turns.antigravity_turn(_pre(tp))["prompt"] == "late arrival"


def test_gate_falls_back_to_last_user_input_and_reads_only_the_tail(home, tmp_path):
    tp = _transcript(tmp_path)
    missing = tp.parent / "nope.jsonl"
    out = owner_turns.antigravity_turn({**_pre(tp), "transcriptPath": str(missing), "lastUserInput": "from the payload"})
    assert out["prompt"] == "from the payload"
    big = _transcript(tmp_path, "ag-big")
    _step(big, "USER_INPUT", 1, "old turn")
    with big.open("a") as f:
        f.write(json.dumps({"type": "AGENCY_TOOL_CALL", "step_index": 2, "content": "x" * (5 << 20)}) + "\n")
        f.write(json.dumps({"type": "USER_INPUT", "step_index": 3, "content": "newest turn"}) + "\n")
    assert owner_turns.antigravity_turn(_pre(big, "ag-big"))["prompt"] == "newest turn"


def test_trusted_list_does_not_include_antigravity_turns(home):
    # Decision 6.2: record the turns, trust them only after the real run and Matt's word.
    assert not [e for e in owner_turns.DEFAULT_TRUSTED if e.startswith(("antigravity", "cursor"))]
    assert owner_turns.settings()["trusted"] == list(owner_turns.DEFAULT_TRUSTED)


def test_gated_payload_is_recorded_as_an_antigravity_owner_turn(home, tmp_path):
    tp = _transcript(tmp_path)
    _step(tp, "USER_INPUT", 1, "ship it")
    turn = owner_turns.antigravity_turn(_pre(tp))
    owner_turns.record(turn, env={}, issuer={"refused": "test"})
    rows = [json.loads(x) for x in owner_turns.session_file("ag-1").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["harness"] == "antigravity" and rows[0]["entrypoint"] == "app"
    assert "ship it" not in owner_turns.session_file("ag-1").read_text()               # facts, never the words


# ---- the shim, end to end ----------------------------------------------------------------

def _shim(payload: dict) -> subprocess.CompletedProcess:
    env = {**os.environ, "KHIPU_RECALL_DAEMON": "0", "KHIPU_PYTHON": sys.executable,
           "KHIPU_ROOT": str(Path(__file__).resolve().parents[3])}
    return subprocess.run(["/bin/sh", str(SHIM)], input=json.dumps(payload), capture_output=True, text=True,
                          env=env, timeout=60)


def test_the_real_shim_injects_and_records_once_per_user_turn(home, tmp_path):
    tp = _transcript(tmp_path)
    _step(tp, "USER_INPUT", 1, "what did we decide about the khipu hook packs")
    payload = _pre(tp)
    first = _shim(payload)
    assert first.returncode == 0, first.stderr
    out = json.loads(first.stdout)
    assert out == {} or (list(out) == ["injectSteps"] and list(out["injectSteps"][0]) == ["ephemeralMessage"])
    second = _shim({**payload, "invocationNum": 2})
    third = _shim({**payload, "invocationNum": 3})
    assert json.loads(second.stdout) == {} and json.loads(third.stdout) == {}
    rows = [json.loads(x) for x in owner_turns.session_file("ag-1").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["harness"] == "antigravity"                       # one turn, recorded once
    _step(tp, "USER_INPUT", 4, "and the trust prompt?")
    assert _shim(payload).returncode == 0
    assert len(owner_turns.session_file("ag-1").read_text().splitlines()) == 2


def _stop_hook(payload: dict, event: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "KHIPU_PYTHON": sys.executable, "KHIPU_ROOT": str(Path(__file__).resolve().parents[3]),
           "KHIPU_CAPTURE_QUEUE_ONLY": "1"}                                        # no drain in a test
    return subprocess.run(["/bin/sh", str(SHIM.parent / "khipu-stop-hook"), "--event", event],
                          input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=60)


def test_a_jsonl_turn_unflushed_at_pre_invocation_is_recorded_once_by_the_real_stop_hook(home, tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_AG_FLUSH_WAIT_S", "0")
    tp = _transcript(tmp_path)
    payload = _pre(tp)
    assert _shim(payload).returncode == 0                                              # nothing readable yet
    assert not owner_turns.session_file("ag-1").exists()
    _step(tp, "USER_INPUT", 1, "<USER_REQUEST>ship the late turn</USER_REQUEST>")
    _step(tp, "PLANNER_RESPONSE", 2, "on it")
    assert _stop_hook(payload, "PostInvocation").returncode == 0
    assert len(owner_turns.session_file("ag-1").read_text().splitlines()) == 1          # PostInvocation recorded it
    assert _stop_hook(payload, "Stop").returncode == 0
    assert json.loads(_shim({**payload, "invocationNum": 2}).stdout) == {}
    rows = [json.loads(x) for x in owner_turns.session_file("ag-1").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["harness"] == "antigravity" and rows[0]["typed"] is True
    assert "ship the late turn" not in owner_turns.session_file("ag-1").read_text()


def test_a_jsonl_turn_recorded_by_stop_alone_and_left_alone_when_already_recorded(home, tmp_path):
    tp = _transcript(tmp_path)
    _step(tp, "USER_INPUT", 1, "first turn")
    issuer = {"refused": "test"}
    assert owner_turns.antigravity_late_turn(_pre(tp), "Stop", now=50.0, issuer=issuer)
    assert owner_turns.antigravity_late_turn(_pre(tp), "PostInvocation", now=51.0, issuer=issuer) is None
    _step(tp, "USER_INPUT", 2, "second turn")
    assert owner_turns.antigravity_prompt(_pre(tp), now=60.0, wait=0, issuer=issuer)["prompt"] == "second turn"
    assert owner_turns.antigravity_late_turn(_pre(tp), "Stop", now=61.0, issuer=issuer) is None
    rows = [json.loads(x) for x in owner_turns.session_file("ag-1").read_text().splitlines()]
    assert [r["ts"] for r in rows] == [50.0, 60.0]


def _ag_ts() -> list[float]:
    path = owner_turns.session_file("ag-1")
    return [json.loads(x)["ts"] for x in path.read_text().splitlines()] if path.exists() else []


def test_a_jsonl_pending_note_from_one_turn_never_dates_the_next(home, tmp_path):
    issuer = {"refused": "test"}
    tp = _transcript(tmp_path)
    _step(tp, "USER_INPUT", 1, "first")
    assert owner_turns.antigravity_prompt(_pre(tp, initialNumSteps=1), now=1000.0, wait=0, issuer=issuer)
    _step(tp, "PLANNER_RESPONSE", 2, "working")
    assert owner_turns.antigravity_prompt(_pre(tp, invocationNum=2, initialNumSteps=1), now=1010.0, wait=0,
                                          issuer=issuer) == {}
    _step(tp, "USER_INPUT", 3, "second")
    assert owner_turns.antigravity_prompt(_pre(tp, invocationNum=3, initialNumSteps=3), now=1100.0, wait=0,
                                          issuer=issuer)["prompt"] == "second"
    assert _ag_ts() == [1000.0, 1100.0]
    # B's own unflushed call still dates B when B is recorded late.
    assert owner_turns.antigravity_prompt(_pre(tp, invocationNum=4, initialNumSteps=4), now=1200.0, wait=0,
                                          issuer=issuer) == {}
    _step(tp, "USER_INPUT", 4, "third")
    assert owner_turns.antigravity_late_turn(_pre(tp), "PostInvocation", now=1204.0, issuer=issuer)
    assert _ag_ts() == [1000.0, 1100.0, 1200.0]


def test_a_jsonl_turn_recorded_from_last_user_input_is_one_row_after_the_flush(home, tmp_path):
    issuer = {"refused": "test"}
    tp = _transcript(tmp_path)
    tp.write_text("")
    first = owner_turns.antigravity_prompt(_pre(tp, lastUserInput="ship it"), now=1000.0, wait=0, issuer=issuer)
    assert first["prompt"] == "ship it" and _ag_ts() == [1000.0]
    _step(tp, "USER_INPUT", 1, "<USER_REQUEST>ship it</USER_REQUEST>")
    _step(tp, "PLANNER_RESPONSE", 2, "shipped")
    assert owner_turns.antigravity_late_turn(_pre(tp), "PostInvocation", now=1004.0, issuer=issuer) is None
    assert owner_turns.antigravity_late_turn(_pre(tp), "Stop", now=1005.0, issuer=issuer) is None
    assert _ag_ts() == [1000.0]


def test_a_jsonl_last_user_input_alias_never_swallows_a_later_identical_turn(home, tmp_path):
    issuer = {"refused": "test"}
    tp = _transcript(tmp_path)
    tp.write_text("")
    assert owner_turns.antigravity_prompt(_pre(tp, lastUserInput="again"), now=1000.0, wait=0,
                                          issuer=issuer)["prompt"] == "again"
    _step(tp, "USER_INPUT", 1, "<USER_REQUEST>again</USER_REQUEST>")
    _step(tp, "PLANNER_RESPONSE", 2, "done")
    _step(tp, "USER_INPUT", 3, "<USER_REQUEST>again</USER_REQUEST>")
    assert owner_turns.antigravity_late_turn(_pre(tp, initialNumSteps=3), "PostInvocation", now=2000.0, issuer=issuer)
    assert owner_turns.antigravity_late_turn(_pre(tp, initialNumSteps=3), "Stop", now=2001.0, issuer=issuer) is None
    assert _ag_ts() == [1000.0, 2000.0]


# ---- verify -----------------------------------------------------------------------------

def test_prompt_probe_proves_one_injection_per_turn(home):
    out = integ._probe_antigravity_prompt(f"env KHIPU_RECALL_DAEMON=0 KHIPU_PYTHON={shlex.quote(sys.executable)} /bin/sh {shlex.quote(str(SHIM))}")
    assert out["ok"] is True and out["once_per_turn"] is True and out["turns_gated"] == 2, out


def test_native_extract_probe_reads_an_antigravity_transcript_and_queues_one_job(home):
    cmd = f"{shlex.quote(str(SHIM.parent / 'khipu-stop-hook'))} --event Stop"
    out = integ._probe_native_extract(cmd, "antigravity")
    assert out["ok"] is True and out["harness_seen"] == "antigravity" and out["queued"] == 1, out


def test_verify_reports_a_missing_install_and_never_probes_pretooluse(home):
    _seed(home, foreign=False)
    out = integ.verify("antigravity")
    assert out["detected"] and out["ok"] is False
    assert out["components"]["hook"]["error"] == "not installed"
    assert "PreToolUse" not in json.dumps(out)


def test_verify_runs_every_installed_component(home, tmp_path):
    _seed(home, foreign=False)
    integ.install("antigravity")
    project = tmp_path / "repo"
    project.mkdir()
    integ.install("antigravity", project=str(project))
    with mock.patch.object(integ, "_probe_mcp", return_value={"ok": True}), \
            mock.patch.object(integ, "_probe_hook", return_value={"ok": True}), \
            mock.patch.object(integ, "_probe_aegis_refusal", return_value={"ok": True, "ms": 1}), \
            mock.patch.object(integ, "_probe_native_extract", return_value={"ok": True}) as extract, \
            mock.patch.object(integ, "_probe_recall", return_value={"ok": True}) as recall, \
            mock.patch.object(integ, "_probe_antigravity_prompt", return_value={"ok": True}) as prompt, \
            mock.patch.object(integ, "_runtime", return_value={"ok": True}), \
            mock.patch("khipu.probe.run_probe", return_value={"ok": True}):
        out = integ.verify("antigravity", project=str(project))
    assert out["ok"] is True, out
    assert set(out["components"]) == {"mcp", "hook", "extract", "recall", "prompt_recall", "recall_instruction",
                                      "project_files", "recall_probe"}
    assert extract.call_args.args[0].endswith("--event Stop") and extract.call_args.args[1] == "antigravity"
    assert prompt.call_args.args[0] == integ.prompt_recall_hook()
    start_payload = json.loads(recall.call_args.args[1])
    assert hook_shape.detect(start_payload, {})[0] == "antigravity"


def test_recall_probe_reads_antigravity_injectsteps(home):
    steps = {"injectSteps": [{"systemMessage": "use khipu_search first"}]}
    assert integ._injected_context(steps) == "use khipu_search first"
    assert integ._injected_context({"additional_context": "c"}) == "c"
    assert integ._injected_context({"injectSteps": [{"toolCall": {}}]}) == ""
