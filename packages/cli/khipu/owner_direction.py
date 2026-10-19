"""Owner direction: an owner action a conversation makes on the owner's direction.

The third way past an owner gate, beside the app and a yes typed on a
terminal (scope: docs/plans/2026-10-16-owner-direction.md, section 2). A
conversation submits the structured action (``khipu_owner_act``, or
``--owner-direction`` on the owner command) and it applies. The one check
(``check``) is where the request comes from: a conversation where Khipu's own
prompt hook recorded an owner turn within the window (``owner_turns``), whose
hook ran above this call. That keeps background jobs, dispatched jobs and
unattended conversations from making owner changes on their own. Nothing
here reads the owner's words or calls a model: this is a permission decision.

The session is never an argument: it is read from the calling process's
environment, as ``routing.conversation_author`` reads it, and the turn's
hook must be an ancestor of the call, so a session id set by hand in another
process does not pass.

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
    "route.pause", "route.lift", "route.clear", "route.note.set", "route.note.end", "route.rule.add",
    "route.rule.edit", "route.rule.retire", "route.rule.restore", "route.rule.confirm", "route.rule.undo",
    "route.rule.dismiss", "route.hold.add", "route.hold.end", "route.priority.set", "route.priority.confirm",
    "route.priority.undo", "route.priority.clear", "accounts.track", "accounts.rename", "accounts.stop",
    "accounts.move", "accounts.decline", "accounts.offer_again", "accounts.auto_track", "accounts.request.confirm",
    "accounts.request.undo", "config.usage_accounts", "config.routing_setting", "config.answer_request.confirm",
    "config.answer_request.undo", "promote.gate_ignore", "promote.gate_ignore.cancel", "ledger.import",
    "ledger.restore", "ledger.purge", "ledger.project", "ledger.add", "ledger.set", "ledger.remove", "work.hide", "work.unhide",
    "work.same", "work.not_same", "owed.change", "owed.tidy", "direction.off", "direction.on", "direction.acknowledge", "direction.set",
    "direction.undo", "project.alias_add", "project.alias_remove", "project.merge", "project.keep_separate",
})


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


def check(action: str, *, env=None, now: float | None = None) -> dict:
    """The owner-direction check (scope section 2): the auth stamp for the
    record, or ``DirectionRefused``. The one test is where the request comes
    from: a conversation the owner typed in recently, where Khipu's own
    prompt hook recorded an eligible owner turn within the window
    (``latest_window_h``, 6 hours by default), and whose hook ran above this call. A background job, a
    dispatched job or a conversation nobody has typed in lately is refused.
    Nothing here reads what the owner said, and nothing calls a model."""
    if action not in ACTIONS:
        raise DirectionRefused("not_by_direction", "That change can't be made by direction.")
    now = _clock() if now is None else now
    conf = owner_turns.settings()
    if not conf["enabled"] and action != "direction.on":
        raise DirectionRefused("direction_off", "Owner direction is off, so only the Khipu app or a yes typed on a "
                                                "terminal can make this change.")
    session = session_of(env)
    if not session:
        raise DirectionRefused("no_recorded_turn", "Khipu can't tell which conversation this is, so it can't tell "
                                                   "whether the owner is in it.")
    if len(_sessions_named(env)) > 1:
        # A job started from a conversation keeps the conversation's id
        # beside its own: which one is asking can't be told.
        raise DirectionRefused("no_recorded_turn", "This call names more than one conversation, so Khipu can't tell "
                                                   "whether the owner is in the one asking. Ask the owner to say it "
                                                   "in the conversation they are typing in.")
    turn = _recent_turn(session, conf, now)
    _from_the_hook(turn)
    return {"auth": "direction", "harness": turn.get("harness"), "session": session, "turn": turn["turn"],
            "binding": "session" if turn.get("harness") in owner_turns.SESSION_ONLY_HARNESSES else "hook"}


def _recent_turn(session: str, conf: dict, now: float) -> dict:
    """The session's latest eligible owner turn, when it is inside the window."""
    recorded = owner_turns.turns(session, now)
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
    if now - _float(turn.get("ts")) > conf["latest_window_h"] * 3600:
        raise DirectionRefused("stale",
                               f"The owner has not typed in this conversation for over {conf['latest_window_h']:g} hours, "
                               "so it can't make an owner's change now. It works again once the owner sends a "
                               "message here.")
    return turn


