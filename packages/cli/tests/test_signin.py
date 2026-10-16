"""Signing in a new account (settings clarity phase 9, slice 9D;
docs/plans/2026-10-15-settings-clarity.md, "Signing in a new account").

Every login here is fake: a ``popen`` that records its call, a ``ps`` table
and a ``killpg`` that records signals, or one shell script under the temp
tree that only sleeps. No real CLI login runs and no real login folder is
looked at.
"""
from __future__ import annotations

import builtins
import json
import os
import stat
import subprocess
import threading
import time
from pathlib import Path
from types import MappingProxyType

import pytest

from khipu import discover, signin, usage

NOW = 1_800_000_000.0
LSTART = "Thu Oct 15 00:00:00 2026"
LOGIN_VARS = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "GROK_HOME")
ALL_PROVEN = MappingProxyType({cli: True for cli in signin.CLIS})


# ---- fakes -----------------------------------------------------------------------


class FakeProc:
    def __init__(self, pid: int, procs: "Procs", stdout=None):
        self.pid, self.procs, self.stdout = pid, procs, stdout

    def poll(self):
        return None if self.pid in self.procs.table else 0

    def wait(self, timeout=None):
        return 0


class Procs:
    """A process table for ``ps``, a ``killpg`` that ends the group, and a
    ``popen`` that adds a process. Pids are above macOS's highest pid, so a
    real ``os.getpgid`` on one finds nothing."""

    def __init__(self):
        self.table: dict[int, dict] = {}
        self.killed: list[tuple[int, int]] = []
        self.calls: list[tuple[list, dict]] = []
        self.next_pid = 1_000_001
        self.stdout = None
        self.delay = 0.0

    def ps(self, pid):
        info = self.table.get(pid)
        return dict(info) if info else None

    def killpg(self, pgid, sig):
        self.killed.append((pgid, sig))
        self.table.pop(pgid, None)

    def popen(self, argv, **kw):
        self.calls.append((list(argv), kw))
        if self.delay:
            time.sleep(self.delay)
        pid = self.next_pid
        self.next_pid += 1
        self.table[pid] = {"stat": "S", "lstart": LSTART, "command": " ".join(argv)}
        stdout, self.stdout = self.stdout, None
        return FakeProc(pid, self, stdout)

    def end(self, pid):
        self.table.pop(pid, None)

    def kw(self) -> dict:
        return {"popen": self.popen, "ps": self.ps, "killpg": self.killpg}


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A temp HOME and Khipu data folder, fake CLI paths, no login variable
    in Khipu's own environment."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    for name in LOGIN_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(usage, "_command", lambda harness: f"/fake/bin/{harness}")
    monkeypatch.setattr(signin, "KILL_GRACE_S", 0.2)
    procs = Procs()
    procs.home, procs.tmp = home, tmp_path
    return procs


def _tree(root: Path) -> dict[str, bytes]:
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = Path(dirpath) / name
            out[str(p.relative_to(root))] = p.read_bytes()
    return out


def _folder(name: str = "Claude client", cli: str = "claude") -> str:
    made = signin.make_folder(cli, name)
    assert made["ok"], made
    return made["folder"]


def _attempt_file() -> Path:
    return usage.cache_file().with_name(signin.ATTEMPT_NAME)


def _code_pipe(lines: list[bytes], *, close: bool = True):
    r, w = os.pipe()
    for line in lines:
        os.write(w, line)
    if close:
        os.close(w)
        w = None
    return os.fdopen(r, "rb", buffering=0), w


# ---- the folder slug ---------------------------------------------------------------


def test_slug_drops_one_leading_cli_word_and_collapses_punctuation():
    assert signin.slug("Claude client", "claude") == "client"
    assert signin.slug("claude code Work", "claude") == "work"
    assert signin.slug("Grok Build lab", "grok") == "lab"
    assert signin.slug("Codex  Work!! -- Stuff.", "codex") == "work-stuff"
    assert signin.slug("Claude Claude", "claude") == "claude"
    assert signin.slug("bakery", "claude") == "bakery"
    assert signin.slug("x" * 40, "claude") == "x" * 32
    assert signin.slug("a" * 31 + " b", "claude") == "a" * 31


