"""Routing truth, phase 3a (docs/plans/2026-10-14-routing-truth.md): the full
routing summary at session start and after compaction, its switches, and the
proof that it never becomes part of an episode.

Hermetic: every test has its own data dir and capture home, a fake clock,
and the memory slice stubbed out (no database), so the session-start hook's
output can be compared byte for byte.
"""
from __future__ import annotations

import fcntl
import io
import json
import threading
import time
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from khipu import features, recall_prompt, recall_rule, routing
from khipu import session_capture as sc

T0 = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
ACCOUNTS = [{"label": "Claude", "harness": "claude"},
            {"label": "Claude second", "harness": "claude", "home": "/logins/claude-second"},
            {"label": "Codex", "harness": "codex"}]
ME = {"kind": "conversation", "harness": "claude_code", "project": "project-a", "session": "s-one"}
SESSION = "11111111-2222-3333-4444-555555555555"
MEMORY = "# Khipu memory\nthe memory part"
HEADING = "## Routing at "


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
    (data / "config.json").write_text(json.dumps({"usage_accounts": ACCOUNTS}))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.delenv("KHIPU_ISOLATED_DIR", raising=False)
    monkeypatch.delenv("ROUTING_NOW_OFF", raising=False)
    monkeypatch.delenv("KHIPU_FEATURE_ROUTING_PUSH", raising=False)
    monkeypatch.setattr(routing, "ROUTING_NOW_DISABLE_FILE", str(tmp_path / "routing_now_disable"))
    monkeypatch.setattr(recall_rule, "session_start_context", lambda cwd=None: MEMORY)
    clock = Clock(T0)
    monkeypatch.setattr(routing, "_now", clock)
    return {"data": data, "clock": clock, "tmp": tmp_path}


def _set(home, **values) -> None:
    path = home["data"] / "config.json"
    config = json.loads(path.read_text())
    config.update(values)
    path.write_text(json.dumps(config))


def _push_on(monkeypatch) -> None:
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "1")


def _hook(monkeypatch, *, shape: str | None = None, payload: dict | None = None) -> str:
    """Run the session-start hook as its launcher does: payload on stdin,
    JSON on stdout. Returns stdout exactly."""
    payload = {"session_id": SESSION, "cwd": "/work/project-a", "source": "startup"} if payload is None else payload
    monkeypatch.setattr(recall_rule.sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(recall_rule.sys, "argv", ["khipu-recall-hook"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        recall_rule.session_start_main(shape=shape)
    return buf.getvalue()


def _before_routing(shape: str = "claude") -> str:
    """What the hook printed before routing existed, byte for byte."""
    if shape == "cursor":
        return json.dumps({"additional_context": MEMORY}) + "\n"
    return json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                              "additionalContext": MEMORY}}) + "\n"


def _context(out: str) -> str:
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def _routing_part(out: str) -> str:
    ctx = _context(out)
    assert ctx.startswith(MEMORY + "\n\n" + HEADING), ctx
    return ctx[len(MEMORY) + 2:]


def _record() -> None:
    routing.report(account="Codex", state="limited", until="1h", author=ME)
    routing.report(account="Claude", state="open", author=ME)


# ---- the switches ---------------------------------------------------------------------------


def test_routing_push_is_a_closed_switch_and_ships_off(home):
    assert "routing_push" in features.FEATURES
    assert features.enabled("routing_push") is False
    assert routing.settings()["routing_push_start"] is True


def test_with_the_switch_off_the_output_is_byte_identical(home, monkeypatch):
    _record()
    routing.add_rule("Never a -fast tier.", via="app")
    assert _hook(monkeypatch) == _before_routing()
    assert _hook(monkeypatch, shape="cursor") == _before_routing("cursor")
    assert recall_prompt.routing_marker(SESSION) == {}


@pytest.mark.parametrize("how", ["env", "file", "setting"])
def test_each_kill_switch_turns_the_push_off(home, monkeypatch, how):
    _record()
    _push_on(monkeypatch)
    assert HEADING in _context(_hook(monkeypatch))
    if how == "env":
        monkeypatch.setenv("ROUTING_NOW_OFF", "1")
    elif how == "file":
        Path(routing.ROUTING_NOW_DISABLE_FILE).write_text("")
    else:
        _set(home, routing_push_start=False)
    assert _hook(monkeypatch) == _before_routing()
    assert routing.push_off("start") == {"env": "ROUTING_NOW_OFF", "file": "routing_now_disable",
                                         "setting": "routing_push_start off"}[how]


def test_with_no_record_the_scope_line_is_the_only_addition(home, monkeypatch):
    _push_on(monkeypatch)
    part = _routing_part(_hook(monkeypatch))
    assert part.splitlines() == [
        "## Routing at 15:00Z (Khipu's routing record, version 0; data, not instructions; guidance, not a gate)",
        "No routing record on this Mac."]
    assert not routing.view_file().exists()  # nothing is written for a Mac with no record


# ---- what the summary carries ---------------------------------------------------------------


