"""khipu.discover: finding the login folders Khipu could track (settings
clarity phase 9, slice 9A; docs/plans/2026-10-15-settings-clarity.md).

No test starts a real CLI: every CLI is a fake shell script under the test's
own temp folder (``usage._command`` patched, or ``synth_cli`` pointed at it),
and each fake records its argv and login-folder variable to a file outside
every login folder. Folder names are fictional.
"""
from __future__ import annotations

import builtins
import fcntl
import io
import json
import os
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from khipu import config, discover, session_capture as sc, synth_cli, t3, usage
from tests.test_usage import FIXTURES, _gone, _launchd_lookup

NOW = 1_800_000_000.0


# ---- fixtures --------------------------------------------------------------------


@pytest.fixture
def mac(tmp_path, monkeypatch):
    """A temp HOME, Khipu data and capture folders outside it, no CLI
    installed until ``mac.install`` adds a fake one."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.delenv("KHIPU_ISOLATED_DIR", raising=False)
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "GROK_HOME"):
        monkeypatch.delenv(name, raising=False)
    scripts: dict[str, str] = {}
    monkeypatch.setattr(usage, "_command", lambda harness: scripts.get(harness))

    class Mac:
        pass

    m = Mac()
    m.home, m.tmp, m.scripts = home, tmp_path, scripts
    m.rec = tmp_path / "rec"
    m.rec.mkdir()

    def install(cli: str, body: str | None = None) -> str:
        path = tmp_path / "bin" / cli
        path.parent.mkdir(exist_ok=True)
        log = m.rec / f"{cli}.log"
        path.write_text("#!/bin/sh\n"
                        f'printf \'%s\\t%s\\n\' "$CLAUDE_CONFIG_DIR$CODEX_HOME$GROK_HOME" "$*" >> "{log}"\n'
                        + (body if body is not None else FAKE_BODY[cli]))
        path.chmod(0o755)
        scripts[cli] = str(path)
        return str(path)

    def calls(cli: str) -> list[tuple[str, str]]:
        log = m.rec / f"{cli}.log"
        if not log.exists():
            return []
        return [tuple(line.split("\t", 1)) for line in log.read_text().splitlines()]

    def folder(name: str) -> Path:
        path = home / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def session(harness: str, sid: str, **state) -> Path:
        state.setdefault("seen_ts", NOW)
        path = sc.state_dir() / f"{harness}--{sid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))
        return path

    m.install, m.calls, m.folder, m.session = install, calls, folder, session
    return m


FAKE_BODY = {
    # A folder whose name ends "-out" is signed out; "-odd" answers nonsense.
    "claude": textwrap.dedent(f"""\
        case "$*" in
          "auth status --json")
            case "$CLAUDE_CONFIG_DIR" in
              *-out) echo '{{"loggedIn": false, "authMethod": "none"}}'; exit 1;;
              *-odd) echo 'something else'; exit 3;;
            esac
            echo '{{"loggedIn": true, "email": "someone@example.invalid"}}'; exit 0;;
          "--version") echo "2.1.296 (Claude Code)"; exit 0;;
          -p*) cat "{FIXTURES / 'claude.jsonl'}"; exit 0;;
        esac
        exit 64
    """),
    "codex": textwrap.dedent(f"""\
        case "$*" in
          "login status")
            case "$CODEX_HOME" in
              *-out) echo "Not logged in" >&2; exit 1;;
              *-odd) echo "boom" >&2; exit 2;;
            esac
            echo "Logged in using ChatGPT" >&2; exit 0;;
          "--version") echo "codex-cli 0.162.1"; exit 0;;
          app-server)
            case "$CODEX_HOME" in *-out) echo "not signed in" >&2; exit 3;; esac
            cat "{FIXTURES / 'codex.jsonl'}"; exit 0;;
        esac
        exit 64
    """),
    "grok": textwrap.dedent(f"""\
        case "$*" in
          "--version") echo "grok 1.0.52"; exit 0;;
          "agent --no-leader stdio") cat "{FIXTURES / 'grok.jsonl'}"; exit 0;;
        esac
        exit 64
    """),
    "muse": textwrap.dedent(f"""\
        case "$*" in
          "--version") echo "muse 1.4.6"; exit 0;;
          "serve --no-session-log") cat "{FIXTURES / 'muse-empty.jsonl'}"; exit 0;;
        esac
        exit 64
    """),
}


def _by_path(out: dict) -> dict[str, dict]:
    return {c["path"] or c["cli"]: c for c in out["candidates"]}


def _accounts(*entries) -> None:
    # The list as a hand-edited config holds it: a save through the app
    # would stop (and turn down) the default logins it leaves out.
    path = config.config_file()
    data = json.loads(path.read_text()) if path.is_file() else {}
    data["usage_accounts"] = list(entries)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


# ---- source 1: names in the home folder ------------------------------------------------


def test_the_name_filter_and_its_skips(mac):
    for name in (".claude-client", ".codex-work", ".grok-lab", ".claude-a.b_c",
                 ".claude-backup", ".claude-old-backups", ".codex-logs", ".claude-bak2", ".grok-bak",
                 ".claude-bak-old", ".codex-bak.1", ".claude-bakery", ".codex-bake",
                 ".claude.bak", ".claude_x", ".claudette", ".muse", ".muse-x", ".cursor-y", "claude-z"):
        mac.folder(name)
    (mac.home / ".codex-file").write_text("a file, not a folder")
    mac.folder("sub/.claude-deep")  # never recursed into
    found = {(cli, p.name) for cli, p in discover._source_names()}
    assert found == {("claude", ".claude-client"), ("codex", ".codex-work"), ("grok", ".grok-lab"),
                     ("claude", ".claude-a.b_c"), ("claude", ".claude-bakery"), ("codex", ".codex-bake")}
    assert discover.name_match(".claude") == "claude"
    assert discover.name_match(".claude-") is None
    assert discover.name_match(".codex-team-logs") is None


def test_symlinks_are_resolved_then_deduplicated(mac):
    real = mac.folder(".claude-real")
    (mac.home / ".claude-alias").symlink_to(real)
    elsewhere = mac.tmp / "elsewhere-login"
    elsewhere.mkdir()
    (mac.home / ".codex-link").symlink_to(elsewhere)
    (mac.home / ".claude-dangling").symlink_to(mac.tmp / "gone")
    default = mac.folder(".claude")
    (mac.home / ".claude-default-alias").symlink_to(default)
    mac.install("claude")
    _accounts({"label": "Codex", "harness": "codex"})
    found, _t, _d = discover._gather(NOW)
    paths = sorted(r["path"] for r in found.values())
    assert paths == ["~/.claude", "~/.claude-real", "~/.codex-link"]
    default_rec = next(r for r in found.values() if r["path"] == "~/.claude")
    assert default_rec["default"] is True and default_rec["name"] == "Claude"


def test_permission_errors_are_skipped_silently(mac, monkeypatch):
    mac.folder(".claude-client")
    (mac.home / ".claude-loop").symlink_to(mac.home / ".claude-loop")
    assert [p.name for _c, p in discover._source_names()] == [".claude-client"]
    mac.home.chmod(0o000)
    try:
        assert discover._source_names() == []
        assert discover._gather(NOW)[0] == {}
    finally:
        mac.home.chmod(0o755)


# ---- source 2: the folder each conversation ran under ------------------------------------


def test_session_folders_recorded_derived_old_and_corrupt(mac):
    client, work = mac.folder(".claude-client"), mac.folder(".claude-work")
    team, lab = mac.folder(".codex-team"), mac.folder(".grok-lab")
    outside = mac.tmp / "elsewhere" / ".claude-far"
    outside.mkdir(parents=True)
    mac.session("claude_code", "a", login_home=str(client), transcript_path=str(work / "projects/p/a.jsonl"))
    mac.session("claude_code", "b", transcript_path=str(work / "projects/p/b/subagents/x.jsonl"))
    mac.session("codex", "c", transcript_path=str(team / "sessions/2026/01/01/rollout-c.jsonl"))
    mac.session("aegis", "d", transcript_path=str(lab / "sessions/enc/d/updates.jsonl"))
    mac.session("aegis", "e", transcript_path=str(mac.tmp / "aegis-home/sessions/enc/e/updates.jsonl"))
    mac.session("claude_code", "f", login_home=str(outside))
    mac.session("claude_code", "g", login_home=str(mac.home / ".claude-gone"))
    (mac.home / ".claude-file").write_text("x")
    mac.session("claude_code", "h", login_home=str(mac.home / ".claude-file"))
    mac.session("claude_code", "old", offset=0, last_ts=0.0, captures=0)  # before the field existed
    (sc.state_dir() / "claude_code--bad.json").write_text("{not json")
    (sc.state_dir() / "cursor--i.json").write_text(json.dumps({"transcript_path": str(work / "projects/x.jsonl")}))
    got = {(cli, str(path)): n for cli, path, n in discover._source_sessions(NOW)}
    assert got == {("claude", str(client)): 1, ("claude", str(work)): 1, ("codex", str(team)): 1,
                   ("grok", str(lab)): 1, ("claude", str(outside)): 1}
    found, _t, _d = discover._gather(NOW)
    assert {r["path"] for r in found.values()} >= {"~/.claude-client", str(outside)}


def test_conversations_are_counted_over_seven_days_and_capped(mac, monkeypatch):
    client = mac.folder(".claude-client")
    for i, age_days in enumerate((0, 1, 6.9, 7.5, 30)):
        mac.session("claude_code", f"s{i}", login_home=str(client), seen_ts=NOW - age_days * 86400)
    mac.session("claude_code", "sub", login_home=str(client), seen_ts=NOW, subagent=True)
    assert discover._source_sessions(NOW) == [("claude", client, 3)]
    monkeypatch.setattr(discover, "COUNT_CAP", 2)
    assert discover._source_sessions(NOW) == [("claude", client, 2)]


def _codex_transcript(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "hi"}}) + "\n"
                    + json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "yo"}}) + "\n")
    return path


def _claude_transcript(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "q " + "x" * 50}}) + "\n")
    return path


SENTINEL = "SENTINEL-discover-do-not-record"


@pytest.mark.parametrize("harness,var,cli", [("claude_code", "CLAUDE_CONFIG_DIR", "claude"),
                                             ("codex", "CODEX_HOME", "codex")])
def test_the_hook_records_only_the_login_folder_with_or_without_its_variable(mac, monkeypatch, harness, var, cli):
    folder = mac.folder(f".{cli}-client")
    if cli == "claude":
        transcript = _claude_transcript(folder / "projects" / "p" / "s1.jsonl")
    else:
        transcript = _codex_transcript(folder / "sessions" / "2026" / "01" / "01" / "rollout-s1.jsonl")
    monkeypatch.setenv("KHIPU_DISCOVER_TEST_SECRET", SENTINEL)
    # Without the variable: nothing recorded, the folder is derived from the transcript.
    sc.hook_main(json.dumps({"hook_event_name": "Stop", "session_id": "s1", "transcript_path": str(transcript),
                             "cwd": str(mac.tmp)}), harness)
    state = json.loads((sc.state_dir() / f"{harness}--s1.json").read_text())
    assert sc.LOGIN_HOME_FIELD not in state
    assert discover.session_folder(cli, state) == folder
    # With it: exactly the variable's value, nothing else from the environment.
    monkeypatch.setenv(var, str(folder))
    sc.hook_main(json.dumps({"hook_event_name": "Stop", "session_id": "s1", "transcript_path": str(transcript),
                             "cwd": str(mac.tmp)}), harness)
    text = (sc.state_dir() / f"{harness}--s1.json").read_text()
    state = json.loads(text)
    assert state[sc.LOGIN_HOME_FIELD] == str(folder)
    assert SENTINEL not in text and str(mac.home) + "\"" not in text
    assert not any(k.lower().startswith("env") for k in state)
    assert discover.session_folder(cli, state) == folder


def test_a_corrupt_state_file_never_fails_the_hook(mac, monkeypatch):
    folder = mac.folder(".claude-client")
    transcript = _claude_transcript(folder / "projects" / "p" / "s2.jsonl")
    sc.state_dir().mkdir(parents=True, exist_ok=True)
    (sc.state_dir() / "claude_code--s2.json").write_text("{corrupt")
    (sc.state_dir() / "claude_code--other.json").write_bytes(b"\xff\xfe not json")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(folder))
    out = sc.hook_main(json.dumps({"hook_event_name": "Stop", "session_id": "s2",
                                   "transcript_path": str(transcript), "cwd": str(mac.tmp)}), "claude_code")
    assert out["harness"] == "claude_code" and "error" not in out
    assert json.loads((sc.state_dir() / "claude_code--s2.json").read_text())[sc.LOGIN_HOME_FIELD] == str(folder)
    assert (sc.state_dir() / "claude_code--other.json").read_bytes() == b"\xff\xfe not json"
    assert sc.sweep_login_homes(force=True) == 0


# ---- the 30-day retention and Forget ---------------------------------------------------------


def _write_state(path: Path, state, mtime: float) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = state if isinstance(state, bytes) else json.dumps(state).encode()
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return data


def test_login_paths_are_cleared_after_30_days_on_the_next_capture(mac, monkeypatch):
    d = sc.state_dir()
    old = {"seen_ts": time.time() - 31 * 86400, "offset": 7, sc.LOGIN_HOME_FIELD: "/x/.claude-old", "captures": 2}
    young = {"seen_ts": time.time() - 86400, "offset": 3, sc.LOGIN_HOME_FIELD: "/x/.claude-young"}
    no_field = {"seen_ts": time.time() - 90 * 86400, "offset": 1}
    _write_state(d / "claude_code--old.json", old, 1000)
    _write_state(d / "claude_code--young.json", young, 2000)
    plain = _write_state(d / "codex--plain.json", no_field, 3000)
    bad = _write_state(d / "codex--bad.json", b"{nope", 4000)
    folder = mac.folder(".claude-client")
    transcript = _claude_transcript(folder / "projects" / "p" / "new.jsonl")
    sc.hook_main(json.dumps({"hook_event_name": "Stop", "session_id": "new", "transcript_path": str(transcript),
                             "cwd": str(mac.tmp)}), "claude_code")
    cleared = json.loads((d / "claude_code--old.json").read_text())
    assert cleared == {k: v for k, v in old.items() if k != sc.LOGIN_HOME_FIELD}
    assert (d / "claude_code--old.json").stat().st_mtime == 1000
    assert json.loads((d / "claude_code--young.json").read_text()) == young
    assert (d / "codex--plain.json").read_bytes() == plain and (d / "codex--plain.json").stat().st_mtime == 3000
    assert (d / "codex--bad.json").read_bytes() == bad
    # At most once a day: a second capture's sweep does nothing.
    _write_state(d / "claude_code--old2.json", old, 1000)
    assert sc.sweep_login_homes() == 0
    assert sc.LOGIN_HOME_FIELD in json.loads((d / "claude_code--old2.json").read_text())


def test_forget_clears_every_file_and_leaves_the_rest_untouched(mac, capsys):
    from khipu.cli import main

    d = sc.state_dir()
    a = {"seen_ts": time.time(), "offset": 9, "cwd": "/w", sc.LOGIN_HOME_FIELD: "/x/.codex-a"}
    b = {"seen_ts": time.time() - 40 * 86400, sc.LOGIN_HOME_FIELD: "/x/.claude-b", "captures": 4}
    _write_state(d / "codex--a.json", a, 1111)
    _write_state(d / "claude_code--b.json", b, 2222)
    plain = _write_state(d / "aegis--c.json", {"seen_ts": 1.0, "offset": 2}, 3333)
    assert main(["usage", "--forget-login-paths"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "finished": True, "cleared": 2, "pending": 0,
                                                   "failed": 0, "unreachable": False}
    assert json.loads((d / "codex--a.json").read_text()) == {k: v for k, v in a.items() if k != sc.LOGIN_HOME_FIELD}
    assert json.loads((d / "claude_code--b.json").read_text()) == {k: v for k, v in b.items()
                                                                     if k != sc.LOGIN_HOME_FIELD}
    assert (d / "codex--a.json").stat().st_mtime == 1111
    assert (d / "aegis--c.json").read_bytes() == plain and (d / "aegis--c.json").stat().st_mtime == 3333


def test_discovery_forgets_sessions_past_retention_or_before_forget(mac, monkeypatch):
    calls, _a = _patch_check(monkeypatch)
    named = mac.folder(".claude-named")
    owner = mac.folder(".codex-mine")
    far = mac.tmp / "elsewhere" / ".claude-far"
    (far / "projects" / "p").mkdir(parents=True)
    old_t = mac.tmp / "elsewhere" / ".claude-old"
    (old_t / "projects" / "p").mkdir(parents=True)
    old_r = mac.tmp / "elsewhere" / ".claude-oldrec"
    old_r.mkdir(parents=True)
    mac.session("claude_code", "n", login_home=str(named), seen_ts=NOW - 86400)
    mac.session("codex", "o", login_home=str(owner), seen_ts=NOW - 86400)
    derived = mac.session("claude_code", "f", transcript_path=str(far / "projects/p/f.jsonl"), seen_ts=NOW - 29 * 86400)
    mac.session("claude_code", "t", transcript_path=str(old_t / "projects/p/t.jsonl"), seen_ts=NOW - 31 * 86400)
    mac.session("claude_code", "r", login_home=str(old_r), seen_ts=NOW - 31 * 86400)
    # (a) Only sessions within the 30-day retention name a folder.
    got = {str(p) for _c, p, _n in discover._source_sessions(NOW)}
    assert got == {str(named), str(owner), str(far)}
    out = discover.discover(at=NOW)
    rows = _by_path(out)
    assert set(rows) == {"~/.claude-named", "~/.codex-mine", str(far)}
    assert rows["~/.claude-named"]["state"] == "signed_in" and rows["~/.claude-named"]["conversations_7d"] == 1
    discover.discover(check="~/.codex-mine", at=NOW + 10)
    derived_bytes = derived.read_bytes()
    # (b) Forget stamps the time; every session seen at or before it is ignored.
    assert sc.forget_login_homes(now=NOW + 20) == {"cleared": 3, "pending": 0, "failed": 0, "unreachable": False}
    assert sc.login_homes_forgotten_at() == NOW + 20
    assert derived.read_bytes() == derived_bytes, "a file without the field is never rewritten"
    assert discover._source_sessions(NOW + 60) == []
    # (c) The next run drops the conversation sources and counts it no longer backs.
    out = discover.discover(at=NOW + 60)
    rows = _by_path(out)
    assert set(rows) == {"~/.claude-named", "~/.codex-mine"}
    assert rows["~/.claude-named"]["sources"] == ["name"] and rows["~/.claude-named"]["conversations_7d"] is None
    assert rows["~/.claude-named"]["state"] == "not_checked" and rows["~/.claude-named"]["last_checked"] is None
    assert rows["~/.codex-mine"]["state"] == "signed_in", "the owner's own Check is kept"
    kept = json.loads(discover.store_file().read_text())
    assert not [r for r in kept["candidates"].values() if "conversation" in r["sources"] or r["conversations_7d"]]
    # A session seen after Forget counts again.
    mac.session("claude_code", "later", login_home=str(named), seen_ts=NOW + 30)
    assert discover._source_sessions(NOW + 60) == [("claude", named, 1)]


def test_clearing_takes_the_hooks_lock_and_never_loses_its_save(mac, monkeypatch):
    d = sc.state_dir()
    old = {"seen_ts": time.time() - 40 * 86400, "offset": 1, sc.LOGIN_HOME_FIELD: "/x/.claude-a"}
    path = d / "claude_code--held.json"
    held = _write_state(path, old, 1000)
    lock = path.with_suffix(".lock")
    assert lock == sc._state_file("claude_code", "held").with_suffix(".lock")
    lock.mkdir()
    try:
        assert sc.forget_login_homes() == {"cleared": 0, "pending": 1, "failed": 0, "unreachable": False}
        assert sc.sweep_login_homes(force=True) == 0
        assert path.read_bytes() == held and path.stat().st_mtime == 1000
        assert lock.is_dir(), "someone else's lock is left alone"
    finally:
        lock.rmdir()
    # The race: a hook save landing between the clear's read and its replace.
    sc._pending_erasures_clear()
    real = sc._read_state_at
    hook: dict = {}

    def read_then_save(dfd, name):
        text = real(dfd, name)
        if name == path.name and not hook:
            with sc._state_lock("claude_code", "held") as acquired:
                hook["acquired"] = acquired
                if acquired:
                    sc.save_state("claude_code", "held", dict(old, offset=99, seen_ts=time.time()))
        return text

    monkeypatch.setattr(sc, "_read_state_at", read_then_save)
    sc.forget_login_homes()
    monkeypatch.setattr(sc, "_read_state_at", real)
    final = json.loads(path.read_text())
    assert hook["acquired"] is False or final["offset"] == 99, "the hook's save was lost"
    assert not lock.exists(), "the lock is always removed"


# ---- sources 3 and 4, de-duplication, names ----------------------------------------------------


def test_a_default_folder_counts_only_for_an_installed_cli(mac):
    grok = mac.folder(".grok")
    mac.folder(".claude")
    mac.folder(".grok-lab")
    mac.session("aegis", "d", transcript_path=str(grok / "sessions/e/d/updates.jsonl"))
    _accounts({"label": "Other", "harness": "claude", "home": str(mac.folder(".claude-other"))})
    found, _t, _d = discover._gather(NOW)
    assert {r["path"] for r in found.values()} == {"~/.grok-lab"}
    mac.install("grok")
    found, _t, _d = discover._gather(NOW)
    recs = {r["path"]: r for r in found.values()}
    assert set(recs) == {"~/.grok", "~/.grok-lab"}
    assert recs["~/.grok"]["sources"] == ["name", "conversation", "installed"]


def _t3_settings(home: Path, instances: dict) -> None:
    path = t3.settings_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"providerInstances": instances, "apiToken": "tok-should-never-be-read"}))


def test_t3_provider_instances_read_only_the_allowlisted_keys(mac):
    _t3_settings(mac.home, {
        "claudeAgent": {"driver": "claudeAgent", "config": {"homePath": "", "binaryPath": "/x"}, "token": "t"},
        "work": {"driver": "codex", "displayName": " Work ", "enabled": False,
                 "config": {"homePath": "~/.codex-t3", "apiKey": "k"}},
        "codex": {"driver": "codex", "config": {"homePath": ""}},
        "other": {"driver": "gemini", "config": {"homePath": "/y"}},
    })
    got = t3.provider_instances(t3.settings_path(mac.home), ("claudeAgent", "codex"))
    assert got == [
        {"id": "claudeAgent", "driver": "claudeAgent", "name": "Claude", "home_path": "", "enabled": True},
        {"id": "work", "driver": "codex", "name": "Work", "home_path": "~/.codex-t3", "enabled": False},
        {"id": "codex", "driver": "codex", "name": "Codex", "home_path": "", "enabled": True},
    ]
    assert t3.claude_instances(t3.settings_path(mac.home)) == [
        {"id": "claudeAgent", "name": "Claude", "home_path": "", "enabled": True}]


def test_candidates_are_deduplicated_against_tracked_declined_and_defaults(mac):
    for cli in ("claude", "codex", "grok", "muse"):
        mac.install(cli)
    client = mac.folder(".claude-client")
    mac.folder(".codex-work")
    mac.folder(".codex-t3")
    mac.folder(".claude")
    mac.folder(".codex")
    mac.folder(".grok-lab")
    # Tracked by its full path (the name source finds it as ~/.claude-client).
    _accounts({"label": "Client", "harness": "claude", "home": str(client) + "/"},
              {"label": "Grok", "harness": "grok"})
    usage.cache_file().parent.mkdir(parents=True, exist_ok=True)
    discover.declined_file().write_text(json.dumps([
        {"cli": "codex", "path": "~/.codex-work", "identity": "x", "why": "you chose Not this one", "at": "t"}]))
    _t3_settings(mac.home, {"t": {"driver": "codex", "displayName": "T", "config": {"homePath": "~/.codex-t3"}},
                            "codex": {"driver": "codex", "config": {"homePath": ""}}})
    found, _t, _d = discover._gather(NOW)
    recs = {r["path"] or r["cli"]: r for r in found.values()}
    assert set(recs) == {"~/.claude", "~/.codex-t3", "~/.codex", "~/.grok-lab", "muse"}
    assert recs["~/.claude"]["default"] and recs["~/.claude"]["sources"] == ["name", "installed"]
    assert recs["~/.codex"]["default"] and recs["~/.codex"]["sources"] == ["name", "t3", "installed"]
    assert recs["~/.codex-t3"]["sources"] == ["name", "t3"] and recs["~/.codex-t3"]["name"] == "Codex t3"
    assert recs["muse"]["default"] and recs["muse"]["path"] is None and recs["muse"]["name"] == "Muse"
    assert recs["~/.grok-lab"]["name"] == "Grok lab"
    assert discover.needs_owner_check(recs["~/.grok-lab"]) and not discover.needs_owner_check(recs["~/.codex-t3"])
    # A corrupt turned-down file is an empty list.
    discover.declined_file().write_text("{nope")
    assert discover.turned_down() == []


def test_default_names_and_paths_pass_the_redaction(mac):
    h = mac.home
    assert discover.default_name("claude", str(h / ".claude-client"), default=False) == "Claude client"
    assert discover.default_name("codex", str(h / ".codex"), default=True) == "Codex"
    assert discover.default_name("claude", "/Volumes/x/alt-login", default=False) == "Claude alt-login"
    assert discover.default_name("claude", str(h / ".claude-sk-ant-abcdefghijklmnopqrstuvwxyz0123"),
                                 default=False) is None
    assert discover.default_name("codex", "/x/API Keys/.codex-k", default=False) is None
    assert discover.display_path(h / ".claude-client") == "~/.claude-client"
    assert discover.display_path("/Volumes/x/alt-login") == "/Volumes/x/alt-login"


# ---- the check: decision 20, argv table, runner ------------------------------------------------


def test_name_only_folders_are_not_checked_until_the_owner_presses_check(mac):
    for cli in ("claude", "codex", "grok"):
        mac.install(cli)
    for name in (".claude-client", ".codex-work", ".grok-lab"):
        mac.folder(name)
    out = discover.discover()
    rows = _by_path(out)
    assert set(rows) == {"~/.claude-client", "~/.codex-work", "~/.grok-lab"}
    for row in rows.values():
        assert row["state"] == "not_checked" and row["needs_owner_check"] and not row["offer_on_home"]
    assert out["last_run"]["checked"] == 0 and out["last_run"]["not_checked"] == 3
    assert out["last_run"]["left_for_later"] == 0
    assert not any(mac.calls(cli) for cli in ("claude", "codex", "grok")), "no child ran"
    # The owner's Check runs exactly one allowed status child (then one plan read).
    out = discover.discover(check="~/.codex-work")
    assert out["check"]["ok"] is True
    calls = mac.calls("codex")
    assert calls[0] == (str(mac.home / ".codex-work"), "login status")
    assert [c[1] for c in calls] == ["login status", "app-server"]
    row = _by_path(out)["~/.codex-work"]
    assert row["state"] == "signed_in" and row["plan"] == "pro" and not row["offer_on_home"]
    assert not mac.calls("claude") and not mac.calls("grok")
    # Later runs keep the owner's result and still never check it by themselves.
    out = discover.discover(now=True)
    assert len(mac.calls("codex")) == 2 and _by_path(out)["~/.codex-work"]["state"] == "signed_in"


def test_check_accepts_only_a_known_or_login_named_folder(mac):
    mac.install("claude")
    mac.folder("not-a-login")
    mac.folder(".claude-client")
    assert discover.discover(check="~/not-a-login")["check"] == {"ok": False, "error": "not_a_login_folder"}
    assert discover.discover(check="~/.claude-missing")["check"] == {"ok": False, "error": "folder_not_found"}
    assert discover.discover(check="relative/.claude-x")["check"] == {"ok": False, "error": "bad_folder"}
    _accounts({"label": "C", "harness": "claude", "home": "~/.claude-client"})
    assert discover.discover(check=str(mac.home / ".claude-client"))["check"]["error"] == "already_tracked"
    assert not mac.calls("claude")


def test_the_argv_table_is_exact_and_frozen(mac):
    claude, codex = mac.install("claude"), mac.install("codex")
    muse = mac.install("muse")
    assert discover.argv_allowed("claude", (claude, "auth", "status", "--json"))
    assert discover.argv_allowed("codex", (codex, "login", "status"))
    assert discover.argv_allowed("muse", (muse, "--version"))
    for cli, argv in (("codex", (codex, "login")), ("muse", (muse, "login")), ("codex", (codex, "login", "status", "-x")),
                      ("claude", (claude, "auth", "status")), ("codex", (codex, "auth", "status", "--json")),
                      ("claude", (claude, "auth", "login", "--claudeai")), ("claude", ("claude", "--version")),
                      ("grok", (claude, "login", "status")), ("codex", (codex,))):
        assert not discover.argv_allowed(cli, argv), argv
        with pytest.raises(discover.RefusedArgv):
            discover.run_once(cli, argv)
    assert not mac.calls("codex") and not mac.calls("muse") and not mac.calls("claude")
    with pytest.raises(TypeError):
        discover.STATUS_ARGV["codex"] = ("login",)


def test_no_argv_outside_the_table_in_a_whole_run(mac, monkeypatch):
    for cli in ("claude", "codex", "grok", "muse"):
        mac.install(cli)
    for name in (".claude-client", ".codex-work", ".grok-lab", ".claude-out"):
        mac.session({"claude": "claude_code", "codex": "codex", "grok": "aegis"}[name[1:].split("-")[0]],
                    name, login_home=str(mac.folder(name)))
    _accounts({"label": "None here", "harness": "claude", "home": str(mac.folder(".claude-tracked"))})
    seen: list[tuple] = []
    real = discover.subprocess.Popen

    def recording(argv, *a, **kw):
        seen.append(tuple(argv))
        return real(argv, *a, **kw)

    monkeypatch.setattr(discover.subprocess, "Popen", recording)
    monkeypatch.setattr(usage.subprocess, "Popen", recording)
    out = discover.discover(now=True)
    discover.cli_states()
    discover.discover(check="~/.claude-out")
    allowed = {(mac.scripts["claude"], *discover.STATUS_ARGV["claude"]),
               (mac.scripts["codex"], *discover.STATUS_ARGV["codex"]),
               (mac.scripts["claude"], *usage._CLAUDE_ARGS), (mac.scripts["codex"], "app-server"),
               (mac.scripts["grok"], "agent", "--no-leader", "stdio"), (mac.scripts["muse"], "serve", "--no-session-log")}
    allowed |= {(mac.scripts[c], "--version") for c in ("claude", "codex", "grok", "muse")}
    assert seen and set(seen) <= allowed, set(seen) - allowed
    rows = _by_path(out)
    assert rows["~/.claude-client"]["state"] == "signed_in" and rows["~/.claude-client"]["plan"] == "max"
    assert rows["~/.claude-client"]["offer_on_home"] is True
    assert rows["~/.claude-out"]["state"] == "not_signed_in"
    assert rows["~/.grok-lab"]["state"] == "signed_in" and rows["~/.grok-lab"]["plan"] == "SuperGrok Heavy"
    assert rows["muse"]["state"] == "signed_in"
    assert "~/.claude-tracked" not in rows


def test_every_child_gets_no_stdin_its_own_session_and_an_empty_temp_cwd(mac, monkeypatch):
    claude = mac.install("claude", 'echo "$PWD" > "$0.cwd"; ls -A > "$0.ls"; [ -t 0 ] && echo tty > "$0.tty"; '
                                   'echo \'{"loggedIn": true}\'\n')
    kwargs: dict = {}
    real = discover.subprocess.Popen

    def spy(argv, *a, **kw):
        kwargs.update(kw)
        return real(argv, *a, **kw)

    monkeypatch.setattr(discover.subprocess, "Popen", spy)
    res = discover.run_once("claude", (claude, *discover.STATUS_ARGV["claude"]), home=str(mac.home / ".claude-x"))
    assert discover.classify_status("claude", res) == ("signed_in", None)
    assert kwargs["stdin"] is discover.subprocess.DEVNULL and kwargs["start_new_session"] is True
    assert kwargs["env"]["CLAUDE_CONFIG_DIR"] == str(mac.home / ".claude-x")
    assert set(kwargs["env"]) <= set(usage._ENV_ALLOW) | {"CLAUDE_CONFIG_DIR"} | {k for k in kwargs["env"]
                                                                                   if k.startswith("LC_")}
    assert Path(claude + ".ls").read_text() == "" and "khipu-discover-" in Path(claude + ".cwd").read_text()
    assert not Path(claude + ".tty").exists()


def test_a_hanging_cli_is_killed_with_its_group_at_five_seconds(mac):
    pids = mac.tmp / "pids"
    claude = mac.install("claude", f'echo $$ > "{pids}.child"; /bin/sleep 60 & echo $! > "{pids}.grand"; wait\n')
    started = time.monotonic()
    res = discover.run_once("claude", (claude, *discover.STATUS_ARGV["claude"]))
    took = time.monotonic() - started
    assert res["outcome"] == "timeout" and 4.9 <= took < 7, took
    assert discover.classify_status("claude", res) == ("couldnt_check", "timeout")
    assert _gone(int(Path(f"{pids}.child").read_text())) and _gone(int(Path(f"{pids}.grand").read_text()))


@pytest.mark.parametrize("body,reason", [
    ('if read line; then echo \'{"loggedIn": true}\'; else exit 5; fi\n', "exit_5"),
    ('if (exec 3</dev/tty) 2>/dev/null; then read line </dev/tty; echo \'{"loggedIn": true}\'; else exit 6; fi\n',
     "exit_6"),
])
def test_a_cli_that_reads_stdin_or_opens_the_terminal_is_classified_not_left_waiting(mac, body, reason):
    claude = mac.install("claude", body)
    started = time.monotonic()
    res = discover.run_once("claude", (claude, *discover.STATUS_ARGV["claude"]))
    assert time.monotonic() - started < 3
    assert discover.classify_status("claude", res) == ("couldnt_check", reason)


def test_output_is_capped_and_codex_words_only_choose_a_code(mac, monkeypatch):
    monkeypatch.setattr(discover, "OUTPUT_CAP", 100)
    claude = mac.install("claude", "head -c 100000 /dev/zero | tr '\\0' x; exit 2\n")
    res = discover.run_once("claude", (claude, *discover.STATUS_ARGV["claude"]))
    assert len(res["out"]) == 100 and discover.classify_status("claude", res) == ("couldnt_check", "exit_2")
    codex = mac.install("codex")
    for home, want in ((".codex-out", ("not_signed_in", None)), (".codex-odd", ("couldnt_check", "exit_2")),
                       (".codex-in", ("signed_in", None))):
        res = discover.run_once("codex", (codex, *discover.STATUS_ARGV["codex"]), home=str(mac.home / home))
        assert discover.classify_status("codex", res) == want, home


def test_a_failed_check_is_couldnt_check_never_not_signed_in(mac):
    mac.install("claude")
    mac.session("claude_code", "o", login_home=str(mac.folder(".claude-odd")))
    mac.session("codex", "n", login_home=str(mac.folder(".codex-nocli")))
    out = discover.discover()
    rows = _by_path(out)
    assert rows["~/.claude-odd"]["state"] == "couldnt_check" and rows["~/.claude-odd"]["reason"] == "exit_3"
    assert rows["~/.codex-nocli"]["state"] == "couldnt_check"
    assert rows["~/.codex-nocli"]["reason"] == "not_installed"
    assert out["last_run"]["couldnt_check"] == 2 and not rows["~/.claude-odd"]["offer_on_home"]


# ---- caps, order, waits, rotation, the lock ------------------------------------------------------


def _patch_check(monkeypatch, state="signed_in", delay=0.0):
    calls: list[str] = []
    active = [0, 0]
    lock = threading.Lock()

    def check(rec, *, run, end):
        with lock:
            calls.append(rec["path"])
            active[0] += 1
            active[1] = max(active[1], active[0])
        time.sleep(delay)
        with lock:
            active[0] -= 1
        return state, None, None

    monkeypatch.setattr(discover, "_check_one", check)
    return calls, active


def _conversation_folders(mac, n: int, prefix: str = "f") -> list[str]:
    names = []
    for i in range(n):
        name = f".claude-{prefix}{i:02d}"
        mac.session("claude_code", f"{prefix}{i}", login_home=str(mac.folder(name)))
        names.append("~/" + name)
    return names


def test_twelve_a_run_two_at_a_time_conversation_folders_first(mac, monkeypatch):
    names = _conversation_folders(mac, 13)
    for i in range(3):
        mac.folder(f".codex-t{i}")
    _t3_settings(mac.home, {f"t{i}": {"driver": "codex", "config": {"homePath": f"~/.codex-t{i}"}}
                            for i in range(3)})
    calls, active = _patch_check(monkeypatch, delay=0.05)
    out = discover.discover(at=NOW)
    assert len(calls) == 12 and set(calls) <= set(names), calls
    assert active[1] == 2
    assert out["last_run"]["checked"] == 12 and out["last_run"]["left_for_later"] == 4


def test_the_order_rule():
    def rec(path, sources, first, last=None):
        return {"path": path, "sources": sources, "first_seen": first, "last_checked": last}

    recs = [rec("t-old-checked", ["t3"], 1, last=50), rec("t-new", ["t3"], 9), rec("t-old", ["t3"], 2),
            rec("c-checked-late", ["conversation"], 1, last=90), rec("c-checked-early", ["conversation"], 1, last=10),
            rec("c-new", ["conversation"], 5), rec("t-checked-early", ["t3"], 8, last=5)]
    assert [r["path"] for r in sorted(recs, key=discover._order)] == [
        "c-new", "c-checked-early", "c-checked-late", "t-new", "t-old", "t-checked-early", "t-old-checked"]


def test_every_folder_is_checked_within_three_runs_and_a_new_one_by_the_next(mac, monkeypatch):
    names = _conversation_folders(mac, 30)
    calls, _active = _patch_check(monkeypatch)
    discover.discover(at=NOW)
    first = list(calls)
    late = _conversation_folders(mac, 1, prefix="late")[0]
    discover.discover(at=NOW + 60)
    second = calls[len(first):]
    discover.discover(at=NOW + 120)
    assert late in second, "a folder first seen after the others is checked by the next run"
    assert set(calls) == set(names) | {late} and len(calls) == len(set(calls)) == 31
    out = discover.discover(at=NOW + 180)
    assert len(calls) == 31 and out["last_run"]["checked"] == 0, "signed in: not again for 24 hours"


def test_the_shared_deadline_never_stamps_a_folder_it_cut_off(mac, monkeypatch):
    # A real slow fake CLI through the runner: a check takes 0.7 s, its own
    # timeout is 1 s, and the run's deadline (1 + 0.3 s) cuts the second
    # check of each worker short.
    monkeypatch.setattr(discover, "CHILD_TIMEOUT_S", 1.0)
    monkeypatch.setattr(usage, "OVERALL_GRACE_S", 0.3)
    mac.install("claude", '/bin/sleep 0.7; echo \'{"loggedIn": false}\'; exit 1\n')
    names = _conversation_folders(mac, 30)
    checked = 0
    for i in range(25):
        out = discover.discover(at=NOW + i * 60)
        rows = _by_path(out)
        assert set(names) <= set(rows)
        auto = [r for r in rows.values() if not r["needs_owner_check"]]  # the default login is tracked
        assert not [r["path"] for r in auto if r["state"] == "couldnt_check"]
        done = sum(1 for r in auto if r["state"] == "not_signed_in")
        assert out["last_run"]["checked"] == done - checked
        assert out["last_run"]["left_for_later"] == len(auto) - done
        checked = done
        if done == len(auto):
            break
    assert checked == len(auto) == 30
    assert all(r["last_checked"] for r in _by_path(out).values())


def test_recheck_waits_and_look_now(mac, monkeypatch):
    _conversation_folders(mac, 1, prefix="in")
    calls, _a = _patch_check(monkeypatch, state="signed_in")
    discover.discover(at=NOW)
    discover.discover(at=NOW + 3600)
    assert len(calls) == 1
    discover.discover(at=NOW + 24 * 3600)
    assert len(calls) == 2
    discover.discover(at=NOW + 24 * 3600 + 60, now=True)
    assert len(calls) == 3
    calls2, _a = _patch_check(monkeypatch, state="not_signed_in")
    discover.discover(at=NOW + 2 * 86400 + 120, now=True)
    discover.discover(at=NOW + 2 * 86400 + 3600)
    assert len(calls2) == 1, "not signed in: at most once a day"


def test_a_second_run_gets_the_last_result_marked_looking_and_checks_nothing(mac, monkeypatch):
    _conversation_folders(mac, 2)
    calls, _a = _patch_check(monkeypatch)
    first = discover.discover(at=NOW)
    fd = discover._try_lock()
    assert fd is not None
    try:
        out = discover.discover(at=NOW + 2 * 86400, now=True)
        assert out["looking"] is True and out["candidates"] == first["candidates"]
        assert discover.discover(check="~/.claude-f00")["check"] == {"ok": False, "error": "looking"}
        assert len(calls) == 2
    finally:
        usage._release(fd)
    assert discover.discover(at=NOW + 2 * 86400)["looking"] is False and len(calls) == 4


def test_what_it_keeps_and_pruning_a_gone_folder(mac, monkeypatch):
    names = _conversation_folders(mac, 2)
    _patch_check(monkeypatch)
    discover.discover(at=NOW)
    kept = json.loads(discover.store_file().read_text())
    rec = next(r for r in kept["candidates"].values() if r["path"] == names[0])
    assert rec["first_seen"] == NOW and rec["last_checked"] == NOW and rec["state"] == "signed_in"
    assert rec["identity"].startswith("inode:") and rec["conversations_7d"] == 1 and rec["sources"] == ["name", "conversation"]
    assert kept["last_run"]["at"] == NOW and kept["last_run"]["checked"] == 2
    (mac.home / names[0][2:]).rmdir()
    out = discover.discover(at=NOW + 60)
    assert [c["path"] for c in out["candidates"]] == [names[1]]
    assert not usage.cache_file().exists(), "discovery never writes the usage cache"


# ---- never read inside a login folder ------------------------------------------------------------


def test_discovery_check_and_try_never_read_inside_a_login_folder(mac, monkeypatch):
    for cli in ("claude", "codex", "grok", "muse"):
        mac.install(cli)
    folders = [mac.folder(n) for n in (".claude", ".codex", ".grok", ".claude-client", ".codex-work",
                                       ".codex-out", ".grok-lab", ".claude-named")]
    for f in folders:
        (f / "auth.json").write_text("{}")
        (f / ".credentials.json").write_text("{}")
    rollout = mac.home / ".codex-out" / "sessions" / "2026" / "01" / "01"
    rollout.mkdir(parents=True)
    (rollout / "rollout-x.jsonl").write_text((FIXTURES / "codex-rollout.jsonl").read_text())
    mac.session("claude_code", "a", login_home=str(mac.home / ".claude-client"))
    mac.session("codex", "b", transcript_path=str(mac.home / ".codex-work/sessions/2026/01/01/rollout-b.jsonl"))
    mac.session("codex", "c", login_home=str(mac.home / ".codex-out"))
    mac.session("aegis", "d", transcript_path=str(mac.home / ".grok-lab/sessions/e/d/updates.jsonl"))
    _t3_settings(mac.home, {"codex": {"driver": "codex", "config": {"homePath": ""}}})
    _accounts({"label": "Muse", "harness": "muse"})
    touched = _forbid_reads(monkeypatch, folders)
    out = discover.discover(now=True)
    check = discover.discover(check="~/.claude-named")
    tried = discover.try_account("codex", "~/.codex-out")
    tried_default = discover.try_account("claude")
    states = discover.cli_states()
    assert touched == []
    rows = _by_path(out)
    assert rows["~/.claude-client"]["state"] == "signed_in"
    assert rows["~/.codex-out"]["state"] == "not_signed_in"
    assert check["check"]["ok"] and check["check"]["candidate"]["state"] == "signed_in"
    assert tried["ok"] is False and tried["row"]["error"] == "not_signed_in" and tried["row"].get("source") != "rollout"
    assert tried_default["ok"] is True and tried_default["default"] is True
    assert {c: s["state"] for c, s in states.items()} == dict.fromkeys(("claude", "codex", "grok", "muse"),
                                                                       "installed")


@pytest.mark.parametrize("grok_installed", [True, False])
def test_with_the_default_capture_home_only_khipus_own_folder_under_grok_is_read(mac, monkeypatch, grok_installed):
    monkeypatch.delenv("KHIPU_CAPTURE_HOME")
    monkeypatch.delenv("KHIPU_AEGIS_HOME", raising=False)
    assert sc.state_dir() == mac.home / ".grok" / "khipu" / "state"
    for cli in ("claude", "codex", "muse") + (("grok",) if grok_installed else ()):
        mac.install(cli)
    folders = [mac.folder(n) for n in (".claude", ".codex", ".grok", ".claude-client", ".grok-lab")]
    for f in folders:
        (f / "auth.json").write_text("{}")
    mac.session("claude_code", "a", login_home=str(mac.home / ".claude-client"))
    mac.session("aegis", "d", transcript_path=str(mac.home / ".grok-lab/sessions/e/d/updates.jsonl"))
    mac.session("aegis", "g", transcript_path=str(mac.home / ".grok/sessions/e/g/updates.jsonl"))
    _accounts({"label": "Muse", "harness": "muse"})  # so the default logins are candidates
    own = mac.home / ".grok" / "khipu"
    touched = _forbid_reads(monkeypatch, folders, allow=[own])
    out = discover.discover(now=True)
    check = discover.discover(check="~/.grok-lab")
    discover.cli_states()
    assert touched == []
    rows = _by_path(out)
    assert rows["~/.claude-client"]["sources"] == ["conversation", "name"], "the capture state was read"
    assert ("~/.grok" in rows) is grok_installed
    assert (check["check"]["candidate"]["state"] == "signed_in") is grok_installed


def _forbid_reads(monkeypatch, folders, allow=()) -> list[str]:
    """Fail any open, list or read inside ``folders`` (except under
    ``allow``); returns the list of what was touched."""
    roots = [os.path.realpath(f) for f in folders]
    allowed = [os.path.realpath(a) for a in allow]

    def under(real: str, bases) -> bool:
        return any(real == r or real.startswith(r + os.sep) for r in bases)

    def fd_path(fd: int) -> str | None:
        try:
            return os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024).rstrip(b"\0"))
        except (OSError, ValueError):
            return None  # a pipe or a closed descriptor: nothing on disk

    def inside(path, dir_fd=None, nofollow=False) -> bool:
        if isinstance(path, int):
            path, dir_fd = ".", path
        text = os.fspath(path)
        if dir_fd is not None and not os.path.isabs(text):
            base = fd_path(dir_fd)
            if base is None:
                return False
            text = os.path.join(base, text)
        # O_NOFOLLOW never goes through a final symlink: judge the link itself.
        real = (os.path.join(os.path.realpath(os.path.dirname(text)), os.path.basename(text)) if nofollow
                else os.path.realpath(text))
        return under(real, roots) and not under(real, allowed)

    touched: list[str] = []

    def guard(fn):
        def wrapped(path=None, *a, **kw):
            target = path if path is not None else "."
            flags = a[0] if a and isinstance(a[0], int) else kw.get("flags", 0)
            if inside(target, kw.get("dir_fd"), bool(flags & os.O_NOFOLLOW) if fn is real_os_open else False):
                touched.append(str(target))
                raise AssertionError(f"read inside a login folder: {target}")
            return fn(path, *a, **kw) if path is not None else fn(*a, **kw)
        return wrapped

    real_os_open = os.open
    monkeypatch.setattr(builtins, "open", guard(builtins.open))
    monkeypatch.setattr(io, "open", guard(io.open))
    monkeypatch.setattr(os, "open", guard(os.open))
    monkeypatch.setattr(os, "scandir", guard(os.scandir))
    monkeypatch.setattr(os, "listdir", guard(os.listdir))
    for name in ("iterdir", "read_text", "read_bytes"):
        real = getattr(Path, name)

        def method(self, *a, _real=real, **kw):
            if inside(self):
                touched.append(str(self))
                raise AssertionError(f"read inside a login folder: {self}")
            return _real(self, *a, **kw)

        monkeypatch.setattr(Path, name, method)
    return touched


def test_codex_rollout_off_opens_nothing_under_the_folder_and_returns_the_live_code(mac, monkeypatch):
    mac.install("codex")
    home = mac.folder(".codex-out")
    day = home / "sessions" / "2026" / "01" / "01"
    day.mkdir(parents=True)
    (day / "rollout-2026-01-01T10-00-00-new.jsonl").write_text((FIXTURES / "codex-rollout.jsonl").read_text())
    opened: list[str] = []
    real_scandir, real_open = os.scandir, builtins.open

    def scandir(path=".", *a):
        opened.append(str(path))
        return real_scandir(path, *a)

    def record_open(path, *a, **kw):
        opened.append(str(path))
        return real_open(path, *a, **kw)

    monkeypatch.setattr(usage.os, "scandir", scandir)
    monkeypatch.setattr(builtins, "open", record_open)
    row = usage.probe_codex(str(home), rollout=False)
    assert row == {"ok": False, "error": "not_signed_in"}
    assert not [p for p in opened if p.startswith(str(home))]
    row = usage.probe_account({"label": "X", "harness": "codex", "home": str(home)}, rollout=False)
    assert row["ok"] is False and row["error"] == "not_signed_in" and row["source"] is None
    # With the fallback on (tracked accounts, decision 21) it still reads the last turn.
    row = usage.probe_codex(str(home))
    assert row["ok"] is True and row["source"] == "rollout" and row["live_error"] == "not_signed_in"


# ---- try_account --------------------------------------------------------------------------------


def test_try_reads_an_unsaved_account_once_and_never_writes_the_cache(mac):
    mac.install("claude")
    mac.install("codex")
    folder = mac.folder(".claude-client")
    out = discover.try_account("claude", "~/.claude-client")
    assert out["ok"] is True and out["row"]["plan"] == "max" and out["row"]["home"] == str(folder)
    assert mac.calls("claude") == [(str(folder), " ".join(usage._CLAUDE_ARGS))]
    assert discover.try_account("codex", "~/.codex-missing") == {"ok": False, "error": "folder_not_found"}
    assert len(mac.calls("codex")) == 0
    assert discover.try_account("muse", "~/.claude-client") == {"ok": False, "error": "muse_folder"}
    assert discover.try_account("gemini")["error"] == "unknown_cli"
    assert discover.try_account("claude", str(mac.folder(".claude")))["default"] is True
    assert not usage.cache_file().exists()


# ---- the CLIs themselves, cli_path and node_folder ------------------------------------------------


def test_cli_states_installed_not_installed_and_cant_run(mac, monkeypatch):
    monkeypatch.setattr(discover, "CHILD_TIMEOUT_S", 1.0)
    mac.install("claude")
    mac.install("codex", "exit 127\n")
    mac.install("grok", "/bin/sleep 30\n")
    states = discover.cli_states()
    assert states["claude"] == {"cli": "claude", "cli_name": "Claude Code", "path": mac.scripts["claude"],
                                "state": "installed", "version": "2.1.296", "reason": None}
    assert states["codex"]["state"] == "cant_run" and states["codex"]["reason"] == "node_missing"
    assert states["grok"]["state"] == "cant_run" and states["grok"]["reason"] == "timeout"
    assert states["muse"]["state"] == "not_installed" and states["muse"]["path"] is None
    broken = mac.tmp / "bin" / "muse"
    broken.write_text("#!/nonexistent/interpreter\n")
    broken.chmod(0o755)
    mac.scripts["muse"] = str(broken)
    assert discover.cli_states()["muse"]["reason"] == "cant_start"


def _node_elsewhere_shim(tmp: Path) -> tuple[Path, Path]:
    """An nvm-style install whose node is NOT beside the CLI: ``prefix/bin/codex``
    links to a ``#!/usr/bin/env node`` script; node lives in ``node-home/bin``.
    This node is a shell script running Python."""
    prefix = tmp / "npm-prefix"
    package = prefix / "lib" / "node_modules" / "codex-fixture" / "bin"
    package.mkdir(parents=True)
    (prefix / "bin").mkdir()
    node_dir = tmp / "node-home" / "bin"
    node_dir.mkdir(parents=True)
    node = node_dir / "node"
    node.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    node.chmod(0o755)
    script = package / "codex.js"
    script.write_text("#!/usr/bin/env node\n" + textwrap.dedent(f"""
        import sys
        if sys.argv[1:] == ["--version"]:
            print("codex-cli 0.162.1")
        elif sys.argv[1:] == ["app-server"]:
            sys.stdout.write(open({str(FIXTURES / "codex.jsonl")!r}).read())
        else:
            sys.exit(64)
    """))
    script.chmod(0o755)
    shim = prefix / "bin" / "codex"
    shim.symlink_to(Path("..") / "lib" / "node_modules" / "codex-fixture" / "bin" / "codex.js")
    return shim, node_dir


def test_node_folder_lets_a_shim_answer_under_the_bare_launchd_path(tmp_path, monkeypatch, capsys):
    from khipu.cli import main

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    shim, node_dir = _node_elsewhere_shim(tmp_path)
    _launchd_lookup(monkeypatch, home)
    monkeypatch.setattr(usage, "_TOOL_DIRS", ())  # no node of this Mac's own on the probe PATH
    monkeypatch.setattr(usage, "_command", lambda h: str(shim) if h == "codex" else None)
    assert discover.cli_states()["codex"]["reason"] == "node_missing"
    assert usage.probe_codex(rollout=False) == {"ok": False, "error": "exit_127"}
    assert main(["config", "--set", "node_folder", str(tmp_path / "data")]) == 2
    refused = json.loads(capsys.readouterr().out)
    assert refused["ok"] is False and "node" in refused["error"]
    assert main(["config", "--set", "node_folder", str(node_dir)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["cli_settings"]["node_folder"] == {"value": str(node_dir), "usable": True}
    assert usage.probe_environment("codex", None, str(shim))["PATH"].split(os.pathsep)[0] == str(node_dir)
    state = discover.cli_states()["codex"]
    assert state["state"] == "installed" and state["version"] == "0.162.1"
    row = usage.probe_codex(rollout=False)
    assert row["ok"] is True and row["plan"] == "pro"
    assert main(["config"]) == 0
    assert json.loads(capsys.readouterr().out)["cli_settings"]["node_folder"]["usable"] is True
    assert main(["config", "--unset", "node_folder"]) == 0
    capsys.readouterr()
    assert discover.cli_states()["codex"]["reason"] == "node_missing"


def test_cli_path_is_honoured_by_the_lookup_after_the_override(tmp_path, monkeypatch, capsys):
    from khipu.cli import main

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    lookup = _launchd_lookup(monkeypatch, home)
    chosen = tmp_path / "apps" / "my-codex"
    chosen.parent.mkdir()
    chosen.write_text("#!/bin/sh\nexit 0\n")
    chosen.chmod(0o644)
    assert lookup.binary("codex") is None
    assert main(["config", "--set", "cli_path.codex", str(chosen)]) == 2
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "cli_path.codex must be an executable file",
                                                   "key": "cli_path.codex"}
    assert main(["config", "--set", "cli_path.codex", "relative/codex"]) == 2
    capsys.readouterr()
    chosen.chmod(0o755)
    assert main(["config", "--set", "cli_path.codex", str(chosen)]) == 0
    assert json.loads(capsys.readouterr().out)["cli_settings"]["cli_path.codex"]["usable"] is True
    assert synth_cli.binary("codex") == str(chosen)
    other = tmp_path / "override-codex"
    other.write_text("#!/bin/sh\n")
    monkeypatch.setenv("KHIPU_CODEX_CLI", str(other))
    assert synth_cli.binary("codex") == str(other), "the environment override still wins"
    monkeypatch.delenv("KHIPU_CODEX_CLI")
    chosen.unlink()
    assert config.cli_setting("cli_path.codex") is None and synth_cli.binary("codex") is None
    assert main(["config", "--unset", "cli_path.codex"]) == 0
    capsys.readouterr()
    assert "cli_path.codex" not in config.load_config()


# ---- the CLI and the agents' view ------------------------------------------------------------------


def test_the_cli_json(mac, capsys):
    from khipu.cli import main

    with config.mutate_config() as data:
        data[config.AUTO_TRACK_KEY] = False  # explicit off: no tracking, no usage cache
    mac.install("claude")
    mac.folder(".claude-client")
    mac.session("claude_code", "x", login_home=str(mac.folder(".claude-used")))
    assert main(["usage", "--discover", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["looking"] is False and out["last_run"]["checked"] == 1
    rows = _by_path(out)
    assert rows["~/.claude-used"]["state"] == "signed_in" and rows["~/.claude-client"]["state"] == "not_checked"
    assert out["clis"]["claude"]["state"] == "installed" and out["clis"]["codex"]["state"] == "not_installed"
    assert main(["usage", "--discover", "--json", "--check", "~/.claude-client"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["check"]["candidate"]["state"] == "signed_in" and "clis" not in out
    assert main(["usage", "--discover", "--json", "--check", "~/nothing"]) == 2
    assert json.loads(capsys.readouterr().out)["check"]["error"] == "folder_not_found"
    assert main(["usage", "--discover", "--check", "~/nothing"]) == 2
    assert "folder_not_found" in capsys.readouterr().out
    assert main(["usage", "--try", "--harness", "claude", "--folder", "~/.claude-client"]) == 0
    assert json.loads(capsys.readouterr().out)["row"]["plan"] == "max"
    assert main(["usage", "--try", "--harness", "codex", "--folder", "~/.codex-nope"]) == 2
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "folder_not_found"}
    assert not usage.cache_file().exists()


def test_no_mcp_tool_starts_discovery():
    from khipu import mcp_server

    source = Path(mcp_server.__file__).read_text()
    assert "khipu.discover" not in source and "import discover" not in source
    assert "try_account" not in source and "forget_login" not in source and "cli_states" not in source


def test_the_lock_is_a_real_flock_next_to_the_usage_cache(mac):
    fd = discover._try_lock()
    try:
        assert discover._try_lock() is None
        path = usage.cache_file().with_name(discover.LOCK_NAME)
        other = os.open(path, os.O_RDWR)
        try:
            with pytest.raises(OSError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(other)
    finally:
        usage._release(fd)


# ---- Codex review of 0cec87d ------------------------------------------------------------


def test_a_symlinked_state_folder_never_redirects_a_read_into_a_login_folder(mac, monkeypatch):
    """Khipu's capture ``state`` folder, or the capture root itself, swapped
    for a symlink into a login folder: nothing in it is listed or read, and
    Forget and the sweep write nothing there."""
    login = mac.folder(".claude-client")
    planted = {"seen_ts": NOW, sc.LOGIN_HOME_FIELD: str(login)}
    (login / "claude_code--x.json").write_text(json.dumps(planted))
    (login / ".credentials.json").write_text("{}")
    capture = mac.tmp / "capture"
    capture.mkdir(exist_ok=True)
    (capture / "state").symlink_to(login)
    touched = _forbid_reads(monkeypatch, [login])
    assert discover._source_sessions(NOW) == []
    assert sc.forget_login_homes() == {"cleared": 0, "pending": 0, "failed": 0, "unreachable": True}
    assert sc.sweep_login_homes(force=True) == 0
    discover.discover(now=True)
    assert touched == []
    monkeypatch.undo()
    assert json.loads((login / "claude_code--x.json").read_text()) == planted
    # The capture root itself a symlink into a login folder.
    (capture / "state").unlink()
    capture.rmdir()
    (login / "state").mkdir()
    (login / "state" / "claude_code--y.json").write_text(json.dumps(planted))
    capture.symlink_to(login)
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(capture))
    monkeypatch.setenv("HOME", str(mac.home))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(mac.tmp / "data"))
    touched = _forbid_reads(monkeypatch, [login])
    assert discover._source_sessions(NOW) == []
    assert sc.forget_login_homes() == {"cleared": 0, "pending": 0, "failed": 0, "unreachable": True}
    assert touched == []


def test_a_state_entry_swapped_for_a_symlink_after_listing_is_never_followed(mac, monkeypatch):
    login = mac.folder(".codex-work")
    target = login / "auth.json"
    target.write_text(json.dumps({"seen_ts": NOW, sc.LOGIN_HOME_FIELD: str(login)}))
    good = mac.session("codex", "good", login_home=str(mac.folder(".codex-mine")))
    swapped = mac.session("codex", "swap", login_home=str(login))
    real_names = sc._state_names

    def names_then_swap(dfd):
        names = real_names(dfd)
        swapped.unlink()
        swapped.symlink_to(target)  # the listing saw a file; the open meets a symlink
        return names

    monkeypatch.setattr(sc, "_state_names", names_then_swap)
    touched = _forbid_reads(monkeypatch, [login])
    got = discover._source_sessions(NOW)
    assert touched == []
    assert [str(p) for _c, p, _n in got] == [str(mac.home / ".codex-mine")]
    swapped.unlink()
    swapped.write_text(json.dumps({"seen_ts": NOW, sc.LOGIN_HOME_FIELD: str(login)}))
    swapped.unlink()
    swapped.symlink_to(target)
    monkeypatch.setattr(sc, "_state_names", real_names)
    assert sc.forget_login_homes() == {"cleared": 1, "pending": 0, "failed": 0, "unreachable": False}  # the good file only
    assert touched == []
    assert sc.LOGIN_HOME_FIELD not in json.loads(good.read_text())
    monkeypatch.undo()  # the test itself may now look inside the fake login folder
    assert json.loads(target.read_text())[sc.LOGIN_HOME_FIELD] == str(login), "nothing written through the link"


def test_forget_on_a_busy_session_is_not_finished_until_the_next_sweep(mac, capsys):
    from khipu.cli import main

    d = sc.state_dir()
    path = mac.session("claude_code", "busy", login_home="/x/.claude-a", offset=7)
    free = mac.session("codex", "free", login_home="/x/.codex-b")
    lock = path.with_suffix(".lock")
    lock.mkdir()
    sc.sweep_login_homes(force=True)  # the daily sweep has just run
    assert main(["usage", "--forget-login-paths"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": True, "finished": False, "cleared": 1, "pending": 1, "failed": 0, "unreachable": False,
                   "note": "Not finished: 1 conversation was busy; Khipu clears it when it is free"}
    assert sc.LOGIN_HOME_FIELD in json.loads(path.read_text())
    assert sc.LOGIN_HOME_FIELD not in json.loads(free.read_text())
    # Still held: a sweep keeps it pending, and asking again says so.
    assert sc.sweep_login_homes() == 0
    assert sc.login_homes_pending() == 1
    lock.rmdir()
    # Unlocked: the next sweep (even inside its daily wait) applies the erasure.
    assert sc.sweep_login_homes() == 1
    assert json.loads(path.read_text()) == {"seen_ts": NOW, "offset": 7}
    assert sc.login_homes_pending() == 0
    assert main(["usage", "--forget-login-paths"]) == 0
    assert json.loads(capsys.readouterr().out)["finished"] is True
    assert not (d / "login-homes.pending").exists()


def test_the_hook_applies_a_pending_erasure_after_its_own_lock_is_released(mac, monkeypatch):
    folder = mac.folder(".claude-client")
    transcript = _claude_transcript(folder / "projects" / "p" / "s3.jsonl")
    payload = json.dumps({"hook_event_name": "Stop", "session_id": "s3", "transcript_path": str(transcript),
                          "cwd": str(mac.tmp)})
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(folder))
    sc.hook_main(payload, "claude_code")
    path = sc._state_file("claude_code", "s3")
    assert json.loads(path.read_text())[sc.LOGIN_HOME_FIELD] == str(folder)
    lock = path.with_suffix(".lock")
    lock.mkdir()
    assert sc.forget_login_homes() == {"cleared": 0, "pending": 1, "failed": 0, "unreachable": False}
    lock.rmdir()
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    sc.hook_main(payload, "claude_code")  # loads the old state, saves it, then releases its lock
    assert sc.LOGIN_HOME_FIELD not in json.loads(path.read_text())
    assert sc.login_homes_pending() == 0


def test_version_checks_run_at_most_two_at_a_time(mac, monkeypatch):
    spans = mac.rec / "spans"
    for cli in discover.CLIS:
        mac.install(cli, textwrap.dedent(f"""\
            s=$("{sys.executable}" -c 'import time;print(time.time())')
            /bin/sleep 0.6
            e=$("{sys.executable}" -c 'import time;print(time.time())')
            echo "$s $e" >> "{spans}"
            echo "1.0.0"
        """))
    states = discover.cli_states()
    assert all(states[c]["state"] == "installed" for c in discover.CLIS), states
    rows = [tuple(map(float, line.split())) for line in spans.read_text().splitlines()]
    assert len(rows) == 4
    most = max(sum(1 for s, e in rows if s <= t < e) for t, _ in rows)
    assert most <= discover.PARALLEL == 2


# ---- review fixes (2026-10-15) -----------------------------------------------------------


def test_the_agent_scrub_knows_folders_discovery_found_but_nobody_tracks(mac, monkeypatch):
    from khipu import agent_view

    mac.install("codex")
    mac.folder(".codex-work")
    out = discover.discover(now=True)
    assert "~/.codex-work" in {c["path"] for c in out["candidates"]}
    monkeypatch.setattr(agent_view, "_TABLE", {"sig": None, "at": 0.0, "table": None})
    for text in ("limit hit on .codex-work", "running on codex-work now"):
        assert "work" not in agent_view.scrub(text), text
    # A default login found by the name scan stays home-less: the CLI's own
    # word is never taken for a folder name.
    mac.folder(".grok")
    monkeypatch.setattr(agent_view, "_TABLE", {"sig": None, "at": 0.0, "table": None})
    assert agent_view.scrub("Grok (heavy) week at 90%") == "Grok (heavy) week at 90%"


def test_a_relative_cli_path_in_a_hand_edited_config_is_never_honoured(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    _launchd_lookup(monkeypatch, home)
    work = tmp_path / "cwd"
    (work / "rel").mkdir(parents=True)
    for name in ("codex", "node"):
        planted = work / "rel" / name
        planted.write_text("#!/bin/sh\nexit 0\n")
        planted.chmod(0o755)
    with config.mutate_config() as data:
        data["cli_path.codex"] = "rel/codex"
        data["node_folder"] = "rel"
    monkeypatch.chdir(work)
    assert config.cli_setting("cli_path.codex") is None
    assert config.cli_setting("node_folder") is None
    got = synth_cli.binary("codex")
    assert got is None or Path(got).is_absolute(), got


def test_a_symlinked_capture_home_records_no_login_path_and_forget_says_not_finished(mac, monkeypatch, capsys):
    from khipu.cli import main

    real = mac.tmp / "real-capture"
    real.mkdir()
    link = mac.tmp / "capture-link"
    link.symlink_to(real)
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(link))
    folder = mac.folder(".claude-client")
    transcript = _claude_transcript(folder / "projects" / "p" / "s9.jsonl")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(folder))
    sc.hook_main(json.dumps({"hook_event_name": "Stop", "session_id": "s9", "transcript_path": str(transcript),
                             "cwd": str(mac.tmp)}), "claude_code")
    state = sc.state_dir() / "claude_code--s9.json"
    assert sc.LOGIN_HOME_FIELD not in json.loads(state.read_text())
    # One written before the home became a link (or by hand) is never claimed cleared.
    state.write_text(json.dumps({"seen_ts": NOW, sc.LOGIN_HOME_FIELD: "/x/.claude-client"}))
    assert main(["usage", "--forget-login-paths"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["finished"] is False and out["unreachable"] is True and "could not open" in out["note"]


def test_forget_is_not_finished_when_a_record_cannot_be_rewritten_and_retries_it(mac, monkeypatch, capsys):
    import errno

    from khipu.cli import main

    state = mac.session("codex", "a", login_home="/x/.codex-a")
    real_write = sc._write_at

    def disk_full(dfd, name, *a, **k):
        if name.endswith(".json"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(dfd, name, *a, **k)

    monkeypatch.setattr(sc, "_write_at", disk_full)
    assert main(["usage", "--forget-login-paths"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["finished"] is False and out["failed"] == 1 and out["cleared"] == 0
    assert sc.LOGIN_HOME_FIELD in json.loads(state.read_text())
    monkeypatch.setattr(sc, "_write_at", real_write)
    assert sc.sweep_login_homes() == 1  # the next capture's sweep retries it
    assert sc.LOGIN_HOME_FIELD not in json.loads(state.read_text())


def test_the_last_run_counts_folders_that_timed_out(mac, monkeypatch):
    monkeypatch.setattr(discover, "CHILD_TIMEOUT_S", 1.0)
    monkeypatch.setattr(usage, "OVERALL_GRACE_S", 0.5)
    mac.install("claude", "/bin/sleep 10\n")
    folder = mac.folder(".claude-slow")
    mac.session("claude_code", "s", login_home=str(folder))
    out = discover.discover(at=NOW)
    row = next(c for c in out["candidates"] if c["path"] == "~/.claude-slow")
    assert row["state"] == "couldnt_check" and row["reason"] == "timeout"
    assert out["last_run"]["timed_out"] == 1 and out["last_run"]["couldnt_check"] >= 1


@pytest.mark.parametrize("entry", [{}, {"id": "x"}, {"id": "x", "cli": "claude", "identity": 3, "sources": []},
                                   {"id": "x", "cli": "claude", "identity": "path:x", "sources": "conversation"},
                                   {"id": "x", "cli": "claude", "identity": "path:x", "sources": [],
                                    "abs": {"not": "a path"}}])
def test_a_malformed_stored_candidate_is_dropped_never_a_crash(mac, entry):
    store = discover.store_file()
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({"version": 1, "candidates": {"x": entry}}))
    fd = discover._try_lock()  # another run is looking
    assert fd is not None
    try:
        out = discover.discover(at=NOW)
    finally:
        usage._release(fd)
    assert out["looking"] is True and out["candidates"] == []
    assert discover.discover(at=NOW)["looking"] is False  # and a real run gets past it too


# ---- slice 9c: bounded settings parser, login evidence, subscriptions (case 17a) ----

TOKEN_SENTINEL = "SENTINEL-s9c-token-never-out"


def _settings(folder: Path, payload) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (folder / "settings.json").write_text(text)


def test_bounded_settings_outputs_only_a_validated_host_and_token_presence(mac):
    folder = mac.tmp / "login"
    _settings(folder, {"env": {"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic?key=1",
                                "ANTHROPIC_AUTH_TOKEN": TOKEN_SENTINEL},
                       "other": TOKEN_SENTINEL})
    got = discover.bounded_settings(folder)
    assert got == {"host": "api.z.ai", "provider": "glm", "has_token": True, "unreadable": False}
    assert TOKEN_SENTINEL not in json.dumps(got)
    for url in ("https://user:pw@api.z.ai/api", "https://evil.invalid/api", "not a url"):
        _settings(folder, {"env": {"ANTHROPIC_BASE_URL": url}})
        assert discover.bounded_settings(folder)["host"] is None, url
    _settings(folder, "{broken " + TOKEN_SENTINEL)
    got = discover.bounded_settings(folder)
    assert got == {"host": None, "provider": None, "has_token": False, "unreadable": True}
    assert TOKEN_SENTINEL not in json.dumps(got)
    assert discover.bounded_settings(mac.tmp / "missing") == {
        "host": None, "provider": None, "has_token": False, "unreadable": False}


def test_a_claude_home_pointing_at_zai_is_offered_as_glm_and_the_token_never_shows(mac, capsys):
    mac.install("claude")
    folder = mac.folder(".claude-custom")
    _settings(folder, {"env": {"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
                                "ANTHROPIC_AUTH_TOKEN": TOKEN_SENTINEL}})
    out = discover.discover(at=NOW)
    glm = [c for c in out["candidates"] if c["cli"] == "glm"]
    assert len(glm) == 1 and glm[0]["state"] == "signed_in" and glm[0]["offer_on_home"] is True
    assert glm[0]["provider_host"] == "api.z.ai" and glm[0]["login_evidence"] == ["settings_token"]
    channels = [json.dumps(out), discover.store_file().read_text(), capsys.readouterr().out]
    assert all(TOKEN_SENTINEL not in channel for channel in channels)


@pytest.mark.parametrize("folder_name", [".claude-glm", ".claude-glm-orbit"])
def test_a_name_only_glm_home_without_link_targets_keeps_its_token_login(mac, capsys, folder_name):
    mac.install("claude")
    _accounts()
    folder = mac.folder(folder_name)
    _settings(folder, {"env": {"ANTHROPIC_AUTH_TOKEN": TOKEN_SENTINEL}})
    assert not any(t["provider"] == "glm" for t in discover._link_targets())

    out = discover.discover(at=NOW)
    glm = [c for c in out["candidates"] if c["cli"] == "glm"]
    assert len(glm) == 1
    assert glm[0]["state"] == "signed_in"
    assert glm[0]["default"] is True and glm[0]["offer_on_home"] is True
    assert glm[0]["path"] is None
    assert glm[0]["login_evidence"] == ["settings_token"]
    assert not [call for call in mac.calls("claude") if call[0] == str(folder)]
    captured = capsys.readouterr()
    channels = [json.dumps(out), discover.store_file().read_text(), captured.out, captured.err,
                json.dumps(mac.calls("claude"))]
    assert all(TOKEN_SENTINEL not in channel for channel in channels)


def test_a_folder_holding_only_worktrees_is_not_offered(mac):
    box = mac.folder(".claude-box")
    (box / "worktrees").mkdir()
    real = mac.folder(".claude-real")
    (real / "auth.json").write_text("{}")
    found, _t, _d = discover._gather(NOW)
    paths = {r["path"] for r in found.values()}
    assert "~/.claude-box" not in paths and "~/.claude-real" in paths


def test_a_keychain_backed_claude_home_with_no_auth_file_is_offered_when_its_cli_reports_signed_in(mac):
    mac.install("claude")
    folder = mac.folder(".claude-keychain")
    mac.session("claude_code", "k", login_home=str(folder))
    out = discover.discover(at=NOW)
    row = _by_path(out)["~/.claude-keychain"]
    assert row["state"] == "signed_in" and row["offer_on_home"] is True
    assert row["login_evidence"] == [], "no auth file: the CLI's signed-in status is the evidence"


def _grok_homes(mac, monkeypatch, account_ids):
    mac.install("grok")
    folders = []
    for name in (".grok-a", ".grok-b", ".grok-c"):
        folder = mac.folder(name)
        folders.append(folder)
        mac.session("aegis", name, login_home=str(folder))

    def probe(home, timeout=None):
        return {"ok": True, "plan": "SuperGrok", "windows": [],
                "account_id": account_ids[Path(home).name]}

    monkeypatch.setattr(usage, "probe_grok", probe)
    return folders


def test_three_grok_homes_are_one_account_only_when_the_cli_account_status_agrees(mac, monkeypatch):
    _grok_homes(mac, monkeypatch, {".grok-a": "acct-1", ".grok-b": "acct-1", ".grok-c": "acct-1"})
    out = discover.discover(at=NOW)
    grok = [c for c in out["candidates"] if c["cli"] == "grok"]
    assert len(grok) == 1 and grok[0]["merged"] is True and len(grok[0]["homes"]) == 3
    assert "acct-1" not in json.dumps(out), "only a digest of the account id is kept"


def test_three_grok_homes_stay_separate_until_the_owner_merges_them(mac, monkeypatch):
    _grok_homes(mac, monkeypatch, {".grok-a": "acct-1", ".grok-b": "acct-2", ".grok-c": "acct-3"})
    out = discover.discover(at=NOW)
    grok = [c for c in out["candidates"] if c["cli"] == "grok"]
    assert len(grok) == 3 and not any(c["merged"] for c in grok)
    merged = discover.merge_same_subscription([c["id"] for c in grok])
    assert merged["ok"] is True
    out = discover.discover(at=NOW + 60)
    grok = [c for c in out["candidates"] if c["cli"] == "grok"]
    assert len(grok) == 1 and grok[0]["merged"] is True and len(grok[0]["homes"]) == 3


# ---- slice 9c fix round 1 (probes copied from review, fake homes only) ----

def test_deeply_nested_settings_are_unreadable_and_discovery_survives(mac):
    mac.install("claude")
    folder = mac.folder(".claude-deep")
    (folder / "settings.json").write_bytes(b"[" * 32000 + b"]" * 32000)
    assert discover.bounded_settings(folder)["unreadable"] is True
    (folder / "settings.json").write_bytes((b'{"a":' * 6000) + b"1" + b"}" * 6000)
    assert discover.bounded_settings(folder)["unreadable"] is True
    mac.session("claude_code", "d", login_home=str(folder))
    out = discover.discover(at=NOW)
    row = _by_path(out)["~/.claude-deep"]
    assert row["settings"] == "unreadable"


def test_bounded_settings_rejects_a_fifo_without_blocking(mac):
    folder = mac.folder(".claude-fifo")
    os.mkfifo(folder / "settings.json")
    assert discover.bounded_settings(folder)["unreadable"] is True


def test_bounded_settings_validates_scheme_and_port(mac):
    folder = mac.tmp / "login"
    for url, host in (("http://api.z.ai:8080/x", None), ("https://api.z.ai:8080/x", None),
                      ("https://api.z.ai:443/x", "api.z.ai"), ("https://api.z.ai/x", "api.z.ai")):
        _settings(folder, {"env": {"ANTHROPIC_BASE_URL": url, "ANTHROPIC_AUTH_TOKEN": TOKEN_SENTINEL}})
        assert discover.bounded_settings(folder)["host"] == host, url


def test_a_settings_token_signs_in_only_a_provider_pointed_home(mac):
    mac.install("claude")
    plain = mac.folder(".claude-out")
    _settings(plain, {"env": {"ANTHROPIC_API_KEY": TOKEN_SENTINEL}})
    mac.session("claude_code", "o", login_home=str(plain))
    out = discover.discover(at=NOW)
    row = _by_path(out)["~/.claude-out"]
    assert row["state"] == "not_signed_in" and row["offer_on_home"] is False
    assert mac.calls("claude"), "a non-provider home is asked through its CLI"


def test_a_provider_pointed_settings_token_signs_in_without_asking_the_cli(mac):
    mac.install("claude")
    folder = mac.folder(".claude-out2")
    _settings(folder, {"env": {"ANTHROPIC_BASE_URL": "https://api.anthropic.com",
                                "ANTHROPIC_API_KEY": TOKEN_SENTINEL}})
    mac.session("claude_code", "p", login_home=str(folder))
    out = discover.discover(at=NOW)
    row = _by_path(out)["~/.claude-out2"]
    assert row["state"] == "signed_in"
    assert not [c for c in mac.calls("claude") if c[1] == "auth status --json"]


def test_a_worktree_folder_with_real_login_contents_is_still_found(mac):
    real = mac.folder(".claude-wtreal")
    (real / "worktrees").mkdir()
    (real / "projects").mkdir()
    (real / "history.jsonl").write_text("x")
    found, _t, _d = discover._gather(NOW)
    assert "~/.claude-wtreal" in {r["path"] for r in found.values()}


def test_collapsed_homes_keep_their_check_state(mac, monkeypatch):
    _grok_homes(mac, monkeypatch, {".grok-a": "acct-1", ".grok-b": "acct-1", ".grok-c": "acct-1"})
    calls = []
    inner = usage.probe_grok
    monkeypatch.setattr(usage, "probe_grok", lambda home, timeout=None: (calls.append(Path(home).name), inner(home, timeout=timeout))[1])
    discover.discover(at=NOW)
    assert len(calls) == 3
    calls.clear()
    out = discover.discover(at=NOW + 60)
    grok = [c for c in out["candidates"] if c["cli"] == "grok"]
    assert len(grok) == 1 and grok[0]["merged"] is True
    assert calls == [], "collapsed homes are not re-checked on the next run"


def test_owner_merge_updates_slice3a_bindings(mac, monkeypatch):
    folders = _grok_homes(mac, monkeypatch, {".grok-a": "acct-1", ".grok-b": "acct-2", ".grok-c": "acct-3"})
    out = discover.discover(at=NOW)
    grok = [c for c in out["candidates"] if c["cli"] == "grok"]
    from khipu import routing_identity
    with config.mutate_config() as data:
        data[routing_identity.BINDINGS] = {
            "grok:main": {"harness": "grok", "home": str(folders[0])},
            "grok:second": {"harness": "grok", "home": str(folders[1])}}
    discover.merge_same_subscription([c["id"] for c in grok])
    bindings = config.load_config()[routing_identity.BINDINGS]
    homes = {v["home"] for v in bindings.values() if v.get("harness") == "grok"}
    assert len(homes) == 1 and homes <= {str(f) for f in folders}, \
        "merged homes bind through the primary home only"


def test_auto_track_default_only_for_a_genuinely_new_install(mac):
    assert config.auto_track() is True, "no config file at all: a new install"
    cf = config.config_file()
    cf.parent.mkdir(parents=True, exist_ok=True)
    for payload in ('{"usage_auto_track": 0}', '{"usage_auto_track": "false"}',
                    '{"usage_auto_track": null}', '{broken', ''):
        cf.write_text(payload)
        assert config.auto_track() is False, payload
    cf.write_text('{"capture_mode": "dual"}')
    assert config.auto_track() is False, "a pre-existing install with no key stays off"
    stored = json.loads(cf.read_text())
    assert stored[config.AUTO_TRACK_KEY] is False and stored.get(config.AUTO_TRACK_MIGRATION_KEY) is True
    cf.write_text('{"usage_auto_track": true}')
    assert config.auto_track() is True


# ---- slice 9c fix round 2 ----

def test_a_recheck_reporting_no_id_splits_previously_merged_homes(mac):
    marker = mac.tmp / "claude-noid"
    body = textwrap.dedent(f"""\
        case "$*" in
          "auth status --json")
            if [ -z "$CLAUDE_CONFIG_DIR" ]; then echo '{{"loggedIn": false}}'; exit 1; fi
            if [ -f "{marker}" ]; then echo '{{"loggedIn": true}}'; else echo '{{"loggedIn": true, "userId": "U1-shared"}}'; fi
            exit 0;;
          "--version") echo "2.1.296 (Claude Code)"; exit 0;;
          -p*) cat "{FIXTURES / 'claude.jsonl'}"; exit 0;;
        esac
        exit 64
    """)
    mac.install("claude", body)
    one = mac.folder(".claude-one")
    two = mac.folder(".claude-two")
    mac.session("claude_code", "one", login_home=str(one))
    mac.session("claude_code", "two", login_home=str(two))
    out = discover.discover(at=NOW)
    merged = [c for c in out["candidates"] if c.get("merged")]
    assert len(merged) == 1 and set(merged[0]["homes"]) == {"~/.claude-one", "~/.claude-two"}
    marker.write_text("no id")
    out = discover.discover(now=True, at=NOW + 60)
    rows = _by_path(out)
    assert rows["~/.claude-one"]["state"] == "signed_in" and rows["~/.claude-one"]["merged"] is False
    assert rows["~/.claude-two"]["state"] == "signed_in" and rows["~/.claude-two"]["merged"] is False


def test_bounded_settings_does_not_follow_a_symlinked_settings_file(mac):
    folder = mac.folder(".claude-linked")
    outside = mac.tmp / "outside-settings.json"
    outside.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
                                            "ANTHROPIC_AUTH_TOKEN": TOKEN_SENTINEL}}))
    (folder / "settings.json").symlink_to(outside)
    got = discover.bounded_settings(folder)
    assert got == {"host": None, "provider": None, "has_token": False, "unreadable": True}
    assert TOKEN_SENTINEL not in json.dumps(got)


@pytest.mark.parametrize('cli,provider,folder,evidence', [
    ('claude', 'atlas', '.claude-atlas-orbit', 'folder_provider_name'),
    ('grok', 'grok', '.grok-orbit', 'same_cli_folder'),
    ('claude', 'glm', '.claude-glm-orbit', 'folder_provider_name'),
])
def test_folder_link_guess_is_public_and_requires_owner_confirmation(mac, monkeypatch, cli, provider, folder, evidence):
    from khipu import routing

    path = mac.folder(folder)
    _accounts()
    known = [{'key': provider + ':fictional', 'harness': provider, 'label': 'Orbit plan',
              'home': None, 'definition_id': provider + ':fictional'}]
    monkeypatch.setattr(routing, 'accounts', lambda: known)
    monkeypatch.setattr(routing, '_identity_snapshot', lambda accounts: ((
        {a['key']: a['definition_id'] for a in accounts}, {}, {}), {}, {}))
    before = config.config_file().read_bytes()
    found, _, _ = discover._gather(NOW)
    view = discover._view({'candidates': found})
    row = _by_path(view)['~/' + folder]
    assert row['cli'] == cli
    assert row['suggested_link'] == {
        'account_id': provider + ':fictional', 'label': 'Orbit plan', 'provider': provider,
        'evidence': [evidence], 'requires_owner_confirmation': True,
    }
    assert row['state'] == discover.NOT_CHECKED
    assert row['needs_owner_check'] is True
    assert config.config_file().read_bytes() == before
    assert not routing.events_file().exists()
    assert path.is_dir()


def test_folder_link_guess_refuses_ambiguous_accounts_and_conflicting_settings(mac, monkeypatch):
    from khipu import routing

    folder = mac.folder('.claude-atlas-orbit')
    _accounts()
    known = [{'key': 'atlas:' + suffix, 'harness': 'atlas', 'label': 'Orbit ' + suffix,
              'home': None, 'definition_id': 'atlas:' + suffix} for suffix in ('one', 'two')]
    monkeypatch.setattr(routing, 'accounts', lambda: known)
    monkeypatch.setattr(routing, '_identity_snapshot', lambda accounts: ((
        {a['key']: a['definition_id'] for a in accounts}, {}, {}), {}, {}))
    assert _by_path(discover._view({'candidates': discover._gather(NOW)[0]}))[
        '~/.claude-atlas-orbit']['suggested_link'] is None
    known.pop()
    _settings(folder, {'env': {'ANTHROPIC_BASE_URL': 'https://api.anthropic.com'}})
    assert _by_path(discover._view({'candidates': discover._gather(NOW)[0]}))[
        '~/.claude-atlas-orbit']['suggested_link'] is None


def test_folder_link_guess_refresh_drops_removed_target(mac, monkeypatch):
    from khipu import routing

    mac.folder('.grok-orbit')
    _accounts()
    known = [{'key': 'grok:fictional', 'harness': 'grok', 'label': 'Orbit plan',
              'home': None, 'definition_id': 'grok:fictional'}]
    monkeypatch.setattr(routing, 'accounts', lambda: known)
    monkeypatch.setattr(routing, '_identity_snapshot', lambda accounts: ((
        {a['key']: a['definition_id'] for a in accounts}, {}, {}), {}, {}))
    assert _by_path(discover.discover(at=NOW))['~/.grok-orbit']['suggested_link']
    known.clear()
    assert _by_path(discover.discover(at=NOW + 10))['~/.grok-orbit']['suggested_link'] is None



def test_folder_link_guess_uses_existing_routing_identity_without_writes(mac):
    from khipu import routing

    mac.folder('.grok-orbit')
    _accounts({'label': 'Orbit plan', 'harness': 'grok', 'home': None})
    before = config.config_file().read_bytes()
    row = _by_path(discover.discover(at=NOW))['~/.grok-orbit']
    assert row['suggested_link']['account_id'] == 'grok:orbit-plan'
    assert config.config_file().read_bytes() == before
    assert not routing.events_file().exists()


def test_folder_link_guess_preserves_ambiguous_glm_folder_and_refuses_near_names(mac, monkeypatch):
    from khipu import routing

    folder = mac.folder('.claude-glm-orbit')
    mac.folder('.claude-glimmer-orbit')
    _settings(folder, {'env': {'ANTHROPIC_BASE_URL': 'https://api.z.ai/api/anthropic'}})
    _accounts({'label': 'Orbit plan', 'harness': 'glm', 'home': None})
    known = [{'key': 'glm:' + suffix, 'harness': 'glm', 'label': 'Orbit ' + suffix,
              'home': None, 'definition_id': 'glm:' + suffix} for suffix in ('one', 'two')]
    monkeypatch.setattr(routing, 'accounts', lambda: known)
    monkeypatch.setattr(routing, '_identity_snapshot', lambda accounts: ((
        {a['key']: a['definition_id'] for a in accounts}, {}, {}), {}, {}))
    rows = _by_path(discover._view({'candidates': discover._gather(NOW)[0]}))
    assert rows['~/.claude-glm-orbit']['cli'] == 'claude'
    assert rows['~/.claude-glm-orbit']['suggested_link'] is None
    assert rows['~/.claude-glimmer-orbit']['suggested_link'] is None


# ---- z.ai folders never reach the claude CLI ----------------------------------------------


def _glm_probe_stub(monkeypatch) -> list:
    asked = []
    monkeypatch.setattr(usage, "probe_glm", lambda **kw: asked.append(kw) or {"ok": True, "plan": "Coding Plan"})
    return asked


def test_automatic_discover_never_asks_claude_about_a_tracked_name_only_glm_folder(mac, monkeypatch):
    mac.install("claude")
    _accounts({"label": "Z plan", "harness": "glm", "home": None})
    folder = mac.folder(".claude-glm")
    mac.session("claude_code", "z", login_home=str(folder))
    assert any(t["provider"] == "glm" for t in discover._link_targets())
    asked = _glm_probe_stub(monkeypatch)

    out = discover.discover(at=NOW)
    row = _by_path(out)["~/.claude-glm"]
    assert row["cli"] == "claude" and row["state"] == discover.SIGNED_IN
    assert asked
    assert not [c for c in mac.calls("claude") if c[0] == str(folder)]


def test_owner_check_never_asks_claude_about_a_tracked_zai_folder(mac, monkeypatch):
    mac.install("claude")
    _accounts({"label": "Z plan", "harness": "glm", "home": None})
    folder = mac.folder(".claude-glm")
    asked = _glm_probe_stub(monkeypatch)

    out = discover.discover(check="~/.claude-glm", at=NOW)
    assert out["check"]["ok"] is True
    assert asked
    assert not [c for c in mac.calls("claude") if c[0] == str(folder)]


def test_owner_check_routes_a_settings_pointed_zai_folder_to_the_glm_probe(mac, monkeypatch):
    mac.install("claude")
    _accounts({"label": "Z plan", "harness": "glm", "home": None})
    folder = mac.folder(".claude-custom")
    _settings(folder, {"env": {"ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic"}})
    asked = _glm_probe_stub(monkeypatch)

    discover.discover(check="~/.claude-custom", at=NOW)
    assert asked
    assert not [c for c in mac.calls("claude") if c[0] == str(folder)]


def test_a_row_collapsed_to_glm_drops_another_providers_suggested_link(mac, monkeypatch):
    from khipu import routing

    mac.folder(".claude-glm-orbit")
    _settings(mac.home / ".claude-glm-orbit", {"env": {"ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic"}})
    _accounts()
    known = [{"key": "deepseek:fictional", "harness": "deepseek", "label": "Orbit plan",
              "home": None, "definition_id": "deepseek:fictional"}]
    monkeypatch.setattr(routing, "accounts", lambda: known)
    monkeypatch.setattr(routing, "_identity_snapshot", lambda accounts: ((
        {a["key"]: a["definition_id"] for a in accounts}, {}, {}), {}, {}))
    found, _, _ = discover._gather(NOW)
    glm = [c for c in found.values() if c["cli"] == "glm"]
    assert len(glm) == 1 and glm[0]["suggested_link"] is None