_CHAIN_MOST = 16
# Processes that sit between a harness and the command it runs (its shells,
# Khipu's own Python, a launcher), passed over to find the harness that
# runs this call.
_PASS_THROUGH = ("env", "timeout", "nohup", "uv", "uvx")


def _caller_chain() -> list[tuple[int, float, str]]:
    """This process's ancestors, parent first, as (pid, start time, the
    name of the file it runs). Stops where a parent started after its child
    (its number was reused)."""
    me = owner_turns._proc_info(os.getpid())
    if me is None:
        return []
    pid, born = me[0], me[2]
    out: list[tuple[int, float, str]] = []
    for _ in range(_CHAIN_MOST):
        info = owner_turns._proc_info(pid)
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


def _from_the_hook(turn: dict) -> None:
    """The turn was recorded by a hook its harness ran (its issuer is not an
    agent's shell or a script), and that harness is the nearest one above
    this call. A session id set by hand in another process, or inherited by
    a job started from the conversation, points at a harness that is not
    the one running this call."""
    issuer = turn.get("issuer")
    refused = not isinstance(issuer, dict) or issuer.get("refused") is not None \
        or not isinstance(issuer.get("pid"), int)
    if not refused:
        refused = (issuer["pid"], issuer.get("start")) != _nearest_harness(_caller_chain())
    if refused:
        raise DirectionRefused("not_from_the_hook",
                               "The owner's recent turn was not recorded by the prompt hook of the conversation "
                               "making this call, so this call can't make an owner's change. Ask the owner to say "
                               "it in the conversation they are typing in.")


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


