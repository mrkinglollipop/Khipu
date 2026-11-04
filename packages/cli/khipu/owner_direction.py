"""Owner direction: an owner action a conversation makes on the owner's direction.

The third way past an owner gate, beside the app and a yes typed on a
terminal (scope: docs/plans/2026-10-16-owner-direction.md, section 2). A
conversation submits the structured action (``khipu_owner_act``, or
``--owner-direction`` on the owner command) and it applies. The one check
(``check``) is where the request comes from: a conversation with a recorded
typed owner message (``owner_turns``, or T3's message rows). Held changes use
a message after their ask, with no reply-age limit. Nothing here reads the
owner's words or calls a model.

The session is never an argument: it is read from the calling process's
environment, as ``routing.conversation_author`` reads it. Interpretation of
the owner's direction and same-user code remain outside this gate.

Every owner action through a gate (the app, a terminal, a direction) is a
line in ``<data dir>/owner-actions.jsonl`` with what it changed; a direction
is applied only when it can be undone from that line. A purge by direction
is scheduled a day out, and Undo cancels it until then.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import math
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from khipu import owner_turns

PURGE_DELAY_S = 24 * 3600
LOG_NAME = "owner-actions.jsonl"
HEAD_NAME = "owner-actions.head.json"
# The log is kept in sealed segments so an append never reads the whole of it.
# When the current log (``LOG_NAME``) holds ``SEGMENT_MAX_LINES`` lines it is
# renamed to ``owner-actions.<six digit number>.jsonl`` and a fresh one starts;
# the fresh log's first line chains to the sealed segment's last hash, so the
# chain runs unbroken through every segment. No segment is ever removed:
# ``read_log`` and the all-segment check (``verify_log``) read them all.
SEGMENT_MAX_LINES = 2000
_SEGMENT = re.compile(r"^owner-actions\.(\d{6})\.jsonl$")
# A break found in the log (a changed line, lines cut from its end) is kept
# here, apart from the log and its head, until the owner acknowledges it: a
# later append rewrites the head and could otherwise make a cut end look
# whole again.
ALARM_NAME = "owner-actions.alarm.json"
# Every line Khipu writes to the log carries this version. Lines without one
# may lead the log (written before it was chained); after the first
# versioned line, a line without it, or with an older one, is a break.
LOG_VERSION = 2
_SESSION_VARS = ("KHIPU_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID")

_clock = time.time

_DISPATCHED_WHY = ("This call comes from a dispatched job, which can never make an owner's change. Ask the owner to "
                   "say it in the conversation they are typing in.")
_T3_UNREADABLE_WHY = ("Khipu could not read T3's record of this conversation, so it can't tell whether the owner is "
                      "in it. Nothing was changed; try again.")
# Owner hold/confirm: a few bounded retries under the longer T3 read budget.
_OWNER_T3_ATTEMPTS = 4


class DirectionRefused(Exception):
    """The direction was not accepted; ``message`` is plain English the agent
    can relay to the owner."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def refusal(self) -> dict:
        return {"ok": False, "error": self.message, "code": self.code}


# ---- the action table --------------------------------------------------------------------
#
# Every owner action a direction can make. Whether each can be undone from
# its log line is ``_UNDO``.

ACTIONS: frozenset[str] = frozenset({
    "owed.correct_job_origin", "owed.correct_job_origin.confirm",
    "route.definition.set", "route.pause", "route.lift", "route.clear", "route.note.set", "route.note.end", "route.rule.add",
    "route.rule.edit", "route.rule.retire", "route.rule.restore", "route.rule.confirm", "route.rule.undo",
    "route.rule.dismiss", "route.hold.add", "route.hold.end", "route.cap.set", "route.cap.remove",
    "route.priority.set", "route.priority.amend", "route.priority.confirm",
    "route.priority.undo", "route.priority.clear", "route.tier_table.set", "route.tier_table.confirm", "route.override.grant", "route.override.end", "route.override.keep",
    "route.tier_table.clear", "accounts.track", "accounts.rename", "accounts.stop",
    "accounts.move", "accounts.decline", "accounts.offer_again", "accounts.auto_track", "accounts.request.confirm",
    "accounts.request.undo", "config.usage_accounts", "config.routing_setting", "config.answer_request.confirm",
    "config.answer_request.undo", "promote.gate_ignore", "promote.gate_ignore.cancel", "ledger.import",
    "ledger.restore", "ledger.purge", "ledger.project", "ledger.add", "ledger.set", "ledger.remove", "work.hide", "work.unhide",
    "work.same", "work.not_same", "owed.change", "owed.tidy", "direction.off", "direction.on", "direction.acknowledge", "direction.set",
    "direction.undo", "project.alias_add", "project.alias_remove", "project.merge", "project.keep_separate",
    "project.separate", "forget.send", "forget.here", "forget.not_same", "outbox.merge_preferences",
    "outbox.span_covered",
})

# Actions a recent owner turn does not make: ``khipu_owner_act`` always holds
# them with this question, and the command's ``--owner-direction`` passes only
# in the replay of a request ``confirm`` proved he answered (``replaying``).
CONFIRM_ONLY: dict[str, str] = {
    "outbox.span_covered": ("Retire the dead outbox jobs `khipu outbox span-covered` counts? Each has a live episode "
                            "whose range contains its own and which holds every evidence field it carries. Summaries "
                            "are not compared and a range does not prove the same text, so each job's own summary "
                            "then stays only in the local record; nothing is written to the hub."),
}

# Routing changes that apply as soon as the owner types them (2026-11-03, owner
# decision): account priority and its exceptions, standing rules, the note,
# pauses, holds and lifts. They still need the check below (a provably owner
# turn); a turn that fails it keeps today's refusal or hold. Forget, purge,
# permissions and trust settings, secrets and every other action are not here,
# so they keep the explicit confirm they need.
ROUTING_INSTANT: frozenset[str] = frozenset({
    "route.pause", "route.lift", "route.note.set", "route.note.end", "route.rule.add", "route.rule.edit",
    "route.rule.retire", "route.rule.restore", "route.rule.confirm", "route.rule.undo", "route.rule.dismiss",
    "route.hold.add", "route.hold.end", "route.priority.set", "route.priority.amend", "route.priority.confirm",
    "route.priority.undo", "route.override.grant", "route.override.end", "route.override.keep",
})


# Every action ``khipu_needs_you`` offers to settle something on the list.
SETTLE_ACTIONS: frozenset[str] = frozenset({
    "route.priority.confirm", "route.priority.undo", "route.rule.confirm", "route.rule.undo", "route.rule.dismiss",
    "route.rule.retire", "config.answer_request.confirm", "config.answer_request.undo", "accounts.request.confirm",
    "accounts.request.undo", "route.tier_table.confirm", "route.tier_table.set", "route.tier_table.clear",
    "owed.correct_job_origin.confirm", "project.merge", "project.keep_separate", "forget.send", "forget.here",
    "forget.not_same", "direction.acknowledge", "route.hold.end", "route.clear", "route.cap.set",
    "route.override.end", "route.override.keep",
})
# A conversation's override changes only that conversation's advice, so
# nothing about it is pushed to the others (no receipt line, no push words).
LOCAL_ONLY_ROUTING: frozenset[str] = frozenset({"route.override.grant", "route.override.end", "route.override.keep"})
# The routing changes that answer a request waiting on the owner, not a change
# of his own. His typed message can be the direction for a change of his own;
# an answer needs him to have been shown what it answers, so in T3 it applies at
# once only on a message newer than an ask in that thread showing it
# (``_shown_read``). A conversation's own request applied on the owner's turn
# (``REQUEST_APPLIED``) is the change he typed, and stays as it was.
ANSWER_ACTIONS: frozenset[str] = frozenset({
    "route.priority.confirm", "route.priority.undo", "route.rule.confirm", "route.rule.undo", "route.rule.dismiss",
})


def instant_allowed(action: str, target: str | None = None) -> bool:
    """Whether ``action`` is a routing change that applies as soon as the
    owner types it. An Undo (``direction.undo``) counts only when it undoes
    one of those: undoing a trust, permission or forget action never does."""
    if action in ROUTING_INSTANT:
        return True
    if action == "direction.undo" and target:
        line = _find(str(target))
        return bool(line and line.get("action") in ROUTING_INSTANT)
    return False


# ---- the session --------------------------------------------------------------------------


def session_of(env=None) -> str:
    env = os.environ if env is None else env
    for name in _SESSION_VARS:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return ""


def _sessions_named(env=None) -> set[str]:
    env = os.environ if env is None else env
    return {v for v in ((env.get(name) or "").strip() for name in _SESSION_VARS) if v}


def _title(name: str) -> str:
    return name[:1].upper() + name[1:] if name and name.islower() and " " not in name else name



# ---- the check ---------------------------------------------------------------------------


def check(action: str, *, env=None, now: float | None = None, instant: bool = False, reading: bool = False) -> dict:
    """A recorded owner turn in this conversation, or the replay of its answer.

    T3 uses its owner-message rows. Native fronts use their recorded typed
    turns. Reading only reports capability; it never applies a change.
    """
    if action not in ACTIONS:
        raise DirectionRefused("not_by_direction", "That change can't be made by direction.")
    now = _clock() if now is None else now
    conf = owner_turns.settings()
    if not conf["enabled"] and action != "direction.on":
        raise DirectionRefused("direction_off", "Owner direction is off, so only the Khipu app or a yes typed on a "
                                                "terminal can make this change.")
    environ = os.environ if env is None else env
    session = _caller_session(environ)
    confirmed = _CONFIRMED.get()
    if confirmed is not None and confirmed["action"] == action and confirmed["stamp"]["session"] == session:
        # The replay of a held request ``confirm`` proved the owner confirmed.
        return dict(confirmed["stamp"])
    from khipu import t3

    try:
        turn = _recent_turn(session, conf, now)
    except DirectionRefused:
        if any(t.get("entrypoint") == "t3" for t in owner_turns.turns(session, now)):
            # A T3 conversation: its turns never count directly, so the
            # change is held for the owner to confirm (``hold``, ``confirm``).
            # T3's own record decides whether it is a job (``_t3_bound``).
            with t3.owner_read_budget():
                _t3_bound(environ, session, now)
                stamp = _instant_t3(environ, session, now, action) if instant else None
            if stamp is not None:
                return stamp
            raise DirectionRefused("confirm_needed", _HOLD_WHY) from None
        raise
    if not reading and action in SETTLE_ACTIONS and not REQUEST_APPLIED.get():
        _not_the_reading_turn(session, turn)
    stamp = {"auth": "direction", "harness": turn.get("harness"), "session": session, "turn": turn["turn"],
             "prompt_sha256": turn.get("prompt_sha256"), "occurrence": turn.get("occurrence"),
             "binding": "session" if turn.get("harness") in owner_turns.SESSION_ONLY_HARNESSES else "hook"}
    if instant:
        stamp["instant"] = {"source": "hook", "turn": turn["turn"], "turn_at": turn.get("at"),
                            "window_h": conf["latest_window_h"]}
    return stamp


def _recent_turn(session: str, conf: dict, now: float, recorded: list[dict] | None = None) -> dict:
    """The latest recorded eligible typed turn; an answer has no age cutoff."""
    recorded = owner_turns.turns(session, now) if recorded is None else recorded
    usable = [t for t in recorded if owner_turns.eligible(t, conf)[0]]
    if not usable:
        untrusted = owner_turns.untrusted_front_end(recorded, conf)
        if untrusted:
            raise DirectionRefused("no_recorded_turn",
                                   f"The owner typed here, but in {untrusted}, which is not a front end that counts "
                                   "as the owner. To add it, switch it on in the Khipu app (Settings, Owner actions, "
                                   "Front ends that count as you), or ask for it in a conversation from a front end "
                                   "already on that list.")
        raise DirectionRefused("no_recorded_turn",
                               "The owner has not typed in this conversation, so it can't make an owner's change "
                               "(a background or dispatched job never can). Ask the owner to say it in a "
                               "conversation they are typing in.")
    turn = usable[-1]
    return turn


_HOLD_WHY = ("This is a T3 conversation, so the change waits for the owner to confirm it. Ask for it with "
             "khipu_owner_act: Khipu holds it and tells you what to ask the owner.")
_ASK_NOW = ("Not confirmed yet: no message the owner typed in this conversation since they were last asked, "
            "so nothing was changed. Ask the owner to confirm now, in this conversation, then call "
            "direction.confirm again.")


def _usable_t3_turn(t: dict) -> bool:
    return t.get("entrypoint") == "t3" and not any(
        t.get(k) for k in ("dispatched", "helper", "wrapped", "delegated", "machine"))


def _t3_thread_of(session: str) -> str:
    """This conversation's one T3 thread, re-read from T3's record (a few
    bounded retries under the owner read budget), or ``DirectionRefused``."""
    from khipu import t3

    attempts = _OWNER_T3_ATTEMPTS if t3.owner_read_active() else 2
    for _ in range(attempts):
        threads, definitive = t3.session_thread_ids_result(session)
        if definitive:
            break
    else:
        raise DirectionRefused("t3_unreadable", _T3_UNREADABLE_WHY)
    if not threads:
        raise DirectionRefused("no_recorded_turn", "T3's record no longer shows this conversation, so Khipu can't "
                                                   "tell whether the owner is in it. Nothing was changed.")
    if len(threads) > 1:
        raise DirectionRefused("no_recorded_turn", "T3's record names more than one thread for this conversation, "
                                                   "so Khipu can't tell which one the owner is in. Nothing was "
                                                   "changed.")
    return threads[0]


def _t3_lookup_failed(why: str | None) -> bool:
    """True when ``dispatched_job`` refused for lack of a readable T3 record,
    not because the record showed a job."""
    return isinstance(why, str) and "could not look up" in why


def _t3_refusals(environ, session: str) -> str:
    return _t3_thread_of(session)


def _t3_bound(environ, session: str, now: float) -> tuple[str, dict]:
    thread = _t3_thread_of(session)
    recorded = owner_turns.turns(session, now)
    return thread, recorded[-1] if recorded else {}


# ---- held requests and the owner's confirmation ---------------------------------------------
#
# A change held for the owner to confirm (2026-11-01: "all we need is the
# confirmation step"). It has no age limit, and any conversation can confirm
# it. The confirm is made during the agent turn the owner's reply started,
# so that turn is what is proved, never a search of stored turns:
#
# A reply is a typed turn after the ask: a T3 owner-message row, or a native
# prompt-hook row. There is no hash equality, process ancestry, word-matching,
# global message quota or reply-age challenge. Re-reading the same pinned
# answers leaves the existing ask boundary alone.

REQUESTS_NAME = "owner-requests.json"
_REQUESTS_KEPT_S = 30 * 24 * 3600  # settled requests; a pending or applying one is never dropped
_CONFIRMED: contextvars.ContextVar[dict | None] = contextvars.ContextVar("khipu_owner_confirmed", default=None)
# Marks a conversation's own request so it applies on the owner's turn.
# The log names the instant application; Undo takes the whole request back.
REQUEST_APPLIED: contextvars.ContextVar[bool] = contextvars.ContextVar("khipu_request_applied", default=False)


def requests_path() -> Path:
    from khipu.paths import data_dir

    return data_dir() / REQUESTS_NAME