@pytest.mark.parametrize("name", ["", "   ", "Claude", "default", "Claude default", "backup", "backups",
                                  "logs", "bak", "bak2", "old backup", "work-logs", "a\x07b", "tab\tname",
                                  "!!!"])
def test_slug_refuses_empty_default_skipped_and_control_names(name):
    assert signin.slug(name, "claude") is None


def test_slug_refuses_muse_and_unknown_clis():
    assert signin.slug("work", "muse") is None
    assert signin.slug("work", "other") is None


# ---- making the folder ---------------------------------------------------------------


def test_make_folder_is_an_exclusive_0700_mkdir(box, monkeypatch):
    monkeypatch.setattr(os, "makedirs", lambda *a, **k: pytest.fail("never makedirs"))
    out = signin.make_folder("claude", "Claude client")
    assert out == {"ok": True, "folder": "~/.claude-client", "reused": False}
    info = os.lstat(box.home / ".claude-client")
    assert stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700


@pytest.mark.parametrize("kind", ["file", "folder", "symlink", "dangling"])
def test_anything_at_the_path_is_taken_then_two_then_three(box, kind):
    target = box.home / ".codex-work"
    if kind == "file":
        target.write_text("x")
    elif kind == "folder":
        target.mkdir()
    elif kind == "symlink":
        (box.tmp / "elsewhere").mkdir()
        target.symlink_to(box.tmp / "elsewhere")
    else:
        target.symlink_to(box.tmp / "missing")
    assert signin.make_folder("codex", "work")["folder"] == "~/.codex-work-2"
    (box.home / ".codex-other").write_text("x")
    (box.home / ".codex-other-2").mkdir()
    assert signin.make_folder("codex", "other")["folder"] == "~/.codex-other-3"


def test_a_folder_khipu_made_that_never_signed_in_is_reused(box):
    first = signin.make_folder("claude", "Claude client")
    again = signin.make_folder("claude", "client")
    assert again == {"ok": True, "folder": first["folder"], "reused": True}
    assert not (box.home / ".claude-client-2").exists()


def test_make_folder_refuses_muse_and_bad_names(box):
    assert signin.make_folder("muse", "work") == {"ok": False, "error": signin.E_REFUSED}
    assert signin.make_folder("claude", "default") == {"ok": False, "error": signin.E_SLUG}
    assert list(box.home.iterdir()) == []


# ---- only a folder Khipu made ------------------------------------------------------------


def test_khipu_made_accepts_its_own_folder(box):
    folder = _folder()
    assert signin.khipu_made("claude", folder) == (box.home / ".claude-client", None)


def test_khipu_made_and_start_refuse_everything_else(box, monkeypatch):
    mine = _folder()
    (box.home / ".claude-hand").mkdir()
    # A Khipu folder moved away and a symlink of its name put in its place.
    moved = _folder("swap")
    os.rename(box.home / ".claude-swap", box.tmp / "away")
    (box.home / ".claude-swap").symlink_to(box.tmp / "away")
    # A folder whose inode changed since Khipu made it.
    renewed = _folder("renew")
    os.rmdir(box.home / ".claude-renew")
    (box.tmp / "spacer").mkdir()
    os.mkdir(box.home / ".claude-renew", 0o700)
    refused = ["~/..", "~/.claude-client/../.claude-client", "~/sub/.claude-client", "~/.claude-client/",
               "~/.claude-cli\x00ent", "~/.claude-client\n", str(box.home / ".claude-client"), "~/.claude-hand",
               moved, renewed, "~/.claude", "", None]
    monkeypatch.setattr(signin, "PROVEN", ALL_PROVEN)
    for folder in refused:
        assert signin.khipu_made("claude", folder) == (None, signin.E_REFUSED), folder
        assert signin.start("claude", folder, **box.kw())["error"] == signin.E_REFUSED, folder
    assert signin.khipu_made("codex", mine) == (None, signin.E_REFUSED)
    assert signin.start("codex", mine, **box.kw())["error"] == signin.E_REFUSED
    assert signin.khipu_made("muse", mine) == (None, signin.E_REFUSED)
    assert signin.start("muse", mine, **box.kw())["error"] == signin.E_REFUSED
    assert box.calls == []


