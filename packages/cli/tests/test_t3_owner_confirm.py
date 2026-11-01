"""Owner changes held for the owner to confirm (the owner's rule, 2026-11-01):
an agent asks, Khipu holds the change and says what to ask ("Confirm ...?"),
and the confirm applies it only when the confirming conversation's own record
shows a message the owner typed after the latest ask Khipu recorded for that
conversation (at the hold, and on every confirm that found no reply). No age
limit, no codes, no word matching. T3's record is its message rows; every other trusted
front end's is the prompt hook's eligible turns.

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
    real = t3.owner_message_after
    monkeypatch.setattr(t3, "owner_message_after", lambda thread, since: (None, False))
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "t3_unreadable"
    monkeypatch.setattr(t3, "owner_message_after", real)
    world["t3"].path.write_bytes(b"not a database")
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "t3_unreadable", out
    assert not _paused()


def test_the_message_reader_on_a_broken_file_is_not_definitive(world):
    from khipu import t3

    world["t3"].path.write_bytes(b"not a database")
    assert t3.owner_message_after(THREAD, 0.0) == (None, False)


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
    assert not ok and out["code"] == "not_confirmed" and not _paused()
    _front_end_turn(world, front, "s-front")
    ok, out = _confirm_id(request)
    assert ok, out
    assert _paused() and out["confirmed"]["source"] == "hook"


@pytest.mark.parametrize("front", FRONTS)
def test_a_two_day_old_owner_turn_is_past_retention_so_the_confirm_asks_again(world, front):
    # The proof is the owner's reply to the latest ask, so turns keep the normal
    # one-day retention: an older turn is gone, and the confirm asks again.
    request = _hold_by_request(world, front, "s-front", ago=3 * DAY)
    _front_end_turn(world, front, "s-front", ago=2 * DAY)
    owner_turns.sweep()
    assert owner_turns.turns("s-front") == []
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "not_confirmed" and "Ask the owner to confirm now" in out["error"]
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


def test_review_p2_a_t3_confirm_needs_no_hook_turn_from_the_last_day(world):
    # T3 confirm went through _t3_bound -> turns() (24 h), so a 2-day-old hook
    # turn was invisible -> no_recorded_turn. T3's record is the proof.
    request = _hold_in_t3(world, ago=2 * DAY)
    assert owner_turns.turns(T3_SESSION) == []
    mid = world["t3"].message(THREAD, time.time() + 1)
    ok, out = _confirm_id(request)
    assert ok, out
    assert _paused() and out["confirmed"]["message"] == mid


def test_review_p2_the_30_day_cap_no_longer_deletes_proof_of_a_waiting_request(world):
    # The 30-day evidence cap deleted proof while the request stayed pending.
    # Now proof is the reply to the latest ask: the confirm asks, the reply passes.
    request = _hold_by_request(world, "claude_code:cli", "s-front", ago=35 * DAY)
    _front_end_turn(world, "claude_code:cli", "s-front", ago=31 * DAY)
    owner_turns.sweep()
    ok, out = _confirm_id(request)
    assert not ok and out["code"] == "not_confirmed" and "Ask the owner to confirm now" in out["error"], out
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
    assert not ok and "Ask the owner to confirm now" in out["error"]
    _front_end_turn(world, "claude_code:cli", "s-front")
    ok, out = _confirm_id(request)
    assert ok, out
    assert _paused() and out["confirmed"]["source"] == "hook"


def test_the_owner_replies_after_an_ask_on_a_40_day_old_request_in_t3(world):
    request = _hold_in_t3(world, ago=40 * DAY)
    owner_turns.sweep()
    ok, out = _confirm_id(request)
    assert not ok and "Ask the owner to confirm now" in out["error"]
    mid = world["t3"].message(THREAD, time.time() + 1)
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
