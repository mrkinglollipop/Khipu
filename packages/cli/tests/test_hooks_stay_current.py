"""Hook wiring that keeps itself current, and builds that say which commit they are.

A promote and a new app build refresh the hooks Khipu already wired into Claude
Code and Cursor (never Codex's, never a harness with no Khipu hook), `khipu
doctor` says when any harness lags (warning only), and `khipu --version`,
doctor and the promote's version line carry the commit.

Every test runs under its own temporary HOME; none can touch a real ~/.claude,
~/.codex or ~/.cursor.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from khipu import build_info, cli, integrations as integ
from khipu import promote as pm


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(h / ".config" / "khipu"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(integ, "HOME", h)
    monkeypatch.setattr(integ, "CLAUDE_JSON", h / ".claude.json")
    monkeypatch.setattr(integ, "CLAUDE_SETTINGS", h / ".claude" / "settings.json")
    monkeypatch.setattr(integ, "CURSOR_MCP", h / ".cursor" / "mcp.json")
    monkeypatch.setattr(integ, "CURSOR_HOOKS", h / ".cursor" / "hooks.json")
    monkeypatch.setattr(integ, "CODEX_TOML", h / ".codex" / "config.toml")
    monkeypatch.setattr(integ, "CODEX_HOOKS", h / ".codex" / "hooks.json")
    return h


def _wire_everything(home: Path) -> None:
    """Claude Code, Cursor and Codex found, each installed as this version would."""
    (home / ".claude").mkdir()
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {}}))
    (home / ".cursor").mkdir()
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text('model = "x"\n')
    for harness in ("claude_code", "cursor", "codex"):
        assert not integ.install(harness).get("error")
        assert integ.hook_plan(harness)["changes"] == []


def _age(home: Path) -> dict[str, bytes]:
    """Leave each harness's hook list the way an older build wrote it: Claude
    Code and Cursor miss events, the delegation matcher is the old wide one,
    Codex has no per-prompt or delegation hook. Returns the files' bytes."""
    s = home / ".claude" / "settings.json"
    d = json.loads(s.read_text())
    d["hooks"].pop("SubagentStop")
    for e in d["hooks"]["PreToolUse"]:
        e["matcher"] = ".*"
    s.write_text(json.dumps(d))
    c = home / ".cursor" / "hooks.json"
    d = json.loads(c.read_text())
    d["hooks"].pop("beforeSubmitPrompt")
    d["hooks"].pop("postToolUse")
    c.write_text(json.dumps(d))
    x = home / ".codex" / "hooks.json"
    d = json.loads(x.read_text())
    d["hooks"].pop("UserPromptSubmit")
    d["hooks"].pop("PreToolUse")
    x.write_text(json.dumps(d))
    return {name: (home / name).read_bytes()
            for name in (".claude/settings.json", ".cursor/hooks.json", ".codex/hooks.json", ".codex/config.toml")}


# ---- the refresh ---------------------------------------------------------------


def test_a_refresh_updates_claude_and_cursor_and_leaves_codex_alone(home):
    _wire_everything(home)
    before = _age(home)
    codex_files = sorted(p.name for p in (home / ".codex").iterdir())
    out = integ.refresh_hooks()
    by = {e["harness"]: e for e in out["harnesses"]}
    assert out["ok"] is True
    # Claude Code: the missing SubagentStop and the narrowed matcher, in the installer's words.
    assert by["claude_code"]["state"] == "changed"
    assert any("hooks.SubagentStop += khipu-stop-hook" in c for c in by["claude_code"]["changes"])
    assert any("khipu-delegation-hook matcher -> " + integ.DELEGATION_MATCHER in c
               for c in by["claude_code"]["changes"])
    assert all(str(home / ".claude" / "settings.json") in c for c in by["claude_code"]["changes"]
               if "hooks." in c)
    # Cursor: the two events its older hooks.json lacked.
    assert by["cursor"]["state"] == "changed"
    assert any("hooks.beforeSubmitPrompt += khipu-prompt-recall" in c for c in by["cursor"]["changes"])
    assert any("hooks.postToolUse += khipu-delegation-hook" in c for c in by["cursor"]["changes"])
    # Both files really changed and are now what an install would write.
    assert (home / ".claude" / "settings.json").read_bytes() != before[".claude/settings.json"]
    assert (home / ".cursor" / "hooks.json").read_bytes() != before[".cursor/hooks.json"]
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == integ.DELEGATION_MATCHER
    assert integ.hook_plan("claude_code")["changes"] == []
    assert integ.hook_plan("cursor")["changes"] == []
    # Codex: not one byte of its files, and the owner is told what waits.
    assert (home / ".codex" / "hooks.json").read_bytes() == before[".codex/hooks.json"]
    assert (home / ".codex" / "config.toml").read_bytes() == before[".codex/config.toml"]
    assert by["codex"]["state"] == "needs the owner"
    assert any("UserPromptSubmit" in c for c in by["codex"]["changes"])
    assert "approve the hooks in Codex" in by["codex"]["note"]
    assert sorted(p.name for p in (home / ".codex").iterdir()) == codex_files


