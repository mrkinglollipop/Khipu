"""khipu.routing_sources, phase 1b of docs/plans/2026-10-14-routing-truth.md:
the job runner adapter (read only), the lane map, the usage cache source,
silent and unreadable runners, the table pointer, and write order settled
when a line is written.

Hermetic: each test gets its own data dir and a fake clock, and copies the
runner fixtures (tests/fixtures/routing, shaped like a real lanes runner's
files with every name changed) into its own temp folder.
"""
from __future__ import annotations

import builtins
import fcntl
import hashlib
import io
import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from khipu import routing, routing_sources

FIXTURES = Path(__file__).parent / "fixtures" / "routing"
T0 = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
ACCOUNTS = [{"label": "Claude", "harness": "claude"},
            {"label": "Claude second", "harness": "claude", "home": "/logins/claude-second"},
            {"label": "Codex", "harness": "codex"},
            {"label": "Grok", "harness": "grok"},
            {"label": "Muse", "harness": "muse"}]
SECOND = "claude|/logins/claude-second"
LANES_A = {"second": "Claude second", "main": "Claude", "codex": "Codex", "grok": "Grok", "grokcheck": "Grok",
           "muse": "Muse"}
LANES_B = {"second": "Claude second", "second-sonnet": "Claude second", "codex": "Codex", "grok": "Grok"}


class Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> datetime:
        self.now = self.now + timedelta(**kw)
        return self.now


