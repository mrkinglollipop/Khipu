"""Routing truth, phase 3b (docs/plans/2026-10-14-routing-truth.md): what
changed in the routing record, before every prompt, on both of the per-prompt
hook's paths: the recall daemon (``recall_daemon.RecallService`` and the real
``bin/khipu-recall-client.py``) and the one-shot path
(``recall_prompt.prompt_recall_main``).

Hermetic: every test has its own data dir and a short capture home (the
daemon's socket lives there), a fake clock, and the search stubbed (no
database, no network), so the hook's output can be compared byte for byte
with what it printed before routing existed.
"""
from __future__ import annotations

import fcntl
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from khipu import promote, recall_daemon, recall_prompt, routing

T0 = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
ACCOUNTS = [{"label": "Claude", "harness": "claude"},
            {"label": "Claude second", "harness": "claude", "home": "/logins/claude-second"},
            {"label": "Codex", "harness": "codex"}]
ME = {"kind": "conversation", "harness": "claude_code", "project": "project-a", "session": "s-one"}
SESSION = "11111111-2222-3333-4444-555555555555"
PROMPT = "what did we decide about the recall hook"
HITS = [{"kind": "episode", "id": "41", "score": 0.9, "snippet": "kept the hook fail-open",
         "label": "kept the hook fail-open"}]
FULL = "## Routing at "
CHANGES = "## Routing changes at "
CLIENT = Path(__file__).resolve().parents[1] / "bin" / "khipu-recall-client.py"


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
    # A generous routing budget, so a loaded test machine never makes a content test flaky;
    # the latency tests set their own.
    (data / "config.json").write_text(json.dumps({"usage_accounts": ACCOUNTS,
                                                  "routing_prompt_budget_ms": 1000}))
    capture = Path(tempfile.mkdtemp(prefix="krp-", dir="/tmp"))  # AF_UNIX paths are short
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(capture))
    monkeypatch.delenv("KHIPU_ISOLATED_DIR", raising=False)
    monkeypatch.delenv("ROUTING_NOW_OFF", raising=False)
    monkeypatch.delenv("KHIPU_FEATURE_ROUTING_PUSH", raising=False)
    monkeypatch.setattr(routing, "ROUTING_NOW_DISABLE_FILE", str(tmp_path / "routing_now_disable"))
    clock = Clock(T0)
    monkeypatch.setattr(routing, "_now", clock)
    search = {"hits": list(HITS), "delay": 0.0}

    def fake_search(*_a, **_k):
        if search["delay"]:
            time.sleep(search["delay"])
        return {"hits": [dict(h) for h in search["hits"]], "legs": ["lexical"], "degraded": None}

    monkeypatch.setattr(recall_prompt, "_search_hits", fake_search)
    monkeypatch.setattr(recall_prompt, "_deliverable_context_line", lambda *_a, **_k: "")
    yield {"data": data, "clock": clock, "tmp": tmp_path, "capture": capture, "search": search}
    shutil.rmtree(capture, ignore_errors=True)


def _set(home, **values) -> None:
    path = home["data"] / "config.json"
    config = json.loads(path.read_text())
    config.update(values)
    path.write_text(json.dumps(config))


def _push_on(monkeypatch) -> None:
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "1")


def _record() -> None:
    routing.report(account="Codex", state="limited", until="1h", author=ME)
    routing.report(account="Claude", state="open", author=ME)


def _payload(prompt: str = PROMPT, session: str | None = SESSION) -> dict:
    out = {"prompt": prompt, "cwd": "/work/project-a", "hook_event_name": "UserPromptSubmit"}
    if session is not None:
        out["session_id"] = session
    return out


def _cold(monkeypatch, prompt: str = PROMPT, session: str | None = SESSION) -> str:
    """The one-shot path as the launcher runs it: payload on stdin, JSON on
    stdout. Returns stdout exactly."""
    monkeypatch.setattr(recall_prompt.sys, "stdin", io.StringIO(json.dumps(_payload(prompt, session))))
    buf = io.StringIO()
    with redirect_stdout(buf):
        recall_prompt.prompt_recall_main(shape="claude")
    return buf.getvalue()


def _ctx(out: str) -> str:
    data = json.loads(out)
    return data["hookSpecificOutput"]["additionalContext"] if data else ""


def _routing_part(out: str) -> str:
    ctx = _ctx(out)
    at = ctx.find("## Routing")
    return ctx[at:] if at >= 0 else ""


def _recall_part(out: str) -> str:
    ctx = _ctx(out)
    at = ctx.find("## Routing")
    return ctx if at < 0 else ctx[:at].rstrip("\n")


def _log_lines() -> list[str]:
    path = recall_prompt._log_path()
    return path.read_text().splitlines() if path.exists() else []


def _last_log() -> str:
    return _log_lines()[-1]


def _marker() -> dict:
    return recall_prompt.routing_marker(SESSION)


def _seen_now() -> dict:
    """The session saw the current view in full (as its start hook leaves it)."""
    view = routing.read_view()
    assert recall_prompt.mark_routing_seen(SESSION, version=view["version"], epoch=view["epoch"],
                                           full_at=routing._iso(routing._now()))
    return _marker()


def _without_routing(monkeypatch, run) -> str:
    """What ``run`` prints with the push off, for a byte-for-byte comparison."""
    with monkeypatch.context() as m:
        m.delenv("KHIPU_FEATURE_ROUTING_PUSH", raising=False)
        return run()


# ---- the daemon path -------------------------------------------------------------------------


@pytest.fixture
def daemon(home):
    # Forks from this process (the seam), so a test's patched clock and fold
    # reach the read; ``helper_daemon`` below runs the real routing helper.
    svc = recall_daemon.RecallService(home["capture"] / "recall.sock", stamp_fn=lambda: "fixed",
                                      idle_check=False, routing_forkserver=False)
    assert svc.bind()
    thread = threading.Thread(target=svc.run, daemon=True)
    thread.start()
    yield svc
    svc.stop()
    thread.join(5)
    svc.close()


def _client(home, prompt: str = PROMPT, env: dict | None = None) -> tuple[int, str]:
    """The real client, started as the launcher starts it (``env``: more of
    the hook's own environment)."""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ["HOME"],
           "KHIPU_CAPTURE_HOME": str(home["capture"]), **(env or {})}
    proc = subprocess.run([sys.executable, "-S", "-E", "-B", str(CLIENT), "claude"],
                          input=json.dumps(_payload(prompt)).encode(), env=env, capture_output=True, timeout=10)
    return proc.returncode, proc.stdout.decode()


def _no_ack(home, prompt: str = PROMPT) -> dict:
    """A client that reads the answer and hangs up without confirming it."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(str(home["capture"] / "recall.sock"))
    try:
        req = {"op": "recall", "raw": json.dumps(_payload(prompt)), "shape": "claude", "protocol": 1,
               "deadline": time.monotonic() + 1.6}
        sock.sendall(json.dumps(req).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.split(b"\n", 1)[0])
    finally:
        sock.close()


def _wait_log(count: int) -> str:
    """The daemon logs after the client's ack, on its own thread."""
    for _ in range(250):
        lines = _log_lines()
        if len(lines) >= count:
            return lines[count - 1]
        time.sleep(0.02)
    raise AssertionError(f"no log line {count}: {_log_lines()}")


# ---- the switches ------------------------------------------------------------------------------


def test_with_the_push_off_the_output_and_the_log_line_are_as_before(home, monkeypatch):
    _record()
    routing.add_rule("Never a -fast tier.", via="app")
    out = _cold(monkeypatch)
    assert "## Prior work" in _ctx(out) and "## Routing" not in out
    assert " routing=" not in _last_log()
    _push_on(monkeypatch)
    for how in ("env", "file", "setting"):
        recall_prompt._save_recent_batches(SESSION, [])  # the same hits again are not a repeat
        if how == "env":
            monkeypatch.setenv("ROUTING_NOW_OFF", "1")
        elif how == "file":
            monkeypatch.delenv("ROUTING_NOW_OFF")
            Path(routing.ROUTING_NOW_DISABLE_FILE).write_text("")
        else:
            Path(routing.ROUTING_NOW_DISABLE_FILE).unlink()
            _set(home, routing_push_prompt=False)
        assert _cold(monkeypatch) == out, how
        assert " routing=" not in _last_log()
    assert _marker() == {}


