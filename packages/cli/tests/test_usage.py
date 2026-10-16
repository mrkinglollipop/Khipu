"""khipu.usage: plan headroom read from each subscription CLI.

Every adapter runs against a fake child process that answers with a reply
recorded from the real CLI on 2026-10-14 (numbers fixed, ids and emails
replaced; tests/fixtures/usage/). No test starts a real CLI: ``_command`` is
patched in every test that probes, except the Node version manager test, whose
lookup sees only a temp HOME and launchd's PATH.
"""
from __future__ import annotations

import ast
import builtins
import io
import json
import os
import re
import shutil
import sys
import textwrap
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from khipu import mcp_server, usage

FIXTURES = Path(__file__).parent / "fixtures" / "usage"

SENTINEL = "SENTINEL-c0ffee-do-not-leak"

FAKE_CLI = textwrap.dedent("""
    import json, os, subprocess, sys, time
    SENTINEL = "SENTINEL-c0ffee-do-not-leak"
    fixture, mode, out = sys.argv[1], sys.argv[2], sys.argv[3]
    record = {"pid": os.getpid(), "env": sorted(os.environ), "argv": sys.argv[4:], "cwd": os.getcwd(),
              "cwd_entries": os.listdir("."),
              "env_values": {k: os.environ[k] for k in ("CLAUDE_CONFIG_DIR", "GROK_HOME", "CODEX_HOME",
                                                        "GROK_CURSOR_MCPS_ENABLED", "GROK_CLAUDE_MCPS_ENABLED")
                             if k in os.environ}}
    if mode.startswith("grandchild"):
        record["grandchild"] = subprocess.Popen(["/bin/sleep", "60"]).pid  # inherits stdout and the group
    with open(out, "w") as fh:
        json.dump(record, fh)
    if mode in ("hang", "deaf", "grandchild_hang"):
        time.sleep(60)
    if mode == "grandchild":
        sys.exit(0)
    if mode == "exit":
        sys.stderr.write("fake: not signed in\\n")
        sys.exit(3)
    if mode == "secret_exit":
        sys.stderr.write("fatal: " + SENTINEL + " token=" + SENTINEL + "\\n")
        sys.exit(7)
    frames = [json.loads(l) for l in open(fixture) if l.strip()]
    for line in sys.stdin:
        msg = json.loads(line)
        rid = msg.get("id", msg.get("request_id"))
        if rid is None:
            continue
        while frames:
            frame = frames.pop(0)
            sys.stdout.write(json.dumps(frame) + "\\n")
            sys.stdout.flush()
            if frame.get("id") == rid or (frame.get("response") or {}).get("request_id") == rid:
                break
""")


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """Build per-harness fake CLIs; returns (configure, scripts) where
    configure(harness, fixture, mode="answer") registers one."""
    script = tmp_path / "fake_cli.py"
    script.write_text(FAKE_CLI)
    scripts: dict[str, str] = {}
    outs: dict[str, Path] = {}

    def configure(harness: str, fixture, mode: str = "answer") -> Path:
        out = tmp_path / f"{harness}-child.json"
        wrapper = tmp_path / f"fake-{harness}"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "{FIXTURES / fixture}" {mode} "{out}" "$@"\n')
        wrapper.chmod(0o755)
        scripts[harness] = str(wrapper)
        outs[harness] = out
        return out

    monkeypatch.setattr(usage, "_command", lambda harness: scripts.get(harness))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    return configure


def _child(out: Path) -> dict:
    return json.loads(out.read_text())


# ---- adapters against recorded replies -------------------------------------


def test_claude_adapter_reads_the_recorded_reply(fake):
    out = fake("claude", "claude.jsonl")
    row = usage.probe_claude()
    assert row["ok"] is True
    assert row["plan"] == "max"
    assert row["version"] == "2.1.296"
    assert [(w["name"], w["used_pct"]) for w in row["windows"]] == [
        ("five_hour", 16), ("weekly", 17), ("weekly_fable", 34)]
    assert row["windows"][0]["resets_at"] == "2026-01-01T12:00:00Z"
    assert row["windows"][2]["model"] == "Fable"
    assert row["credits"] == {"extra_usage_enabled": False}
    argv = _child(out)["argv"]
    assert "--bare" not in argv
    assert argv[:1] == ["-p"] and "--setting-sources" in argv and "--no-session-persistence" in argv


def test_claude_adapter_sets_the_configured_home(fake, tmp_path):
    out = fake("claude", "claude.jsonl")
    usage.probe_claude(str(tmp_path / "second"))
    assert _child(out)["env_values"]["CLAUDE_CONFIG_DIR"] == str(tmp_path / "second")


def test_claude_login_without_rate_limits_is_a_coded_failure_with_a_fixed_note(fake):
    fake("claude", "claude-null.jsonl")
    row = usage.probe_account({"label": "Claude", "harness": "claude", "home": None})
    assert row["ok"] is False
    assert row["error"] == "no_plan_limits"
    assert "/login" in row["note"]


def test_codex_adapter_reads_the_recorded_reply(fake):
    fake("codex", "codex.jsonl")
    row = usage.probe_codex()
    assert row["ok"] is True and row["source"] == "live"
    assert row["plan"] == "pro"
    assert row["version"] == "0.162.1"
    # secondary was null: absent, not zero
    assert row["windows"] == [{"name": "weekly", "used_pct": 40, "resets_at": "2026-01-01T02:00:00Z",
                               "window_mins": 10080, "slot": "primary"}]
    assert row["credits"] == {"balance": 1234.5, "has_credits": True, "unlimited": False}


def test_grok_adapter_reads_the_recorded_reply_with_its_home(fake, tmp_path):
    out = fake("grok", "grok.jsonl")
    row = usage.probe_grok(str(tmp_path / "grok-home"))
    assert row["ok"] is True
    assert row["plan"] == "SuperGrok Heavy"
    assert row["version"] == "1.0.52"
    assert row["windows"] == [{"name": "weekly", "used_pct": 43.5, "resets_at": "2026-01-11T12:00:00Z"}]
    assert row["credits"] == {"prepaid_balance": 44}
    child = _child(out)
    assert child["env_values"]["GROK_HOME"] == str(tmp_path / "grok-home")
    assert child["argv"][-1] == "stdio"
    # Grok cannot start with no MCP servers; these stop the Cursor/Claude imports.
    assert child["env_values"]["GROK_CURSOR_MCPS_ENABLED"] == "0"
    assert child["env_values"]["GROK_CLAUDE_MCPS_ENABLED"] == "0"


def test_grok_default_home_drops_an_inherited_grok_home(fake, monkeypatch):
    monkeypatch.setenv("GROK_HOME", "/somewhere/else")
    out = fake("grok", "grok.jsonl")
    usage.probe_grok(None)
    assert "GROK_HOME" not in _child(out)["env"]


def test_muse_empty_result_is_ok_with_no_windows(fake):
    fake("muse", "muse-empty.jsonl")
    row = usage.probe_muse()
    assert row == {"ok": True, "plan": None, "windows": [], "version": "1.4.6", "source": "last_observed",
                   "note": "no turn observed yet"}


def test_muse_observed_usage_carries_observed_at(fake):
    fake("muse", "muse-observed.jsonl")
    row = usage.probe_muse()
    assert row["ok"] is True and row["plan"] == "fixture_tier"
    assert row["observed_at"] == "2026-01-01T00:00:00Z"
    assert [(w["name"], w["used_pct"], w.get("window_mins")) for w in row["windows"]] == [
        ("five_hour", 61, 300), ("weekly", 62, 10080)]


def test_probe_environment_carries_no_key_and_no_claudecode(fake, monkeypatch):
    monkeypatch.setenv("META_API_KEY", "fixture-not-a-key")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-not-a-key")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/inherited/home")
    for harness, fixture, probe in (("muse", "muse-empty.jsonl", usage.probe_muse),
                                    ("claude", "claude.jsonl", usage.probe_claude),
                                    ("codex", "codex.jsonl", usage.probe_codex),
                                    ("grok", "grok.jsonl", lambda: usage.probe_grok(None))):
        out = fake(harness, fixture)
        assert probe()["ok"] is True
        env = _child(out)["env"]
        for name in ("META_API_KEY", "CLAUDECODE", "ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR"):
            assert name not in env, (harness, name)


# ---- parallel run, timeout, kill ---------------------------------------------