def _append(line: dict) -> dict:
    """Append one line to the chain, under a lock so two writers never
    chain to the same line, and on disk before it returns; then the chain's
    new length and head beside it. A head that could not be written leaves
    the check red (the log is ahead of it), never the change undone."""
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
            before = _verify_chain()
        except Exception:  # noqa: BLE001 - the check never stops an owner's change from being logged
            before = None
        if before is not None and before["first_bad"]:
            _raise_alarm(before["first_bad"])
        line["v"] = LOG_VERSION
        line["prev"] = _prev_of(raw)
        line["hash"] = _line_hash(line)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(("\n" if raw and not raw.endswith("\n") else "") + json.dumps(line, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        try:
            _write_private(head_path(), json.dumps({"v": 1, "lines": _chained(raw) + 1, "head": line["hash"]})
                           + "\n")
        except OSError:
            pass
    return line


def scrub_words(sessions) -> int:
    """Forget's owner words scan on the log: blank the quoted words of every
    line these sessions wrote or acted for (``"words": null, "scrubbed":
    true``). ``words_sha`` and the hash stay, so the chain still holds and
    the action and its Undo stay. Returns how many lines were scrubbed;
    raises OSError when the log can't be read or rewritten."""
    names = set(sessions)
    with _log_locked() as path:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return 0
        out, count = [], 0
        for text in raw.splitlines():
            try:
                row = json.loads(text)
            except ValueError:
                row = None
            if (isinstance(row, dict) and isinstance(row.get("words"), str)
                    and (row.get("session") in names or row.get("via_session") in names)):
                row["words_sha"] = row.get("words_sha") or _sha(row["words"])
                row["words"], row["scrubbed"] = None, True
                text, count = json.dumps(row, ensure_ascii=False), count + 1
            out.append(text)
        if count:
            _write_private(path, "".join(text + "\n" for text in out))
    return count


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


def _verify_chain() -> dict:
    """Whether the log's hash chain holds: ``first_bad`` names the first line
    that has no hash or a wrong one, no version or an older one after the
    chain began, does not follow the line before it, or whose words do not
    match their hash; or, by the head kept beside the log, the line where
    lines Khipu wrote are missing from the end (line numbers count from 1)."""
    try:
        raw = log_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        raw = ""
    except OSError as err:
        return {"ok": False, "lines": 0, "first_bad": {"line": 0, "id": None, "kind": None,
                                                        "why": f"the log can't be read ({err.strerror})"}}
    texts = raw.splitlines()
    rows: list = []
    for text in texts:
        try:
            rows.append(json.loads(text) if text.strip() else None)
        except ValueError:
            rows.append(ValueError)
    count = sum(1 for r in rows if r is not None)

    def bad(i: int, why: str) -> dict:
        row = rows[i] if 0 <= i < len(rows) and isinstance(rows[i], dict) else {}
        return {"ok": False, "lines": count, "first_bad": {"line": i + 1, "id": row.get("id"),
                                                           "kind": row.get("kind"), "why": why}}

    first = next((i for i, r in enumerate(rows) if isinstance(r, dict) and ("hash" in r or "v" in r)), None)
    start = first if first is not None else len(rows)
    for i in range(start):
        if rows[i] is ValueError:
            return bad(i, "it is not a readable line")
    head = _head()
    if head is ValueError:
        return bad(0, "the record of the log's length kept beside it can't be read")
    chained, last = 0, None
    if first is not None:
        prev = _legacy_prev(texts[:first]) if first else ""
        for i in range(first, len(rows)):
            row = rows[i]
            if row is None:
                continue
            if not isinstance(row, dict):
                return bad(i, "it is not a readable line")
            if not isinstance(row.get("hash"), str):
                return bad(i, "it has no hash, so Khipu did not write it")
            if row.get("v") != LOG_VERSION:
                return bad(i, "it has no version or an older one after the chain began, so Khipu did not write it")
            if row.get("prev") != prev:
                if first and i == first and str(row.get("prev") or "").startswith(LEGACY):
                    return bad(i, "a line written before the log was chained was changed")
                return bad(i, "it does not follow the line before it: a line is missing or was changed")
            if _line_hash(row) != row["hash"]:
                return bad(i, "it was changed after Khipu wrote it")
            words = row.get("words")
            if words is not None and (not isinstance(words, str) or row.get("words_sha") != _sha(words)):
                return bad(i, "its words do not match their hash")
            prev, chained, last = row["hash"], chained + 1, i
    if head is None:
        if chained:
            return bad(first, "the record of the log's length kept beside it is missing")
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
    out = {"ok": True, "lines": count, "first_bad": None}
    if first is None and count:
        out["note"] = "Every line was written before the log was chained."
    elif first:
        out["note"] = f"The first {first} lines were written before the log was chained."
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
    summary = f"Owner-actions log: {chain['lines']} lines, chain intact."
    if chain.get("note"):
        summary += " " + chain["note"]
    return {"ok": True, "chain": chain, "warnings": [], "summary": summary}


def read_log() -> list[dict]:
    try:
        raw = log_path().read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
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
        if line["state"] in ("cancelled", "done", "failed") or undo_state == "failed":
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
    stamp = check(want["action"], env=env, now=now)
    before = _before(want)
    if before is _NO_UNDO and want["action"] == "ledger.import":
        raise DirectionRefused("not_by_direction",
                               "A catch-up import can't be undone, so it can't be made by direction; it needs the "
                               "Khipu app or a yes typed on a terminal.")
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
    if not pend or pend["intent"]["action"] not in ("owed.change", "owed.tidy"):
        return
    line = pend.get("line")
    if line is None:
        raise OSError("the owner action must be recorded before its batch can be written")
    before = dict(pend["before"]) if isinstance(pend.get("before"), dict) else {}
    if pend["intent"]["action"] == "owed.change":
        before["batches"] = [*(before.get("batches") or []), batch]
    else:
        before["batch"] = batch
    _append({"kind": "state", "id": _new_id(), "at": _iso(_clock()), "target": line["id"],
             "state": "pending", "before": before})
    pend["before"] = before


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
                "action": undone.get("action"), "target": undone.get("target"), "auth": stamp.get("auth")}
    else:
        line = {"kind": "action", "id": _new_id(), "at": _iso(_clock()), "action": want["action"],
                "target": want["target"], "before": None if before in (_NO_UNDO, _UNREADABLE) else before,
                "auth": stamp.get("auth")}
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
    for key in ("words", "harness", "session", "via_session", "turn", "binding", "auth_chain"):
        if stamp.get(key) is not None:
            line[key] = stamp[key]
    line["state"] = "pending"
    return line


def _abandon() -> None:
    """End a written-ahead owner action that never reached ``finish``: the
    change raised, so it is marked failed (best effort: unmarked, it reads
    as unconfirmed, and Undo still works)."""
    pend = _PENDING.get()
    _PENDING.set(None)
    if pend and pend.get("line"):
        try:
            _append({"kind": "state", "id": _new_id(), "at": _iso(_clock()), "target": pend["line"]["id"],
                     "state": "failed"})
        except OSError:
            pass


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
    if not refused:
        _name_in_push(want, stamp, line, pend.get("stamped"))
    if stamp.get("auth") == "direction" and isinstance(result, dict) and not refused:
        out = dict(result, owner_action=line["id"])
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
    "config.routing_setting": "Changed a routing setting",
    "config.answer_request.confirm": "Confirmed a conversation's change to a routing setting",
    "config.answer_request.undo": "Undid a conversation's change to a routing setting",
    "accounts.decline": "Stopped offering an account", "accounts.offer_again": "Offered an account again",
    "accounts.auto_track": "Changed whether new accounts are tracked",
    "owed.tidy": "Applied or undid an Owed list clean-up",
}


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

    return next((r for r in routing.read_view()["rules"] if str(r["n"]) == str(n)), {}) or {}


