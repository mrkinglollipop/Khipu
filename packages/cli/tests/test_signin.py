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
import signal
import stat
import subprocess
import threading
import time
from pathlib import Path
from types import MappingProxyType

import pytest

from khipu import discover, signin, usage

NOW = 1_800_000_000.0
REAL_GETPGID, REAL_MEMBERS = signin.__dict__.get("_getpgid"), signin.__dict__.get("group_members")
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
        for pid, info in list(self.table.items()):
            if info.get("pgid", pid) == pgid and sig not in info.get("ignore", ()):
                self.table.pop(pid)

    def getpgid(self, pid):
        if pid not in self.table:
            raise ProcessLookupError(pid)
        return self.table[pid].get("pgid", pid)

    def members(self, pgid):
        return [pid for pid, info in self.table.items()
                if info.get("pgid", pid) == pgid and not info["stat"].startswith("Z")]

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
    monkeypatch.setattr(signin, "_getpgid", procs.getpgid, raising=False)
    monkeypatch.setattr(signin, "group_members", procs.members, raising=False)
    # The app that ran ``start``: the parent of the process running it.
    procs.table[os.getppid()] = {"stat": "S", "lstart": LSTART, "command": "/fake/Khipu.app/Contents/MacOS/khipu"}
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


def test_a_taken_long_slug_keeps_its_suffix_within_32_characters(box):
    long = "x" * 32
    (box.home / f".claude-{long}").mkdir()
    for n in range(2, 12):
        out = signin.make_folder("claude", long)
        assert out["ok"], out
        folder = out["folder"]
        slug_part = folder[len("~/.claude-"):]
        assert len(slug_part) <= signin.SLUG_MAX and slug_part.endswith(f"-{n}"), folder
        assert signin.khipu_made("claude", folder)[0] is not None
        signin._mark_signed_in("claude", box.home / folder[2:])  # so the next call makes a new one


def test_khipu_made_refuses_a_slug_over_32_characters(box, monkeypatch):
    name = ".claude-" + "y" * 33
    (box.home / name).mkdir()
    info = os.lstat(box.home / name)
    signin._save_folders([{"cli": "claude", "slug": "y" * 33, "name": name,
                           "identity": signin._ident(info), "signed_in": False}])
    assert signin.khipu_made("claude", "~/" + name) == (None, signin.E_REFUSED)


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
    assert res["state"] == signin.NOT_SIGNED_IN and res["ok"] is True
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


@pytest.mark.parametrize("missing", [None, {}, {"stat": "S", "lstart": LSTART}])
def test_status_reobserves_one_failed_process_observation_without_ending_login(box, missing):
    out = signin.start("claude", _folder(), **box.kw())
    before = _attempt_file().read_bytes()
    observed, checked = [], []

    def ps(pid):
        observed.append(pid)
        return missing if len(observed) == 1 else box.ps(pid)

    res = signin.status(out["attempt"]["id"], ps=ps, killpg=box.killpg,
                        check=lambda *a: checked.append(a) or signin.NOT_SIGNED_IN)
    assert res["state"] == signin.WAITING and res["ok"] is True
    assert observed == [1_000_001, 1_000_001]
    assert checked == [] and box.killed == []
    assert _attempt_file().read_bytes() == before
    assert (box.home / ".claude-client").is_dir()


@pytest.mark.parametrize("change", [{"stat": "Z"},
                                    {"stat": "Z", "lstart": None, "command": None},
                                    {"lstart": "Fri Oct 16 09:00:00 2026"},
                                    {"lstart": "Fri Oct 16 09:00:00 2026", "command": None},
                                    {"command": "/usr/bin/python3 something else"},
                                    {"command": "/fake/bin/claude auth status --json"}])
def test_status_never_retries_an_observed_zombie_or_reused_pid(box, monkeypatch, change):
    out = signin.start("claude", _folder(), **box.kw())
    box.table[1_000_001].update(change)
    checked = []
    monkeypatch.setattr(time, "sleep", lambda *a: pytest.fail("observed death or reuse is conclusive"))
    res = signin.status(out["attempt"]["id"], ps=box.ps, killpg=box.killpg,
                        check=lambda *a: checked.append(a) or signin.NOT_SIGNED_IN)
    assert res["state"] == signin.NOT_SIGNED_IN
    assert len(checked) == 1 and box.killed == []
    assert not _attempt_file().exists()