def test_one_hanging_cli_does_not_delay_the_others(fake):
    hung = fake("claude", "claude.jsonl", mode="hang")
    fake("codex", "codex.jsonl")
    fake("grok", "grok.jsonl")
    fake("muse", "muse-empty.jsonl")
    accounts = [{"label": usage.LABELS[h], "harness": h, "home": None} for h in usage.HARNESSES]
    started = time.monotonic()
    rows = usage.probe_all(accounts, timeout=1.5)
    elapsed = time.monotonic() - started
    assert elapsed < 4.5
    by = {r["harness"]: r for r in rows}
    assert by["claude"]["ok"] is False and by["claude"]["error"] == "timeout"
    assert all(by[h]["ok"] for h in ("codex", "grok", "muse"))
    assert by["codex"]["ms"] < 1500
    pid = _child(hung)["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_cli_that_exits_is_reported_as_a_code_not_its_words(fake):
    fake("grok", "grok.jsonl", mode="exit")
    row = usage.probe_grok(None)
    assert row == {"ok": False, "error": "not_signed_in"}
    fake("grok", "grok.jsonl", mode="secret_exit")
    assert usage.probe_grok(None) == {"ok": False, "error": "exit_7"}


def test_missing_cli_is_reported_not_hidden(fake):
    row = usage.probe_account({"label": "Muse", "harness": "muse", "home": None})
    assert row["ok"] is False and row["error"] == "not_installed"
    assert row["windows"] == [] and row["probed_at"]


# ---- Codex rollout fallback ----------------------------------------------------


def test_codex_falls_back_to_the_newest_rollout(fake, tmp_path):
    fake("codex", "codex.jsonl", mode="exit")
    home = tmp_path / "codex-home"
    day = home / "sessions" / "2026" / "01" / "01"
    day.mkdir(parents=True)
    old = day / "rollout-2026-01-01T09-00-00-old.jsonl"
    old.write_text('{"timestamp": "2026-01-01T09:00:00Z", "type": "event_msg", "payload": {"type": '
                   '"token_count", "rate_limits": {"primary": {"used_percent": 1.0, "window_minutes": 300}}}}\n')
    os.utime(old, (1, 1))
    shutil.copy(FIXTURES / "codex-rollout.jsonl", day / "rollout-2026-01-01T10-00-00-new.jsonl")
    row = usage.probe_codex(str(home))
    assert row["ok"] is True and row["source"] == "rollout"
    assert row["observed_at"] == "2026-01-01T10:00:09Z"
    assert row["plan"] == "pro"
    assert row["windows"] == [{"name": "five_hour", "used_pct": 71.0, "resets_at": "2026-01-01T02:00:00Z",
                               "window_mins": 300, "slot": "primary"}]
    # The note is plain words (the app shows it as written); the code has its own field.
    assert row["live_error"] == "not_signed_in"
    assert not re.search(r"[a-z]+_[a-z0-9_]+|\(", row["note"])
    # A signed-out Codex says so: reading again will not help, signing in will.
    assert row["note"] != usage.ROLLOUT_NOTE
    assert row["note"] == ("Codex is not signed in with this login folder, so these figures are from its last "
                           "turn. Open Codex with this login folder and sign in again")
    assert usage._rollout_note("not_signed_in", None).startswith("Codex is not signed in, so these figures")
    assert usage._rollout_note("timeout", None) == usage.ROLLOUT_NOTE


def test_codex_without_live_answer_or_rollout_fails(fake, tmp_path):
    fake("codex", "codex.jsonl", mode="exit")
    row = usage.probe_codex(str(tmp_path / "empty-home"))
    assert row["ok"] is False and row["error"] == "not_signed_in"


# ---- cache and stale marking -----------------------------------------------------


def _row(harness="claude", label="Claude", *, ok=True, used=10, probed_at=None, **extra):
    probed_at = probed_at or usage._iso(time.time())
    row = {"harness": harness, "account": label, "home": None, "plan": "max", "ok": ok, "error": None,
           "windows": [{"name": "weekly", "used_pct": used, "resets_at": "2026-01-02T00:00:00Z"}] if ok else [],
           "credits": None, "probed_at": probed_at, "ms": 5}
    row.update(extra)
    return row


def _cached(probed_at, used=55, **extra):
    """A cache entry as _store writes it after a good probe."""
    return _row(probed_at=probed_at, used=used, last_attempt_at=probed_at, last_good_at=probed_at,
                last_error=None, **extra)


def _ago(minutes: float) -> str:
    return usage._iso((datetime.now(timezone.utc) - timedelta(minutes=minutes)).timestamp())


def test_cache_max_age_and_fresh(fake, monkeypatch):
    calls = []

    def probe_all(accounts, *, timeout):
        calls.append([a["label"] for a in accounts])
        return [_row(label=a["label"], used=len(calls)) for a in accounts]

    monkeypatch.setattr(usage, "probe_all", probe_all)
    accounts = [{"label": "Claude", "harness": "claude", "home": None}]
    first = usage.report(accounts=accounts)
    assert calls == [["Claude"]] and first["accounts"][0]["cached"] is False
    second = usage.report(accounts=accounts)
    assert calls == [["Claude"]], "inside max age the cache answers"
    assert second["accounts"][0]["cached"] is True and second["accounts"][0]["stale"] is False
    assert second["accounts"][0]["windows"][0]["used_pct"] == 1
    usage.report(accounts=accounts, fresh=True)
    assert len(calls) == 2
    usage.report(accounts=accounts, max_age=0)
    assert len(calls) == 3
    saved = json.loads(usage.cache_file().read_text())
    assert saved["version"] == 2
    assert saved["rows"]["claude|"]["windows"][0]["used_pct"] == 3


def test_failed_probe_keeps_the_last_figures_marked_stale(fake, monkeypatch):
    usage._save_cache({"claude|": _cached(_ago(10))})
    monkeypatch.setattr(usage, "probe_all", lambda accounts, *, timeout: [_row(ok=False, error="timeout")])
    rep = usage.report(accounts=[{"label": "Claude", "harness": "claude", "home": None}])
    row = rep["accounts"][0]
    assert row["ok"] is False and row["error"] == "timeout"
    assert row["windows"][0]["used_pct"] == 55
    assert row["stale"] is True
    assert "[stale]" in rep["summary_text"][0] and "probe failed" in rep["summary_text"][0]


def test_a_held_refresh_lock_keeps_the_last_figures_on_the_foreground_read(fake, monkeypatch):
    # Routing fix round 2, R2-5: with the refresh lock held (a reset check or
    # a background refresh), the foreground report dropped each due
    # account's cached figures and said "Reading for the first time".
    accounts = [{"label": "Codex", "harness": "codex", "home": None},
                {"label": "Claude", "harness": "claude", "home": None}]
    usage._save_cache({"codex|": _cached(_ago(60), harness="codex", account="Codex")})
    monkeypatch.setattr(usage, "_try_refresh_lock", lambda: None)
    monkeypatch.setattr(usage, "probe_all", lambda *_a, **_k: pytest.fail("no probe beside the held lock"))
    rep = usage.report(accounts=accounts)
    codex, claude = rep["accounts"]
    assert rep["refreshing"] is True
    assert codex["windows"][0]["used_pct"] == 55 and codex["error"] is None
    assert codex["stale"] is True and codex["refreshing"] is True and codex["cached"] is True
    assert (claude["error"], claude["refreshing"]) == ("pending", True)
    fresh = usage.report(accounts=accounts, fresh=True)["accounts"][0]
    assert fresh["windows"][0]["used_pct"] == 55 and fresh["refreshing"] is True


def test_old_observation_is_stale_even_after_a_fresh_probe(fake, monkeypatch):
    monkeypatch.setattr(usage, "probe_all", lambda accounts, *, timeout: [
        _row("muse", "Muse", source="last_observed", observed_at="2026-01-01T00:00:00Z")])
    row = usage.report(accounts=[{"label": "Muse", "harness": "muse", "home": None}], fresh=True)["accounts"][0]
    assert row["stale"] is True


# ---- accounts, text, MCP ------------------------------------------------------------


def test_default_accounts_are_the_installed_clis(fake):
    fake("claude", "claude.jsonl")
    fake("grok", "grok.jsonl")
    assert usage.configured_accounts() == [
        {"label": "Claude", "harness": "claude", "home": None},
        {"label": "Grok", "harness": "grok", "home": None}]


def test_configured_accounts_keep_invalid_entries_visible(fake, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": [
        {"label": "Claude second", "harness": "claude", "home": "~/.claude-second"},
        {"label": "Odd", "harness": "gemini"},
        {"label": "Muse home", "harness": "muse", "home": "/x"},
    ]}))
    accounts = usage.configured_accounts()
    assert accounts[0] == {"label": "Claude second", "harness": "claude", "home": "~/.claude-second"}
    assert accounts[1]["invalid"] == ('Khipu does not read the CLI "gemini"; it reads Claude Code, Codex, '
                                      "Grok Build and Muse")
    assert "Muse" in accounts[2]["invalid"]
    row = usage.probe_account(accounts[1])
    assert row["ok"] is False and row["error"] == "invalid_account"
    assert row["note"].startswith("This account entry is not valid: Khipu does not read")
    assert "usage_accounts" not in row["note"]