@pytest.fixture
def home(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    runners = tmp_path / "runners"
    shutil.copytree(FIXTURES / "lanes-a", runners / "project-a")
    shutil.copytree(FIXTURES / "lanes-b", runners / "project-b")
    (data / "config.json").write_text(json.dumps({"usage_accounts": ACCOUNTS}))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    clock = Clock(T0)
    monkeypatch.setattr(routing, "_now", clock)
    return {"data": data, "clock": clock, "tmp": tmp_path, "a": runners / "project-a", "b": runners / "project-b"}


def _set(home, **values) -> None:
    path = home["data"] / "config.json"
    config = json.loads(path.read_text())
    config.update(values)
    path.write_text(json.dumps(config))


def _register(home, *, b: bool = False, lanes_a: dict | None = None, lanes_b: dict | None = None) -> None:
    sources = [{"name": "project A", "kind": "lanes", "path": str(home["a"])}]
    lane_map = {"project A": LANES_A if lanes_a is None else lanes_a}
    if b:
        sources.append({"name": "project B", "kind": "lanes", "path": str(home["b"])})
        lane_map["project B"] = LANES_B if lanes_b is None else lanes_b
    _set(home, routing_sources=sources, routing_lane_map=lane_map)


def _account(view: dict, key: str) -> dict:
    return next(a for a in view["accounts"] if a["key"] == key)


def _beat(folder: Path, at: datetime, interval: float = 20.0) -> None:
    (folder / "state" / "heartbeat.json").write_text(json.dumps(
        {"pid": 1, "time": at.timestamp(), "at": routing._iso(at), "interval": interval, "error": None}))


def _append(folder: Path, line: str) -> None:
    with open(folder / "events.log", "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _usage(home, rows: dict) -> None:
    path = home["data"] / "state" / "usage.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 2, "rows": rows}))


def _row(at: datetime, windows: list[dict], **kw) -> dict:
    row = {"ok": True, "windows": windows, "probed_at": routing._iso(at), "last_attempt_at": routing._iso(at),
           "last_good_at": routing._iso(at), "source": "live", "error": None}
    row.update(kw)
    return row


# ---- the runner fixtures ---------------------------------------------------------------


def test_a_muse_guess_and_a_second_account_line_with_a_parsed_time(home):
    _register(home)
    view = routing.read_view()
    muse = _account(view, "muse|")
    assert (muse["state"], muse["basis"], muse["until"]) == ("limited", "estimated", "2026-10-14T15:20:02Z")
    second = _account(view, SECOND)
    assert (second["state"], second["basis"], second["until"]) == ("limited", "cli", "2026-10-14T17:52:00Z")
    assert second["witness"]["source"] == "runner" and second["witness"]["message_time"] is True
    assert "time from Claude's message, seen by project A lanes (read by Khipu)" in routing.account_line(second, T0)
    assert "back about 15:20Z" in routing.account_line(muse, T0) and "a guess" in routing.account_line(muse, T0)
    # The basis rule, on the real shapes: a 30-minute rest cut to whole
    # seconds lands a second short of 30 minutes, and is still the guess.
    at = datetime(2026, 10, 14, 3, 9, 3, tzinfo=timezone.utc)
    assert routing_sources.runner_basis(at, datetime(2026, 10, 14, 3, 39, 2, tzinfo=timezone.utc)) == "estimated"
    assert routing_sources.runner_basis(T0, T0 + timedelta(minutes=30)) == "estimated"
    assert routing_sources.runner_basis(T0 + timedelta(seconds=13), T0 + timedelta(hours=2, minutes=52)) == "cli"
    # Every evidence line is worded by Khipu from the line's parsed fields,
    # never the runner's own text.
    assert second["witness"]["evidence"] == "project A runner: lane second limited until 17:52Z (pipeline pipe-12)"


def test_a_probe_at_100_percent_replaces_a_runners_guess(home):
    _register(home)
    assert _account(routing.read_view(), "muse|")["basis"] == "estimated"
    resets = T0 + timedelta(days=2)
    _usage(home, {"muse|": _row(T0 - timedelta(minutes=2), [
        {"name": "weekly", "used_pct": 100.0, "resets_at": routing._iso(resets), "window_mins": 10080}],
        source="last_observed", observed_at=routing._iso(T0 - timedelta(minutes=2)))})
    muse = _account(routing.read_view(), "muse|")
    assert (muse["state"], muse["basis"], muse["until"]) == ("limited", "cli", routing._iso(resets))
    assert muse["witness"]["source"] == "probe" and muse["witness"]["observed"] is True
    assert "read from Muse's last turn" in routing.account_line(muse, T0)


def test_a_later_probe_with_headroom_reopens(home):
    _register(home)
    windows = [{"name": "five_hour", "used_pct": 40.0, "resets_at": routing._iso(T0 + timedelta(hours=2))}]
    # A probe from before the limit was seen does not end it.
    _usage(home, {SECOND: _row(T0 - timedelta(hours=2), windows)})
    assert _account(routing.read_view(), SECOND)["state"] == "limited"
    home["clock"].advance(minutes=5)
    _usage(home, {SECOND: _row(T0 + timedelta(minutes=4), windows)})
    second = _account(routing.read_view(), SECOND)
    assert (second["state"], second["witness"]["source"]) == ("open", "probe")
    assert routing.account_line(second, home["clock"].now).startswith("Claude second: open; read from Claude 1 min ago")


def test_two_lanes_on_one_account_close_together(home):
    _register(home, b=True)
    _append(home["a"], "2026-10-14T14:58:00Z pipe-20 LIMITED lane=grokcheck until=2026-10-14T20:02:00Z "
                       "job=check-20 (account limit reached); the check step is retried later")
    view = routing.read_view()
    grok = _account(view, "grok|")
    assert (grok["state"], grok["until"]) == ("limited", "2026-10-14T20:02:00Z")
    assert view["unmatched"] == []
    # Project A's and project B's "second" lanes are one Claude account: one line, the newest sighting.
    seconds = [a for a in view["accounts"] if a["key"] == SECOND]
    assert len(seconds) == 1 and seconds[0]["witness"]["written_by"] == {"kind": "runner", "name": "project B"}
    # B launched on that account after A had seen its limit: doctor says so.
    assert "A job was started on Claude second after it had reached its limit." in \
        routing.doctor_block()["warnings"]


def test_a_limit_from_an_unmatched_lane_is_reported_and_never_guessed(home):
    _register(home, lanes_a={k: v for k, v in LANES_A.items() if k != "second"})
    view = routing.read_view()
    assert [(u["runner"], u["lane"]) for u in view["unmatched"]] == [("project A", "second")]
    assert _account(view, SECOND)["state"] == "unknown"
    assert "A limit came from project A's second lane, which isn't matched to an account." in \
        routing.doctor_block()["warnings"]
    runner = view["sources"]["runners"][0]
    assert runner["unmatched_lanes"] == ["second"]


def test_a_runner_file_that_fails_to_parse_is_reported_with_its_last_good_read(home, capsys):
    # The second account's limit is known from global.json alone (its LIMITED line is not in the tail).
    lines = (home["a"] / "events.log").read_text().splitlines()
    (home["a"] / "events.log").write_text("\n".join(x for x in lines if "pipe-12 LIMITED" not in x) + "\n")
    _register(home)
    assert _account(routing.read_view(), SECOND)["state"] == "limited"
    home["clock"].advance(minutes=1)
    (home["a"] / "state" / "global.json").write_text("{not json")
    (home["a"] / "state" / "heartbeat.json").write_text("[]")
    view = routing.read_view()
    second = _account(view, SECOND)
    assert (second["state"], second["until"]) == ("limited", "2026-10-14T17:52:00Z")  # the last good read
    runner = view["sources"]["runners"][0]
    assert runner["unreadable"] and not runner["silent"]  # the last good heartbeat still counts
    assert runner["files"]["global"] == {"error": "not readable JSON", "good_at": "2026-10-14T15:00:00Z",
                                         "since": "2026-10-14T15:01:00Z"}
    assert "Khipu can't read project A's files since 15:01Z." in routing.doctor_block()["warnings"]
    change = view["changes"][-1]
    assert "runner:project A" in change["entities"]
    assert routing.run_cli(_args(route_cmd="sources")) == 0
    out = capsys.readouterr().out
    assert "global: can't read it (not readable JSON) since 15:01Z; last good read 15:00Z" in out
    # Fixed: read again.
    shutil.copy(FIXTURES / "lanes-a" / "state" / "global.json", home["a"] / "state" / "global.json")
    _beat(home["a"], home["clock"].now)
    assert not routing.read_view()["sources"]["runners"][0]["unreadable"]


def test_a_cleared_runner_limit_stays_cleared_until_the_runner_writes_a_newer_limited_line(home):
    _register(home)
    assert _account(routing.read_view(), SECOND)["state"] == "limited"
    home["clock"].advance(minutes=1)
    routing.clear("Claude second", via="app")
    assert _account(routing.read_view(), SECOND)["state"] == "open"
    home["clock"].advance(minutes=1)
    _beat(home["a"], home["clock"].now)  # the runner keeps rewriting its files: nothing new about the limit
    (home["a"] / "state" / "global.json").write_text((home["a"] / "state" / "global.json").read_text() + " ")
    second = _account(routing.read_view(), SECOND)
    assert (second["state"], second["witness"]["cleared"]) == ("open", True)
    home["clock"].advance(minutes=1)
    _append(home["a"], "2026-10-14T15:03:00Z pipe-21 LIMITED lane=second until=2026-10-14T19:52:00Z "
                       "job=fix-21 (account limit reached); the fix step is retried later")
    second = _account(routing.read_view(), SECOND)
    assert (second["state"], second["until"]) == ("limited", "2026-10-14T19:52:00Z")


def test_a_stale_heartbeat_makes_an_account_unknown_when_the_runner_is_its_only_source(home):
    _register(home)
    view = routing.read_view()
    assert _account(view, SECOND)["state"] == "limited"
    assert view["valid_until"] == "2026-10-14T15:01:30Z"  # 14:59:50 plus five 20-second intervals
    home["clock"].now = T0 + timedelta(minutes=2)
    view = routing.read_view()  # no file changed: the clock alone
    second = _account(view, SECOND)
    assert second["state"] == "unknown" and second["last_seen"]["state"] == "limited"
    assert "runner:project A" in view["changes"][-1]["entities"]
    assert "Your project A job runner stopped reporting at 14:59Z" in view["changes"][-1]["lines"]
    assert "Your project A job runner stopped reporting at 14:59Z." in routing.doctor_block()["warnings"]
    # With another source for the account, the runner's limit still counts.
    _usage(home, {SECOND: _row(T0 - timedelta(hours=3), [{"name": "five_hour", "used_pct": 10.0,
                                                          "resets_at": routing._iso(T0)}])})
    assert _account(routing.read_view(), SECOND)["state"] == "limited"


def test_a_source_path_under_a_credential_folder_is_refused(home, monkeypatch):
    keys = home["tmp"] / "API Keys" / "runner"
    shutil.copytree(FIXTURES / "lanes-a", keys)
    portfolio = home["tmp"] / "My Portfolio" / "runner"
    shutil.copytree(FIXTURES / "lanes-a", portfolio)
    link = home["tmp"] / "linked-runner"
    link.symlink_to(portfolio)
    # A file inside a registered folder that resolves into one is refused too.
    (home["b"] / "state" / "global.json").unlink()
    (home["b"] / "state" / "global.json").symlink_to(keys / "state" / "global.json")
    # Written by hand: `khipu config` would refuse the first two.
    _set(home, routing_sources=[{"name": "keys", "kind": "lanes", "path": str(keys)},
                                {"name": "linked", "kind": "lanes", "path": str(link)},
                                {"name": "project B", "kind": "lanes", "path": str(home["b"])}],
         routing_lane_map={"project B": LANES_B})
    opened: list[str] = []
    real_open = os.open

    def spy(path, flags, *a, **kw):
        opened.append(os.path.realpath(os.fspath(path)))
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(os, "open", spy)
    view = routing.read_view()
    assert view["sources"]["refused"] == ["keys", "linked"]
    assert [r["name"] for r in view["sources"]["runners"]] == ["project B"]
    assert view["sources"]["runners"][0]["files"]["global"]["error"] == "not allowed to read it"
    assert not any("API Keys" in p or "My Portfolio" in p for p in opened)
    assert "A job runner folder was refused because it is inside a private folder." in \
        routing.doctor_block()["warnings"]
    assert _account(view, SECOND)["state"] == "limited"  # project B's own events.log still counts


def _snapshot(folder: Path) -> dict:
    return {str(p.relative_to(folder)): (p.stat().st_mtime_ns,
                                         hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
            for p in sorted(folder.rglob("*"))}


def _args(**kw):
    import argparse

    base = {"route_cmd": None, "json": False, "since": None, "summary": False, "brief": False, "view_account": None}
    base.update(kw)
    return argparse.Namespace(**base)


def test_every_runner_file_is_opened_read_only(home, monkeypatch, capsys):
    _register(home, b=True)
    routing.add_rule("Never a -fast tier.", via="app")
    folders = [str(home["a"]), str(home["b"])]
    before = {f: _snapshot(Path(f)) for f in folders}
    calls: list[tuple[str, object]] = []
    real_os_open, real_open, real_io_open = os.open, builtins.open, io.open

    def inside(path) -> bool:
        try:
            text = os.path.realpath(os.fspath(path))
        except TypeError:
            return False
        return any(text.startswith(os.path.realpath(f)) for f in folders)

    # Every runner file by its device and inode: a lock is caught on any
    # descriptor that reaches one, however it was opened (os.open, open(),
    # io.open, Path.open or anything else).
    runner_files = {(st.st_dev, st.st_ino): str(f) for f in (p for d in folders for p in Path(d).rglob("*"))
                    if f.is_file() for st in [f.stat()]}
    locks: list[tuple[str, str]] = []
    real_flock, real_lockf, real_fcntl = fcntl.flock, fcntl.lockf, fcntl.fcntl
    set_lock = {getattr(fcntl, n) for n in ("F_SETLK", "F_SETLKW", "F_OFD_SETLK", "F_OFD_SETLKW") if hasattr(fcntl, n)}
    # Open flags that take a lock themselves (macOS).
    lock_bits = getattr(os, "O_SHLOCK", 0) | getattr(os, "O_EXLOCK", 0)

    def spy_os_open(path, flags, *a, **kw):
        fd = real_os_open(path, flags, *a, **kw)
        if inside(path):
            calls.append((os.fspath(path), flags))
        return fd

    def runner_fd(target) -> str | None:
        try:
            fd = target if isinstance(target, int) else target.fileno()
            st = os.fstat(fd)
        except (AttributeError, OSError, ValueError):
            return None
        return runner_files.get((st.st_dev, st.st_ino))

    def spy_flock(target, op):
        if runner_fd(target):
            locks.append((runner_fd(target), "flock"))
        return real_flock(target, op)

    def spy_lockf(target, cmd, *a):
        if runner_fd(target):
            locks.append((runner_fd(target), "lockf"))
        return real_lockf(target, cmd, *a)

    def spy_fcntl(target, cmd, *a):
        if cmd in set_lock and runner_fd(target):
            locks.append((runner_fd(target), "fcntl"))
        return real_fcntl(target, cmd, *a)

    def spy_open(file, mode="r", *a, **kw):
        if not isinstance(file, int) and inside(file):
            calls.append((os.fspath(file), mode))
        return real_open(file, mode, *a, **kw)

    monkeypatch.setattr(os, "open", spy_os_open)
    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(io, "open", spy_open)
    monkeypatch.setattr(fcntl, "flock", spy_flock)
    monkeypatch.setattr(fcntl, "lockf", spy_lockf)
    monkeypatch.setattr(fcntl, "fcntl", spy_fcntl)
    routing.read_view()
    routing.doctor_block()
    assert routing.run_cli(_args(route_cmd="sources", json=True)) == 0
    home["clock"].advance(minutes=3)  # a refold after the heartbeat went quiet
    routing.read_view()
    monkeypatch.setattr(io, "open", real_io_open)
    assert calls, "the runner files were never read"
    assert locks == []  # no lock of any kind on a runner file
    write_bits = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
    for path, how in calls:
        if isinstance(how, int):
            assert how & write_bits == 0, path
            assert how & os.O_ACCMODE == os.O_RDONLY, path
            assert how & lock_bits == 0, path  # no lock taken by the open itself
        else:
            assert not set(how) & set("wax+"), path
    assert {f: _snapshot(Path(f)) for f in folders} == before  # nothing written or left behind
    capsys.readouterr()


def test_a_config_key_outside_the_named_list_never_reaches_the_record(home, capsys):
    _register(home, b=True)
    routing.add_rule("Never a -fast tier.", via="app")
    view = routing.read_view()
    kept = view["source_cache"]["runners"]["project A"]["config"]["data"]
    assert set(kept) == {"lanes", "check_lane", "fix_lanes"}
    assert all(set(spec) == {"kind", "model", "confirm_lane", "fast", "model_in_launch", "script"}
               for spec in kept["lanes"].values())
    texts = [routing.view_file().read_text(), routing.events_file().read_text(),
             json.dumps(routing.doctor_block())]
    for argv in (_args(json=True), _args(route_cmd="sources", json=True), _args(summary=True)):
        assert routing.run_cli(argv) == 0
        texts.append(capsys.readouterr().out)
    assert not any("CANARY" in text for text in texts)


def test_a_pasted_secret_in_a_runner_line_is_redacted(home):
    _register(home)
    secret = "sk-ant-api03-" + "x" * 40
    _append(home["a"], f"2026-10-14T14:59:00Z pipe-30 LIMITED lane=main until=2026-10-14T16:00:00Z "
                       f"job=fix-30 (account limit reached: key {secret})")
    view = routing.read_view()
    assert secret not in routing.view_file().read_text()
    assert _account(view, "claude|")["state"] == "limited"


# ---- the usage cache --------------------------------------------------------------------


def test_the_usage_cache_gives_model_scoped_limits_and_down(home):
    resets = T0 + timedelta(days=2)
    _usage(home, {
        "claude|": _row(T0 - timedelta(minutes=1), [
            {"name": "five_hour", "used_pct": 20.0, "resets_at": routing._iso(T0 + timedelta(hours=3))},
            {"name": "weekly", "used_pct": 60.0, "resets_at": routing._iso(resets)},
            {"name": "weekly_opus", "used_pct": 100.0, "resets_at": routing._iso(resets), "model": "Opus"}]),
        "codex|": {"ok": False, "error": "not_signed_in", "last_error": "not_signed_in", "windows": [],
                   "probed_at": routing._iso(T0), "last_attempt_at": routing._iso(T0)},
    })
    view = routing.read_view()
    assert view["record"] is True
    claude = _account(view, "claude|")
    assert claude["state"] == "open" and [m["model"] for m in claude["models"]] == ["Opus"]
    line = routing.account_line(claude, T0)
    assert "Opus limit reached, back at Fri 15:00Z" in line and "(other models open)" in line
    codex = _account(view, "codex|")
    assert (codex["state"], codex["reason"]) == ("down", "not signed in")


# ---- the table pointer --------------------------------------------------------------------


def _table(path: Path, body: str, stamp: str) -> None:
    path.write_text(f"# Routing now\n\n## NOW\n```\nupdated: {stamp} by an orchestrator\n{body}\n```\n\n"
                    "## Change log\n- an entry\n")


def test_the_table_pointer_moves_the_version_and_never_carries_its_prose(home):
    table = home["tmp"] / "routing-table.md"
    _table(table, "PROSE-MARKER lane order one", "2026-10-14 13:48Z")
    _set(home, routing_table=str(table))
    view = routing.read_view()
    first = view["version"]
    assert routing.summary(view, now=T0)["text"].splitlines()[1] == (
        "These account lines are newer than the routing table's account rows; the table's NOW block was "
        "last stamped 13:48Z, 1 h 12 min ago.")
    home["clock"].advance(minutes=5)
    routing.read_view()
    assert routing.read_view()["version"] == first  # nothing changed
    _table(table, "PROSE-MARKER lane order two", "2026-10-14 15:04Z")
    view = routing.read_view()
    assert view["version"] == first + 1 and view["changes"][-1]["entities"] == ["table"]
    line = view["changes"][-1]["lines"][0]
    assert line.startswith("The routing table's NOW block changed at ")
    assert line.endswith("(stamped 15:04Z).")
    assert "by an orchestrator" not in routing.view_file().read_text()
    assert "PROSE-MARKER" not in routing.view_file().read_text()


# ---- write order, settled when a line is written (the phase 1a known limit) -----------------


@pytest.mark.parametrize("n", [0, 1, 10])
def test_a_run_of_lines_from_a_clock_reading_2001_never_ages_the_history(home, n):
    clock = home["clock"]
    routing.report(account="Claude", state="open", author={"kind": "conversation", "harness": "claude_code"})
    clock.advance(minutes=1)
    routing.add_hold("keep Codex", reason="keep", scope="account", account="Codex", owner=True, via="app")
    clock.now = datetime(2001, 1, 1, tzinfo=timezone.utc)
    for _ in range(3):
        routing.report(account="Muse", state="open", author={"kind": "conversation", "harness": "codex"})
    for i in range(n):
        clock.now = T0 + timedelta(minutes=10 + i)
        routing.report(account="Codex", state="open", author={"kind": "conversation", "harness": "codex"})
    events, _bad = routing.read_events()
    assert [e["seq"] for e in events] == list(range(len(events)))
    assert routing._in_write_order(events)[0]["written_at"] == routing._iso(T0)  # the history keeps its times
    assert routing._in_write_order(events)[1]["written_at"] == routing._iso(T0 + timedelta(minutes=1))
    clock.now = T0 + timedelta(minutes=30)
    view = routing.read_view()
    assert _account(view, "claude|")["state"] == "open"
    assert _account(view, "claude|")["witness"]["at"] == routing._iso(T0)
    assert view["holds"][0]["remind"] is False
    warnings = routing.doctor_block()["warnings"]
    assert any("dated well before" in w and "2001-01-01T00:00:00Z" in w for w in warnings), warnings
    assert not any("dated well after" in w for w in warnings), warnings


def test_the_fold_orders_lines_by_their_sequence_number(home):
    for account in ("Claude", "Codex"):
        routing.report(account=account, state="open", author={"kind": "conversation", "harness": "codex"})
        home["clock"].advance(minutes=1)
    path = routing.events_file()
    lines = path.read_text().splitlines()
    path.write_text("\n".join(reversed(lines)) + "\n")  # a copy that came back out of order
    events, _bad = routing.read_events()
    assert [e["seq"] for e in events] == [0, 1] and [e["_seq"] for e in events] == [0, 1]
    # A line with no sequence number (phase 1a) keeps its place in the file.
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "evidence", "id": "old", "source": "report", "state": "open",
                             "account": "muse|", "written_at": routing._iso(T0)}) + "\n")
    events, _bad = routing.read_events()
    assert [e.get("seq") for e in events] == [0, 1, None]
    routing.report(account="Muse", state="limited", until="1h", author={"kind": "conversation", "harness": "codex"})
    assert routing.read_events()[0][-1]["seq"] == 2