@pytest.mark.parametrize("change", [{"stat": "Z"}, {"lstart": "Fri Oct 16 09:00:00 2026"}])
def test_status_rejects_death_or_pid_reuse_after_a_missing_observation(box, monkeypatch, change):
    out = signin.start("claude", _folder(), **box.kw())
    box.table[1_000_001].update(change)
    observed, checked, slept = [], [], []
    monkeypatch.setattr(time, "sleep", lambda delay: slept.append(delay))

    def ps(pid):
        observed.append(pid)
        return None if len(observed) == 1 else box.ps(pid)

    res = signin.status(out["attempt"]["id"], ps=ps, killpg=box.killpg,
                        check=lambda *a: checked.append(a) or signin.NOT_SIGNED_IN)
    assert res["state"] == signin.NOT_SIGNED_IN
    assert observed == [1_000_001] * 3 and len(slept) == 1
    assert len(checked) == 1 and box.killed == []
    assert not _attempt_file().exists()


def test_status_bounds_persistent_missing_observations_and_finishes_a_dead_login(box, monkeypatch):
    out = signin.start("claude", _folder(), **box.kw())
    box.end(1_000_001)
    clock, observed, checked = [0.0], [], []
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))

    def ps(pid):
        observed.append(pid)
        return box.ps(pid)

    res = signin.status(out["attempt"]["id"], ps=ps, killpg=box.killpg,
                        check=lambda *a: checked.append(a) or signin.NOT_SIGNED_IN)
    assert res["state"] == signin.NOT_SIGNED_IN
    assert 0 < clock[0] <= 0.5 and 2 < len(observed) <= 8
    assert len(checked) == 1 and box.killed == []
    assert not _attempt_file().exists()


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


def test_no_code_in_time_stops_the_login_through_the_verified_stop(box, monkeypatch):
    monkeypatch.setattr(signin, "PROVEN", ALL_PROVEN)
    monkeypatch.setattr(signin, "CODE_WAIT_S", 0.3)
    seen = []
    real = signin.stop_group
    monkeypatch.setattr(signin, "stop_group", lambda rec, **kw: seen.append(rec["pid"]) or real(rec, **kw))
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


def test_a_folder_swapped_for_a_symlink_before_the_launch_is_refused(box, monkeypatch, tmp_path):
    folder = _folder()
    outside = tmp_path / "outside"
    outside.mkdir()
    real_env = usage.probe_environment

    def swap(*a, **k):
        # Between the check and the spawn: the folder moves away and a link
        # to somewhere outside Khipu's folder takes its name.
        os.rename(box.home / ".claude-client", box.home / ".claude-moved")
        os.symlink(outside, box.home / ".claude-client")
        return real_env(*a, **k)

    monkeypatch.setattr(usage, "probe_environment", swap)
    out = signin.start("claude", folder, **box.kw())
    assert out == {"ok": False, "error": signin.E_REFUSED}
    assert box.calls == [] and not _attempt_file().exists() and list(outside.iterdir()) == []


def test_a_folder_swapped_for_another_real_folder_before_the_launch_is_refused(box, monkeypatch):
    folder = _folder()
    real_env = usage.probe_environment

    def swap(*a, **k):
        os.rename(box.home / ".claude-client", box.home / ".claude-moved")
        os.mkdir(box.home / ".claude-client", 0o700)
        return real_env(*a, **k)

    monkeypatch.setattr(usage, "probe_environment", swap)
    assert signin.start("claude", folder, **box.kw()) == {"ok": False, "error": signin.E_REFUSED}
    assert box.calls == []


def test_no_signal_once_the_number_stops_being_the_login(box):
    signin.start("claude", _folder(), **box.kw())
    rec = json.loads(_attempt_file().read_text())
    leader = box.table[1_000_001]
    leader["ignore"] = {signal.SIGTERM}
    real_killpg = box.killpg

    def killpg(pgid, sig):
        real_killpg(pgid, sig)
        # During the grace wait the number comes to name another process.
        leader.update({"lstart": "Sat Oct 17 01:02:03 2026", "command": "/usr/bin/other"})

    assert signin.kill_if_ours(rec, ps=box.ps, killpg=killpg) == "unverified"
    assert box.killed == [(1_000_001, signal.SIGTERM)]