def test_a_second_refresh_changes_nothing(home):
    _wire_everything(home)
    _age(home)
    integ.refresh_hooks()
    snapshot = {p: p.read_bytes() for p in (home / ".claude" / "settings.json", home / ".cursor" / "hooks.json")}
    again = integ.refresh_hooks()
    states = {e["harness"]: e["state"] for e in again["harnesses"]}
    assert states["claude_code"] == states["cursor"] == "current"
    assert {p: p.read_bytes() for p in snapshot} == snapshot


def test_a_dry_run_refresh_writes_nothing(home):
    _wire_everything(home)
    before = _age(home)
    out = integ.refresh_hooks(dry_run=True)
    assert {e["harness"]: e["state"] for e in out["harnesses"]}["claude_code"] == "would change"
    assert {name: (home / name).read_bytes() for name in before} == before


def test_a_harness_with_no_khipu_hook_is_never_installed_into(home):
    (home / ".claude").mkdir()
    legacy = {"hooks": {"PreCompact": [{"hooks": [{"type": "command", "command": "python3 /me/flush.py"}]}]}}
    (home / ".claude" / "settings.json").write_text(json.dumps(legacy))
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"khipu": {"command": "/old/khipu-mcp"}}}))
    (home / ".cursor").mkdir()  # found, nothing in it
    claude_before = (home / ".claude" / "settings.json").read_bytes()
    mcp_before = (home / ".claude.json").read_bytes()
    out = integ.refresh_hooks()
    states = {e["harness"]: e["state"] for e in out["harnesses"]}
    assert states["claude_code"] == states["cursor"] == "not installed; left alone"
    assert states["codex"] == "not found"
    assert (home / ".claude" / "settings.json").read_bytes() == claude_before
    assert (home / ".claude.json").read_bytes() == mcp_before
    assert not (home / ".cursor" / "hooks.json").exists() and not (home / ".cursor" / "mcp.json").exists()
    assert not (home / ".config" / "khipu" / "bin").exists()  # no launcher link was made either


def test_an_unreadable_hook_file_fails_that_harness_and_not_the_others(home):
    _wire_everything(home)
    _age(home)
    (home / ".claude" / "settings.json").write_text("{ not json")
    out = integ.refresh_hooks()
    by = {e["harness"]: e for e in out["harnesses"]}
    assert out["ok"] is False
    assert by["claude_code"]["state"] == "failed" and by["claude_code"]["error"]
    assert by["cursor"]["state"] == "changed"
    assert (home / ".claude" / "settings.json").read_text() == "{ not json"


def test_a_machine_running_promoted_code_leaves_the_refresh_to_the_promote(home, tmp_path):
    _wire_everything(home)
    before = _age(home)
    slot = tmp_path / "slot"
    (slot / "packages" / "cli" / "khipu").mkdir(parents=True)
    live = home / ".config" / "khipu" / "live"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.symlink_to(slot)
    out = integ.refresh_hooks()  # this process is not the live code
    assert out["ok"] and out["harnesses"] == [] and "khipu promote" in out["skipped"]
    assert "left alone" in integ.format_hook_refresh(out)[0]
    assert {name: (home / name).read_bytes() for name in before} == before


