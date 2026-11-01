"""Owner changes held for the owner to confirm (the owner's rule, 2026-11-01):
an agent asks, Khipu holds the change and says what to ask ("Confirm ...?"),
and the confirm applies it only when the turn the owner's reply started is
proved: outside T3 the current turn the direction check verifies, later than
the latest ask Khipu recorded for that conversation and never a late copy; in
T3 a call from the conversation's own harness with an owner message in T3's
record that the thread's ask snapshot does not hold. No age limit, no codes,
no word matching.

Hermetic: a temp data dir and capture home, and a synthetic T3 database
(``KHIPU_T3_DB``) with provider-thread, thread and message tables. Nothing
here reads T3's real database or starts a harness."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone

import pytest

from khipu import owner_act, owner_direction, owner_turns, routing

ACCOUNTS = [{"label": "Claude", "harness": "claude"}, {"label": "Codex", "harness": "codex"},
            {"label": "Grok", "harness": "grok"}]
T3_SESSION, THREAD = "s-t3-owner", "t3-owner-thread"
OTHER_SESSION, OTHER_THREAD = "s-t3-other", "t3-other-thread"
_ENV_VARS = ("KHIPU_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
             "KHIPU_HARNESS", "GROK_HOOK_NAME", "GROK_HOOK_EVENT", "KHIPU_JOB", "KHIPU_PARENT_SESSION",
             "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_DISPATCHED_LANE")
DAY = 24 * 3600.0
OWNER_WORDS = "yes, confirm the Codex pause"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class T3:
    def __init__(self, path):
        self.path = path
        self.n = 0
        with sqlite3.connect(path) as con:
            con.execute("CREATE TABLE orchestration_v2_projection_provider_threads "
                        "(thread_id TEXT, payload_json TEXT, updated_at TEXT)")
            con.execute("CREATE TABLE orchestration_v2_projection_threads (thread_id TEXT, payload_json TEXT)")
            con.execute("CREATE TABLE orchestration_v2_projection_messages (message_id TEXT PRIMARY KEY, "
                        "thread_id TEXT NOT NULL, run_id TEXT, node_id TEXT, role TEXT NOT NULL, "
                        "streaming INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                        "payload_json TEXT NOT NULL)")
        self.thread(THREAD, T3_SESSION)
        self.thread(OTHER_THREAD, OTHER_SESSION)

    def thread(self, thread: str, native: str, *, parent: str | None = None, relationship: str | None = None):
        with sqlite3.connect(self.path) as con:
            con.execute("INSERT INTO orchestration_v2_projection_provider_threads VALUES (?, ?, ?)",
                        (thread, json.dumps({"nativeThreadRef": {"nativeId": native}}), "2026-11-01"))
            lineage = {"parentThreadId": parent, "relationshipToParent": relationship, "rootThreadId": thread}
            con.execute("INSERT INTO orchestration_v2_projection_threads VALUES (?, ?)",
                        (thread, json.dumps({"id": thread, "lineage": lineage})))

    def message(self, thread: str, ts: float, *, text: str = OWNER_WORDS, by: str = "user", source: str = "mobile",
                **extra) -> str:
        self.n += 1
        mid = f"msg-{self.n}"
        payload = {"id": mid, "threadId": thread, "role": "user", "text": text, "createdBy": by,
                   "creationSource": source, "createdAt": _iso(ts), "attachments": [], **extra}
        with sqlite3.connect(self.path) as con:
            con.execute("INSERT INTO orchestration_v2_projection_messages VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)",
                        (mid, thread, "run", "node", "user", _iso(ts), _iso(ts), json.dumps(payload)))
        return mid


@pytest.fixture
def world(tmp_path, monkeypatch, khipu_app):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": ACCOUNTS}))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    db = tmp_path / "statev2.sqlite"
    t3db = T3(db)
    monkeypatch.setenv("KHIPU_T3_DB", str(db))
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    khipu_app.as_shell("/opt/harness/claude")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    owner_turns.write_settings({"enabled": True})
    return {"tmp": tmp_path, "t3": t3db, "mp": monkeypatch}


def _be(world, session: str, var: str = "CLAUDE_CODE_SESSION_ID", **env) -> None:
    for name in _ENV_VARS:
        world["mp"].delenv(name, raising=False)
    world["mp"].setenv(var, session)
    for name, value in env.items():
        world["mp"].setenv(name, value)


def _t3_hook_turn(session: str = T3_SESSION, *, ago: float = 0.0, prompt: str = OWNER_WORDS, **env) -> None:
    """T3's Agent SDK runs Claude Code's prompt hook (entrypoint sdk-ts)."""
    owner_turns.record({"session_id": session, "prompt": prompt, "cwd": "/repo"},
                       {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "sdk-ts", **env}, now=time.time() - ago)


def _rollout(world, originator: str):
    path = world["tmp"] / f"rollout-{originator}.jsonl"
    path.write_text(json.dumps({"type": "session_meta", "payload": {"originator": originator}}) + "\n")
    return path


def _front_end_turn(world, front: str, session: str, *, ago: float = 0.0) -> None:
    """The owner types in a non-T3 front end: what its prompt hook records."""
    payload = {"session_id": session, "prompt": OWNER_WORDS, "cwd": "/repo"}
    if front.startswith("codex:"):
        payload["transcript_path"] = str(_rollout(world, front.split(":", 1)[1]))
        env = {"CODEX_THREAD_ID": session}
    else:
        env = {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": front.split(":", 1)[1]}
    owner_turns.record(payload, env, now=time.time() - ago)


def _be_front(world, front: str, session: str) -> None:
    _be(world, session, "CODEX_THREAD_ID" if front.startswith("codex:") else "CLAUDE_CODE_SESSION_ID")


def _paused() -> bool:
    return next(a for a in routing.read_view()["accounts"] if a["key"] == "codex|")["state"] == "paused"


def _hold_in_t3(world, *, ago: float = 0.0) -> str:
    """The agent in the owner's T3 conversation asks for a pause: it is held."""
    _t3_hook_turn(ago=ago)
    _be(world, T3_SESSION)
    real = owner_direction._clock
    world["mp"].setattr(owner_direction, "_clock", lambda: real() - ago)
    try:
        ok, out = owner_act.act("route.pause", {"account": "Codex"})
    finally:
        world["mp"].setattr(owner_direction, "_clock", real)
    assert ok and out["pending"] is True and out["applied"] is False, out
    assert out["ask"] == "Confirm route.pause (account: Codex)?"
    assert "Confirm route.pause (account: Codex)?" in out["line"] and out["request"] in out["line"]
    assert not _paused()
    return out["request"]


def _confirm_id(request: str):
    return owner_act.act("direction.confirm", {"request": request})


# ---- T3: the owner's message in T3's record decides ----------------------------------------------