# ---- the guardrail -------------------------------------------------------------------------


def test_only_claude_is_proven():
    assert dict(signin.PROVEN) == {"claude": True, "codex": False, "grok": False, "muse": False}
    offers = signin.offers()
    assert [cli for cli, o in offers.items() if o["offered"]] == ["claude"]
    assert offers["codex"]["waits_for"] == "codex_keychain"


@pytest.mark.parametrize("cli", ["codex", "grok", "muse"])
def test_start_refuses_unproven_clis(box, cli):
    folder = None if cli == "muse" else _folder("work", cli)
    out = signin.start(cli, folder, **box.kw())
    assert out == {"ok": False, "error": signin.E_GUARDRAIL, "waits_for": signin.GUARDRAIL[cli]}
    assert box.calls == []


@pytest.mark.parametrize("cli,var", [("claude", "CLAUDE_CONFIG_DIR"), ("codex", "CODEX_HOME"), ("grok", "GROK_HOME")])
def test_only_that_folders_variable_is_set(box, monkeypatch, cli, var):
    monkeypatch.setattr(signin, "PROVEN", ALL_PROVEN)
    for name in LOGIN_VARS:
        monkeypatch.setenv(name, str(box.tmp / "inherited"))
    folder = _folder("work", cli)
    if cli == "grok":
        box.stdout, _ = _code_pipe([b"Go to https://accounts.x.ai/device and enter ABCD-1234\n"])
    out = signin.start(cli, folder, drain=lambda s: s.close(), **box.kw())
    assert out["ok"], out
    argv, kw = box.calls[0]
    assert argv == [f"/fake/bin/{cli}", *signin.ARGV[cli]]
    assert kw["env"][var] == str(box.home / f".{cli}-work")
    assert [n for n in LOGIN_VARS if n in kw["env"]] == [var]
    assert kw["stdin"] is subprocess.DEVNULL and kw["start_new_session"] is True


def test_muse_starts_with_no_folder_and_no_login_variable(box, monkeypatch):
    monkeypatch.setattr(signin, "PROVEN", ALL_PROVEN)
    box.stdout, _ = _code_pipe([b"Open https://www.meta.ai/device code QRST-55AA\n"])
    out = signin.start("muse", None, drain=lambda s: s.close(), **box.kw())
    assert out["ok"] and out["code"] == "QRST-55AA" and out["attempt"]["folder"] is None
    argv, kw = box.calls[0]
    assert argv == ["/fake/bin/muse", "login"]
    assert not [n for n in LOGIN_VARS if n in kw["env"]]


# ---- nothing is saved unless it signed in ------------------------------------------------------


def _snapshot(box) -> tuple:
    return _tree(box.tmp / "data"), _tree(box.home), usage.configured_accounts()


def test_failure_saves_nothing(box):
    folder = _folder()
    before = _snapshot(box)
    out = signin.start("claude", folder, **box.kw())
    box.end(1_000_001)  # the login exited without signing in
    res = signin.status(out["attempt"]["id"], ps=box.ps, check=lambda cli, path: signin.NOT_SIGNED_IN)
    assert res["state"] == signin.NOT_SIGNED_IN and res["ok"] is False
    assert _snapshot(box) == before
    assert not usage.cache_file().exists() and not _attempt_file().exists()