def _priority_rows(held: dict | None) -> list[dict] | None:
    if not held:
        return None
    return [{"account": e["account"], "role": e.get("role"), "until": e.get("until")} for e in held["entries"]]


def _before(want: dict):
    """What an owner action changes, read before it does (``before`` on its
    log line); ``_NO_UNDO`` when it can't be undone from the line."""
    from khipu import routing

    action, target = want["action"], want["target"]
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
    if action in ("route.rule.edit", "route.rule.undo"):
        return {"text": _rule_now(target).get("text")}
    if action == "route.rule.add":
        # The new rule's number is known only after, so its line's id is
        # chosen now: Undo finds that line even if the finish was never
        # written, whatever else was added meanwhile.
        return {"event": routing._new_id("o")}
    if action == "route.hold.add":
        return {"hold": routing._new_id("h")}
    if action == "route.rule.confirm":
        rule = _rule_now(target)
        if not rule.get("pending"):
            return {"seeded": True}
        return {"pending_kind": rule.get("pending_kind"), "prior_text": rule.get("prior_text")}
    if action == "route.hold.end":
        hold = next((h for h in routing.read_view()["holds"] if h["id"] == target), None)
        if hold is None:
            return None
        return {"text": hold.get("text"), "reason": hold.get("reason"), "scope": hold.get("scope"),
                "until": hold.get("until")}
    if action.startswith("route.priority."):
        held = routing.read_view().get("priority")
        if action == "route.priority.confirm":
            replaced = (held or {}).get("replaced")
            return {"entries": _priority_rows(replaced), "until": (replaced or {}).get("until")}
        return {"entries": _priority_rows(held), "until": (held or {}).get("until")}
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
    if action == "config.routing_setting":
        from khipu import config

        data = config.load_config()
        return {"value": data.get(target), "set": target in data}
    if action in ("config.answer_request.confirm", "config.answer_request.undo"):
        from khipu import config

        data = config.load_config()
        waiting = routing._setting_requests(data).get(target) or {}
        if action == "config.answer_request.confirm":
            return {"value": waiting.get("prior"), "set": bool(waiting.get("prior_set"))}
        return {"value": data.get(target), "set": target in data}
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
    if action == "owed.tidy":
        facts = want.get("facts") or {}
        return {"project": target, "change": facts.get("change"), "batch": facts.get("batch")}
    if action in ("direction.off", "direction.on"):
        return {"enabled": owner_turns.settings()["enabled"]}
    if action == "direction.acknowledge":
        return {"alarm": read_alarm()}
    if action == "direction.set":
        return {"settings": {k: owner_turns.settings()[k] for k in ("latest_window_h",
                                                                   "trusted", "session_binding")}}
    if action.startswith("project."):
        from khipu import project_group

        read = project_group.read_before(action, target)
        return _UNREADABLE if read is None else read
    if action in _UNDO or action in ("ledger.purge", "direction.undo"):
        return None
    return _NO_UNDO


def _after(want: dict, result) -> Any:
    if not isinstance(result, dict):
        return None
    keep = ("id", "n", "account", "until", "entries", "version", "state", "runs_at", "ended", "label", "accounts")
    return {k: result[k] for k in keep if k in result} or None