def test_the_refresh_command_prints_json_and_fails_only_when_a_harness_failed(home, capsys):
    _wire_everything(home)
    _age(home)
    assert cli.main(["integrations", "refresh", "--dry-run"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] is True
    assert cli.main(["integrations", "refresh"]) == 0
    assert {e["harness"]: e["state"] for e in json.loads(capsys.readouterr().out)["harnesses"]}["cursor"] == "changed"
    (home / ".cursor" / "hooks.json").write_text("nope")
    assert cli.main(["integrations", "refresh"]) == 1


# ---- after a promote -----------------------------------------------------------


def _live_env(home: Path) -> dict[str, str]:
    return pm._code_env(pm._self_root(), pm._base_env(pm.DATA_DIR_ENV_KEYS))


def test_the_promotes_refresh_runs_the_live_code_and_changes_claude_and_cursor_only(home, monkeypatch):
    """A real subprocess of this checkout's code under the temp HOME, the way
    the promote runs the code it just made live."""
    monkeypatch.setenv("KHIPU_PYTHON", sys.executable)
    _wire_everything(home)
    before = _age(home)
    res = pm._refresh_hooks(pm._self_root(), _live_env(home))
    assert res["ok"] is True, res
    by = {e["harness"]: e for e in res["harnesses"]}
    assert by["claude_code"]["state"] == by["cursor"]["state"] == "changed"
    assert by["codex"]["state"] == "needs the owner"
    assert (home / ".claude" / "settings.json").read_bytes() != before[".claude/settings.json"]
    assert (home / ".cursor" / "hooks.json").read_bytes() != before[".cursor/hooks.json"]
    assert (home / ".codex" / "hooks.json").read_bytes() == before[".codex/hooks.json"]
    # The result prints each file and what changed, and what waits on the owner.
    text = "\n".join(pm._hooks_lines({"hooks_refresh": res}))
    assert f"{home / '.claude' / 'settings.json'}: hooks.SubagentStop += khipu-stop-hook" in text
    assert "hooks Codex: needs the owner" in text and "approve the hooks in Codex" in text


def _entry_after_flip(monkeypatch, refresh):
    """What `_post_flip_result` makes of a flip whose jobs refresh worked and
    whose hook refresh is `refresh(argv)`."""
    def fake_run(argv, *, env=None, input=None, cwd=None, timeout=120):
        if "integrations" in argv:
            return refresh(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True, "refreshed": []}), "")

    monkeypatch.setattr(pm, "_run", fake_run)
    monkeypatch.setattr(pm, "precompile", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(pm, "live_root", lambda: Path("/nonexistent-live"))
    return pm._post_flip_result({"action": "promote", "ok": True, "slot": "/s/a", "ref": "origin/main"}, 0.0)


@pytest.mark.parametrize("refresh", [
    lambda argv: subprocess.CompletedProcess(argv, 1, "", "Traceback: boom"),
    lambda argv: subprocess.CompletedProcess(argv, 0, "not json at all", ""),
    lambda argv: (_ for _ in ()).throw(OSError("no python")),
    lambda argv: subprocess.CompletedProcess(argv, 1, json.dumps({"ok": False, "harnesses": [
        {"harness": "claude_code", "state": "failed", "error": "unreadable"}]}), ""),
], ids=["exit-1", "not-json", "raises", "a-harness-failed"])
def test_a_failed_hook_refresh_never_fails_the_promote(monkeypatch, refresh):
    out = _entry_after_flip(monkeypatch, refresh)
    assert out["ok"] is True and "partial" not in out and out["live_changed"] is True
    assert out["after"]["hooks_refresh"]["ok"] is False
    text = pm.format_result({**out, "slot": "/s/a"})
    assert "hooks refresh failed (the promote itself is not affected)" in text
    assert "live is now a." in text


def test_a_good_hook_refresh_is_in_the_promotes_output(monkeypatch):
    body = {"ok": True, "harnesses": [
        {"harness": "claude_code", "state": "changed",
         "changes": ["/h/.claude/settings.json: hooks.PreToolUse khipu-delegation-hook matcher -> X"]},
        {"harness": "cursor", "state": "current"}]}
    out = _entry_after_flip(monkeypatch, lambda argv: subprocess.CompletedProcess(argv, 0, json.dumps(body), ""))
    text = pm.format_result({**out, "slot": "/s/a"})
    assert out["ok"] is True and out["after"]["hooks_refresh"]["ok"] is True
    assert "hooks Claude Code: changed" in text
    assert "/h/.claude/settings.json: hooks.PreToolUse khipu-delegation-hook matcher -> X" in text


# ---- the doctor row ------------------------------------------------------------


def test_the_doctor_row_is_clean_when_every_wired_hook_is_current(home):
    _wire_everything(home)
    rep = integ.hook_wiring_report()
    assert rep["ok"] and rep["warnings"] == []
    assert {r["harness"]: r["state"] for r in rep["harnesses"]} == {
        "claude_code": "current", "cursor": "current", "codex": "current"}


def test_the_doctor_warns_per_stale_harness_with_the_fix(home):
    _wire_everything(home)
    _age(home)
    rep = integ.hook_wiring_report()
    assert rep["ok"] is False
    claude, cursor, codex = rep["warnings"]
    assert claude.startswith("Claude Code: the wired hooks are behind this version")
    assert "hooks.SubagentStop += khipu-stop-hook" in claude
    assert "khipu integrations install claude_code" in claude and "refreshes it by itself" in claude
    assert cursor.startswith("Cursor:") and "khipu integrations install cursor" in cursor
    assert codex.startswith("Codex:") and "it needs you" in codex
    assert "khipu integrations install codex" in codex and "approve the hooks in Codex" in codex
    # The row does not call a refresh-only fix a Codex job.
    assert "approve" not in claude and "approve" not in cursor


def test_a_stale_codex_mcp_alone_does_not_ask_for_a_hook_approval(home):
    _wire_everything(home)
    (home / ".codex" / "config.toml").write_text('model = "x"\n[mcp_servers.khipu]\ncommand = "/old/khipu-mcp"\n')
    rep = integ.hook_wiring_report()
    assert "khipu integrations install codex" in rep["warnings"][0]
    assert "approve" not in rep["warnings"][0]


def test_a_harness_found_with_no_khipu_hook_has_no_warning(home):
    (home / ".cursor").mkdir()
    (home / ".claude").mkdir()
    rep = integ.hook_wiring_report()
    assert rep["warnings"] == []
    assert {r["harness"]: r["state"] for r in rep["harnesses"]} == {
        "claude_code": "not installed", "cursor": "not installed"}


def test_a_stale_hook_list_never_turns_doctors_ok_false(home, monkeypatch, capsys):
    assert "hook_wiring" not in cli.OK_GATES and "hook_wiring_ok" not in cli.OK_GATES
    assert not any("hook_wiring" in g for g in cli.LOCAL_GATES)
    _wire_everything(home)
    cli.main(["doctor", "--quick"])
    clean = json.loads(capsys.readouterr().out)
    _age(home)
    rc = cli.main(["doctor", "--quick"])
    stale = json.loads(capsys.readouterr().out)
    assert clean["hook_wiring"]["warnings"] == []
    assert len(stale["hook_wiring"]["warnings"]) == 3
    assert all(w in stale["warnings"] for w in stale["hook_wiring"]["warnings"])
    assert stale["ok"] == clean["ok"] and rc in (0, 2)
    assert not any(not v for k, v in stale.items() if k.startswith("hook_wiring"))


def test_a_failing_row_is_a_warning_not_a_crash(home, monkeypatch):
    monkeypatch.setattr(integ, "hook_wiring_report", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    block = cli._hook_wiring_block()
    assert block["ok"] is False and "boom" in block["warnings"][0]


# ---- which commit this build is -------------------------------------------------


def _git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "-c", "commit.gpgsign=false", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def fresh_commit():
    real = build_info.commit
    real.cache_clear()
    yield
    real.cache_clear()


def test_a_checkout_reads_its_commit_from_git(tmp_path, monkeypatch, fresh_commit):
    _git("init", "-q", cwd=tmp_path)
    (tmp_path / "f").write_text("x")
    _git("add", "-A", cwd=tmp_path)
    _git("commit", "-q", "-m", "x", cwd=tmp_path)
    full = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(tmp_path), capture_output=True,
                          text=True, check=True).stdout.strip()
    monkeypatch.setattr(build_info, "_root", lambda: tmp_path)
    assert build_info.commit() == full[:8]
    assert build_info.stamp() == f"{build_info.__version__} ({full[:8]})"


def test_a_bundle_reads_the_commit_its_build_stamped(tmp_path, monkeypatch, fresh_commit):
    (tmp_path / "BUILD_COMMIT").write_text("25718b62ab\n")
    monkeypatch.setattr(build_info, "_root", lambda: tmp_path)
    assert build_info.stamp() == f"{build_info.__version__} (25718b62)"


def test_a_bundle_with_no_stamp_says_commit_unknown(tmp_path, monkeypatch, fresh_commit):
    monkeypatch.setattr(build_info, "_root", lambda: tmp_path)
    assert build_info.commit() is None
    assert build_info.stamp() == f"{build_info.__version__} (commit unknown)"


def test_a_bundle_inside_some_other_repository_does_not_report_that_repository(tmp_path, monkeypatch, fresh_commit):
    _git("init", "-q", cwd=tmp_path)
    (tmp_path / "f").write_text("x")
    _git("add", "-A", cwd=tmp_path)
    _git("commit", "-q", "-m", "x", cwd=tmp_path)
    bundle = tmp_path / "apps" / "desktop" / "khipu-resources"
    bundle.mkdir(parents=True)
    monkeypatch.setattr(build_info, "_root", lambda: bundle)
    assert build_info.stamp().endswith("(commit unknown)")


@pytest.mark.parametrize("text", ["", "not a sha", "ZZZZZZZZ", "abc", "25718b62 extra"])
def test_a_damaged_stamp_is_unknown_never_a_guess(tmp_path, monkeypatch, fresh_commit, text):
    (tmp_path / "BUILD_COMMIT").write_text(text)
    monkeypatch.setattr(build_info, "_root", lambda: tmp_path)
    assert build_info.commit() is None


def test_version_flag_prints_version_and_commit(capsys, monkeypatch, fresh_commit):
    monkeypatch.setattr(build_info, "commit", lambda: "25718b62")
    with pytest.raises(SystemExit) as stop:
        cli.main(["--version"])
    assert stop.value.code == 0
    assert capsys.readouterr().out.strip() == f"khipu {build_info.__version__} (25718b62)"


def test_doctor_quick_reports_version_and_commit(home, capsys, monkeypatch, fresh_commit):
    monkeypatch.setattr(build_info, "commit", lambda: "25718b62")
    cli.main(["doctor", "--quick"])
    assert json.loads(capsys.readouterr().out)["version"] == f"{build_info.__version__} (25718b62)"


def test_the_promotes_version_line_shows_both_commits(home, tmp_path, monkeypatch):
    candidate = tmp_path / "candidate"
    live = tmp_path / "live-code"
    for root in (candidate, live):
        (root / "packages" / "cli" / "khipu").mkdir(parents=True)
    monkeypatch.setattr(pm, "_live_code_root", lambda: live)
    facts = {candidate: {"version": "0.4.15", "stamp": "0.4.15 (25718b62)", "schema": 4, "disk_schema": 4},
             live: {"version": "0.4.15", "stamp": "0.4.15 (fb707582)", "schema": 4, "disk_schema": 4}}
    monkeypatch.setattr(pm, "_code_facts", lambda root: facts[Path(root)])
    out = pm.gate_version(candidate)
    assert out["ok"] and "version 0.4.15 (fb707582) -> 0.4.15 (25718b62)" in out["detail"]


def test_code_with_no_stamp_of_its_own_gets_its_commit_from_git_or_says_unknown(tmp_path, monkeypatch):
    plain = tmp_path / "bundle"
    plain.mkdir()
    assert pm._build_label({"version": "0.4.14"}, plain) == "0.4.14 (commit unknown)"
    _git("init", "-q", cwd=tmp_path / "bundle")
    (plain / "f").write_text("x")
    _git("add", "-A", cwd=plain)
    _git("commit", "-q", "-m", "x", cwd=plain)
    head = pm._head(plain)
    assert pm._build_label({"version": "0.4.14"}, plain) == f"0.4.14 ({head[:8]})"


def test_the_build_scripts_stamp_the_commit_before_signing():
    scripts = Path(__file__).resolve().parents[3] / "apps" / "desktop" / "scripts"
    bundle = (scripts / "bundle_cli.sh").read_text()
    assert 'rev-parse --short=8 HEAD' in bundle and '"$OUT/BUILD_COMMIT"' in bundle
    release = (scripts / "release_macos.sh").read_text()
    assert "KhipuBuildCommit" in release
    assert release.index("KhipuBuildCommit") < release.index('codesign --force --deep --options runtime')
    assert build_info.STAMP_FILE == "BUILD_COMMIT"
    for script in ("bundle_cli.sh", "release_macos.sh"):
        assert subprocess.run(["bash", "-n", str(scripts / script)]).returncode == 0