def test_summary_and_table():
    row = _row(windows=[{"name": "five_hour", "used_pct": 13, "resets_at": "2026-10-14T17:40:00Z"},
                        {"name": "weekly", "used_pct": 34, "resets_at": "2026-10-18T15:00:00Z"}])
    row["stale"] = False
    line = usage.summary_line(row)
    assert line == "Claude (max): 5 h 13%, week 34%; tightest week 34% resets Sun 15:00Z"
    failed = usage.summary_line(_row(ok=False, error="not_installed", plan=None))
    assert failed == "Claude: unavailable: not_installed"
    text = usage.table({"accounts": [row]})
    assert "ACCOUNT" in text and "5 h" in text and "34%" in text


def test_mcp_tool_returns_report_and_declines_on_the_gateway(fake, monkeypatch):
    monkeypatch.setattr(usage, "report", lambda **kw: {"accounts": [], "summary_text": [], "kw": kw})
    out = mcp_server.TOOL_FUNCS["khipu_usage"]({"fresh": True, "max_age": 30})
    assert out["kw"] == {"fresh": True, "max_age": 30.0, "background": True}
    monkeypatch.setenv(mcp_server.GATEWAY_ACTIVE_ENV, "1")
    out = mcp_server.TOOL_FUNCS["khipu_usage"]({})
    assert out["available"] is False


def test_cli_json(fake, monkeypatch, capsys):
    from khipu import cli

    monkeypatch.setattr(usage, "report", lambda **kw: {"accounts": [_row()], "summary_text": ["x"], "kw": kw})
    assert cli.main(["usage", "--json", "--fresh", "--max-age", "60"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["kw"] == {"fresh": True, "max_age": 60.0}


# ---- credential trap ----------------------------------------------------------------

_FORBIDDEN = ("key", "token", "auth.json")


def _forbidden(name) -> bool:
    return any(word in str(name).lower() for word in _FORBIDDEN)


class _RecordingEnviron(dict):
    """Logs every value read by name, and every bulk read (items, values,
    copy, dict(environ)) as ("env-bulk", how). Iterating names is not a read."""

    def __init__(self, data, log):
        super().__init__(data)
        self.log = log

    def __iter__(self):
        return iter(list(super().keys()))

    def keys(self):
        return list(super().keys())

    def items(self):
        self.log.append(("env-bulk", "items"))
        return super().items()

    def values(self):
        self.log.append(("env-bulk", "values"))
        return super().values()

    def copy(self):
        self.log.append(("env-bulk", "copy"))
        return dict(super().items())

    def __getitem__(self, key):
        self.log.append(("env", key))
        return super().__getitem__(key)

    def get(self, key, default=None):
        self.log.append(("env", key))
        return super().get(key, default)

    def __contains__(self, key):
        self.log.append(("env", key))
        return super().__contains__(key)


def test_no_credential_is_read_by_name_at_runtime(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("SERVICE_TOKEN", "fixture-sentinel")
    for harness, fixture in (("claude", "claude.jsonl"), ("grok", "grok.jsonl"), ("muse", "muse-empty.jsonl")):
        fake(harness, fixture)
    fake("codex", "codex.jsonl", mode="exit")  # exercises the rollout fallback's file reads
    home = tmp_path / "codex-home" / "sessions" / "2026"
    home.mkdir(parents=True)
    shutil.copy(FIXTURES / "codex-rollout.jsonl", home / "rollout-x.jsonl")
    log: list[tuple[str, str]] = []
    real_open = builtins.open

    def recording_open(file, *args, **kwargs):
        log.append(("file", str(file)))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(os, "environ", _RecordingEnviron(os.environ, log))
    monkeypatch.setattr(builtins, "open", recording_open)
    monkeypatch.setattr(io, "open", recording_open)
    accounts = [{"label": "C", "harness": "claude", "home": None},
                {"label": "X", "harness": "codex", "home": str(tmp_path / "codex-home")},
                {"label": "G", "harness": "grok", "home": None},
                {"label": "M", "harness": "muse", "home": None}]
    rep = usage.report(accounts=accounts, fresh=True)
    assert all(r["ok"] for r in rep["accounts"]), rep["summary_text"]
    assert any(kind == "file" and "rollout" in name for kind, name in log), "the trap saw the file reads"
    bad = [entry for entry in log if _forbidden(entry[1])]
    assert bad == []
    assert [entry for entry in log if entry[0] == "env-bulk"] == [], "no probe reads the whole environment"


def test_the_trap_catches_bulk_environment_reads(monkeypatch):
    from khipu import synth_cli

    monkeypatch.setenv("SERVICE_TOKEN", "fixture-sentinel")
    log: list[tuple[str, str]] = []
    monkeypatch.setattr(os, "environ", _RecordingEnviron(os.environ, log))
    synth_cli.environment()  # the summariser's copy-then-drop builder
    os.environ.copy()
    dict(os.environ)
    assert ("env-bulk", "items") in log and ("env-bulk", "copy") in log
    assert ("env", "SERVICE_TOKEN") in log, "dict(environ) reads every value by name"


def test_usage_source_reads_no_credential_by_name():
    tree = ast.parse((Path(usage.__file__)).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "auth.json" not in node.value
        if isinstance(node, ast.Call):
            func = ast.unparse(node.func)
            if func in ("os.environ.get", "os.getenv", "os.environ.__getitem__", "open") or \
                    func.endswith((".read_text", ".read_bytes")):
                for arg in node.args:
                    assert not _forbidden(ast.unparse(arg)), ast.unparse(node)
        if isinstance(node, ast.Subscript) and ast.unparse(node.value) == "os.environ":
            assert not _forbidden(ast.unparse(node.slice)), ast.unparse(node)


# ---- fix round (Codex cross-family review, 2026-10-14) ---------------------------


def _jsonl(path: Path, frames: list[dict]) -> Path:
    path.write_text("".join(json.dumps(f) + "\n" for f in frames))
    return path


def _gone(pid: int, wait_s: float = 3.0) -> bool:
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


_CLAUDE_INIT = {"type": "control_response", "response": {"subtype": "success", "request_id": "init",
                                                         "response": {"claude_code_version": "2.1.296"}}}
_GROK_INIT = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1, "_meta": {"agentVersion": "1.0.52"}}}
_MUSE_INIT = {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "muse", "version": "1.4.6"}}}