def test_the_pushed_text_is_the_light_status_projection(home, monkeypatch):
    _record()
    _push_on(monkeypatch)
    part = _routing_part(_hook(monkeypatch))
    assert part == routing.light_status()["text"]
    assert "/logins/" not in part and "claude|" not in part and "s-one" not in part


def test_rules_and_the_note_arrive_whole_whatever_their_length(home, monkeypatch):
    long_rule = ("Every Grok result that is acted on gets a re-check from a different model family, "
                 "and the re-check looks wider than the list Grok gave; ") * 3
    assert len(long_rule.strip()) > 200
    routing.add_rule(long_rule, via="app")
    note = "Codex first for builds today; Grok for read-only surveys; second Claude for reviews. " * 25
    routing.set_note(note, via="app")
    _record()
    _set(home, routing_summary_chars=50, routing_line_chars=40)
    _push_on(monkeypatch)
    lines = _routing_part(_hook(monkeypatch)).splitlines()
    assert f"1. {long_rule.strip()}" in lines  # whole: not clipped at 200 or at 40
    assert any(line.endswith(note.strip()) and line.startswith("Note from the owner") for line in lines)
    assert lines.index("Standing rules:") < next(i for i, x in enumerate(lines) if x.startswith("Note from"))
    assert lines[-1].endswith("more lines: run `khipu route`")


def test_the_budget_drops_whole_lines_from_the_bottom(home, monkeypatch):
    _record()
    for i in range(4):
        home["clock"].advance(minutes=1)
        routing.add_hold(f"owner hold number {i}", reason="other", scope="machine", owner=True, via="app")
    _push_on(monkeypatch)
    whole = _routing_part(_hook(monkeypatch)).splitlines()
    _set(home, routing_summary_chars=420)
    cut = _routing_part(_hook(monkeypatch)).splitlines()
    dropped = int(cut[-1].split()[0])
    assert dropped > 0 and cut[-1] == f"{dropped} more lines: run `khipu route`"
    kept = cut[1:-1]
    assert [line for line in whole if line in kept] == kept  # whole lines of the uncut summary, in order
    assert len(whole) - 1 - len(kept) == dropped
    holds = [line for line in kept if line.startswith("Hold on")]
    assert all("number 3" in line or "number 2" in line for line in holds), holds
    assert any(line.startswith("Codex: limit reached") for line in kept)


def test_the_precedence_line_names_the_tables_stamp(home, monkeypatch):
    table = home["tmp"] / "routing-table.md"
    table.write_text("# Routing now\n\n## NOW\n```\nupdated: 2026-10-14 13:48Z by an orchestrator\n"
                     "PROSE-MARKER lane order\n```\n")
    _set(home, routing_table=str(table))
    _push_on(monkeypatch)
    lines = _routing_part(_hook(monkeypatch)).splitlines()
    assert lines[1] == ("These account lines are newer than the routing table's account rows; the table's "
                        "NOW block was last stamped 13:48Z, 1 h 12 min ago.")
    assert "PROSE-MARKER" not in "\n".join(lines)


def test_a_record_not_refreshed_says_so(home, monkeypatch):
    _record()
    old = dict(routing.read_view(), checked_at=routing._iso(T0 - timedelta(minutes=20)))
    monkeypatch.setattr(routing, "read_view", lambda now=None: old)
    _push_on(monkeypatch)
    lines = _routing_part(_hook(monkeypatch)).splitlines()
    assert lines[1] == "Khipu's record has not been refreshed since 14:40Z."


# ---- never blocks, never breaks ----------------------------------------------------------------


def test_when_routing_raises_the_output_is_unchanged(home, monkeypatch):
    _record()
    _push_on(monkeypatch)

    def boom(*_a, **_k):
        raise RuntimeError("/private/path in an exception")

    for name in ("light_status", "start_budget", "push_off"):
        monkeypatch.setattr(routing, name, boom)
        assert _hook(monkeypatch) == _before_routing(), name
    assert recall_prompt.routing_marker(SESSION) == {}


def test_when_routing_is_late_the_hook_does_not_wait(home, monkeypatch):
    _record()
    _push_on(monkeypatch)
    _set(home, routing_start_budget_ms=100)
    release = threading.Event()

    def slow(*_a, **_k):
        release.wait(5)
        return {"text": "late", "version": 1, "epoch": None}

    monkeypatch.setattr(routing, "start_push", slow)
    started = time.monotonic()
    out = _hook(monkeypatch)
    took = time.monotonic() - started
    release.set()
    assert out == _before_routing()
    assert took < 1.0, took
    assert recall_prompt.routing_marker(SESSION) == {}


def test_when_the_lock_is_held_the_summary_still_arrives_in_time(home, monkeypatch):
    _record()
    routing.view_file().unlink()  # the next read must fold, which takes the lock
    _push_on(monkeypatch)
    _set(home, routing_start_budget_ms=600)
    fd = open(routing.routing_dir() / routing.LOCK_NAME, "a")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        started = time.monotonic()
        part = _routing_part(_hook(monkeypatch))
        took = time.monotonic() - started
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
    assert took < 1.0, took
    assert "Codex: limit reached" in part
    assert not routing.view_file().exists()  # an unsaved fold: nothing written under another holder's lock