@contextlib.contextmanager
def _requests_locked():
    import fcntl

    path = requests_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(path.with_name(f".{path.name}.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield path
    finally:
        os.close(lock_fd)


def _load(path: Path) -> dict:
    """The requests file: ``requests`` and ``erased``, Forget's erasure
    generation (``forget_requests`` bumps it, ``hold`` checks it)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"requests": [], "erased": 0}
    rows = data.get("requests") if isinstance(data, dict) else None
    erased = data.get("erased", 0) if isinstance(data, dict) else None
    if not isinstance(rows, list) or type(erased) is not int:
        raise OSError("the held owner requests can't be read")
    return {"requests": [r for r in rows if isinstance(r, dict)], "erased": erased}


def _read_requests(path: Path) -> list[dict]:
    return _load(path)["requests"]


def _save(path: Path, state: dict, now: float) -> None:
    kept = [r for r in state["requests"]
            if r.get("settled_ts") is None or r.get("action") == READ_ACTION
            or now - _float(r.get("settled_ts")) < _REQUESTS_KEPT_S]
    _write_private(path, json.dumps({"requests": kept, "erased": state["erased"]}, ensure_ascii=False,
                                    sort_keys=True) + "\n")


def ask_line(action: str, args: dict) -> str:
    what = ", ".join(f"{k}: {json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v}"
                     for k, v in sorted(args.items()))
    return f"Confirm {action}" + (f" ({what})" if what else "") + "?"


# ---- the list as an ask ---------------------------------------------------------------------------
#
# Reading ``khipu_needs_you`` is what puts the question to the owner, so it is an
# ask boundary like a hold's: what he typed before it, the question included,
# is no reply to it. It is kept as a row of its own (state ``shown``, under the requests lock,
# Forget's scan and the 30-day retention like any settled row) with the
# signature of each answer the read showed whole.

READ_ACTION = "needs_you.read"
# The action being answered, as ``owner_act.act`` was asked for it, through to
# the owner gate the command reaches: never from an argument.
ANSWERING: contextvars.ContextVar[str | None] = contextvars.ContextVar("khipu_answering", default=None)
_SAME_TURN = ("Not done: this is the same turn that read what is waiting, so the owner has not answered yet, and "
              "nothing was changed. Show them the words, and call this after they reply.")


def signature(action: str, args: dict) -> str:
    return _sha(json.dumps({"action": action, "args": args}, sort_keys=True, default=str))[:24]


def _read_rows() -> list[dict]:
    try:
        return sorted((r for r in _read_requests(requests_path()) if r.get("action") == READ_ACTION),
                      key=lambda r: _float(r.get("settled_ts")), reverse=True)
    except (OSError, ValueError):
        return []


def _shown_read(thread: str, answer: str | None) -> dict | None:
    """The read in this T3 thread that showed ``answer``, when one did."""
    for row in _read_rows():
        if _t3_parts((row.get("t3_snapshots") or {}).get(thread)) is not None:
            return row if answer and answer in (row.get("shown") or []) else None
    return None


def _not_the_reading_turn(session: str, turn: dict) -> None:
    """Outside T3: an answer is refused in the turn that read the list. An
    agent that never read it has nothing to be compared with, and passes."""
    for row in _read_rows():
        boundary = (row.get("asks") or {}).get(session) if row.get("session") == session else None
        if boundary is not None and not _after_ask(turn, boundary):
            raise DirectionRefused("not_confirmed", _SAME_TURN)


def note_read(shown: list[str], *, env=None, now: float | None = None) -> str | None:
    """Record that this conversation was just given the list: the ask boundary
    for the conversation and the requests it showed. None
    when it is recorded, or when this call could never settle anything anyway
    (a job, a child thread, no conversation to name); otherwise why not, and
    the list then offers no settle."""
    from khipu import t3

    now = _clock() if now is None else now
    environ = os.environ if env is None else env
    try:
        session = _caller_session(environ)
        with _requests_locked() as path, t3.owner_read_budget():
            turns_ = owner_turns.turns(session, now)
            in_t3 = any(t.get("entrypoint") == "t3" for t in turns_)
            if not in_t3 and owner_turns.dispatched_job(environ, session):
                return None
            thread = _t3_bound(environ, session, now)[0] if in_t3 else None
            read = _t3_read(thread, (), None) if in_t3 else None

            def grown(row: dict) -> None:
                if in_t3:
                    boundaries = row.get("t3_snapshots")
                    if not isinstance(boundaries, dict):
                        boundaries = row["t3_snapshots"] = {}
                    boundaries[thread] = _t3_boundary(boundaries.get(thread), read, session)
                else:
                    boundaries = row.get("asks")
                    if not isinstance(boundaries, dict):
                        boundaries = row["asks"] = {}
                    boundaries[session] = _ask_boundary(boundaries.get(session), turns_, now, session)

            state = _load(path)
            mine = next((r for r in state["requests"]
                         if r.get("action") == READ_ACTION and r.get("session") == session), None)
            previous = set((mine or {}).get("shown") or [])
            def same(answer: str) -> bool:
                return answer in previous
            for row in state["requests"]:
                answer = signature("direction.confirm", {"request": row.get("id")})
                if row.get("state") == "pending" and answer in shown and not same(answer):
                    grown(row)
            if mine is None:
                mine = {"id": "read-" + secrets.token_hex(6), "action": READ_ACTION, "state": "shown",
                        "session": session, "created_ts": now, "created_at": _iso(now)}
                state["requests"].append(mine)
            if not all(same(v) for v in shown):
                grown(mine)
            mine["shown"] = sorted(previous | set(shown))
            mine.update(settled_ts=now, settled_at=_iso(now))
            _save(path, state, now)
    except DirectionRefused as refused:
        if refused.code == "t3_unreadable":
            return refused.message
        return None
    except (OSError, ValueError):
        return "Khipu can't write down that this list was shown."
    return None


def _seed(thread: str | None, session: str, answer: str) -> dict | None:
    """The boundary of the read that showed ``answer``: a hold made after the
    owner replied to that read starts from it, so his one yes counts."""
    for row in _read_rows():
        if thread is not None and _t3_parts((row.get("t3_snapshots") or {}).get(thread)) is not None:
            return ({"t3_snapshots": {thread: json.loads(json.dumps(row["t3_snapshots"][thread]))}}
                    if answer in (row.get("shown") or []) else None)
        if thread is None and row.get("session") == session and _ask_parts((row.get("asks") or {}).get(session)):
            return ({"asks": {session: json.loads(json.dumps(row["asks"][session]))}}
                    if answer in (row.get("shown") or []) else None)
    return None


def reviewed_hold(request: dict) -> bool:
    """Whether the held input includes the content required for replay."""
    args, files = request.get("args"), request.get("files")
    if not isinstance(args, dict):
        return False
    key = {"ledger.import": "file", "owed.tidy": "plan"}.get(request.get("action"))
    return not key or not args.get(key) or (isinstance(files, dict) and str(args[key]) in files)


def _review_files(action: str, args: dict) -> dict:
    key = {"ledger.import": "file", "owed.tidy": "plan"}.get(action)
    if not key or not args.get(key):
        return {}
    from khipu import safe_read
    from khipu.redact import redact_secrets
    try:
        raw, _ = safe_read.read_file(args[key])
        text = raw.decode("utf-8")
        json.loads(text)
        if redact_secrets(text, force=True)[1]:
            raise ValueError("The complete file cannot be disclosed safely in one ask.")
    except (OSError, ValueError, safe_read.Refused) as err:
        raise DirectionRefused("file_unreviewable", f"The file cannot be reviewed completely: {err}") from None
    return {str(args[key]): {"text": text, "sha256": _sha(text)}}


def reviewed_file(path) -> bytes | None:
    """The immutable bytes disclosed by this held action, never a later path read."""
    replay = _CONFIRMED.get() or {}
    files = replay.get("files") or {}
    item = files.get(str(path))
    if item is None:
        return None
    if _sha(item["text"]) != item["sha256"]:
        raise DirectionRefused("file_changed", "The reviewed file snapshot is damaged; ask again.")
    return item["text"].encode("utf-8")


_ONE_WAITING = frozenset({"route.override.grant", "route.override.end", "route.override.keep",
                          "route.priority.amend"})


def hold(action: str, args: dict, *, env=None, now: float | None = None, ask: str | None = None) -> dict:
    """Hold ``action`` with its checked ``args`` for the owner to confirm.
    ``ask`` is the question to put to the owner when the plain one
    (``ask_line``) would leave out what the yes also covers.
    The caller must identify a conversation with a recorded typed turn.
    Admission and the save run under the requests lock Forget takes, and a
    Forget since the hold began (``erased``) refuses it."""
    from khipu import t3

    now = _clock() if now is None else now
    environ = os.environ if env is None else env
    conf = owner_turns.settings()
    if not conf["enabled"] and action != "direction.on":
        raise DirectionRefused("direction_off", "Owner direction is off, so only the Khipu app or a yes typed on a "
                                                "terminal can make this change.")
    session = _caller_session(environ)
    files = _review_files(action, args)
    if files:
        ask = (ask or ask_line(action, args)) + " Reviewed file contents: " + " ".join(
            f"{name} (sha256 {item['sha256']}): {item['text']}" for name, item in files.items())
    try:
        erased = _load(requests_path())["erased"]
        with _requests_locked() as path:
            turns_ = owner_turns.turns(session, now)
            extra = {}
            if any(t.get("entrypoint") == "t3" for t in turns_):
                with t3.owner_read_budget():
                    thread, _ = _t3_bound(environ, session, now)
                    seeded = _seed(thread, session, signature(action, args))
                    extra = {**seeded, "seeded": True} if seeded else {
                        "t3_snapshots": {thread: _t3_boundary(None, _t3_read(thread, (), None), session)}}
            else:
                if owner_turns.dispatched_job(environ, session):
                    raise DirectionRefused("no_recorded_turn", _DISPATCHED_WHY)
                usable = [t for t in turns_ if owner_turns.eligible(t, conf)[0]]
                if not usable:
                    raise DirectionRefused("no_recorded_turn",
                                           "The owner has not typed in this conversation, so it can't ask for an "
                                           "owner's change (a background or dispatched job never can).")
                seeded = _seed(None, session, signature(action, args))
                extra = {**seeded, "seeded": True} if seeded else {
                    "asks": {session: _ask_boundary(None, turns_, now, session)}}
            request = {"id": "req-" + secrets.token_hex(6), "action": action, "args": args, "state": "pending",
                       "created_ts": now, "created_at": _iso(now), "session": session, "by": _asker(environ),
                       "ask": ask or ask_line(action, args), "files": files, **extra}
            state = _load(path)
            if state["erased"] != erased:
                raise DirectionRefused("forgotten", "Khipu forgot this conversation's words while the request was "
                                                    "being held, so nothing was held or changed. Ask again if the "
                                                    "owner still wants it.")
            if action in _ONE_WAITING:
                # A retry of a request that is still waiting is that request,
                # not a second copy for the owner to answer.
                same = next((r for r in state["requests"] if r.get("state") == "pending" and r.get("action") == action
                             and r.get("session") == session
                             and signature(action, r.get("args") or {}) == signature(action, args)), None)
                if same is not None:
                    return same
            state["requests"].append(request)
            _save(path, state, now)
    except OSError as err:
        raise DirectionRefused("log_unwritable", f"Khipu can't keep the request ({err}), so nothing was held or "
                                                 "changed. Try again once the disk can be written.") from None
    return request


def _asker(environ) -> dict:
    """Who is holding a request, for the owner's list: the harness and
    project routing's author stamp names. Forget removes it with the words."""
    from khipu import routing

    by = routing.conversation_author(environ)
    return {k: by[k] for k in ("kind", "harness", "project", "session") if by.get(k)}


def _caller_session(environ) -> str:
    """The calling conversation, after the refusals that need no record."""
    session = session_of(environ)
    if owner_turns.job_marker(environ):
        raise DirectionRefused("no_recorded_turn", _DISPATCHED_WHY)
    if not session:
        raise DirectionRefused("no_recorded_turn", "Khipu can't tell which conversation this is, so it can't tell "
                                                   "whether the owner is in it.")
    return session


def _entries(raw, width: int) -> list[list]:
    """A boundary's stored entries as ``[*key, ts]`` (``ts`` None: kept
    whatever the bound). Entries saved before they carried a time (a bare id,
    or a key without its time) are read as kept. ValueError when malformed."""
    if not isinstance(raw, list):
        raise ValueError("entries")
    out = []
    for item in raw:
        if width == 1 and isinstance(item, str):
            item = [item]
        if not isinstance(item, list) or len(item) not in (width, width + 1):
            raise ValueError("entry")
        key = item[:width]
        if not all(isinstance(k, str) for k in key[:1]) or (width == 2 and type(key[1]) is not int):
            raise ValueError("entry key")
        ts = item[width] if len(item) > width else None
        if ts is not None and (isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts)):
            raise ValueError("entry time")
        out.append([*key, ts])
    return out


def _bounded(entries: list[list], since: float | None, *, keep_ties: bool = False) -> list[list]:
    """Keep the recorded ask entries without an artificial message-count limit."""
    best: dict[tuple, list] = {}
    for entry in entries:
        key, ts = tuple(entry[:-1]), entry[-1]
        have = best.get(key)
        if have is None or (have[-1] is not None and (ts is None or ts > have[-1])):
            best[key] = list(entry)
    ordered = sorted(best.values(), key=lambda e: (e[-1] is not None, -(e[-1] or 0.0)))
    return ordered


def _makers(boundary) -> list[str]:
    by = boundary.get("by") if isinstance(boundary, dict) else None
    if isinstance(by, str):
        by = [by]
    return [s for s in by if isinstance(s, str)] if isinstance(by, list) else []


def _ask_parts(boundary) -> tuple[float, list[list], list[list]] | None:
    """(time, turn entries, occurrence entries) of a stored ask boundary
    outside T3, or None when there is none or it can't be read."""
    if not isinstance(boundary, dict):
        return None
    try:
        since = boundary["ts"]
        if isinstance(since, bool) or not isinstance(since, (int, float)) or not math.isfinite(since):
            return None
        return float(since), _entries(boundary["turns"], 1), _entries(boundary["copies"], 2)
    except (KeyError, TypeError, ValueError):
        return None


def _ask_boundary(old, turns_: list[dict], now: float, session: str) -> dict:
    """The ask boundary outside T3, grown from ``old`` with ``turns_`` (the
    rows of the read that found no reply): the later of the two times, and
    the ids and occurrences (prompt hash, occurrence) of every turn either
    holds, so neither they nor a copy of one count."""
    parts = _ask_parts(old)
    since, turns_in, copies_in = parts if parts is not None else (now, [], [])
    since = max(since, now)
    turns_in += [[t["turn"], _float(t.get("ts"))] for t in turns_ if isinstance(t.get("turn"), str) and t["turn"]]
    copies_in += [[t["prompt_sha256"], t["occurrence"], _float(t.get("ts"))] for t in turns_
                  if isinstance(t.get("prompt_sha256"), str) and type(t.get("occurrence")) is int]
    out = {"by": session, "ts": since, "turns": _bounded(turns_in, since), "copies": _bounded(copies_in, since)}
    return out


def _after_ask(turn: dict, boundary) -> bool:
    parts = _ask_parts(boundary)
    return bool(parts and _float(turn.get("ts")) > parts[0]
                and turn.get("turn") not in {e[0] for e in parts[1]})


def _t3_read(thread: str, seen, since) -> dict:
    """``t3.owner_reply_read`` with a few bounded retries under the owner
    read budget, or ``DirectionRefused``."""
    from khipu import t3

    attempts = _OWNER_T3_ATTEMPTS if t3.owner_read_active() else 2
    for _ in range(attempts):
        read, definitive = t3.owner_reply_read(thread, seen, since)
        if definitive:
            return read
    raise DirectionRefused("t3_unreadable", "Khipu could not read T3's record of this conversation's messages, "
                                            "so nothing was held or changed. Try again.")


def _t3_parts(snapshot) -> tuple[list[list], float | None, str | None] | None:
    """(owner message entries, latest message's T3 time and text form) of a
    stored T3 ask snapshot, or None when there is none, it can't be read or
    it is unknown (``_t3_boundary``)."""
    if not isinstance(snapshot, dict) or snapshot.get("unknown"):
        return None
    try:
        ids = _entries(snapshot["ids"], 1)
    except (KeyError, ValueError):
        return None
    latest = snapshot.get("latest_ts")
    if latest is not None and (isinstance(latest, bool) or not isinstance(latest, (int, float))
                               or not math.isfinite(latest)):
        return None
    if not ids and latest is None:
        # An older stored shape of the unknown snapshot: no message at all,
        # so it bounds nothing and must never act as an empty exclusion.
        return None
    return ids, latest, snapshot.get("latest_at")


def _t3_boundary(old, read: dict, session: str) -> dict:
    """The T3 ask snapshot, grown from ``old`` with ``read`` (the read that
    found no reply): the owner message ids either holds and the later of the
    two latest-message times (T3's clock), by the conversations that made it.

    A snapshot holding no message at all is unknown, never an empty
    exclusion: T3's projection can lag, so the owner's earlier messages may
    not be in it yet, and a confirm on an unknown snapshot only takes a fresh
    one. Once the agent's own question is in the projection it is not empty."""
    parts = _t3_parts(old)
    ids, latest, latest_at = parts if parts is not None else ([], None, None)
    if read["latest_ts"] is not None and (latest is None or read["latest_ts"] > latest):
        latest, latest_at = read["latest_ts"], read["latest_at"]
    entries = ids + [[str(i), float(ts)] for i, ts in read["owners"]]
    makers = sorted(set(_makers(old)) | {session})
    if not entries and latest is None:
        return {"by": makers, "unknown": True}
    # A confirm accepts an unseen message dated at the snapshot's time, so
    # the ids tied with it are kept: only older ones may be dropped.
    return {"by": makers, "ids": _bounded(entries, latest, keep_ties=True),
            "latest_at": latest_at, "latest_ts": latest}


def _proof(environ, session: str, request: dict, now: float) -> dict | str:
    """The auth stamp for a confirm (the rules above this section), or the
    message telling the agent to ask again when this turn is no reply to the
    latest ask: the boundary is then grown in ``request`` from the same read."""
    member = owner_turns._t3_membership(session)
    if member == "unverified":
        raise DirectionRefused("t3_unreadable", "Khipu could not read T3's record to tell whether this is a T3 "
                                                "conversation, so it can't confirm. Nothing was changed; try "
                                                "again.")
    if member == "t3":
        return _t3_proof(environ, session, request, now)
    return _turn_proof(environ, session, request, now)


def _t3_proof(environ, session: str, request: dict, now: float) -> dict | None:
    thread = _t3_refusals(environ, session)
    snapshots = request.get("t3_snapshots")
    if not isinstance(snapshots, dict):
        snapshots = request["t3_snapshots"] = {}
    old = snapshots.get(thread)
    parts = _t3_parts(old)
    if parts is None and isinstance(request.get("created_ts"), (int, float)):
        parts = ([], request["created_ts"], None)
    read = _t3_read(thread, [e[0] for e in parts[0]] if parts else (), parts[1] if parts else None)
    message = read["message"] if parts is not None else None
    if message is not None:
        sha = owner_turns.prompt_sha256(message["text"])
        return {"auth": "direction", "harness": "t3", "session": session, "turn": message["id"],
                "prompt_sha256": sha, "binding": "t3",
                "confirmed": {"source": "t3", "message": message["id"], "message_at": message["at"],
                              "message_sha256": sha}}
    snapshots[thread] = _t3_boundary(old, read, session)
    return _ASK_NOW


def _t3_baseline(thread: str) -> tuple[str, dict] | None:
    """(request id, snapshot) of the latest known ask boundary any held
    request recorded for this T3 thread, or None when there is none. Only a
    snapshot that bounds something counts (``_t3_parts``); a request Forget
    scrubbed holds none."""
    try:
        rows = _read_requests(requests_path())
    except OSError:
        return None
    best = None
    for row in rows:
        snapshots = row.get("t3_snapshots")
        snapshot = snapshots.get(thread) if isinstance(snapshots, dict) else None
        parts = _t3_parts(snapshot)
        if parts is None:
            continue
        rank = parts[1] if parts[1] is not None else float("-inf")
        if best is None or rank > best[0]:
            best = (rank, str(row.get("id")), snapshot)
    return None if best is None else (best[1], best[2])


def _instant_t3(environ, session: str, now: float, action: str | None = None) -> dict | None:
    """A T3 owner's message for an instant change, independent of hook text.

    Existing answering actions use their shown ask. An own request keeps the
    owner's interpretation tradeoff and needs no earlier question.
    """
    from khipu import t3

    try:
        thread = _t3_thread_of(session)
        answered = _shown_read(thread, ANSWERING.get()) if action in ANSWER_ACTIONS else None
        if action in ANSWER_ACTIONS and not REQUEST_APPLIED.get() and answered is None:
            return None
        base = ((answered["id"], answered["t3_snapshots"][thread]) if answered is not None
                else None if REQUEST_APPLIED.get() else _t3_baseline(thread))
        if base is None:
            if not REQUEST_APPLIED.get():
                return None
            # An own request needs the current typed message, not an earlier question.
            request_id, snapshot = None, {"ids": [], "latest_ts": 0}
        else:
            request_id, snapshot = base
        stamp = _t3_proof(environ, session, {"t3_snapshots": {thread: json.loads(json.dumps(snapshot))}}, now)
    except DirectionRefused:
        return None
    if not isinstance(stamp, dict):
        return None
    proved = stamp.pop("confirmed")
    stamp["instant"] = {"source": "t3", "message": proved["message"], "message_at": proved["message_at"],
                        "message_sha256": proved["message_sha256"],
                        **({"after_ask": request_id} if request_id is not None else {})}
    return stamp


def _turn_proof(environ, session: str, request: dict, now: float) -> dict | str:
    recorded = owner_turns.turns(session, now)
    if any(t.get("entrypoint") in ("t3", "t3-unverified") for t in recorded):
        raise DirectionRefused("no_recorded_turn", "This conversation's turns were recorded as T3's, but T3's "
                                                   "record no longer shows it, so Khipu can't confirm here.")
    if owner_turns.dispatched_job(environ, session):
        raise DirectionRefused("no_recorded_turn", _DISPATCHED_WHY)
    conf = owner_turns.settings()
    turn = next((t for t in reversed(recorded) if owner_turns.eligible(t, conf)[0]), None)
    if turn is None:
        raise DirectionRefused("no_recorded_turn", "No typed owner reply was recorded in this conversation.")
    asks = request.get("asks")
    if not isinstance(asks, dict):
        asks = request["asks"] = {}
    old = asks.get(session) or {"ts": request.get("created_ts", now), "turns": [], "copies": []}
    why = None if _after_ask(turn, old) else _ASK_NOW
    if why is None:
        return {"auth": "direction", "harness": turn.get("harness"), "session": session, "turn": turn["turn"],
                "prompt_sha256": turn.get("prompt_sha256"), "occurrence": turn.get("occurrence"),
                "binding": "session" if turn.get("harness") in owner_turns.SESSION_ONLY_HARNESSES else "hook",
                "confirmed": {"source": "hook", "message": turn["turn"], "message_at": turn.get("at"),
                              "message_sha256": turn.get("prompt_sha256")}}
    asks[session] = _ask_boundary(old, recorded, now, session)
    return why


def confirm(request_id: str, *, env=None, now: float | None = None) -> tuple[dict, dict]:
    """(the held request, its confirm stamp), claimed for applying, or
    ``DirectionRefused``. Every refusal a change has runs first; then the
    proof (``_proof``). A confirm that finds no reply to the latest ask
    grows the ask boundary and says to ask the owner now. The request is
    marked ``applying`` under the lock, so two confirms never apply it
    twice; ``settle`` records the outcome."""
    from khipu import t3

    now = _clock() if now is None else now
    environ = os.environ if env is None else env
    session = _caller_session(environ)
    try:
        with _requests_locked() as path:
            state = _load(path)
            request = next((r for r in state["requests"] if r.get("id") == request_id), None)
            if request is None:
                raise DirectionRefused("not_found", "There is no held request with that id.")
            if request.get("state") != "pending":
                raise DirectionRefused("not_pending", f"That request is already {request.get('state')}.")
            if not owner_turns.settings()["enabled"] and request.get("action") != "direction.on":
                raise DirectionRefused("direction_off", "Owner direction is off, so only the Khipu app or a yes "
                                                        "typed on a terminal can make this change.")
            if not reviewed_hold(request):
                raise DirectionRefused("legacy_hold", "This old held change has no saved content to apply. Decline it or ask for it to be reissued with its full content.")
            with t3.owner_read_budget():
                member = owner_turns._t3_membership(session)
                if member == "unverified":
                    raise DirectionRefused("t3_unreadable", _T3_UNREADABLE_WHY)
                stamp = _proof(environ, session, request, now)
            if isinstance(stamp, str):
                _save(path, state, now)
                raise DirectionRefused("not_confirmed", stamp)
            stamp["confirmed"]["request"] = request_id
            request.update(state="applying", confirmed=dict(stamp["confirmed"], session=session))
            _save(path, state, now)
    except OSError as err:
        raise DirectionRefused("log_unwritable", f"Khipu can't read or write the held requests ({err}), so nothing "
                                                 "was changed.") from None
    return request, stamp


DECLINE_ACTION = "direction.decline"


def decline(request_id: str, *, env=None, now: float | None = None) -> dict:
    """The owner's no to a held request: it is dropped and nothing changes.
    The proof a confirm needs, so a conversation can't answer for the owner
    either way; with no reply since the ask it asks again, as a confirm does."""
    from khipu import t3

    now = _clock() if now is None else now
    environ = os.environ if env is None else env
    session = _caller_session(environ)
    try:
        with _requests_locked() as path:
            state = _load(path)
            request = next((r for r in state["requests"] if r.get("id") == request_id), None)
            if request is None:
                raise DirectionRefused("not_found", "There is no held request with that id.")
            if request.get("state") != "pending":
                raise DirectionRefused("not_pending", f"That request is already {request.get('state')}.")
            if not owner_turns.settings()["enabled"] and request.get("action") != "direction.on":
                raise DirectionRefused("direction_off", "Owner direction is off, so only the Khipu app or a yes "
                                                        "typed on a terminal can answer this request.")
            with t3.owner_read_budget():
                stamp = _proof(environ, session, request, now)
            if isinstance(stamp, str):
                _save(path, state, now)
                raise DirectionRefused("not_confirmed", stamp.replace("direction.confirm", "direction.decline"))
            line = {"kind": "action", "id": _new_id(), "at": _iso(now), "action": DECLINE_ACTION,
                    "target": request_id, "before": None, "auth": stamp.get("auth"), "undo": None,
                    "no_undo": "Nothing was changed, so there is nothing to undo.", "state": "applied",
                    "facts": {"declined": request.get("action"), "request_session": request.get("session")}}
            line.update({k: stamp[k] for k in ("harness", "session", "turn", "prompt_sha256", "occurrence", "binding",
                                               "confirmed") if stamp.get(k) is not None})
            from khipu import needs_you

            origin = "this conversation" if request.get("session") == session else "another conversation"
            sentence = f"Declined the request from {origin} to {needs_you.ACTION_WORDS.get(request.get('action'), 'make a change')}. Nothing was changed."
            line["receipt"] = {"what": "Declined a held request", "text": sentence}
            _append(line)
            request.update(owner_action=line["id"], receipt=sentence, state="declined", settled_ts=now, settled_at=_iso(now),
                           declined=dict(stamp["confirmed"], session=session))
            _save(path, state, now)
    except OSError as err:
        raise DirectionRefused("log_unwritable", f"Khipu can't read or write the held requests ({err}), so nothing "
                                                 "was changed.") from None
    _write_receipt(sentence, line, stamp)
    return request


_ERROR_CODE = re.compile(r"[a-z][a-z0-9_.]{0,63}")
_SETTLE_ERROR = "The held change was not applied."


def settle(request_id: str, ok: bool, result: dict | None = None, *, now: float | None = None) -> None:
    """Record how an ``applying`` request ended. Only its state, the time and,
    on a failure, a fixed code and a generic message are written: never the
    action's arguments or the command's text, and no field Forget removed
    while it was applying comes back."""
    now = _clock() if now is None else now
    with _requests_locked() as path:
        state = _load(path)
        for row in state["requests"]:
            if row.get("id") != request_id or row.get("state") != "applying":
                continue
            row.update(state="applied" if ok else "failed", settled_ts=now, settled_at=_iso(now))
            if not ok:
                code = result.get("code") if isinstance(result, dict) else None
                row.update(error_code=code if isinstance(code, str) and _ERROR_CODE.fullmatch(code) else "failed",
                           error=_SETTLE_ERROR)
        _save(path, state, now)


# Everything on a request that holds the owner's words or marks their
# conversation's messages: the action's args, the ask line, an error, and
# the ask boundaries (T3 message ids and times, turn ids and hashes).
_REQUEST_WORDS = ("args", "ask", "by", "error", "t3_snapshots", "asks", "shown", "files", "disclosed", "review")


def forget_requests(sessions, *, protected_sessions=(), now: float | None = None) -> dict:
    """Forget's owner words scan on the held requests: a pending request
    these sessions made is withdrawn (reason "forgotten"), and every one
    they made loses its words and ask boundaries (``_REQUEST_WORDS``); on
    every other request, the ask boundaries these sessions made (by their
    ``by``; one that names no maker too) are removed. Ids, times and hashes
    stay. Always under the requests lock, and always bumps the erasure
    generation, so a hold that began before it is refused. Raises OSError
    when the file can't be read or rewritten."""
    now = _clock() if now is None else now
    names = set(sessions) - set(protected_sessions)
    out = {"requests_withdrawn": 0, "requests_scrubbed": 0, "request_boundaries_scrubbed": 0}
    if not names:
        return out
    with _requests_locked() as path:
        state = _load(path)
        state["erased"] += 1
        for row in state["requests"]:
            if row.get("session") in names:
                if row.get("state") == "pending":
                    row.update(state="withdrawn", reason="forgotten", settled_ts=now, settled_at=_iso(now))
                    out["requests_withdrawn"] += 1
                elif any(k in row for k in _REQUEST_WORDS):
                    out["requests_scrubbed"] += 1
                for key in _REQUEST_WORDS:
                    row.pop(key, None)
                continue
            removed = 0
            for key in ("asks", "t3_snapshots"):
                boundaries = row.get(key)
                if not isinstance(boundaries, dict):
                    continue
                for name in list(boundaries):
                    makers = set(_makers(boundaries[name])) | ({name} if key == "asks" else set())
                    if not _makers(boundaries[name]) or makers & names:
                        del boundaries[name]
                        audience = ("session:" if key == "asks" else "t3:") + name
                        row["disclosed"] = [d for d in row.get("disclosed") or [] if d != audience]
                        removed += 1
                if not boundaries:
                    row.pop(key)
            out["request_boundaries_scrubbed"] += removed
        _save(path, state, now)
    return out


@contextlib.contextmanager
def confirmed_replay(action: str, stamp: dict, *, files: dict | None = None):
    """While the held request's own command runs, the check returns the
    confirm stamp for that action in that conversation, and nothing else."""
    token = _CONFIRMED.set({"action": action, "stamp": dict(stamp), "files": files or {}})
    try:
        yield
    finally:
        _CONFIRMED.reset(token)


def replaying(action: str) -> bool:
    """Whether this runs inside the replay of a held request for ``action``
    that ``confirm`` proved the owner confirmed (``confirmed_replay``): a
    context of this process, never something a command's caller can pass."""
    confirmed = _CONFIRMED.get()
    return (confirmed is not None and confirmed.get("action") == action
            and bool(((confirmed.get("stamp") or {}).get("confirmed") or {}).get("request")))


_CHAIN_MOST = 16
# Processes that sit between a harness and the command it runs (its shells,
# Khipu's own Python, a launcher), passed over to find the harness that
# runs this call.
_PASS_THROUGH = ("env", "timeout", "nohup", "uv", "uvx")


def _caller_chain() -> list[tuple[int, float, str]]:
    """This process's ancestors, parent first, as (pid, start time, the
    name of the file it runs). Stops where a parent started after its child
    (its number was reused), and at a process with no path that no hook saw
    with one (``owner_turns._known_info``)."""
    me = owner_turns._proc_info(os.getpid())
    if me is None:
        return []
    pid, born = me[0], me[2]
    out: list[tuple[int, float, str]] = []
    for _ in range(_CHAIN_MOST):
        info = owner_turns._known_info(pid)
        if info is None or info[2] > born:
            break
        out.append((pid, info[2], os.path.basename(os.path.realpath(info[1]))))
        pid, born = info[0], info[2]
        if pid <= 1:
            break
    return out


def _nearest_harness(chain: list[tuple[int, float, str]]) -> tuple[int, float] | None:
    """The first ancestor that is not a shell, an interpreter or a launcher:
    the harness whose conversation runs this call. A dispatched job (``claude
    -p``, ``codex exec``, a T3 delegate) is a harness of its own, so its
    commands find it, never the conversation that started it."""
    for pid, start, name in chain:
        low = name.lower()
        if low in owner_turns.HOOK_SHELLS or low in owner_turns.REFUSED_ISSUERS \
                or low.startswith(owner_turns.REFUSED_ISSUER_PREFIXES) or low in _PASS_THROUGH:
            continue
        return pid, start
    return None


_PROGRAM_NAMES = {"claude_code": "Claude", "codex": "Codex", "cursor": "Cursor", "antigravity": "Antigravity",
                  "aegis": "Grok"}


def _program_replaced() -> bool:
    """Whether the walk up to this call's harness ends at a process with no
    path and no record of one: its file went before any hook saw it, so only
    restarting it gets a proof back. Nothing above the harness counts: this
    conversation's restart would not touch it. Says why a refusal happened;
    never decides one."""
    me = pid = os.getpid()
    for _ in range(_CHAIN_MOST):
        info = owner_turns._known_info(pid)
        if info is None:
            return owner_turns._proc_bare(pid) is not None
        name = os.path.basename(os.path.realpath(info[1]))
        if (pid != me and _nearest_harness([(pid, info[2], name)])) or info[0] <= 1:
            return False
        pid = info[0]
    return False


def _replaced_why(turn: dict) -> str | None:
    """The refusal's real reason when the program running this conversation
    was updated before any hook saw it (``_program_replaced``), which no later
    message here can mend. Never for an issuer refused for what it is: a
    restart would not change that."""
    issuer = turn.get("issuer")
    if not isinstance(issuer, dict) or issuer.get("refused") not in (None, "unknown") or not _program_replaced():
        return None
    name = _PROGRAM_NAMES.get(turn.get("harness"), "agent")
    return (f"This conversation's {name} program, or a program it runs, was updated while running, before Khipu had "
            "seen it, so Khipu can't tie this call to this conversation's prompt hook. Nothing was changed, and "
            f"repeating the message here won't help. Restart this conversation's {name} program if the owner is "
            "typing in it, then have them type the request again; a background or dispatched job can't make an "
            "owner's change, restarted or not.")




def _float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0



# A file the word-matching design kept (widenings waiting for a yes, with
# the owner's words). Nothing writes it now; the timer and forget delete it.
OLD_PENDING_NAME = "owner-direction-pending.json"


def remove_old_widenings() -> int:
    """Delete the old widenings file, if one is left. Returns 1 when it was
    there. Raises OSError when it can't be deleted."""
    from khipu.paths import data_dir

    path = data_dir() / OLD_PENDING_NAME
    if not path.exists():
        return 0
    path.unlink()
    return 1


# ---- the owner-actions log -------------------------------------------------------------------


def log_path() -> Path:
    from khipu.paths import data_dir

    return data_dir() / LOG_NAME


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


# The log is hash-chained (scope 2.8): each line carries ``v``
# (``LOG_VERSION``), ``prev``, the hash of the line before it, and ``hash``,
# over its own canonical form. The words are in that form only as
# ``words_sha``, so forgetting an episode can scrub a line's words (``"words":
# null, "scrubbed": true``) without breaking the chain. Lines from before the
# chain (no version and no hash on any of them) may lead the log; the first
# chained line's ``prev`` is then ``LEGACY`` plus the digest of those lines,
# so changing them breaks the chain too.
#
# A chain alone can't show lines cut from its end, so every append also
# writes the chain's length and last hash to a second file beside the log
# (``HEAD_NAME``); verify compares the two.
_UNHASHED = ("hash", "words", "scrubbed")
LEGACY = "legacy:"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _line_hash(line: dict) -> str:
    body = {k: v for k, v in line.items() if k not in _UNHASHED}
    return _sha(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str))