def test_timeout_saves_nothing_and_stops_the_login(box):
    folder = _folder()
    before = _snapshot(box)
    out = signin.start("claude", folder, now=NOW, **box.kw())
    late = NOW + signin.wait_s() + 1
    res = signin.status(out["attempt"]["id"], now=late, ps=box.ps, killpg=box.killpg,
                        check=lambda *a: pytest.fail("no check after a timeout"))
    assert res["state"] == signin.TIMED_OUT and res["error"] == usage.E_SIGN_IN_TIMEOUT
    assert [pg for pg, _ in box.killed] == [1_000_001]
    assert _snapshot(box) == before
    assert not usage.cache_file().exists() and not _attempt_file().exists()


def test_cancel_saves_nothing_and_stops_the_login(box):
    folder = _folder()
    before = _snapshot(box)
    out = signin.start("claude", folder, **box.kw())
    res = signin.cancel(out["attempt"]["id"], ps=box.ps, killpg=box.killpg)
    assert res["state"] == signin.CANCELLED and res["stopped"] == "killed"
    assert box.killed[0][0] == 1_000_001
    assert _snapshot(box) == before
    assert not usage.cache_file().exists() and not _attempt_file().exists()


def test_status_waits_while_the_login_runs(box):
    out = signin.start("claude", _folder(), **box.kw())
    res = signin.status(out["attempt"]["id"], ps=box.ps, check=lambda *a: pytest.fail("not yet"))
    assert res["state"] == signin.WAITING and _attempt_file().exists()


# ---- the code line ------------------------------------------------------------------------------


def test_code_reader_keeps_only_the_link_and_code(box):
    stream, _ = _code_pipe([b"Starting Grok Build...\n", b"\x1b[1mnoise\x1b[0m\n",
                            b"Z" * (signin.LINE_MAX + 50) + b" https://accounts.x.ai/x ABCD-0000\n",
                            b"Open https://accounts.x.ai/activate?u=1 and enter WXYZ-7788 to continue\r\n",
                            b"later https://accounts.x.ai/other EEEE-1111\n"])
    found = signin._read_code(stream, "grok", time.monotonic() + 5)
    assert found == ("https://accounts.x.ai/activate?u=1", "WXYZ-7788")


def test_an_over_long_line_split_across_reads_is_skipped(box):
    r, w = os.pipe()
    stream = os.fdopen(r, "rb", buffering=0)

    def feed():
        os.write(w, b"Q" * (signin.LINE_MAX + 10))
        time.sleep(0.1)
        os.write(w, b" https://accounts.x.ai/x ABCD-0000\n")
        os.write(w, b"https://accounts.x.ai/ok GOOD-0001\n")
        os.close(w)

    threading.Thread(target=feed).start()
    assert signin._read_code(stream, "grok", time.monotonic() + 5) == ("https://accounts.x.ai/ok", "GOOD-0001")


@pytest.mark.parametrize("line", [b"Open https://evil.example/activate and enter WXYZ-7788\n",
                                  b"Open https://accounts.x.ai.evil.example/a and enter WXYZ-7788\n",
                                  b"Open https://accounts.x.ai@evil.example/a and enter WXYZ-7788\n",
                                  b"Open http://accounts.x.ai/a and enter WXYZ-7788\n",
                                  b"Open https://accounts.x.ai/a and enter wxyz-7788\n"])
def test_a_link_off_the_host_list_or_a_wrong_code_is_refused(box, line):
    stream, _ = _code_pipe([line])
    assert signin._read_code(stream, "grok", time.monotonic() + 5) is None


def test_the_total_cap_stops_reading(box, monkeypatch):
    monkeypatch.setattr(signin, "READ_MAX", 2048)
    stream, w = _code_pipe([b"noise line\n" * 300, b"https://accounts.x.ai/a ABCD-1234\n"], close=False)
    t0 = time.monotonic()
    assert signin._read_code(stream, "grok", time.monotonic() + 10) is None
    assert time.monotonic() - t0 < 2
    os.close(w)