def test_the_routing_field_ends_the_log_line_and_every_reader_still_parses_it(home, monkeypatch):
    _record()
    _push_on(monkeypatch)
    _cold(monkeypatch)
    line = _last_log()
    assert line.endswith(" routing=full"), line
    gate = promote._SUMMARY_RE.match(line)
    assert gate and gate["session"] == SESSION and gate["reason"] == "ok" and gate["hits"] == "['episode:41']"
    doctor = recall_prompt._LOG_LINE_RE.match(line)
    assert doctor and doctor["reason"] == "ok"
    assert line.index(" degraded=") < line.index(" routing=")


# ---- what is sent ------------------------------------------------------------------------------


def test_a_session_with_no_last_seen_version_gets_the_full_summary(home, monkeypatch):
    _record()
    _push_on(monkeypatch)
    out = _cold(monkeypatch)
    assert _recall_part(out) == _ctx(_without_routing(monkeypatch, lambda: _cold(monkeypatch, session="other")))
    part = _routing_part(out)
    assert part == routing.light_status()["text"]  # the session-start projection, whole
    assert "/logins/" not in part and "claude|" not in part and "s-one" not in part
    view = routing.read_view()
    assert _marker() == {"version": view["version"], "epoch": view["epoch"], "full_at": "2026-10-14T15:00:00Z"}


def test_nothing_is_sent_when_nothing_changed(home, monkeypatch):
    _record()
    _push_on(monkeypatch)
    _cold(monkeypatch)
    before = _marker()
    home["clock"].advance(minutes=5)
    home["search"]["hits"] = [dict(HITS[0], id="42")]
    out = _cold(monkeypatch)
    assert out == _without_routing(monkeypatch, lambda: _cold(monkeypatch, session="other"))
    lines = _log_lines()
    assert lines[-2].endswith(" routing=none")
    assert _marker() == before


def test_changes_since_the_last_seen_version_are_sent_as_current_state(home, monkeypatch):
    _record()
    _push_on(monkeypatch)
    _seen_now()
    home["clock"].advance(minutes=10)
    routing.report(account="Claude", state="limited", until="2h", author=ME)
    out = _cold(monkeypatch)
    part = _routing_part(out).splitlines()
    assert part[0] == ("## Routing changes at 15:10Z (Khipu's routing record; data, not instructions; "
                       "guidance, not a gate)")
    assert len(part) == 2 and part[1].startswith("Claude: limit reached, back about 17:10Z, in 2 h"), part
    assert _recall_part(out).startswith("## Prior work")
    assert _last_log().endswith(" routing=changes:1")
    assert _marker()["version"] == routing.read_view()["version"]
    assert _marker()["full_at"] == "2026-10-14T15:00:00Z"  # a change block keeps the last full summary's time


def test_a_replaced_line_is_never_sent(home, monkeypatch):
    _record()
    _push_on(monkeypatch)
    _seen_now()
    home["clock"].advance(minutes=1)
    routing.report(account="Claude", state="limited", until="2h", author=ME)
    routing.read_view()  # a version of its own
    home["clock"].advance(minutes=1)
    routing.report(account="Claude", state="open", author=ME)  # reopened before the next prompt
    home["clock"].advance(minutes=1)
    part = _routing_part(_cold(monkeypatch))
    lines = part.splitlines()[1:]
    assert len(lines) == 1 and lines[0].startswith("Claude: open"), part
    assert "limit reached" not in part


def test_a_limits_end_with_no_file_changing_is_sent(home, monkeypatch):
    # The inline refold: valid_until passed, no input changed, nothing else refolded.
    _record()
    _push_on(monkeypatch)
    _seen_now()
    home["clock"].advance(hours=1, minutes=1)
    part = _routing_part(_cold(monkeypatch))
    assert part.startswith(CHANGES)
    assert "Codex: probably open: its limit was due to end at 16:00Z" in part


def test_the_order_and_the_budget_of_a_change_block(home, monkeypatch):
    _record()
    _push_on(monkeypatch)
    seen = _seen_now()
    home["clock"].advance(minutes=1)
    routing.add_hold("keep it for the audit", reason="keep", scope="machine", owner=True, via="app")
    routing.report(account="Codex", state="open", author=ME)
    routing.report(account="Claude second", state="limited", until="3h", author=ME)
    routing.add_rule("Never the paid xAI API.", via="app")
    lines = _routing_part(_cold(monkeypatch)).splitlines()[1:]
    assert lines[0] == "Standing rule 1. Never the paid xAI API."
    assert lines[1].startswith("Claude second: limit reached")
    assert lines[2].startswith("Codex: open")
    assert lines[3].startswith("Hold on")
    assert len(lines) == 4
    # Over the budget: whole lines from the bottom, then where to read the rest.
    recall_prompt.mark_routing_seen(SESSION, version=seen["version"], epoch=seen["epoch"],
                                    full_at=seen["full_at"])
    _set(home, routing_prompt_chars=150)
    lines = _routing_part(_cold(monkeypatch)).splitlines()[1:]
    assert lines[0] == "Standing rule 1. Never the paid xAI API."
    assert lines[1].startswith("Claude second: limit reached")
    assert lines[-1] == f"2 more changes: `khipu_route` since {seen['version']}"
    assert sum(len(x) + 1 for x in lines[1:]) <= 150


def test_a_changed_rule_longer_than_the_budget_is_sent_whole(home, monkeypatch):
    _record()
    _push_on(monkeypatch)
    _seen_now()
    _set(home, routing_prompt_chars=60, routing_line_chars=40)
    rule = ("Every Grok result that is acted on gets a re-check from a different model family, "
            "and the re-check looks wider than the list Grok gave. ") * 4
    routing.add_rule(rule, via="app")
    note = "Codex first for builds today; Grok for read-only surveys; second Claude for reviews. " * 3
    routing.set_note(note, via="app")
    home["clock"].advance(minutes=1)
    routing.report(account="Claude", state="limited", until="2h", author=ME)
    lines = _routing_part(_cold(monkeypatch)).splitlines()
    assert f"Standing rule 1. {rule.strip()}" in lines  # whole: not clipped at 40, not capped at 60
    assert any(line.startswith("Note from the owner") and line.endswith(note.strip()) for line in lines)
    claude = next(line for line in lines if line.startswith("Claude"))
    assert len(claude) <= 40


@pytest.mark.parametrize("why", ["behind", "old", "epoch"])
def test_the_fallback_to_the_full_summary(home, monkeypatch, why):
    _record()
    _push_on(monkeypatch)
    seen = _seen_now()
    if why == "behind":
        for i in range(21):  # 21 versions: more than routing_resend_after_versions (20)
            routing.add_rule(f"Rule number {i}.", via="app")
            routing.read_view()
    elif why == "old":
        home["clock"].advance(hours=6, minutes=1)
        routing.report(account="Claude", state="open", author=ME)
    else:
        recall_prompt.mark_routing_seen(SESSION, version=seen["version"], epoch="0ther0ne",
                                        full_at=seen["full_at"])
    out = _cold(monkeypatch)
    assert _routing_part(out).startswith(FULL), _routing_part(out)
    assert _last_log().endswith(" routing=full")
    assert _marker()["full_at"] == routing._iso(home["clock"]())
    if why == "behind":
        # Twenty versions behind is still a change block.
        for i in range(20):
            routing.add_rule(f"Another rule {i}.", via="app")
            routing.read_view()
        home["search"]["hits"] = [dict(HITS[0], id="42")]
        assert _routing_part(_cold(monkeypatch)).startswith(CHANGES)


# ---- whatever the search did ---------------------------------------------------------------------


@pytest.mark.parametrize("search", ["gated", "dedup", "timeout", "empty"])
def test_the_routing_lines_arrive_whatever_the_search_did(home, monkeypatch, search):
    _record()
    _push_on(monkeypatch)
    prompt = PROMPT
    if search == "gated":
        prompt = "ok"
    elif search == "dedup":
        _without_routing(monkeypatch, lambda: _cold(monkeypatch))  # the same hits, delivered once
    elif search == "timeout":
        monkeypatch.setattr(recall_prompt, "TIMEOUT_S", 0.2)
        home["search"]["delay"] = 1.0
    else:
        home["search"]["hits"] = []
    out = _cold(monkeypatch, prompt=prompt)
    assert _recall_part(out) == ""
    assert _routing_part(out).startswith(FULL)
    reason = {"gated": "no content tokens", "dedup": "dedup", "timeout": "timeout>0.2s", "empty": "ok"}
    assert f" reason={reason[search]} " in _last_log()
    assert _last_log().endswith(" routing=full")
    assert _marker()["version"] == routing.read_view()["version"]


