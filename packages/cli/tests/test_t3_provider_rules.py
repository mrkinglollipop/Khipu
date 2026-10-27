"""Slice F (docs/plans/2026-10-27-cursor-antigravity-hooks.md): per-function
T3 provider states for Cursor and Antigravity, Khipu's per-project
Antigravity files, the warnings and notes, and the child advisory.

Fake HOME and fake ~/.t3 only; nothing here touches a real harness home.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest import mock

import pytest

from khipu import cli, integrations as integ, routing_delegation, t3, t3_antigravity as ag

SECRET = "SYNTHETIC-T3-SETTINGS-SENTINEL"
BRIEF = "Build the synthetic routing fixture carefully and report back."


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KHIPU_T3_DB", str(tmp_path / "missing.sqlite"))
    with mock.patch.object(integ, "HOME", tmp_path):
        yield tmp_path


def _settings(root: Path, instances: dict) -> None:
    path = root / ".t3" / "userdata" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"providerInstances": instances}), encoding="utf-8")


def _cursor(root: Path, *, hooks=None, mcp=True) -> None:
    folder = root / ".cursor"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "hooks.json").write_text(json.dumps({"version": 1, "hooks": hooks or {}}), encoding="utf-8")
    if mcp:
        (folder / "mcp.json").write_text(
            json.dumps({"mcpServers": {"khipu": {"command": "/x/.config/khipu/bin/khipu-mcp"}}}), encoding="utf-8")


def _entry(command: str) -> list[dict]:
    return [{"command": command, "timeout": 3}]


# Today's shipped Cursor pack, before slice B: stop events, sessionStart --cursor.
TODAY = {
    "stop": _entry("/x/khipu-stop-hook"), "preCompact": _entry("/x/khipu-stop-hook"),
    "sessionEnd": _entry("/x/khipu-stop-hook"), "subagentStop": _entry("/x/khipu-stop-hook"),
    "sessionStart": _entry("/x/khipu-recall-hook --cursor"),
}
FULL = dict(TODAY, sessionStart=_entry("/x/khipu-recall-hook"),
            beforeSubmitPrompt=_entry("/x/khipu-prompt-recall"),
            postToolUse=[{"command": "/x/khipu-delegation-hook", "matcher": "Task|Shell"}])


def _health(root: Path) -> dict:
    out = t3.health(root)
    assert out is not None
    return out


def _warnings(out: dict) -> list[str]:
    """The provider warnings: the fake T3 here has no thread database, which
    adds a lookup warning these tests are not about."""
    return [w for w in out["warnings"] if w.startswith("T3 provider")]


def _by_id(out: dict) -> dict:
    return {p["id"]: p for p in out["providers"]}


def _tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


CURSOR_INSTANCE = {"cursor": {"driver": "cursor", "displayName": "Cursor", "enabled": True, "apiKey": SECRET,
                              "config": {"apiToken": SECRET}}}
AG_INSTANCE = {"antigravity": {"driver": "antigravity", "displayName": "Antigravity", "enabled": True,
                               "config": {"oauth": SECRET}}}


# ---- Cursor -----------------------------------------------------------------


def test_cursor_full_install_has_every_function_and_no_warning(home):
    _settings(home, CURSOR_INSTANCE)
    _cursor(home, hooks=FULL)
    out = _health(home)
    cursor = _by_id(out)["cursor"]
    assert cursor["functions"] == {f: "installed" for f in t3.FUNCTIONS}
    assert cursor["hooks"] == "installed"
    assert _warnings(out) == [] and out["notes"] == []
    assert SECRET not in json.dumps(out)


def test_cursor_today_pack_is_partial_and_raises_no_warning(home):
    # The shipped pack has no beforeSubmitPrompt or delegation hook yet: shown
    # per function, not raised as a doctor warning the owner cannot clear.
    _settings(home, CURSOR_INSTANCE)
    _cursor(home, hooks=TODAY)
    out = _health(home)
    cursor = _by_id(out)["cursor"]
    assert cursor["functions"]["capture"] == cursor["functions"]["start_block"] == "installed"
    assert cursor["functions"]["prompt_recall"] == cursor["functions"]["delegation"] == "missing"
    assert cursor["hooks"] == "partial"
    assert _warnings(out) == []


def test_cursor_with_nothing_keeps_the_existing_warning_word_for_word(home):
    _settings(home, CURSOR_INSTANCE)
    out = _health(home)
    assert _by_id(out)["cursor"]["hooks"] == "missing"
    assert _warnings(out) == ["T3 provider Cursor has no Khipu hooks, so sessions on it get no Khipu routing start block"]


def test_cursor_missing_core_function_is_named(home):
    _settings(home, CURSOR_INSTANCE)
    _cursor(home, hooks={"stop": _entry("/x/khipu-stop-hook")}, mcp=False)
    warnings = _warnings(_health(home))
    assert warnings == ["T3 provider Cursor is missing Khipu routing start block and memory tools"]


def test_cursor_foreign_hooks_that_mention_khipu_do_not_count(home):
    _settings(home, CURSOR_INSTANCE)
    _cursor(home, hooks={"stop": _entry("echo /opt/khipu/bin/khipu-stop-hook"),
                         "sessionStart": _entry("session_start.sh khipu-recall-hook")}, mcp=False)
    assert _by_id(_health(home))["cursor"]["hooks"] == "missing"


def test_cursor_event_names_are_matched_per_function(home):
    # A stop hook under sessionStart is not capture; a delegation hook under stop is not delegation.
    _settings(home, CURSOR_INSTANCE)
    _cursor(home, hooks={"sessionStart": _entry("/x/khipu-stop-hook"), "stop": _entry("/x/khipu-delegation-hook")},
            mcp=False)
    functions = _by_id(_health(home))["cursor"]["functions"]
    assert functions["capture"] == functions["start_block"] == functions["delegation"] == "missing"


# ---- Antigravity: states ----------------------------------------------------


def test_antigravity_without_any_project_is_missing_with_the_limits_stated(home):
    _settings(home, AG_INSTANCE)
    out = _health(home)
    provider = _by_id(out)["antigravity"]
    assert provider["functions"] == {"capture": "missing", "start_block": "unsupported",
                                     "prompt_recall": "unsupported", "delegation": "unsupported", "mcp": "missing"}
    assert provider["hooks"] == "missing"
    assert provider["projects"] == {"registered": 0, "installed": 0}
    assert len(_warnings(out)) == 1 and "Antigravity has no Khipu hooks" in _warnings(out)[0]
    assert not any("terminal" in w.lower() for w in out["warnings"])
    assert any("without Khipu's start block" in n and "khipu_route" in n for n in out["notes"])
    assert SECRET not in json.dumps(out)


def test_antigravity_unsupported_is_never_a_warning_once_the_project_is_added(home):
    _settings(home, AG_INSTANCE)
    project = home / "proj"
    project.mkdir()
    assert ag.install_project(str(project), home=home)["ok"]
    out = _health(home)
    provider = _by_id(out)["antigravity"]
    assert provider["functions"]["capture"] == provider["functions"]["mcp"] == "installed"
    assert provider["functions"]["start_block"] == "unsupported"
    assert provider["hooks"] == "installed"
    assert provider["projects"] == {"registered": 1, "installed": 1}
    assert _warnings(out) == []
    assert len(out["notes"]) == 1  # the start-block limit stays stated


def test_antigravity_removing_the_project_files_turns_it_back_to_missing(home):
    _settings(home, AG_INSTANCE)
    project = home / "proj"
    project.mkdir()
    ag.install_project(str(project), home=home)
    (project / ".agents" / "mcp_config.json").unlink()
    provider = _by_id(_health(home))["antigravity"]
    assert provider["functions"]["capture"] == "installed" and provider["functions"]["mcp"] == "missing"
    assert provider["hooks"] == "partial"
    assert "missing Khipu memory tools" in _warnings(_health(home))[0]


def test_antigravity_profile_files_count_when_present_and_are_read_only(home):
    # A future T3 setting (or the owner) may put Khipu in the private profile; Khipu reads it, never writes it.
    _settings(home, AG_INSTANCE)
    profile = home / ".t3" / "userdata" / "providers" / "antigravity" / hashlib.sha256(b"antigravity").hexdigest()
    (profile / "config").mkdir(parents=True)
    (profile / "config" / "hooks.json").write_text(json.dumps(
        {"khipu": {"enabled": True, "Stop": [{"type": "command", "command": "/x/khipu-stop-hook"}]}}), encoding="utf-8")
    (profile / "config" / "mcp_config.json").write_text(
        json.dumps({"mcpServers": {"khipu": {"command": "/x/khipu-mcp"}}}), encoding="utf-8")
    before = _tree(home / ".t3")
    functions = _by_id(_health(home))["antigravity"]["functions"]
    assert functions["capture"] == functions["mcp"] == "installed"
    assert _tree(home / ".t3") == before
    assert ag.profile_dir("antigravity", home) == profile


def test_antigravity_hook_set_switched_off_does_not_count(home):
    _settings(home, AG_INSTANCE)
    project = home / "proj"
    project.mkdir()
    ag.install_project(str(project), home=home)
    hooks = json.loads((project / ".agents" / "hooks.json").read_text())
    hooks["khipu"]["enabled"] = False
    (project / ".agents" / "hooks.json").write_text(json.dumps(hooks))
    assert _by_id(_health(home))["antigravity"]["functions"]["capture"] == "missing"


def test_runtime_version_is_recorded_and_a_new_one_asks_for_a_recheck(home):
    _settings(home, AG_INSTANCE)
    tools = home / ".t3" / "tools" / "antigravity-acp" / "darwin-arm64" / "versions"
    (tools / "1.3.0").mkdir(parents=True)
    project = home / "proj"
    project.mkdir()
    ag.install_project(str(project), home=home)
    runtime = _by_id(_health(home))["antigravity"]["runtime"]
    assert runtime == {"version": "1.3.0", "checked_version": "1.3.0", "recheck": False}
    (tools / "1.4.0").mkdir()
    (tools / "1.10.0").mkdir()
    out = _health(home)
    runtime = _by_id(out)["antigravity"]["runtime"]
    assert runtime["version"] == "1.10.0" and runtime["checked_version"] == "1.3.0" and runtime["recheck"] is True
    assert any("runtime (1.10.0)" in n for n in out["notes"])
    # Installing again is the re-check: it records the version Khipu saw.
    ag.install_project(str(project), home=home)
    runtime = _by_id(_health(home))["antigravity"]["runtime"]
    assert runtime["checked_version"] == "1.10.0" and runtime["recheck"] is False


# ---- Antigravity: per-project files ------------------------------------------


def test_install_writes_the_three_files_and_never_pre_or_post_tool_use(home):
    project = home / "proj"
    project.mkdir()
    out = ag.install_project(str(project), home=home)
    assert out["ok"] and len(out["changes"]) == 3
    assert any("Trust" in n for n in out["notes"])
    hooks = json.loads((project / ".agents" / "hooks.json").read_text())
    assert set(hooks) == {"khipu"} and hooks["khipu"]["enabled"] is True
    events = {k for k in hooks["khipu"] if k != "enabled"}
    # The workspace set: no SessionStart (T3's runtime has none; standalone
    # Antigravity gets it from the global file), never a tool event.
    assert events == {"PreInvocation", "PostInvocation", "Stop"}
    assert "PreToolUse" not in hooks["khipu"] and "PostToolUse" not in hooks["khipu"]
    for event in events:
        for handler in hooks["khipu"][event]:
            assert handler["type"] == "command" and t3._khipu_hook_name(handler["command"])
    for event in ("PostInvocation", "Stop"):
        assert hooks["khipu"][event][0]["command"].endswith(f"khipu-stop-hook --event {event}")
    assert json.loads((project / ".agents" / "mcp_config.json").read_text())["mcpServers"]["khipu"]["command"]
    rule = (project / ".agents" / "rules" / "khipu.md").read_text()
    assert rule.startswith("---\n") and "trigger: always_on\n---" in rule and "khipu_route" in rule
    assert len(rule.encode()) < 24_000  # Antigravity truncates larger rule files
    registry = json.loads(ag.registry_path(home).read_text())
    assert list(registry["projects"]) == [str(project.resolve())]


def test_install_twice_is_a_no_op_and_dry_run_writes_nothing(home):
    project = home / "proj"
    project.mkdir()
    dry = ag.install_project(str(project), dry_run=True, home=home)
    assert dry["ok"] and dry["changes"] and not (project / ".agents").exists()
    assert not ag.registry_path(home).exists()
    ag.install_project(str(project), home=home)
    before = _tree(project)
    again = ag.install_project(str(project), home=home)
    assert again["changes"] == [] and _tree(project) == before


def test_install_and_uninstall_leave_foreign_content_byte_identical(home):
    project = home / "proj"
    (project / ".agents" / "rules").mkdir(parents=True)
    foreign_hooks = {"other-tool": {"enabled": True, "Stop": [{"type": "command", "command": "other-stop.sh"}]}}
    foreign_mcp = {"mcpServers": {"other": {"command": "other-mcp"}}}
    (project / ".agents" / "hooks.json").write_text(json.dumps(foreign_hooks), encoding="utf-8")
    (project / ".agents" / "mcp_config.json").write_text(json.dumps(foreign_mcp), encoding="utf-8")
    (project / ".agents" / "rules" / "mine.md").write_text("my rule\n", encoding="utf-8")
    ag.install_project(str(project), home=home)
    hooks = json.loads((project / ".agents" / "hooks.json").read_text())
    assert hooks["other-tool"] == foreign_hooks["other-tool"] and "khipu" in hooks
    assert json.loads((project / ".agents" / "mcp_config.json").read_text())["mcpServers"]["other"] == {"command": "other-mcp"}

    out = ag.uninstall_project(str(project), home=home)
    assert out["ok"] and not out["status"]["installed"]
    assert json.loads((project / ".agents" / "hooks.json").read_text()) == foreign_hooks
    assert json.loads((project / ".agents" / "mcp_config.json").read_text()) == foreign_mcp
    assert (project / ".agents" / "rules" / "mine.md").read_text() == "my rule\n"
    assert not (project / ".agents" / "rules" / "khipu.md").exists()
    assert ag.registered_projects(home) == []


def test_uninstall_removes_files_khipu_created_and_leaves_no_backups_in_the_repo(home):
    project = home / "proj"
    project.mkdir()
    ag.install_project(str(project), home=home)
    ag.uninstall_project(str(project), home=home)
    assert not (project / ".agents").exists()


def test_uninstall_leaves_a_rule_file_khipu_did_not_write(home):
    project = home / "proj"
    (project / ".agents" / "rules").mkdir(parents=True)
    (project / ".agents" / "rules" / "khipu.md").write_text("the owner's own khipu.md\n", encoding="utf-8")
    out = ag.uninstall_project(str(project), home=home)
    assert out["changes"] == []
    assert (project / ".agents" / "rules" / "khipu.md").read_text() == "the owner's own khipu.md\n"


def test_install_refuses_t3s_private_state_and_other_harness_homes(home):
    profile = home / ".t3" / "userdata" / "providers" / "antigravity" / "abc"
    profile.mkdir(parents=True)
    for private in (profile, home / ".gemini", home / ".cursor"):
        private.mkdir(parents=True, exist_ok=True)
        before = _tree(home)
        out = ag.install_project(str(private), home=home)
        assert out["ok"] is False and "not a project" in out["error"]
        assert integ.install("antigravity", project=str(private))["ok"] is False
        assert _tree(home) == before
    assert ag.install_project(str(home / "nope"), home=home)["ok"] is False


def test_unreadable_hooks_file_is_reported_and_left_alone(home, capsys):
    project = home / "proj"
    (project / ".agents").mkdir(parents=True)
    (project / ".agents" / "hooks.json").write_text("{not json", encoding="utf-8")
    assert cli.main(["t3", "antigravity", "add", "--project", str(project)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["aborted"] is True
    assert (project / ".agents" / "hooks.json").read_text() == "{not json"
    assert not (project / ".agents" / "mcp_config.json").exists()


def test_cli_add_status_list_remove_round_trip(home, capsys):
    project = home / "proj"
    project.mkdir()
    assert cli.main(["t3", "antigravity", "add", "--project", str(project)]) == 0
    assert json.loads(capsys.readouterr().out)["status"]["installed"] is True
    assert cli.main(["t3", "antigravity", "status", "--project", str(project)]) == 0
    assert json.loads(capsys.readouterr().out)["installed"] is True
    assert cli.main(["t3", "antigravity", "list"]) == 0
    assert [p["project"] for p in json.loads(capsys.readouterr().out)["projects"]] == [str(project.resolve())]
    assert cli.main(["t3", "antigravity", "remove", "--project", str(project)]) == 0
    assert json.loads(capsys.readouterr().out)["status"]["installed"] is False


def test_install_never_writes_under_t3_or_gemini(home):
    _settings(home, AG_INSTANCE)
    project = home / "proj"
    project.mkdir()
    before = _tree(home / ".t3")
    ag.install_project(str(project), home=home)
    ag.uninstall_project(str(project), home=home)
    assert _tree(home / ".t3") == before
    assert not (home / ".gemini").exists()


# ---- the child advisory -------------------------------------------------------


def _advisory(instance_id: str) -> str | None:
    payload = {"tool_name": "delegate_task",
               "tool_input": {"target": {"providerInstanceId": instance_id}, "work": {"text": BRIEF}}}
    return routing_delegation._t3_child_advisory(payload)


def test_child_advisory_follows_the_start_block_not_the_rollup(home):
    _settings(home, {**CURSOR_INSTANCE, **AG_INSTANCE, "cursor_bare": {"driver": "cursor", "enabled": True}})
    _cursor(home, hooks=TODAY)  # partial, but the start block is there
    assert _advisory("cursor") is None
    (home / ".cursor" / "hooks.json").write_text(json.dumps({"hooks": {"stop": _entry("/x/khipu-stop-hook")}}))
    assert _advisory("cursor") == routing_delegation.CHILD_NO_ROUTING_ADVISORY


def test_child_advisory_always_fires_for_antigravity_even_fully_installed(home):
    _settings(home, AG_INSTANCE)
    assert _advisory("antigravity") == routing_delegation.CHILD_NO_ROUTING_ADVISORY
    project = home / "proj"
    project.mkdir()
    ag.install_project(str(project), home=home)
    assert _by_id(_health(home))["antigravity"]["hooks"] == "installed"
    assert _advisory("antigravity") == routing_delegation.CHILD_NO_ROUTING_ADVISORY


# ---- unchanged drivers --------------------------------------------------------


def test_claude_codex_and_unknown_drivers_keep_their_single_word(home):
    codex_home = home / "codex-home"
    codex_home.mkdir()
    (codex_home / "hooks.json").write_text(json.dumps(
        {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "khipu-recall-hook"}]}]}}))
    _settings(home, {
        "codex": {"driver": "codex", "enabled": True, "config": {"homePath": str(codex_home)}},
        "grok": {"driver": "grokBuild", "displayName": "Grok Build", "enabled": True},
    })
    out = _health(home)
    providers = _by_id(out)
    assert providers["codex"]["hooks"] == "installed" and "functions" not in providers["codex"]
    assert providers["grok"]["hooks"] == "missing" and "functions" not in providers["grok"]
    assert _warnings(out) == ["T3 provider Grok Build (grokBuild) has no Khipu hooks, "
                               "so sessions on it get no Khipu routing start block"]


# ---- one writer: the Antigravity pack and `khipu t3 antigravity` agree ----------


def test_t3_add_and_pack_project_install_write_the_same_bytes(home):
    via_t3, via_pack = home / "a", home / "b"
    via_t3.mkdir()
    via_pack.mkdir()
    assert ag.install_project(str(via_t3), home=home)["ok"]
    # No ~/.gemini here, so the pack writes the project files only, as t3 does.
    assert integ.install("antigravity", project=str(via_pack)).get("ok", True)
    assert _tree(via_t3) == _tree(via_pack)
    assert integ.status("antigravity", project=str(via_t3))["project"]["installed"] is True
    assert ag.project_status(via_pack)["installed"] is True


def test_t3_add_refuses_a_foreign_khipu_hook_set(home):
    project = home / "proj"
    (project / ".agents").mkdir(parents=True)
    foreign = {"khipu": {"enabled": True, "Stop": [{"type": "command", "command": "someone-elses-hook"}]}}
    (project / ".agents" / "hooks.json").write_text(json.dumps(foreign), encoding="utf-8")
    out = ag.install_project(str(project), home=home)
    assert out["ok"] is False and "does not own" in out["error"]
    assert json.loads((project / ".agents" / "hooks.json").read_text()) == foreign
    assert ag.registered_projects(home) == []