def test_a_failed_group_check_sends_nothing_and_says_so(box, monkeypatch):
    signin.start("claude", _folder(), **box.kw())
    rec = json.loads(_attempt_file().read_text())

    def denied(pid):
        raise PermissionError(pid)

    monkeypatch.setattr(signin, "_getpgid", denied)
    assert signin.kill_if_ours(rec, ps=box.ps, killpg=box.killpg) == "unverified"
    assert box.killed == []
    res = signin.reap(ps=box.ps, killpg=box.killpg)
    assert res["reaped"] == "unverified" and box.killed == []


def test_a_child_that_ignores_sigterm_is_killed_with_the_group(box):
    out = signin.start("claude", _folder(), **box.kw())
    box.table[1_000_050] = {"stat": "S", "lstart": LSTART, "command": "/fake/bin/node helper",
                            "pgid": 1_000_001, "ignore": {signal.SIGTERM}}
    res = signin.cancel(out["attempt"]["id"], ps=box.ps, killpg=box.killpg)
    assert res["stopped"] == "killed" and "left" not in res
    assert box.killed == [(1_000_001, signal.SIGTERM), (1_000_001, signal.SIGKILL)]
    assert box.members(1_000_001) == []


def test_a_group_that_will_not_end_is_reported_not_called_killed(box):
    out = signin.start("claude", _folder(), **box.kw())
    box.table[1_000_050] = {"stat": "S", "lstart": LSTART, "command": "/fake/bin/node helper",
                            "pgid": 1_000_001, "ignore": {signal.SIGTERM, signal.SIGKILL}}
    res = signin.cancel(out["attempt"]["id"], ps=box.ps, killpg=box.killpg)
    assert res["stopped"] == "left" and res["left"] == [1_000_050]


def test_a_leftover_group_whose_leader_already_ended_is_reported_and_not_signalled(box):
    signin.start("claude", _folder(), **box.kw())
    box.table[1_000_050] = {"stat": "S", "lstart": LSTART, "command": "/fake/bin/node helper", "pgid": 1_000_001}
    box.end(1_000_001)
    assert signin.reap(ps=box.ps, killpg=box.killpg) == {"ok": True, "reaped": "left", "left": [1_000_050]}
    assert box.killed == [] and _attempt_file().exists()


@pytest.mark.parametrize("matching", [True, False])
def test_after_an_app_crash_a_start_reaps_before_the_deadline(box, matching):
    folder = _folder()
    first = signin.start("claude", folder, now=NOW, **box.kw())
    box.end(os.getppid())  # the app that owned the attempt is gone
    if not matching:
        box.table[1_000_001]["lstart"] = "Sat Oct 17 01:02:03 2026"
    out = signin.start("claude", folder, now=NOW + 60, **box.kw())
    assert out["ok"], out
    assert out["attempt"]["id"] != first["attempt"]["id"]
    assert [pg for pg, _ in box.killed] == ([1_000_001] if matching else [])
    assert json.loads(_attempt_file().read_text())["pid"] == 1_000_002


def test_a_start_while_the_owning_app_lives_is_still_busy(box):
    folder = _folder()
    signin.start("claude", folder, now=NOW, **box.kw())
    assert signin.start("claude", folder, now=NOW + 60, **box.kw())["error"] == signin.E_BUSY
    assert box.killed == []


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


def test_a_sign_in_saves_no_account_and_the_folder_is_tracked_only_with_a_version(box, khipu_app):
    from khipu import accounts

    read = accounts.status()["version"]
    stored = usage.configured_accounts()
    folder = _folder()
    out = signin.start("claude", folder, **box.kw())
    box.end(1_000_001)
    res = signin.status(out["attempt"]["id"], ps=box.ps, check=lambda cli, path: signin.SIGNED_IN)
    assert res["state"] == signin.SIGNED_IN
    # Signing in writes no config: the account list and its version are as read.
    assert usage.configured_accounts() == stored and accounts.status()["version"] == read
    entry = [{"harness": "claude", "home": folder, "label": "Claude client"}]
    for expect, code in ((None, "version_needed"), ("stale", "version_moved")):
        with pytest.raises(usage.AccountsError) as err:
            accounts.track(entry, expect=expect, read=False)
        assert err.value.code == code
    saved = accounts.track(entry, expect=read, read=False)
    assert saved["accounts"] == [{"key": f"claude|{box.home / '.claude-client'}", "label": "Claude client"}]
    assert saved["version"] != read


# ---- a real process -----------------------------------------------------------------------------------------