def test_a_direct_action_in_t3_is_held_not_applied(world):
    request = _hold_in_t3(world)
    (row,) = owner_direction._read_requests(owner_direction.requests_path())
    assert (row["id"], row["action"], row["args"], row["state"]) == (request, "route.pause", {"account": "Codex"},
                                                                     "pending")
    assert owner_direction.actions() == []


def test_an_owner_message_after_a_three_day_old_request_confirms_it(world):
    request = _hold_in_t3(world, ago=3 * DAY)
    mid = world["t3"].message(THREAD, time.time() - 5)
    _t3_hook_turn()
    ok, out = _confirm_id(request)
    assert ok, out
    assert _paused()
    assert out["confirmed"]["message"] == mid and out["confirmed"]["source"] == "t3"
    (row,) = owner_direction._read_requests(owner_direction.requests_path())
    assert row["state"] == "applied"
    # Applied once: a second confirm is refused.
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "not_pending"


def test_an_owner_message_only_before_the_request_is_refused(world):
    world["t3"].message(THREAD, time.time() - 120)
    request = _hold_in_t3(world)
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "not_confirmed" and "Ask the owner to confirm now" in out["error"]
    assert not _paused()


@pytest.mark.parametrize("by, source, extra", [
    ("agent", "mcp", {"scheduledTaskId": "task-1"}),               # a scheduled wake
    ("agent", "mcp", {"senderThreadId": "t3-sender"}),             # a relay from another conversation
    ("agent", "provider", {"notification": {"kind": "command"}}),  # a background notification
    ("user", "server", {}),                                        # "Continue where you left off."
    ("user", "server", {"delegatedCompletion": {"taskId": "x"}}),  # a child's completion
    ("user", "mobile", {"scheduledTaskId": "task-2"}),             # owner-shaped, but a wake key
], ids=["wake", "relay", "notification", "resume", "completion", "wake-key"])
def test_machine_messages_after_the_request_never_confirm(world, by, source, extra):
    request = _hold_in_t3(world)
    world["t3"].message(THREAD, time.time() + 1, text="Continue where you left off.", by=by, source=source, **extra)
    _t3_hook_turn()
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "not_confirmed", out
    assert not _paused()


def test_an_owner_message_skewed_ahead_of_khipus_clock_before_the_request_is_refused(world):
    # Review 2026-11-01 P1: T3 stamped the owner's earlier message 60 s ahead of
    # Khipu's clock. It was in the thread when the request was held, so it
    # never confirms; only a new owner message does.
    world["t3"].message(THREAD, time.time() + 60)
    request = _hold_in_t3(world)
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "not_confirmed", out
    assert not _paused()
    mid = world["t3"].message(THREAD, time.time() + 90)
    ok, out = _confirm_id(request)
    assert ok, out
    assert _paused() and out["confirmed"]["message"] == mid


def test_500_machine_rows_after_the_owners_yes_do_not_hide_it(world):
    # Review 2026-11-01 P2: machine traffic must not push the owner's message
    # out of the bounded read.
    request = _hold_in_t3(world)
    mid = world["t3"].message(THREAD, time.time() + 1)
    for i in range(500):
        world["t3"].message(THREAD, time.time() + 2 + i / 1000, text="wake", by="agent", source="mcp",
                            scheduledTaskId=f"task-{i}")
    _t3_hook_turn()
    ok, out = _confirm_id(request)
    assert ok, out
    assert _paused() and out["confirmed"]["message"] == mid


def test_an_owner_message_in_a_different_thread_is_refused(world):
    request = _hold_in_t3(world)
    world["t3"].message(OTHER_THREAD, time.time() + 1)
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "not_confirmed"
    assert not _paused()


def test_an_ambiguous_thread_is_refused(world):
    request = _hold_in_t3(world)
    world["t3"].message(THREAD, time.time() + 1)
    world["t3"].thread("t3-second-thread", T3_SESSION)
    ok, out = _confirm_id(request)
    assert not ok and "more than one thread" in out["error"]
    assert not _paused()


def test_an_unreadable_t3_database_is_refused(world, monkeypatch):
    from khipu import t3

    request = _hold_in_t3(world)
    world["t3"].message(THREAD, time.time() + 1)
    real = t3.owner_reply_read
    monkeypatch.setattr(t3, "owner_reply_read", lambda thread, seen, since: (None, False))
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "t3_unreadable"
    monkeypatch.setattr(t3, "owner_reply_read", real)
    world["t3"].path.write_bytes(b"not a database")
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "t3_unreadable", out
    assert not _paused()


def test_the_message_reader_on_a_broken_file_is_not_definitive(world):
    from khipu import t3

    world["t3"].path.write_bytes(b"not a database")
    assert t3.owner_reply_read(THREAD, (), None) == (None, False)


@pytest.mark.parametrize("thread, parent, relationship", [
    ("thread:delegated-task:command%3Ajob", None, None),
    ("thread:provider:child", THREAD, "subagent"),
    ("thread:provider:fork", THREAD, "fork"),
], ids=["delegated-id", "lineage-subagent", "lineage-fork"])
def test_a_delegated_or_child_thread_with_an_owner_row_is_refused(world, thread, parent, relationship):
    request = _hold_in_t3(world)
    world["t3"].thread(thread, "s-child", parent=parent, relationship=relationship)
    world["t3"].message(thread, time.time() + 1)
    _t3_hook_turn("s-child")
    assert owner_turns.turns("s-child")[-1]["dispatched"] is True
    _be(world, "s-child")
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "no_recorded_turn" and "dispatched job" in out["error"], out
    ok, out = owner_act.act("route.pause", {"account": "Codex"})
    assert not ok and out["code"] == "no_recorded_turn"
    assert not _paused()


def test_a_top_level_thread_is_not_a_child():
    from khipu import t3

    assert not t3._names_parent({"lineage": {"parentThreadId": None, "relationshipToParent": None,
                                             "rootThreadId": "t"}})
    assert t3._names_parent({"lineage": {"parentThreadId": "p", "relationshipToParent": "subagent"}})


def test_a_dispatched_call_cannot_hold_or_confirm(world):
    request = _hold_in_t3(world)
    world["t3"].message(THREAD, time.time() + 1)
    _be(world, T3_SESSION, CLAUDE_DISPATCHED_LANE="claude-second")
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "no_recorded_turn"
    ok, out = owner_act.act("direction.request", {"action": "route.pause", "args": {"account": "Codex"}})
    assert not ok and out["code"] == "no_recorded_turn"
    assert not _paused()


# ---- every other trusted front end: the hook's eligible turn decides -----------------------------

FRONTS = ["claude_code:cli", "claude_code:claude-desktop", "codex:codex_desktop"]