# ---- doctor's checks from the runner files ------------------------------------------------


def test_doctor_checks_watched_rules_against_runner_settings_and_never_passes_what_it_cannot_see(home):
    _register(home)
    assert routing.doctor_block()["rule_checks"] == []  # no rules: nothing is watched (the public default)
    for text in ("Grok never builds or fixes privacy or safety code.",
                 "Every Grok result that is acted on gets a re-check from a different model family.",
                 "Never a -fast tier.", "Never the paid xAI API."):
        routing.add_rule(text, via="app")
    config = json.loads((home["a"] / "config.json").read_text())
    config["lanes"]["codex"]["model"] = "gpt-6.1-sol-fast"
    del config["lanes"]["grokcheck"]["confirm_lane"]
    (home["a"] / "config.json").write_text(json.dumps(config))
    _append(home["a"], "2026-10-14T14:59:30Z pipe-40 ADDED branch=project-a/private-thing stage=built "
                       "check_lane=codex fix_lanes=grok,second max_checks=3 privacy")
    block = routing.doctor_block()
    broken = [c["text"] for c in block["rule_checks"] if c["status"] == "broken"]
    assert broken == [
        "A standing rule may be broken: project A's settings name a model ending in -fast for its codex lane.",
        "A standing rule may be broken: project A's Grok check lane has no confirming lane from another model "
        "family.",
        "A standing rule may be broken: a Grok lane is set to work on pipe-40, which is marked private."]
    assert all(text in block["warnings"] for text in broken)
    unseen = [c["text"] for c in block["rule_checks"] if c["status"] == "cannot_check"]
    assert "project A's grok lane: can't be checked: the model is set inside launch-grok.sh" in unseen
    assert ("project A's grok lane: whether it uses the paid xAI API can't be checked: the launch environment "
            "is set inside launch-grok.sh") in unseen
    assert not any(c["status"] not in ("broken", "cannot_check") for c in block["rule_checks"])