def test_secret_bearing_cli_text_never_reaches_any_output(fake, tmp_path, monkeypatch, capsys):
    from khipu import cli

    grok = _jsonl(tmp_path / "grok-secret.jsonl", [
        _GROK_INIT,
        {"jsonrpc": "2.0", "method": "_x.ai/mcp/servers_updated",
         "params": {"mcpServers": [{"name": "x", "env": {"SERVICE_TOKEN": SENTINEL}}]}},
        {"jsonrpc": "2.0", "id": 2, "error": {"code": -32000, "message": f"boom {SENTINEL}"}}])
    claude = _jsonl(tmp_path / "claude-secret.jsonl", [
        _CLAUDE_INIT,
        {"type": "control_response", "response": {"subtype": "error", "request_id": "usage",
                                                  "error": f"failed: {SENTINEL}"}}])
    fake("grok", grok)
    fake("claude", claude)
    fake("muse", "muse-empty.jsonl", mode="secret_exit")
    fake("codex", "codex.jsonl", mode="secret_exit")
    codex_home = tmp_path / "codex-home" / "sessions"
    codex_home.mkdir(parents=True)
    shutil.copy(FIXTURES / "codex-rollout.jsonl", codex_home / "rollout-x.jsonl")
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": [
        {"label": "Claude", "harness": "claude"}, {"label": "Grok", "harness": "grok"},
        {"label": "Muse", "harness": "muse"},
        {"label": "Codex", "harness": "codex", "home": str(tmp_path / "codex-home")}]}))

    assert cli.main(["usage", "--json", "--fresh"]) == 0
    printed = capsys.readouterr().out
    by = {r["harness"]: r for r in json.loads(printed)["accounts"]}
    assert by["grok"]["error"] == "cli_error"
    assert by["claude"]["error"] == "cli_error"
    assert by["muse"]["error"] == "exit_7"
    assert by["codex"]["ok"] is True and by["codex"]["note"] == usage.ROLLOUT_NOTE
    assert by["codex"]["live_error"] == "exit_7"
    assert cli.main(["usage", "--fresh"]) == 0
    printed += capsys.readouterr().out
    mcp_out = mcp_server.TOOL_FUNCS["khipu_usage"]({"fresh": True})
    assert usage.wait_for_refresh(20)
    mcp_out2 = mcp_server.TOOL_FUNCS["khipu_usage"]({})
    for text in (printed, json.dumps(mcp_out), json.dumps(mcp_out2), usage.cache_file().read_text()):
        assert SENTINEL not in text