def test_no_code_in_time_stops_the_login_through_kill_if_ours(box, monkeypatch):
    monkeypatch.setattr(signin, "PROVEN", ALL_PROVEN)
    monkeypatch.setattr(signin, "CODE_WAIT_S", 0.3)
    seen = []
    real = signin.kill_if_ours
    monkeypatch.setattr(signin, "kill_if_ours", lambda rec, **kw: seen.append(rec["pid"]) or real(rec, **kw))
    box.stdout, w = _code_pipe([b"Starting...\n"], close=False)
    out = signin.start("grok", _folder("lab", "grok"), **box.kw())
    os.close(w)
    assert out == {"ok": False, "error": signin.E_NO_CODE, "cli_name": "Grok Build"}
    assert seen == [1_000_001] and box.killed[0][0] == 1_000_001
    assert not _attempt_file().exists()


def test_the_code_reaches_only_the_reply(box, monkeypatch, capsys, caplog):
    monkeypatch.setattr(signin, "PROVEN", ALL_PROVEN)
    caplog.set_level("DEBUG")
    box.stdout, _ = _code_pipe([b"noise\n", b"Visit https://accounts.x.ai/device and enter K7Q2-ZZ91\n"])
    out = signin.start("grok", _folder("lab", "grok"), drain=lambda s: s.close(), **box.kw())
    assert out["code"] == "K7Q2-ZZ91" and out["link"] == "https://accounts.x.ai/device"
    res = signin.status(out["attempt"]["id"], ps=box.ps)
    assert "K7Q2-ZZ91" not in json.dumps(res)
    for blob in _tree(box.tmp).values():
        assert b"K7Q2-ZZ91" not in blob
    captured = capsys.readouterr()
    assert "K7Q2-ZZ91" not in captured.out + captured.err + caplog.text


# ---- one sign-in at a time ------------------------------------------------------------------------