def test_route_json_carries_what_the_app_card_needs(home, capsys):
    """Phase 4: `khipu route --json` (the app's fixed-argv read) carries each
    watched rule's check with the time Khipu read the file it was judged on,
    the times the app words its warnings by, and each author in words; and
    still no fold internals."""
    _register(home)
    routing.add_rule("Never a -fast tier.", via="app")
    config = json.loads((home["a"] / "config.json").read_text())
    config["lanes"]["codex"]["model"] = "gpt-6.1-sol-fast"
    (home["a"] / "config.json").write_text(json.dumps(config))
    conversation = {"kind": "conversation", "harness": "claude_code", "project": "project-b"}
    routing.report(account="Muse", state="limited", until="1h", author=conversation)
    routing.add_hold("keep it free", reason="keep", scope="account", account="Muse", author=conversation)
    assert routing.run_cli(_args(json=True)) == 0
    out = json.loads(capsys.readouterr().out)
    broken = [c for c in out["rule_checks"] if c["status"] == "broken"]
    assert [c["text"] for c in broken] == [
        "A standing rule may be broken: project A's settings name a model ending in -fast for its codex lane."]
    assert broken[0]["rule"] == 1 and broken[0]["at"] == routing._iso(T0)
    assert out["warn_after"] == {"disagreement": 30 * 60, "not_refreshed": 5 * 60}
    muse = next(a for a in out["accounts"] if a["label"] == "Muse")
    assert muse["witness"]["by"] == "a Claude Code conversation in project-b"
    assert out["holds"][0]["by"] == "a Claude Code conversation in project-b"
    assert not {"fingerprint", "digest", "source_cache"} & set(out)


def test_a_launch_while_paused_is_named(home):
    _register(home)
    routing.pause("Claude", via="app")
    home["clock"].advance(minutes=1)
    _append(home["a"], "2026-10-14T15:00:30Z pipe-41 LAUNCH fix job=fix-41 lane=main round=1 worktree=/work/a/41")
    assert "A job was started on Claude while you had it paused." in routing.doctor_block()["warnings"]


# ---- fix round 1 ------------------------------------------------------------------------------

BIG = "1" + "0" * 400  # a number JSON reads but a float can't hold


@pytest.mark.parametrize("name,text,rejected", [
    ("heartbeat", '{"time": 1791989990.0, "interval": 1e12}', True),
    ("heartbeat", '{"time": 1791989990.0, "interval": %s}' % BIG, True),
    ("heartbeat", '{"time": %s, "interval": 20}' % BIG, True),
    ("heartbeat", '{"time": 253402300799, "interval": 20}', False),
    ("global", '{"limited": {"main": {"until": %s}, "second": {"until": 1792000320.0}}}' % BIG, False),
], ids=["interval-1e12", "interval-400-digits", "time-400-digits", "time-year-9999", "until-400-digits"])
def test_a_runner_file_with_a_number_out_of_range_is_reported_and_never_breaks_a_read(home, capsys, name, text,
                                                                                         rejected):
    _register(home)
    assert _account(routing.read_view(), SECOND)["state"] == "limited"
    home["clock"].advance(seconds=10)
    (home["a"] / "state" / f"{name}.json").write_text(text)
    view = routing.read_view()
    runner = view["sources"]["runners"][0]
    if rejected:  # reported with its last good read, never guessed around
        assert runner["files"][name]["error"] == "a number out of range"
        assert runner["files"][name]["good_at"] == "2026-10-14T15:00:00Z"
        assert runner["heartbeat_at"] == "2026-10-14T14:59:50Z" and not runner["silent"]
    else:
        assert runner["files"][name]["error"] is None
    assert _account(view, SECOND)["state"] == "limited"
    assert routing.run_cli(_args()) == 0
    assert routing.run_cli(_args(route_cmd="sources")) == 0
    routing.doctor_block()
    capsys.readouterr()


def test_a_usage_figure_out_of_range_never_breaks_a_read(home):
    path = home["data"] / "state" / "usage.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = json.dumps(_row(T0, [])).replace('"windows": []', '"windows": [{"name": "weekly", "used_pct": %s}]' % BIG)
    path.write_text('{"version": 2, "rows": {"claude|": %s}}' % row)
    assert _account(routing.read_view(), "claude|")["state"] == "unknown"


def test_a_window_at_100_percent_with_no_reset_time_never_counts_as_headroom(home):
    _register(home)
    assert _account(routing.read_view(), SECOND)["state"] == "limited"
    at = T0 - timedelta(minutes=1)
    _usage(home, {SECOND: _row(at, [{"name": "five_hour", "used_pct": 100.0, "resets_at": None}]),
                  "claude|": _row(at, [{"name": "five_hour", "used_pct": 40.0, "resets_at": None},
                                       {"name": "weekly_opus", "used_pct": 100.0, "resets_at": None, "model": "Opus"},
                                       {"name": "weekly_sonnet", "used_pct": 100.0,
                                        "resets_at": routing._iso(at - timedelta(hours=1)), "model": "Sonnet"}])})
    items, _status = routing_sources.usage_evidence()
    assert not any(e["state"] == "open" and (e["used_pct"] or 0) >= 100 for e in items)
    assert [(e["account"], e["scope"], e["state"]) for e in items] == [("claude|", "all", "open")]
    second = _account(routing.read_view(), SECOND)
    assert (second["state"], second["until"], second["witness"]["source"]) == (
        "limited", "2026-10-14T17:52:00Z", "runner")


def test_a_launch_before_a_pause_is_not_named_as_a_launch_while_paused(home):
    _register(home, b=True)  # project B launched on the second account at 14:47Z, after its limit began
    assert _account(routing.read_view(), SECOND)["state"] == "limited"
    routing.pause("Claude second", via="app")
    second = _account(routing.read_view(), SECOND)
    assert second["state"] == "paused" and second.get("since") is None
    warnings = routing.doctor_block()["warnings"]
    assert not any(w.startswith("A job was started on Claude second") for w in warnings), warnings
    home["clock"].advance(minutes=1)
    _append(home["b"], "2026-10-14T15:00:30Z pipe-93 LAUNCH fix job=auto-fix-93-r1 lane=second round=1 "
                       "worktree=/work/project-b/wt-93")
    assert "A job was started on Claude second while you had it paused." in routing.doctor_block()["warnings"]