def test_probe_environment_is_an_allowlist(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("SERVICE_TOKEN", "fixture-sentinel")
    monkeypatch.setenv("UNLISTED_THING", "x")
    monkeypatch.setenv("LC_ALL", "C")
    env = usage.probe_environment("grok", str(tmp_path / "g"))
    allowed = set(usage._ENV_ALLOW) | {"GROK_HOME", *usage._GROK_ENV}
    assert {k for k in env if not k.startswith("LC_")} <= allowed
    assert env["LC_ALL"] == "C" and env["GROK_HOME"] == str(tmp_path / "g")
    assert set(usage.probe_environment("codex")) - set(usage._ENV_ALLOW) == {"LC_ALL"} | (
        {k for k in os.environ if k.startswith("LC_")})
    for harness, fixture, probe in (("claude", "claude.jsonl", usage.probe_claude),
                                    ("grok", "grok.jsonl", lambda: usage.probe_grok(None))):
        out = fake(harness, fixture)
        assert probe()["ok"] is True
        child = _child(out)["env"]
        assert "SERVICE_TOKEN" not in child and "UNLISTED_THING" not in child, harness


LAUNCHD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def _npm_shim(tmp_path, fixture: str, out: Path, prefix: Path | None = None, name: str = "codex") -> Path:
    """An npm-style install: ``prefix/bin/<name>`` links to a
    ``#!/usr/bin/env node`` script, and node sits beside the link (nvm's
    layout). This node is a shell script running the fake CLI's Python."""
    prefix = prefix or tmp_path / "npm-prefix"
    package = prefix / "lib" / "node_modules" / "codex-fixture" / "bin"
    package.mkdir(parents=True)
    (prefix / "bin").mkdir()
    node = prefix / "bin" / "node"
    node.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    node.chmod(0o755)
    script = package / "codex.js"
    script.write_text("#!/usr/bin/env node\n" + textwrap.dedent(f"""
        import runpy, sys
        sys.argv = [{str(tmp_path / "fake_cli.py")!r}, {str(FIXTURES / fixture)!r}, "answer", {str(out)!r},
                    *sys.argv[1:]]
        runpy.run_path(sys.argv[0], run_name="__main__")
    """))
    script.chmod(0o755)
    shim = prefix / "bin" / name
    shim.symlink_to(Path("..") / "lib" / "node_modules" / "codex-fixture" / "bin" / "codex.js")
    return shim


def test_a_node_shim_answers_under_the_bare_launchd_path(fake, tmp_path, monkeypatch):
    """Khipu.app runs under launchd's PATH, where ``/usr/bin/env node`` finds
    no node: an npm-installed Codex exited 127 on every probe from the app
    while ``khipu usage`` in Terminal worked."""
    out = tmp_path / "codex-child.json"
    shim = _npm_shim(tmp_path, "codex.jsonl", out)
    monkeypatch.setattr(usage, "_command", lambda harness: str(shim) if harness == "codex" else None)
    monkeypatch.setenv("PATH", LAUNCHD_PATH)
    row = usage.probe_codex()
    assert row["ok"] is True and row["source"] == "live", row
    assert row["plan"] == "pro"
    assert _child(out)["argv"] == ["app-server"]


def _launchd_lookup(monkeypatch, home: Path):
    """``synth_cli.binary`` as Khipu.app runs it: launchd's PATH, ``home`` as
    HOME, no override, and of the usual install folders only those under HOME
    (so a CLI this Mac has in /opt/homebrew or /Applications never answers)."""
    from khipu import synth_cli

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", LAUNCHD_PATH)
    for harness in usage.HARNESSES:
        monkeypatch.delenv(f"KHIPU_{harness.upper()}_CLI", raising=False)
    monkeypatch.setattr(synth_cli, "_CANDIDATES", {h: tuple(c for c in v if c.startswith("~"))
                                                   for h, v in synth_cli._CANDIDATES.items()})
    return synth_cli


def _stub(path: Path, mode: int = 0o755) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 9\n")
    path.chmod(mode)
    return path


def test_an_nvm_cli_is_found_and_answers_under_the_bare_launchd_path(tmp_path, monkeypatch):
    """Khipu.app's PATH names no Node version manager folder, so a Claude Code
    installed with ``npm i -g`` under nvm was left out of plan usage. The
    newest Node's copy wins: v22 over v20, and over v9, which a sort by text
    would put first. Its node, beside it, answers through ``_probe_path``."""
    (tmp_path / "fake_cli.py").write_text(FAKE_CLI)
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    home = tmp_path / "home"
    versions = home / ".nvm" / "versions" / "node"
    for older in ("v20.11.1", "v9.11.2"):
        _stub(versions / older / "bin" / "claude")
    out = tmp_path / "claude-child.json"
    shim = _npm_shim(tmp_path, "claude.jsonl", out, prefix=versions / "v22.0.0", name="claude")
    synth_cli = _launchd_lookup(monkeypatch, home)
    assert synth_cli.binary("claude") == str(shim)
    assert usage.configured_accounts() == [{"label": "Claude", "harness": "claude", "home": None}]
    assert str(shim.parent) in usage.probe_environment("claude", None, str(shim))["PATH"].split(os.pathsep)
    row = usage.probe_claude()
    assert row["ok"] is True and row["plan"] == "max", row
    assert _child(out)["argv"][:1] == ["-p"]


def test_a_node_manager_match_that_is_not_executable_is_skipped(tmp_path, monkeypatch):
    synth_cli = _launchd_lookup(monkeypatch, tmp_path)
    _stub(tmp_path / ".nvm" / "versions" / "node" / "v22.0.0" / "bin" / "codex", 0o644)
    (tmp_path / ".volta" / "bin" / "codex").mkdir(parents=True)  # a folder by the CLI's name
    assert synth_cli.binary("codex") is None
    older = _stub(tmp_path / ".nvm" / "versions" / "node" / "v20.11.1" / "bin" / "codex")
    assert synth_cli.binary("codex") == str(older)
    for folder in (".volta/bin", "Library/pnpm", ".bun/bin", ".local/share/fnm/node-versions/v24.1.0/installation/bin",
                   ".asdf/shims"):
        found = _stub(tmp_path / folder / "grok")
        assert synth_cli.binary("grok") == str(found), folder
        found.chmod(0o644)
    assert synth_cli.binary("grok") is None


def test_probe_path_keeps_the_callers_own_and_adds_the_cli_folders(tmp_path, monkeypatch):
    out = tmp_path / "unused.json"
    (tmp_path / "fake_cli.py").write_text(FAKE_CLI)
    shim = _npm_shim(tmp_path, "codex.jsonl", out)
    monkeypatch.setenv("HOME", str(tmp_path / "owner"))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    path = usage.probe_environment("codex", None, str(shim))["PATH"].split(os.pathsep)
    assert path[:2] == ["/usr/bin", "/bin"], "the caller's own PATH comes first"
    assert path[2] == str(shim.parent)
    assert path[3] == os.path.dirname(os.path.realpath(shim))
    for folder in ("/opt/homebrew/bin", "/usr/local/bin", str(tmp_path / "owner" / ".npm-global" / "bin"),
                   str(tmp_path / "owner" / ".local" / "bin"), "/usr/sbin", "/sbin"):
        assert folder in path, folder
    assert len(path) == len(set(path)), "each folder once"
    # With no PATH at all (and no binary) the child still gets the usual folders.
    monkeypatch.delenv("PATH")
    assert usage.probe_environment("muse")["PATH"].split(os.pathsep)[:2] == ["/opt/homebrew/bin", "/usr/local/bin"]


def test_every_probe_runs_in_a_fresh_empty_temp_cwd(fake):
    for harness, fixture, probe in (("claude", "claude.jsonl", usage.probe_claude),
                                    ("codex", "codex.jsonl", usage.probe_codex),
                                    ("grok", "grok.jsonl", lambda: usage.probe_grok(None)),
                                    ("muse", "muse-empty.jsonl", usage.probe_muse)):
        out = fake(harness, fixture)
        assert probe()["ok"] is True
        child = _child(out)
        assert child["cwd_entries"] == [], harness
        assert Path(child["cwd"]).name.startswith("khipu-usage-"), harness
        assert not Path(child["cwd"]).exists(), "the temp cwd is removed afterwards"


def test_a_grandchild_holding_stdout_is_killed(fake):
    out = fake("claude", "claude.jsonl", mode="grandchild")
    started = time.monotonic()
    row = usage.probe_claude(timeout=5)
    assert row == {"ok": False, "error": "no_answer"}
    assert time.monotonic() - started < 3, "the CLI's exit is noticed though a grandchild holds stdout"
    assert _gone(_child(out)["grandchild"])


def test_the_deadline_kills_the_group_with_its_grandchildren(fake):
    out = fake("grok", "grok.jsonl", mode="grandchild_hang")
    row = usage.probe_grok(None, timeout=1)
    assert row == {"ok": False, "error": "timeout"}
    child = _child(out)
    assert _gone(child["pid"]) and _gone(child["grandchild"])


def test_held_open_stdin_does_not_hold_the_probe(fake, tmp_path):
    out = fake("codex", "codex.jsonl", mode="deaf")  # never reads stdin, which stays open for Codex
    started = time.monotonic()
    row = usage.probe_codex(str(tmp_path / "no-sessions"), timeout=1)
    assert row == {"ok": False, "error": "timeout"}
    assert time.monotonic() - started < 2
    assert _gone(_child(out)["pid"])


def test_probe_all_returns_by_its_overall_deadline(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(usage, "OVERALL_GRACE_S", 0.3)
    monkeypatch.setattr(usage, "probe_account", lambda account, *, timeout: release.wait(30) and {})
    started = time.monotonic()
    rows = usage.probe_all([{"label": "Claude", "harness": "claude", "home": None}], timeout=0.2)
    release.set()
    assert time.monotonic() - started < 1.5
    assert rows[0]["ok"] is False and rows[0]["error"] == "timeout"


def _rollout_line(used: float, ts: str = "2026-01-01T10:00:00Z") -> str:
    return json.dumps({"timestamp": ts, "type": "event_msg", "payload": {
        "type": "token_count", "rate_limits": {"primary": {"used_percent": used, "window_minutes": 300}}}}) + "\n"


def test_codex_fallback_is_bounded(tmp_path):
    day = tmp_path / "home" / "sessions" / "2026" / "01" / "01"
    day.mkdir(parents=True)
    filler = ('{"type": "event_msg", "payload": {"type": "agent_message", "message": "' + "x" * 200 + '"}}\n')
    huge = day / "rollout-huge.jsonl"
    with huge.open("w") as fh:
        fh.write(_rollout_line(1.0))
        fh.write(filler * 40_000)  # ~9 MB
        fh.write(_rollout_line(71.0, "2026-01-01T11:00:00Z"))
    started = time.monotonic()
    row = usage.codex_rollout(str(tmp_path / "home"))
    assert time.monotonic() - started < 1
    assert row["windows"][0]["used_pct"] == 71.0
    # Only the tail is read: a figure outside the last 256 KiB is never seen.
    with huge.open("w") as fh:
        fh.write(_rollout_line(1.0))
        fh.write(filler * 40_000)
    assert usage.codex_rollout(str(tmp_path / "home")) is None
    # Only the five newest files by mtime are read.
    older = day / "rollout-older.jsonl"
    older.write_text(_rollout_line(5.0))
    os.utime(older, (1, 1))
    for n in range(5):
        (day / f"rollout-new-{n}.jsonl").write_text(filler)
    assert usage.codex_rollout(str(tmp_path / "home")) is None
    os.utime(older, None)
    assert usage.codex_rollout(str(tmp_path / "home"))["windows"][0]["used_pct"] == 5.0
    # A spent deadline gives up at once.
    assert usage.codex_rollout(str(tmp_path / "home"), deadline=time.monotonic() - 1) is None


def test_mcp_tool_answers_from_the_cache_while_a_refresh_hangs(fake, monkeypatch):
    usage._save_cache({"claude|": _cached(_ago(10))})
    release = threading.Event()
    calls = []

    def slow(accounts, *, timeout):
        calls.append([a["label"] for a in accounts])
        release.wait(10)
        return [_row(used=77) for _ in accounts]

    monkeypatch.setattr(usage, "probe_all", slow)
    monkeypatch.setattr(usage, "configured_accounts",
                        lambda: [{"label": "Claude", "harness": "claude", "home": None}])
    started = time.monotonic()
    out = mcp_server.TOOL_FUNCS["khipu_usage"]({})
    assert time.monotonic() - started < 0.5
    row = out["accounts"][0]
    assert out["refreshing"] is True and row["cached"] is True and row["stale"] is True
    assert row["windows"][0]["used_pct"] == 55
    mcp_server.TOOL_FUNCS["khipu_usage"]({"fresh": True})
    assert calls == [["Claude"]], "one refresh at a time"
    release.set()
    assert usage.wait_for_refresh(5)
    after = mcp_server.TOOL_FUNCS["khipu_usage"]({})
    assert after["refreshing"] is False
    assert after["accounts"][0]["windows"][0]["used_pct"] == 77 and after["accounts"][0]["stale"] is False


def test_mcp_tool_marks_a_never_probed_account_pending(fake, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(usage, "probe_all", lambda accounts, *, timeout: release.wait(10) and [])
    out = usage.report(accounts=[{"label": "Grok", "harness": "grok", "home": None}], background=True)
    release.set()
    assert out["accounts"][0]["error"] == "pending" and out["accounts"][0]["refreshing"] is True
    assert "first probe running" in out["summary_text"][0]
    usage.wait_for_refresh(5)


def test_a_refresh_held_elsewhere_is_not_duplicated(fake, monkeypatch):
    import fcntl

    calls = []
    monkeypatch.setattr(usage, "probe_all", lambda accounts, *, timeout: calls.append(1) or [])
    lock = usage.cache_file().with_name(usage.REFRESH_LOCK_NAME)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # another process's refresh
        out = usage.report(accounts=[{"label": "Claude", "harness": "claude", "home": None}], background=True)
        assert out["refreshing"] is True and calls == []
    finally:
        os.close(fd)


def test_a_changed_home_is_a_different_account(fake, monkeypatch, tmp_path):
    usage._save_cache({f"claude|{tmp_path / 'a'}": _cached(usage._iso(time.time()))})
    calls = []
    monkeypatch.setattr(usage, "probe_all", lambda accounts, *, timeout: calls.append(
        [a["home"] for a in accounts]) or [_row(used=9, home=a["home"]) for a in accounts])
    same = usage.report(accounts=[{"label": "Renamed", "harness": "claude", "home": str(tmp_path / "a")}])
    assert calls == [] and same["accounts"][0]["cached"] is True and same["accounts"][0]["account"] == "Renamed"
    moved = usage.report(accounts=[{"label": "Renamed", "harness": "claude", "home": str(tmp_path / "b")}])
    assert calls == [[str(tmp_path / "b")]]
    assert moved["accounts"][0]["windows"][0]["used_pct"] == 9


def test_repeated_failures_keep_the_last_good_figures(fake, monkeypatch):
    good_at = _ago(1)
    usage._save_cache({"claude|": _cached(good_at)})
    monkeypatch.setattr(usage, "probe_all", lambda accounts, *, timeout: [
        dict(_row(ok=False, error="timeout"), probed_at=usage._iso(time.time()))])
    account = [{"label": "Claude", "harness": "claude", "home": None}]
    for _ in range(3):
        row = usage.report(accounts=account, fresh=True)["accounts"][0]
        assert row["windows"][0]["used_pct"] == 55
        assert row["probed_at"] == good_at and row["last_good_at"] == good_at
        assert row["ok"] is False and row["last_error"] == "timeout" and row["stale"] is True
        assert row["last_attempt_at"] >= good_at
    saved = json.loads(usage.cache_file().read_text())["rows"]["claude|"]
    assert saved["windows"][0]["used_pct"] == 55 and saved["probed_at"] == good_at


@pytest.mark.parametrize("harness,frames", [
    ("claude", [_CLAUDE_INIT, {"type": "control_response", "response": []}]),
    ("claude", [_CLAUDE_INIT, {"type": "control_response", "response": {
        "subtype": "success", "request_id": "usage", "response": []}}]),
    ("claude", [_CLAUDE_INIT, {"type": "control_response", "response": {
        "subtype": "success", "request_id": "usage", "response": {"rate_limits": {"five_hour": "x"}}}}]),
    ("claude", [_CLAUDE_INIT, {"type": "control_response", "response": {
        "subtype": "success", "request_id": "usage",
        "response": {"rate_limits": {"five_hour": {"utilization": "16"}}}}}]),
    ("claude", [_CLAUDE_INIT, {"type": "control_response", "response": {
        "subtype": "success", "request_id": "usage", "response": {"rate_limits": {"model_scoped": {}}}}}]),
    ("grok", [_GROK_INIT, {"jsonrpc": "2.0", "id": 2, "result": {}}]),
    ("grok", [_GROK_INIT, {"jsonrpc": "2.0", "id": 2, "result": {"config": {"currentPeriod": []}}}]),
    ("grok", [_GROK_INIT, {"jsonrpc": "2.0", "id": 2, "result": []}]),
    ("muse", [_MUSE_INIT, {"jsonrpc": "2.0", "id": 2, "result": {"usage": "unexpected"}}]),
    ("muse", [_MUSE_INIT, {"jsonrpc": "2.0", "id": 2, "result": {"usage": {"window": 5}}}]),
    ("codex", [{"id": 1, "result": {}}, {"id": 2, "result": {"rateLimits": {"primary": []}}}]),
    ("codex", [{"id": 1, "result": {}}, {"id": 2, "result": {"rateLimits": None}}]),
    ("codex", [{"id": 1, "result": {}}, {"id": 2, "result": {"rateLimits": {"credits": "lots"}}}]),
])
def test_malformed_nested_replies_are_bad_reply(fake, tmp_path, harness, frames):
    fake(harness, _jsonl(tmp_path / f"{harness}-bad.jsonl", frames))
    probe = {"claude": usage.probe_claude, "grok": lambda: usage.probe_grok(None), "muse": usage.probe_muse,
             "codex": lambda: usage.probe_codex(str(tmp_path / "no-sessions"))}[harness]
    assert probe() == {"ok": False, "error": "bad_reply"}


def test_legitimate_absence_stays_ok(fake, tmp_path):
    fake("muse", _jsonl(tmp_path / "muse-empty-usage.jsonl",
                        [_MUSE_INIT, {"jsonrpc": "2.0", "id": 2, "result": {"usage": {}}}]))
    assert usage.probe_muse()["ok"] is True
    fake("codex", _jsonl(tmp_path / "codex-none.jsonl", [
        {"id": 1, "result": {}}, {"id": 2, "result": {"rateLimits": {"primary": None, "secondary": None}}}]))
    row = usage.probe_codex(str(tmp_path / "no-sessions"))
    assert row["ok"] is True and row["windows"] == []
    fake("grok", _jsonl(tmp_path / "grok-nopct.jsonl", [_GROK_INIT, {"jsonrpc": "2.0", "id": 2,
                                                                     "result": {"config": {}}}]))
    row = usage.probe_grok(None)
    assert row["ok"] is True and row["windows"] == []


# ---- usage_accounts: settable from the app, a bad home never crashes -----------

NO_USER_HOME = "~khipu-no-such-user-7f3a/.claude"


def _config_cli(argv, capsys):
    """``khipu config`` as the app runs it: a write to usage_accounts is the
    owner's (decision 15), so it carries ``--from-app`` and each test that
    makes one takes the ``khipu_app`` fixture (the app as its caller)."""
    from khipu.cli import main

    owner = ["--from-app"] if "usage_accounts" in argv else []
    rc = main(["config", *owner, *argv])
    return rc, json.loads(capsys.readouterr().out)


def test_a_home_naming_no_user_is_an_invalid_row_not_a_crash(fake, tmp_path):
    fake("claude", "claude.jsonl")
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "config.json").write_text(json.dumps({"usage_accounts": [
        {"label": "Claude", "harness": "claude"},
        {"label": "Typo", "harness": "claude", "home": NO_USER_HOME},
    ]}))
    rep = usage.report(fresh=True)
    rows = {r["account"]: r for r in rep["accounts"]}
    assert rows["Claude"]["ok"] is True
    assert rows["Typo"]["ok"] is False and rows["Typo"]["error"] == "invalid_account"
    assert f"Khipu cannot find the login folder {NO_USER_HOME}; start it with ~/" in rows["Typo"]["note"]
    # Neither the cache key nor the probe environment raises on such a home.
    assert usage._key({"harness": "claude", "home": NO_USER_HOME}) == f"claude|{NO_USER_HOME}"
    assert usage.probe_environment("claude", NO_USER_HOME)["CLAUDE_CONFIG_DIR"] == NO_USER_HOME


def test_a_relative_login_folder_is_an_invalid_entry_never_probed(fake, tmp_path):
    """A hand-edited ".claude-second" was listed as valid and probed inside a
    fresh temp folder, so it always read "not signed in" and asked the owner
    to sign in again, which could never fix it."""
    out = fake("claude", "claude.jsonl")
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "config.json").write_text(json.dumps({"usage_accounts": [
        {"label": "Second", "harness": "claude", "home": ".claude-second"},
        {"label": "Work", "harness": "codex", "home": "codex-work"},
    ]}))
    accounts = {a["label"]: a for a in usage.configured_accounts()}
    assert accounts["Second"]["invalid"].startswith("Khipu cannot find the login folder .claude-second; start it with ~/")
    assert accounts["Work"]["invalid"].startswith("Khipu cannot find the login folder codex-work")
    rows = {r["account"]: r for r in usage.report(fresh=True)["accounts"]}
    assert rows["Second"]["ok"] is False and rows["Second"]["error"] == "invalid_account"
    assert rows["Second"]["note"].startswith("This account entry is not valid: Khipu cannot find the login folder")
    assert not out.exists(), "an invalid entry is never probed"


def test_usage_accounts_are_set_shown_and_unset_through_config(fake, capsys, khipu_app):
    from khipu import config

    fake("claude", "claude.jsonl")
    fake("grok", "grok.jsonl")
    rc, out = _config_cli(["--set", "usage_accounts", json.dumps([
        {"label": " Claude second ", "harness": "Claude", "home": "~/.claude-second"},
        {"label": "Grok", "harness": "grok", "home": None},
    ])], capsys)
    assert rc == 0 and out["ok"] is True
    stored = json.loads(config.config_file().read_text())["usage_accounts"]
    assert stored == [{"label": "Claude second", "harness": "claude", "home": "~/.claude-second"},
                      {"label": "Grok", "harness": "grok"}]
    assert out["usage_accounts"]["source"] == "file"
    assert [a["label"] for a in out["usage_accounts"]["accounts"]] == ["Claude second", "Grok"]
    rc, shown = _config_cli([], capsys)
    assert rc == 0 and shown["usage_accounts"]["value"] == stored
    rc, out = _config_cli(["--unset", "usage_accounts"], capsys)
    assert rc == 0 and out["usage_accounts"] == {
        "value": None, "source": "default",
        "accounts": [{"label": "Claude", "harness": "claude", "home": None},
                     {"label": "Grok", "harness": "grok", "home": None}],
        "user_home": str(Path.home()), "version": config.accounts_version(), "none": False, "request": None}
    assert "usage_accounts" not in json.loads(config.config_file().read_text())
    # An empty list is the default too.
    assert _config_cli(["--set", "usage_accounts", json.dumps([{"harness": "codex"}])], capsys)[0] == 0
    rc, out = _config_cli(["--set", "usage_accounts", "[]"], capsys)
    assert rc == 0 and out["usage_accounts"]["source"] == "default"


_TWO = [{"label": "Claude", "harness": "claude"}, {"label": "Codex", "harness": "codex"}]
# (value, words in the CLI's error, the refusal's code, index, field, other).
_REFUSALS = [
    ("not json", "must be JSON", "bad_list", None, None, None),
    ('{"harness": "claude"}', "must be a list", "bad_list", None, None, None),
    ('["claude"]', "entry 1 is not an object", "bad_entry", 0, None, None),
    ('[{"harness": "cluade"}]', 'Khipu does not read the CLI "cluade"', "unknown_cli", 0, "harness", None),
    ('[{"harness": "muse", "home": "/x"}]', "Muse has one sign-in", "muse_folder", 0, "home", None),
    ('[{"harness": "claude", "token": "x"}]', "unknown field 'token'", "bad_entry", 0, None, None),
    ('[{"harness": "claude", "home": 3}]', "home must be text", "not_one_line", 0, "home", None),
    ('[{"harness": "claude", "label": "a\\nb"}]', "one line of plain text", "not_one_line", 0, "label", None),
    # The app's own entries come after the defaults it lists: the index is the entry's own.
    (json.dumps([*_TWO, {"label": "Typo", "harness": "codex", "home": "~.codex-work"}]),
     "Khipu cannot find the login folder ~.codex-work", "no_such_folder", 2, "home", None),
    (json.dumps([{"harness": "claude", "home": NO_USER_HOME}]), "cannot find the login folder", "no_such_folder",
     0, "home", None),
    # A relative folder (the "~/" left off) resolves inside each probe's fresh temp folder: never readable.
    (json.dumps([{"harness": "claude", "home": ".claude-second"}]),
     "Khipu cannot find the login folder .claude-second; start it with ~/", "no_such_folder", 0, "home", None),
    (json.dumps([*_TWO, {"label": "Work", "harness": "codex", "home": "codex-work"}]),
     "Khipu cannot find the login folder codex-work", "no_such_folder", 2, "home", None),
    ('[{"harness": "claude"}, {"label": "claude", "harness": "codex"}]', "two usage_accounts entries are named",
     "name_taken", 1, "label", 0),
    ('[{"label": "A", "harness": "claude", "home": "/x"}, {"label": "B", "harness": "claude", "home": "/x/"}]',
     "reads the same login", "login_taken", 1, "home", 0),
    (json.dumps([*_TWO, {"label": "Work", "harness": "codex", "home": "~/.codex-work"},
                 {"label": "Work again", "harness": "codex", "home": str(Path.home() / ".codex-work")}]),
     "reads the same login", "login_taken", 3, "home", 2),
    (json.dumps([*_TWO, {"harness": "claude", "label": "x" * 65}]), "longer than 64", "too_long", 2, "label", None),
    (json.dumps([{"harness": "claude", "home": "/" + "x" * 1024}]), "longer than 1024", "too_long", 0, "home", None),
    (json.dumps([{"harness": "claude", "label": f"c{i}"} for i in range(33)]), "at most 32", "too_many", None, None,
     None),
]


@pytest.mark.parametrize("value, needle, code, index, field, other", _REFUSALS,
                         ids=[f"{row[2]}-{n}" for n, row in enumerate(_REFUSALS)])
def test_usage_accounts_refuses_what_cannot_be_probed(fake, capsys, khipu_app, value, needle, code, index, field,
                                                      other):
    from khipu import config

    rc, out = _config_cli(["--set", "usage_accounts", value], capsys)
    assert rc == 2 and out["ok"] is False and needle in out["error"]
    # A fixed code with the entry and field at fault, so the app can say it in
    # its own words and point at the field instead of showing this text.
    assert (out["code"], out["index"], out["field"], out["other"]) == (code, index, field, other)
    assert not config.config_file().exists()


def test_every_refusal_code_has_a_sentence_in_the_app():
    """Settings turns each refusal code into its own sentence (PlansCard.tsx
    ACCOUNT_REFUSAL); a code added here without one would read as the
    generic "could not save"."""
    app = Path(__file__).resolve().parents[3] / "apps" / "desktop" / "src" / "PlansCard.tsx"
    if not app.is_file():
        pytest.skip("desktop sources not in this checkout")
    block = app.read_text(encoding="utf-8").split("const ACCOUNT_REFUSAL", 1)[1].split("\n};", 1)[0]
    codes = {row[2] for row in _REFUSALS}
    missing = sorted(code for code in codes if f"  {code}: " not in block)
    assert not missing, f"no sentence in PlansCard.tsx for {missing}"


def test_every_error_code_has_a_plain_phrase_in_the_app():
    """The desktop shows each fixed code as a plain phrase (PlansCard.tsx
    ERROR_PHRASE); a code added here without one would read "couldn't read"."""
    app = Path(__file__).resolve().parents[3] / "apps" / "desktop" / "src" / "PlansCard.tsx"
    if not app.is_file():
        pytest.skip("desktop sources not in this checkout")
    block = app.read_text(encoding="utf-8").split("const ERROR_PHRASE", 1)[1].split("};", 1)[0]
    codes = {value for name, value in vars(usage).items() if name.startswith("E_") and isinstance(value, str)}
    assert codes, "no E_ codes found"
    missing = sorted(code for code in codes if f"  {code}: " not in block)
    assert not missing, f"no plain phrase in PlansCard.tsx for {missing}"
    # exit_<n> / signal_<n> are matched by pattern there.
    assert "(exit|signal)_" in app.read_text(encoding="utf-8")


def test_usage_accounts_without_the_owner_is_refused_and_stores_nothing(fake, capsys, khipu_app):
    """Decision 15: no conversation sets the account list by passing a flag;
    without --from-app from the app itself, or a typed yes, nothing is
    written."""
    from khipu import config
    from khipu.cli import main

    value = json.dumps([{"label": "Claude", "harness": "claude"}])
    assert main(["config", "--set", "usage_accounts", value]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["code"] == "owner_only"
    khipu_app.as_shell("/usr/local/bin/claude")
    assert main(["config", "--from-app", "--set", "usage_accounts", value]) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "owner_only"
    assert main(["config", "--from-app", "--unset", "usage_accounts"]) == 2
    capsys.readouterr()
    assert not config.config_file().exists()


# ---- notes on kept figures, plain words only ---------------------------------------


# Words an owner who is not technical should not meet in a note the app shows.
# PATH in capitals only: "the full path" is plain English, "PATH" is not.
_JARGON = re.compile(r"\b(?:(?-i:PATH)|home|homes|token|tokens|probe|probes|probing|harness|setup)\b|usage_accounts",
                     re.I)


def _every_note() -> list[str]:
    codes = [value for name, value in vars(usage).items() if name.startswith("E_") and isinstance(value, str)]
    notes = [usage.ROLLOUT_NOTE]
    for code in codes:
        for harness in usage.HARNESSES:
            for home in (None, "~/.x"):
                notes += [usage._note(code, harness, home), usage._rollout_note(code, home)]
    notes += [usage._account_from_entry(entry)["invalid"] for entry in (
        "claude", {"harness": "gemini"}, {"harness": ""}, {"harness": "muse", "home": "/x"},
        {"harness": "claude", "home": NO_USER_HOME}, {"harness": "claude", "home": ".claude-second"})]
    return [note for note in notes if note]


def test_no_note_khipu_writes_carries_a_code():
    """The app shows a row's note as written: no snake_case code and no
    jargon (home, PATH, token, probe) in any."""
    notes = _every_note()
    assert len(notes) > 20
    for note in notes:
        assert not re.search(r"\b[a-z]+_[a-z0-9_]+\b", note), note
        assert not _JARGON.search(note.replace(NO_USER_HOME, "")), note
    assert usage._note("not_installed", "grok") == "Grok Build is not installed on this Mac"
    assert usage._note("not_signed_in", "claude", "~/.claude-second") == (
        "Claude Code is not signed in with this login folder. Open Claude Code with this login folder and sign in "
        "again")
    assert usage._note("no_plan_limits", "claude", "~/.claude-t3-second") == (
        "This sign-in reports no plan limits. Open Claude Code with this login folder and sign in again (/login)")


def test_a_failed_probe_row_carries_the_plain_note_for_its_account(fake):
    (Path.home() / ".grok-work").mkdir(exist_ok=True)  # a missing folder is its own note (9B)
    row = usage.probe_account({"label": "Grok work", "harness": "grok", "home": "~/.grok-work"})
    assert row["ok"] is False and row["error"] == "not_installed"
    assert row["note"] == "Grok Build is not installed on this Mac"


def test_a_failed_probe_shows_its_own_note_on_the_kept_figures(fake, monkeypatch):
    usage._save_cache({"claude|": _cached(_ago(60 * 24), note=None)})
    failed = dict(_row(ok=False, error="no_plan_limits"), note=usage._note("no_plan_limits", "claude"))
    monkeypatch.setattr(usage, "probe_all", lambda accounts, *, timeout: [failed])
    row = usage.report(accounts=[{"label": "Claude", "harness": "claude", "home": None}], fresh=True)["accounts"][0]
    assert row["ok"] is False and row["windows"][0]["used_pct"] == 55
    assert row["note"] == usage._note("no_plan_limits", "claude") and "/login" in row["note"]
    saved = json.loads(usage.cache_file().read_text())["rows"]["claude|"]
    assert saved["note"] == usage._note("no_plan_limits", "claude")


def test_a_fallback_note_does_not_outlive_its_good_read(fake, monkeypatch):
    usage._save_cache({"codex|": _cached(_ago(5), harness="codex", label="Codex", source="rollout",
                                         note=usage.ROLLOUT_NOTE, live_error="exit_1")})
    monkeypatch.setattr(usage, "probe_all", lambda accounts, *, timeout: [
        _row("codex", "Codex", ok=False, error="timeout", note=None)])
    row = usage.report(accounts=[{"label": "Codex", "harness": "codex", "home": None}], fresh=True)["accounts"][0]
    assert row["ok"] is False and row["error"] == "timeout" and row["source"] == "rollout"
    assert row["windows"][0]["used_pct"] == 55
    assert row["note"] is None and "live_error" not in row


# ---- doctor and khipu_status: the cache only ------------------------------------------


def _never_probe(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("doctor and khipu_status read the cache only")

    for name in ("probe_all", "probe_account", "_converse", "_start_refresh", "_try_refresh_lock",
                 "probe_claude", "probe_codex", "probe_grok", "probe_muse"):
        monkeypatch.setattr(usage, name, boom)


def _seed_plans(tmp_path, *, claude_pct=94.0):
    """Four accounts: Claude main fresh with a five-hour window at 94%
    resetting in 40 min, Codex a day old (99% week already reset, five-hour
    under the line), Grok kept after a failed read at 89.96% (shown 90%),
    Muse never read."""
    now = datetime.now(timezone.utc)
    soon = usage._iso((now + timedelta(minutes=40, seconds=20)).timestamp())
    later = usage._iso((now + timedelta(days=2)).timestamp())
    past = usage._iso((now - timedelta(hours=1)).timestamp())
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "config.json").write_text(json.dumps({"usage_accounts": [
        {"label": "Claude main", "harness": "claude"}, {"label": "Codex", "harness": "codex"},
        {"label": "Grok", "harness": "grok"}, {"label": "Muse", "harness": "muse"}]}))
    usage._save_cache({
        "claude|": _cached(_ago(1), windows=[{"name": "five_hour", "used_pct": claude_pct, "resets_at": soon},
                                            {"name": "weekly", "used_pct": 30, "resets_at": later}]),
        "codex|": _cached(_ago(60 * 24), harness="codex", label="Codex", plan="pro", windows=[
            {"name": "weekly", "used_pct": 99, "resets_at": past},
            {"name": "five_hour", "used_pct": 89.94, "resets_at": soon}]),
        "grok|": dict(_cached(_ago(30), harness="grok", label="Grok", plan="heavy",
                              windows=[{"name": "weekly", "used_pct": 89.96, "resets_at": later}]),
                      ok=False, error="timeout"),
    })
    return {"soon": soon, "later": later}


def test_doctor_block_reads_the_cache_only_and_warns_at_90(fake, tmp_path, monkeypatch):
    times = _seed_plans(tmp_path)
    _never_probe(monkeypatch)
    block = usage.doctor_block()
    by = {a["account"]: a for a in block["accounts"]}
    assert block["source"] == "cache" and block["alert_pct"] == 90
    assert (by["Claude main"]["window"], by["Claude main"]["used_pct"], by["Claude main"]["stale"]) == (
        "five-hour", 94, False)
    assert by["Claude main"]["resets_at"] == times["soon"]
    # A reset window's old figure no longer applies: the tightest still in force is the five-hour.
    assert (by["Codex"]["window"], by["Codex"]["used_pct"], by["Codex"]["stale"]) == ("five-hour", 89.9, True)
    assert by["Grok"]["ok"] is False and by["Grok"]["error"] == "timeout" and by["Grok"]["stale"] is True
    assert by["Muse"]["never_read"] is True and by["Muse"]["window"] is None
    grok_at = by["Grok"]["figures_at"]
    assert block["warnings"] == [
        "Plan nearly used up: Claude main (max) five-hour at 94%, resets in 40 min",
        f"Plan nearly used up: Grok (heavy) week at 90% as of {usage._when(grok_at)}, "
        f"resets {usage._when(times['later'])}",
    ]


def _doctor(capsys) -> tuple[int, dict]:
    from contextlib import ExitStack
    from unittest import mock

    from khipu import cli
    from tests.test_cli_recall_quality import _green_doctor_patches

    patches = [*_green_doctor_patches(),
               mock.patch("khipu.probe.status", return_value={"ok": True, "reason": None}),
               mock.patch("khipu.drift.recall_quality", return_value={}),
               mock.patch("khipu.recall_prompt.prompt_recall_outcomes", return_value={"ok": True, "count": 25})]
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        rc = cli.cmd_doctor(cli.build_parser().parse_args(["doctor"]))
    return rc, json.loads(capsys.readouterr().out)


def test_doctor_carries_plan_usage_and_its_warning_and_never_gates_ok(fake, tmp_path, monkeypatch, capsys):
    _seed_plans(tmp_path, claude_pct=10)
    _never_probe(monkeypatch)
    rc_calm, calm = _doctor(capsys)
    assert calm["warnings"] == calm["plan_usage"]["warnings"]
    assert not any("Claude main" in w for w in calm["warnings"])
    _seed_plans(tmp_path, claude_pct=94)
    rc_hot, hot = _doctor(capsys)
    assert [a["account"] for a in hot["plan_usage"]["accounts"]] == ["Claude main", "Codex", "Grok", "Muse"]
    assert hot["warnings"][0] == "Plan nearly used up: Claude main (max) five-hour at 94%, resets in 40 min"
    assert hot["warnings"] == hot["plan_usage"]["warnings"]
    assert (rc_hot, hot["ok"]) == (rc_calm, calm["ok"])


def test_khipu_status_carries_the_same_warning_line(fake, tmp_path, monkeypatch):
    from unittest import mock

    _seed_plans(tmp_path)
    _never_probe(monkeypatch)
    with mock.patch("khipu.drift.status_payload", return_value={"counts": {"episodes": 1}}), \
            mock.patch("khipu.hub_snapshot.snapshot_freshness", return_value={"ok": True}):
        out = mcp_server._tool_status({})
    assert out["warnings"] == usage.doctor_block()["warnings"]
    assert out["warnings"][0].startswith("Plan nearly used up: Claude main (max) five-hour at 94%")


def test_doctor_block_never_raises_on_a_broken_cache(fake, tmp_path, monkeypatch):
    _never_probe(monkeypatch)
    usage.cache_file().parent.mkdir(parents=True, exist_ok=True)
    usage.cache_file().write_text("{not json")
    assert usage.doctor_block()["warnings"] == []


def test_foreground_report_never_probes_without_the_shared_refresh_lock(fake, monkeypatch):
    account = {"label": "Claude", "harness": "claude", "home": None}
    monkeypatch.setattr(usage, "_try_refresh_lock", lambda: None)
    monkeypatch.setattr(usage, "probe_all", lambda *_a, **_kw: pytest.fail("started beside the lock holder"))
    out = usage.report(fresh=True, accounts=[account])
    assert out["refreshing"] is True
    assert out["accounts"][0]["error"] == usage.E_PENDING