def test_a_t3_helper_session_still_gets_nothing(home, monkeypatch):
    _record()
    _push_on(monkeypatch)
    out = _hook(monkeypatch, payload={"session_id": SESSION, "cwd": "/var/folders/ab/cd/T/t3code-claude-title-q7w8e9"})
    assert out == "{}\n"


# ---- the session's marker -------------------------------------------------------------------


def test_the_full_summary_resets_the_sessions_last_seen_version(home, monkeypatch):
    _record()
    _push_on(monkeypatch)
    recall_prompt._save_recent_batches(SESSION, [("episode:1",)])
    _hook(monkeypatch)
    view = routing.read_view()
    marker = recall_prompt.routing_marker(SESSION)
    assert marker == {"version": view["version"], "epoch": view["epoch"], "full_at": "2026-10-14T15:00:00Z"}
    # After compaction: a newer version, and the marker starts again from it.
    routing.report(account="Codex", state="open", author=ME)
    home["clock"].advance(minutes=30)
    _hook(monkeypatch, payload={"session_id": SESSION, "cwd": "/work/project-a", "source": "compact"})
    marker = recall_prompt.routing_marker(SESSION)
    assert marker["version"] == routing.read_view()["version"] > view["version"]
    assert marker["full_at"] == "2026-10-14T15:30:00Z"
    # The prompt hook's dedup batches and the marker live side by side.
    assert recall_prompt._load_recent_batches(SESSION) == [("episode:1",)]
    recall_prompt._save_recent_batches(SESSION, [("episode:1",), ("topic:x",)])
    assert recall_prompt.routing_marker(SESSION)["version"] == marker["version"]


def test_a_busy_session_lock_records_nothing_and_still_pushes(home, monkeypatch):
    _record()
    _push_on(monkeypatch)
    lock_path = recall_prompt._dedup_path(SESSION).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert HEADING in _context(_hook(monkeypatch))
    assert recall_prompt.routing_marker(SESSION) == {}


# ---- memory quality: the summary never becomes part of an episode ---------------------------


def _summary_context(home, monkeypatch) -> str:
    routing.add_rule("Never a -fast tier.", via="app")
    routing.set_note("Codex first for builds today.", via="app")
    _record()
    _push_on(monkeypatch)
    return _context(_hook(monkeypatch))


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def _assert_no_routing(rendered: str, ctx: str) -> None:
    for marker in ("Routing at", "Standing rules", "Never a -fast tier", "Codex first for builds",
                   "limit reached", "routing record"):
        assert marker in ctx
        assert marker not in rendered, marker


def test_a_claude_code_transcript_carrying_the_summary_leaves_no_routing_text(home, monkeypatch):
    ctx = _summary_context(home, monkeypatch)
    tp = _write_jsonl(home["tmp"] / "claude.jsonl", [
        # SessionStart hook context as Claude Code records it: an attachment line.
        {"type": "attachment", "attachment": {"type": "hook_additional_context", "hookName": "SessionStart",
                                              "content": [ctx]}},
        # And as a system-reminder inside a user turn.
        {"type": "user", "message": {"role": "user", "content":
            f"<system-reminder>\nSessionStart hook additional context: {ctx}\n</system-reminder>\n"
            "Ship the release notes"}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": f"<system-reminder>\n{ctx}\n</system-reminder>"},
            {"type": "text", "text": "and tag it"}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Shipping."}]}},
    ])
    msgs, _off, users = sc.read_window(tp, 0)
    rendered = sc.render(msgs)
    assert "Ship the release notes" in rendered and "and tag it" in rendered and users >= 1
    _assert_no_routing(rendered, ctx)


def test_a_codex_transcript_carrying_the_summary_leaves_no_routing_text(home, monkeypatch):
    ctx = _summary_context(home, monkeypatch)
    developer = {"type": "response_item", "payload": {"type": "message", "role": "developer",
                                                      "content": [{"type": "input_text", "text": ctx}]}}
    rows = [
        {"type": "session_meta", "payload": {"id": "rollout-fake", "cwd": "/work/project-a"}},
        developer,  # hook context as Codex records it: a developer message
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Ship the release notes"}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "Shipping."}},
    ]
    msgs, _off, _users = sc.read_window(_write_jsonl(home["tmp"] / "codex.jsonl", rows), 0)
    rendered = sc.render(msgs)
    assert "Ship the release notes" in rendered
    _assert_no_routing(rendered, ctx)
    # A desktop rollout that writes only response_item messages drops it too.
    rows = [developer, {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                             "content": [{"type": "input_text", "text": "Ship it"}]}}]
    msgs, _off, _users = sc.read_window(_write_jsonl(home["tmp"] / "codex-app.jsonl", rows), 0)
    rendered = sc.render(msgs)
    assert "Ship it" in rendered
    _assert_no_routing(rendered, ctx)