@pytest.mark.parametrize("old", [
    {"state": "open"},  # older than routing_stale_after
    {"state": "limited", "until": "1h"},  # a limit whose time has passed
])
def test_evidence_that_no_longer_counts_never_revives_a_silent_runners_limit(home, old):
    _register(home)
    home["clock"].now = T0 - timedelta(hours=13)
    routing.report(account="Claude second", author={"kind": "conversation", "harness": "claude_code"}, **old)
    home["clock"].now = T0 + timedelta(minutes=10)
    second = _account(routing.read_view(), SECOND)
    assert second["state"] == "unknown" and second.get("silent") is True
    # A source that still counts keeps the runner's limit, as before.
    _usage(home, {SECOND: _row(T0 - timedelta(hours=3), [{"name": "five_hour", "used_pct": 10.0,
                                                          "resets_at": routing._iso(T0)}])})
    assert _account(routing.read_view(), SECOND)["state"] == "limited"


def test_a_low_clock_run_after_a_one_line_record_keeps_the_phase_1a_handling(home):
    # The scope's Known limits: a record of one line is not stamped (one line
    # written ahead would otherwise pin every later line), so a longer run of
    # lines from a clock reading 2001 still ages it.
    clock = home["clock"]
    routing.report(account="Claude", state="open", author={"kind": "conversation", "harness": "claude_code"})
    clock.now = datetime(2001, 1, 1, tzinfo=timezone.utc)
    for _ in range(3):
        routing.report(account="Muse", state="open", author={"kind": "conversation", "harness": "codex"})
    clock.now = T0 + timedelta(minutes=10)
    routing.report(account="Codex", state="open", author={"kind": "conversation", "harness": "codex"})
    assert not any("clock_at" in e for e in routing.read_events()[0])
    clock.now = T0 + timedelta(minutes=30)
    assert _account(routing.read_view(), "claude|")["state"] == "unknown"
    scope = (Path(__file__).parents[3] / "docs" / "plans" / "2026-10-14-routing-truth.md").read_text()
    assert "A record of one line (phase 1b)." in scope


def _confirm_checks(lanes: dict) -> list[dict]:
    cache = {"runners": {"project A": {"config": {"data": {"lanes": lanes, "check_lane": None, "fix_lanes": []}}}}}
    rules = [{"n": 2, "text": "Every Grok result that is acted on gets a re-check from a different model family."}]
    return routing_sources.rule_checks(cache, rules)


def test_a_grok_check_lane_whose_confirming_lane_is_missing_or_unknown_never_passes():
    facts = {"fast": False, "model_in_launch": False, "script": None}
    gc = dict(facts, kind="grokcheck", model=None, confirm_lane=None)
    other = dict(facts, kind="claude", model=None, confirm_lane=None)
    assert _confirm_checks({"gc": dict(gc, confirm_lane="c"), "c": other}) == []
    missing = _confirm_checks({"gc": dict(gc, confirm_lane="nosuch")})
    assert [c["status"] for c in missing] == ["broken"]
    assert "confirming lane" in missing[0]["text"]
    unknown = _confirm_checks({"gc": dict(gc, confirm_lane="c"), "c": dict(other, kind=None)})
    assert [(c["status"], c["text"]) for c in unknown] == [
        ("cannot_check", "project A's Grok check lane: whether its confirming lane c is from another model "
                         "family can't be checked: its kind isn't set")]


def test_a_private_pipeline_on_the_default_lanes_is_checked_against_them(home):
    _register(home)
    routing.add_rule("Grok never builds or fixes privacy or safety code.", via="app")
    config = json.loads((home["a"] / "config.json").read_text())
    config["defaults"]["fix_lanes"] = ["grok"]
    (home["a"] / "config.json").write_text(json.dumps(config))
    _append(home["a"], "2026-10-14T14:59:40Z pipe-50 ADDED branch=project-a/thing stage=built max_checks=3 privacy")
    _append(home["a"], "2026-10-14T14:59:41Z pipe-51 ADDED branch=project-a/other stage=built check_lane=codex "
                       "fix_lanes=second max_checks=3 privacy")
    broken = [c["text"] for c in routing.doctor_block()["rule_checks"] if c["status"] == "broken"]
    assert broken == ["A standing rule may be broken: a Grok lane is set to work on pipe-50, which is marked private."]


# ---- fix round 2 ------------------------------------------------------------------------


def _limit(folder: Path, lanes: dict[str, datetime]) -> None:
    """The runner's global.json, holding these lanes' limits."""
    (folder / "state" / "global.json").write_text(json.dumps(
        {"limited": {lane: {"until": until.timestamp(), "reason": "fix-40: account limit reached"}
                     for lane, until in lanes.items()}}))