def test_two_starts_at_once_leave_exactly_one_owner(box):
    folder = _folder()
    box.delay = 0.3
    results = []
    threads = [threading.Thread(target=lambda: results.append(signin.start("claude", folder, **box.kw())))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(bool(r["ok"]) for r in results) == [False, True]
    assert [r["error"] for r in results if not r["ok"]] == [signin.E_BUSY]
    assert len(box.calls) == 1


def test_a_start_while_one_runs_is_busy_and_again_ends_it_first(box):
    folder = _folder()
    first = signin.start("claude", folder, **box.kw())
    busy = signin.start("claude", folder, **box.kw())
    assert busy["error"] == signin.E_BUSY and busy["attempt"]["id"] == first["attempt"]["id"]
    assert box.killed == []
    again = signin.start("claude", folder, again=True, **box.kw())
    assert again["ok"] and again["attempt"]["id"] != first["attempt"]["id"]
    assert box.killed[0][0] == 1_000_001
    assert json.loads(_attempt_file().read_text())["pid"] == 1_000_002


# ---- stale records and crashes ------------------------------------------------------------------------


@pytest.mark.parametrize("change", [{"lstart": "Fri Oct 16 09:00:00 2026"},
                                    {"command": "/usr/bin/python3 something else"},
                                    {"command": "/fake/bin/claude auth status --json"}])
def test_a_stale_record_kills_nothing_and_is_reaped(box, change):
    out = signin.start("claude", _folder(), **box.kw())
    box.table[1_000_001].update(change)
    rec = json.loads(_attempt_file().read_text())
    assert signin.kill_if_ours(rec, ps=box.ps, killpg=box.killpg) == "stale"
    assert signin.reap(ps=box.ps, killpg=box.killpg) == {"ok": True, "reaped": "stale"}
    assert box.killed == [] and not _attempt_file().exists()
    assert signin.status(out["attempt"]["id"], ps=box.ps)["error"] == signin.E_NO_ATTEMPT


def test_a_record_with_no_start_time_never_kills():
    rec = {"pid": 1_000_001, "pgid": 1_000_001, "cli": "claude", "tail": list(signin.ARGV["claude"]),
           "lstart": None, "command": None}
    killed = []
    ps = lambda pid: {"stat": "S", "lstart": LSTART, "command": "/x auth login --claudeai"}  # noqa: E731
    assert signin.kill_if_ours(rec, ps=ps, killpg=lambda *a: killed.append(a)) == "stale"
    assert killed == []


@pytest.mark.parametrize("matching", [True, False])
def test_after_a_crash_the_next_start_reaps_only_a_matching_process(box, matching):
    folder = _folder()
    signin.start("claude", folder, now=NOW, **box.kw())  # the app then crashed, the record stayed
    if not matching:
        box.table[1_000_001]["lstart"] = "Sat Oct 17 01:02:03 2026"
    out = signin.start("claude", folder, now=NOW + signin.wait_s() + 60, **box.kw())
    assert out["ok"]
    assert [pg for pg, _ in box.killed] == ([1_000_001] if matching else [])


def test_start_reaps_a_leftover_whose_process_ended(box):
    folder = _folder()
    signin.start("claude", folder, **box.kw())
    box.end(1_000_001)
    assert signin.start("claude", folder, **box.kw())["ok"]
    assert box.killed == []


def test_start_never_keeps_a_login_it_cannot_prove_is_its_own(box):
    box.ps = lambda pid: None
    out = signin.start("claude", _folder(), popen=box.popen, ps=box.ps, killpg=box.killpg)
    assert out == {"ok": False, "error": signin.E_START}
    assert box.killed and box.killed[0][0] == 1_000_001 and not _attempt_file().exists()


# ---- cancel --------------------------------------------------------------------------------------------


def test_cancel_stops_only_its_own_group(box):
    out = signin.start("claude", _folder(), **box.kw())
    assert signin.cancel("someone-else", ps=box.ps, killpg=box.killpg) == {"ok": False, "error": signin.E_NO_ATTEMPT}
    assert box.killed == [] and _attempt_file().exists()
    assert signin.cancel(out["attempt"]["id"], ps=box.ps, killpg=box.killpg)["ok"]


def test_cancel_removes_the_empty_folder_khipu_made(box):
    out = signin.start("claude", _folder(), **box.kw())
    res = signin.cancel(out["attempt"]["id"], remove_folder=True, ps=box.ps, killpg=box.killpg)
    assert res["folder_removed"] is True and not (box.home / ".claude-client").exists()
    assert signin._folders() == []


def test_cancel_keeps_a_folder_that_holds_anything_and_never_lists_it(box, monkeypatch):
    out = signin.start("claude", _folder(), **box.kw())
    inside = box.home / ".claude-client"
    (inside / "settings.json").write_text("{}")
    real_listdir, real_scandir, real_open = os.listdir, os.scandir, builtins.open

    def guard(path):
        p = os.fspath(path) if not isinstance(path, int) else ""
        if p and os.path.abspath(p).startswith(str(inside)):
            raise AssertionError(f"looked inside the login folder: {p}")

    monkeypatch.setattr(os, "listdir", lambda p=".": guard(p) or real_listdir(p))
    monkeypatch.setattr(os, "scandir", lambda p=".": guard(p) or real_scandir(p))
    monkeypatch.setattr(builtins, "open", lambda f, *a, **k: guard(f) or real_open(f, *a, **k))
    res = signin.cancel(out["attempt"]["id"], remove_folder=True, ps=box.ps, killpg=box.killpg)
    monkeypatch.undo()
    assert res["ok"] and res["folder_removed"] is False
    assert (inside / "settings.json").read_text() == "{}"


# ---- after it ends ----------------------------------------------------------------------------------------


def test_a_folder_that_signed_in_is_never_removed_or_reused(box):
    folder = _folder()
    out = signin.start("claude", folder, **box.kw())
    box.end(1_000_001)
    seen = []
    res = signin.status(out["attempt"]["id"], ps=box.ps,
                        check=lambda cli, path: seen.append((cli, path)) or signin.SIGNED_IN)
    assert res["ok"] and res["state"] == signin.SIGNED_IN
    assert seen == [("claude", box.home / ".claude-client")]
    assert signin.remove_unused_folder("claude", folder) is False and (box.home / ".claude-client").is_dir()
    assert signin.make_folder("claude", "client")["folder"] == "~/.claude-client-2"
    assert signin.start("claude", folder, **box.kw())["error"] == signin.E_REFUSED


# ---- a real process -----------------------------------------------------------------------------------------


def _listing() -> list[str]:
    res = subprocess.run(["/bin/ps", "-axww", "-o", "stat=,command="], capture_output=True, text=True, timeout=10)
    return [line.strip() for line in res.stdout.splitlines() if not line.strip().startswith("Z")]


def test_a_real_fake_login_is_waited_for_and_cancel_leaves_nothing_running(box, monkeypatch):
    script = box.tmp / "bin" / "claude"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\nwhile :; do sleep 1; done\n")
    script.chmod(0o755)
    monkeypatch.setattr(usage, "_command", lambda harness: str(script))
    out = signin.start("claude", _folder())
    try:
        assert out["ok"], out
        assert any(str(script) in line for line in _listing())
        assert signin.status(out["attempt"]["id"])["state"] == signin.WAITING
        assert signin.running()
        res = signin.cancel(out["attempt"]["id"])
        assert res["stopped"] == "killed"
        end = time.monotonic() + 3
        while time.monotonic() < end and any(str(script) in line for line in _listing()):
            time.sleep(0.1)
        assert not any(str(script) in line for line in _listing())
        assert not signin.running()
    finally:
        rec = json.loads(_attempt_file().read_text()) if _attempt_file().exists() else None
        if rec:
            signin.kill_if_ours(rec)


# ---- discovery, the CLI, the wait -----------------------------------------------------------------------------


def test_discovery_says_looking_while_a_sign_in_runs(box, monkeypatch):
    monkeypatch.setattr(signin, "proc_info", box.ps)
    signin.start("claude", _folder(), **box.kw())
    out = discover.discover()
    assert out["looking"] is True
    assert discover.discover(check="~/.claude-client")["check"] == {"ok": False, "error": "sign_in_running"}
    box.end(1_000_001)
    assert discover.discover()["looking"] is False


def test_the_cli_flags(box, capsys):
    from khipu.cli import main

    assert main(["usage", "--sign-in-offers"]) == 0
    assert json.loads(capsys.readouterr().out)["claude"]["offered"] is True
    assert main(["usage", "--sign-in-folder", "--harness=claude", "--name=Claude client"]) == 0
    assert json.loads(capsys.readouterr().out)["folder"] == "~/.claude-client"
    assert main(["usage", "--sign-in-folder", "--harness=claude", "--name=default"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == signin.E_SLUG
    assert main(["usage", "--sign-in-status=nope"]) == 2
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": signin.E_NO_ATTEMPT}
    assert main(["usage", "--sign-in-cancel=nope"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == signin.E_NO_ATTEMPT
    assert main(["usage", "--sign-in-new", "--harness=codex", "--folder=~/.codex-x"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == signin.E_GUARDRAIL
    assert main(["usage", "--sign-in-reap"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "reaped": None}


def test_wait_s_is_shortened_only_under_a_test_home(box, monkeypatch):
    monkeypatch.setenv("KHIPU_SIGN_IN_WAIT_S", "5")
    monkeypatch.delenv("KHIPU_TEST", raising=False)
    assert signin.wait_s() == signin.WAIT_S
    monkeypatch.setenv("KHIPU_TEST", "1")
    assert signin.wait_s() == 5.0
    monkeypatch.delenv("KHIPU_DATA_DIR")
    assert signin.wait_s() == signin.WAIT_S