def _restore_setting(key: str, value, was_set: bool) -> None:
    from khipu import routing
    from khipu.config import mutate_config

    with routing._lock(), mutate_config() as data:
        if was_set:
            data[key] = value
        else:
            data.pop(key, None)
        requests = routing._setting_requests(data)
        requests.pop(key, None)
        if requests:
            data[routing.SETTINGS_REQUEST_KEY] = requests
        else:
            data.pop(routing.SETTINGS_REQUEST_KEY, None)


def _set_priority_back(before: dict, via: str) -> dict:
    from khipu import routing

    if before.get("entries"):
        return routing.set_priority(before["entries"], until=before.get("until"), via=via,
                                    expect=routing.priority_version())
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

    before = line.get("before") or {}
    scope = before.get("scope") or {}
    kind = scope.get("kind")
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


def _undo_rule_confirm(line, via):
    from khipu import routing

    before = line.get("before") or {}
    if before.get("seeded"):
        return routing.unconfirm_rule(line["target"], via=via)
    if before.get("pending_kind") == "changed" and isinstance(before.get("prior_text"), str):
        return routing.edit_rule(line["target"], before["prior_text"], via=via)
    return routing.retire_rule(line["target"], via=via)


def _undo_ledger_set(line, via):
    fields = (line.get("before") or {}).get("fields") or {}
    return _ledger_write("set", line["target"], fields)


def _undo_direction(line, via):
    before = line.get("before") or {}
    if line["action"] in ("direction.off", "direction.on"):
        return {"ok": True, "settings": owner_turns.write_settings({"enabled": bool(before.get("enabled"))})}
    return {"ok": True, "settings": owner_turns.write_settings(dict(before.get("settings") or {}))}


def _undo_acknowledge(line, via):
    """The alarm the owner acknowledged comes back, as it was."""
    alarm = (line.get("before") or {}).get("alarm")
    if isinstance(alarm, dict) and read_alarm() is None:
        _write_private(alarm_path(), json.dumps(alarm) + "\n")
    return {"ok": True, "alarm": read_alarm()}


def _b(line: dict) -> dict:
    return line.get("before") or {}


def _undo_clear(line, via):
    """A dismissed report comes back; a clear is dropped, and the clear
    before it (``before``) counts again (the fold keeps them in order)."""
    from khipu import routing

    report = (line.get("facts") or {}).get("report")
    if report:
        return routing.undismiss(line["target"], report, via=via)
    clear_id = (line.get("after") or {}).get("id")
    if clear_id is None:
        # Unconfirmed: the clear's own id was never recorded, but a clear
        # newer than the one it superseded can only be this one.
        now = routing.current_clear(line["target"])
        clear_id = now if now != _b(line).get("clear") else None
    if clear_id is None:
        raise ValueError("Khipu can't tell which clear this was, so it can't undo it")
    return routing.undo_clear(line["target"], clear_id, via=via)


def _undo_lift(line, via):
    from khipu import routing

    return routing.pause(line["target"], until=_b(line).get("until"), via=via)


def _undo_note_set(line, via):
    from khipu import routing

    text = _b(line).get("text")
    return routing.set_note(text, via=via) if text else routing.end_note(via=via)


def _undo_note_end(line, via):
    from khipu import routing

    return routing.set_note(_b(line).get("text") or "", via=via)


def _undo_rule_words(line, via):
    from khipu import routing

    return routing.edit_rule(line["target"], _b(line).get("text") or "", via=via)


def _retire_rule(n_of):
    def run(line, via):
        from khipu import routing

        return routing.retire_rule(n_of(line), via=via)
    return run


def _undo_restore_rule(line, via):
    from khipu import routing

    return routing.restore_rule(line["target"], via=via)


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


def _added_rule(line) -> int:
    from khipu import routing

    n = (line.get("after") or {}).get("n")
    if n is None:
        # Unconfirmed: the rule written under the id this action chose.
        planned = _b(line).get("event")
        events, _bad = routing.read_events()
        n = next((e["n"] for e in events if planned and e.get("type") == "rule_add" and e.get("id") == planned
                  and isinstance(e.get("n"), int)), None)
    if n is None:
        raise ValueError("Khipu can't tell which rule this added, so it can't undo it")
    return n


def _undo_priority(line, via):
    return _set_priority_back(_b(line), via)


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
    _restore_setting(line["target"], _b(line).get("value"), bool(_b(line).get("set")))
    return {"ok": True}