def _scroll(folder: Path, at: datetime, size: int = 600 * 1024) -> None:
    """Ordinary lines enough to push every earlier line out of the tail."""
    line = f"{routing._iso(at)} pipe-41 NOTE " + "x" * 80 + "\n"
    with open(folder / "events.log", "a", encoding="utf-8") as fh:
        fh.write(line * (size // len(line) + 1))


@pytest.mark.parametrize("clear_ago,line_ago", [(timedelta(hours=3), timedelta(hours=2)),
                                                (timedelta(days=2), timedelta(days=1))])
def test_a_limit_written_after_a_clear_stays_after_its_line_leaves_the_tail(home, clear_ago, line_ago):
    _register(home)
    _limit(home["a"], {})  # the runner starts with no limits
    log = home["a"] / "events.log"
    log.write_text("".join(line for line in log.read_text().splitlines(True) if " LIMITED " not in line))
    home["clock"].now = T0 - clear_ago
    routing.clear("Claude second", via="app")
    home["clock"].now = T0
    until = T0 + timedelta(days=5)
    _append(home["a"], f"{routing._iso(T0 - line_ago)} pipe-40 LIMITED lane=second until={routing._iso(until)} "
                       "job=fix-40 (account limit reached); the fix step is retried later")
    _limit(home["a"], {"second": until})
    second = _account(routing.read_view(), SECOND)
    assert (second["state"], second["until"]) == ("limited", "2026-10-19T15:00:00Z")
    _scroll(home["a"], T0 - timedelta(minutes=30))
    view = routing.read_view()
    kept = view["source_cache"]["runners"]["project A"]["events"]["data"]
    assert kept["limited_lines"] == []  # the line has left the tail; global.json still holds the limit
    second = _account(view, SECOND)
    assert (second["state"], second["until"]) == ("limited", "2026-10-19T15:00:00Z")
    assert second["witness"]["cleared"] is False and second["witness"]["at"] == routing._iso(T0 - line_ago)


def test_a_limit_seen_before_a_clear_stays_cleared_after_the_tail_moves_past_the_clear(home):
    _register(home)
    # The fixture's second-account limit is known from global.json alone:
    # dated at the oldest line the tail held when Khipu first read it.
    assert _account(routing.read_view(), SECOND)["state"] == "limited"
    home["clock"].advance(minutes=1)
    routing.clear("Claude second", via="app")
    home["clock"].advance(minutes=1)
    _beat(home["a"], home["clock"].now)
    filler = f"{routing._iso(home['clock'].now - timedelta(seconds=20))} pipe-42 HOLD lane=main " + "y" * 80 + "\n"
    with open(home["a"] / "events.log", "a", encoding="utf-8") as fh:
        fh.write(filler * (600 * 1024 // len(filler) + 1))
    second = _account(routing.read_view(), SECOND)
    assert (second["state"], second["witness"]["cleared"]) == ("open", True)


def test_a_tail_keeps_a_whole_first_line(tmp_path):
    first = "2026-10-14T12:00:00Z pipe-12 LIMITED lane=second until=2026-10-14T17:52:00Z job=fix-12 (limit)\n"
    pad = "2026-10-14T12:00:01Z pipe-12 NOTE " + "z" * 60 + "\n"
    body = first + pad * ((routing_sources.TAIL_BYTES - len(first)) // len(pad))
    body += "w" * (routing_sources.TAIL_BYTES - len(body) - 1) + "\n"
    assert len(body.encode()) == routing_sources.TAIL_BYTES
    path = tmp_path / "events.log"

    def lanes(text: str) -> list[str]:
        path.write_text(text)
        data, _mtime = routing_sources.read_only(path, tail=routing_sources.TAIL_BYTES)
        return [line["lane"] for line in routing_sources._parse_events(data, T0)["limited_lines"]]

    assert lanes(body) == ["second"]  # the whole file, read from its first byte
    assert lanes("earlier line\n" + body) == ["second"]  # the tail starts right after a newline
    # The tail starts inside a line: that cut-off line is left out.
    assert lanes("cut" + body) == []
    assert lanes(body[1:]) == []  # smaller than the tail: the whole file, its first line as written


def test_a_line_numbered_at_the_most_a_line_can_hold_never_loses_the_next_write(home):
    routing.report(account="Claude", state="open", author={"kind": "conversation", "harness": "codex"})
    most = 2 ** 53
    with open(routing.events_file(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "evidence", "id": "edited", "source": "report", "state": "open",
                             "account": "muse|", "written_at": routing._iso(T0), "seq": most}) + "\n")
        fh.write(json.dumps({"type": "rule_add", "id": "o-edited", "n": most, "text": "An edited rule.",
                             "written_at": routing._iso(T0), "seq": most}) + "\n")
    home["clock"].advance(minutes=1)
    routing.report(account="Codex", state="limited", until="1h", author={"kind": "conversation", "harness": "codex"})
    added = routing.add_rule("A rule added after the edited one.", via="app")
    events, bad = routing.read_events()
    assert bad == 0 and len(events) == 5
    assert all(e["seq"] <= most for e in events)
    view = routing.read_view()
    assert _account(view, "codex|")["state"] == "limited"
    assert added["n"] < most and any(r["n"] == added["n"] for r in view["rules"])
    assert any("numbered at the most a line can hold" in w for w in routing.doctor_block()["warnings"])


# ---- fix round 3 ------------------------------------------------------------------------


def _bounces(folder: Path, start: datetime, count: int) -> None:
    """Muse LIMITED lines with the runner's 30-minute guess, one every 14 s."""
    with open(folder / "events.log", "a", encoding="utf-8") as fh:
        for i in range(count):
            at = start + timedelta(seconds=14 * (i + 1))
            fh.write(f"{routing._iso(at)} pipe-17 LIMITED lane=muse until="
                     f"{routing._iso(at + timedelta(minutes=29, seconds=59))} job=recheck-17-auto-r{i} "
                     "(account limit reached); the check step is retried later\n")


@pytest.mark.parametrize("bounces", [150, 250])
def test_a_limit_written_after_a_clear_stands_when_the_line_cap_drops_its_line(home, bounces):
    # R3-1: with more LIMITED lines after it than the cap keeps, the limit's
    # own line was dropped while still in the tail, and the limit was dated
    # at the oldest line of the tail, before the owner's clear: it showed open.
    _register(home)
    _limit(home["a"], {})
    log = home["a"] / "events.log"
    log.write_text("".join(line for line in log.read_text().splitlines(True) if " LIMITED " not in line))
    _append(home["a"], f"{routing._iso(T0 - timedelta(hours=5))} pipe-30 ADDED branch=project-a/early "
                       "stage=built check_lane=codex fix_lanes=second max_checks=3")
    home["clock"].now = T0 - timedelta(hours=3)
    routing.clear("Claude second", via="app")
    home["clock"].now = T0
    until = T0 + timedelta(days=5)
    _append(home["a"], f"{routing._iso(T0 - timedelta(hours=2))} pipe-40 LIMITED lane=second "
                       f"until={routing._iso(until)} job=fix-40 (account limit reached); the fix step is retried later")
    _bounces(home["a"], T0 - timedelta(hours=2), bounces)
    _limit(home["a"], {"second": until})
    assert log.stat().st_size < routing_sources.TAIL_BYTES  # every line is inside the tail
    second = _account(routing.read_view(), SECOND)
    assert (second["state"], second["until"]) == ("limited", routing._iso(until)), second
    assert second["witness"]["at"] == routing._iso(T0 - timedelta(hours=2))
    assert second["witness"]["cleared"] is False


def test_the_runner_tail_and_its_line_caps_are_settings(home):
    # R3-1, decision 13: the tail read and the lines kept from it are
    # settings the owner can raise or remove (0: the whole file, every line).
    _register(home)
    _bounces(home["a"], T0 - timedelta(hours=2), 250)
    cache = routing.read_view()["source_cache"]["runners"]["project A"]["events"]["data"]
    assert len(cache["limited_lines"]) == 200
    _set(home, routing_runner_limited_lines=0)
    home["clock"].advance(seconds=1)
    cache = routing.read_view()["source_cache"]["runners"]["project A"]["events"]["data"]
    assert len(cache["limited_lines"]) == 253  # the fixture's three and the 250 bounces
    _set(home, routing_runner_tail_bytes=300)
    home["clock"].advance(seconds=1)
    cache = routing.read_view()["source_cache"]["runners"]["project A"]["events"]["data"]
    assert len(cache["limited_lines"]) == 1
    _set(home, routing_runner_tail_bytes=0)
    home["clock"].advance(seconds=1)
    cache = routing.read_view()["source_cache"]["runners"]["project A"]["events"]["data"]
    assert len(cache["limited_lines"]) == 253
    status = routing.settings_status()
    assert status["routing_runner_tail_bytes"]["default"] == routing_sources.TAIL_BYTES
    assert status["routing_runner_limited_lines"]["default"] == routing_sources.KEEP_LINES["LIMITED"]


def test_a_low_clock_line_after_one_line_written_ahead_never_lifts_the_lines_after_it(home):
    # R3-2: a line from a clock reading 2001 took the time of the one line
    # written ahead before it, which made two lines agree on that time, so
    # the fold kept the ahead time and lifted every correct line after it.
    clock = home["clock"]
    author = {"kind": "conversation", "harness": "claude_code"}
    routing.report(account="Codex", state="open", author=author)
    clock.advance(minutes=1)
    routing.report(account="Muse", state="open", author=author)
    clock.now = T0 + timedelta(hours=2)  # a clock reading ahead, once
    routing.report(account="Muse", state="open", author=author)
    clock.now = datetime(2001, 1, 1, tzinfo=timezone.utc)
    routing.report(account="Muse", state="open", author=author)
    assert "clock_at" in routing.read_events()[0][-1]
    clock.now = T0 + timedelta(minutes=10)
    routing.report(account="Claude", state="open", author=author)
    clock.now = T0 + timedelta(minutes=10, hours=12, seconds=60)
    claude = _account(routing.read_view(), "claude|")
    assert claude["state"] == "unknown", claude
    warnings = routing.doctor_block()["warnings"]
    assert any("1 line dated well before" in w and "2001-01-01T00:00:00Z" in w for w in warnings), warnings
    assert any("dated well after" in w and routing._iso(T0 + timedelta(hours=2)) in w for w in warnings), warnings
    assert not any("2 lines dated well before" in w for w in warnings), warnings


# ---- fix round 4 ------------------------------------------------------------------------

FAKE_XAI = "xai-" + "Q7fakeKeyNotReal0123456789abcdefGHIJKLmnop"
FAKE_API_VALUE = "plainApiValue-not-a-real-one-4471"
FAKE_PASSWORD = "plainPassword-not-real-9902"


def test_launch_arguments_never_reach_the_record_only_the_facts_the_checks_need(home, capsys):
    # R4-3: each launch argument was redacted on its own, so a secret passed
    # as its own argument (an xAI key, the value after --api-key or
    # --password) was kept unmasked in view.json's source_cache.
    _register(home)
    routing.add_rule("Never a -fast tier.", via="app")
    routing.add_rule("Never the paid xAI API.", via="app")
    config = json.loads((home["a"] / "config.json").read_text())
    config["lanes"]["grok"]["launch"] = ["grok", "--api-key", FAKE_API_VALUE, "--password", FAKE_PASSWORD,
                                         FAKE_XAI, "-m", "grok-4.7"]
    config["lanes"]["grok"]["model"] = "grok-4.7"
    config["lanes"]["muse"]["launch"] = ["bash", "/runner/project-a/launch-muse.sh", "muse-1.0-fast"]
    (home["a"] / "config.json").write_text(json.dumps(config))
    _append(home["a"], f"2026-10-14T14:58:00Z pipe-60 LIMITED lane=codex until=2026-10-14T15:30:00Z job=x "
                       f"--api-key {FAKE_API_VALUE} --password {FAKE_PASSWORD} {FAKE_XAI}")
    _limit(home["a"], {"main": T0 + timedelta(hours=1)})
    glob = json.loads((home["a"] / "state" / "global.json").read_text())
    glob["limited"]["main"]["reason"] = f"--api-key {FAKE_API_VALUE} --password={FAKE_PASSWORD} {FAKE_XAI}"
    (home["a"] / "state" / "global.json").write_text(json.dumps(glob))
    view = routing.read_view()
    kept = view["source_cache"]["runners"]["project A"]["config"]["data"]
    assert all("launch" not in spec for spec in kept["lanes"].values())
    grok, muse = kept["lanes"]["grok"], kept["lanes"]["muse"]
    assert (grok["fast"], grok["model_in_launch"], grok["script"]) == (False, True, "grok")
    assert (muse["fast"], muse["script"]) == (True, "launch-muse.sh")
    checks = routing.doctor_block()["rule_checks"]
    assert any(c["status"] == "broken" and "-fast for its muse lane" in c["text"] for c in checks), checks
    assert any("project A's grok lane: whether it uses the paid xAI API can't be checked: the launch environment "
               "is set inside grok" == c["text"] for c in checks), checks
    texts = [json.dumps(routing.doctor_block())]
    for argv in (_args(json=True), _args(route_cmd="sources", json=True), _args(summary=True)):
        assert routing.run_cli(argv) == 0
        texts.append(capsys.readouterr().out)
    for path in home["data"].rglob("*"):
        if path.is_file():
            texts.append(path.read_bytes().decode("utf-8", "replace"))
    for secret in (FAKE_XAI, FAKE_API_VALUE, FAKE_PASSWORD):
        assert not any(secret in text for text in texts), secret


def test_a_launch_list_kept_by_an_older_view_is_dropped_when_config_json_fails(home):
    # R4-3: a view saved before the fix kept the argv; a config.json that
    # can't be read keeps its last good read, which must not carry it on.
    _register(home)
    routing.read_view()
    view = json.loads(routing.view_file().read_text())
    lanes = view["source_cache"]["runners"]["project A"]["config"]["data"]["lanes"]
    lanes["grok"] = {"kind": "grok", "model": "grok-4.7", "confirm_lane": None,
                     "launch": ["grok", "--api-key", FAKE_API_VALUE, "x-fast"]}
    routing._save_view(view)
    (home["a"] / "config.json").write_text("{not json")
    home["clock"].advance(seconds=1)
    view = routing.read_view()
    grok = view["source_cache"]["runners"]["project A"]["config"]["data"]["lanes"]["grok"]
    assert "launch" not in grok and grok["fast"] is True
    assert FAKE_API_VALUE not in routing.view_file().read_text()


def test_a_write_from_a_clock_behind_the_record_never_moves_the_version_on_read(home):
    # R4-1: a write that copied the record's time (clock_at set)
    # refolded and saved the view at that copied time, while a read folds at
    # the clock, so every write and read pair flipped the view and moved the
    # version.
    clock = home["clock"]
    author = {"kind": "conversation", "harness": "claude_code"}
    routing.report(account="Claude", state="limited", until="30m", author=author)
    clock.advance(hours=1)  # the limit has ended at the record's newest time, not at the clock below
    routing.report(account="Codex", state="open", author=author)
    clock.now = datetime(2001, 1, 1, tzinfo=timezone.utc)
    for _ in range(2):
        written = routing.report(account="Muse", state="open", author=author)
        assert "clock_at" in routing.read_events()[0][-1]
        versions = {written["version"]}
        for _ in range(3):
            clock.advance(seconds=1)
            versions.add(routing.read_view()["version"])
        assert len(versions) == 1, versions
        assert json.loads(routing.view_file().read_text())["folded_at"].startswith("2001-")


def test_the_first_bound_counts_every_line_in_the_tail(home):
    # R4-2: ``first`` (which dates a global.json limit with no line in the
    # tail) was taken over ADDED, LIMITED, LAUNCH and HOLD lines only, so a
    # tail of other lines fell back to global.json's modification time.
    _register(home)
    oldest = T0 - timedelta(hours=3)
    (home["a"] / "events.log").write_text(
        f"{routing._iso(oldest)} pipe-70 VERDICT check 1 on codex said CLEAR\n"
        f"{routing._iso(oldest + timedelta(minutes=5))} pipe-70 DONE fix round 1\n")
    _limit(home["a"], {"main": T0 + timedelta(hours=1)})
    cache = routing.read_view()["source_cache"]["runners"]["project A"]
    assert cache["events"]["data"]["first"] == routing._iso(oldest)
    assert cache["sightings"]["main"]["at"] == routing._iso(oldest)
    assert cache["sightings"]["main"]["line"] is False


def test_the_word_after_a_secret_flag_is_masked_whatever_it_looks_like():
    # R5-1: a value starting with a dash read as the next flag, and a bare
    # --key was not a secret flag at all.
    for line in ("job --api-key -Kq3fakeSecretValue9", "job --token -abcFAKEtoken123",
                 "job --key fakeKeyValue12345", "job --password=-Hunter2fake"):
        masked = routing_sources._redacted(line)
        assert masked.startswith("job ") and not any(
            v in masked for v in ("Kq3fake", "abcFAKE", "fakeKeyValue", "Hunter2fake")), masked


def test_a_source_cache_from_older_masking_is_dropped_not_carried(home):
    # R5-2: a file that can't be read keeps its last good read; a read kept
    # under older masking must not carry its text on.
    _register(home)
    routing.read_view()
    view = json.loads(routing.view_file().read_text())
    cache = view["source_cache"]
    cache.pop("redaction", None)
    cache["runners"]["project A"]["events"]["data"]["limited_lines"] = [
        {"text": "job=z xai-OLDVIEWfakeKey0123456789abcdefABCD"}]
    routing._save_view(view)
    (home["a"] / "events.log").unlink()
    home["clock"].advance(seconds=5)
    routing.read_view()
    assert "OLDVIEWfakeKey" not in routing.view_file().read_text()
    kept = json.loads(routing.view_file().read_text())["source_cache"]
    assert kept["redaction"] == routing_sources.REDACTION



# ---- fix round 6: only parsed fields and checked names, never a source's text -------------
#
# Each test below reproduces one class of leak found against round 5 with
# fake values, and asserts the fake secret reaches nothing Khipu stores
# (its data folder) and none of khipu route, route --json, route --summary,
# route sources, route sources --json and doctor's block.


def _nowhere(home, capsys, *secrets: str) -> None:
    """The source files themselves (the runner folders, and the usage cache
    a test writes as input) are not Khipu's: everything else is checked."""
    source = home["data"] / "state" / "usage.json"
    texts = [json.dumps(routing.doctor_block())]
    for argv in (_args(), _args(json=True), _args(summary=True), _args(route_cmd="sources"),
                 _args(route_cmd="sources", json=True)):
        assert routing.run_cli(argv) == 0
        texts.append(capsys.readouterr().out)
    for path in home["data"].rglob("*"):
        if path.is_file() and path != source:
            texts.append(path.read_bytes().decode("utf-8", "replace"))
    for secret in secrets:
        assert not any(secret in text for text in texts), secret


def _reason(home, lane: str, until: datetime, reason) -> None:
    _limit(home["a"], {lane: until})
    glob = json.loads((home["a"] / "state" / "global.json").read_text())
    glob["limited"][lane]["reason"] = reason
    (home["a"] / "state" / "global.json").write_text(json.dumps(glob))


def _limited(home, rest: str) -> None:
    _append(home["a"], f"2026-10-14T14:58:00Z pipe-61 LIMITED lane=codex until=2026-10-14T15:30:00Z job=x {rest}")


def test_r6_flag_spellings_the_mask_missed_never_reach_the_record(home, capsys):
    # Class 1: -k, -p, --bearer, --pass and --pass= were no secret flags.
    _register(home)
    fakes = ["kFlagFakeValueOne", "pFlagFakeValueTwo", "bearerFakeValueThree", "passFakeValueFour",
             "passEqFakeValueFive"]
    rest = f"-k {fakes[0]} -p {fakes[1]} --bearer {fakes[2]} --pass {fakes[3]} --pass={fakes[4]}"
    _limited(home, rest)
    _reason(home, "main", T0 + timedelta(hours=1), rest)
    view = routing.read_view()
    assert _account(view, "codex|")["state"] == "limited"
    _nowhere(home, capsys, *fakes)


def test_r6_a_flag_followed_by_a_quote_or_a_list_never_reaches_the_record(home, capsys):
    # Class 2: argv=["--api-key", "X"] in a line, and a reason written as a
    # list (which was turned into text).
    _register(home)
    _limited(home, 'argv=["--api-key", "listArgFakeValueSix"]')
    _reason(home, "main", T0 + timedelta(hours=1), ["--api-key", "listReasonFakeSeven"])
    routing.read_view()
    _nowhere(home, capsys, "listArgFakeValueSix", "listReasonFakeSeven")


def test_r6_a_quoted_value_of_several_words_never_reaches_the_record(home, capsys):
    # Class 3: only the first word after the flag was masked.
    _register(home)
    rest = '--token "a quotedFakeEight" --password=\'w quotedFakeNine\' password="w1 quotedFakeTen"'
    _limited(home, rest)
    _reason(home, "main", T0 + timedelta(hours=1), rest)
    routing.read_view()
    _nowhere(home, capsys, "quotedFakeEight", "quotedFakeNine", "quotedFakeTen")


def test_r6_shapes_redaction_does_not_know_never_reach_the_record(home, capsys):
    # Class 4: ?key=, bare key=, ?sig=, "Authorization: Token", a short
    # bearer value or one holding "!", and the table's updated: line.
    _register(home)
    fakes = ["queryKeyFakeEleven", "bareKeyFakeTwelve", "sigFakeThirteen", "tokenHdrFakeFourteen", "shrtFk15",
             "bang!FakeSixteen", "tableKeyFakeSeventeen"]
    rest = (f"url=https://h.test/x?key={fakes[0]} key={fakes[1]} https://h.test/y?sig={fakes[2]} "
            f"Authorization: Token {fakes[3]} Bearer {fakes[4]} Bearer {fakes[5]}")
    _limited(home, rest)
    _reason(home, "main", T0 + timedelta(hours=1), rest)
    table = home["tmp"] / "routing-table.md"
    _table(table, "lane order", f"2026-10-14 13:48Z by x https://h.test/?key={fakes[6]}")
    _set(home, routing_table=str(table))
    view = routing.read_view()
    assert view["sources"]["table"]["stamped_at"] == "2026-10-14T13:48:00Z"
    _nowhere(home, capsys, *fakes)


def test_r6_a_usage_row_for_an_account_not_configured_is_skipped(home, capsys):
    # Class 5: a row keyed by a folder that is no configured account became
    # an account, with its raw key and folder.
    _register(home)
    secret = "sk-ant-api03-Fake18" + "z" * 30
    _usage(home, {f"codex|/x/{secret}": _row(T0 - timedelta(minutes=1), [
        {"name": "five_hour", "used_pct": 100.0, "resets_at": routing._iso(T0 + timedelta(hours=2))}])})
    view = routing.read_view()
    assert all(secret not in a["key"] for a in view["accounts"])
    assert view["sources"]["usage"]["accounts"] == 0
    _nowhere(home, capsys, secret, "Fake18")


def test_r6_a_secret_split_by_whitespace_keeps_neither_half(home, capsys):
    # Class 6: the first half was masked and the second half kept.
    _register(home)
    rest = "token=sk-ant-api03-" + "A" * 24 + " secondHalfFakeNineteen"
    _limited(home, rest)
    _reason(home, "main", T0 + timedelta(hours=1), rest)
    routing.read_view()
    _nowhere(home, capsys, "secondHalfFakeNineteen", "A" * 24)


def test_r6_a_name_field_that_fails_the_shape_check_is_withheld(home, capsys):
    # Every name a runner file gives (lane, pipeline, model, kind) is kept
    # only in the strict shape and when it doesn't look like a key.
    _register(home)
    secret = "xai-" + "NameFakeKey20abcdefghijklmnop"
    _append(home["a"], f"2026-10-14T14:58:00Z {secret} LIMITED lane={secret} until=2026-10-14T15:30:00Z")
    config = json.loads((home["a"] / "config.json").read_text())
    config["lanes"]["grok"]["model"] = secret
    config["lanes"]["grok"]["kind"] = ["grok", secret]
    config["lanes"][secret] = {"kind": "grok", "model": "grok-4.7"}
    (home["a"] / "config.json").write_text(json.dumps(config))
    view = routing.read_view()
    kept = view["source_cache"]["runners"]["project A"]["config"]["data"]["lanes"]
    assert kept["grok"]["model"] == routing_sources.WITHHELD and kept["grok"]["kind"] is None
    assert routing_sources.WITHHELD in kept
    for name in ("muse", "second-sonnet", "gpt-6.1-sol", "launch-muse.sh", "pipe-12"):
        assert routing_sources.safe_name(name) == name
    for name in ("sk-ant-x", "a b", "AIzaFake", "x" * 65, "abc123def456ghi789jkl012mno", "lane=x"):
        assert routing_sources.safe_name(name) == routing_sources.WITHHELD, name
    assert routing_sources.safe_name(7) is None and routing_sources.safe_name(["a"]) is None
    _nowhere(home, capsys, secret, "NameFakeKey20")


def test_names_shaped_like_other_vendors_keys_are_withheld():
    # Attack L1: a GitLab token, a UUID-format key and a 16-23 character run
    # of letters and digits passed the name check.
    for name in ("glpat" + "-ZQXabcdefghij0123456", "zqx4e8a0-e29b-41d4-a716-446655440000",
                 "zqx0a1b2c3d4e5f6a7b8c9d", "hf_FakeTokenValue", "npm_FakeTokenValue"):
        assert routing_sources.safe_name(name) == routing_sources.WITHHELD, name
    for name in ("claude-opus-5-5", "muse-wonder-full-1.0", "gemini-embedding-2", "repo-familiarization-5e5cf7",
                 "secondary-sonnet", "grokcheck"):
        assert routing_sources.safe_name(name) == name, name


def test_a_window_with_no_number_neither_crashes_nor_miscounts_the_accounts(home):
    # Attack R1: the account counter was overwritten by each window's
    # percentage, and a window with no number made the next row raise.
    _usage(home, {
        "claude|": _row(T0 - timedelta(minutes=1), [{"name": "five_hour", "used_pct": 40.0},
                                                    {"name": "credits", "used_pct": None}]),
        "codex|": _row(T0 - timedelta(minutes=1), [{"name": "five_hour", "used_pct": 10.0}]),
    })
    routing.read_view()
    _, status = routing_sources.usage_evidence()
    assert status["accounts"] == 2


def test_a_broken_rule_keeps_the_time_khipu_first_saw_it(home, capsys):
    """Fix round 1 (F4): a check judged on a runner's settings is dated by
    when Khipu first read the file as it reads now, not by the latest
    refresh; a changed file is a new sighting. Each check names its runner
    and file so the app can say "in its settings file"."""
    _register(home)
    routing.add_rule("Never a -fast tier.", via="app")
    config = json.loads((home["a"] / "config.json").read_text())
    config["lanes"]["codex"]["model"] = "gpt-6.1-sol-fast"
    (home["a"] / "config.json").write_text(json.dumps(config))

    def broken() -> dict:
        assert routing.run_cli(_args(json=True)) == 0
        out = json.loads(capsys.readouterr().out)
        found = [c for c in out["rule_checks"] if c["status"] == "broken"]
        assert len(found) == 1
        return found[0]

    first = broken()
    assert first["at"] == routing._iso(T0)
    assert first["runner"] == "project A" and first["file"] == "settings"
    # The runner keeps running: its heartbeat moves, the settings don't.
    for minutes in (20, 40):
        home["clock"].now = T0 + timedelta(minutes=minutes)
        _beat(home["a"], home["clock"].now)
        assert broken()["at"] == routing._iso(T0)
    # The settings change (still broken): Khipu saw this version later.
    config["lanes"]["codex"]["model"] = "gpt-6.1-astra-fast"
    (home["a"] / "config.json").write_text(json.dumps(config))
    home["clock"].now = T0 + timedelta(minutes=50)
    _beat(home["a"], home["clock"].now)
    assert broken()["at"] == routing._iso(T0 + timedelta(minutes=50))