def _hold_by_request(world, front: str, session: str, *, ago: float) -> str:
    _front_end_turn(world, front, session, ago=ago)
    _be_front(world, front, session)
    real = owner_direction._clock
    world["mp"].setattr(owner_direction, "_clock", lambda: real() - ago)
    try:
        ok, out = owner_act.act("direction.request", {"action": "route.pause", "args": {"account": "Codex"}})
    finally:
        world["mp"].setattr(owner_direction, "_clock", real)
    assert ok and out["pending"] is True, out
    return out["request"]


@pytest.mark.parametrize("front", FRONTS)
def test_a_two_day_old_request_confirms_on_a_later_owner_turn(world, front):
    # The turn the request was made on is past the recorder's retention by now.
    request = _hold_by_request(world, front, "s-front", ago=2 * DAY)
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "no_recorded_turn" and not _paused()
    _front_end_turn(world, front, "s-front")
    ok, out = _confirm_id(request)
    assert ok, out
    assert _paused() and out["confirmed"]["source"] == "hook"


@pytest.mark.parametrize("front", FRONTS)
def test_a_two_day_old_owner_turn_is_past_retention_so_the_confirm_asks_again(world, front):
    # The proof is the current turn, so turns keep the normal one-day
    # retention: an older turn is gone, and the confirm is refused until the
    # owner types again.
    request = _hold_by_request(world, front, "s-front", ago=3 * DAY)
    _front_end_turn(world, front, "s-front", ago=2 * DAY)
    owner_turns.sweep()
    assert owner_turns.turns("s-front") == []
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "no_recorded_turn" and not _paused()
    _front_end_turn(world, front, "s-front")
    ok, out = _confirm_id(request)
    assert ok, out
    assert _paused() and out["confirmed"]["source"] == "hook"


@pytest.mark.parametrize("front", FRONTS)
def test_only_a_turn_older_than_the_request_is_refused(world, front):
    request = _hold_by_request(world, front, "s-front", ago=2 * 3600)
    _front_end_turn(world, front, "s-front", ago=3 * 3600)
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "not_confirmed" and "Ask the owner to confirm now" in out["error"]
    assert not _paused()


@pytest.mark.parametrize("front", ["claude_code:cli", "codex:codex_desktop"])
def test_a_t3_request_confirms_from_claude_code_or_codex(world, front):
    # No ask was made in the confirming conversation yet: its first confirm
    # records one, and the owner's reply after it confirms.
    request = _hold_in_t3(world, ago=DAY)
    _front_end_turn(world, front, "s-elsewhere")
    _be_front(world, front, "s-elsewhere")
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "not_confirmed" and "Ask the owner to confirm now" in out["error"]
    assert not _paused()
    _front_end_turn(world, front, "s-elsewhere")
    ok, out = _confirm_id(request)
    assert ok, out
    assert _paused() and out["confirmed"]["source"] == "hook"


@pytest.mark.parametrize("front", ["claude_code:cli", "codex:codex_desktop"])
def test_a_claude_code_or_codex_request_confirms_from_t3_after_its_snapshot(world, front):
    # Review 2026-11-01 (cross-conversation): T3 holds no snapshot for a request
    # made elsewhere, so the first confirm only takes one; an owner message
    # already in the thread never confirms.
    request = _hold_by_request(world, front, "s-elsewhere", ago=DAY)
    world["t3"].message(THREAD, time.time() - 1)
    _t3_hook_turn()
    _be(world, T3_SESSION)
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "not_confirmed" and "Ask the owner to confirm now" in out["error"], out
    assert not _paused()
    (row,) = owner_direction._read_requests(owner_direction.requests_path())
    assert row["state"] == "pending" and list(row["t3_snapshots"]) == [THREAD]
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "not_confirmed" and not _paused()
    mid = world["t3"].message(THREAD, time.time() + 1)
    ok, out = _confirm_id(request)
    assert ok, out
    assert _paused() and out["confirmed"]["source"] == "t3" and out["confirmed"]["message"] == mid


@pytest.mark.parametrize("front", ["claude_code:cli", "claude_code:claude-desktop", "codex:codex_cli_rs",
                                   "codex:codex_desktop"])
def test_direct_actions_from_claude_code_and_codex_still_apply_at_once(world, front):
    _front_end_turn(world, front, "s-direct")
    _be_front(world, front, "s-direct")
    ok, out = owner_act.act("route.pause", {"account": "Codex"})
    assert ok and not out.get("pending"), out
    assert _paused()
    assert not owner_direction.requests_path().exists()


def test_a_direct_action_outside_the_window_is_still_refused_as_today(world):
    _front_end_turn(world, "claude_code:cli", "s-direct", ago=7 * 3600)
    _be(world, "s-direct")
    ok, out = owner_act.act("route.pause", {"account": "Codex"})
    assert not ok and not _paused()


# ---- the audit line ---------------------------------------------------------------------------------


def test_the_audit_line_names_the_confirming_message_by_id_time_and_hash_only(world):
    request = _hold_in_t3(world, ago=3600)
    words = "Yes, go ahead and pause Codex — confirmed"
    when = time.time() - 2
    mid = world["t3"].message(THREAD, when, text=words)
    _t3_hook_turn()
    ok, out = _confirm_id(request)
    assert ok, out
    (line,) = owner_direction.actions()
    canonical = " ".join(words.split())
    assert line["action"] == "route.pause" and line["auth"] == "direction"
    assert line["confirmed"] == {"source": "t3", "message": mid, "message_at": _iso(when), "request": request,
                                 "message_sha256": hashlib.sha256(canonical.encode()).hexdigest()}
    assert words not in owner_direction.log_path().read_text()
    assert words not in owner_direction.requests_path().read_text()


# ---- Forget -------------------------------------------------------------------------------------------


def test_forget_withdraws_a_held_request_and_removes_its_words(world):
    # Review 2026-11-01 P2: owner-word cleanup covers the session's held requests.
    from khipu import forget

    request = _hold_in_t3(world)
    out = forget._owner_words_scan(T3_SESSION, gateway=False)
    assert out["ok"], out
    assert out["removed"]["requests_withdrawn"] == 1 and out["removed"]["requests_scrubbed"] == 0
    (row,) = owner_direction._read_requests(owner_direction.requests_path())
    assert (row["id"], row["state"], row["reason"]) == (request, "withdrawn", "forgotten")
    assert "args" not in row and "ask" not in row
    assert "Codex" not in owner_direction.requests_path().read_text()
    world["t3"].message(THREAD, time.time() + 1)
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "not_pending" and not _paused()