def test_a_search_timeout_on_the_daemon_path_still_delivers_the_routing_lines(home, monkeypatch, daemon):
    _record()
    _push_on(monkeypatch)
    monkeypatch.setattr(recall_prompt, "TIMEOUT_S", 0.3)  # too late for the client to try the one-shot path
    home["search"]["delay"] = 1.0
    code, out = _client(home)
    assert code == 0
    assert _recall_part(out) == "" and _routing_part(out).startswith(FULL)
    line = _wait_log(1)
    assert " reason=timeout>0.3s " in line and line.endswith(" routing=full")
    assert _marker()["version"] == routing.read_view()["version"]


def test_a_quick_search_error_on_the_daemon_path_leaves_the_one_shot_retry_alone(home, monkeypatch, daemon):
    _record()
    _push_on(monkeypatch)

    def broken(*_a, **_k):
        raise RuntimeError("hub down")

    monkeypatch.setattr(recall_prompt, "_search_hits", broken)
    code, _out = _client(home)
    assert code == 3  # the client still falls back to the one-shot path, as before routing
    assert _wait_log(1).endswith(" routing=unsent:full")
    assert _marker() == {}  # the one-shot path sends it


# ---- confirmed delivery ------------------------------------------------------------------------------


def test_an_undelivered_block_is_sent_again_on_the_one_shot_path(home, monkeypatch):
    _record()
    _push_on(monkeypatch)

    def broken_print(*_a, **_k):
        raise OSError("the harness closed stdout")

    with monkeypatch.context() as m:
        m.setattr("builtins.print", broken_print)
        _cold(monkeypatch)
    assert _last_log().endswith(" routing=unsent:full")
    assert _marker() == {}
    assert _routing_part(_cold(monkeypatch)).startswith(FULL)
    assert _marker()["version"] == routing.read_view()["version"]


def test_the_seen_marker_works_on_the_daemon_path(home, monkeypatch, daemon):
    _record()
    _push_on(monkeypatch)
    code, out = _client(home)
    assert code == 0 and _routing_part(out).startswith(FULL)
    assert _wait_log(1).endswith(" routing=full")
    first = _marker()
    assert first["version"] == routing.read_view()["version"]
    # Nothing changed: nothing sent, and the recall part as before.
    home["search"]["hits"] = [dict(HITS[0], id="42")]
    code, out = _client(home)
    assert code == 0 and "## Routing" not in out and "## Prior work" in out
    assert _wait_log(2).endswith(" routing=none")
    # A change: only the change, and the marker moves.
    home["clock"].advance(minutes=3)
    routing.report(account="Claude", state="limited", until="1h", author=ME)
    home["search"]["hits"] = [dict(HITS[0], id="43")]
    code, out = _client(home)
    assert _routing_part(out).startswith(CHANGES) and "Claude: limit reached" in out
    assert _wait_log(3).endswith(" routing=changes:1")
    assert _marker()["version"] == routing.read_view()["version"] > first["version"]
    assert _marker()["full_at"] == first["full_at"]


def test_an_unconfirmed_block_on_the_daemon_path_is_sent_again(home, monkeypatch, daemon):
    _record()
    _push_on(monkeypatch)
    answer = _no_ack(home)
    assert answer["ok"] is True
    assert answer["output"]["hookSpecificOutput"]["additionalContext"].count(FULL) == 1
    assert _wait_log(1).endswith(" routing=unsent:full")
    assert _marker() == {}
    home["search"]["hits"] = [dict(HITS[0], id="42")]
    _code, out = _client(home)
    assert _routing_part(out).startswith(FULL)
    assert _wait_log(2).endswith(" routing=full")
    assert _marker()["version"] == routing.read_view()["version"]


# ---- fails open, and never slows recall -------------------------------------------------------------


def test_an_unreadable_record_fails_open_with_recall_unchanged(home, monkeypatch):
    _record()
    _push_on(monkeypatch)
    before = _without_routing(monkeypatch, lambda: _cold(monkeypatch, session="other"))
    folder = routing.routing_dir()
    folder.chmod(0)
    try:
        out = _cold(monkeypatch)
    finally:
        folder.chmod(0o700)
    assert out == before
    assert " routing=skipped:" in _last_log()
    assert _marker() == {}
    # Readable again: the block arrives.
    home["search"]["hits"] = [dict(HITS[0], id="42")]
    assert _routing_part(_cold(monkeypatch)).startswith(FULL)


def test_a_damaged_view_is_refolded_from_the_log(home, monkeypatch):
    _record()
    _push_on(monkeypatch)
    routing.view_file().write_bytes(b"\x00\xff{not json" * 1000)
    part = _routing_part(_cold(monkeypatch))
    assert part.startswith(FULL) and "Codex: limit reached" in part


@pytest.mark.parametrize("name", ["prompt_push", "_prompt_view", "summary", "push_off"])
def test_a_routing_failure_never_breaks_the_hook(home, monkeypatch, name):
    _record()
    _push_on(monkeypatch)
    before = _without_routing(monkeypatch, lambda: _cold(monkeypatch, session="other"))

    def boom(*_a, **_k):
        raise RuntimeError("/private/path in an exception")

    monkeypatch.setattr(routing, name, boom)
    assert _cold(monkeypatch) == before
    assert "/private/path" not in "\n".join(_log_lines())
    assert _marker() == {}


def test_no_session_id_sends_nothing(home, monkeypatch):
    _record()
    _push_on(monkeypatch)
    out = _cold(monkeypatch, session=None)
    assert "## Routing" not in out
    assert _last_log().endswith(" routing=skipped:no-session")


def _cpu_bound_fold(seconds: float):
    real_fold = routing.fold

    def fold(*a, **k):
        end = time.monotonic() + seconds
        while time.monotonic() < end:  # pure Python, as a fold of a big log is
            pass
        return real_fold(*a, **k)

    return fold


def _damage(home, monkeypatch, damage: str) -> None:
    """A record that takes seconds to read: a log whose fold is CPU-bound for
    3 s, or a 40 MB view.json Khipu did not save (no matching digest), which
    is parsed whole before it is refolded."""
    routing.view_file().unlink()
    _set(home, routing_prompt_budget_ms=100)
    if damage == "huge-log":
        monkeypatch.setattr(routing, "fold", _cpu_bound_fold(3.0))
        return
    row = json.dumps({"key": "x" * 80, "state": "open"})
    count = 40 * 1024 * 1024 // (len(row) + 1)
    routing.view_file().write_text('{"schema": 1, "accounts": [' + ",".join([row] * count) + "]}")


def _wait_saved() -> None:
    """The child that was folding finishes and saves the view."""
    for _ in range(200):
        if routing._load_view() is not None:
            return
        time.sleep(0.1)
    raise AssertionError("the late child never saved the view")


@pytest.mark.parametrize("damage", ["huge-log", "huge-view"])
def test_a_huge_record_never_slows_the_one_shot_path(home, monkeypatch, damage):
    _record()
    _damage(home, monkeypatch, damage)
    started = time.monotonic()
    before = _cold(monkeypatch, session="other")
    baseline = time.monotonic() - started
    _push_on(monkeypatch)
    started = time.monotonic()
    out = _cold(monkeypatch)
    took = time.monotonic() - started
    assert out == before  # late: nothing added, and recall's part byte for byte
    assert took < baseline + 0.1 + 0.4, (took, baseline)
    assert _last_log().endswith(" routing=skipped:budget")
    assert _marker() == {}
    _wait_saved()
    monkeypatch.setattr(routing, "fold", _cpu_bound_fold(30.0))  # would be late if it folded again
    home["search"]["hits"] = [dict(HITS[0], id="42")]
    assert _routing_part(_cold(monkeypatch)).startswith(FULL)


