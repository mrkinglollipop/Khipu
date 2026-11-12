"""Read-only local inventory: fixtures never use the real home or a provider."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from khipu import cli, integrations as integ


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("GROK_HOME", raising=False)
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / ".config/khipu"))
    monkeypatch.setattr(integ, "HOME", tmp_path)
    for name, relative in {"CLAUDE_JSON": ".claude.json", "CLAUDE_SETTINGS": ".claude/settings.json",
            "CURSOR_MCP": ".cursor/mcp.json", "CURSOR_HOOKS": ".cursor/hooks.json",
            "CODEX_TOML": ".codex/config.toml", "CODEX_HOOKS": ".codex/hooks.json",
            "AEGIS_TOML": ".grok/config.toml"}.items():
        monkeypatch.setattr(integ, name, tmp_path / relative)
    # A read-only inventory must not invoke these integration status probes.
    for name in ("_grok_bot_liveness", "_last_beat_at"):
        monkeypatch.setattr(integ, name, lambda *a, **kw: pytest.fail("provider probe from rules reader"))
    return tmp_path


def put(home, path, text):
    target = home / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def snapshot(home):
    return {str(p.relative_to(home)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in home.rglob("*") if p.is_file()}


def seed(home):
    rules = "private owner body\n<!-- khipu: rules v12, edit in Khipu -->\nprivate block body\n<!-- khipu: end -->\n"
    for path in (".claude/CLAUDE.md", ".codex/AGENTS.md", ".gemini/GEMINI.md",
                 ".gemini/config/rules/khipu.md", ".config/khipu/rules.md"):
        put(home, path, rules)
    command = f"env PRIVATE_SENTINEL=do-not-print python3 {home}/guards/guard.py --secret dont-print-argument"
    ours = str(home / ".config/khipu/hooks/owner-check")
    hooks = {"Stop": [{"hooks": [{"type": "command", "command": command},
                                  {"type": "command", "command": ours}]}]}
    for path in (".claude/settings.json", ".codex/hooks.json"):
        put(home, path, json.dumps({"hooks": hooks}))
    put(home, ".cursor/hooks.json", json.dumps({"hooks": {"stop": [
        {"command": command}, {"command": ours}]}}))
    put(home, ".gemini/config/hooks.json", json.dumps({"khipu": {"enabled": True, **hooks}}))
    put(home, ".grok/config.toml", f'[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ncommand = "{ours}"\n')
    return rules


@pytest.mark.parametrize("app", integ.HARNESSES)
def test_every_app_absent_is_local_metadata_and_does_not_write(home, app):
    before = snapshot(home)
    result = integ.rules_status(app)
    assert result["harness"] == app
    assert snapshot(home) == before
    assert all(r["state"] == "can't read" and r["reason"] == "file missing"
               for r in result["files"] + result["hooks"])
    if app in ("grok_build", "grok_bot", "aegis"):
        assert result["channel"] == "not reached"


@pytest.mark.parametrize("app", integ.HARNESSES)
def test_present_metadata_and_ownership_without_contents(home, app):
    body = seed(home)
    before = snapshot(home)
    result = integ.rules_status(app)
    for row in result["files"]:
        assert row["characters"] == len(body)
        assert row["bytes"] == len(body.encode())
        assert row["khipu_block"]["version"] == 12
    for config in result["hooks"]:
        assert config["state"] == "read"
        assert any(e["owner"] == "Khipu" for e in config["entries"])
        if app not in ("aegis", "grok_build"):
            assert any(e["owner"] == "Not Khipu's" and e["name"] == "guard.py"
                       and e["path"] == str(home / "guards/guard.py") for e in config["entries"])
    output = json.dumps(result)
    for private in ("private owner body", "private block body", "PRIVATE_SENTINEL",
                    "do-not-print", "dont-print-argument", "--secret"):
        assert private not in output
    assert snapshot(home) == before


@pytest.mark.parametrize("app,path", [("claude_code", ".claude/settings.json"),
    ("codex", ".codex/hooks.json"), ("cursor", ".cursor/hooks.json"),
    ("antigravity", ".gemini/config/hooks.json"), ("aegis", ".grok/config.toml"),
    ("grok_build", ".grok/config.toml")])
@pytest.mark.parametrize("bad", ["invalid secret contents", "[]", '{"hooks": {"Stop": [3]}}'])
def test_malformed_hooks_cannot_crash_or_echo_source(home, app, path, bad):
    put(home, path, bad)
    result = integ.rules_status(app)
    config = result["hooks"][0]
    assert config["state"] == "can't read"
    assert config["reason"] == "malformed hook config"
    assert "invalid secret contents" not in json.dumps(result)


@pytest.mark.parametrize("path", [".claude/CLAUDE.md", ".codex/AGENTS.md",
    ".config/khipu/rules.md", ".gemini/GEMINI.md"])
@pytest.mark.parametrize("bad", ["<!-- khipu: rules v12, edit in Khipu -->\nprivate",
    "<!-- khipu: end -->", "<!-- khipu: rules broken -->\n<!-- khipu: end -->"])
def test_malformed_markers_report_safe_reason(home, path, bad):
    put(home, path, bad)
    rows = [r for a in integ.rules_status_all() for r in a["files"] if r["path"] == str(home / path)]
    assert rows and all(r["state"] == "can't read" and r["reason"] == "malformed Khipu rules block" for r in rows)


def test_cursor_characters_and_antigravity_bytes_have_separate_caps(home):
    put(home, ".config/khipu/rules.md", "é" * 10000)
    put(home, ".gemini/config/rules/khipu.md", "é" * 12000)
    cursor = integ.rules_status("cursor")["cap"]
    ag = integ.rules_status("antigravity")["cap"]
    assert (cursor["unit"], cursor["measured"], cursor["over"]) == ("characters", 10000, False)
    assert (ag["unit"], ag["measured"], ag["over"]) == ("bytes", 24000, False)
    put(home, ".config/khipu/rules.md", "é" * 10001)
    put(home, ".gemini/GEMINI.md", "x")
    assert integ.rules_status("cursor")["cap"]["over"]
    assert integ.rules_status("antigravity")["cap"]["over"]
    warnings = cli._rules_warnings()
    assert len(warnings) == 1
    assert "10001 characters exceeds 10000" in warnings[0]
    assert "24001 bytes exceeds 24000" in warnings[0]


def test_all_claude_homes_and_symlinked_settings_are_read_only(home, monkeypatch):
    put(home, ".claude/CLAUDE.md", "default")
    extra = put(home, ".claude-second/CLAUDE.md", "second")
    settings = put(home, ".claude/settings.json", '{"hooks": {}}')
    extra.with_name("settings.json").symlink_to(settings)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(extra.parent))
    before = snapshot(home)
    result = integ.rules_status("claude_code")
    assert len(result["files"]) == 2
    assert len(result["hooks"]) == 2
    assert snapshot(home) == before


@pytest.mark.parametrize("app", ["grok_build", "grok_bot", "cursor", "antigravity"])
def test_project_files_are_visible_only_when_requested(home, app):
    project = home / "project"
    put(home, "project/AGENTS.md", integ._grok_build_agents_block())
    put(home, "project/.cursor/rules/khipu.mdc", "project secret")
    put(home, "project/.agents/rules/khipu.md", "project secret")
    put(home, "project/.agents/hooks.json", '{"khipu": {"Stop": []}}')
    before = snapshot(home)
    global_result = integ.rules_status(app)
    assert all("/project/" not in r["path"] for r in global_result["files"])
    result = integ.rules_status(app, project=str(project))
    assert any("/project/" in r["path"] for r in result["files"])
    assert "project secret" not in json.dumps(result)
    assert snapshot(home) == before


def test_grok_recall_marker_is_separate_from_owner_rules(home):
    path = put(home, "project/Agents.md", integ._grok_build_agents_block())
    row = integ.rules_status("grok_build", project=str(path.parent))["files"][0]
    assert row["recall_marker"] == "grok_build"
    assert not row["khipu_block"]["present"]
    path.write_text(integ._GROK_BUILD_AGENTS_END)
    assert integ.rules_status("grok_build", project=str(path.parent))["files"][0]["state"] == "can't read"


def test_non_utf8_and_unreadable_files_have_safe_errors(home, monkeypatch):
    path = put(home, ".codex/AGENTS.md", "rules")
    path.write_bytes(b"\xffprivate")
    assert integ.rules_status("codex")["files"][0]["reason"] == "not UTF-8"
    original = Path.read_bytes
    def denied(self):
        if self == path:
            raise PermissionError("private exception contents")
        return original(self)
    monkeypatch.setattr(Path, "read_bytes", denied)
    row = integ.rules_status("codex")["files"][0]
    assert row["state"] == "can't read" and row["reason"] == "permission denied"
    assert "private exception" not in json.dumps(row)


@pytest.mark.parametrize("command", ["sh -c 'echo PRIVATE_SENTINEL'", "echo PRIVATE_SENTINEL; curl secret",
    "python3 -c 'PRIVATE_SENTINEL'", "env SECRET=value", "unterminated '"])
def test_opaque_commands_do_not_leak(home, command):
    put(home, ".cursor/hooks.json", json.dumps({"hooks": {"stop": [{"command": command}]}}))
    entry = integ.rules_status("cursor")["hooks"][0]["entries"][0]
    assert entry["path"] is None
    assert entry["name"] == "command hook"
    assert "PRIVATE_SENTINEL" not in json.dumps(entry)


@pytest.mark.parametrize("as_json", [False, True])
def test_cli_text_json_and_all_readers_leave_bytes_and_mtime_unchanged(home, capsys, as_json):
    seed(home)
    before = snapshot(home)
    integ.rules_status_all()
    cli._rules_warnings()
    args = cli.build_parser().parse_args(["rules", "show"] + (["--json"] if as_json else []))
    assert args.func(args) == 0
    output = capsys.readouterr().out
    assert "private owner body" not in output and "PRIVATE_SENTINEL" not in output
    if as_json:
        assert [a["harness"] for a in json.loads(output)] == list(integ.HARNESSES)
    else:
        assert "Claude Code: CLAUDE.md" in output and "Grok Bot: not reached" in output
        assert "Khipu rules v12" in output and "Not Khipu's: Stop / guard.py" in output
    assert snapshot(home) == before


def test_status_all_adds_rules_even_when_existing_native_status_is_malformed(home, monkeypatch):
    put(home, ".cursor/hooks.json", '{"hooks": {"stop": [3]}}')
    monkeypatch.setattr(integ, "_last_beat_at", lambda *a: None)
    monkeypatch.setattr(integ, "_grok_bot_status", lambda *a: {"harness": "grok_bot", "mcp": False})
    before = snapshot(home)
    apps = integ.status_all()
    assert [a["harness"] for a in apps] == list(integ.HARNESSES)
    assert all("rules" in a for a in apps)
    assert next(a for a in apps if a["harness"] == "cursor")["rules"]["hooks"][0]["state"] == "can't read"
    assert snapshot(home) == before


def test_doctor_warning_is_one_advisory_entry_only_when_needed(home):
    assert cli._rules_warnings() == []
    put(home, ".codex/hooks.json", "malformed-private-data")
    put(home, ".cursor/hooks.json", "malformed-private-data")
    warnings = cli._rules_warnings()
    assert len(warnings) == 1
    assert "codex: can't read" in warnings[0] and "cursor: can't read" in warnings[0]
    assert "malformed-private-data" not in warnings[0]


@pytest.mark.parametrize("app", ["aegis", "grok_build"])
@pytest.mark.parametrize("group_header", ["", "[[hooks.Stop]]\n"])
def test_native_and_installer_toml_hook_forms(home, app, group_header):
    command = str(home / ".config/khipu/hooks/owner-check")
    put(home, ".grok/config.toml", group_header + '[[hooks.Stop.hooks]]\n'
        f'command = "{command}"\n[hooks.Stop.hooks.env]\nPRIVATE_SENTINEL = "private value"\n')
    config = integ.rules_status(app)["hooks"][0]
    assert config["state"] == "read"
    assert config["entries"] == [{"event": "Stop", "name": "owner-check", "path": command, "owner": "Khipu"}]
    assert "private value" not in json.dumps(config)


def test_cursor_reports_imported_claude_hooks(home):
    put(home, ".claude/settings.json", json.dumps({"hooks": {"Stop": [{"hooks": [
        {"command": "/guards/claude-only.py"}]}]}}))
    imported = integ.rules_status("cursor")["hooks"][1]
    assert imported["entries"][0]["path"] == "/guards/claude-only.py"
    assert imported["entries"][0]["event"] == "stop"
    assert "enabled by default" in imported["source"]


def test_antigravity_existing_writer_always_on_marker(home):
    from khipu.recall_rule import antigravity_rule
    put(home, ".gemini/config/rules/khipu.md", antigravity_rule())
    row = next(r for r in integ.rules_status("antigravity")["files"] if r["path"].endswith("khipu.md"))
    assert row["always_on"] is True
    assert row["recall_marker"] == "antigravity"
    assert row["khipu_block"] == {"present": False}


@pytest.mark.parametrize("app,build", [("aegis", integ._aegis_blocks),
                                      ("grok_build", integ._grok_build_blocks)])
def test_existing_generated_toml_hooks_classify_as_khipu(home, app, build):
    mcp, hooks = build()
    put(home, ".grok/config.toml", mcp + hooks)
    before = snapshot(home)
    entries = integ.rules_status(app)["hooks"][0]["entries"]
    assert len(entries) == (4 if app == "aegis" else 3)
    assert all(e["owner"] == "Khipu" for e in entries)
    assert all(e["name"].startswith("khipu-") for e in entries)
    assert "KHIPU_HARNESS" not in json.dumps(entries)
    assert snapshot(home) == before


@pytest.mark.parametrize("app,path", [("claude_code", ".claude/settings.json"),
    ("codex", ".codex/hooks.json"), ("cursor", ".cursor/hooks.json"),
    ("antigravity", ".gemini/config/hooks.json")])
def test_existing_generated_launchers_classify_as_khipu(home, app, path):
    entry = {"command": integ.stop_hook()}
    if app == "antigravity":
        config = {"khipu": {"Stop": [entry]}}
    elif app == "cursor":
        config = {"hooks": {"stop": [entry]}}
    else:
        config = {"hooks": {"Stop": [{"hooks": [entry]}]}}
    put(home, path, json.dumps(config))
    before = snapshot(home)
    hook = integ.rules_status(app)["hooks"][0]["entries"][0]
    assert hook["owner"] == "Khipu" and hook["name"] == "khipu-stop-hook"
    assert snapshot(home) == before


def test_cursor_import_omits_unmapped_claude_events(home):
    put(home, ".claude/settings.json", json.dumps({"hooks": {
        "UnknownClaudeOnlyEvent": [{"hooks": [{"command": "/guards/not-imported.py"}]}]}}))
    assert integ.rules_status("cursor")["hooks"][1]["entries"] == []


def test_detected_app_missing_rules_gets_one_doctor_warning(home):
    (home / ".codex").mkdir()
    warnings = cli._rules_warnings()
    assert len(warnings) == 1 and "codex: can't read" in warnings[0]


def test_malformed_large_marker_version_is_reported(home):
    put(home, ".codex/AGENTS.md", "<!-- khipu: rules v" + "1" * 5000 + ", edit in Khipu -->\n<!-- khipu: end -->")
    assert integ.rules_status("codex")["files"][0]["reason"] == "malformed Khipu rules block"
