"""khipu.usage: plan headroom read from each subscription CLI.

Every adapter runs against a fake child process that answers with a reply
recorded from the real CLI on 2026-10-14 (numbers fixed, ids and emails
replaced; tests/fixtures/usage/). No test starts a real CLI: ``_command`` is
patched in every test that probes.
"""
from __future__ import annotations

import ast
import builtins
import io
import json
import os
import shutil
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from khipu import mcp_server, usage

FIXTURES = Path(__file__).parent / "fixtures" / "usage"

FAKE_CLI = textwrap.dedent("""
    import json, os, sys, time
    fixture, mode, out = sys.argv[1], sys.argv[2], sys.argv[3]
    if out != "-":
        with open(out, "w") as fh:
            json.dump({"pid": os.getpid(), "env": sorted(os.environ), "argv": sys.argv[4:],
                       "env_values": {k: os.environ[k] for k in ("CLAUDE_CONFIG_DIR", "GROK_HOME",
                                                                "CODEX_HOME") if k in os.environ}}, fh)
    if mode == "hang":
        time.sleep(60)
    if mode == "exit":
        sys.stderr.write("fake: not signed in\\n")
        sys.exit(3)
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

    def configure(harness: str, fixture: str, mode: str = "answer") -> Path:
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


def test_claude_login_without_rate_limits_is_a_failure_in_its_own_words(fake):
    fake("claude", "claude-null.jsonl")
    row = usage.probe_claude()
    assert row["ok"] is False
    assert "/login" in row["error"]


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
    assert by["claude"]["ok"] is False and by["claude"]["error"] == "no answer within 1.5 s"
    assert all(by[h]["ok"] for h in ("codex", "grok", "muse"))
    assert by["codex"]["ms"] < 1500
    pid = _child(hung)["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_cli_that_exits_reports_its_own_words(fake):
    fake("grok", "grok.jsonl", mode="exit")
    row = usage.probe_grok(None)
    assert row["ok"] is False
    assert row["error"] == "fake: not signed in"


def test_missing_cli_is_reported_not_hidden(fake):
    row = usage.probe_account({"label": "Muse", "harness": "muse", "home": None})
    assert row["ok"] is False and row["error"] == "muse CLI not found"
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
    assert "live read failed (fake: not signed in)" in row["note"]


def test_codex_without_live_answer_or_rollout_fails(fake, tmp_path):
    fake("codex", "codex.jsonl", mode="exit")
    row = usage.probe_codex(str(tmp_path / "empty-home"))
    assert row["ok"] is False and row["error"] == "fake: not signed in"


# ---- cache and stale marking -----------------------------------------------------


def _row(harness="claude", label="Claude", *, ok=True, used=10, probed_at=None, **extra):
    row = {"harness": harness, "account": label, "home": None, "plan": "max", "ok": ok, "error": None,
           "windows": [{"name": "weekly", "used_pct": used, "resets_at": "2026-01-02T00:00:00Z"}] if ok else [],
           "credits": None, "probed_at": probed_at or usage._iso(time.time()), "ms": 5}
    row.update(extra)
    return row


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
    assert saved["rows"]["claude:Claude"]["windows"][0]["used_pct"] == 3


def test_failed_probe_keeps_the_last_figures_marked_stale(fake, monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).replace(microsecond=0)
    usage._save_cache({"claude:Claude": _row(probed_at=old.isoformat().replace("+00:00", "Z"), used=55)})
    monkeypatch.setattr(usage, "probe_all", lambda accounts, *, timeout: [
        _row(ok=False, error="no answer within 5 s")])
    rep = usage.report(accounts=[{"label": "Claude", "harness": "claude", "home": None}])
    row = rep["accounts"][0]
    assert row["ok"] is False and row["error"] == "no answer within 5 s"
    assert row["windows"][0]["used_pct"] == 55
    assert row["stale"] is True
    assert "[stale]" in rep["summary_text"][0] and "probe failed" in rep["summary_text"][0]


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
    assert "unknown harness" in accounts[1]["invalid"]
    assert "Muse" in accounts[2]["invalid"]
    row = usage.probe_account(accounts[1])
    assert row["ok"] is False and row["error"].startswith("invalid usage_accounts entry")


def test_summary_and_table():
    row = _row(windows=[{"name": "five_hour", "used_pct": 13, "resets_at": "2026-10-14T17:40:00Z"},
                        {"name": "weekly", "used_pct": 34, "resets_at": "2026-10-18T15:00:00Z"}])
    row["stale"] = False
    line = usage.summary_line(row)
    assert line == "Claude (max): 5 h 13%, week 34%; tightest week 34% resets Sun 15:00Z"
    failed = usage.summary_line(_row(ok=False, error="claude CLI not found", plan=None))
    assert failed == "Claude: unavailable: claude CLI not found"
    text = usage.table({"accounts": [row]})
    assert "ACCOUNT" in text and "5 h" in text and "34%" in text


def test_mcp_tool_returns_report_and_declines_on_the_gateway(fake, monkeypatch):
    monkeypatch.setattr(usage, "report", lambda **kw: {"accounts": [], "summary_text": [], "kw": kw})
    out = mcp_server.TOOL_FUNCS["khipu_usage"]({"fresh": True, "max_age": 30})
    assert out["kw"] == {"fresh": True, "max_age": 30.0}
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
    def __init__(self, data, log):
        super().__init__(data)
        self.log = log

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