def test_forget_removes_a_settled_requests_words_and_counts_it(world):
    from khipu import forget

    request = _hold_by_request(world, "claude_code:cli", "s-front", ago=3600)
    _front_end_turn(world, "claude_code:cli", "s-front")
    ok, out = _confirm_id(request)
    assert ok, out
    out = forget._owner_words_scan("claude_code:s-front", gateway=False)
    assert out["ok"] and out["removed"]["requests_scrubbed"] == 1 and out["removed"]["requests_withdrawn"] == 0
    (row,) = owner_direction._read_requests(owner_direction.requests_path())
    assert row["state"] == "applied" and "args" not in row and "ask" not in row
    assert "Codex" not in owner_direction.requests_path().read_text()


def test_a_request_pending_past_30_days_stays_pending(world):
    request = _hold_by_request(world, "claude_code:cli", "s-front", ago=45 * DAY)
    owner_turns.sweep()
    _front_end_turn(world, "claude_code:cli", "s-front")
    ok, out = _confirm_id(request)
    assert ok, out


# ---- review 2026-11-01 (Sol re-check of 6686fc1d): the seven scenarios ---------------------------


def test_review_p1_a_delayed_duplicate_of_the_held_turn_never_confirms(world):
    # A delayed duplicate turn (new id, later ts, same occurrence,
    # duplicate_of=held_turn; e.g. PostInvocation flush) must not pass the
    # held turn's exclusion: there is no new owner message.
    _front_end_turn(world, "claude_code:cli", "s-front", ago=60)
    (held,) = owner_turns.turns("s-front")
    _be(world, "s-front")
    ok, out = owner_act.act("direction.request", {"action": "route.pause", "args": {"account": "Codex"}})
    assert ok and out["pending"], out
    owner_turns.record({"session_id": "s-front", "prompt": OWNER_WORDS, "cwd": "/repo"},
                       {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"}, now=time.time() + 1,
                       extra={"duplicate_of": held["turn"], "occurrence": held["occurrence"]})
    dup = owner_turns.turns("s-front", time.time() + 2)[-1]
    assert dup["turn"] != held["turn"] and dup["occurrence"] == held["occurrence"] and dup["eligible"], dup
    ok, out = _confirm_id(out["request"])
    assert not ok and out["code"] == "not_confirmed", out
    assert not _paused()


def test_a_copy_sharing_an_excluded_turns_occurrence_never_confirms(world):
    _front_end_turn(world, "claude_code:cli", "s-front", ago=60)
    (held,) = owner_turns.turns("s-front")
    _be(world, "s-front")
    ok, out = owner_act.act("direction.request", {"action": "route.pause", "args": {"account": "Codex"}})
    owner_turns.record({"session_id": "s-front", "prompt": OWNER_WORDS, "cwd": "/repo"},
                       {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"}, now=time.time() + 1,
                       extra={"occurrence": held["occurrence"]})
    ok, out = _confirm_id(out["request"])
    assert not ok and out["code"] == "not_confirmed", out
    assert not _paused()


def test_review_p2_forget_while_applying_then_a_failed_command_keeps_the_request_scrubbed(world, monkeypatch):
    # Forget during the applying window, then the command fails: settle() must
    # not write the arg back via the error.
    from khipu import forget

    request = _hold_by_request(world, "claude_code:cli", "s-front", ago=3600)
    _front_end_turn(world, "claude_code:cli", "s-front")

    def run(action, full):
        assert forget._owner_words_scan("claude_code:s-front", gateway=False)["ok"]
        return False, {"ok": False, "error": "Codex: account Codex can't be paused", "code": "bad_account"}

    monkeypatch.setattr(owner_act, "_run", run)
    ok, out = _confirm_id(request)
    assert not ok
    (row,) = owner_direction._read_requests(owner_direction.requests_path())
    assert row["state"] == "failed" and row["error_code"] == "bad_account", row
    assert "args" not in row and "ask" not in row and "asks" not in row
    assert "Codex" not in owner_direction.requests_path().read_text()


def test_a_t3_confirm_on_a_two_day_old_request_needs_the_replys_hook_turn(world):
    # T3's record proves the reply; the hook turn the reply records binds the
    # call to the conversation's harness, so a 2-day-old hold needs no older turn.
    request = _hold_in_t3(world, ago=2 * DAY)
    assert owner_turns.turns(T3_SESSION) == []
    mid = world["t3"].message(THREAD, time.time() + 1)
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "no_recorded_turn" and not _paused()
    _t3_hook_turn()
    ok, out = _confirm_id(request)
    assert ok, out
    assert _paused() and out["confirmed"]["message"] == mid


def test_review_p2_the_30_day_cap_no_longer_deletes_proof_of_a_waiting_request(world):
    # The 30-day evidence cap deleted proof while the request stayed pending.
    # Now proof is the current turn: refused until the owner types, then it passes.
    request = _hold_by_request(world, "claude_code:cli", "s-front", ago=35 * DAY)
    _front_end_turn(world, "claude_code:cli", "s-front", ago=31 * DAY)
    owner_turns.sweep()
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "no_recorded_turn", out
    (row,) = owner_direction._read_requests(owner_direction.requests_path())
    assert row["state"] == "pending"
    _front_end_turn(world, "claude_code:cli", "s-front")
    ok, out = _confirm_id(request)
    assert ok, out
    assert _paused()


def test_review_p2_500_workflow_script_rows_after_the_owners_yes_do_not_hide_it(world):
    # The SQL filter didn't mirror is_owner_message (workflowScript): 500 such
    # rows filled the limit and hid the proof.
    request = _hold_in_t3(world)
    mid = world["t3"].message(THREAD, time.time() + 1)
    for i in range(500):
        world["t3"].message(THREAD, time.time() + 2 + i / 1000, text="run step", workflowScript=f"step-{i}")
    ok, out = _confirm_id(request)
    assert ok, out
    assert _paused() and out["confirmed"]["message"] == mid


@pytest.mark.parametrize("payload, owner", [
    ({"workflowScript": "x"}, False), ({"workflowScript": ""}, True), ({"workflowScript": {}}, True),
    ({"notification": "{}"}, False), ({"notification": []}, True), ({"notification": None}, True),
    ({"notification": 0}, False), ({"scheduledTaskId": False}, False), ({"threadId": OTHER_THREAD}, False),
    ({"threadId": None}, True), ({"text": 5}, False), ({"creationSource": "server"}, False),
    ({"createdBy": "agent"}, False), ({}, True),
])
def test_the_sql_filter_and_is_owner_message_agree(world, payload, owner):
    from khipu import t3

    mid = world["t3"].message(THREAD, time.time(), **payload)
    with sqlite3.connect(world["t3"].path) as con:
        (raw,) = con.execute("SELECT payload_json FROM orchestration_v2_projection_messages WHERE message_id = ?",
                             (mid,)).fetchone()
        sql = con.execute("SELECT COUNT(*) FROM orchestration_v2_projection_messages WHERE message_id = :mid "
                          f"AND thread_id = :thread AND {t3._owner_rows_sql()}",
                          {"mid": mid, "thread": THREAD}).fetchone()[0]
    assert t3.is_owner_message(json.loads(raw), THREAD) is owner
    assert bool(sql) is owner


def test_review_p2_forget_removes_t3_snapshots_and_ask_boundaries(world):
    # Forget left t3_snapshots (thread id, message ids, time).
    from khipu import forget

    world["t3"].message(THREAD, time.time() - 60)
    _hold_in_t3(world)
    other = _hold_by_request(world, "claude_code:cli", "s-front", ago=60)
    assert forget._owner_words_scan(T3_SESSION, gateway=False)["ok"]
    assert forget._owner_words_scan("claude_code:s-front", gateway=False)["ok"]
    rows = owner_direction._read_requests(owner_direction.requests_path())
    assert all("t3_snapshots" not in r and "asks" not in r for r in rows), rows
    text = owner_direction.requests_path().read_text()
    assert THREAD not in text and "msg-1" not in text
    assert {r["id"] for r in rows if r["state"] == "withdrawn"} >= {other}


@pytest.mark.parametrize("where", ["t3", "claude_code:cli"])
def test_review_p3_hold_refuses_when_owner_direction_is_off(world, where):
    if where == "t3":
        _t3_hook_turn()
        _be(world, T3_SESSION)
    else:
        _front_end_turn(world, where, "s-front")
        _be(world, "s-front")
    owner_turns.write_settings({"enabled": False})
    with pytest.raises(owner_direction.DirectionRefused) as refused:
        owner_direction.hold("route.pause", {"account": "Codex"})
    assert refused.value.code == "direction_off"
    with pytest.raises(owner_direction.DirectionRefused) as checked:
        owner_direction.check("route.pause")
    assert refused.value.message == checked.value.message
    assert not owner_direction.requests_path().exists()


# ---- the ask boundary ------------------------------------------------------------------------------


def test_the_owner_replies_after_an_ask_on_a_40_day_old_request_outside_t3(world):
    request = _hold_by_request(world, "claude_code:cli", "s-front", ago=40 * DAY)
    owner_turns.sweep()
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "no_recorded_turn"
    _front_end_turn(world, "claude_code:cli", "s-front")
    ok, out = _confirm_id(request)
    assert ok, out
    assert _paused() and out["confirmed"]["source"] == "hook"


def test_the_owner_replies_after_an_ask_on_a_40_day_old_request_in_t3(world):
    request = _hold_in_t3(world, ago=40 * DAY)
    owner_turns.sweep()
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "no_recorded_turn"
    mid = world["t3"].message(THREAD, time.time() + 1)
    _t3_hook_turn()
    ok, out = _confirm_id(request)
    assert ok, out
    assert _paused() and out["confirmed"]["message"] == mid


def test_an_owner_turn_after_the_hold_but_before_the_latest_ask_is_refused(world):
    request = _hold_by_request(world, "claude_code:cli", "s-front", ago=3600)
    _front_end_turn(world, "claude_code:cli", "s-other", ago=60)   # after the hold
    _be(world, "s-other")
    ok, out = _confirm_id(request)                                 # the latest ask here
    assert not ok and "Ask the owner to confirm now" in out["error"]
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "not_confirmed" and not _paused()
    _front_end_turn(world, "claude_code:cli", "s-other")
    ok, out = _confirm_id(request)
    assert ok, out
    assert _paused()


def test_a_confirm_with_no_reply_moves_the_ask_boundary(world):
    request = _hold_by_request(world, "claude_code:cli", "s-front", ago=3600)
    (before,) = owner_direction._read_requests(owner_direction.requests_path())
    ok, out = _confirm_id(request)
    assert not ok
    (after,) = owner_direction._read_requests(owner_direction.requests_path())
    assert after["asks"]["s-front"]["ts"] > before["asks"]["s-front"]["ts"]


# ---- review 2026-11-01 (Astra whole-branch review at add8406d): the six scenarios ----------------
#
# The proof is the current turn (outside T3) or a new owner message in T3's
# record, never a search of stored turns; boundaries only grow.

AG_ISSUER = {"pid": 4_000_000, "start": 1.0, "exe": "language_server_macos_arm", "app": "Antigravity.app",
             "headless": False, "tty": False}


def _ag_transcript(world, session: str):
    path = world["tmp"] / ".gemini" / "antigravity" / "brain" / session / ".system_generated" / "logs" / "transcript.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return path


def _ag_payload(path, session: str, **extra) -> dict:
    return {"conversationId": session, "workspacePaths": ["/repo"], "transcriptPath": str(path),
            "invocationNum": 1, "initialNumSteps": 0, **extra}


def _ag_turn(world, session: str, prompt: str = OWNER_WORDS, *, ago: float = 0.0) -> None:
    """The owner types in the Antigravity App: what its prompt hook records."""
    path = _ag_transcript(world, session)
    owner_turns.record({**_ag_payload(path, session), "prompt": prompt}, env={}, now=time.time() - ago,
                       issuer=dict(AG_ISSUER))


def test_review2_p1_a_duplicate_after_the_prune_never_confirms(world):
    # A confirm after the 24 h prune replaced the boundary with the turns then
    # kept, losing the held turn's id and occurrence; a delayed duplicate of it
    # then confirmed.
    _front_end_turn(world, "claude_code:cli", "s-front", ago=25 * 3600)
    (held,) = owner_turns.turns("s-front", time.time() - 25 * 3600 + 1)
    _be(world, "s-front")
    real = owner_direction._clock
    world["mp"].setattr(owner_direction, "_clock", lambda: real() - 25 * 3600 + 2)
    ok, out = owner_act.act("direction.request", {"action": "route.pause", "args": {"account": "Codex"}})
    world["mp"].setattr(owner_direction, "_clock", real)
    assert ok and out["pending"], out
    request = out["request"]
    assert owner_turns.turns("s-front") == []
    ok, out = _confirm_id(request)
    assert not ok and not _paused()
    owner_turns.record({"session_id": "s-front", "prompt": OWNER_WORDS, "cwd": "/repo"},
                       {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"}, now=time.time() + 1,
                       extra={"duplicate_of": held["turn"], "occurrence": held["occurrence"]})
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "not_confirmed", out
    assert not _paused()
    (row,) = owner_direction._read_requests(owner_direction.requests_path())
    assert held["turn"] in {e[0] for e in row["asks"]["s-front"]["turns"]}


def test_review2_p1_an_antigravity_postinvocation_flush_copy_never_confirms(world):
    # The turn was recorded from lastUserInput at PreInvocation; its flushed
    # step, recorded at PostInvocation, could not be paired with it
    # (occurrence 2, no duplicate_of) and confirmed as a new owner reply.
    session = "ag-owner"
    path = _ag_transcript(world, session)
    owner_turns.antigravity_prompt(_ag_payload(path, session, initialNumSteps=1, lastUserInput=OWNER_WORDS),
                                   now=time.time() - 30, wait=0, issuer=dict(AG_ISSUER))
    (first,) = owner_turns.turns(session)
    assert first["fallback"] and first["eligible"], first
    _be(world, session, "KHIPU_SESSION_ID")
    ok, out = owner_act.act("direction.request", {"action": "route.pause", "args": {"account": "Codex"}})
    assert ok and out["pending"], out
    path.write_text(json.dumps({"type": "USER_INPUT", "step_index": 0, "content": OWNER_WORDS}) + "\n")
    assert owner_turns.antigravity_late_turn(_ag_payload(path, session), "PostInvocation",
                                             now=time.time() + 1, issuer=dict(AG_ISSUER))
    copy = owner_turns.turns(session, time.time() + 2)[-1]
    assert copy["occurrence"] == 2 and "duplicate_of" not in copy and copy["eligible"], copy
    ok, out = _confirm_id(out["request"])
    assert not ok and out["code"] == "not_confirmed", out
    assert not _paused()


def test_review2_p2_a_t3_confirm_from_a_process_inheriting_the_session_is_refused(world):
    # A different harness (its own PID and start) that inherits the T3
    # session's id is not the hook that recorded the conversation's turn.
    request = _hold_in_t3(world)
    world["t3"].message(THREAD, time.time() + 1)
    _t3_hook_turn()
    world["mp"].setattr(owner_direction, "_caller_chain", lambda: [(4_200_000, 7.0, "claude")])
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "not_from_the_hook", out
    assert not _paused()


def test_review2_p2_a_t3_confirm_needs_t3_trusted_now(world):
    request = _hold_in_t3(world)
    world["t3"].message(THREAD, time.time() + 1)
    _t3_hook_turn()
    owner_turns.write_settings({"trusted": []})
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "not_confirmed" and "claude_code:t3" in out["error"], out
    assert not _paused()


def test_review2_p2_forget_scrubs_a_conversations_boundaries_on_another_conversations_request(world):
    from khipu import forget

    request = _hold_by_request(world, "claude_code:cli", "s-front", ago=60)
    _front_end_turn(world, "claude_code:cli", "s-other", ago=30)
    _be(world, "s-other")
    ok, out = _confirm_id(request)                      # records s-other's ask on s-front's request
    assert not ok
    world["t3"].message(THREAD, time.time() - 20)
    _t3_hook_turn()
    _be(world, T3_SESSION)
    ok, out = _confirm_id(request)                      # records the T3 thread's snapshot on it
    assert not ok
    (row,) = owner_direction._read_requests(owner_direction.requests_path())
    assert set(row["asks"]) == {"s-front", "s-other"} and list(row["t3_snapshots"]) == [THREAD]
    assert forget._owner_words_scan("claude_code:s-other", gateway=False)["ok"]
    assert forget._owner_words_scan(T3_SESSION, gateway=False)["ok"]
    (row,) = owner_direction._read_requests(owner_direction.requests_path())
    assert row["state"] == "pending" and list(row["asks"]) == ["s-front"], row
    assert THREAD not in row.get("t3_snapshots", {}) and "msg-1" not in owner_direction.requests_path().read_text()


def test_review2_p2_a_hold_racing_forget_never_writes_its_words_back(world):
    import threading

    from khipu import forget

    _front_end_turn(world, "claude_code:cli", "s-front")
    _be(world, "s-front")
    real_ask = owner_direction.ask_line
    done = threading.Event()

    def racing(action, args):
        # Forget runs to the end while the hold is between admission and save.
        worker = threading.Thread(target=lambda: (forget._owner_words_scan("claude_code:s-front", gateway=False),
                                                  done.set()))
        worker.start()
        worker.join(1.0)
        return real_ask(action, args)

    world["mp"].setattr(owner_direction, "ask_line", racing)
    owner_act.act("direction.request", {"action": "route.pause", "args": {"account": "Codex"}})
    assert done.wait(5.0)
    rows = owner_direction._read_requests(owner_direction.requests_path())
    assert all(r["state"] == "withdrawn" and "args" not in r and "ask" not in r for r in rows), rows
    assert "Codex" not in owner_direction.requests_path().read_text()


def test_review2_p2_a_reply_landing_during_a_no_reply_confirm_is_not_swallowed(world):
    # The new snapshot came from a second read, so an owner message landing
    # between the no-reply read and it was taken into the snapshot.
    from khipu import t3

    request = _hold_in_t3(world)
    real = t3._read_messages
    landed = []

    def read_then_land(thread_id, read):
        out = real(thread_id, read)
        if not landed:
            landed.append(world["t3"].message(THREAD, time.time() + 1))
        return out

    world["mp"].setattr(t3, "_read_messages", read_then_land)
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "not_confirmed", out
    world["mp"].setattr(t3, "_read_messages", real)
    _t3_hook_turn()
    ok, out = _confirm_id(request)
    assert ok, out
    assert _paused() and out["confirmed"]["message"] == landed[0]


# ---- the current turn from each front end ---------------------------------------------------------


@pytest.mark.parametrize("front", ["claude_code:cli", "codex:codex_desktop", "antigravity:app"])
def test_a_confirm_from_the_current_turn_of_each_front_end(world, front):
    if front.startswith("antigravity:"):
        _ag_turn(world, "s-cur", ago=60)
        _be(world, "s-cur", "KHIPU_SESSION_ID")
    else:
        _front_end_turn(world, front, "s-cur", ago=60)
        _be_front(world, front, "s-cur")
    assert owner_turns.turns("s-cur")[-1]["eligible"], owner_turns.turns("s-cur")
    ok, out = owner_act.act("direction.request", {"action": "route.pause", "args": {"account": "Codex"}})
    assert ok and out["pending"], out
    request = out["request"]
    ok, out = _confirm_id(request)                       # the same turn as the ask
    assert not ok and out["code"] == "not_confirmed" and not _paused()
    if front.startswith("antigravity:"):
        _ag_turn(world, "s-cur", prompt="yes")
    else:
        _front_end_turn(world, front, "s-cur")
    ok, out = _confirm_id(request)
    assert ok, out
    assert _paused() and out["confirmed"]["source"] == "hook"
    assert out["confirmed"]["message"] == owner_turns.turns("s-cur")[-1]["turn"]


def test_an_older_eligible_turn_never_confirms_when_the_current_turn_is_a_late_copy(world):
    # A real reply recorded after the ask, then a late copy as the current
    # turn: the current turn is what is proved, and it is a copy.
    request = _hold_by_request(world, "claude_code:cli", "s-front", ago=60)
    _front_end_turn(world, "claude_code:cli", "s-front", ago=10)
    (reply,) = [t for t in owner_turns.turns("s-front") if t["ts"] > time.time() - 30]
    owner_turns.record({"session_id": "s-front", "prompt": OWNER_WORDS, "cwd": "/repo"},
                       {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"}, now=time.time(),
                       extra={"duplicate_of": reply["turn"], "occurrence": reply["occurrence"]})
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "not_confirmed" and not _paused()


def test_a_boundary_that_would_drop_a_live_exclusion_refuses(world):
    request = _hold_by_request(world, "claude_code:cli", "s-front", ago=60)
    path = owner_direction.requests_path()
    data = json.loads(path.read_text())
    future = time.time() + 3600
    data["requests"][0]["asks"]["s-front"]["turns"] = [[f"t{i}", future] for i in range(201)]
    path.write_text(json.dumps(data))
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "ask_limit", out
    (row,) = owner_direction._read_requests(path)
    assert len(row["asks"]["s-front"]["turns"]) == 201


# ---- Antigravity's submit-time fallback, and T3 running other agents ------------------------------


def test_an_antigravity_confirm_from_a_submit_time_fallback_turn(world):
    # The transcript is not written yet when the PreInvocation hook runs, so it
    # records the reply from lastUserInput: that is the current turn.
    session = "ag-fallback"
    path = _ag_transcript(world, session)
    _ag_turn(world, session, ago=60)
    _be(world, session, "KHIPU_SESSION_ID")
    ok, out = owner_act.act("direction.request", {"action": "route.pause", "args": {"account": "Codex"}})
    assert ok and out["pending"], out
    request = out["request"]
    owner_turns.antigravity_prompt(_ag_payload(path, session, initialNumSteps=2, lastUserInput="yes"),
                                   now=time.time() + 1, wait=0, issuer=dict(AG_ISSUER))
    reply = owner_turns.turns(session, time.time() + 2)[-1]
    assert reply["fallback"] and reply["ag_step"] == 2 and "late" not in reply and reply["eligible"], reply
    ok, out = _confirm_id(request)
    assert ok, out
    assert _paused() and out["confirmed"]["message"] == reply["turn"]


def test_an_antigravity_fallback_turn_from_before_the_ask_never_confirms(world):
    session = "ag-fallback"
    path = _ag_transcript(world, session)
    owner_turns.antigravity_prompt(_ag_payload(path, session, initialNumSteps=1, lastUserInput=OWNER_WORDS),
                                   now=time.time() - 30, wait=0, issuer=dict(AG_ISSUER))
    _be(world, session, "KHIPU_SESSION_ID")
    ok, out = owner_act.act("direction.request", {"action": "route.pause", "args": {"account": "Codex"}})
    assert ok and out["pending"], out
    ok, out = _confirm_id(out["request"])
    assert not ok and out["code"] == "not_confirmed" and not _paused(), out


def test_an_antigravity_flushed_copy_from_a_later_preinvocation_never_confirms(world):
    # The ask turn was read from lastUserInput; a later PreInvocation of the
    # same turn reads its flushed step. That copy is late even though no
    # PostInvocation or Stop recorded it.
    session = "ag-flush"
    path = _ag_transcript(world, session)
    owner_turns.antigravity_prompt(_ag_payload(path, session, initialNumSteps=1, lastUserInput=OWNER_WORDS),
                                   now=time.time() - 30, wait=0, issuer=dict(AG_ISSUER))
    _be(world, session, "KHIPU_SESSION_ID")
    ok, out = owner_act.act("direction.request", {"action": "route.pause", "args": {"account": "Codex"}})
    assert ok and out["pending"], out
    path.write_text(json.dumps({"type": "USER_INPUT", "step_index": 1, "content": OWNER_WORDS}) + "\n")
    owner_turns.antigravity_prompt(_ag_payload(path, session, initialNumSteps=1, invocationNum=2),
                                   now=time.time() + 1, wait=0, issuer=dict(AG_ISSUER))
    copy = owner_turns.turns(session, time.time() + 2)[-1]
    assert copy.get("late") == "flush" and not copy.get("fallback"), copy
    ok, out = _confirm_id(out["request"])
    assert not ok and out["code"] == "not_confirmed", out
    assert not _paused()


def _t3_agent_turn(harness: str) -> None:
    """The prompt hook of the agent T3 runs (Cursor or Antigravity) records the owner's turn."""
    if harness == "cursor":
        payload = {"hook_event_name": "beforeSubmitPrompt", "conversation_id": T3_SESSION, "session_id": T3_SESSION,
                   "generation_id": "g1", "cursor_version": "3.26.13", "workspace_roots": ["/repo"],
                   "prompt": OWNER_WORDS,
                   "transcript_path": f"/x/.cursor/projects/p/agent-transcripts/{T3_SESSION}.jsonl"}
    else:
        payload = {"conversationId": T3_SESSION, "workspacePaths": ["/repo"], "prompt": OWNER_WORDS,
                   "transcriptPath": f"/x/.gemini/antigravity/brain/{T3_SESSION}/.system_generated/logs/"
                                     "transcript.jsonl"}
    owner_turns.record(payload, env={})


@pytest.mark.parametrize("harness", ["cursor", "antigravity"])
def test_a_t3_confirm_in_a_conversation_t3_runs_on_cursor_or_antigravity(world, harness):
    request = _hold_in_t3(world)
    mid = world["t3"].message(THREAD, time.time() + 1)
    _t3_agent_turn(harness)
    latest = owner_turns.turns(T3_SESSION)[-1]
    assert (latest["harness"], latest["entrypoint"]) == (harness, "t3"), latest
    _be(world, T3_SESSION, "KHIPU_SESSION_ID")
    ok, out = _confirm_id(request)
    assert ok, out
    assert _paused() and out["confirmed"]["source"] == "t3" and out["confirmed"]["message"] == mid


# ---- review 3: Antigravity turn identity, a foreign headless process, a stale step, a tied time -------


def _ag_request(world, session: str, account: str = "Codex") -> str:
    _be(world, session, "KHIPU_SESSION_ID")
    ok, out = owner_act.act("direction.request", {"action": "route.pause", "args": {"account": account}})
    assert ok and out["pending"], out
    return out["request"]


def _ag_user_step(path, index: int, text: str = OWNER_WORDS, *, then: int | None = None) -> None:
    lines = [{"type": "USER_INPUT", "step_index": index, "content": text}]
    if then is not None:
        lines.append({"type": "PLANNER_RESPONSE", "step_index": then, "content": "ok"})
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))


def test_review3_p1a_a_stepless_fallback_read_again_from_the_transcript_never_confirms(world):
    # A PreInvocation with no initialNumSteps records a fallback with no step;
    # the request is held; a later PreInvocation reads the same words from the
    # transcript. Pairing can't bind them, so the copy got occurrence 2 with no
    # late and no duplicate_of, and confirmed.
    session = "ag-p1a"
    path = _ag_transcript(world, session)
    payload = _ag_payload(path, session, lastUserInput=OWNER_WORDS)
    del payload["initialNumSteps"]
    owner_turns.antigravity_prompt(payload, now=time.time() - 30, wait=0, issuer=dict(AG_ISSUER))
    (first,) = owner_turns.turns(session)
    assert first["fallback"] and "ag_step" not in first and first["eligible"], first
    request = _ag_request(world, session)
    _ag_user_step(path, 0)
    owner_turns.antigravity_prompt(_ag_payload(path, session, initialNumSteps=0, invocationNum=2),
                                   now=time.time() + 1, wait=0, issuer=dict(AG_ISSUER))
    copy = owner_turns.turns(session, time.time() + 2)[-1]
    assert copy["occurrence"] == 2 and not copy.get("late") and "duplicate_of" not in copy and copy["eligible"], copy
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "not_confirmed", out
    assert not _paused()


def test_review3_p1b_a_fallback_step_read_again_after_the_prune_never_confirms(world):
    # A fallback with ag_step=1; the request is held; after 25 h a later
    # PreInvocation reads step 1 from the transcript, and pairing searched the
    # pruned history, so the copy confirmed.
    session = "ag-p1b"
    path = _ag_transcript(world, session)
    then = time.time() - 25 * 3600
    owner_turns.antigravity_prompt(_ag_payload(path, session, initialNumSteps=1, lastUserInput=OWNER_WORDS),
                                   now=then, wait=0, issuer=dict(AG_ISSUER))
    real = owner_direction._clock
    world["mp"].setattr(owner_direction, "_clock", lambda: then + 2)
    request = _ag_request(world, session)
    world["mp"].setattr(owner_direction, "_clock", real)
    assert owner_turns.turns(session) == []
    _ag_user_step(path, 1)
    owner_turns.antigravity_prompt(_ag_payload(path, session, initialNumSteps=1, invocationNum=2),
                                   now=time.time() + 1, wait=0, issuer=dict(AG_ISSUER))
    copy = owner_turns.turns(session, time.time() + 2)[-1]
    assert copy["occurrence"] == 2 and not copy.get("late") and "duplicate_of" not in copy and copy["eligible"], copy
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "not_confirmed", out
    assert not _paused()


def _grok_paused() -> bool:
    return next(a for a in routing.read_view()["accounts"] if a["key"] == "grok|")["state"] == "paused"


def test_review3_a_second_antigravity_yes_for_a_new_request_with_a_new_step_confirms(world):
    session = "ag-again"
    path = _ag_transcript(world, session)
    _ag_turn(world, session, ago=60)
    first = _ag_request(world, session)
    owner_turns.antigravity_prompt(_ag_payload(path, session, initialNumSteps=2, lastUserInput="yes"),
                                   now=time.time() + 1, wait=0, issuer=dict(AG_ISSUER))
    ok, out = _confirm_id(first)
    assert ok and _paused(), out
    _ag_user_step(path, 2, "yes", then=3)
    second = _ag_request(world, session, "Grok")
    owner_turns.antigravity_prompt(_ag_payload(path, session, initialNumSteps=4, lastUserInput="yes"),
                                   now=time.time() + 2, wait=0, issuer=dict(AG_ISSUER))
    reply = owner_turns.turns(session, time.time() + 3)[-1]
    assert reply["fallback"] and reply["ag_step"] == 4 and reply["occurrence"] == 2, reply
    ok, out = _confirm_id(second)
    assert ok, out
    assert _grok_paused() and out["confirmed"]["message"] == reply["turn"]


def test_review3_p2_a_foreign_headless_process_recording_its_own_hook_turn_in_t3_is_refused(world):
    # A different headless process inherits the T3 session and records its own
    # hook turn (sdk-cli, print, headless, not eligible): it is the nearest
    # harness, so _from_the_hook matched it, and the trust key became
    # claude_code:t3.
    request = _hold_in_t3(world)
    world["t3"].message(THREAD, time.time() + 1)
    owner_turns.record({"session_id": T3_SESSION, "prompt": OWNER_WORDS, "cwd": "/repo"},
                       {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"}, now=time.time() + 2,
                       issuer={"pid": 4_000_000, "start": 1.0, "exe": "claude", "print": True, "headless": True, "tty": False, "app": None})
    latest = owner_turns.turns(T3_SESSION, time.time() + 3)[-1]
    assert latest["entrypoint"] == "sdk-cli" and latest["eligible"] is False, latest
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "not_from_the_hook", out
    assert not _paused()


def test_review3_p2_an_antigravity_reply_while_the_transcript_ends_at_the_previous_step_confirms(world):
    # The previous turn's step 1 was recorded from the transcript; the reply's
    # PreInvocation names step 2 while the transcript still ends at step 1, so
    # the recorder re-selected the claimed step 1 and recorded nothing.
    session = "ag-stale"
    path = _ag_transcript(world, session)
    _ag_user_step(path, 1)
    owner_turns.antigravity_prompt(_ag_payload(path, session, initialNumSteps=1),
                                   now=time.time() - 30, wait=0, issuer=dict(AG_ISSUER))
    assert len(owner_turns.turns(session)) == 1
    request = _ag_request(world, session)
    owner_turns.antigravity_prompt(_ag_payload(path, session, initialNumSteps=2, lastUserInput="yes"),
                                   now=time.time() + 1, wait=0, issuer=dict(AG_ISSUER))
    rows = owner_turns.turns(session, time.time() + 2)
    assert len(rows) == 2, rows
    reply = rows[-1]
    assert reply["fallback"] and reply["ag_step"] == 2, reply
    ok, out = _confirm_id(request)
    assert ok, out
    assert _paused() and out["confirmed"]["message"] == reply["turn"]


def test_review3_p2_a_t3_reply_tied_with_the_snapshot_time_confirms(world):
    at = time.time() - 60
    world["t3"].message(THREAD, at)
    request = _hold_in_t3(world)
    mid = world["t3"].message(THREAD, at)
    _t3_hook_turn()
    ok, out = _confirm_id(request)
    assert ok, out
    assert _paused() and out["confirmed"]["message"] == mid


def test_review3_p2_a_snapshot_that_cannot_keep_its_tied_ids_refuses(world):
    at = time.time() - 60
    for _ in range(owner_direction._BOUNDARY_MOST + 1):
        world["t3"].message(THREAD, at)
    _t3_hook_turn()
    _be(world, T3_SESSION)
    ok, out = owner_act.act("route.pause", {"account": "Codex"})
    assert not ok and out["code"] == "ask_limit", out
    assert not _paused()