def _legacy_form(text: str) -> str:
    """A line from before the chain as its digest sees it: words only as
    their hash, so scrubbing them (``scrub_words``) leaves the digest as it
    was, while any other change to the line still breaks the chain."""
    try:
        row = json.loads(text)
    except ValueError:
        return text
    if not isinstance(row, dict) or ("words" not in row and "words_sha" not in row):
        return text
    words = row.get("words")
    body = {k: v for k, v in row.items() if k not in _UNHASHED}
    body["words_sha"] = _sha(words) if isinstance(words, str) else row.get("words_sha")
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _legacy_prev(lines: list[str]) -> str:
    return LEGACY + _sha("".join(_legacy_form(text) + "\n" for text in lines))


def _prev_of(raw: str) -> str:
    lines = raw.splitlines()
    last = next((text for text in reversed(lines) if text.strip()), None)
    if last is None:
        return ""
    try:
        row = json.loads(last)
    except ValueError:
        row = None
    if isinstance(row, dict) and isinstance(row.get("hash"), str):
        return row["hash"]
    return _legacy_prev(lines)


@contextlib.contextmanager
def _log_locked():
    """The log's lock (a sidecar file): one writer at a time."""
    import fcntl

    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(path.with_name(f".{path.name}.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield path
    finally:
        os.close(lock_fd)


def head_path() -> Path:
    return log_path().with_name(HEAD_NAME)


def _chained(raw: str) -> int:
    count = 0
    for text in raw.splitlines():
        try:
            row = json.loads(text)
        except ValueError:
            continue
        if isinstance(row, dict) and ("hash" in row or "v" in row):
            count += 1
    return count


def segment_name(seq: int) -> str:
    return f"owner-actions.{seq:06d}.jsonl"


def _sealed_paths() -> list[Path]:
    """The sealed segments, oldest first (name order is sequence order)."""
    folder = log_path().parent
    try:
        names = sorted(n for n in os.listdir(folder) if _SEGMENT.match(n))
    except OSError:
        return []
    return [folder / n for n in names]


def _segment_seq(path: Path) -> int:
    return int(_SEGMENT.match(path.name).group(1))


def _log_files() -> list[Path]:
    """Every file that holds log lines, in order: sealed segments, then the log."""
    return [*_sealed_paths(), log_path()]


def _file_tail(path: Path) -> str:
    """What the next line chains to after this segment: its last line's hash,
    found by reading the end of the file (the whole of it for a segment of
    lines from before the chain)."""
    try:
        size = path.stat().st_size
        chunk = 65536
        with open(path, "rb") as fh:
            while True:
                fh.seek(max(0, size - chunk))
                lines = [t for t in fh.read(min(size, chunk)).split(b"\n") if t.strip()]
                if len(lines) >= 2 or size <= chunk:
                    break
                chunk *= 4
        row = json.loads(lines[-1]) if lines else None
        if isinstance(row, dict) and isinstance(row.get("hash"), str):
            return row["hash"]
    except ValueError:
        pass
    return _prev_of(path.read_text(encoding="utf-8"))


def _seal(path: Path, raw: str, sealed: list[Path]) -> str:
    """With the log lock held: rename the current log to the next sealed
    segment and restart the head beside it. Returns the hash the next line
    chains to."""
    tail = _prev_of(raw)
    seq = (_segment_seq(sealed[-1]) if sealed else 0) + 1
    dest = path.with_name(segment_name(seq))
    if dest.exists():
        raise OSError(f"{dest.name} already exists")
    os.replace(path, dest)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        _write_private(head_path(), json.dumps({"v": 2, "seq": seq, "lines": 0, "head": tail}) + "\n")
    except OSError:
        pass  # the check reads a head one segment behind as a seal that was cut short
    return tail


def _append(line: dict) -> dict:
    """Append one line to the chain, under a lock so two writers never
    chain to the same line, and on disk before it returns; then the chain's
    new length and head beside it. A head that could not be written leaves
    the check red (the log is ahead of it), never the change undone. Only the
    current segment is read and checked; a full current segment is sealed
    first (``SEGMENT_MAX_LINES``)."""
    with _log_locked() as path:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raw = ""
        line = json.loads(json.dumps(line, ensure_ascii=False, default=str))
        for key in ("prev", "hash", "scrubbed"):
            line.pop(key, None)
        if isinstance(line.get("words"), str):
            line["words_sha"] = _sha(line["words"])
        else:
            line.pop("words_sha", None)
        try:
            before = _verify_chain(current_only=True, raw=raw)
        except Exception:  # noqa: BLE001 - the check never stops an owner's change from being logged
            before = None
        if before is not None and before["first_bad"]:
            _raise_alarm(before["first_bad"])
        sealed = _sealed_paths()
        incoming = _file_tail(sealed[-1]) if sealed else ""
        if raw.count("\n") >= SEGMENT_MAX_LINES:
            incoming = _seal(path, raw, sealed)
            sealed, raw = _sealed_paths(), ""
        line["v"] = LOG_VERSION
        line["prev"] = _prev_of(raw) or incoming
        line["hash"] = _line_hash(line)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(("\n" if raw and not raw.endswith("\n") else "") + json.dumps(line, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        try:
            _write_private(head_path(), json.dumps({"v": 2, "seq": len(sealed), "lines": _chained(raw) + 1,
                                                    "head": line["hash"]}) + "\n")
        except OSError:
            pass
    return line


def scrub_words(sessions, *, protected_sessions=()) -> int:
    """Forget's owner words scan on the log: blank the quoted words of every
    line these sessions wrote or acted for (``"words": null, "scrubbed":
    true``). ``words_sha`` and the hash stay, so the chain still holds and
    the action and its Undo stay. Returns how many lines were scrubbed;
    raises OSError when the log can't be read or rewritten. A line linked
    to a protected live session keeps its words even if its other session
    is being forgotten."""
    names = set(sessions)
    protected = set(protected_sessions)
    total = 0
    with _log_locked():
        for path in _log_files():
            try:
                raw = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                continue
            out, count = [], 0
            for text in raw.splitlines():
                try:
                    row = json.loads(text)
                except ValueError:
                    row = None
                if (isinstance(row, dict) and isinstance(row.get("words"), str)
                        and (row.get("session") in names or row.get("via_session") in names)
                        and row.get("session") not in protected and row.get("via_session") not in protected):
                    row["words_sha"] = row.get("words_sha") or _sha(row["words"])
                    row["words"], row["scrubbed"] = None, True
                    text, count = json.dumps(row, ensure_ascii=False), count + 1
                out.append(text)
            if count:
                _write_private(path, "".join(text + "\n" for text in out))
                total += count
    return total


def _head() -> dict | None | type:
    """The chain's length and head as last written beside the log: None
    when there is no such file, ``ValueError`` when it can't be read."""
    try:
        data = json.loads(head_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return ValueError
    if not (isinstance(data, dict) and isinstance(data.get("lines"), int) and isinstance(data.get("head"), str)):
        return ValueError
    return data


def alarm_path() -> Path:
    return log_path().with_name(ALARM_NAME)


def read_alarm() -> dict | None:
    """The tamper alarm, while it waits for the owner's acknowledgement."""
    try:
        data = json.loads(alarm_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        # An alarm file that can't be read is still an alarm.
        return {"at": None, "first_bad": {"line": 0, "id": None, "kind": None,
                                          "why": "the record of an earlier break can't be read"}}
    return data if isinstance(data, dict) and isinstance(data.get("first_bad"), dict) else None


def _raise_alarm(bad: dict) -> None:
    """Keep the first break found until the owner acknowledges it. Best
    effort: the live check still shows a break it can see."""
    if read_alarm() is not None:
        return
    try:
        _write_private(alarm_path(), json.dumps({"at": _iso(_clock()), "first_bad": bad}) + "\n")
    except OSError:
        pass


def acknowledge_alarm() -> dict | None:
    """The owner has seen the tamper alarm: it is cleared (the alarm it was,
    or None when none was raised). A break the log still shows raises it
    again at the next check."""
    alarm = read_alarm()
    with contextlib.suppress(FileNotFoundError):
        alarm_path().unlink()
    return alarm


def verify_log() -> dict:
    """Whether the log holds: the chain as it stands (``_verify_chain``), and
    no tamper alarm waiting for the owner. A break found now raises the
    alarm; while it waits, the log reads as broken at the entry it named,
    however the log looks since, and ``alarm`` carries it."""
    # Under the log's lock, so an append's seal (the rename and the head rewrite)
    # is never caught half done and read as a break. Nothing here is called with
    # the lock held; an append calls ``_verify_chain`` itself.
    if log_path().parent.exists():
        with _log_locked():
            out = _verify_chain()
    else:
        out = _verify_chain()
    if out["first_bad"]:
        _raise_alarm(out["first_bad"])
    alarm = read_alarm()
    if alarm is None:
        return out
    if out["ok"]:
        bad = dict(alarm["first_bad"])
        bad["why"] = (f"{bad.get('why') or 'the log was changed'} (found {alarm.get('at') or 'earlier'}; this "
                      "alarm stays until the owner acknowledges it)")
        out = {"ok": False, "lines": out["lines"], "first_bad": bad}
    out["alarm"] = alarm
    return out


def _split_rows(raw: str) -> tuple[list[str], list]:
    texts = raw.splitlines()
    rows: list = []
    for text in texts:
        try:
            rows.append(json.loads(text) if text.strip() else None)
        except ValueError:
            rows.append(ValueError)
    return texts, rows


def _walk(texts: list[str], rows: list, incoming: str | None, base: int, segment: str) -> dict:
    """The chain inside one segment. ``incoming`` is the hash the segment's
    first line must chain to (None for the first segment of the log, which may
    open with lines from before the chain); ``base`` is the number of lines in
    the segments before it, so line numbers count from the first segment."""
    count = sum(1 for r in rows if r is not None)

    def bad(i: int, why: str) -> dict:
        row = rows[i] if 0 <= i < len(rows) and isinstance(rows[i], dict) else {}
        found = {"line": base + i + 1, "id": row.get("id"), "kind": row.get("kind"), "why": why}
        if segment != LOG_NAME:
            found["segment"] = segment
        return {"ok": False, "lines": base + count, "first_bad": found}

    first = next((i for i, r in enumerate(rows) if isinstance(r, dict) and ("hash" in r or "v" in r)), None)
    res: dict = {"rows": rows, "count": count, "first": first, "chained": 0, "last": None, "bad": None, "bad_fn": bad}
    if incoming is not None:
        lead = next((i for i, r in enumerate(rows) if r is not None), None)
        if lead is not None and first != lead:
            res["bad"] = bad(lead, "it follows an earlier segment of the log, so it must be chained to it")
            return res
    start = first if first is not None else len(rows)
    for i in range(start):
        if rows[i] is ValueError:
            res["bad"] = bad(i, "it is not a readable line")
            return res
    if first is not None:
        prev = incoming if incoming is not None else (_legacy_prev(texts[:first]) if first else "")
        chained, last = 0, None
        for i in range(first, len(rows)):
            row = rows[i]
            if row is None:
                continue
            why = None
            if not isinstance(row, dict):
                why = "it is not a readable line"
            elif not isinstance(row.get("hash"), str):
                why = "it has no hash, so Khipu did not write it"
            elif row.get("v") != LOG_VERSION:
                why = "it has no version or an older one after the chain began, so Khipu did not write it"
            elif row.get("prev") != prev:
                if first and i == first and str(row.get("prev") or "").startswith(LEGACY):
                    why = "a line written before the log was chained was changed"
                else:
                    why = "it does not follow the line before it: a line is missing or was changed"
            elif _line_hash(row) != row["hash"]:
                why = "it was changed after Khipu wrote it"
            elif row.get("words") is not None and (not isinstance(row.get("words"), str)
                                                  or row.get("words_sha") != _sha(row["words"])):
                why = "its words do not match their hash"
            if why:
                res["bad"] = bad(i, why)
                return res
            prev, chained, last = row["hash"], chained + 1, i
        res["chained"], res["last"] = chained, last
    return res


def _verify_chain(*, current_only: bool = False, raw: str | None = None) -> dict:
    """Whether the log's hash chain holds: ``first_bad`` names the first line
    that has no hash or a wrong one, no version or an older one after the
    chain began, does not follow the line before it, or whose words do not
    match their hash; or, by the head kept beside the log, the line where
    lines Khipu wrote are missing from the end (line numbers count from 1,
    through every sealed segment).

    ``current_only`` (the append's check) reads the current segment alone,
    chained to the last sealed segment's end; it reads them all when the head
    does not agree with the segments on disk, and ``verify_log`` always does."""
    sealed = _sealed_paths()
    head = _head()
    if current_only and sealed and (not isinstance(head, dict) or head.get("seq", 0) != len(sealed)):
        current_only = False
    base, incoming, last_sealed, note = 0, None, None, None
    try:
        if sealed and current_only:
            incoming = _file_tail(sealed[-1])
        elif sealed:
            expected = 1
            for seg in sealed:
                if _segment_seq(seg) != expected:
                    return {"ok": False, "lines": base, "first_bad": {
                        "line": base + 1, "id": None, "kind": None, "segment": seg.name,
                        "why": f"sealed segment {segment_name(expected)} is missing"}}
                expected += 1
                texts, rows = _split_rows(seg.read_text(encoding="utf-8"))
                res = _walk(texts, rows, incoming, base, seg.name)
                if res["bad"]:
                    return res["bad"]
                if note is None and incoming is None:
                    first = res["first"]
                    note = ("Every line was written before the log was chained." if first is None and res["count"]
                            else f"The first {first} lines were written before the log was chained." if first else "")
                tail = _prev_of("\n".join(texts)) or incoming
                last_sealed = (res["chained"], tail)
                incoming, base = tail, base + res["count"]
    except OSError as err:
        return {"ok": False, "lines": base, "first_bad": {"line": base + 1, "id": None, "kind": None,
                                                           "why": f"the log can't be read ({err.strerror})"}}
    if raw is None:
        try:
            raw = log_path().read_text(encoding="utf-8")
        except FileNotFoundError:
            raw = ""
        except OSError as err:
            return {"ok": False, "lines": base, "first_bad": {"line": base + 1, "id": None, "kind": None,
                                                               "why": f"the log can't be read ({err.strerror})"}}
    texts, rows = _split_rows(raw)
    res = _walk(texts, rows, incoming, base, LOG_NAME)
    if res["bad"]:
        return res["bad"]
    bad, first, chained, last = res["bad_fn"], res["first"], res["chained"], res["last"]
    start = first if first is not None else len(rows)
    if head is ValueError:
        return bad(0, "the record of the log's length kept beside it can't be read")
    if head is not None and head.get("seq", 0) != len(sealed):
        if (head.get("seq", 0) == len(sealed) - 1 and last_sealed is not None
                and head["lines"] == last_sealed[0] and head["head"] == last_sealed[1]):
            head = {"lines": 0, "head": last_sealed[1], "seq": len(sealed)}  # a seal cut short before its head
        else:
            return bad(0, "the sealed segments of the log do not match the record kept beside it: "
                          "one is missing or was added")
    if head is None:
        if chained or sealed:
            return bad(first or 0, "the record of the log's length kept beside it is missing")
    else:
        end = (last + 1) if last is not None else start
        if chained < head["lines"]:
            gone = head["lines"] - chained
            return bad(end, f"{gone} line{'s' if gone != 1 else ''} Khipu wrote {'are' if gone != 1 else 'is'} "
                            "missing from the end of the log")
        if chained == head["lines"] and chained and rows[last]["hash"] != head["head"]:
            return bad(last, "the log's last line is not the one Khipu wrote last")
        # An append that stopped before its head was written leaves the log
        # one line ahead, chained to the head.
        ahead = chained == head["lines"] + 1 and rows[last]["prev"] == head["head"]
        if chained > head["lines"] and not ahead:
            return bad(last, "the log has lines past the record of its length kept beside it")
    out = {"ok": True, "lines": base + res["count"], "first_bad": None, "segments": len(sealed)}
    if note is None:
        if first is None and res["count"]:
            note = "Every line was written before the log was chained."
        elif first:
            note = f"The first {first} lines were written before the log was chained."
    if note:
        out["note"] = note
    return out


def doctor_block() -> dict:
    """Doctor's "owner actions log" row: red when the hash chain is broken,
    naming the first bad line."""
    chain = verify_log()
    bad = chain.get("first_bad")
    if bad:
        what = f"line {bad['line']}" + (f" ({bad['id']})" if bad.get("id") else "")
        summary = (f"The owner-actions log was changed outside Khipu at {what}: {bad['why']}. "
                   "Check that entry in Owner actions.")
        return {"ok": False, "chain": chain, "warnings": [summary], "summary": summary}
    segments = f" in {chain['segments'] + 1} files" if chain.get("segments") else ""
    summary = f"Owner-actions log: {chain['lines']} lines{segments}, chain intact."
    if chain.get("note"):
        summary += " " + chain["note"]
    return {"ok": True, "chain": chain, "warnings": [], "summary": summary}


def read_log() -> list[dict]:
    """Every line, oldest first, through every sealed segment."""
    out = []
    for path in _log_files():
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for text in raw.splitlines():
            try:
                row = json.loads(text)
            except ValueError:
                continue
            if isinstance(row, dict):
                out.append(row)
    return out


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id() -> str:
    return secrets.token_hex(4)


# What a state line may add to the line it completes (``finish``).
_LINE_FIELDS = ("kind", "id", "at", "target", "state", "prev", "hash", "words", "words_sha", "scrubbed")


def _merged(rows: list[dict]) -> dict[str, dict]:
    """Every action and undo line by id as it stands now: its completion's
    fields merged in and ``state`` resolved. A line written ahead that was
    never completed is ``unconfirmed``; an action is ``undone`` by an undo
    that was applied, and unconfirmed by one that is (whether it is undone
    is not known). The undo this process is making now (``_PENDING``) does
    not count yet."""
    inflight = ((_PENDING.get() or {}).get("line") or {}).get("id")
    lines: dict[str, dict] = {}
    for row in rows:
        kind = row.get("kind")
        if kind in ("action", "undo") and isinstance(row.get("id"), str):
            lines[row["id"]] = dict(row, state=row.get("state") or ("applied" if kind == "undo" else None))
            if kind == "action" and lines[row["id"]]["state"] is None:
                lines[row["id"]]["state"] = "applied"
        elif kind == "state" and row.get("target") in lines:
            line = lines[row["target"]]
            line.update({k: v for k, v in row.items() if k not in _LINE_FIELDS})
            line["state"] = row.get("state")
    for line in lines.values():
        if line["state"] == "pending":
            line["state"] = "unconfirmed"
    for row in rows:
        if (row.get("kind") != "undo" or row.get("undoes") not in lines or row.get("id") not in lines
                or row["id"] == inflight):
            continue
        undo_state, line = lines[row["id"]]["state"], lines[row["undoes"]]
        # "partial": the undo left something it could not prove was its to revert.
        if line["state"] in ("cancelled", "done", "failed") or undo_state in ("failed", "partial"):
            continue
        line["state"] = "unconfirmed" if undo_state == "unconfirmed" else "undone"
    return lines


def actions() -> list[dict]:
    """The owner actions in log order, each as it stands now (``_merged``)."""
    return [line for line in _merged(read_log()).values() if line.get("kind") == "action"]


def line_state(line_id: str) -> str | None:
    """A logged action's (or undo's) state now: ``applied``, ``scheduled``,
    ``done``, ``cancelled``, ``undone``, ``failed`` (it did not happen),
    ``unconfirmed`` (written ahead, never completed), or None when there is
    no such line."""
    line = _merged(read_log()).get(line_id)
    return line["state"] if line else None


def _find(line_id: str) -> dict | None:
    line = _merged(read_log()).get(line_id)
    return line if line and line.get("kind") == "action" else None


# ---- around an owner write (every gate) --------------------------------------------------------

_PENDING: contextvars.ContextVar[dict | None] = contextvars.ContextVar("khipu_owner_action", default=None)


def operation_id(event: dict) -> str | None:
    """Only the intended write inherits the written-ahead action's identity."""
    pend = _PENDING.get()
    if not pend:
        return None
    via = {"direction": "direction", "app": "app", "terminal": "cli"}.get(pend["stamp"].get("auth"))
    if event.get("via") != via:
        return None
    action = pend["intent"]["action"]
    if action in {"route.rule.add", "route.hold.add"} and event.get("id") != planned_id(action):
        return None
    return (pend.get("line") or {}).get("id")


def planned_id(action: str) -> str | None:
    """The id the owner action being written chose for the line it adds
    (a rule or a hold), so the writer uses it; None for any other write."""
    pend = _PENDING.get()
    if not pend or pend["intent"]["action"] != action or not isinstance(pend.get("before"), dict):
        return None
    return pend["before"].get({"route.rule.add": "event", "route.hold.add": "hold"}.get(action, ""))


def intent(action: str, target: str, *, facts: dict | None = None) -> dict:
    """What an owner command is about to do, for the check and the log."""
    return {"action": action, "target": str(target), "facts": dict(facts or {})}


def gate(want: dict | None, *, env=None, now: float | None = None) -> dict:
    """The direction branch of an owner gate: the auth stamp, after the check
    passed and the action can be undone. Raises ``DirectionRefused``."""
    if want is None:
        raise DirectionRefused("not_by_direction",
                               "That change can't be made by direction: it needs the Khipu app or a yes typed on a "
                               "terminal.")
    stamp = check(want["action"], env=env, now=now, instant=instant_allowed(want["action"], want["target"]))
    want.pop("over_newer", None)
    want.pop("owner_words", None)
    before = _before(want)
    if before is _UNREADABLE:
        raise DirectionRefused("before_unreadable",
                               "Khipu can't read what this would change right now, so it could not be undone; "
                               "nothing was changed. Try again when the database is back.")
    _abandon()
    pend = {"intent": want, "stamp": stamp, "before": before, "stamped": _routing_stamps()}
    try:
        pend["line"] = _append(_ahead_line(want, stamp, before))
    except OSError as err:
        raise DirectionRefused("log_unwritable",
                               "Khipu can't write the owner-actions log, where this change's Undo is kept "
                               f"({err.strerror or err}), so nothing was changed. Try again once the disk can be "
                               "written.") from None
    _PENDING.set(pend)
    return stamp


def noted(want: dict | None, stamp: dict) -> None:
    """The app's or a terminal's owner action, to be logged once it is done:
    written ahead like a direction's when the log can be written, and never
    holding up the owner's own change when it can't."""
    _abandon()
    if want is None:
        return
    try:
        before = _before(want)
    except Exception:  # noqa: BLE001 - the log never stops the owner's own change
        before = None
    pend = {"intent": want, "stamp": dict(stamp), "before": before, "stamped": _routing_stamps()}
    try:
        pend["line"] = _append(_ahead_line(want, pend["stamp"], before))
    except Exception:  # noqa: BLE001 - the whole line is tried again once the change is done
        pass
    _PENDING.set(pend)


def pending_direction() -> dict | None:
    pend = _PENDING.get()
    return pend["stamp"] if pend and pend["stamp"].get("auth") == "direction" else None


def record_owed_batch(batch: str) -> None:
    """Persist a batch's identity before its transaction can change items."""
    pend = _PENDING.get()
    if not pend or pend["intent"]["action"] not in ("owed.change", "owed.tidy", "owed.correct_job_origin.confirm"):
        return
    line = pend.get("line")
    if line is None:
        raise OSError("the owner action must be recorded before its batch can be written")
    before = dict(pend["before"]) if isinstance(pend.get("before"), dict) else {}
    if pend["intent"]["action"] in ("owed.change", "owed.correct_job_origin.confirm"):
        before["batches"] = [*(before.get("batches") or []), batch]
    else:
        before["batch"] = batch
    _append({"kind": "state", "id": _new_id(), "at": _iso(_clock()), "target": line["id"],
             "state": "pending", "before": before})
    pend["before"] = before


def record_owed_undo_scope(batch: str, ids: list[int]) -> None:
    """Write the exact successful inverse scope before the hub can commit.

    The writer starts with an empty scope and replaces it after successful
    restores. Requested selectors remain in the action's facts, never its
    inverse. A logging failure must propagate and roll back the hub write.
    """
    pend = _PENDING.get()
    if not pend or pend["intent"]["action"] != "owed.tidy":
        return
    facts = pend["intent"].get("facts") or {}
    if facts.get("change") != "undo" or facts.get("batch") != batch:
        return
    line = pend.get("line")
    if line is None:
        raise OSError("the owner action must be recorded before its undo can be written")
    before = dict(pend.get("before") or {}, ids=list(ids))
    _append({"kind": "state", "id": _new_id(), "at": _iso(_clock()), "target": line["id"],
             "state": "pending", "before": before})
    pend["before"] = before


def record_preference_merges(entries: list[dict]) -> None:
    """Persist one transaction's preference additions (episode ids, exact
    strings, prior lists) before it commits: the Undo of the merge."""
    pend = _PENDING.get()
    line = (pend or {}).get("line")
    if not pend or pend["intent"]["action"] != "outbox.merge_preferences" or line is None:
        raise OSError("the owner action must be recorded before preferences can be merged")
    _append({"kind": "state", "id": _new_id(), "at": _iso(_clock()), "target": line["id"],
             "state": "pending", "preference_merges": entries})


_RESOLVING_ACTIONS = ("outbox.merge_preferences", "outbox.span_covered")


def record_preference_resolutions(jobs: list[dict]) -> None:
    """Persist the held jobs a merge is about to archive as resolved (record
    id, job name, where it was held) before any moves: the Undo puts them
    back."""
    pend = _PENDING.get()
    line = (pend or {}).get("line")
    if not pend or pend["intent"]["action"] not in _RESOLVING_ACTIONS or line is None:
        raise OSError("the owner action must be recorded before held lines can be resolved")
    _append({"kind": "state", "id": _new_id(), "at": _iso(_clock()), "target": line["id"],
             "state": "pending", "preference_resolutions": jobs})


def record_preference_commits(episode_ids: list[int]) -> None:
    """Mark one transaction's recorded additions committed, after its commit
    returns: Undo reverts only entries proven committed."""
    pend = _PENDING.get()
    line = (pend or {}).get("line")
    if not pend or pend["intent"]["action"] != "outbox.merge_preferences" or line is None:
        raise OSError("the owner action must be recorded before a merge commit can be marked")
    _append({"kind": "state", "id": _new_id(), "at": _iso(_clock()), "target": line["id"],
             "state": "pending", "preference_commits": [{"episode_id": int(i)} for i in episode_ids]})


def preference_merges(line_id: str, key: str = "preference_merges") -> list[dict]:
    """Every addition a merge recorded (or, with ``preference_resolutions``,
    every job it resolved), in log order. Read from the raw log: the merged
    view keeps only the last state line's fields."""
    out: list[dict] = []
    for row in read_log():
        if row.get("kind") == "state" and row.get("target") == line_id:
            out += [e for e in row.get(key) or [] if isinstance(e, dict)]
    return out


def record_setting_before(key: str, data: dict) -> None:
    """Record the exact replaced state while the setter holds both locks."""
    from khipu import routing

    pend = _PENDING.get()
    if (not pend or pend["intent"]["action"] not in
            ("config.routing_setting", "config.answer_request.confirm", "config.answer_request.undo")
            or pend["intent"]["target"] != key):
        return
    before = {"value": data.get(key), "set": key in data}
    request = routing._setting_requests(data).get(key)
    if request is not None or key == "routing_replication_live":
        before["request"] = request
    line = pend.get("line")
    pend["before"] = before
    if line is None:
        if pend["stamp"].get("auth") == "direction":
            raise OSError("the owner action must be recorded before its setting can change")
        return
    _append({"kind": "state", "id": _new_id(), "at": _iso(_clock()), "target": line["id"],
             "state": "pending", "before": before})


# Actions whose undo restores what the snapshot read: it is read again under
# the routing lock, just before the action's own line is written, so a write
# between the check and the change can never become the state Undo restores.
_SNAPSHOT_UNDER_LOCK = ("route.priority.", "route.cap.", "route.hold.end", "route.rule.edit", "route.rule.undo",
                        "route.rule.retire", "route.rule.dismiss", "route.rule.confirm")


def record_routing_before(events: list[dict]) -> None:
    """Called by the routing writer with the routing lock held, before it
    appends ``events``: when they are this owner action's own lines, record
    the snapshot as it stands now (a ``state`` line, as for a setting)."""
    pend = _PENDING.get()
    if not pend or pend.get("snapshot_locked") or not isinstance(pend.get("line"), dict):
        return
    action, line_id = pend["intent"]["action"], pend["line"]["id"]
    if not action.startswith(_SNAPSHOT_UNDER_LOCK):
        return
    if not any(str(e.get("op") or "") == line_id or str(e.get("op") or "").startswith(f"{line_id}:") for e in events):
        return
    pend["snapshot_locked"] = True  # set first: reading the view may write (a seed import)
    try:
        before = _before(pend["intent"])
        if before is _NO_UNDO or before is _UNREADABLE:
            return
        _append({"kind": "state", "id": _new_id(), "at": _iso(_clock()), "target": line_id,
                 "state": "pending", "before": before})
        pend["before"] = before
    except Exception:
        # As for a setting: a direction's change waits for its Undo to be
        # recorded; the owner's own change from the app is never held up.
        if pend["stamp"].get("auth") == "direction":
            raise


def record_owner_write(tool: str, args: dict) -> None:
    """Keep the hub identity before sending, even if its COMMIT reply is lost."""
    pend = _PENDING.get()
    if not pend or pend["stamp"].get("auth") != "direction":
        return
    line = pend.get("line")
    if line is None:
        raise OSError("the owner action must be recorded before its hub write")
    write = {"tool": tool, "args": {"write_id": args["write_id"]}}
    _append({"kind": "state", "id": _new_id(), "at": _iso(_clock()), "target": line["id"],
             "state": "pending", "owner_write": write})
    pend["owner_write"] = write


def import_source(path, project: str, *, catch_up: bool = False) -> dict | None:
    """The parsed tracker read by this import's gate; never stored in the log."""
    pend = _PENDING.get()
    if pend is None:
        return None
    want = pend["intent"]
    facts = want.get("facts") or {}
    if (want["action"] != "ledger.import" or want["target"] != str(path)
            or facts.get("project") != project or catch_up):
        return None
    return want.get("_import_source")


def _ahead_line(want: dict, stamp: dict, before) -> dict:
    """The line written before an owner change (state ``pending``): what it
    is, who asked and how, and ``before``, so Undo works whatever happens to
    the change after. ``direction.undo`` writes an ``undo`` line."""
    if want["action"] == "direction.undo":
        undone = _find(want["target"]) or {}
        line = {"kind": "undo", "id": _new_id(), "at": _iso(_clock()), "undoes": want["target"],
                "action": undone.get("action"), "target": undone.get("target"), "auth": stamp.get("auth"),
                "inverse_id": "owner-undo:" + want["target"]}
        if not (undone.get("action") or "").startswith("route."):
            line["inverse_started"] = False
        writes = _hub_inverse_writes(undone)
        if writes:
            line["inverse_writes"] = writes
        if want.get("facts"):
            line["facts"] = want["facts"]
    else:
        line = {"kind": "action", "id": _new_id(), "at": _iso(_clock()), "action": want["action"],
                "target": want["target"], "before": None if before in (_NO_UNDO, _UNREADABLE) else before,
                "auth": stamp.get("auth")}
        if want["action"].startswith("route.rule.") and want["action"] != "route.rule.add":
            from khipu import routing

            line["target"] = routing.rule_reference(want["target"])[0]
        why = _no_undo(want["action"], before)
        if why:
            line.update(undo=None, no_undo=why)
        if want["action"] == "ledger.purge" and stamp.get("auth") == "direction":
            # Scheduled from the moment it is written, so Undo and the timer
            # read the same day whether or not the finish is written.
            runs_at = _clock() + PURGE_DELAY_S
            line.update(runs_at=_iso(runs_at), runs_at_ts=runs_at)
        if want.get("facts"):
            line["facts"] = want["facts"]
    for key in ("words", "harness", "session", "via_session", "turn", "prompt_sha256", "occurrence",
                "binding", "auth_chain", "confirmed", "instant"):
        if stamp.get(key) is not None:
            line[key] = stamp[key]
    if REQUEST_APPLIED.get() and stamp.get("instant") is not None:
        line["request_applied"] = True
    line["state"] = "pending"
    return line


def _hub_write_state(write: dict) -> str:
    """Only an unavailable outcome is uncertain; an absent receipt means no write."""
    from khipu import owner_writes

    try:
        outcome = owner_writes._landed(write["tool"], write["args"])
    except Exception:  # noqa: BLE001 - includes a hub without the receipt migration
        return "pending"
    if outcome is None or outcome[0] == "refused":
        return "failed"
    return "applied" if outcome[0] == "applied" else "pending"


def _inverse_state(writes: list[dict]) -> str:
    states = [_hub_write_state(write) for write in writes]
    if "pending" in states:
        return "pending"
    # A partially committed multi-batch inverse can resume: each committed
    # part keeps its own receipt, so retrying cannot apply it twice.
    return "applied" if all(state == "applied" for state in states) else "failed"


def _origin_request_outcome(request_id: str) -> tuple[str, dict | None]:
    """Resolve a correction by its durable request, never by a COMMIT reply.

    A completed SELECT is authoritative even if closing its read transaction
    loses another acknowledgement. No SELECT reply means the outcome is still
    unknown, and Undo must wait rather than disable or replay it.
    """
    from khipu.db import connect

    found, read = None, False
    try:
        with connect(record_reachability=False) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT state, result FROM commitment_origin_requests WHERE id = %s", (request_id,))
                found, read = cur.fetchone(), True
    except Exception:  # noqa: BLE001 - unavailable durable outcome remains uncertain
        if not read:
            return "pending", None
    if found and found[0] == "applied" and isinstance(found[1], dict):
        return "applied", found[1]
    if found and found[0] == "applied":
        return "pending", None
    return "failed", None


def _origin_outcome_fields(state: str, result: dict | None) -> dict:
    fields = {"state": state}
    if result is not None:
        fields.update(result=result, before={"batches": result.get("batches", [])})
    return fields


def _abandon() -> None:
    """A lost hub reply is uncertain until its durable outcome can be read."""
    pend = _PENDING.get()
    _PENDING.set(None)
    if pend and pend.get("line"):
        state = "failed"
        outcome = {}
        if pend["intent"]["action"] == "owed.correct_job_origin.confirm":
            state, result = _origin_request_outcome(pend["intent"]["target"])
            outcome = _origin_outcome_fields(state, result)
        write = pend.get("owner_write")
        if write:
            state = _hub_write_state(write)
        elif pend["line"].get("inverse_writes"):
            state = _inverse_state(pend["line"]["inverse_writes"])
        elif pend["intent"]["action"] in _RESOLVING_ACTIONS and _merge_recorded(pend["line"]["id"]):
            # Batches before the failure committed and their Undo is logged:
            # unconfirmed, so Undo still runs.
            state = "pending"
        try:
            _append({"kind": "state", "id": _new_id(), "at": _iso(_clock()), "target": pend["line"]["id"],
                     "state": state, **outcome})
        except OSError:
            pass


def _merge_recorded(line_id: str) -> bool:
    try:
        return bool(preference_merges(line_id) or preference_merges(line_id, "preference_resolutions"))
    except Exception:  # noqa: BLE001 - an unreadable log can't show a merge to undo
        return False


_INCOMPLETE = ("The change was made and its Undo is recorded, but Khipu could not write that it finished, so "
               "Owner actions shows it as unconfirmed.")


def finish(result: dict | None, **extra) -> dict | None:
    """Complete the owner action ``gate``/``noted`` wrote ahead, now that it
    is done (a ``state`` line: applied, or failed when the result refused);
    the result, with the log line's id when it was a direction."""
    pend = _PENDING.get()
    if pend is None:
        return result
    _PENDING.set(None)
    want, stamp = pend["intent"], pend["stamp"]
    refused = isinstance(result, dict) and result.get("ok") is False
    done = {"after": _after(want, result), **extra}
    if isinstance(result, dict):
        done["result"] = {k: v for k, v in result.items() if k != "owner_action"}
    line = pend.get("line")
    receipt = None if refused or line is None else _receipt_text(want, stamp, line, result, pend.get("before"))
    if receipt is not None:
        done["receipt"] = {"what": receipt[0]}
    incomplete = False
    try:
        if line is None:
            # The app's or a terminal's change whose line could not be
            # written ahead: the whole line, now.
            line = dict(_ahead_line(want, stamp, pend["before"]), **done)
            line["state"] = "failed" if refused else extra.get("state", "applied")
            _append(line)
        else:
            _append({"kind": "state", "id": _new_id(), "at": _iso(_clock()), "target": line["id"],
                     **done, "state": "failed" if refused else extra.get("state", "applied")})
    except OSError:
        if pend.get("line") is None:
            return result
        incomplete = True
    if not refused and want["action"] not in LOCAL_ONLY_ROUTING:
        _name_in_push(want, stamp, line, pend.get("stamped"))
        if receipt is not None:
            _write_receipt(receipt[1], line, stamp)
    # The app needs the id too: its Undo runs on exactly this line, never on one
    # picked out of the log afterwards.
    if stamp.get("auth") in ("direction", "app") and isinstance(result, dict) and not refused:
        out = dict(result, owner_action=line["id"])
        if receipt is not None and want["action"] in LOCAL_ONLY_ROUTING:
            out["receipt"] = receipt[1]
        if incomplete:
            out["record_incomplete"] = _INCOMPLETE
        return out
    return result


def _routing_stamps() -> int:
    from khipu import routing

    return routing._owner_stamped[0]


# The push's fixed words for an owner change the routing record does not
# itself hold (``routing.owner_change``): never a target, which may name a
# project or a row, since the push reaches every conversation.
_PUSH_WORDS = {
    "direction.on": "Turned owner direction on", "direction.off": "Turned owner direction off",
    "direction.set": "Changed the owner-direction settings",
    "direction.acknowledge": "Acknowledged the owner-actions tamper alarm",
    "direction.undo": "Undid an owner change",
    "ledger.import": "Imported a tracker into the ledger", "ledger.restore": "Brought back a removed ledger row",
    "ledger.purge": "Removed a ledger row for good", "ledger.project": "Changed a ledger project",
    "ledger.add": "Added a ledger row", "ledger.set": "Changed a ledger row", "ledger.remove": "Removed a ledger row",
    "work.hide": "Hid a work item", "work.unhide": "Showed a hidden work item",
    "work.same": "Marked a ledger row and a commitment as the same work",
    "work.not_same": "Marked a ledger row and a commitment as different work",
    "promote.gate_ignore": "Set Update anyway for the next promotion",
    "promote.gate_ignore.cancel": "Cancelled Update anyway",
    "direction.decline": "Declined a change a conversation asked for",
    "config.routing_setting": "Changed a routing setting",
    "config.answer_request.confirm": "Confirmed a conversation's change to a routing setting",
    "config.answer_request.undo": "Undid a conversation's change to a routing setting",
    "accounts.decline": "Stopped offering an account", "accounts.offer_again": "Offered an account again",
    "accounts.auto_track": "Changed whether new accounts are tracked",
    "owed.tidy": "Applied or undid an Owed list clean-up",
    "owed.correct_job_origin": "Requested historical job-origin corrections",
    "owed.correct_job_origin.confirm": "Confirmed historical job-origin corrections",
    "forget.send": "Sent this Mac's forgets the hub lacked to the hub",
    "forget.here": "Forgot a hub episode this Mac had forgotten by id",
    "forget.not_same": "Marked a hub episode as not the one forgotten on this Mac",
    "outbox.merge_preferences": "Merged preferences held only in memory-file lines into the hub",
    "outbox.span_covered": "Retired dead outbox jobs whose range and evidence fields another episode holds",
}


# ---- the receipt every conversation gets for a routing change by direction ---------------------
#
# One short whole sentence, written into the routing record as a receipt line
# (``routing.owner_receipt``) so every conversation gets it ahead of its other
# routing text on its next prompt, until it has been delivered
# (``routing_delivery``, as an exception's change sentence is). It says what
# changed in the owner's recorded words (a rule's, the note's, a priority's),
# where it came from, and how to undo it. Never fails the change.

RECEIPT_WORDS = 110  # characters of the owner's words in a receipt


def _where(harness) -> str:
    from khipu import routing

    if harness == "t3":
        return "a T3 conversation"
    return routing.author_words({"kind": "conversation", "harness": harness or routing.UNKNOWN_HARNESS})


def _quoted(text) -> str:
    from khipu import routing

    words = routing._word_clip(text, RECEIPT_WORDS) if isinstance(text, str) else ""
    return f' "{words}"' if words else ""


def _receipt_what(want: dict, result, before=None) -> str:
    """What a routing change did, in a few words and the owner's own."""
    from khipu import routing

    action, target = want["action"], want["target"]
    facts = want.get("facts") or {}
    result = result if isinstance(result, dict) else {}
    view = routing.read_view()
    if action in ("route.pause", "route.lift"):
        entry = next((a for a in view["accounts"] if a["key"] == target), {}) or {}
        label = entry.get("label") or routing._label_for_key(target)
        if action == "route.lift":
            return f"Lifted the pause on {label}"
        until = entry.get("until") if entry.get("state") == routing.PAUSED else None
        return f"Paused {label}" + (f" until {until}" if until else "")
    if action == "route.note.set":
        return "Set the note:" + _quoted((view.get("note") or {}).get("text"))
    if action == "route.note.end":
        return "Ended the note"
    if action.startswith("route.rule."):
        verb = {"add": "Added", "edit": "Changed", "retire": "Retired", "restore": "Restored", "confirm": "Confirmed",
                "undo": "Undid the change to", "dismiss": "Dismissed"}[action.rsplit(".", 1)[-1]]
        n = result.get("n")
        rule = next((r for r in view.get("rules") or [] if r["n"] == n or r.get("entity") == str(target)), None)
        number = n if n is not None else (rule or {}).get("n")
        words = (rule or {}).get("text") or routing._rule_words(str(target))
        return f"{verb} standing rule {number if number is not None else '(number pending)'}:" + _quoted(words)
    if action == "route.hold.add":
        hold = next((h for h in view.get("holds") or [] if h["id"] == result.get("id")), {}) or {}
        return "Added a hold:" + _quoted(hold.get("text"))
    if action == "route.hold.end":
        return "Ended a hold"
    if action.startswith("route.override."):
        from khipu import conversation_overrides as overrides

        if action == "route.override.grant":
            row = {"until": result.get("until"), "review_at": result.get("review_at")}
            label = routing._label_for_key(target)
            return (f"Let {overrides.subject_words(facts.get('subject') or {})} run ahead of pace on {label} "
                    f"{overrides.when_words(row)}")
        return "Ended an override" if action == "route.override.end" else "Kept an override for another week"
    held = view.get("priority") or {}
    labels = ", ".join(e["label"] for e in held.get("entries") or [])
    if action == "route.priority.amend":
        entry = next((e for e in held.get("entries") or [] if e["account"] == target), {}) or {}
        words = facts.get("exception") or facts.get("role") or facts.get("append") or entry.get("role")
        until = f" until {facts['until']}" if facts.get("exception") and facts.get("until") else ""
        changed = f"Changed {entry.get('label') or routing._label_for_key(target)} in the priority{until}:" + _quoted(words)
        if facts.get("confirm_pending"):
            # One yes covered both: name the conversation's request it confirmed.
            prior = (before.get("replaced") or {}).get("entries") if isinstance(before, dict) else None
            confirmed = _priority_changes((before or {}).get("entries") or [], prior or []) \
                if isinstance(before, dict) else ""
            return "Confirmed the priority request" + (f" ({confirmed})" if confirmed else "") + "; " + changed
        return changed
    if action == "route.priority.undo":
        return "Undid the priority change"
    verb = "Confirmed" if action == "route.priority.confirm" else "Set"
    # Every account whose role or exception words go live, with the new words,
    # so the owner sees what the change put in force (not only the labels).
    # A confirm compares with the priority it replaces, a set with the one before.
    prior = None
    if isinstance(before, dict):
        prior = (before.get("replaced") or {}).get("entries") if action == "route.priority.confirm" \
            else before.get("entries")
    changes = _priority_changes(held.get("entries") or [], prior or [])
    return f"{verb} the priority" + (f": {changes}" if changes else f": {labels}" if labels else "")


PRIORITY_WORDS = 70  # characters of one account's new words in a receipt
PRIORITY_RECEIPT_MOST = 400


def _priority_changes(entries: list, prior: list) -> str:
    """Each account in ``entries`` whose role or exceptions differ from
    ``prior`` (or that is new), with its new words clipped, joined; empty when
    nothing differs."""
    from khipu import routing

    was = {e.get("account"): e for e in prior if isinstance(e, dict)}
    parts = []
    for entry in entries:
        old = was.get(entry.get("account"))
        label = entry.get("label") or routing._label_for_key(str(entry.get("account")))
        if old is None or old.get("role") != entry.get("role"):
            words = routing._word_clip(entry.get("role") or "", PRIORITY_WORDS)
            parts.append(f'{label} "{words}"' if words else f"{label} (no words)")
        old_ex = {x.get("id"): x for x in (old or {}).get("exceptions") or [] if isinstance(x, dict)}
        for exc in entry.get("exceptions") or []:
            if isinstance(exc, dict) and old_ex.get(exc.get("id")) != exc:
                parts.append(f'{label} exception "{routing._word_clip(exc.get("text") or "", PRIORITY_WORDS)}"')
    text = "; ".join(parts)
    return text if len(text) <= PRIORITY_RECEIPT_MOST else text[:PRIORITY_RECEIPT_MOST].rsplit(" ", 1)[0] + "..."


def _receipt_text(want: dict, stamp: dict, line: dict, result, before=None) -> tuple[str, str] | None:
    """(what, the whole sentence) for a routing change made by direction, or
    None when this change gets no receipt."""
    action = want["action"]
    try:
        if action == "direction.undo":
            # An Undo of a change that had a receipt has its own, whoever makes it.
            undone = _find(want["target"])
            if not undone or not (undone.get("receipt") or {}).get("what"):
                return None
            what = "Undid: " + str(undone["receipt"]["what"])
        elif stamp.get("auth") != "direction":
            return None
        elif action in ROUTING_INSTANT:
            what = _receipt_what(want, result, before)
        else:
            return None
    except Exception:  # noqa: BLE001 - a receipt never stops the change; the plain verb says enough
        what = "Changed routing" if action != "direction.undo" else "Undid a routing change"
    how = {"app": "in the app", "terminal": "on a terminal"}.get(stamp.get("auth"),
                                                                 f"by your direction in {_where(stamp.get('harness'))}")
    sentence = f"{what}, {how}; undo it with `khipu owner-direction undo {line['id']}` or Undo in Settings."
    return what, sentence


def _write_receipt(sentence: str, line: dict, stamp: dict) -> None:
    try:
        from khipu import routing

        routing.owner_receipt(sentence, line["id"], stamp.get("harness"))
    except Exception:  # noqa: BLE001 - the change is made and logged; the receipt is extra
        pass


def _name_in_push(want: dict, stamp: dict, line: dict | None, stamped) -> None:
    """Name an owner change in the next routing push when it wrote no owner's
    line of the routing record itself (those are named from the record).
    Never fails the change."""
    if line is None or stamped is None or _routing_stamps() != stamped:
        return
    words = _PUSH_WORDS.get(want["action"], "Made an owner change")
    if want["action"] == "ledger.purge" and stamp.get("auth") == "direction":
        words = "Scheduled a ledger row to be removed for good in a day"
    try:
        from khipu import routing

        routing.owner_change(words, line["id"], stamp.get("auth") or "terminal", stamp.get("harness"))
    except Exception:  # noqa: BLE001 - the change is made and logged; the push line is extra
        pass


# ---- before, after and undo, per action ----------------------------------------------------

_NO_UNDO = object()  # the change has no way back (logged with its reason)
_UNREADABLE = object()  # what it changes can't be read now: a direction is refused

# Why a change has no undo, in words the app shows beside it.
_NO_UNDO_WHY = {
    "ledger.import": "A catch-up import can't be undone: it changes rows that were already there.",
    "forget.send": "A forget sent to the hub can't be undone: the hub keeps the episodes forgotten.",
    "forget.here": "A forget can't be undone: the hub keeps the episode forgotten.",
    "forget.not_same": "Not undone from its record line: forget that hub episode instead if it was the same one.",
}


def _no_undo(action: str, before) -> str | None:
    if action in ("ledger.purge", "direction.undo"):
        return None
    if before is _UNREADABLE:
        return "What this changed could not be read at the time, so it can't be undone."
    if before is _NO_UNDO or action not in _UNDO:
        return _NO_UNDO_WHY.get(action, "This change can't be undone from its record line.")
    return None


def _entry(key: str) -> dict:
    from khipu import routing

    return next((a for a in routing.read_view()["accounts"] if a["key"] == key), {}) or {}


def _rule_now(n) -> dict:
    from khipu import routing

    return next((r for r in routing.read_view()["rules"] if (str(r["n"]) == str(n) or r.get("entity") == str(n))), {}) or {}


def _priority_rows(held: dict | None) -> list[dict] | None:
    if not held:
        return None
    return [{"account": e["account"], "role": e.get("role"), "until": e.get("until"),
             **({"level": dict(e["level"])} if e.get("level") else {}),
             **({"exceptions": [dict(x) for x in e["exceptions"]]} if e.get("exceptions") else {})}
            for e in held["entries"]]


def _before(want: dict):
    """What an owner action changes, read before it does (``before`` on its
    log line); ``_NO_UNDO`` when it can't be undone from the line."""
    from khipu import routing

    action, target = want["action"], want["target"]
    if action == "route.definition.set":
        facts = want["facts"]
        value = facts["value"]
        definitions = routing.read_view().get("definitions") or {}
        if facts["definition"] == "lane_map":
            account = (definitions.get("lane_maps", {}).get(value["runner"]) or {}).get(value["lane"])
            prior = dict(value, account=account) if account else None
        else:
            prior = definitions.get("providers" if facts["definition"] == "provider" else "accounts", {}).get(target)
        return {"value": prior}
    if action in ("route.cap.set", "route.cap.remove"):
        facts = want.get("facts") or {}
        entry = _entry(target)

        def same_slot(c: dict) -> bool:
            return (c.get("kind") == facts.get("kind") and c.get("purpose", "job") == facts.get("purpose", "job")
                    and (c.get("kind") in ("spend", "concurrency") or c.get("window") == facts.get("window")))

        prior = next((c for c in entry.get("caps") or [] if same_slot(c)), None)
        layers = [c for c in entry.get("cap_layers") or [] if same_slot(c)]
        return {"cap": prior, "layers": layers or ([prior] if prior else []), "provenance": 1}
    if action == "route.pause":
        entry = _entry(target)
        paused = entry.get("state") == routing.PAUSED
        return {"paused": paused, "until": entry.get("until") if paused else None}
    if action == "route.lift":
        return {"until": _entry(target).get("until")}
    if action == "route.clear":
        return {"clear": routing.current_clear(target), "report": (want.get("facts") or {}).get("report")}
    if action in ("route.note.set", "route.note.end"):
        note = routing.read_view().get("note")
        return {"text": note["text"] if note else None}
    if action in ("route.rule.edit", "route.rule.undo", "route.rule.retire", "route.rule.dismiss"):
        rule = _rule_now(target)
        # ``provenance``: this snapshot says whose words it holds; an undo of
        # one without it is refused (``_require_provenance``).
        out = {"text": rule.get("text")} if action in ("route.rule.edit", "route.rule.undo") else {}
        out["provenance"] = 1
        if rule.get("pending"):
            # A conversation's pending proposal: its stored line, so undo
            # puts it back as that conversation's, never as the owner's words.
            out.update(pending_kind=rule.get("pending_kind"),
                       request=routing.pending_rule_line(rule["entity"]))
        return out
    if action == "route.rule.add":
        # The new rule's number is known only after, so its line's id is
        # chosen now: Undo finds that line even if the finish was never
        # written, whatever else was added meanwhile.
        return {"event": routing._new_id("o"), "provenance": 1}
    if action == "route.hold.add":
        return {"hold": routing._new_id("h"), "provenance": 1}
    if action == "route.rule.restore":
        return {"provenance": 1}
    if action == "route.rule.confirm":
        rule = _rule_now(target)
        if not rule.get("pending"):
            return {"seeded": True}
        return {"pending_kind": rule.get("pending_kind"), "prior_text": rule.get("prior_text"), "provenance": 1}
    if action == "route.hold.end":
        hold = next((h for h in routing.read_view()["holds"] if h["id"] == target), None)
        if hold is None:
            return None
        return {"text": hold.get("text"), "reason": hold.get("reason"), "scope": hold.get("scope"),
                "until": hold.get("until"), "owner": bool(hold.get("owner")), "written_by": hold.get("written_by"),
                "via": hold.get("via"), "provenance": 1}
    if action.startswith("route.priority."):
        # The stored line, never the folded view: undo puts back entries the
        # view leaves out, and an entry whose end has passed since never
        # refuses it. A conversation's pending request keeps its author, its
        # words and the priority it replaced, so undo returns it pending.
        stored = routing.read_view().get("priority_stored")
        if not stored:
            return {"entries": None, "until": None, "stored": True, "provenance": 1}
        return {**{k: stored.get(k) for k in ("until", "pending", "written_by", "via", "owner_request", "replaced")},
                "entries": [dict(e) for e in stored["entries"]], "stored": True, "provenance": 1}
    if action.startswith("route.tier_table."):
        held = routing.read_view().get("tier_table")
        return {"tiers": dict(held["tiers"]) if held else None}
    if action == "accounts.rename":
        return {"label": next((a["label"] for a in routing.accounts() if a["key"] == target), None)}
    if action == "accounts.stop":
        entry = next((a for a in routing.accounts() if a["key"] == target), None)
        return {"harness": entry["harness"], "home": entry.get("home"), "label": entry["label"]} if entry else None
    if action == "accounts.move":
        return {"home": target.partition("|")[2] or None}
    if action == "accounts.auto_track":
        from khipu import config

        return {"on": bool(config.auto_track())}
    if action in ("accounts.request.confirm", "accounts.request.undo", "config.usage_accounts", "accounts.track"):
        from khipu import config

        data = config.load_config()
        request = data.get("usage_accounts_request") if isinstance(data.get("usage_accounts_request"), dict) else {}
        stored = data.get("usage_accounts")
        if action == "accounts.request.confirm":
            return {"list": request.get("prior") if isinstance(request.get("prior"), list) else None}
        return {"list": stored if isinstance(stored, list) else None}
    if action in ("config.routing_setting", "config.answer_request.confirm", "config.answer_request.undo"):
        from khipu import config

        data = config.load_config()
        before = {"value": data.get(target), "set": target in data}
        request = routing._setting_requests(data).get(target)
        if request is not None or target == "routing_replication_live":
            before["request"] = request
        return before
    if action in ("promote.gate_ignore", "promote.gate_ignore.cancel"):
        from khipu import promote

        return {"checks": list(promote.gate_ignore_status().get("value") or [])}
    if action == "ledger.import":
        facts = want.get("facts") or {}
        if facts.get("catch_up") or not facts.get("project"):
            return _NO_UNDO
        from khipu import ledger_import

        try:
            source = ledger_import.read_tracker(target)
        except Exception:  # noqa: BLE001 - the import itself says what is wrong with the file
            return _UNREADABLE
        digest = source["digest"]
        try:
            had = ledger_import.done_imports(facts["project"], digest)
        except Exception:  # noqa: BLE001 - an import needs the database too
            return _UNREADABLE
        want["_import_source"] = source
        return {"project": facts["project"], "digest": digest, "had": had}
    if action == "ledger.project":
        from khipu import ledger

        try:
            listed = ledger.project_list()["projects"]
        except ledger.LedgerError:
            return _UNREADABLE
        proj = next((p for p in listed if p["project"] == target), None) or {}
        return {"name": proj.get("name"), "hidden": bool(proj.get("hidden"))}
    if action in ("work.hide", "work.unhide"):
        from khipu import work

        return {"hidden": work.read_hidden().get(target)}
    if action in ("work.same", "work.not_same"):
        from khipu import work

        row, _, commitment = target.partition("|")
        project, _, row_id = row.partition("#")
        pair = [project, row_id, commitment]
        links = work.read_links()
        return {"link": "same" if pair in links["same"] else "not_same" if pair in links["not_same"] else None}
    if action == "ledger.set":
        from khipu import ledger

        project, _, row_id = target.partition("#")
        try:
            row = ledger.get(project, row_id)["row"]
        except Exception:  # noqa: BLE001 - a row Khipu can't read now can't be put back
            return _UNREADABLE
        return {"fields": {k: row.get(k) for k in ("state", "evidence", "owner", "title", "link")}}
    if action == "owed.change":
        # The writer records each project's batch before changing its items.
        from khipu import mcp_server

        try:
            mine = mcp_server._owed_owners_items([int(cid) for cid in target.split(",")])
        except Exception:  # noqa: BLE001 - what Khipu can't read now can't be put back
            return _UNREADABLE
        return _UNREADABLE if mine is None else {"owners_items": mine}
    if action == "owed.correct_job_origin.confirm":
        return {"batches": []}
    if action == "owed.tidy":
        facts = want.get("facts") or {}
        return {"project": target, "change": facts.get("change"), "batch": facts.get("batch"),
                **({"ids": []} if facts.get("change") == "undo" else {})}
    if action == "outbox.merge_preferences":
        # Each transaction's additions are written ahead as their own state
        # line (``record_preference_merges``) before it commits.
        return {"change": "apply"}
    if action == "outbox.span_covered":
        # Each batch's jobs are written ahead (``record_preference_resolutions``).
        return {"change": "apply"}
    if action in ("direction.off", "direction.on"):
        return {"enabled": owner_turns.settings()["enabled"]}
    if action == "direction.acknowledge":
        return {"alarm": read_alarm()}
    if action == "direction.set":
        key, _, member = target.partition(" ")
        current = owner_turns.settings()
        if key in ("trust", "untrust"):
            return {"member": member, "trusted": member in current["trusted"]}
        return {"settings": {key: current[key]}}
    if action.startswith("project."):
        # Read, changed and diffed under the group lock in the change's own
        # transaction: its result's ``delta`` (on the finish line) is the Undo.
        return {}
    if action == "route.override.keep":
        from khipu import conversation_overrides

        row = next((r for r in conversation_overrides.list_overrides() if r.get("id") == target), None)
        return {"review": {"override": target, "review_at": row.get("review_at")}} if row else {}
    if action.startswith("route.override."):
        return {}
    if action in _UNDO or action in ("ledger.purge", "direction.undo"):
        return None
    return _NO_UNDO


def _after(want: dict, result) -> Any:
    if not isinstance(result, dict):
        return None
    keep = ("id", "entity", "n", "account", "until", "entries", "version", "state", "runs_at", "ended", "label", "accounts", "tiers",
            "replaced", "override", "review_at", "end")
    return {k: result[k] for k in keep if k in result} or None


def _restore_setting(key: str, value, was_set: bool, request=None) -> None:
    from khipu import routing
    from khipu.config import mutate_config

    with routing._lock(), mutate_config() as data:
        if was_set:
            data[key] = value
        else:
            data.pop(key, None)
        requests = routing._setting_requests(data)
        if request is not None:
            requests[key] = request
        else:
            requests.pop(key, None)
        if requests:
            data[routing.SETTINGS_REQUEST_KEY] = requests
        else:
            data.pop(routing.SETTINGS_REQUEST_KEY, None)
    routing.refresh_view()  # config.json is an input of the view: the next prompt finds it current


def _set_priority_back(before: dict, via: str, holds: dict | None = None, *, _undo_default_holds: bool = False) -> dict:
    from khipu import routing

    if before.get("entries"):
        return routing.set_priority(before["entries"], until=before.get("until"), via=via,
                                    expect=routing.priority_version(), holds=holds,
                                    _undo_default_holds=_undo_default_holds)
    if routing.read_view().get("priority"):
        return routing.clear_priority(via=via, expect=routing.priority_version())
    return {"ok": True}


def _ledger_write(verb: str, target: str, fields: dict | None = None) -> dict:
    from khipu import ledger

    project, _, row_id = target.partition("#")
    op = ledger.new_op(verb, project, row_id, fields=fields or {}, from_app=True)
    return ledger.write(op)


def _undo_pause(line, via):
    from khipu import routing

    before = line.get("before") or {}
    if before.get("paused"):
        return routing.pause(line["target"], until=before.get("until"), via=via)
    return routing.lift(line["target"], via=via)


def _undo_hold_end(line, via):
    from khipu import routing

    before = dict(line.get("before") or {})
    scope = before.get("scope") or {}
    kind = scope.get("kind")
    # A conversation's hold comes back as that conversation's (its limits
    # included); only the owner's own hold comes back as his.
    author = before.get("written_by")
    if "owner" in before and not before["owner"]:
        if not isinstance(author, dict) or author.get("kind") != "conversation":
            raise ValueError("Khipu can't tell which conversation wrote this hold, so it can't undo its end")
        return routing.add_hold(before.get("text") or "", reason=before.get("reason") or "other", scope=kind,
                                account=scope.get("account"), until=before.get("until"), owner=False,
                                author=dict(author), via=before.get("via") or "mcp")
    return routing.add_hold(before.get("text") or "", reason=before.get("reason") or "other", scope=kind,
                            account=scope.get("account"), until=before.get("until"), owner=True, via=via)


def _undo_track(line, via):
    from khipu import accounts, config

    before = (line.get("before") or {}).get("list")
    return accounts.save_list(json.dumps(before) if before else None, expect=config.accounts_version(), via=via)


def _undo_stop(line, via):
    from khipu import accounts, config

    b = line.get("before") or {}
    return accounts.track([{"harness": b.get("harness"), "home": b.get("home"), "label": b.get("label")}],
                          expect=config.accounts_version(), via=via)


def _undo_move(line, via):
    from khipu import accounts, config, usage

    new_key = usage._key({"harness": line["target"].partition("|")[0],
                          "home": (line.get("facts") or {}).get("folder")})
    return accounts.move(new_key, (line.get("before") or {}).get("home"), expect=config.accounts_version(), via=via)


def _undo_rename(line, via):
    from khipu import accounts, config

    return accounts.rename(line["target"], (line.get("before") or {}).get("label"), expect=config.accounts_version(),
                           via=via)


def _undo_hidden(line, via):
    from khipu import work

    hidden = work.read_hidden()
    mark = (line.get("before") or {}).get("hidden")
    if mark is None:
        hidden.pop(line["target"], None)
    else:
        hidden[line["target"]] = mark
    work._write_json(work._hidden_file(), hidden)
    return {"ok": True}


def _undo_project(line, via):
    from khipu import ledger

    before = line.get("before") or {}
    return ledger.set_project(line["target"], name=before.get("name") or "", hidden=bool(before.get("hidden")))


def _undo_link(line, via):
    from khipu import work

    row, _, commitment = line["target"].partition("|")
    project, _, row_id = row.partition("#")
    pair = [project, row_id, commitment]
    links = work.read_links()
    for key in ("same", "not_same"):
        links[key] = [x for x in links[key] if x != pair]
    was = (line.get("before") or {}).get("link")
    if was:
        links[was].append(pair)
    work._write_json(work._links_file(), links)
    return {"ok": True}


def _undo_rule_confirm(line, via, holds=None):
    from khipu import routing

    before = line.get("before") or {}
    holds = _undo_holds(line, holds)
    if before.get("seeded"):
        return routing.unconfirm_rule(line["target"], via=via)
    if before.get("pending_kind") == "changed" and isinstance(before.get("prior_text"), str):
        return routing.edit_rule(line["target"], before["prior_text"], via=via, holds=holds,
                                 _undo_default_holds=True, enforce_caps=False)
    return routing.retire_rule(line["target"], via=via)


def _undo_ledger_set(line, via):
    fields = (line.get("before") or {}).get("fields") or {}
    return _ledger_write("set", line["target"], fields)


def _undo_direction(line, via):
    before = line.get("before") or {}
    if line["action"] in ("direction.off", "direction.on"):
        return {"ok": True, "settings": owner_turns.write_settings({"enabled": bool(before.get("enabled"))})}
    key, _, member = line["target"].partition(" ")
    if key in ("trust", "untrust"):
        # Restore this member only, including logs from before member snapshots.
        was_trusted = before.get("trusted") if "member" in before else member in (
            (before.get("settings") or {}).get("trusted") or [])
        trusted = [x for x in owner_turns.settings()["trusted"] if x != member]
        if was_trusted:
            trusted.append(member)
        values = {"trusted": trusted}
    else:
        settings = before.get("settings")
        if not isinstance(settings, dict) or key not in settings:
            raise ValueError(f"Khipu's Undo snapshot has no prior value for {key}; nothing was changed")
        values = {key: settings[key]}
    return {"ok": True, "settings": owner_turns.write_settings(values)}


def _undo_acknowledge(line, via):
    """The alarm the owner acknowledged comes back, as it was."""
    alarm = (line.get("before") or {}).get("alarm")
    if isinstance(alarm, dict) and read_alarm() is None:
        _write_private(alarm_path(), json.dumps(alarm) + "\n")
    return {"ok": True, "alarm": read_alarm()}


def _b(line: dict) -> dict:
    return line.get("before") or {}


def _undo_holds(line: dict, explicit: dict | None) -> dict:
    """Choices for an inverse write.

    Keep the original action's recorded choices, then let this Undo's
    explicit choices replace them.  The routing write adds current missing
    choices while it holds the log lock, so a hold that arrives after this
    direction log entry cannot turn the inverse into a new refusal.
    """
    choices = dict((line.get("facts") or {}).get("holds") or {})
    if explicit:
        choices.update(explicit)
    return choices


def _undo_clear(line, via):
    """A dismissed report comes back; a clear is dropped, and the clear
    before it (``before``) counts again (the fold keeps them in order)."""
    from khipu import routing

    report = (line.get("facts") or {}).get("report")
    if report:
        return routing.undismiss(line["target"], report, via=via)
    clear_id = (line.get("after") or {}).get("id")
    if clear_id is None:
        # A lost completion does not make the account's newest clear ours.
        matches = [e for e in routing.read_events()[0]
                   if e.get("type") == "clear" and e.get("op") == line["id"]]
        clear_id = matches[0].get("id") if len(matches) == 1 else None
    if clear_id is None:
        raise ValueError("Khipu can't tell which clear this was, so it can't undo it")
    return routing.undo_clear(line["target"], clear_id, via=via)


def _undo_lift(line, via):
    from khipu import routing

    return routing.pause(line["target"], until=_b(line).get("until"), via=via)


def _undo_note_set(line, via):
    from khipu import routing

    text = _b(line).get("text")
    return routing.set_note(text, via=via, enforce_caps=False) if text else routing.end_note(via=via)


def _undo_note_end(line, via):
    from khipu import routing

    return routing.set_note(_b(line).get("text") or "", via=via, enforce_caps=False)


def _undo_rule_words(line, via, holds=None):
    from khipu import routing

    back = _pending_rule_back(line, via, holds)
    if back is not None:
        return back
    words = _b(line).get("text") or ""
    return routing.edit_rule(line["target"], words, via=via, holds=_undo_holds(line, holds),
                             _undo_default_holds=True, enforce_caps=False)


def _retire_rule(n_of):
    def run(line, via):
        from khipu import routing

        return routing.retire_rule(n_of(line), via=via)
    return run


LEGACY_PROVENANCE = "this change was saved before Khipu recorded who wrote it; set it again instead"


_PROVENANCE_FAMILIES = ("route.priority.", "route.rule.", "route.hold.", "route.cap.")


def _require_provenance(line: dict) -> None:
    """Every priority, rule and hold undo needs a snapshot that says whose
    words it holds (``provenance``). One saved before Khipu recorded that is
    refused, never guessed at: refusing is the accepted cost."""
    before = _b(line)
    # Provable from the snapshot itself: confirming a seeded rule restores
    # no words on undo, only marks the seed unconfirmed again.
    owners_only = line["action"] == "route.rule.confirm" and before == {"seeded": True}
    if line["action"].startswith(_PROVENANCE_FAMILIES) and not before.get("provenance") and not owners_only:
        raise ValueError(LEGACY_PROVENANCE)


def _pending_rule_back(line, via, holds):
    """None when the change was not over a conversation's pending rule
    proposal; otherwise that proposal put back as the conversation's."""
    from khipu import routing

    before = _b(line)
    if before.get("pending_kind") == "added":
        # Restoring would make the conversation's proposed rule read as the
        # owner's; it has to be proposed again.
        raise ValueError("this was a conversation's proposed rule; Khipu won't bring it back as yours, "
                         "so ask the conversation to propose it again")
    if before.get("pending_kind") == "changed":
        return routing.restore_rule_request(line["target"], before.get("request") or {}, via=via,
                                            holds=_undo_holds(line, holds),
                                            retired=line["action"] == "route.rule.retire")
    return None


def _undo_restore_rule(line, via, holds=None):
    from khipu import routing

    back = _pending_rule_back(line, via, holds)
    if back is not None:
        return back
    return routing.restore_rule(line["target"], via=via, holds=_undo_holds(line, holds),
                                _undo_default_holds=True)


def _undo_hold_add(line, via):
    from khipu import routing

    hold_id = (line.get("after") or {}).get("id")
    if hold_id is None:
        # Unconfirmed: the id this action gave its hold before writing it.
        planned = _b(line).get("hold")
        events, _bad = routing.read_events()
        if planned and any(e.get("type") == "hold" and e.get("id") == planned for e in events):
            hold_id = planned
    if hold_id is None:
        raise ValueError("Khipu can't tell which hold this was, so it can't undo it")
    return routing.end_hold(hold_id, owner=True, via=via)


def _added_rule(line) -> str | int:
    from khipu import routing

    n = (line.get("after") or {}).get("entity") or (line.get("after") or {}).get("n")
    if n is None:
        # Unconfirmed: the rule written under the id this action chose.
        planned = _b(line).get("event")
        events, _bad = routing.read_events()
        n = next((e.get("entity") or e.get("n") for e in events if planned and e.get("type") == "rule_add"
                  and e.get("id") == planned), None)
    if n is None:
        raise ValueError("Khipu can't tell which rule this added, so it can't undo it")
    return n


def _undo_priority(line, via, holds=None):
    from khipu import routing

    before = _b(line)
    if line.get("action") == "route.priority.confirm" and line.get("request_applied") and before.get("pending"):
        # A conversation's request the owner's own turn applied at once: its
        # Undo takes the whole change back (the priority it replaced, or
        # none), not just its confirmation, which would leave it live.
        replaced = before.get("replaced") or {}
        if replaced.get("entries"):
            return routing.restore_priority({"entries": replaced["entries"], "until": replaced.get("until")},
                                            via=via, holds=_undo_holds(line, holds))
        if routing.read_view().get("priority"):
            return routing.clear_priority(via=via, expect=routing.priority_version())
        return {"ok": True}
    if before.get("stored") and before.get("entries"):
        return routing.restore_priority(before, via=via, holds=_undo_holds(line, holds))
    return _set_priority_back(before, via, _undo_holds(line, holds),
                              _undo_default_holds=True)


def _undo_tier_table(line, via):
    from khipu import routing

    tiers = _b(line).get("tiers")
    if tiers:
        return routing.set_tier_table(tiers, via=via)
    return routing.clear_tier_table(via=via)


def _undo_decline(line, via):
    from khipu import accounts

    return accounts.offer_again(line["target"], (line.get("facts") or {}).get("folder"))


def _undo_offer_again(line, via):
    from khipu import accounts

    return accounts.decline(line["target"], (line.get("facts") or {}).get("folder"))


def _undo_auto_track(line, via):
    from khipu import accounts

    return accounts.set_auto_track(bool(_b(line).get("on")))


def _undo_setting(line, via):
    before = _b(line)
    if (line["target"] == "routing_replication_live" and before.get("value") is True
            and "request" not in before):
        raise ValueError("Khipu cannot confirm the prior replication permission from this older Undo; "
                         "choose the replication setting in Settings")
    _restore_setting(line["target"], _b(line).get("value"), bool(_b(line).get("set")),
                     _b(line).get("request"))
    return {"ok": True}


def _undo_gate_ignore(line, via):
    from khipu import promote

    promote.set_gate_ignore(_b(line).get("checks") or None, by=_by(via))
    return {"ok": True}


def _undo_project_group(line, via):
    from khipu import project_group

    return project_group.run_undo(line["action"], line["target"], line.get("result"))


def _undo_import(line, via):
    from khipu import ledger_import

    before = _b(line)
    if not before.get("digest"):
        raise ValueError("Khipu can't tell which import this was, so it can't undo it")
    return ledger_import.undo_import(before["project"], before["digest"], before.get("had"))


def _undo_owed_change(line, via):
    from khipu import owed_tidy

    batches = _b(line).get("batches") or (line.get("result") or {}).get("batches") or []
    if not batches:
        raise ValueError("Khipu has no record of which batch this change wrote, so it can't undo it")
    restored, left_alone = 0, []
    for batch in batches:
        out = owed_tidy.undo(batch, owner_gate=_by(via), write_id=_inverse_write_id(line, batch))
        if not out.get("ok"):
            raise ValueError(out.get("error") or f"batch {batch} could not be undone")
        restored += int(out.get("restored") or 0)
        left_alone += out.get("left_alone") or []
    return {"ok": True, "restored": restored, "left_alone": left_alone}


def _undo_owed_tidy(line, via):
    from khipu import owed_tidy

    batch = _b(line).get("batch") or (line.get("result") or {}).get("batch")
    if not batch:
        raise ValueError("Khipu has no record of which batch this clean-up wrote, so it can't undo it")
    if _b(line).get("change") == "undo":
        if _b(line).get("ids") == []:
            return {"ok": True, "reapplied": 0, "left_alone": []}
        out = owed_tidy.redo(batch, project=line["target"], owner_gate=_by(via),
                             ids=_b(line).get("ids"), write_id=_inverse_write_id(line, batch))
        counted = {"reapplied": int(out.get("reapplied") or 0)}
    else:
        out = owed_tidy.undo(batch, project=line["target"], owner_gate=_by(via),
                             write_id=_inverse_write_id(line, batch))
        counted = {"restored": int(out.get("restored") or 0)}
    if not out.get("ok"):
        raise ValueError(out.get("error") or f"batch {batch} could not be undone")
    return {"ok": True, **counted, "left_alone": out.get("left_alone") or []}


_INCLUDE_UNPROVEN: contextvars.ContextVar[bool] = contextvars.ContextVar("khipu_include_unproven", default=False)


def _undo_outbox_preferences(line, via):
    from khipu import outbox_preferences

    entries = preference_merges(line["id"])
    resolved = preference_merges(line["id"], "preference_resolutions")
    if not (entries or resolved):
        result = line.get("result") or {}
        if (int(result.get("episodes_merged") or 0) == 0 and int(result.get("lines_resolved") or 0) == 0
                and line["state"] == "applied"):
            return {"ok": True, "restored": 0, "left_alone": []}
        raise ValueError("Khipu has no record of which preferences this merge added, so it can't undo it")
    # Committed: a batch's own marker, or the finished result's episode list
    # (written only after every batch committed).
    committed = {int(c["episode_id"]) for c in preference_merges(line["id"], "preference_commits")}
    committed |= {int(i) for i in (line.get("result") or {}).get("episode_ids") or []}
    return outbox_preferences.undo(entries, resolved, committed, include_unproven=_INCLUDE_UNPROVEN.get(),
                                   retract_reason=f"undo of owner action {line['id']} "
                                                  "(outbox merge-line-preferences --include-decisions)")


def _undo_span_covered(line, via):
    from khipu import outbox_span_covered

    entries = preference_merges(line["id"], "preference_resolutions")
    if not entries and not (line["state"] == "applied" and not int((line.get("result") or {}).get("retired") or 0)):
        raise ValueError("Khipu has no record of which jobs this retired, so it can't undo it")
    return outbox_span_covered.undo(entries)


def _ledger_undo(verb: str):
    def run(line, via):
        return _ledger_write(verb, line["target"])
    return run


def _undo_cap(line: dict, via: str) -> dict:
    from khipu import routing

    facts = line.get("facts") or {}
    before = line.get("before") or {}
    previous = before.get("cap")
    layers = before.get("layers") if "layers" in before else ([previous] if previous else [])
    # Clear the slot, then put back each layer it held: a cap set over the
    # undone one would leave the undone one beneath it.
    return routing.restore_cap_layers(line["target"], window=facts.get("window") or (previous or {}).get("window"),
                                      kind=facts.get("kind") or (previous or {}).get("kind"),
                                      purpose=facts.get("purpose", "job"), layers=layers, via=via)


def _undo_definition(line: dict, via: str) -> dict:
    from khipu import routing

    facts = line["facts"]
    previous = line["before"]["value"]
    if previous is not None:
        return routing.set_definition(facts["definition"], previous, via=via)
    return routing._owner_write({"type": "definition_remove", "definition": facts["definition"],
                                 "value": facts["value"]}, via)


def _undo_override_grant(line, via):
    from khipu import conversation_overrides as overrides

    after = line.get("after") or {}
    if not after.get("id"):
        raise ValueError("Khipu can't tell which override this was, so it can't undo it")
    overrides.end(after["id"])
    for old in after.get("replaced") or []:
        overrides.restore(ended=old["end"])
    return {"ok": True}


def _undo_override_end(line, via):
    from khipu import conversation_overrides as overrides

    ended = (line.get("after") or {}).get("end") or (line.get("result") or {}).get("end")
    if not ended:
        raise ValueError("Khipu can't tell which ending this was, so it can't undo it")
    overrides.restore(ended=ended)
    return {"ok": True}


def _undo_override_keep(line, via):
    from khipu import conversation_overrides as overrides

    review = (line.get("before") or {}).get("review")
    if not review:
        raise ValueError("Khipu can't tell the earlier review date, so it can't undo this")
    overrides.restore(review=review)
    return {"ok": True}


_UNDO = {
    "route.override.grant": _undo_override_grant,
    "route.override.end": _undo_override_end,
    "route.override.keep": _undo_override_keep,
    "route.cap.set": _undo_cap,
    "route.cap.remove": _undo_cap,
    "route.definition.set": _undo_definition,
    "route.pause": _undo_pause,
    "route.lift": _undo_lift,
    "route.clear": _undo_clear,
    "route.note.set": _undo_note_set,
    "route.note.end": _undo_note_end,
    "route.rule.add": _retire_rule(_added_rule),
    "route.rule.edit": _undo_rule_words,
    "route.rule.retire": _undo_restore_rule,
    "route.rule.restore": _retire_rule(lambda line: line["target"]),
    "route.rule.confirm": _undo_rule_confirm,
    "route.rule.undo": _undo_rule_words,
    "route.rule.dismiss": _undo_restore_rule,
    "route.hold.add": _undo_hold_add,
    "route.hold.end": _undo_hold_end,
    "route.priority.set": _undo_priority,
    "route.priority.amend": _undo_priority,
    "route.priority.confirm": _undo_priority,
    "route.priority.undo": _undo_priority,
    "route.priority.clear": _undo_priority,
    "route.tier_table.set": _undo_tier_table,
    "route.tier_table.confirm": _undo_tier_table,
    "route.tier_table.clear": _undo_tier_table,
    "accounts.track": _undo_track,
    "accounts.rename": _undo_rename,
    "accounts.stop": _undo_stop,
    "accounts.move": _undo_move,
    "accounts.decline": _undo_decline,
    "accounts.offer_again": _undo_offer_again,
    "accounts.auto_track": _undo_auto_track,
    "accounts.request.confirm": _undo_track,
    "accounts.request.undo": _undo_track,
    "config.usage_accounts": _undo_track,
    "config.routing_setting": _undo_setting,
    "config.answer_request.confirm": _undo_setting,
    "config.answer_request.undo": _undo_setting,
    "promote.gate_ignore": _undo_gate_ignore,
    "promote.gate_ignore.cancel": _undo_gate_ignore,
    "ledger.import": _undo_import,
    "ledger.restore": _ledger_undo("remove"),
    "ledger.add": _ledger_undo("remove"),
    "ledger.set": _undo_ledger_set,
    "ledger.remove": _ledger_undo("restore"),
    "ledger.project": _undo_project,
    "work.hide": _undo_hidden,
    "work.unhide": _undo_hidden,
    "work.same": _undo_link,
    "work.not_same": _undo_link,
    "project.alias_add": _undo_project_group,
    "project.alias_remove": _undo_project_group,
    "project.merge": _undo_project_group,
    "project.keep_separate": _undo_project_group,
    "project.separate": _undo_project_group,
    "owed.change": _undo_owed_change,
    "owed.tidy": _undo_owed_tidy,
    "owed.correct_job_origin.confirm": _undo_owed_change,
    "outbox.merge_preferences": _undo_outbox_preferences,
    "outbox.span_covered": _undo_span_covered,
    "direction.off": _undo_direction,
    "direction.on": _undo_direction,
    "direction.set": _undo_direction,
    "direction.acknowledge": _undo_acknowledge,
}


def _by(via: str | None) -> str:
    return {"app": "app", "cli": "terminal"}.get(via or "", via or "?")


# ---- the scheduled purge --------------------------------------------------------------------


def schedule_purge(project: str, row_id: str) -> dict:
    """A purge by direction (decision 3): logged as scheduled a day out, the
    day its line written ahead already holds."""
    line = (_PENDING.get() or {}).get("line") or {}
    runs_at = _float(line.get("runs_at_ts")) or _clock() + PURGE_DELAY_S
    out = {"ok": True, "verb": "purge", "id": row_id, "project": project, "state": "scheduled",
           "runs_at": _iso(runs_at),
           "line": f"{row_id} will be removed for good at {_iso(runs_at)}; Undo cancels it until then"}
    return finish(out, state="scheduled", runs_at=_iso(runs_at), runs_at_ts=runs_at) or out


def _scheduled(line: dict) -> bool:
    """A purge waiting for its day: completed as scheduled, or written ahead
    by direction with its day and never completed (the finish could not be
    written)."""
    return line["state"] == "scheduled" or (line["state"] == "unconfirmed" and line.get("auth") == "direction"
                                            and bool(_float(line.get("runs_at_ts"))))


def run_due(now: float | None = None) -> list[str]:
    """Run each scheduled purge whose day has passed. Never raises: one that
    fails stays scheduled and is tried next time."""
    now = _clock() if now is None else now
    done: list[str] = []
    try:
        lines = actions()
    except Exception:  # noqa: BLE001
        return done
    for line in lines:
        if line.get("action") != "ledger.purge" or not _scheduled(line):
            continue
        if float(line.get("runs_at_ts") or 0) > now:
            continue
        try:
            from khipu import ledger

            project, _, row_id = str(line["target"]).partition("#")
            conn = ledger._connect()
            try:
                ledger.purge(conn, project, row_id)
            finally:
                conn.close()
            _append({"kind": "state", "id": _new_id(), "at": _iso(now), "target": line["id"], "state": "done"})
            done.append(line["id"])
        except Exception:  # noqa: BLE001 - the ledger command that called this goes on
            continue
    return done


def timer(now: float | None = None) -> dict:
    """What the replica-refresh job runs for owner direction, every few
    minutes: the turn retention (``owner_turns.sweep``) and the purges whose
    day has passed (``run_due``). Local files and the ledger only. Never
    raises."""
    out: dict[str, Any] = {}
    try:
        out["sweep"] = owner_turns.sweep(now)
    except Exception as err:  # noqa: BLE001 - the job it runs in goes on
        out["sweep"] = {"ok": False, "errors": [f"{type(err).__name__}: {err}"]}
    try:
        out["purged"] = run_due(now)
    except Exception as err:  # noqa: BLE001
        out["purged"] = []
        out["error"] = f"{type(err).__name__}: {err}"
    return out


# ---- undo ----------------------------------------------------------------------------------


def _inverse_write_id(line: dict, batch: str) -> str:
    return "owner-undo:" + line["id"] + ":" + batch


def _hub_inverse_writes(line: dict) -> list[dict]:
    before, result = line.get("before") or {}, line.get("result") or {}
    if line.get("action") in ("owed.change", "owed.correct_job_origin.confirm"):
        batches = before.get("batches") or result.get("batches") or []
    elif line.get("action") == "owed.tidy":
        batch = before.get("batch") or result.get("batch")
        batches = [batch] if batch else []
    else:
        return []
    return [{"tool": "owner_undo", "args": {"write_id": _inverse_write_id(line, batch)}} for batch in batches]


def _unfinished_undos(line_id: str) -> list[dict]:
    inflight = ((_PENDING.get() or {}).get("line") or {}).get("id")
    return [row for row in _merged(read_log()).values()
            if row.get("kind") == "undo" and row.get("undoes") == line_id
            and row.get("state") == "unconfirmed" and row.get("id") != inflight]


def _undo_refusal(line: dict | None, line_id: str) -> str | None:
    """Why the logged action ``line_id`` can't be undone now, or None."""
    if line is None:
        return f"no owner action {line_id} is in the log"
    for row in _unfinished_undos(line_id):
        writes = row.get("inverse_writes")
        if writes:
            settled = _inverse_state(writes)
        elif row.get("inverse_started") is False:
            settled = "failed"
        else:
            continue
        if settled == "pending":
            continue
        try:
            _append({"kind": "state", "id": _new_id(), "at": _iso(_clock()),
                     "target": row["id"], "state": settled})
        except OSError:
            continue
        line.update(_find(line_id) or {})
    state = line["state"]
    if line["action"] == "owed.correct_job_origin.confirm" and state in ("failed", "unconfirmed"):
        settled, result = _origin_request_outcome(line["target"])
        if settled == "pending":
            return "Khipu cannot confirm this correction's hub outcome yet; retry Undo when the hub is available"
        fields = _origin_outcome_fields(settled, result)
        try:
            _append({"kind": "state", "id": _new_id(), "at": _iso(_clock()),
                     "target": line_id, **fields})
        except OSError:
            return "Khipu cannot record this correction's confirmed outcome; retry Undo when the log is writable"
        line.update(fields)
        state = settled
    if state in ("undone", "cancelled"):
        return f"owner action {line_id} was already undone"
    if state == "failed":
        return f"owner action {line_id} did not happen, so there is nothing to undo"
    if not line["action"].startswith("route.") and _unfinished_undos(line_id):
        return ("Khipu cannot confirm whether the previous Undo finished; it will not apply its inverse "
                "again and overwrite a newer owner choice")
    if state == "unconfirmed" and line.get("owner_write"):
        settled = _hub_write_state(line["owner_write"])
        if settled == "pending":
            return "Khipu cannot confirm this write's hub outcome yet; retry Undo when the hub is available"
        try:
            _append({"kind": "state", "id": _new_id(), "at": _iso(_clock()),
                     "target": line_id, "state": settled})
        except OSError:
            return "Khipu cannot record this write's confirmed hub outcome; retry Undo when the log is writable"
        line["state"] = settled
        if settled == "failed":
            return f"owner action {line_id} did not happen, so there is nothing to undo"
    if line["action"] == "ledger.purge":
        if state == "done":
            return f"the purge {line_id} already ran; a row removed for good can't come back"
        if not _scheduled(line):
            return (f"the purge {line_id} was made in the app and ran at once (or Khipu can't tell that it did "
                    "not); a row removed for good can't come back")
        # Due is due whether or not the timer has run it yet: Undo can't
        # race the purge, and what the owner sees can't depend on which won.
        if _float(line.get("runs_at_ts")) <= _clock():
            return (f"the purge {line_id} passed its deadline ({line.get('runs_at') or 'its scheduled time'}), "
                    "so it can no longer be cancelled; it runs on the next timer if it has not run already")
        return None
    if _UNDO.get(line["action"]) is None or "no_undo" in line:
        return line.get("no_undo") or f"owner action {line_id} ({line['action']}) can't be undone"
    return None


# ---- a newer change to the same target ------------------------------------------------------
#
# An Undo restores the state its action's snapshot read. When a later change
# (an owner action, or another write in the routing record) touched the same
# target since, restoring would silently throw that newer change away, so the
# Undo is refused unless the owner asks for it over the newer change.


class NewerChange(ValueError):
    """An Undo refused because a newer change touched the same target."""

    code = "newer_change"

    def __init__(self, message: str, newer: dict):
        super().__init__(message)
        self.newer = newer


# What each target is called in the refusal.
_TARGET_WORDS = {"priority": "priority", "cap": "cap", "rule": "rule", "hold": "hold", "pause": "pause",
                 "note": "routing note", "tier_table": "tier table", "definition": "definition",
                 "accounts": "account list", "account": "account", "offer": "account offer",
                 "auto_track": "new-account setting"}

# A newer owner action, named by what it did.
_ACTION_WORDS = {
    "route.priority.set": "a priority set", "route.priority.amend": "a priority entry change",
    "route.priority.confirm": "a priority confirm", "route.priority.undo": "a priority proposal undone",
    "route.priority.clear": "the priority cleared", "route.cap.set": "a cap set", "route.cap.remove": "a cap removed",
    "route.rule.add": "a rule added", "route.rule.edit": "a rule edit", "route.rule.retire": "a rule retired",
    "route.rule.restore": "a rule restored", "route.rule.confirm": "a rule confirm",
    "route.rule.undo": "a rule proposal undone", "route.rule.dismiss": "a rule proposal dismissed",
    "route.hold.add": "a hold added", "route.hold.end": "a hold ended", "route.pause": "a pause",
    "route.override.grant": "an override granted", "route.override.end": "an override ended",
    "route.override.keep": "an override kept",
    "route.lift": "a lift", "route.note.set": "a routing note set",
    "route.note.end": "the routing note ended", "route.tier_table.set": "a tier table set",
    "route.tier_table.confirm": "a tier table confirm", "route.tier_table.clear": "the tier table cleared",
    "route.definition.set": "a definition set", "accounts.track": "the tracked accounts changed",
    "accounts.rename": "an account renamed", "accounts.stop": "an account no longer tracked",
    "accounts.move": "an account moved", "accounts.decline": "an account no longer offered",
    "accounts.offer_again": "an account offered again", "accounts.auto_track": "new-account tracking changed",
    "accounts.request.confirm": "an account request confirmed", "accounts.request.undo": "an account request undone",
    "config.usage_accounts": "the account list changed",
}

# A routing-record line not made by an owner action, named by its type.
_EVENT_WORDS = {
    "priority_set": "priority", "priority_clear": "priority clear", "priority_confirm": "priority confirm",
    "priority_undo": "priority undo", "cap_set": "cap", "cap_remove": "cap removal", "rule_add": "rule",
    "rule_edit": "rule edit", "rule_retire": "rule retirement", "rule_restore": "rule restore",
    "rule_confirm": "rule confirm", "rule_unconfirm": "rule unconfirm", "rule_undo": "rule undo",
    "rule_dismiss": "rule dismissal", "hold": "hold", "hold_end": "hold end", "pause": "pause", "lift": "lift",
    "note_set": "routing note", "note_end": "routing note end", "tier_table_set": "tier table",
    "tier_table_clear": "tier table clear", "definition_set": "definition",
}


def _cap_key(account, kind, purpose, window) -> tuple:
    from khipu import routing_pace

    return ("cap", account, kind, purpose or "job", window if kind in routing_pace.WINDOW_CAP_KINDS else None)


def _rule_entity(n) -> str | None:
    from khipu import routing

    try:
        return routing.rule_reference(n)[0]
    except (routing.RouteError, ValueError, TypeError):
        return str(n) if n is not None else None


def _line_keys(line: dict, forward: dict | None = None) -> set[tuple]:
    """The targets an owner action's Undo would restore; empty when the
    action has no newer-change check (or its target can't be read, in which
    case its inverse refuses on its own)."""
    from khipu import routing

    action, target = line.get("action") or "", line.get("target")
    facts, before, after = line.get("facts") or {}, line.get("before") or {}, line.get("after") or {}
    acct = (lambda key: routing._forwarded(key, forward)) if forward is not None else (lambda key: key)
    if action.startswith("route.priority."):
        # A priority Undo puts back the whole stored priority, every entry.
        return {("priority",)}
    if action.startswith("route.cap."):
        previous = before.get("cap") or {}
        return {_cap_key(acct(target), facts.get("kind") or previous.get("kind"),
                         facts.get("purpose", previous.get("purpose", "job")),
                         facts.get("window") or previous.get("window"))}
    if action == "route.rule.add":
        try:
            entity = _rule_entity(_added_rule(line))
        except ValueError:
            return set()
        return {("rule", entity)} if entity else set()
    if action.startswith("route.rule."):
        return {("rule", _rule_entity(target))}
    if action == "route.hold.add":
        hold_id = after.get("id") or before.get("hold")
        return {("hold", hold_id)} if hold_id else set()
    if action == "route.hold.end":
        return {("hold", target)}
    if action in ("route.pause", "route.lift"):
        return {("pause", acct(target))}
    # A clear's Undo drops that one clear (or brings back that one report),
    # never a snapshot, so a newer clear is kept either way: no check.
    if action.startswith("route.note."):
        return {("note",)}
    if action.startswith("route.tier_table."):
        return {("tier_table",)}
    if action == "route.definition.set":
        return {("definition", facts.get("definition"), target)}
    if action in ("accounts.track", "accounts.request.confirm", "accounts.request.undo", "config.usage_accounts"):
        return {("accounts",)}
    if action in ("accounts.rename", "accounts.stop"):
        return {("account", acct(target))}
    if action == "accounts.move":
        keys = {("account", acct(target))}
        try:
            from khipu import usage

            keys.add(("account", usage._key({"harness": str(target).partition("|")[0], "home": facts.get("folder")})))
        except Exception:  # noqa: BLE001 - the old key still names it
            pass
        return keys
    if action in ("accounts.decline", "accounts.offer_again"):
        return {("offer", target, facts.get("folder"))}
    if action == "accounts.auto_track":
        return {("auto_track",)}
    return set()


def _event_keys(event: dict, holds: dict[str, str], forward: dict) -> set[tuple]:
    """The targets one routing-record line changed (owner's or a
    conversation's); a cap proposal from a conversation changes no cap."""
    from khipu import routing, routing_events

    kind = event.get("type") or ""
    author = (event.get("written_by") or {}).get("kind")
    if author not in ("owner", "conversation"):
        return set()
    acct = lambda key: routing._forwarded(key, forward)  # noqa: E731
    if kind.startswith("priority_"):
        return {("priority",)}
    if kind in ("cap_set", "cap_remove"):
        if author != "owner":
            return set()
        return {_cap_key(acct(event.get("account")), event.get("cap_kind") or event.get("kind"),
                         event.get("purpose", "job"), event.get("window"))}
    if kind.startswith("rule_"):
        return {("rule", routing_events.entity(event))}
    if kind == "hold":
        return {("hold", event.get("id"))}
    if kind == "hold_end":
        return {("hold", holds.get(event.get("target"), event.get("target")))}
    if kind in ("pause", "lift"):
        return {("pause", acct(event.get("account")))}
    if kind in ("note_set", "note_end"):
        return {("note",)}
    if kind.startswith("tier_table_"):
        return {("tier_table",)}
    if kind == "definition_set":
        value = event.get("value") if isinstance(event.get("value"), dict) else {}
        return {("definition", event.get("definition"),
                 value.get("id") or f"{value.get('runner')}:{value.get('lane')}")}
    return set()


def _overlap(a: set[tuple], b: set[tuple]) -> tuple | None:
    """The shared target, or None. The whole account list overlaps every
    one of its accounts."""
    both = a & b
    if both:
        return next(iter(both))
    for x, y in ((a, b), (b, a)):
        if ("accounts",) in x and any(k[0] == "account" for k in y):
            return ("accounts",)
    return None


def _hold_aliases(events: list[dict] | None = None) -> dict[str, str]:
    """Each name a hold goes by (its id, revision, entity) to its id: an end
    may name a hold by any of them."""
    from khipu import routing, routing_events

    out: dict[str, str] = {}
    for e in routing.read_events()[0] if events is None else events:
        if e.get("type") == "hold" and e.get("id"):
            for alias in (e["id"], e.get("revision"), routing_events.entity(e)):
                if alias:
                    out[alias] = e["id"]
    return out


def _held(keys: set[tuple], aliases: dict[str, str]) -> set[tuple]:
    return {("hold", aliases.get(k[1], k[1])) if k[0] == "hold" else k for k in keys}


def _op_owner(op, lines: dict[str, dict]) -> tuple[str, str] | None:
    """Which owner-actions line a routing write belongs to: ``("action",
    id)`` for an action's own write, ``("undo", id of the undone action)``
    for an Undo's (``owner-undo:<id>``, or an older Undo line's own id), or
    None for a write no owner action made."""
    op = str(op or "")
    parts = op.split(":")
    if op.startswith("owner-undo:") and len(parts) > 1:
        return ("undo", parts[1]) if parts[1] in lines else None
    if parts[0] in lines:
        line = lines[parts[0]]
        # Older Undo lines used their own id as the routing operation.
        if line.get("kind") == "undo":
            return ("undo", line["undoes"]) if line.get("undoes") in lines else None
        return ("action", parts[0])
    return None


def _undo_lines_of(undone_id: str, lines: dict[str, dict]) -> list[str]:
    return [i for i, x in lines.items() if x.get("kind") == "undo" and x.get("undoes") == undone_id
            and x["state"] != "failed"]


def _forwarding():
    """The routing record's account forwarding (a moved login's old key to
    its key now) and its lines in write order, each forwarded as of when it
    was written."""
    from khipu import routing

    return routing.forward_events(routing._in_write_order(routing.read_events()[0]),
                                  {a["key"] for a in routing.accounts()})


def newer_change(line: dict) -> dict | None:
    """The newest change, made after ``line``, to a target its Undo would
    restore: ``{"what", "at", "id", "target", "count"}``; None when there is
    none. Changes that were themselves undone (with their Undo) don't count.
    Account keys are followed through moves to the key the account has now,
    and a later owner action counts by every routing line it wrote, not only
    by its name (``accounts stop`` also changes the priority)."""
    lines = _merged(read_log())
    order = list(lines)
    if line["id"] not in lines:
        return None
    mine_at = order.index(line["id"])
    if not _line_keys(line):
        return None
    forward, ordered = _forwarding()
    aliases = _hold_aliases(ordered) if (line.get("action") or "").startswith("route.hold.") else {}
    keys = _held(_line_keys(line, forward), aliases)
    if not keys:
        return None
    # One finding per change: an owner action (or the Undo of one) is
    # counted once, whether its name or one of its routing lines matched.
    found: dict[tuple, dict] = {}
    for later_id in order[mine_at + 1:]:
        later = lines[later_id]
        if later.get("kind") == "action":
            if later["state"] in ("failed", "undone", "cancelled"):
                continue
            words, subject, source = (_ACTION_WORDS.get(later.get("action"), f"owner action {later.get('action')}"),
                                      later, ("action", later_id))
        elif later.get("kind") == "undo":
            undone = lines.get(later.get("undoes") or "")
            if (undone is None or later["state"] == "failed" or undone["id"] == line["id"]
                    or order.index(undone["id"]) > mine_at):
                # The newer action and its own Undo cancel out.
                continue
            words = "the undo of " + _ACTION_WORDS.get(undone.get("action"), f"owner action {undone.get('action')}")
            subject, source = undone, ("undo", undone["id"])
        else:
            continue
        shared = _overlap(keys, _held(_line_keys(subject, forward), aliases))
        if shared and source not in found:
            found[source] = {"what": f"your later change ({words}, owner action {later_id})", "at": later.get("at"),
                             "id": later_id, "target": shared[0]}
    if (line.get("action") or "").startswith("route."):
        _newer_routing_writes(line, lines, mine_at, keys, forward, ordered, found)
    if not found:
        return None
    newest = max(found.values(), key=lambda f: str(f.get("at") or ""))
    return dict(newest, count=len(found))


def _newer_routing_writes(line: dict, lines: dict[str, dict], mine_at: int, keys: set[tuple], forward: dict,
                          ordered: list[dict], found: dict[tuple, dict]) -> None:
    """Add to ``found`` the routing-record writes after this action's own
    lines that touch its target: a conversation's (a proposal, a hold
    ended), or one an owner action wrote as part of its batch. An action
    whose own lines can't be found is checked against the owner-actions log
    alone."""
    order = list(lines)
    own = [i for i, e in enumerate(ordered) if _op_owner(e.get("op"), lines) == ("action", line["id"])]
    if not own:
        return
    holds = _hold_aliases(ordered)
    for e in ordered[max(own) + 1:]:
        source = _op_owner(e.get("op"), lines)
        if source is not None:
            kind, owner_id = source
            if owner_id == line["id"] or source in found:
                continue
            owner_line = lines[owner_id]
            if kind == "action" and (order.index(owner_id) <= mine_at
                                     or owner_line["state"] in ("failed", "undone", "cancelled")):
                continue
            if kind == "undo" and (order.index(owner_id) > mine_at or not _undo_lines_of(owner_id, lines)):
                # A later action's own Undo cancels it out.
                continue
        shared = _overlap(keys, _event_keys(e, holds, forward))
        if not shared:
            continue
        if source is None:
            who = "a conversation's" if (e.get("written_by") or {}).get("kind") == "conversation" else "your"
            found[("routing", e.get("id"))] = {
                "what": f"{who} later {_EVENT_WORDS.get(e.get('type'), 'routing')} change (routing line {e.get('id')})",
                "at": e.get("written_at"), "id": e.get("id"), "target": shared[0]}
            continue
        kind, owner_id = source
        if kind == "action":
            words = _ACTION_WORDS.get(lines[owner_id].get("action"), f"owner action {lines[owner_id].get('action')}")
            at, line_id = lines[owner_id].get("at"), owner_id
        else:
            line_id = _undo_lines_of(owner_id, lines)[-1]
            words = "the undo of " + _ACTION_WORDS.get(lines[owner_id].get("action"),
                                                        f"owner action {lines[owner_id].get('action')}")
            at = lines[line_id].get("at")
        found[source] = {"what": f"your later change ({words}, owner action {line_id}, routing line {e.get('id')})",
                         "at": at, "id": line_id, "target": shared[0]}


def newer_change_words(line: dict, newer: dict) -> str:
    thing = _TARGET_WORDS.get(newer.get("target"), "thing")
    more = f" (the newest of {newer['count']})" if newer.get("count", 1) > 1 else ""
    return (f"Not undone: {newer['what']}{more} changed the same {thing} after this change, at "
            f"{newer.get('at') or 'a time Khipu did not record'}. Undoing now would put back what was there "
            "before and lose that newer change, so nothing was changed. Undo the newer change first, or undo "
            "this one over it on purpose.")


def _refuse_over_newer(line: dict, over_newer: bool) -> None:
    """Refuse the Undo over a newer change, or (``over_newer``) record that
    change's id on the Undo's own line, as it stands under the lock."""
    newer = newer_change(line)
    if not newer:
        return
    if not over_newer:
        raise NewerChange(newer_change_words(line, newer), newer)
    pend = _PENDING.get()
    if not pend:
        return
    facts = dict(pend["intent"].get("facts") or {})
    if facts.get("newer") == newer.get("id"):
        return
    facts.update(over_newer=True, newer=newer.get("id"))
    pend["intent"]["facts"] = facts
    ahead = pend.get("line")
    if ahead and ahead.get("kind") == "undo" and ahead.get("undoes") == line["id"]:
        _append({"kind": "state", "id": _new_id(), "at": _iso(_clock()), "target": ahead["id"], "state": "pending",
                 "facts": facts})
        ahead["facts"] = facts


def undo(line_id: str, via: str, *, holds: dict | None = None, over_newer: bool = False,
         include_unproven: bool = False) -> dict:
    """Apply the inverse of a logged action. Its ``undo`` line is written
    ahead by the owner gate (``gate``/``noted``) and completed by
    ``finish``. Refused (``NewerChange``) when a newer change touched the
    same target, unless ``over_newer``. ``include_unproven`` (a preference
    merge only) also reverts episodes whose commit was never proven."""
    token = _INCLUDE_UNPROVEN.set(include_unproven)
    try:
        return _undo(line_id, via, holds, over_newer)
    finally:
        _INCLUDE_UNPROVEN.reset(token)


def _undo(line_id: str, via: str, holds: dict | None, over_newer: bool = False) -> dict:
    line = _find(line_id)
    why = _undo_refusal(line, line_id)
    if why:
        raise ValueError(why)
    if line["action"] in ("direction.set", "direction.on", "direction.off") and via not in ("app", "direction"):
        raise ValueError("Authorization settings require verified app or prompt-bound owner direction")
    if line["action"] == "ledger.purge":
        _append({"kind": "state", "id": _new_id(), "at": _iso(_clock()), "target": line_id, "state": "cancelled"})
        return {"ok": True, "undid": line_id, "state": "cancelled"}
    from khipu import routing

    # The routing lock covers the account list's writers too, so no newer
    # change can land between the check below and the inverse.
    locked = line["action"].startswith(("route.", "accounts.", "config.usage_accounts"))
    with routing._lock() if locked else contextlib.nullcontext():
        inverse_id = "owner-undo:" + line_id
        if line["action"].startswith("route."):
            # Older Undo rows used their own line id as the routing operation.
            known_ids = {inverse_id} | {u.get("inverse_id") or u["id"] for u in _unfinished_undos(line_id)}
            matches = [e for e in routing.read_events()[0]
                       if e.get("op") in known_ids and e.get("type") != "owner_change"]
            if matches:
                return {"ok": True, "undid": line_id, "id": matches[0].get("id"), "already_applied": True}
        _refuse_over_newer(line, over_newer)
        inverse = _UNDO[line["action"]]
        pending = (_PENDING.get() or {}).get("line")
        if (pending and pending.get("kind") == "undo" and pending.get("undoes") == line_id
                and pending.get("inverse_started") is False):
            # A crash before this durable marker proves the inverse never ran.
            _append({"kind": "state", "id": _new_id(), "at": _iso(_clock()),
                     "target": pending["id"], "state": "pending", "inverse_started": True})
        operation = routing._OP.set(inverse_id)
        try:
            return _run_inverse(line, inverse, via, holds, line_id)
        finally:
            routing._OP.reset(operation)


def _run_inverse(line, inverse, via, holds, line_id):
    _require_provenance(line)
    if line["action"] in {"route.rule.confirm", "route.rule.edit", "route.rule.retire",
                           "route.rule.undo", "route.rule.dismiss", "route.priority.set", "route.priority.amend", "route.priority.confirm",
                           "route.priority.undo", "route.priority.clear"}:
        out = inverse(line, via, holds)
    else:
        out = inverse(line, via)
    result = dict(out) if isinstance(out, dict) else {"ok": True}
    result.update(ok=True, undid=line_id)
    return result


# ---- the CLI: khipu owner-direction --------------------------------------------------------


def _settings_owner_via(args, want):
    from khipu import routing

    if not getattr(args, "from_app", False) and not getattr(args, "by_direction", False):
        return None
    return routing._owner_via(args, want)


def _print(data: dict) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str, allow_nan=False))


def _set_values(key: str, raw: str) -> dict:
    current = owner_turns.settings()
    if key == "latest_window_h":
        try:
            value = float(raw)
        except ValueError:
            raise ValueError(f"{key} takes a number") from None
        if not math.isfinite(value) or not value > 0:
            raise ValueError(f"{key} must be a finite number above 0")
        return {key: value}
    if key == "session_binding":
        if raw not in ("on", "off"):
            raise ValueError("session_binding takes on or off")
        return {key: raw == "on"}
    if key in ("trust", "untrust"):
        if not re.fullmatch(r"[a-z0-9_]+:[A-Za-z0-9_.-]+", raw or ""):
            raise ValueError(f"{key} takes a front end as harness:entrypoint")
        trusted = [t for t in current["trusted"] if t != raw] + ([raw] if key == "trust" else [])
        return {"trusted": trusted}
    raise ValueError("set takes latest_window_h, session_binding, trust or untrust")


def run_cli(args) -> int:
    """``khipu owner-direction``: show, set, on, off, acknowledge, actions,
    undo."""
    from khipu import routing

    cmd = getattr(args, "od_cmd", None) or "show"
    if cmd == "show":
        conf = owner_turns.settings()
        lines = [x for x in read_log() if x.get("kind") == "action"]
        _print({"ok": True, "settings": conf, "turns": owner_turns.counts(),
                "actions": len(lines), "by_direction": sum(1 for x in lines if x.get("auth") == "direction")})
        return 0
    if cmd == "actions":
        run_due()
        rows = actions()
        rows.reverse()
        chain = verify_log()
        if getattr(args, "json", False):
            _print({"ok": True, "actions": rows, "chain": chain})
        else:
            if chain["first_bad"]:
                print(doctor_block()["summary"])
            for x in rows:
                print(f"{x['id']}  {x['at']}  {x['action']} {x['target']}  ({x.get('auth')}, {x['state']})")
            if not rows:
                print("No owner actions recorded.")
        return 0
    auth = routing._OWNER_AUTH.set(None)
    try:
        if cmd == "undo":
            line = _find(args.id)
            why = _undo_refusal(line, args.id)
            if why:
                code = "not_found" if line is None else "undone" if line["state"] in ("undone", "cancelled") \
                    else "refused"
                _print({"ok": False, "error": why, "code": code})
                return 2
            over = bool(getattr(args, "over_newer", False))
            newer = newer_change(line)
            if newer and not over:
                _print({"ok": False, "error": newer_change_words(line, newer), "code": NewerChange.code,
                        "newer": newer})
                return 2
            choices = routing._holds_arg(args)
            facts = {"holds": choices} if choices is not None else {}
            if over and newer:
                # Logged as its own owner action: an Undo made over the newer change on purpose.
                facts.update(over_newer=True, newer=newer.get("id"))
            want = intent("direction.undo", args.id, facts=facts or None)
            if over and getattr(args, "by_direction", False):
                want.update(over_newer=True, owner_words=getattr(args, "owner_words", None))
            via = (_settings_owner_via(args, want) if line["action"] in
                   ("direction.set", "direction.on", "direction.off") else routing._owner_via(args, want))
            if via is None:
                _print({"ok": False, "error": routing.terminal_refusal(), "code": "owner_only"})
                return 2
            out = undo(args.id, via, holds=choices, over_newer=over)
            _print(finish(out) or out)
            return 0
        if cmd == "on":
            # Turning it on is an owner action like any other: logged, with an Undo.
            want = intent("direction.on", "direction")
            via = _settings_owner_via(args, want)
            if via is None:
                _print({"ok": False, "error": routing.terminal_refusal(), "code": "owner_only"})
                return 2
            out = {"ok": True, "settings": owner_turns.write_settings({"enabled": True})}
            _print(finish(out) or out)
            return 0
        if cmd == "acknowledge":
            verify_log()  # a break the log shows now raises the alarm first
            if read_alarm() is None:
                _print({"ok": True, "acknowledged": None, "line": "No tamper alarm is waiting."})
                return 0
            want = intent("direction.acknowledge", "alarm")
            via = routing._owner_via(args, want)
            if via is None:
                _print({"ok": False, "error": routing.terminal_refusal(), "code": "owner_only"})
                return 2
            alarm = acknowledge_alarm()
            out = {"ok": True, "acknowledged": alarm, "chain": verify_log(),
                   "line": "The tamper alarm is acknowledged."}
            _print(finish(out) or out)
            return 0
        if cmd == "off":
            want = intent("direction.off", "direction")
            via = _settings_owner_via(args, want)
            if via is None:
                _print({"ok": False, "error": routing.terminal_refusal(), "code": "owner_only"})
                return 2
            out = {"ok": True, "settings": owner_turns.write_settings({"enabled": False})}
            _print(finish(out) or out)
            return 0
        if cmd == "set":
            try:
                values = _set_values(args.key, args.value)
            except ValueError as err:
                _print({"ok": False, "error": str(err), "code": "bad_value"})
                return 2
            target = args.key if args.key not in ("trust", "untrust") else f"{args.key} {args.value}"
            want = intent("direction.set", target)
            via = _settings_owner_via(args, want)
            if via is None:
                _print({"ok": False, "error": routing.terminal_refusal(), "code": "owner_only"})
                return 2
            out = {"ok": True, "settings": owner_turns.write_settings(values)}
            _print(finish(out) or out)
            return 0
        _print({"ok": False, "error": f"unknown owner-direction command {cmd!r}", "code": "bad_command"})
        return 2
    except DirectionRefused as err:
        _print(err.refusal())
        return 2
    except NewerChange as err:
        _print({"ok": False, "error": str(err), "code": err.code, "newer": err.newer})
        return 2
    except (ValueError, routing.RouteError) as err:
        _print({"ok": False, "error": str(err), "code": getattr(err, "code", None) or "refused"})
        return 2
    finally:
        _abandon()
        routing._OWNER_AUTH.reset(auth)


def add_owner_flags(parser) -> None:
    """``--owner-direction`` on an owner command: a conversation makes the
    change on the owner's direction (``gate``)."""
    parser.add_argument("--owner-direction", dest="by_direction", action="store_true",
                        help="Make this change on the owner's direction, from a conversation they typed in recently")


def add_parser(sub) -> None:
    p = sub.add_parser("owner-direction", help="Owner direction: its settings, the owner-actions log, and Undo")
    p.set_defaults(func=run_cli, od_cmd=None)
    verbs = p.add_subparsers(dest="od_cmd")
    verbs.add_parser("show", help="The settings and how many turns and actions are recorded")
    act = verbs.add_parser("actions", help="The owner-actions log, newest first")
    act.add_argument("--json", action="store_true")
    for name, words in (("on", "Turn owner direction on"),
                        ("off", "Turn owner direction off")):
        sp = verbs.add_parser(name, help=words)
        sp.add_argument("--from-app", action="store_true", help="==SUPPRESS==")
        add_owner_flags(sp)
    ack = verbs.add_parser("acknowledge", help="Acknowledge the tamper alarm on the owner-actions log")
    ack.add_argument("--from-app", action="store_true", help="==SUPPRESS==")
    add_owner_flags(ack)
    st = verbs.add_parser("set", help="Change a setting: latest_window_h, session_binding, trust or "
                                      "untrust")
    st.add_argument("key")
    st.add_argument("value")
    st.add_argument("--from-app", action="store_true", help="==SUPPRESS==")
    add_owner_flags(st)
    un = verbs.add_parser("undo", help="Undo one owner action by its id")
    un.add_argument("id")
    un.add_argument("--holds", default=None, metavar="JSON",
                    help='Explicit choices for held or paused accounts: {"id": "end" or "keep"}')
    un.add_argument("--over-newer", dest="over_newer", action="store_true",
                    help="Undo even though a newer change touched the same target since; that change is lost")
    un.add_argument("--owner-words", dest="owner_words", default=None, metavar="TEXT",
                    help="==SUPPRESS==")
    un.add_argument("--from-app", action="store_true", help="==SUPPRESS==")
    add_owner_flags(un)