@pytest.mark.parametrize("damage", ["huge-log", "huge-view"])
def test_a_huge_record_never_slows_the_daemon_path(home, monkeypatch, daemon, damage):
    _record()
    _damage(home, monkeypatch, damage)
    # The search itself is CPU-bound Python in the daemon: a routing read holding
    # the daemon's GIL would starve it past its own deadline.

    def busy_search(*_a, **_k):
        end = time.monotonic() + 0.3
        while time.monotonic() < end:
            pass
        return {"hits": [dict(h) for h in home["search"]["hits"]], "legs": ["lexical"], "degraded": None}

    monkeypatch.setattr(recall_prompt, "_search_hits", busy_search)
    started = time.monotonic()
    code, before = _client(home)
    baseline = time.monotonic() - started
    assert code == 0 and "## Prior work" in before
    _wait_log(1)
    _push_on(monkeypatch)
    recall_prompt._save_recent_batches(SESSION, [])  # the same hits again are not a repeat
    started = time.monotonic()
    code, out = _client(home)
    took = time.monotonic() - started
    assert code == 0
    assert out == before  # nothing added, recall's part byte for byte
    assert took < baseline + 0.1 + 0.4, (took, baseline)
    line = _wait_log(2)
    assert " reason=ok " in line and line.endswith(" routing=skipped:budget")
    assert _marker() == {}
    _wait_saved()


def test_only_one_prompt_refolds_at_a_time(home, monkeypatch):
    _record()
    routing.view_file().unlink()
    _push_on(monkeypatch)
    lock = routing.routing_dir() / routing.PROMPT_FOLD_LOCK
    with open(lock, "a") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)  # another prompt's child is folding
        out = _cold(monkeypatch)
    assert "## Routing" not in out
    assert _last_log().endswith(" routing=skipped:fold-busy")
    home["search"]["hits"] = [dict(HITS[0], id="42")]
    assert _routing_part(_cold(monkeypatch)).startswith(FULL)


def _u16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def test_a_block_too_long_for_the_harness_limit_leaves_a_pointer(home, monkeypatch):
    _record()
    routing.set_note("Codex first for builds today; Grok for surveys. " * 250, via="app")
    _push_on(monkeypatch)
    ctx = _ctx(_cold(monkeypatch))
    assert ctx.startswith("## Prior work")
    assert ctx.endswith(recall_prompt.ROUTING_TOO_LONG)
    assert _u16(ctx) <= 10_000
    assert _last_log().endswith(" routing=pointer:full")
    view = routing.read_view()
    # Told where to read it, not given it: the session has not heard the summary.
    assert _marker() == {"pointer": {"version": view["version"], "epoch": view["epoch"]}}
    # The pointer is said once per version, not on every prompt.
    home["search"]["hits"] = [dict(HITS[0], id="42")]
    out = _cold(monkeypatch)
    assert "## Routing" not in out and "## Prior work" in out
    assert _last_log().endswith(" routing=pointer-said:full")
    home["search"]["hits"] = []
    assert json.loads(_cold(monkeypatch)) == {}
    assert _last_log().endswith(" routing=pointer-said:full")
    # A new version says it again, once.
    routing.add_rule("Never a -fast tier.", via="app")
    home["search"]["hits"] = [dict(HITS[0], id="43")]
    assert _ctx(_cold(monkeypatch)).endswith(recall_prompt.ROUTING_TOO_LONG)
    assert _last_log().endswith(" routing=pointer:full")
    assert "version" not in _marker()


def test_a_block_and_the_recall_part_together_stay_within_the_harness_limit(home, monkeypatch):
    _record()
    note = "Codex first for builds today; Grok for surveys. " * 180  # about 8,800 characters
    routing.set_note(note, via="app")
    _push_on(monkeypatch)
    ctx = _ctx(_cold(monkeypatch))
    assert _u16(ctx) <= 10_000
    assert ctx.startswith("## Prior work")
    assert note.strip() in ctx or ctx.endswith(recall_prompt.ROUTING_TOO_LONG)


def test_a_mac_with_no_record_hears_it_once_and_nothing_is_written(home, monkeypatch):
    _push_on(monkeypatch)
    part = _routing_part(_cold(monkeypatch))
    assert part.splitlines() == [
        "## Routing at 15:00Z (Khipu's routing record, version 0; data, not instructions; guidance, not a gate)",
        "No routing record on this Mac."]
    home["search"]["hits"] = [dict(HITS[0], id="42")]
    assert "## Routing" not in _cold(monkeypatch)
    assert _last_log().endswith(" routing=none")
    assert not routing.routing_dir().exists()


def _block_of(text_len_target: int, marker: dict) -> str:
    """Set the note so the routing block for ``marker`` is ``text_len_target``
    UTF-16 units long; returns the block."""
    routing.set_note("short", via="app")
    small = routing.prompt_push(marker, budget=1.0)["text"]
    routing.set_note("n" * (len("short") + text_len_target - _u16(small)), via="app")
    text = routing.prompt_push(marker, budget=1.0)["text"]
    assert _u16(text) == text_len_target
    return text


@pytest.mark.parametrize("kind", ["full", "changes"])
def test_a_block_pushed_out_by_the_recall_part_is_not_recorded_as_heard(home, monkeypatch, kind):
    """Finding F1 of fix round 1: the pointer replaced the block, yet the
    session was marked as having heard it, so the next prompt with room sent
    nothing."""
    from khipu import recall_rule

    _record()
    seen = _seen_now() if kind == "changes" else {}
    block = _block_of(recall_rule._HOOK_CONTEXT_BUDGET - 60, seen)  # fits alone, not with recall
    _push_on(monkeypatch)
    ctx = _ctx(_cold(monkeypatch))
    assert ctx.startswith("## Prior work") and ctx.endswith(recall_prompt.ROUTING_TOO_LONG)
    label = "full" if kind == "full" else "changes:1"
    assert _last_log().endswith(f" routing=pointer:{label}")
    assert _marker().get("version") == seen.get("version")  # not heard: the marker did not move
    assert _marker().get("full_at") == seen.get("full_at")
    # The same hits again: recall is deduplicated, so the block now fits and is sent whole.
    out = _cold(monkeypatch)
    assert " reason=dedup " in _last_log()
    assert _routing_part(out) == block
    assert _last_log().endswith(f" routing={label}")
    view = routing.read_view()
    assert _marker()["version"] == view["version"] and "pointer" not in _marker()
    if kind == "full":
        assert _marker()["full_at"] == routing._iso(routing._now())


def test_a_pushed_out_block_is_sent_again_on_the_daemon_path(home, monkeypatch, daemon):
    from khipu import recall_rule

    _record()
    block = _block_of(recall_rule._HOOK_CONTEXT_BUDGET - 60, {})
    _push_on(monkeypatch)
    code, out = _client(home)
    assert code == 0 and _ctx(out).endswith(recall_prompt.ROUTING_TOO_LONG)
    assert _wait_log(1).endswith(" routing=pointer:full")
    assert "version" not in _marker()
    code, out = _client(home)  # deduplicated: room for the block
    assert code == 0 and _routing_part(out) == block
    assert _wait_log(2).endswith(" routing=full")
    assert _marker()["version"] == routing.read_view()["version"]


def test_the_routing_clock_starts_when_the_read_starts(home, monkeypatch):
    """Finding F2 of fix round 1: the budget was counted from before the
    hook imported and configured routing, which a fresh interpreter spends
    most of 50 ms on, so a gated prompt never got its routing lines."""
    from khipu import features

    _record()
    config = json.loads((home["data"] / "config.json").read_text())
    config.pop("routing_prompt_budget_ms")  # the default, 50 ms
    (home["data"] / "config.json").write_text(json.dumps(config))
    assert routing.prompt_budget() == 0.05
    real = features.enabled

    def slow_start(name, environ=None):  # a cold interpreter reading its config
        time.sleep(0.1)
        return real(name, environ=environ)

    monkeypatch.setattr(features, "enabled", slow_start)
    _push_on(monkeypatch)
    out = _cold(monkeypatch, prompt="ok")
    assert _recall_part(out) == "" and _routing_part(out).startswith(FULL)
    assert _last_log().endswith(" routing=full")