def _listing() -> list[str]:
    res = subprocess.run(["/bin/ps", "-axww", "-o", "stat=,command="], capture_output=True, text=True, timeout=10)
    return [line.strip() for line in res.stdout.splitlines() if not line.strip().startswith("Z")]


def test_a_real_fake_login_is_waited_for_and_cancel_leaves_nothing_running(box, monkeypatch):
    monkeypatch.setattr(signin, "_getpgid", REAL_GETPGID)
    monkeypatch.setattr(signin, "group_members", REAL_MEMBERS)
    script = box.tmp / "bin" / "claude"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\nwhile :; do sleep 1; done\n")
    script.chmod(0o755)
    monkeypatch.setattr(usage, "_command", lambda harness: str(script))
    out = signin.start("claude", _folder())
    try:
        assert out["ok"], out
        end = time.monotonic() + 3
        listed = any(str(script) in line for line in _listing())
        while not listed and time.monotonic() < end:
            time.sleep(0.05)
            listed = any(str(script) in line for line in _listing())
        assert listed
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


def test_a_real_child_that_ignores_sigterm_is_gone_after_cancel(box, monkeypatch):
    monkeypatch.setattr(signin, "_getpgid", REAL_GETPGID)
    monkeypatch.setattr(signin, "group_members", REAL_MEMBERS)
    script = box.tmp / "bin" / "claude"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n(trap '' TERM; while :; do sleep 1; done) &\nwhile :; do sleep 1; done\n")
    script.chmod(0o755)
    monkeypatch.setattr(usage, "_command", lambda harness: str(script))
    out = signin.start("claude", _folder(), ps=signin.proc_info)
    try:
        assert out["ok"], out
        pgid = json.loads(_attempt_file().read_text())["pgid"]
        end = time.monotonic() + 3
        while time.monotonic() < end and len(signin.group_members(pgid) or []) < 2:
            time.sleep(0.05)
        assert len(signin.group_members(pgid)) >= 2
        res = signin.cancel(out["attempt"]["id"])
        assert res["stopped"] == "killed", res
        assert signin.group_members(pgid) == []
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
    # Cancel with no attempt (it never started): only the empty folder's removal runs.
    assert main(["usage", "--sign-in-cancel", "--remove-folder", "--harness=claude",
                 "--folder=~/.claude-client"]) == 0
    assert json.loads(capsys.readouterr().out)["folder_removed"] is True
    assert not (box.home / ".claude-client").exists()
    assert main(["usage", "--sign-in-cancel"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == signin.E_NO_ATTEMPT


def test_wait_s_is_shortened_only_under_a_test_home(box, monkeypatch):
    monkeypatch.setenv("KHIPU_SIGN_IN_WAIT_S", "5")
    monkeypatch.delenv("KHIPU_TEST", raising=False)
    assert signin.wait_s() == signin.WAIT_S
    monkeypatch.setenv("KHIPU_TEST", "1")
    assert signin.wait_s() == 5.0
    monkeypatch.delenv("KHIPU_DATA_DIR")
    assert signin.wait_s() == signin.WAIT_S


def test_status_end_states_are_answers_not_refusals(box):
    """The app turns any ``ok: false`` into a refusal and keeps waiting, so a
    finished attempt answers ``ok: true`` with its state; ``ok: false`` is
    only for a real refusal such as ``no_attempt``."""
    out = signin.start("claude", _folder(), now=NOW, **box.kw())
    res = signin.status(out["attempt"]["id"], now=NOW + signin.wait_s() + 1, ps=box.ps, killpg=box.killpg)
    assert res["ok"] is True and res["state"] == signin.TIMED_OUT and res["error"] == usage.E_SIGN_IN_TIMEOUT
    for state in (signin.NOT_SIGNED_IN, signin.COULDNT_CHECK):
        out = signin.start("claude", _folder(), **box.kw())
        box.end(box.next_pid - 1)  # the login exited
        res = signin.status(out["attempt"]["id"], ps=box.ps, check=lambda *a, s=state: s)
        assert res["ok"] is True and res["state"] == state, res
    assert signin.status("0123456789abcdef") == {"ok": False, "error": signin.E_NO_ATTEMPT}


def test_cancel_after_a_failed_sign_in_removes_the_empty_folder(box):
    """Section 6, After it ends: the empty folder Khipu made is removed when the
    owner chooses Cancel, though status already ended the attempt. The app
    passes the folder Khipu returned at creation."""
    folder = _folder()
    out = signin.start("claude", folder, **box.kw())
    box.end(1_000_001)
    res = signin.status(out["attempt"]["id"], ps=box.ps, check=lambda cli, path: signin.NOT_SIGNED_IN)
    assert res["state"] == signin.NOT_SIGNED_IN
    res = signin.cancel(out["attempt"]["id"], remove_folder=True, cli="claude", folder=folder,
                        ps=box.ps, killpg=box.killpg)
    assert res["ok"] and res["folder_removed"] is True
    assert not (box.home / ".claude-client").exists() and signin._folders() == []


def test_cancel_after_a_timeout_removes_the_empty_folder(box):
    folder = _folder()
    out = signin.start("claude", folder, now=NOW, **box.kw())
    res = signin.status(out["attempt"]["id"], now=NOW + signin.wait_s() + 1, ps=box.ps, killpg=box.killpg,
                        check=lambda *a: signin.NOT_SIGNED_IN)
    assert res["state"] == signin.TIMED_OUT
    res = signin.cancel(out["attempt"]["id"], remove_folder=True, cli="claude", folder=folder,
                        ps=box.ps, killpg=box.killpg)
    assert res["folder_removed"] is True and not (box.home / ".claude-client").exists()


def test_cancel_with_no_attempt_removes_only_an_empty_folder_khipu_made(box):
    """No code (no attempt ever held): Cancel still removes the empty folder,
    but never one that holds anything, one Khipu didn't make, or one a running
    login is using; and without remove_folder it is still no_attempt."""
    folder = _folder()
    assert signin.cancel(None, cli="claude", folder=folder) == {"ok": False, "error": signin.E_NO_ATTEMPT}
    (box.home / ".claude-other").mkdir()
    res = signin.cancel(None, remove_folder=True, cli="claude", folder="~/.claude-other")
    assert res["folder_removed"] is False and (box.home / ".claude-other").exists()
    (box.home / ".claude-client" / "x").write_text("")
    assert signin.cancel(None, remove_folder=True, cli="claude", folder=folder)["folder_removed"] is False
    (box.home / ".claude-client" / "x").unlink()
    out = signin.start("claude", folder, **box.kw())
    res = signin.cancel("fedcba9876543210", remove_folder=True, cli="claude", folder=folder,
                        ps=box.ps, killpg=box.killpg)
    assert res["folder_removed"] is False and (box.home / ".claude-client").exists() and box.killed == []
    assert _attempt_file().exists() and out["ok"]
    signin.cancel(out["attempt"]["id"], ps=box.ps, killpg=box.killpg)
    res = signin.cancel(None, remove_folder=True, cli="claude", folder=folder)
    assert res == {"ok": True, "state": signin.CANCELLED, "stopped": None, "folder_removed": True}
    assert not (box.home / ".claude-client").exists()


# ---- a group that outlives its stop (second review of 419e9c6) -------------------------------


def test_a_new_start_never_forgets_a_surviving_old_group(box):
    folder = _folder()
    signin.start("claude", folder, **box.kw())
    box.table[1_000_050] = {"stat": "S", "lstart": LSTART, "command": "/fake/bin/helper", "pgid": 1_000_001}
    box.end(1_000_001)
    second = signin.start("claude", folder, **box.kw())
    assert not second["ok"] and second["error"] == signin.E_BUSY, (second, box.members(1_000_001))
    assert second["left"] == [1_000_050] and _attempt_file().exists()
    box.end(1_000_050)
    assert signin.start("claude", folder, **box.kw())["ok"]


def test_cancel_keeps_the_record_and_reports_failure_while_the_group_survives(box):
    out = signin.start("claude", _folder(), **box.kw())
    box.table[1_000_001]["ignore"] = {signal.SIGTERM, signal.SIGKILL}
    box.table[1_000_050] = {"stat": "S", "lstart": LSTART, "command": "/fake/bin/helper", "pgid": 1_000_001,
                            "ignore": {signal.SIGTERM, signal.SIGKILL}}
    res = signin.cancel(out["attempt"]["id"], remove_folder=True, ps=box.ps, killpg=box.killpg)
    assert res["stopped"] == "left"
    assert res["ok"] is False and res["error"] == signin.E_NOT_STOPPED and res["left"] == [1_000_001, 1_000_050]
    assert _attempt_file().exists(), (res, box.members(1_000_001))
    assert "folder_removed" not in res and (box.home / ".claude-client").is_dir()
    assert signin.start("claude", out["attempt"]["folder"], **box.kw())["error"] == signin.E_BUSY
    # Stop again, once the group can end, ends it and the record with it.
    for pid in (1_000_001, 1_000_050):
        box.table[pid]["ignore"] = set()
    again = signin.cancel(out["attempt"]["id"], ps=box.ps, killpg=box.killpg)
    assert again["ok"] and again["stopped"] == "killed" and not _attempt_file().exists()


def test_the_reaper_keeps_the_record_while_the_group_survives(box):
    signin.start("claude", _folder(), **box.kw())
    box.table[1_000_050] = {"stat": "S", "lstart": LSTART, "command": "/fake/bin/helper", "pgid": 1_000_001}
    box.end(1_000_001)
    assert signin.reap(ps=box.ps, killpg=box.killpg) == {"ok": True, "reaped": "left", "left": [1_000_050]}
    assert _attempt_file().exists()


def test_a_finished_login_whose_helper_survives_keeps_its_record(box):
    folder = _folder()
    out = signin.start("claude", folder, **box.kw())
    box.table[1_000_050] = {"stat": "S", "lstart": LSTART, "command": "/fake/bin/helper", "pgid": 1_000_001}
    box.end(1_000_001)
    res = signin.status(out["attempt"]["id"], ps=box.ps, check=lambda cli, path: signin.NOT_SIGNED_IN)
    assert res["state"] == signin.NOT_SIGNED_IN and res["left"] == [1_000_050]
    assert _attempt_file().exists() and box.killed == []
    assert signin.start("claude", folder, **box.kw())["error"] == signin.E_BUSY


def test_a_folder_swapped_after_the_last_check_fails_the_start(box, tmp_path):
    folder = _folder()
    path = box.home / ".claude-client"
    outside = tmp_path / "outside"
    outside.mkdir()
    seen = {}

    def swapping_popen(argv, **kw):
        # Right at the spawn, after start's last check before it: the child
        # re-resolves CLAUDE_CONFIG_DIR and would land outside Khipu's folder.
        path.rename(box.home / ".claude-client-moved")
        path.symlink_to(outside, target_is_directory=True)
        seen["target"] = Path(kw["env"]["CLAUDE_CONFIG_DIR"]).resolve()
        return box.popen(argv, **kw)

    res = signin.start("claude", folder, popen=swapping_popen, ps=box.ps, killpg=box.killpg)
    assert res == {"ok": False, "error": signin.E_REFUSED}, (res, seen)
    assert box.killed and box.killed[0][0] == 1_000_001 and box.members(1_000_001) == []
    assert not _attempt_file().exists()


def test_a_parent_folder_swapped_for_a_symlink_after_the_spawn_fails_the_start(box, monkeypatch, tmp_path):
    # The home folder itself is one component of the path the child resolves.
    real_home = box.home
    monkeypatch.setenv("HOME", str(tmp_path / "via" / "home"))
    (tmp_path / "via").mkdir()
    os.rename(real_home, tmp_path / "via" / "home")
    folder = _folder()
    decoy = tmp_path / "decoy"
    (decoy / ".claude-client").mkdir(parents=True)

    def swapping_popen(argv, **kw):
        os.rename(tmp_path / "via", tmp_path / "via-moved")
        os.mkdir(tmp_path / "via")
        os.symlink(decoy, tmp_path / "via" / "home")
        return box.popen(argv, **kw)

    res = signin.start("claude", folder, popen=swapping_popen, ps=box.ps, killpg=box.killpg)
    assert res == {"ok": False, "error": signin.E_REFUSED}
    assert box.members(1_000_001) == [] and not _attempt_file().exists()


def test_a_refused_start_whose_group_survives_keeps_the_record(box, tmp_path):
    folder = _folder()
    path = box.home / ".claude-client"

    def swapping_popen(argv, **kw):
        proc = box.popen(argv, **kw)
        box.table[proc.pid]["ignore"] = {signal.SIGTERM, signal.SIGKILL}
        path.rename(box.home / ".claude-client-moved")
        path.mkdir()
        return proc

    res = signin.start("claude", folder, popen=swapping_popen, ps=box.ps, killpg=box.killpg)
    assert res["ok"] is False and res["error"] == signin.E_REFUSED and res["left"] == [1_000_001]
    assert _attempt_file().exists()