def _undo_gate_ignore(line, via):
    from khipu import promote

    promote.set_gate_ignore(_b(line).get("checks") or None, by=_by(via))
    return {"ok": True}


def _undo_project_group(line, via):
    from khipu import project_group

    return project_group.run_undo(line["action"], line["target"], line.get("before") or {})


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
        out = owed_tidy.undo(batch, owner_gate=_by(via))
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
        out = owed_tidy.redo(batch, project=line["target"], owner_gate=_by(via))
        counted = {"reapplied": int(out.get("reapplied") or 0)}
    else:
        out = owed_tidy.undo(batch, project=line["target"], owner_gate=_by(via))
        counted = {"restored": int(out.get("restored") or 0)}
    if not out.get("ok"):
        raise ValueError(out.get("error") or f"batch {batch} could not be undone")
    return {"ok": True, **counted, "left_alone": out.get("left_alone") or []}


def _ledger_undo(verb: str):
    def run(line, via):
        return _ledger_write(verb, line["target"])
    return run


_UNDO = {
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
    "route.priority.confirm": _undo_priority,
    "route.priority.undo": _undo_priority,
    "route.priority.clear": _undo_priority,
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
    "owed.change": _undo_owed_change,
    "owed.tidy": _undo_owed_tidy,
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


def _undo_refusal(line: dict | None, line_id: str) -> str | None:
    """Why the logged action ``line_id`` can't be undone now, or None."""
    if line is None:
        return f"no owner action {line_id} is in the log"
    state = line["state"]
    if state in ("undone", "cancelled"):
        return f"owner action {line_id} was already undone"
    if state == "failed":
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


def undo(line_id: str, via: str) -> dict:
    """Apply the inverse of a logged action. Its ``undo`` line is written
    ahead by the owner gate (``gate``/``noted``) and completed by
    ``finish``."""
    line = _find(line_id)
    why = _undo_refusal(line, line_id)
    if why:
        raise ValueError(why)
    if line["action"] == "ledger.purge":
        _append({"kind": "state", "id": _new_id(), "at": _iso(_clock()), "target": line_id, "state": "cancelled"})
        return {"ok": True, "undid": line_id, "state": "cancelled"}
    out = _UNDO[line["action"]](line, via)
    result = dict(out) if isinstance(out, dict) else {"ok": True}
    result.update(ok=True, undid=line_id)
    return result


# ---- the CLI: khipu owner-direction --------------------------------------------------------


def _print(data: dict) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _set_values(key: str, raw: str) -> dict:
    current = owner_turns.settings()
    if key == "latest_window_h":
        try:
            value = float(raw)
        except ValueError:
            raise ValueError(f"{key} takes a number") from None
        if not value > 0:
            raise ValueError(f"{key} must be above 0")
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
            want = intent("direction.undo", args.id)
            via = routing._owner_via(args, want)
            if via is None:
                _print({"ok": False, "error": routing._REFUSED, "code": "owner_only"})
                return 2
            out = undo(args.id, via)
            _print(finish(out) or out)
            return 0
        if cmd == "on":
            # Turning it on is an owner action like any other: logged, with an Undo.
            want = intent("direction.on", "direction")
            via = routing._owner_via(args, want)
            if via is None:
                _print({"ok": False, "error": routing._REFUSED, "code": "owner_only"})
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
                _print({"ok": False, "error": routing._REFUSED, "code": "owner_only"})
                return 2
            alarm = acknowledge_alarm()
            out = {"ok": True, "acknowledged": alarm, "chain": verify_log(),
                   "line": "The tamper alarm is acknowledged."}
            _print(finish(out) or out)
            return 0
        if cmd == "off":
            want = intent("direction.off", "direction")
            via = routing._owner_via(args, want)
            if via is None:
                _print({"ok": False, "error": routing._REFUSED, "code": "owner_only"})
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
            via = routing._owner_via(args, want)
            if via is None:
                _print({"ok": False, "error": routing._REFUSED, "code": "owner_only"})
                return 2
            out = {"ok": True, "settings": owner_turns.write_settings(values)}
            _print(finish(out) or out)
            return 0
        _print({"ok": False, "error": f"unknown owner-direction command {cmd!r}", "code": "bad_command"})
        return 2
    except DirectionRefused as err:
        _print(err.refusal())
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
    un.add_argument("--from-app", action="store_true", help="==SUPPRESS==")
    add_owner_flags(un)