def test_a_gated_prompt_on_the_cold_path_gets_routing_at_the_default_budget(home):
    """The one-shot path in a fresh interpreter, as the launcher runs it with
    the recall daemon down, at the default budget."""
    home["clock"].now = datetime.now(timezone.utc).replace(microsecond=0)
    _record()
    routing.read_view()
    config = json.loads((home["data"] / "config.json").read_text())
    config.pop("routing_prompt_budget_ms")
    (home["data"] / "config.json").write_text(json.dumps(config))
    env = dict(os.environ, KHIPU_FEATURE_ROUTING_PUSH="1", KHIPU_RECALL_DAEMON="0",
               PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    env.pop("KHIPU_RECALL_DEADLINE", None)
    lines = []
    for attempt in range(3):  # a loaded machine may lose one; the old clock lost every one
        payload = dict(_payload("ok"), session_id=f"cold-{attempt}")
        proc = subprocess.run([sys.executable, "-c", "from khipu.recall_prompt import prompt_recall_main; "
                               "prompt_recall_main(shape='claude')"],
                              input=json.dumps(payload).encode(), env=env, capture_output=True, timeout=30)
        assert proc.returncode == 0
        lines.append(_last_log())
        if _routing_part(proc.stdout.decode()).startswith(FULL):
            assert lines[-1].endswith(" routing=full")
            return
    raise AssertionError(lines)


@pytest.mark.parametrize("name", ["SIGTERM", "SIGINT", "SIGHUP"])
def test_a_late_routing_child_ends_on_a_stop_signal(home, monkeypatch, name):
    """Finding F3 of fix round 1: the child kept the recall daemon's Python
    SIGTERM and SIGINT handlers, so a stop signal only set an event and the
    child folded on until its alarm, a minute later."""
    import signal

    sig = getattr(signal, name)
    _record()
    _damage(home, monkeypatch, "huge-log")
    monkeypatch.setattr(routing, "fold", _cpu_bound_fold(30.0))
    _push_on(monkeypatch)
    pids = []
    real_fork = os.fork

    def fork():
        pid = real_fork()
        if pid:
            pids.append(pid)
        return pid

    monkeypatch.setattr(recall_prompt.os, "fork", fork)
    before = signal.getsignal(sig)
    signal.signal(sig, lambda *_a: None)  # as ``recall_daemon.serve`` does: stop the service, not the process
    try:
        _cold(monkeypatch)
        assert _last_log().endswith(" routing=skipped:budget") and len(pids) == 1
        os.kill(pids[0], 0)  # still folding
        os.kill(pids[0], sig)
        for _ in range(150):
            try:
                os.kill(pids[0], 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            raise AssertionError(f"the routing child ignored {name}")
    finally:
        signal.signal(sig, before)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_the_routing_imports_cover_everything_the_read_imports(home):
    """The budget starts after ``routing.prompt_imports``: a module the read
    imports lazily and that list misses would be timed by the budget again."""
    _record()
    routing.read_view()
    code = ("import sys\n"
            "from khipu import recall_prompt, routing\n"
            "routing.prompt_imports()\n"
            "before = set(sys.modules)\n"
            "routing.prompt_push(recall_prompt.routing_marker('fresh'), budget=1.0)\n"
            "print(sorted(m for m in set(sys.modules) - before if m.startswith('khipu')))\n")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr.decode()[-2000:]
    assert proc.stdout.decode().strip() == "[]"


# ---- fix round 2 -------------------------------------------------------------------------------


@pytest.mark.parametrize("switch", [{"ROUTING_NOW_OFF": "1"}, {"KHIPU_FEATURE_ROUTING_PUSH": "0"}])
def test_the_hooks_own_switches_turn_the_push_off_on_the_daemon_path(home, monkeypatch, daemon, switch):
    """Finding R2-1: the service read ROUTING_NOW_OFF and the feature switch
    from its own environment, so a hook that set them still got routing."""
    _record()
    code, before = _client(home)
    assert code == 0 and "## Prior work" in before and "## Routing" not in before
    _wait_log(1)
    _push_on(monkeypatch)  # the service's own environment: on
    recall_prompt._save_recent_batches(SESSION, [])
    code, out = _client(home, env=switch)
    assert code == 0 and out == before
    assert " routing=" not in _wait_log(2)
    assert _marker() == {}
    recall_prompt._save_recent_batches(SESSION, [])
    code, out = _client(home)  # the hook's environment says nothing: the service's own stands
    assert code == 0 and _routing_part(out).startswith(FULL)
    assert _wait_log(3).endswith(" routing=full")


def test_the_hooks_own_feature_switch_turns_the_push_on_on_the_daemon_path(home, daemon):
    _record()
    code, out = _client(home, env={"KHIPU_FEATURE_ROUTING_PUSH": "1"})
    assert code == 0 and _routing_part(out).startswith(FULL)
    assert _wait_log(1).endswith(" routing=full")


def test_the_hook_never_imports_routing_itself(home, monkeypatch):
    """Finding R2-3: the hook imported routing and read its settings before
    the search started, about 25 ms on every cold-path prompt's critical path.
    Only the child imports it; the hook's own process never does."""
    _record()
    _push_on(monkeypatch)
    script = (
        "import io, json, sys\n"
        "from khipu import recall_prompt\n"
        "seen = {}\n"
        "def search(*_a, **_k):\n"
        "    seen['at_search'] = 'khipu.routing' in sys.modules\n"
        "    return {'hits': [{'kind': 'episode', 'id': '41', 'score': 0.9, 'snippet': 'kept the hook fail-open',"
        " 'label': 'kept the hook fail-open'}], 'legs': ['lexical'], 'degraded': None}\n"
        "recall_prompt._search_hits = search\n"
        "recall_prompt._deliverable_context_line = lambda *_a, **_k: ''\n"
        f"sys.stdin = io.StringIO({json.dumps(json.dumps(_payload()))})\n"
        "out = io.StringIO()\n"
        "real, sys.stdout = sys.stdout, out\n"
        "recall_prompt.prompt_recall_main(shape='claude')\n"
        "sys.stdout = real\n"
        "seen['after'] = 'khipu.routing' in sys.modules\n"
        "print(json.dumps({'seen': seen, 'out': out.getvalue()}))\n"
    )
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    proc = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr.decode()
    result = json.loads(proc.stdout.decode().splitlines()[-1])
    assert result["seen"] == {"at_search": False, "after": False}
    assert _routing_part(result["out"]).startswith(FULL)
    assert _last_log().endswith(" routing=full")
    assert _marker()["full_at"]  # the child's clock (this subprocess has no fake one)


def test_a_switched_off_setting_is_decided_in_the_child_and_logs_nothing(home, monkeypatch):
    _record()
    _push_on(monkeypatch)
    _set(home, routing_push_prompt=False)
    out = _cold(monkeypatch, session=None)
    assert "## Routing" not in out
    assert " routing=" not in _last_log()


def test_a_fold_longer_than_the_childs_bound_still_saves_the_view(home, monkeypatch):
    """Finding R2-2: a refold slower than the child's bound was killed before
    it saved the view, so every later prompt started the same fold again."""
    _record()
    routing.view_file().unlink()
    _set(home, routing_prompt_budget_ms=50)
    monkeypatch.setattr(recall_prompt, "_ROUTING_CHILD_MOST", 0.2)
    monkeypatch.setattr(routing, "fold", _cpu_bound_fold(2.5))
    _push_on(monkeypatch)
    out = _cold(monkeypatch)
    assert "## Routing" not in out and _last_log().endswith(" routing=skipped:budget")
    _wait_saved()
    monkeypatch.setattr(routing, "fold", _cpu_bound_fold(30.0))  # would be late if it folded again
    home["search"]["hits"] = [dict(HITS[0], id="42")]
    assert _routing_part(_cold(monkeypatch)).startswith(FULL)


def _fold_cost(home, events: list[dict]) -> int:
    """How many times the fold compares two reports' order (``_k``)."""
    path = routing.events_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    read, _bad = routing.read_events()
    _fingerprint, cfg, known = routing._inputs()
    calls = [0]
    real = routing._k

    def counted(item):
        calls[0] += 1
        return real(item)

    routing._k = counted
    try:
        routing.fold(read, now=T0, cfg=cfg, known=known)
    finally:
        routing._k = real
    return calls[0]


def _reports(n: int, shape: str) -> list[dict]:
    """``n`` reports on one account, a second apart: limits and opens taking
    turns, or guessed limits and cli limits taking turns."""
    out = []
    for i in range(n):
        at = routing._iso(T0 - timedelta(seconds=n - i))
        event = {"type": "evidence", "id": f"r{i:06d}", "account": "codex|", "source": "report",
                 "written_at": at, "via": "cli", "written_by": ME, "seq": i + 1}
        if shape == "limit-open":
            event.update(state="limited" if i % 2 else "open",
                         until=routing._iso(T0 + timedelta(hours=1)) if i % 2 else None)
        else:
            event.update(state="limited", until=routing._iso(T0 + timedelta(hours=1)),
                         basis="cli" if i % 2 else "estimated")
        out.append({k: v for k, v in event.items() if v is not None})
    return out


@pytest.mark.parametrize("shape", ["limit-open", "guess-cli"])
def test_a_fold_is_linear_in_the_size_of_the_log(home, shape):
    """Finding R2-2's second half: the fold compared every report with every
    other (5k reports took 0.67 s, 20k took 10 s), so a big log could never
    be folded within any bound."""
    small = _fold_cost(home, _reports(500, shape))
    big = _fold_cost(home, _reports(2000, shape))
    assert big < 5 * small, (small, big)


def test_an_unsaved_view_with_no_saved_one_is_the_same_view_until_it_changes(home, monkeypatch):
    """Finding R2-4: with view.json gone and the routing folder read-only,
    every unsaved fold drew a new random epoch, so every prompt got the full
    summary although nothing had changed."""
    _record()
    routing.view_file().unlink()
    _push_on(monkeypatch)
    folder = routing.routing_dir()
    (folder / routing.PROMPT_FOLD_LOCK).touch()  # an earlier prompt's
    folder.chmod(0o555)
    try:
        first = _cold(monkeypatch)
        assert _routing_part(first).startswith(FULL)
        assert _last_log().endswith(" routing=full")
        epoch = _marker()["epoch"]
        home["search"]["hits"] = [dict(HITS[0], id="42")]
        assert "## Routing" not in _cold(monkeypatch)
        assert _last_log().endswith(" routing=none")
        assert _marker()["epoch"] == epoch
    finally:
        folder.chmod(0o755)
    assert not routing.view_file().exists()



# ---- fix round 3 -------------------------------------------------------------------------------


def _count_forks(monkeypatch) -> list[int]:
    """Every os.fork this (the daemon's) process makes."""
    forks: list[int] = []
    real_fork = os.fork

    def fork():
        pid = real_fork()
        if pid:
            forks.append(pid)
        return pid

    monkeypatch.setattr(recall_prompt.os, "fork", fork)
    return forks


@pytest.mark.parametrize("off", ["setting", "file"])
def test_a_switched_off_push_starts_nothing_in_the_daemon(home, monkeypatch, daemon, off):
    """Finding R3-1: with routing_push_prompt off, or the old hook's bypass
    file present, the daemon still forked itself on every prompt, because
    only the child read those two switches."""
    _record()
    code, before = _client(home)
    assert code == 0 and "## Prior work" in before
    _wait_log(1)
    _push_on(monkeypatch)
    if off == "setting":
        _set(home, routing_push_prompt=False)
    else:
        Path(routing.ROUTING_NOW_DISABLE_FILE).write_text("")
    forks = _count_forks(monkeypatch)
    recall_prompt._save_recent_batches(SESSION, [])
    code, out = _client(home)
    assert code == 0 and out == before
    assert " routing=" not in _wait_log(2)
    assert forks == []


@pytest.fixture
def helper_daemon(home):
    """The service as ``serve`` runs it: the routing child comes from its helper."""
    home["clock"].now = datetime.now(timezone.utc).replace(microsecond=0)  # the helper's child has the real clock
    svc = recall_daemon.RecallService(home["capture"] / "recall.sock", stamp_fn=lambda: "fixed",
                                      idle_check=False)
    assert svc.bind()
    assert not svc.start_routing()  # the push is off when it starts
    thread = threading.Thread(target=svc.run, daemon=True)
    thread.start()
    yield svc
    svc.stop()
    thread.join(5)
    svc.close()


def _helper_ready(svc) -> None:
    svc.routing_forkserver.prepare()
    assert svc.routing_forkserver.wait_ready(10)


def test_the_daemon_never_forks_itself_for_routing(home, monkeypatch, helper_daemon):
    """Finding R3-1: every prompt with the push on forked the whole daemon
    (about 18 ms at the live size) and slowed its search about four times
    while the child lived. One helper, spawned once, forks the children."""
    _record()
    _push_on(monkeypatch)
    forks = _count_forks(monkeypatch)
    _helper_ready(helper_daemon)
    code, out = _client(home)
    assert code == 0 and "## Prior work" in out and _routing_part(out).startswith(FULL)
    assert _wait_log(1).endswith(" routing=full")
    assert isinstance(_marker()["version"], int) and _marker()["full_at"]
    home["search"]["hits"] = [dict(HITS[0], id="42")]
    code, out = _client(home)
    assert code == 0 and "## Prior work" in out and "## Routing" not in out
    assert _wait_log(2).endswith(" routing=none")
    assert forks == [] and helper_daemon.routing_forkserver.spawned == 1


@pytest.mark.parametrize("off", [{"env": {"ROUTING_NOW_OFF": "1"}}, {"setting": False}])
def test_a_switched_off_push_never_asks_the_helper(home, monkeypatch, helper_daemon, off):
    _record()
    _push_on(monkeypatch)
    if "setting" in off:
        _set(home, routing_push_prompt=off["setting"])
    code, out = _client(home, env=off.get("env"))
    assert code == 0 and "## Prior work" in out and "## Routing" not in out
    assert " routing=" not in _wait_log(1)
    assert helper_daemon.routing_forkserver.spawned == 0


def test_a_dead_helper_is_started_again_and_recall_is_unchanged(home, monkeypatch, helper_daemon):
    _record()
    _push_on(monkeypatch)
    monkeypatch.setattr(recall_prompt, "_FORKSERVER_RESPAWN_S", 0.0)
    _helper_ready(helper_daemon)
    code, out = _client(home)
    assert code == 0 and _routing_part(out).startswith(FULL)
    _wait_log(1)
    helper = helper_daemon.routing_forkserver._proc
    helper.kill()
    helper.wait(5)
    _set(home, routing_resend_after_versions=1)
    routing.report(account="Claude", state="limited", until="1h", author=ME)
    home["search"]["hits"] = [dict(HITS[0], id="42")]
    code, out = _client(home)
    # The prompt that finds it dead never waits for the next one to start.
    assert code == 0 and "## Prior work" in out and "## Routing" not in out
    assert _wait_log(2).endswith(" routing=skipped:starting")
    assert helper_daemon.routing_forkserver.wait_ready(10)
    home["search"]["hits"] = [dict(HITS[0], id="43")]
    code, out = _client(home)
    assert code == 0 and "## Prior work" in out and "## Routing" in out
    assert helper_daemon.routing_forkserver.spawned == 2


def test_the_helper_ends_with_the_daemon(home, monkeypatch, helper_daemon):
    _record()
    _push_on(monkeypatch)
    _helper_ready(helper_daemon)
    code, _out = _client(home)
    assert code == 0
    helper = helper_daemon.routing_forkserver._proc
    assert helper.poll() is None
    helper_daemon.close()
    assert helper.wait(5) == 0


# ---- the helper's start and a burst of prompts (fix round 4) ---------------------------------


def _fresh_daemon(home, name: str):
    """A service started as ``serve`` starts it, on its own socket."""
    svc = recall_daemon.RecallService(home["capture"] / f"{name}.sock", stamp_fn=lambda: "fixed",
                                      idle_check=False)
    assert svc.bind()
    svc.start_routing()
    thread = threading.Thread(target=svc.run, daemon=True)
    thread.start()
    return svc, thread


def _end(svc, thread) -> None:
    svc.stop()
    thread.join(5)
    svc.close()


def _timed(svc, prompt: str = PROMPT) -> float:
    """One prompt as the client sends it (request, answer, ack): the seconds
    from the request to the answer."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(str(svc.path))
    try:
        req = {"op": "recall", "raw": json.dumps(_payload(prompt)), "shape": "claude", "protocol": 1,
               "deadline": time.monotonic() + 1.6}
        began = time.monotonic()
        sock.sendall(json.dumps(req).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        took = time.monotonic() - began
        assert json.loads(buf.split(b"\n", 1)[0]).get("ok")
        sock.sendall(b'{"op":"ack"}\n')
        return took
    finally:
        sock.close()


def _burst(svc, count: int) -> list[float]:
    times: list[float] = []
    lock = threading.Lock()
    go = threading.Event()

    def one(i: int) -> None:
        go.wait(5)
        took = _timed(svc, f"{PROMPT} {i}")
        with lock:
            times.append(took)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    go.set()
    for t in threads:
        t.join(15)
    assert len(times) == count
    return times


def _statuses(first: int, count: int) -> list[str]:
    _wait_log(first + count - 1)
    lines = _log_lines()[first - 1:first - 1 + count]
    return [line.rsplit(" routing=", 1)[1] if " routing=" in line else "" for line in lines]


def _routed(status: str) -> bool:
    return status in ("full", "changes", "none") or status.startswith("pointer:")


def _until_routed(svc) -> None:
    """Prompts until one is routed: the helper is up and ready."""
    for _ in range(100):
        before = len(_log_lines())
        _timed(svc)
        if _routed(_statuses(before + 1, 1)[0]):
            return
        time.sleep(0.05)
    raise AssertionError(f"routing never arrived: {_log_lines()[-3:]}")


def _median(values: list[float]) -> float:
    values = sorted(values)
    return values[len(values) // 2]


@pytest.mark.parametrize("when", ["at-start", "later"])
def test_the_first_prompt_after_a_daemon_start_is_no_slower_with_the_push_on(home, monkeypatch, when):
    """Finding R4-1: the helper was spawned inside the first prompt with the
    push on, which waited up to a second for it to import routing (150 to
    350 ms more than a warm daemon). The daemon starts it when it starts, or
    from a thread on the first prompt with the push on; until it is ready a
    prompt logs routing=skipped:starting and never waits for it."""
    _record()
    home["clock"].now = datetime.now(timezone.utc).replace(microsecond=0)
    _set(home, routing_prompt_budget_ms=50)
    off, on = [], []
    for i in range(3):
        monkeypatch.delenv("KHIPU_FEATURE_ROUTING_PUSH", raising=False)
        svc, thread = _fresh_daemon(home, f"off{i}")
        try:
            off.append(_timed(svc))
        finally:
            _end(svc, thread)
        if when == "at-start":
            _push_on(monkeypatch)
        svc, thread = _fresh_daemon(home, f"on{i}")
        try:
            _push_on(monkeypatch)
            before = len(_log_lines())
            on.append(_timed(svc))
            status = _statuses(before + 1, 1)[0]
            assert status == "skipped:starting" or _routed(status), status
            _until_routed(svc)  # once ready, it routes
            assert svc.routing_forkserver.spawned == 1
        finally:
            _end(svc, thread)
    print(f"first prompt medians: off {_median(off):.4f} on {_median(on):.4f}")
    assert _median(on) <= _median(off) + 0.04, (on, off)


def test_a_burst_of_prompts_keeps_the_helper_and_adds_little(home, monkeypatch):
    """Finding R4-2: 30 prompts at once on a fresh daemon filled the
    helper's datagram queue while it imported; the send blocked, the helper
    was killed under the lock (stalling the prompts behind it) and the
    respawn throttle skipped routing for the next bursts: a median of 217 ms
    with the push on against 31 ms off, and none of the 30 routed."""
    _record()
    home["clock"].now = datetime.now(timezone.utc).replace(microsecond=0)
    _set(home, routing_prompt_budget_ms=50)
    svc, thread = _fresh_daemon(home, "off")
    try:
        off = _burst(svc, 30)
    finally:
        _end(svc, thread)
    _push_on(monkeypatch)
    svc, thread = _fresh_daemon(home, "on")
    try:
        first = len(_log_lines()) + 1
        on = _burst(svc, 30)
        statuses = _statuses(first, 30)
        assert all(_routed(s) or s in ("skipped:starting", "skipped:busy") for s in statuses), statuses
        assert svc.routing_forkserver.wait_ready(10)
        helper = svc.routing_forkserver._proc
        for _ in range(2):  # later bursts route, on the same helper
            first = len(_log_lines()) + 1
            again = _burst(svc, 30)
            later = _statuses(first, 30)
            assert any(_routed(s) for s in later), later
            # Never a helper starting again; a child late under the load, or
            # waiting on another's fold, skips as it always did.
            assert all(_routed(s) or s in ("skipped:busy", "skipped:budget", "skipped:fold-busy")
                       for s in later), later
            assert svc.routing_forkserver._proc is helper and helper.poll() is None
        assert svc.routing_forkserver.spawned == 1
    finally:
        _end(svc, thread)
    print(f"burst medians: off {_median(off):.4f} fresh {_median(on):.4f} warm {_median(again):.4f}")
    assert _median(on) <= _median(off) + 0.04, (sorted(on), sorted(off))
    assert _median(again) <= _median(off) + 0.04, (sorted(again), sorted(off))


def test_a_full_helper_queue_skips_only_that_prompt_and_keeps_the_helper(home, monkeypatch):
    """Finding R4-2: a send to a full queue blocked up to 0.1 s and then
    killed the helper, waiting for it under the lock. Now it never blocks:
    the prompt logs routing=skipped:busy and the helper stays."""
    _record()
    home["clock"].now = datetime.now(timezone.utc).replace(microsecond=0)
    _set(home, routing_prompt_budget_ms=50)
    monkeypatch.setattr(recall_prompt, "_FORKSERVER_IN_FLIGHT_MOST", 1000)  # the queue fills first
    _push_on(monkeypatch)
    svc, thread = _fresh_daemon(home, "full")
    try:
        assert svc.routing_forkserver.wait_ready(10)
        helper = svc.routing_forkserver._proc
        os.kill(helper.pid, signal.SIGSTOP)  # it reads nothing: its queue fills
        try:
            first = len(_log_lines()) + 1
            times = _burst(svc, 40)
            statuses = _statuses(first, 40)
        finally:
            os.kill(helper.pid, signal.SIGCONT)
        busy = statuses.count("skipped:busy")
        assert busy and all(s in ("skipped:busy", "skipped:budget") for s in statuses), statuses
        assert max(sorted(times)[:busy]) < 0.5, sorted(times)  # a full queue is never waited on
        assert svc.routing_forkserver._proc is helper and helper.poll() is None
        assert svc.routing_forkserver._in_flight == 0
        time.sleep(0.2)
        _until_routed(svc)
        assert svc.routing_forkserver._proc is helper and svc.routing_forkserver.spawned == 1
    finally:
        _end(svc, thread)


def test_prompts_past_the_in_flight_limit_skip_at_once(home, monkeypatch):
    _record()
    home["clock"].now = datetime.now(timezone.utc).replace(microsecond=0)
    _set(home, routing_prompt_budget_ms=50)
    monkeypatch.setattr(recall_prompt, "_FORKSERVER_IN_FLIGHT_MOST", 2)
    _push_on(monkeypatch)
    svc, thread = _fresh_daemon(home, "cap")
    try:
        assert svc.routing_forkserver.wait_ready(10)
        helper = svc.routing_forkserver._proc
        os.kill(helper.pid, signal.SIGSTOP)
        try:
            first = len(_log_lines()) + 1
            _burst(svc, 6)
            statuses = _statuses(first, 6)
        finally:
            os.kill(helper.pid, signal.SIGCONT)
        assert sorted(statuses) == ["skipped:budget"] * 2 + ["skipped:busy"] * 4, statuses
        assert svc.routing_forkserver._in_flight == 0 and svc.routing_forkserver._proc is helper
    finally:
        _end(svc, thread)


# ---- fix round 5 -------------------------------------------------------------------------------


def _live_children(pid: int) -> list[int]:
    """The live (not yet ended) children of ``pid``."""
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,stat="], capture_output=True, text=True).stdout
    kids = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] == str(pid) and not parts[2].startswith("Z"):
            kids.append(int(parts[0]))
    return kids


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _stalled_view() -> None:
    """view.json becomes a FIFO nobody writes: every read of it blocks."""
    routing.read_view()
    routing.view_file().unlink()
    os.mkfifo(routing.view_file())


def test_readers_a_prompt_stopped_waiting_for_end_soon_and_are_capped(home, monkeypatch):
    """Finding R5-1: a reader whose prompt stopped waiting lived on for a
    minute and the helper took any number of them, so a stalled view.json
    (a FIFO here) left one live reader per prompt. Now a reader that is not
    refolding ends a couple of seconds after its prompt's latest wait, and
    past ``_FORKSERVER_CHILDREN_MOST`` live readers a prompt logs
    routing=skipped:busy."""
    _record()
    home["clock"].now = datetime.now(timezone.utc).replace(microsecond=0)
    _set(home, routing_prompt_budget_ms=50)
    _stalled_view()
    _push_on(monkeypatch)
    svc, thread = _fresh_daemon(home, "stall")
    try:
        assert svc.routing_forkserver.wait_ready(10)
        helper = svc.routing_forkserver._proc
        first = len(_log_lines()) + 1
        count = recall_prompt._FORKSERVER_CHILDREN_MOST + 6
        for i in range(count):
            _timed(svc, f"{PROMPT} {i}")
        statuses = _statuses(first, count)
        assert "skipped:busy" in statuses, statuses
        assert all(s in ("skipped:budget", "skipped:busy") for s in statuses), statuses
        assert len(_live_children(helper.pid)) <= recall_prompt._FORKSERVER_CHILDREN_MOST
        for _ in range(300):  # a couple of seconds after each prompt's latest wait, and the helper reaps
            if not _live_children(helper.pid):
                break
            time.sleep(0.02)
        else:
            raise AssertionError(f"readers still live: {_live_children(helper.pid)}")
        before = len(_log_lines())
        _timed(svc)
        assert _statuses(before + 1, 1) == ["skipped:budget"]  # room again
    finally:
        _end(svc, thread)
        routing.view_file().unlink()


def test_the_helper_ends_its_readers_when_it_stops(home, monkeypatch):
    """Finding R5-1: a stopped helper left its readers running."""
    _record()
    home["clock"].now = datetime.now(timezone.utc).replace(microsecond=0)
    _set(home, routing_prompt_budget_ms=50)
    _stalled_view()
    _push_on(monkeypatch)
    svc, thread = _fresh_daemon(home, "stop")
    try:
        assert svc.routing_forkserver.wait_ready(10)
        helper = svc.routing_forkserver._proc
        for i in range(3):
            _timed(svc, f"{PROMPT} {i}")
        kids = _live_children(helper.pid)
        assert kids
    finally:
        _end(svc, thread)
    stopped = time.monotonic()
    while any(_alive(pid) for pid in kids) and time.monotonic() - stopped < 1.0:
        time.sleep(0.02)
    routing.view_file().unlink()
    assert not any(_alive(pid) for pid in kids), "readers outlived their helper"


def test_a_cold_reader_ends_soon_after_its_prompt(home, monkeypatch):
    """Finding R5-1, the one-shot path: its own child gets the same short life."""
    _record()
    _set(home, routing_prompt_budget_ms=50)
    _stalled_view()
    _push_on(monkeypatch)
    pids = _count_forks(monkeypatch)
    try:
        began = time.monotonic()
        _cold(monkeypatch)
        assert _last_log().endswith(" routing=skipped:budget") and len(pids) == 1
        while _alive(pids[0]) and time.monotonic() - began < 5.0:
            time.sleep(0.02)
        assert not _alive(pids[0]), "the cold reader lived on"
        assert time.monotonic() - began < 1.0 + recall_prompt._ROUTING_CHILD_MOST + 0.5
    finally:
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        routing.view_file().unlink()


def _lock_contended(monkeypatch) -> None:
    def busy():
        raise BlockingIOError("the routing lock is held")

    monkeypatch.setattr(routing, "_lock", busy)


def test_an_unsaved_change_is_never_taken_for_no_change(home, monkeypatch):
    """Finding R5-2: a fold that could not be saved kept the saved view's
    version, so the version check answered "none" and the change was never
    pushed while saves kept failing. Now the prompt skips
    (routing=skipped:unsaved), the seen marker stays, and the next prompt
    that can save delivers it."""
    _record()
    _push_on(monkeypatch)
    before = _seen_now()
    saved = routing.view_file().read_bytes()
    home["clock"].advance(minutes=10)
    routing.report(account="Claude", state="limited", until="2h", author=ME)
    routing.view_file().write_bytes(saved)  # as if the fold after the report had not been saved
    with monkeypatch.context() as m:
        _lock_contended(m)
        with pytest.raises(routing.PushUnsaved):
            routing.prompt_push(before, budget=1.0)
        assert "## Routing" not in _cold(monkeypatch)
        assert _last_log().endswith(" routing=skipped:unsaved")
        assert _marker() == before
    home["search"]["hits"] = [dict(HITS[0], id="42")]
    out = _cold(monkeypatch)
    assert _routing_part(out).startswith(CHANGES) and "Claude: limit reached" in _routing_part(out)
    assert _last_log().endswith(" routing=changes:1")


def test_an_unsaved_view_that_changed_nothing_still_sends_nothing(home, monkeypatch):
    _record()
    _push_on(monkeypatch)
    before = _seen_now()
    home["clock"].advance(minutes=1)
    Path(routing.events_file()).touch()  # an input changed; the state did not
    with monkeypatch.context() as m:
        _lock_contended(m)
        assert routing.prompt_push(before, budget=1.0)["kind"] == "none"
        assert "## Routing" not in _cold(monkeypatch)
        assert _last_log().endswith(" routing=none")
    assert _marker() == before


def _fake_reader(lines: list[tuple[float, bytes]], *, budget: float = 0.01):
    """A ``_RoutingAtPrompt`` reading a pipe a thread writes ``lines`` to,
    each after its delay; the ready line comes first, at once."""
    read_fd, write_fd = os.pipe()
    at = recall_prompt._RoutingAtPrompt()
    at.forked = time.monotonic()
    at.cap = at.forked + recall_prompt._ROUTING_WAIT_MOST
    at.fd, at.status, at.session = read_fd, "skipped:budget", SESSION
    os.write(write_fd, f"{time.monotonic()!r} {budget!r}\n".encode())

    def write() -> None:
        try:
            for delay, data in lines:
                time.sleep(delay)
                os.write(write_fd, data(at) if callable(data) else data)
        except OSError:
            pass
        finally:
            os.close(write_fd)

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    return at, writer


def test_an_answer_finished_after_the_budget_is_not_taken(home):
    """Finding R5-3: once the budget ran out the wait became zero-time but
    still read whatever was buffered, so an answer written about 90 ms after
    the child was ready was taken against a 10 ms budget."""
    answer = {"kind": "full", "text": "## Routing at x", "version": 1, "epoch": "e", "count": 0}

    def late(_at) -> bytes:
        return json.dumps(dict(answer, done=time.monotonic())).encode() + b"\n"

    at, writer = _fake_reader([(0.09, late)])
    writer.join(5)  # the answer is buffered before the prompt reads it, as after a slow search
    assert at.collect() is None and at.status == "skipped:budget"
    at, writer = _fake_reader([(0.0, late)], budget=0.5)
    writer.join(5)
    out = at.collect()
    assert out is not None and out["kind"] == "full"


def test_a_pipe_that_never_stops_is_drained_only_briefly(home):
    """Finding R5-3: a continuously readable pipe kept the drain loop going."""
    read_fd, write_fd = os.pipe()
    at = recall_prompt._RoutingAtPrompt()
    at.forked = time.monotonic()
    at.cap = at.forked + recall_prompt._ROUTING_WAIT_MOST
    at.fd, at.status, at.session = read_fd, "skipped:budget", SESSION
    os.write(write_fd, f"{time.monotonic()!r} 0.01\n".encode())
    writer = subprocess.Popen(["cat", "/dev/zero"], stdout=write_fd)  # never stops, never ends a line
    os.close(write_fd)
    try:
        time.sleep(0.05)
        done = {}

        def run() -> None:
            began = time.monotonic()
            done["out"] = at.collect()
            done["took"] = time.monotonic() - began

        reader = threading.Thread(target=run, daemon=True)
        reader.start()
        reader.join(3)
        assert "took" in done, "the drain never ended"
        assert done["out"] is None and done["took"] < 0.5, done
        assert at.status in ("skipped:budget", "skipped:no-answer")
    finally:
        writer.kill()
        writer.wait(5)
