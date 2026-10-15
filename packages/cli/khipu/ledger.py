"""The ledger: what work is done and what is not, per project (Work view,
phase 1; docs/plans/2026-10-15-work-view.md, Part A).

Rows live in the hub database (migration 0028). Every write is one
transaction: the row is locked, a history entry with the old and new value of
every changed field is appended, then the row is updated. Each write carries an
op id; ``ledger_ops`` binds each op id to its write for good (landed or
refused, purged or not), so a retried write returns the first result or
refusal instead of writing twice, and an id never carries a second write.

When the hub cannot be reached a write waits in the ledger outbox
(``<data dir>/ledger-outbox/``, one file per write, temp file and rename) and
drains on the next ledger call that reaches the hub, at each Stop hook and
nightly. Reads while the hub is away come from the offline copy
(``hub_snapshot``) with the waiting writes applied on top.

A row leaves the ledger only through ``remove`` (restorable in the app for
``ledger_purge_after``) and its purge, which keeps a bare marker in
``ledger_removed`` so the id never comes back. ``_purge_row`` holds the only
DELETE statements in this module.

The CLI keeps the old tracker script's verbs, flags and printed shapes so an
orchestrator switches by replacing ``python3 tracker.py`` with
``khipu ledger``. Each project keeps its own state and kind words.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

# ---- vocabulary ---------------------------------------------------------------

MEANINGS = ("open", "done", "dropped", "needs")
OWNER_WORDS = {"NEEDS OWNER": "needs-owner", "NEEDS MATT": "needs-matt"}
NEEDS_KIND_WORDS = frozenset(OWNER_WORDS.values())
# The default words of a new project, in the tracker's order. The needs-you
# words come from `ledger_owner_word`.
_DEFAULT_STATES = (
    ("VERIFIED", "done"),
    ("MERGED-UNVERIFIED", "open"),
    ("BUILT-UNMERGED", "open"),
    ("IN FLIGHT", "open"),
    ("QUEUED", "open"),
    ("NOT STARTED", "open"),
    (None, "needs"),
    ("DROPPED", "dropped"),
    ("DONE", "done"),
)
_DEFAULT_KINDS = ("goal", "slice", "review", "loose-end", None, "audit-item", "drive", "wave", "train")
START_STATE = "NOT STARTED"
# The tracker printed loose ends in this order of states, then any others.
LOOSE_ORDER = ("IN FLIGHT", "BUILT-UNMERGED", "MERGED-UNVERIFIED", "QUEUED", "NOT STARTED")
EMPTY_OWNERS = frozenset({"", "none", "unassigned", "unowned", "-", "n/a"})
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*")
PROJECT_MAX = 200
SECRET_REFUSAL = "This looks like a key or password; nothing was saved"
QUEUED_WORDS = "Saved on this Mac; it reaches the ledger when the database answers"
HUB_CONNECT_TIMEOUT_S = 5
# How long a write waits for another process draining this Mac's waiting writes
# before it queues behind them instead.
DRAIN_WAIT_S = 30.0

# ---- settings -----------------------------------------------------------------

SETTINGS: dict[str, tuple[str, Any, str]] = {
    "ledger_id_chars": ("count", 80, "Longest row id (0: no limit)"),
    "ledger_title_chars": ("count", 2000, "Longest title (0: no limit)"),
    "ledger_evidence_chars": ("count", 1000, "Longest evidence (0: no limit)"),
    "ledger_stale_after": ("duration", 7 * 86400, "An open row with no change for this long is flagged (0: never)"),
    "ledger_purge_after": ("duration", 30 * 86400,
                           "A removed row can be restored for this long, then is removed for good (0: never)"),
    "ledger_owner_word": ("owner_word", "NEEDS OWNER", "The word for rows waiting on you (NEEDS OWNER or NEEDS MATT)"),
}


def _coerce(kind: str, value: Any, key: str) -> Any:
    if kind == "owner_word":
        word = " ".join(str(value).split()).upper()
        if word not in OWNER_WORDS:
            raise ValueError(f"{key} must be one of: {', '.join(OWNER_WORDS)}")
        return word
    from khipu.routing import _coerce as routing_coerce

    return routing_coerce(kind, value, key)


def settings(config: dict | None = None) -> dict[str, Any]:
    """Each ledger setting's value: the stored one when valid, else the default."""
    if config is None:
        from khipu.config import load_config

        config = load_config()
    out: dict[str, Any] = {}
    for key, (kind, default, _words) in SETTINGS.items():
        value = default
        if config.get(key) is not None:
            try:
                value = _coerce(kind, config[key], key)
            except (ValueError, TypeError):
                value = default
        out[key] = value
    return out


def settings_status() -> dict[str, dict]:
    from khipu.config import load_config

    config = load_config()
    values = settings(config)
    out = {}
    for key, (kind, default, words) in SETTINGS.items():
        source = "default"
        if config.get(key) is not None:
            try:
                _coerce(kind, config[key], key)
                source = "file"
            except (ValueError, TypeError):
                source = "invalid"
        out[key] = {"value": values[key], "default": default, "kind": kind, "source": source, "words": words}
    return out


def set_setting(key: str, raw: Any) -> Path:
    """Store a ledger setting (``None`` removes it: the default again)."""
    from khipu.config import _load_config_for_mutation, save_config

    if key not in SETTINGS:
        raise KeyError(f"unknown ledger setting {key!r}")
    data = _load_config_for_mutation()
    if raw is None:
        data.pop(key, None)
    else:
        data[key] = _coerce(SETTINGS[key][0], raw, key)
    return save_config(data)


def default_vocab(owner_word: str | None = None) -> dict[str, list]:
    word = owner_word or settings()["ledger_owner_word"]
    states = [[w or word, meaning] for w, meaning in _DEFAULT_STATES]
    kinds = [k or OWNER_WORDS[word] for k in _DEFAULT_KINDS]
    return {"state": states, "kind": kinds}


# ---- errors, time, authors ------------------------------------------------------


class LedgerError(Exception):
    """A refused write or read. ``reason`` is a fixed word for --json callers."""

    def __init__(self, message: str, reason: str = "refused"):
        super().__init__(message)
        self.reason = reason


class HubAway(Exception):
    """The hub database could not be reached."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _z(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(text: Any) -> datetime | None:
    if isinstance(text, datetime):
        return text if text.tzinfo else text.replace(tzinfo=timezone.utc)
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _date_words(dt: datetime | None) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d") if dt else "an unknown date"


def author_for(*, from_app: bool = False) -> dict[str, Any]:
    """Who a write is from, set by Khipu from the caller's own environment and
    folder; a caller never names its own author."""
    if from_app:
        return {"kind": "owner"}
    from khipu.routing import conversation_author

    return conversation_author()


def author_words(by: dict | None) -> str:
    by = by or {}
    kind = by.get("kind")
    if kind == "owner":
        return "you, in the app"
    if kind == "import":
        return "imported, author not recorded"
    if kind == "runner":
        return f"a job runner in {by.get('project') or 'an unknown project'}"
    if kind == "nightly":
        return "Khipu's nightly purge"
    if kind != "conversation":
        return "an unknown writer"
    from khipu.routing import HARNESS_WORDS, SHELL

    harness = by.get("harness") or SHELL
    if harness == SHELL:
        words = "a shell command"
    elif harness in HARNESS_WORDS:
        words = f"a {HARNESS_WORDS[harness]} conversation"
    else:
        words = "a conversation from an unknown harness"
    if by.get("project"):
        words += f" in {by['project']}"
    if by.get("session"):
        words += f" (session {str(by['session'])[:8]})"
    return words


# ---- checks on what a caller sends -----------------------------------------------


def _check_project(project: str | None) -> str:
    text = (project or "").strip()
    if not text:
        raise LedgerError("no project: run this inside a project's folder or pass --project", "no_project")
    if len(text) > PROJECT_MAX or any(ord(c) < 32 for c in text) or "#" in text:
        raise LedgerError("a project is one line of at most 200 characters, without #", "bad_project")
    return text


def _check_id(row_id: str, limits: dict[str, Any]) -> str:
    if not isinstance(row_id, str) or not _ID_RE.fullmatch(row_id):
        raise LedgerError("a row id must start with a letter or digit and use only letters, digits and . _ : / -",
                          "bad_id")
    cap = limits["ledger_id_chars"]
    if cap and len(row_id) > cap:
        raise LedgerError(f"row id is {len(row_id)} characters; the limit is {cap} (ledger_id_chars)", "too_long")
    return row_id


def _check_length(field: str, text: str, setting: str, limits: dict[str, Any]) -> None:
    cap = limits[setting]
    if cap and len(text) > cap:
        raise LedgerError(f"{field} is {len(text)} characters; the limit is {cap} ({setting}). Nothing was saved",
                          "too_long")


def _check_link(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    parts = urlsplit(text)
    if parts.scheme != "https" or not parts.netloc or any(c.isspace() for c in text):
        raise LedgerError("a link must be an https link", "bad_link")
    return urlunsplit(("https", parts.netloc, parts.path, "", ""))


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def refuse_secrets(*values: Any) -> None:
    """Refuse when any string a caller sent (ids and the project included)
    looks like a key. Runs before any other check, so no error message, queued
    file or row ever carries one."""
    from khipu.redact import redact_secrets

    for text in _strings(list(values)):
        if text and redact_secrets(text, force=True)[1]:
            raise LedgerError(SECRET_REFUSAL, "secret")


def _check_op(op: dict[str, Any], limits: dict[str, Any]) -> None:
    """Everything that needs no database: secrets, ids, lengths, links. The
    author is Khipu's own, so it is not a caller's string."""
    refuse_secrets({k: v for k, v in op.items() if k != "author"})
    _check_project(op.get("project"))
    _check_id(op.get("row_id") or "", limits)
    fields = op.get("fields") or {}
    if op["verb"] == "add" and not (fields.get("title") or "").strip():
        raise LedgerError("a row needs a title", "no_title")
    if "title" in fields:
        _check_length("title", fields["title"], "ledger_title_chars", limits)
    if "evidence" in fields:
        _check_length("evidence", fields["evidence"], "ledger_evidence_chars", limits)
    if fields.get("parent"):
        _check_id(fields["parent"], {"ledger_id_chars": 0})
    if "link" in fields:
        fields["link"] = _check_link(fields["link"])


def _word(vocab: dict[str, list], field: str, given: str | None) -> str | None:
    """The project's own word for ``given``. NEEDS MATT and NEEDS OWNER (and
    their kinds) both name the project's one needs-you word."""
    if given is None:
        return None
    words = [w for w, _m in vocab["state"]] if field == "state" else list(vocab["kind"])
    if given in words:
        return given
    if field == "state" and " ".join(given.split()).upper() in OWNER_WORDS:
        needs = [w for w, m in vocab["state"] if m == "needs"]
        if len(needs) == 1:
            return needs[0]
    if field == "kind" and given.strip().lower() in NEEDS_KIND_WORDS:
        needs_kinds = [k for k in words if k in NEEDS_KIND_WORDS]
        if len(needs_kinds) == 1:
            return needs_kinds[0]
    raise LedgerError(f"{field} must be one of: {', '.join(words)}", f"bad_{field}")


def _meaning(vocab: dict[str, list], state: str) -> str | None:
    for word, meaning in vocab["state"]:
        if word == state:
            return meaning
    return None


# ---- the op -------------------------------------------------------------------


def new_op(verb: str, project: str, row_id: str, *, fields: dict | None = None, note: str = "",
           if_state: str | None = None, children: str | None = None, op_id: str | None = None,
           from_app: bool = False, source: str = "cli") -> dict[str, Any]:
    if op_id is not None and not re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", op_id):
        raise LedgerError("an op id is 1 to 80 letters, digits and . _ : -", "bad_op_id")
    return {
        "op_id": op_id or uuid.uuid4().hex,
        "verb": verb,
        "project": project,
        "row_id": row_id,
        "fields": {k: v for k, v in (fields or {}).items() if v is not None},
        "note": note or "",
        "if_state": if_state,
        "children": children,
        "author": author_for(from_app=from_app or source == "app"),
        "source": "app" if from_app else source,
        "at": _z(_now()),
    }


_DIGEST_KEYS = ("verb", "project", "row_id", "fields", "note", "if_state", "children")


def op_digest(op: dict[str, Any]) -> str:
    """The write itself, without when or by whom it was made: an op id stands
    for exactly one digest, forever. Taken once, before the offline check
    rewrites any word, and carried with the op from then on."""
    if not op.get("digest"):
        canonical = json.dumps({k: op.get(k) for k in _DIGEST_KEYS}, sort_keys=True, separators=(",", ":"),
                               default=str)
        op["digest"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return op["digest"]


def _reused_error(op: dict[str, Any]) -> LedgerError:
    return LedgerError(f"op id {op['op_id']} was already used for a different write; nothing was changed",
                       "op_id_reused")


# ---- the hub ------------------------------------------------------------------


def _connect():
    from khipu.hub_snapshot import hub_connection_failed, try_hub_connect

    try:
        return try_hub_connect(connect_timeout=HUB_CONNECT_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001  (classified below)
        if hub_connection_failed(exc):
            raise HubAway(str(exc)) from exc
        raise


def _tables_missing(exc: BaseException) -> bool:
    try:
        import psycopg

        return isinstance(exc, psycopg.errors.UndefinedTable)
    except ImportError:
        return False


def _away(exc: BaseException) -> bool:
    if isinstance(exc, HubAway):
        return True
    if isinstance(exc, LedgerError) or _tables_missing(exc):
        return False
    from khipu.hub_snapshot import hub_connection_failed

    return hub_connection_failed(exc)


def _vocab(cur, project: str) -> dict[str, list]:
    cur.execute("SELECT field, word, meaning FROM ledger_vocab WHERE project = %s ORDER BY field, position",
                (project,))
    out: dict[str, list] = {"state": [], "kind": []}
    for field, word, meaning in cur.fetchall():
        if field == "state":
            out["state"].append([word, meaning])
        else:
            out["kind"].append(word)
    return out


def ensure_project(cur, project: str, vocab: dict[str, list] | None = None) -> None:
    """Create ``project`` with ``vocab`` (default: the standard words) if it is
    new. An existing project's words are never changed here."""
    cur.execute("INSERT INTO ledger_projects (project, name) VALUES (%s, %s) "
                "ON CONFLICT (project) DO NOTHING RETURNING project", (project, project))
    if cur.fetchone() is None:
        return
    vocab = vocab or default_vocab()
    for pos, (word, meaning) in enumerate(vocab["state"]):
        if meaning not in MEANINGS:
            raise LedgerError(f"the meaning of {word!r} must be one of: {', '.join(MEANINGS)}", "bad_vocab")
        cur.execute("INSERT INTO ledger_vocab (project, field, word, meaning, position) "
                    "VALUES (%s, 'state', %s, %s, %s) ON CONFLICT DO NOTHING", (project, word, meaning, pos))
    for pos, word in enumerate(vocab["kind"]):
        cur.execute("INSERT INTO ledger_vocab (project, field, word, meaning, position) "
                    "VALUES (%s, 'kind', %s, '', %s) ON CONFLICT DO NOTHING", (project, word, pos))


def _marker(cur, project: str, row_id: str) -> tuple | None:
    cur.execute("SELECT removed_by, removed_at, purged_at FROM ledger_removed WHERE project = %s AND row_id = %s",
                (project, row_id))
    return cur.fetchone()


def _purged_error(row_id: str, marker: tuple) -> LedgerError:
    return LedgerError(f"row {row_id} was removed for good on {_date_words(marker[2])}", "purged")


_ROW_COLS = ("row_id", "title", "kind", "parent", "state", "owner", "evidence", "link", "refs", "seq",
             "created_at", "updated_at", "version", "removed_at", "removed_by", "removed_note")
_HIST_COLS = ("id", "row_id", "at", "at_text", "author", "shape", "from_state", "to_state", "changes", "note",
              "event", "raw", "source")


def _row_dict(values: tuple) -> dict[str, Any]:
    return dict(zip(_ROW_COLS, values, strict=True))


def _lock_row(cur, project: str, row_id: str) -> dict[str, Any] | None:
    cur.execute(f"SELECT {', '.join(_ROW_COLS)} FROM ledger_rows WHERE project = %s AND row_id = %s FOR UPDATE",
                (project, row_id))
    got = cur.fetchone()
    return _row_dict(got) if got else None


def _removed_error(row: dict[str, Any], suffix: str = "") -> LedgerError:
    return LedgerError(f"row {row['row_id']} was removed on {_date_words(row['removed_at'])} by "
                       f"{author_words(row['removed_by'])}{suffix}", "removed")


def _live_row(cur, project: str, row_id: str) -> dict[str, Any]:
    row = _lock_row(cur, project, row_id)
    if row is None:
        marker = _marker(cur, project, row_id)
        if marker:
            raise _purged_error(row_id, marker)
        raise LedgerError(f"no such row: {row_id}", "not_found")
    if row["removed_at"] is not None:
        raise _removed_error(row)
    return row


def _history(cur, op: dict[str, Any], row_id: str, *, from_state: str | None, to_state: str | None,
             changes: dict, note: str, at: datetime) -> None:
    from psycopg.types.json import Jsonb

    cur.execute(
        "INSERT INTO ledger_history (project, row_id, at, at_text, author, from_state, to_state, changes, note, "
        "op_id, source) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (op["project"], row_id, at, _z(at), Jsonb(op["author"]), from_state, to_state, Jsonb(changes), note,
         op["op_id"], op["source"]),
    )


def _last_setter(cur, project: str, row_id: str) -> str:
    cur.execute("SELECT author, at FROM ledger_history WHERE project = %s AND row_id = %s ORDER BY id DESC LIMIT 1",
                (project, row_id))
    got = cur.fetchone()
    return f"{author_words(got[0])} at {_z(got[1])}" if got else "an unknown writer"


def _op_time(op: dict[str, Any]) -> datetime:
    return _parse_time(op.get("at")) or _now()


def _add(cur, op: dict[str, Any]) -> dict[str, Any]:
    project, row_id, fields = op["project"], op["row_id"], op["fields"]
    marker = _marker(cur, project, row_id)
    if marker:
        raise _purged_error(row_id, marker)
    ensure_project(cur, project)
    cur.execute("SELECT next_seq FROM ledger_projects WHERE project = %s FOR UPDATE", (project,))
    seq = cur.fetchone()[0]
    vocab = _vocab(cur, project)
    kind = _word(vocab, "kind", fields.get("kind"))
    state = _word(vocab, "state", fields.get("state") or None)
    if state is None:
        if START_STATE not in [w for w, _m in vocab["state"]]:
            raise LedgerError(f"this project has no {START_STATE}; give --state", "bad_state")
        state = START_STATE
    existing = _lock_row(cur, project, row_id)
    if existing is not None:
        if existing["removed_at"] is not None:
            raise _removed_error(existing, "; it can be restored in the app")
        raise LedgerError(f"row {row_id} already exists; use set to update it", "exists")
    at = _op_time(op)
    row = {"title": fields["title"], "kind": kind, "parent": fields.get("parent") or "", "state": state,
           "owner": fields.get("owner") or "none", "evidence": fields.get("evidence") or "",
           "link": fields.get("link") or ""}
    cur.execute(
        "INSERT INTO ledger_rows (project, row_id, title, kind, parent, state, owner, evidence, link, seq, "
        "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (project, row_id, row["title"], kind, row["parent"], state, row["owner"], row["evidence"], row["link"],
         seq, at, at),
    )
    cur.execute("UPDATE ledger_projects SET next_seq = next_seq + 1 WHERE project = %s", (project,))
    changes = {k: {"old": None, "new": v} for k, v in row.items() if v}
    _history(cur, op, row_id, from_state=None, to_state=state, changes=changes, note=op["note"] or "seeded", at=at)
    return {"ok": True, "verb": "add", "id": row_id, "state": state, "line": f"added {row_id}: {state}",
            "affected": [row_id]}


_SET_FIELDS = ("state", "evidence", "owner", "title", "link")


def _set(cur, op: dict[str, Any], *, late: bool = False) -> dict[str, Any]:
    project, row_id, fields = op["project"], op["row_id"], op["fields"]
    row = _live_row(cur, project, row_id)
    vocab = _vocab(cur, project)
    at = _op_time(op)
    wanted = dict(fields)
    if "state" in wanted:
        wanted["state"] = _word(vocab, "state", wanted["state"])
    if op.get("if_state") is not None:
        expected = _word(vocab, "state", op["if_state"])
        if row["state"] != expected:
            if not late:
                raise LedgerError(f"row {row_id} is {row['state']} now, set by {_last_setter(cur, project, row_id)}; "
                                  "nothing was changed", "moved")
            # A queued write whose condition no longer holds is still kept, as
            # history; the row is not changed.
            requested = {k: {"old": row[k], "new": v} for k, v in wanted.items() if v != row[k]}
            note = f"arrived late; the row had moved to {row['state']}"
            if op["note"]:
                note += f". {op['note']}"
            _history(cur, op, row_id, from_state=row["state"], to_state=row["state"],
                     changes={"requested": requested}, note=note, at=at)
            return {"ok": True, "verb": "set", "id": row_id, "late": True, "state": row["state"],
                    "line": f"set {row_id}: arrived late; the row had moved to {row['state']}", "affected": [row_id]}
    changes = {k: {"old": row[k], "new": wanted[k]} for k in _SET_FIELDS if k in wanted and wanted[k] != row[k]}
    if not changes and not op["note"]:
        return {"ok": True, "verb": "set", "id": row_id, "state": row["state"], "line": f"set {row_id}: no change",
                "affected": []}
    new_state = changes["state"]["new"] if "state" in changes else row["state"]
    _history(cur, op, row_id, from_state=row["state"], to_state=new_state, changes=changes, note=op["note"], at=at)
    assignments = [f"{k} = %s" for k in changes] + ["updated_at = %s", "version = version + 1"]
    cur.execute(f"UPDATE ledger_rows SET {', '.join(assignments)} WHERE project = %s AND row_id = %s",
                [changes[k]["new"] for k in changes] + [at, project, row_id])
    named = [k for k in _SET_FIELDS if k in changes]
    return {"ok": True, "verb": "set", "id": row_id, "state": new_state, "changed": named,
            "line": f"set {row_id}: {', '.join(named) if named else 'note appended'}", "affected": [row_id]}


def _children(cur, project: str, row_id: str) -> list[dict[str, Any]]:
    cur.execute(f"SELECT {', '.join(_ROW_COLS)} FROM ledger_rows WHERE project = %s AND parent = %s "
                "AND removed_at IS NULL ORDER BY seq FOR UPDATE", (project, row_id))
    return [_row_dict(r) for r in cur.fetchall()]


def _mark_removed(cur, op: dict[str, Any], row: dict[str, Any], note: str, at: datetime) -> None:
    from psycopg.types.json import Jsonb

    cur.execute("UPDATE ledger_rows SET removed_at = %s, removed_by = %s, removed_note = %s, "
                "version = version + 1 WHERE project = %s AND row_id = %s",
                (at, Jsonb(op["author"]), op["note"], op["project"], row["row_id"]))
    _history(cur, op, row["row_id"], from_state=row["state"], to_state=row["state"],
             changes={"removed": {"old": None, "new": _z(at)}}, note=note, at=at)


def _remove(cur, op: dict[str, Any]) -> dict[str, Any]:
    project, row_id = op["project"], op["row_id"]
    row = _live_row(cur, project, row_id)
    children = _children(cur, project, row_id)
    choice = op.get("children")
    if children and choice not in ("remove", "move-up"):
        raise LedgerError(f"row {row_id} has {len(children)} child row(s); say --children remove to remove them "
                          "too, or --children move-up to move them to its parent", "has_children")
    at = _op_time(op)
    _mark_removed(cur, op, row, "Removed" + (f": {op['note']}" if op["note"] else ""), at)
    affected = [row_id]
    if choice == "remove":
        queue, seen = list(children), {row_id}
        while queue:
            child = queue.pop(0)
            if child["row_id"] in seen:
                continue
            seen.add(child["row_id"])
            _mark_removed(cur, op, child, f"Removed with its parent {row_id}", at)
            affected.append(child["row_id"])
            queue.extend(_children(cur, project, child["row_id"]))
        line = f"removed {row_id} and {len(affected) - 1} child row(s)" if children else f"removed {row_id}"
    else:
        for child in children:
            _history(cur, op, child["row_id"], from_state=child["state"], to_state=child["state"],
                     changes={"parent": {"old": row_id, "new": row["parent"]}},
                     note=f"Moved up: its parent {row_id} was removed", at=at)
            cur.execute("UPDATE ledger_rows SET parent = %s, updated_at = %s, version = version + 1 "
                        "WHERE project = %s AND row_id = %s", (row["parent"], at, project, child["row_id"]))
            affected.append(child["row_id"])
        line = f"removed {row_id}"
        if children:
            line += f"; {len(children)} child row(s) moved up to {row['parent'] or 'the top'}"
    return {"ok": True, "verb": "remove", "id": row_id, "line": line, "affected": affected}


def _restore(cur, op: dict[str, Any]) -> dict[str, Any]:
    project, row_id = op["project"], op["row_id"]
    row = _lock_row(cur, project, row_id)
    if row is None:
        marker = _marker(cur, project, row_id)
        if marker:
            raise _purged_error(row_id, marker)
        raise LedgerError(f"no such row: {row_id}", "not_found")
    if row["removed_at"] is None:
        raise LedgerError(f"row {row_id} is not removed", "not_removed")
    at = _op_time(op)
    cur.execute("UPDATE ledger_rows SET removed_at = NULL, removed_by = NULL, removed_note = '', "
                "updated_at = %s, version = version + 1 WHERE project = %s AND row_id = %s", (at, project, row_id))
    _history(cur, op, row_id, from_state=row["state"], to_state=row["state"],
             changes={"removed": {"old": _z(row["removed_at"]), "new": None}}, note="Restored by you, in the app",
             at=at)
    return {"ok": True, "verb": "restore", "id": row_id, "line": f"restored {row_id}", "affected": [row_id]}


def _purge_row(cur, project: str, row_id: str) -> bool:
    """Remove a removed row for good: its row, history and ops go, and the bare
    marker (project, id, who removed it, when) stays. The only DELETEs in this
    module are here."""
    cur.execute("SELECT removed_by, removed_at FROM ledger_rows WHERE project = %s AND row_id = %s "
                "AND removed_at IS NOT NULL FOR UPDATE", (project, row_id))
    got = cur.fetchone()
    if got is None:
        return False
    from psycopg.types.json import Jsonb

    cur.execute("INSERT INTO ledger_removed (project, row_id, removed_by, removed_at, purged_at) "
                "VALUES (%s, %s, %s, %s, now()) ON CONFLICT (project, row_id) DO UPDATE SET "
                "removed_by = EXCLUDED.removed_by, removed_at = EXCLUDED.removed_at, purged_at = EXCLUDED.purged_at",
                (project, row_id, Jsonb(got[0]), got[1]))
    cur.execute("DELETE FROM ledger_history WHERE project = %s AND row_id = %s", (project, row_id))
    # An op id is bound to its write for good: the binding stays, and only
    # how the write ended is kept of its result.
    cur.execute("UPDATE ledger_ops SET result = jsonb_strip_nulls(jsonb_build_object('ok', result->'ok', "
                "'reason_word', result->'reason_word', 'purged', true)) WHERE project = %s AND row_id = %s",
                (project, row_id))
    cur.execute("DELETE FROM ledger_rows WHERE project = %s AND row_id = %s AND removed_at IS NOT NULL",
                (project, row_id))
    return True


_VERBS = {"add": _add, "set": _set, "remove": _remove, "restore": _restore}


def _refusal(stored: dict[str, Any], op_id: str) -> LedgerError:
    return LedgerError(str(stored.get("refused") or f"op id {op_id} was refused when it was first sent; "
                                                     "nothing was changed"),
                       str(stored.get("reason_word") or "refused"))


def _bound_result(cur, op: dict[str, Any], digest: str) -> dict[str, Any]:
    """The answer for an op id the hub already holds: its first result, or
    its first refusal raised again. Another write under the id is refused."""
    cur.execute("SELECT project, row_id, verb, digest, result FROM ledger_ops WHERE op_id = %s", (op["op_id"],))
    got = cur.fetchone()
    if got is None or tuple(got[:3]) != (op["project"], op["row_id"], op["verb"]) or got[3] != digest:
        raise _reused_error(op)
    marker = _marker(cur, op["project"], op["row_id"])
    if marker:
        raise _purged_error(op["row_id"], marker)
    stored = dict(got[4] or {})
    if stored.get("ok") is False:
        raise _refusal(stored, op["op_id"])
    return stored


def apply_op(conn, op: dict[str, Any], *, late: bool = False) -> dict[str, Any]:
    """Apply one op in one transaction. The op id is bound to this write in
    the same transaction and never released: a retry gets the result, or the
    refusal, it first had, and a refused op leaves only that binding behind."""
    from psycopg.types.json import Jsonb

    digest = op_digest(op)
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO ledger_ops (op_id, project, row_id, verb, digest) VALUES (%s, %s, %s, %s, %s) "
                        "ON CONFLICT (op_id) DO NOTHING RETURNING op_id",
                        (op["op_id"], op["project"], op["row_id"], op["verb"], digest))
            if cur.fetchone() is None:
                result = _bound_result(cur, op, digest)
                conn.rollback()
                # The first answer may have come from a process that ended
                # before the offline copy heard of it: a retry repairs that.
                _replica_sync(conn, op["project"], result.get("affected") or [])
                return {**result, "retried": True}
            cur.execute("SAVEPOINT ledger_op")
            verb = _VERBS[op["verb"]]
            try:
                result = verb(cur, op, late=late) if op["verb"] == "set" else verb(cur, op)
            except LedgerError as exc:
                cur.execute("ROLLBACK TO SAVEPOINT ledger_op")
                cur.execute("UPDATE ledger_ops SET result = %s WHERE op_id = %s",
                            (Jsonb({"ok": False, "refused": str(exc), "reason_word": exc.reason}), op["op_id"]))
                conn.commit()
                raise
            cur.execute("UPDATE ledger_ops SET result = %s WHERE op_id = %s", (Jsonb(result), op["op_id"]))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    _replica_sync(conn, op["project"], result.get("affected") or [])
    return result


def purge(conn, project: str, row_id: str) -> dict[str, Any]:
    """Remove for good at once (the owner's choice in the app)."""
    refuse_secrets(project, row_id)
    try:
        with conn.cursor() as cur:
            if not _purge_row(cur, project, row_id):
                marker = _marker(cur, project, row_id)
                if marker:
                    raise _purged_error(row_id, marker)
                if _lock_row(cur, project, row_id) is None:
                    raise LedgerError(f"no such row: {row_id}", "not_found")
                raise LedgerError(f"row {row_id} is not removed; remove it first", "not_removed")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    _replica_sync(conn, project, [row_id])
    return {"ok": True, "verb": "purge", "id": row_id, "line": f"removed {row_id} for good"}


def purge_due(conn, *, now: datetime | None = None) -> dict[str, Any]:
    """Purge every row removed longer ago than ``ledger_purge_after`` (the
    nightly). 0 means removed rows are kept until the owner purges them."""
    after = settings()["ledger_purge_after"]
    if not after:
        return {"ok": True, "purged": 0, "reason": "ledger_purge_after is 0"}
    cutoff = (now or _now()) - timedelta(seconds=after)
    with conn.cursor() as cur:
        cur.execute("SELECT project, row_id FROM ledger_rows WHERE removed_at IS NOT NULL AND removed_at < %s "
                    "ORDER BY removed_at", (cutoff,))
        due = cur.fetchall()
    conn.rollback()
    purged = 0
    for project, row_id in due:
        try:
            with conn.cursor() as cur:
                purged += int(_purge_row(cur, project, row_id))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        _replica_sync(conn, project, [row_id])
    return {"ok": True, "purged": purged}


# ---- the offline copy ------------------------------------------------------------


def _replica_pending_path() -> Path:
    from khipu.paths import data_dir

    return data_dir() / "ledger-replica-pending.json"


@contextmanager
def _replica_pending():
    """The ids whose change has not reached the offline copy yet, as
    ``{"<project>#<row id>": token}``. Each change writes a fresh random token,
    never a count: a count restarts once its entry is cleared, so a catch-up
    that read the old entry could clear a newer change's. A catch-up clears an
    entry only while it still holds the token it read. Read and written under a lock."""
    from khipu.outbox import _atomic_write

    path = _replica_pending_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path.with_suffix(".lock")), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            pending = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pending = {}
        if not isinstance(pending, dict):
            pending = {}
        before = dict(pending)
        yield pending
        if pending != before:
            _atomic_write(path, pending)
    finally:
        os.close(fd)


def replica_pending() -> int:
    try:
        with _replica_pending() as pending:
            return len(pending)
    except OSError:
        return 0


def _replica_sync(conn, project: str, row_ids: list[str]) -> None:
    """Write a landed change through to the offline copy, so a removed row is
    gone from it and a new one is in it. The ids are written down first and
    cleared only once copied: a copy skipped because a refresh holds the file,
    or failed, is retried by the next ledger call that reaches the hub."""
    if not row_ids:
        return
    try:
        with _replica_pending() as pending:
            for row_id in row_ids:
                key = f"{project}#{row_id}"
                pending[key] = uuid.uuid4().hex
    except Exception as exc:  # noqa: BLE001  (the hub write already landed)
        print(f"[khipu-ledger] offline copy not updated: {type(exc).__name__}: {exc}", file=sys.stderr)
        return
    replica_catch_up(conn)


def replica_catch_up(conn) -> dict[str, Any]:
    """Copy every id still waiting for the offline copy from the hub as it is
    now. Safe to repeat: the copy keeps the newest version of each row.
    Fail-open."""
    out: dict[str, Any] = {"copied": 0, "left": 0}
    try:
        from khipu import hub_snapshot

        with _replica_pending() as pending:
            seen = dict(pending)
        if not seen:
            return out
        by_project: dict[str, list[str]] = {}
        for key in seen:
            project, _sep, row_id = key.partition("#")
            by_project.setdefault(project, []).append(row_id)
        done: list[str] = []
        for project, ids in by_project.items():
            with conn.cursor() as cur:
                vocab = _vocab(cur, project)
                cur.execute("SELECT name, hidden, created_at FROM ledger_projects WHERE project = %s", (project,))
                proj = cur.fetchone()
                cur.execute(f"SELECT {', '.join(_ROW_COLS)} FROM ledger_rows WHERE project = %s "
                            "AND row_id = ANY(%s)", (project, ids))
                found = [_row_dict(r) for r in cur.fetchall()]
                rows = [r for r in found if r["removed_at"] is None]
                gone = {r["row_id"]: r["version"] for r in found if r["removed_at"] is not None}
                cur.execute(f"SELECT {', '.join(_HIST_COLS)} FROM ledger_history WHERE project = %s "
                            "AND row_id = ANY(%s) ORDER BY id", (project, [r["row_id"] for r in rows]))
                history = [dict(zip(_HIST_COLS, h, strict=True)) for h in cur.fetchall()]
            conn.rollback()
            got = hub_snapshot.ledger_write_snapshot(project, proj, vocab, rows, history, ids, gone=gone)
            # No offline copy and no refresh dumping one: the next refresh
            # starts after this change and copies everything.
            if got.get("ok") or got.get("missing"):
                done += [f"{project}#{i}" for i in ids]
            else:
                print(f"[khipu-ledger] offline copy not updated yet ({got.get('error')}); "
                      "the next ledger call retries it", file=sys.stderr)
        with _replica_pending() as pending:
            for key in done:
                if pending.get(key) == seen[key]:
                    del pending[key]
            out["left"] = len(pending)
        out["copied"] = len(done)
    except Exception as exc:  # noqa: BLE001  (the hub write already landed)
        print(f"[khipu-ledger] offline copy not updated: {type(exc).__name__}: {exc}", file=sys.stderr)
        out["error"] = f"{type(exc).__name__}: {exc}"[:200]
    return out


def catch_up_offline_copy() -> dict[str, Any]:
    """The Stop hook's step: copy ids still waiting for the offline copy."""
    if not replica_pending():
        return {"copied": 0, "left": 0}
    try:
        conn = _connect()
    except HubAway:
        return {"copied": 0, "left": replica_pending(), "away": True}
    try:
        return replica_catch_up(conn)
    finally:
        conn.close()


# ---- the outbox ----------------------------------------------------------------


def outbox_dir() -> Path:
    from khipu.paths import data_dir

    return data_dir() / "ledger-outbox"


def _refused_dir() -> Path:
    return outbox_dir() / "refused"


def _outcomes_dir() -> Path:
    return outbox_dir() / "outcomes"


OUTCOME_KEEP_S = 7 * 86400


_op_lock_held = threading.local()


@contextmanager
def _op_lock():
    """The one lock under which this Mac decides whether an op id is taken
    and where a write stands in line: enqueueing, the direct-write decision,
    recording an outcome and a job leaving the line all happen under it, so no
    check can be overtaken between its look and its act. Taken after the
    drain lock, never before it. Re-entrant within a thread."""
    if getattr(_op_lock_held, "depth", 0):
        _op_lock_held.depth += 1
        try:
            yield
        finally:
            _op_lock_held.depth -= 1
        return
    d = outbox_dir()
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(d / ".enqueue.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _op_lock_held.depth = 1
        try:
            yield
        finally:
            _op_lock_held.depth = 0
    finally:
        os.close(fd)


def _record_outcome(op: dict[str, Any], status: str, **detail: Any) -> None:
    """What became of a job, written before the job leaves the line: every
    writer waiting on its op id reads this, and nothing else, to answer. A job
    gone with no outcome is never called landed. Append-only: the first
    outcome for an op id stands, and one under another write's digest is
    refused as a reused id."""
    from khipu.outbox import _atomic_write

    with _op_lock():
        known = _outcome(op["op_id"])
        if known is not None:
            if known.get("digest") != op_digest(op):
                raise _reused_error(op)
            return
        d = _outcomes_dir()
        d.mkdir(parents=True, exist_ok=True)
        _atomic_write(d / f"{op['op_id']}.json", {"op_id": op["op_id"], "digest": op_digest(op), "status": status,
                                                  "at": _z(_now()), **detail})


def _outcome(op_id: str) -> dict[str, Any] | None:
    try:
        got = json.loads((_outcomes_dir() / f"{op_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return got if isinstance(got, dict) else None


def _prune_outcomes() -> None:
    """After ``OUTCOME_KEEP_S`` an outcome keeps only its op id, digest and how
    it ended, so the id still stands for that one write here and a refusal is
    still a refusal; its result and wording are dropped. Never deleted."""
    from khipu.outbox import _atomic_write

    cutoff = time.time() - OUTCOME_KEEP_S
    try:
        with _op_lock():
            for path in _outcomes_dir().glob("*.json"):
                if path.stat().st_mtime >= cutoff:
                    continue
                got = _outcome(path.stem)
                if got is None or got.get("pruned"):
                    continue
                kept = {"op_id": got.get("op_id"), "digest": got.get("digest"), "status": got.get("status"),
                        "reason_word": got.get("reason_word"), "pruned": True}
                _atomic_write(path, {k: v for k, v in kept.items() if v is not None})
    except OSError:
        pass


def _outbox_jobs() -> list[Path]:
    try:
        return sorted(p for p in outbox_dir().glob("*.json") if p.is_file())
    except OSError:
        return []


def _job_path(op_id: str) -> Path | None:
    """The waiting job for exactly ``op_id``. An op id may hold "-", so a name
    is split at its first "-" only, never matched by suffix."""
    for path in _outbox_jobs():
        if path.name.split("-", 1)[-1] == f"{op_id}.json":
            return path
    return None


def queued_ops() -> list[dict[str, Any]]:
    out = []
    for path in _outbox_jobs():
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(job, dict) and isinstance(job.get("op"), dict):
            out.append(job["op"])
    return out


def enqueue(op: dict[str, Any], reason: str = "") -> Path:
    """Put ``op`` at the back of the line. A job's name is its place: after
    every waiting job's, under a lock, so a clock that steps back or another
    process enqueueing at the same moment cannot put a later write first."""
    from khipu.outbox import _atomic_write

    with _op_lock():
        claim = _claim(op)
        if claim is not None and claim[0] == "queued":
            return claim[1]
        if claim is not None and claim[0] == "refused":
            raise _refusal(claim[1], op["op_id"])
        last = 0
        for path in _outbox_jobs():
            head = path.name.split("-", 1)[0]
            if head.isdigit():
                last = max(last, int(head))
        dst = outbox_dir() / f"{max(time.time_ns(), last + 1):020d}-{op['op_id']}.json"
        _atomic_write(dst, {"op": op, "reason": reason[:200], "queued_at": _z(_now())})
        return dst


def _claim(op: dict[str, Any]) -> tuple[str, Any] | None:
    """What this Mac holds for ``op``'s op id, read under the op lock:
    ("queued", job path), or (status, outcome) for an outcome, or None when
    the id is free here. Raises op_id_reused when the id holds another write.
    A job that leaves the line between the look and the read is answered by
    the outcome written before it left."""
    if not getattr(_op_lock_held, "depth", 0):
        raise RuntimeError("_claim needs the op lock")
    digest = op_digest(op)
    path = _job_path(op["op_id"])
    if path is not None:
        try:
            held = json.loads(path.read_text(encoding="utf-8"))["op"]
        except FileNotFoundError:
            path = None
        except (OSError, ValueError, KeyError, TypeError):
            held = None
        if path is not None:
            if not isinstance(held, dict) or op_digest(held) != digest:
                raise _reused_error(op)
            return "queued", path
    known = _outcome(op["op_id"])
    if known is None:
        return None
    if known.get("digest") != digest:
        raise _reused_error(op)
    return str(known.get("status") or ""), known


def outbox_status() -> dict[str, Any]:
    jobs = _outbox_jobs()
    refused = sorted(_refused_dir().glob("*.json")) if _refused_dir().is_dir() else []
    oldest = None
    for path in jobs:
        try:
            age = int(time.time() - path.stat().st_mtime)
        except OSError:
            continue
        oldest = age if oldest is None else max(oldest, age)
    return {"pending": len(jobs), "refused": len(refused), "oldest_age_s": oldest}


def _drain_result() -> dict[str, Any]:
    return {"jobs": 0, "landed": 0, "refused": 0, "stopped_early": False}


@contextmanager
def _drain_lock(wait_s: float = 0.0):
    """The one lock that orders writes on this Mac: a drain holds it, and so
    does a write while waiting writes go ahead of it. Yields False when it
    could not be had within ``wait_s``."""
    d = outbox_dir()
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(d / ".drain.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + wait_s
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.05)
        yield True
    finally:
        os.close(fd)


def drain(conn=None) -> dict[str, Any]:
    """Send every waiting write to the hub, oldest first. A write the ledger
    refuses now (its row was removed meanwhile, say) moves to ``refused/`` and
    keeps doctor red; nothing is dropped. Stops at the first sign the hub is
    away and keeps the rest."""
    out = _drain_result()
    if not _outbox_jobs():
        return out
    with _drain_lock() as got:
        if not got:
            return {**out, "busy": True}
        return _drain_locked(conn, out)


def _drain_locked(conn, out: dict[str, Any], waiting: frozenset[str] = frozenset()) -> dict[str, Any]:
    """``waiting`` names the op this process's writer is waiting on: it lands
    as a fresh write would (an --if-state that no longer holds is refused, and
    the writer is told). Every other job is a queued write, whoever drains it."""
    owned = conn is None
    try:
        _prune_outcomes()
        jobs = _outbox_jobs()
        out["jobs"] = len(jobs)
        if not jobs:
            return out
        if owned:
            try:
                conn = _connect()
            except HubAway:
                return {**out, "stopped_early": True}
        for path in jobs:
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                op = job["op"]
            except (OSError, ValueError, KeyError, TypeError) as exc:
                _refuse_job(path, {"unreadable": type(exc).__name__})
                out["refused"] += 1
                continue
            try:
                result = apply_op(conn, op, late=op.get("op_id") not in waiting)
            except LedgerError as exc:
                _finish_job(path, job, "refused", refused=str(exc), reason_word=exc.reason)
                out["refused"] += 1
                continue
            except Exception as exc:  # noqa: BLE001
                # The hub away, its tables not migrated yet, or a passing
                # database fault: the job keeps its place and the line waits.
                if _away(exc) or _tables_missing(exc) or _transient(exc):
                    out["stopped_early"] = True
                    break
                # Anything else would fail every time and hold the line for
                # good: it is set aside as refused, kept, and doctor goes red.
                failed = f"this write failed: {type(exc).__name__}"[:200]
                _finish_job(path, job, "refused", refused=failed, reason_word="failed")
                out["refused"] += 1
                continue
            if _finish_job(path, job, "landed", result=result):
                out["landed"] += 1
            else:
                out["refused"] += 1
        return out
    finally:
        if owned and conn is not None:
            conn.close()


def _transient(exc: BaseException) -> bool:
    try:
        import psycopg

        return isinstance(exc, psycopg.OperationalError)
    except ImportError:
        return False


def _finish_job(path: Path, job: dict[str, Any], status: str, **detail: Any) -> bool:
    """Record what became of a job and take it out of the line, as one step
    under the op lock: a writer looking at its op id sees the job or its
    outcome, never neither. False when the outcome could not be recorded
    because the id holds another write here; the job is then kept as refused."""
    with _op_lock():
        try:
            _record_outcome(job["op"], status, **detail)
        except LedgerError as exc:
            _refuse_job(path, {**job, "refused": str(exc), "reason_word": exc.reason})
            return False
        if status == "refused":
            _refuse_job(path, {**job, "refused": detail.get("refused"), "reason_word": detail.get("reason_word")})
        else:
            path.unlink(missing_ok=True)
        return status != "refused"


def _refuse_job(path: Path, job: dict[str, Any]) -> None:
    from khipu.outbox import _atomic_write

    dest = _refused_dir()
    dest.mkdir(parents=True, exist_ok=True)
    _atomic_write(dest / path.name, job)
    path.unlink(missing_ok=True)


# ---- writing ------------------------------------------------------------------


def write(op: dict[str, Any], *, conn=None) -> dict[str, Any]:
    """Check, then apply ``op`` on the hub; queue it when the hub is away.
    Waiting writes go first, so writes land in the order they were made."""
    _check_op(op, settings())
    op_digest(op)
    owned = conn is None
    try:
        if owned:
            conn = _connect()
        with _drain_lock() as got:
            path = _reserve_direct(op) if got else None
            if path is not None:
                return _apply_own_job(conn, op, path)
        return _write_in_line(conn, op)
    except LedgerError:
        raise
    except Exception as exc:  # noqa: BLE001
        if _tables_missing(exc):
            raise LedgerError("the ledger tables are not in the database yet; run khipu migrate", "no_tables") from exc
        if not _away(exc):
            raise
        reason = type(exc).__name__
    finally:
        if owned and conn is not None:
            conn.close()
    return _queue_while_away(op, reason)


def _queue_while_away(op: dict[str, Any], reason: str) -> dict[str, Any]:
    """The hub is away: answer a retry as the write it repeats, else check
    ``op`` against the offline copy and the line and join the line, all under
    the op lock so no other write changes the line between check and place."""
    with _op_lock():
        # A retry of a write already in line, or already landed, is answered
        # as that write; it is not checked again against the line it is part of.
        held = _known_op(op)
        if held is not None:
            return held
        _check_offline(op)
        enqueue(op, reason=reason)
    return _queued_reply(op)


def _known_op(op: dict[str, Any]) -> dict[str, Any] | None:
    """The answer for an op id this Mac already holds: queued, landed with its
    first result, or refused again. Another write under the id is refused."""
    with _op_lock():
        claim = _claim(op)
    if claim is None:
        return None
    status, held = claim
    if status == "queued":
        return _queued_reply(op)
    if status == "refused":
        raise _refusal(held, op["op_id"])
    if status == "landed" and isinstance(held.get("result"), dict):
        result = dict(held["result"])
        result.pop("retried", None)
        return result
    if status == "landed":
        # Landed long ago: the result was pruned, how it ended was kept.
        return {"ok": True, "verb": op["verb"], "id": op["row_id"], "retried": True,
                "line": f"{op['verb']} {op['row_id']}: landed earlier"}
    return None


def _reserve_direct(op: dict[str, Any]) -> Path | None:
    """With the drain lock held: when the line is empty, put ``op`` in it and
    return its job, to be sent at once by this writer. The look at the line,
    the op id check and taking the place are one step under the op lock, so
    a write queued by another process is either seen here first or stands
    behind this one, and keeps standing behind it if the hub goes away
    mid-write. None when other writes wait: this one joins them instead."""
    with _op_lock():
        claim = _claim(op)
        if claim is not None and claim[0] == "refused":
            raise _refusal(claim[1], op["op_id"])
        if (claim is not None and claim[0] == "queued") or _outbox_jobs():
            return None
        return enqueue(op, reason="sent at once")


def _apply_own_job(conn, op: dict[str, Any], path: Path) -> dict[str, Any]:
    """Send the job ``_reserve_direct`` placed, with the drain lock still held,
    and answer as a direct write: its result, its refusal, or queued when the
    hub went away (the job keeps its place at the front of the line)."""
    job = {"op": op}
    try:
        result = apply_op(conn, op)
    except LedgerError as exc:
        if exc.reason == "op_id_reused":
            _drop_job(path)  # the hub holds another write under this id; nothing is ours to keep
        else:
            _finish_job(path, job, "refused", refused=str(exc), reason_word=exc.reason)
            (_refused_dir() / path.name).unlink(missing_ok=True)  # its writer is told here
        raise
    except Exception as exc:  # noqa: BLE001
        if not _tables_missing(exc) and _away(exc):
            return _queued_reply(op)
        _drop_job(path)  # nothing landed; the caller is told as before
        raise
    _finish_job(path, job, "landed", result=result)
    return result


def _drop_job(path: Path) -> None:
    with _op_lock():
        path.unlink(missing_ok=True)


def _write_in_line(conn, op: dict[str, Any]) -> dict[str, Any]:
    """Writes are waiting on this Mac, or another process is draining them:
    this one joins the line at once (the outbox is the line, oldest first), so
    a write made later can never land before it. It then drains the line
    itself when it can, and answers as a direct write would."""
    path = enqueue(op, reason="behind waiting writes")
    deadline = time.monotonic() + DRAIN_WAIT_S
    # From here the job is in the line: the answer can only be what became of
    # it (landed, refused, or still queued), never a fresh offline check.
    try:
        while path.exists():
            with _drain_lock() as got:
                if got:
                    out = _drain_locked(conn, _drain_result(), frozenset({op["op_id"]}))
                    if out["stopped_early"]:
                        break
                    continue
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    except Exception as exc:  # noqa: BLE001  (the job is kept; it is still queued)
        print(f"[khipu-ledger] a waiting write stays queued: {type(exc).__name__}", file=sys.stderr)
    if path.exists():
        return _queued_reply(op)
    # Gone from the line: the outcome its drain wrote first is the answer,
    # shared by every writer of this op id and never consumed by one of them.
    known = _outcome(op["op_id"])
    if known is not None and known.get("digest") == op_digest(op):
        if known.get("status") == "landed":
            return _known_op(op) or {}
        if known.get("status") == "refused":
            # Its writer is told here, so it is not also kept as a refused job.
            (_refused_dir() / path.name).unlink(missing_ok=True)
            raise LedgerError(str(known.get("refused") or "the ledger refused this write"),
                              str(known.get("reason_word") or "refused"))
    # No outcome on this Mac: only the hub's own record of the op is evidence
    # that it landed. It is read, never applied again.
    try:
        landed = _landed_result(conn, op)
    except LedgerError:
        raise
    except Exception:  # noqa: BLE001
        landed = None
    if landed is not None:
        return landed
    raise LedgerError(f"{op['verb']} {op['row_id']}: left the line on this Mac but whether it landed cannot be "
                      "confirmed now; check the row before writing it again", "unconfirmed")


def _landed_result(conn, op: dict[str, Any]) -> dict[str, Any] | None:
    """The first result of exactly this op, when the hub recorded one."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT project, row_id, verb, digest, result FROM ledger_ops WHERE op_id = %s",
                        (op["op_id"],))
            got = cur.fetchone()
            marker = _marker(cur, op["project"], op["row_id"]) if got is not None else None
    finally:
        conn.rollback()
    if got is None or tuple(got[:3]) != (op["project"], op["row_id"], op["verb"]) or got[3] != op_digest(op):
        return None
    if marker:
        raise _purged_error(op["row_id"], marker)
    stored = dict(got[4] or {})
    if stored.get("ok") is False:
        raise _refusal(stored, op["op_id"])
    return stored


def _queued_reply(op: dict[str, Any]) -> dict[str, Any]:
    state = op["fields"].get("state") or (START_STATE if op["verb"] == "add" else "")
    lines = {"add": f"added {op['row_id']}: {state}", "set": f"set {op['row_id']}: queued",
             "remove": f"removed {op['row_id']}: queued"}
    return {"ok": True, "verb": op["verb"], "id": op["row_id"], "queued": True, "state": state or None,
            "line": lines.get(op["verb"], f"{op['verb']} {op['row_id']}: queued"), "words": QUEUED_WORDS}


def _check_offline(op: dict[str, Any]) -> None:
    """What can be checked against the offline copy before queueing: the
    project's words, and whether the row exists once the writes already
    waiting are applied (a row added while away can be changed while away)."""
    project = op["project"]
    view = _offline_view(project)
    queued = [q for q in queued_ops() if q.get("project") == project]
    if view is None and not queued:
        return
    vocab = view["vocab"] if view else {"state": [], "kind": []}
    fields = op["fields"]
    if vocab["state"]:
        if fields.get("state"):
            fields["state"] = _word(vocab, "state", fields["state"])
        if fields.get("kind"):
            fields["kind"] = _word(vocab, "kind", fields["kind"])
    row_id = op["row_id"]
    last = [q.get("verb") for q in queued if q.get("row_id") == row_id and q.get("verb") in ("remove", "restore")]
    restored_in_queue = bool(last) and last[-1] == "restore"
    adds = [q.get("verb") for q in queued if q.get("row_id") == row_id and q.get("verb") in ("add", "remove")]
    added_in_queue = bool(adds) and adds[-1] == "add"
    # The waiting writes are applied to the whole copy, so what they establish
    # (a cascade removing children, a move up) is known for every row.
    merged = _overlay(view or {"vocab": vocab, "rows": [], "as_of": None, "offline": True}, project)
    # Removed by a waiting write, itself or with its parent: definite.
    removed_in_queue = row_id in merged["queue_removed"] or (bool(last) and last[-1] == "remove")
    # A change that landed while the copy was busy is not in it yet: what the
    # copy alone says about such a row (there, gone, its children) is left to
    # the hub; only what the waiting writes establish is checked for it.
    unsure = {k.partition("#")[2] for k in _pending_keys() if k.partition("#")[0] == project}
    rows = {r["row_id"]: r for r in merged["rows"]
            if r["row_id"] not in unsure or r["row_id"] in merged["queue_added"] | merged["queue_restored"]}
    gone = {k: v for k, v in ((view or {}).get("gone") or {}).items() if k not in unsure}
    # Such a row may exist on the hub though the copy lacks it: whether it
    # exists is left to the hub, but the children the copy knows still count.
    hub_decides = row_id not in rows and (restored_in_queue or row_id in unsure) and not removed_in_queue
    if hub_decides and op["verb"] == "add" and (restored_in_queue or added_in_queue):
        raise LedgerError(f"row {row_id} already exists; use set to update it", "exists")
    if hub_decides and op["verb"] == "add":
        return
    if op["verb"] == "add":
        if row_id in rows:
            raise LedgerError(f"row {row_id} already exists; use set to update it", "exists")
        if (row_id in gone and not restored_in_queue) or removed_in_queue:
            if gone.get(row_id, 0) >= _purged_version():
                raise LedgerError(f"row {row_id} was removed for good", "purged")
            raise LedgerError(f"row {row_id} was removed; it can be restored in the app", "removed")
        return
    # Without the project in the offline copy, a row missing here may still be
    # on the hub; only a row removed by a waiting write is known to be gone.
    known = bool(view and view["rows"]) or removed_in_queue or (row_id in gone and not restored_in_queue)
    if op["verb"] in ("set", "remove") and known and row_id not in rows and not hub_decides:
        raise LedgerError(f"no such row: {row_id}", "not_found")
    if op["verb"] == "remove" and op.get("children") not in ("remove", "move-up"):
        children = [r for r in rows.values() if r["parent"] == row_id]
        if children:
            raise LedgerError(f"row {row_id} has {len(children)} child row(s); say --children remove to remove "
                              "them too, or --children move-up to move them to its parent", "has_children")


def _purged_version() -> int:
    from khipu.hub_snapshot import LEDGER_PURGED_VERSION

    return LEDGER_PURGED_VERSION


def _pending_keys() -> set[str]:
    try:
        with _replica_pending() as pending:
            return set(pending)
    except OSError:
        return set()


# ---- reading ------------------------------------------------------------------


def _hub_view(conn, project: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        vocab = _vocab(cur, project)
        cur.execute(f"SELECT {', '.join(_ROW_COLS)} FROM ledger_rows WHERE project = %s AND removed_at IS NULL "
                    "ORDER BY seq", (project,))
        rows = [_row_dict(r) for r in cur.fetchall()]
    conn.rollback()
    return {"vocab": vocab, "rows": rows, "as_of": None, "offline": False}


def _offline_view(project: str) -> dict[str, Any] | None:
    from khipu import hub_snapshot

    got = hub_snapshot.ledger_read_snapshot(project)
    if got is None:
        return None
    rows = [{**r, "created_at": _parse_time(r["created_at"]), "updated_at": _parse_time(r["updated_at"]),
             "refs": json.loads(r["refs"] or "{}") if isinstance(r["refs"], str) else (r["refs"] or {}),
             "removed_at": None, "removed_by": None, "removed_note": ""} for r in got["rows"]]
    return {"vocab": got["vocab"], "rows": rows, "history": got["history"], "gone": got.get("gone") or {},
            "as_of": got.get("as_of"), "offline": True}


def _overlay(view: dict[str, Any], project: str) -> dict[str, Any]:
    """Apply the waiting writes to an offline view, in the order they were made."""
    rows = {r["row_id"]: r for r in view["rows"]}
    removed: dict[str, dict[str, Any]] = {}
    restored: set[str] = set()
    added: set[str] = set()
    vocab = view["vocab"] if view["vocab"]["state"] else default_vocab()
    pending = 0
    for op in queued_ops():
        if op.get("project") != project:
            continue
        pending += 1
        fields, row_id, at = op.get("fields") or {}, op.get("row_id"), _parse_time(op.get("at")) or _now()
        try:
            if op["verb"] == "add" and row_id not in rows:
                added.add(row_id)
                rows[row_id] = {"row_id": row_id, "title": fields.get("title", ""), "kind": fields.get("kind", ""),
                                "parent": fields.get("parent", ""),
                                "state": _word(vocab, "state", fields.get("state")) or START_STATE,
                                "owner": fields.get("owner") or "none", "evidence": fields.get("evidence", ""),
                                "link": fields.get("link", ""), "refs": {},
                                "seq": max([r["seq"] for r in rows.values()] + [0]) + 1,
                                "created_at": at, "updated_at": at, "version": 1, "queued": True}
            elif op["verb"] == "set" and row_id in rows:
                row = rows[row_id]
                if op.get("if_state") and row["state"] != _word(vocab, "state", op["if_state"]):
                    continue
                for key in _SET_FIELDS:
                    if key in fields:
                        row[key] = _word(vocab, "state", fields[key]) if key == "state" else fields[key]
                row["updated_at"], row["queued"] = at, True
            elif op["verb"] == "restore" and row_id in removed and row_id not in rows:
                rows[row_id] = removed.pop(row_id)
                restored.add(row_id)
            elif op["verb"] == "remove":
                # A row missing from the copy (its change not copied yet) is
                # still removed on the hub, and so are its children the copy
                # knows when the cascade says so.
                if row_id in rows:
                    removed[row_id] = rows.pop(row_id)
                parent = removed[row_id]["parent"] if row_id in removed else None
                if op.get("children") == "move-up" and parent is not None:
                    for row in rows.values():
                        if row["parent"] == row_id:
                            row["parent"] = parent
                elif op.get("children") == "remove":
                    gone = {row_id}
                    while True:
                        more = {k for k, r in rows.items() if r["parent"] in gone and k not in gone}
                        if not more:
                            break
                        gone |= more
                    for k in gone - {row_id}:
                        removed[k] = rows.pop(k)
        except LedgerError:
            continue
    return {**view, "vocab": vocab, "rows": sorted(rows.values(), key=lambda r: r["seq"]), "pending": pending,
            "queue_removed": set(removed), "queue_restored": restored - set(removed),
            "queue_added": added - set(removed)}


def load(project: str, *, conn=None) -> dict[str, Any]:
    """A project's live rows and words: from the hub (after draining waiting
    writes), or from the offline copy plus the waiting writes when it is away."""
    owned = conn is None
    try:
        if owned:
            conn = _connect()
        drain(conn)
        replica_catch_up(conn)
        return _hub_view(conn, project)
    except Exception as exc:  # noqa: BLE001
        if _tables_missing(exc):
            raise LedgerError("the ledger tables are not in the database yet; run khipu migrate", "no_tables") from exc
        if not _away(exc):
            raise
        view = _offline_view(project) or {"vocab": {"state": [], "kind": []}, "rows": [], "as_of": None,
                                          "offline": True}
        return _overlay(view, project)
    finally:
        if owned and conn is not None:
            conn.close()


def get(project: str, row_id: str, *, conn=None) -> dict[str, Any]:
    refuse_secrets(project, row_id)
    owned = conn is None
    try:
        if owned:
            conn = _connect()
        drain(conn)
        replica_catch_up(conn)
        with conn.cursor() as cur:
            cur.execute(f"SELECT {', '.join(_ROW_COLS)} FROM ledger_rows WHERE project = %s AND row_id = %s",
                        (project, row_id))
            got = cur.fetchone()
            if got is None:
                marker = _marker(cur, project, row_id)
                conn.rollback()
                if marker:
                    raise _purged_error(row_id, marker)
                raise LedgerError(f"no such row: {row_id}", "not_found")
            row = _row_dict(got)
            if row["removed_at"] is not None:
                conn.rollback()
                raise _removed_error(row)
            cur.execute(f"SELECT {', '.join(_HIST_COLS)} FROM ledger_history WHERE project = %s AND row_id = %s "
                        "ORDER BY id", (project, row_id))
            history = [dict(zip(_HIST_COLS, h, strict=True)) for h in cur.fetchall()]
        conn.rollback()
        return {"row": row, "history": history, "offline": False}
    except LedgerError:
        raise
    except Exception as exc:  # noqa: BLE001
        if not _away(exc):
            raise
        view = _offline_view(project)
        merged = _overlay(view or {"vocab": {"state": [], "kind": []}, "rows": [], "as_of": None}, project)
        for row in merged["rows"]:
            if row["row_id"] == row_id:
                history = [h for h in (view or {}).get("history", []) if h["row_id"] == row_id]
                return {"row": row, "history": history, "offline": True, "as_of": merged.get("as_of")}
        raise LedgerError(f"no such row: {row_id}", "not_found") from exc
    finally:
        if owned and conn is not None:
            conn.close()


def removed_rows(project: str | None, *, conn=None) -> list[dict[str, Any]]:
    """Rows removed and not yet purged, with the days left to restore them."""
    after = settings()["ledger_purge_after"]
    owned = conn is None
    try:
        if owned:
            conn = _connect()
        with conn.cursor() as cur:
            where, params = ("AND project = %s", (project,)) if project else ("", ())
            cur.execute("SELECT project, row_id, title, kind, state, removed_at, removed_by, removed_note "
                        f"FROM ledger_rows WHERE removed_at IS NOT NULL {where} ORDER BY removed_at", params)
            got = cur.fetchall()
        conn.rollback()
    except HubAway as exc:
        raise LedgerError("the database is away; the removed list needs it", "away") from exc
    finally:
        if owned and conn is not None:
            conn.close()
    out = []
    for proj, row_id, title, kind, state, at, by, note in got:
        left = None
        if after:
            # In UTC: adding days to a zoned time is wall-clock arithmetic across a DST change.
            remaining = (at.astimezone(timezone.utc) + timedelta(seconds=after) - _now()).total_seconds()
            left = max(0, -int(-remaining // 86400))
        out.append({"project": proj, "id": row_id, "title": title, "kind": kind, "state": state,
                    "removed_at": _z(at), "removed_by": author_words(by), "note": note, "days_left": left})
    return out


# ---- rendering (the tracker's shapes) ------------------------------------------


def _order(rows: list[dict[str, Any]], vocab: dict[str, list]) -> list[dict[str, Any]]:
    """Kind, then parent group (a parent and its children together), then the
    order rows were added. Never by state or by last change (rule 6)."""
    kinds = {k: i for i, k in enumerate(vocab["kind"])}

    def kind(r: dict[str, Any]) -> tuple:
        return (kinds.get(r["kind"], len(kinds)), "" if r["kind"] in kinds else r["kind"])

    def group(r: dict[str, Any]) -> tuple:
        # A root row heads the group of its own children, as on the tracker's board.
        return (*kind(r), r["parent"] or r["row_id"])

    # Groups follow the order their first row was added, so a new row never
    # moves an older one (the tracker's import gives seq in its board order).
    first: dict[tuple, int] = {}
    for r in rows:
        first[group(r)] = min(first.get(group(r), r["seq"]), r["seq"])
    return sorted(rows, key=lambda r: (*kind(r), first[group(r)], group(r), r["seq"]))


def _updated(row: dict[str, Any]) -> str:
    return _z(row["updated_at"]) if isinstance(row["updated_at"], datetime) else str(row["updated_at"] or "")


def _no_owner(row: dict[str, Any]) -> bool:
    return (row["owner"] or "").strip().lower() in EMPTY_OWNERS


def is_loose(row: dict[str, Any], vocab: dict[str, list]) -> bool:
    """Open, or in a state this project does not know (never silently finished)."""
    return _meaning(vocab, row["state"]) in ("open", None)


def render_list(view: dict[str, Any], *, state=None, kind=None, owner=None, stale_hours=None) -> tuple[list, str]:
    vocab, rows = view["vocab"], _order(view["rows"], view["vocab"])
    if state:
        word = _word(vocab, "state", state) if vocab["state"] else state
        rows = [r for r in rows if r["state"] == word]
    if kind:
        word = _word(vocab, "kind", kind) if vocab["kind"] else kind
        rows = [r for r in rows if r["kind"] == word]
    if owner:
        rows = [r for r in rows if r["owner"] == owner]
    if stale_hours is not None:
        cutoff = _now() - timedelta(hours=stale_hours)
        rows = [r for r in rows if not isinstance(r["updated_at"], datetime) or r["updated_at"] < cutoff]
    lines = [f"{r['row_id']} · {r['state']} · {r['title']} · owner={r['owner']} · updated={_updated(r)}"
             for r in rows]
    return rows, "\n".join(lines + ["", f"{len(rows)} row(s)"])


def render_loose_ends(view: dict[str, Any], *, summary: bool = False) -> tuple[list, str]:
    vocab = view["vocab"]
    rows = [r for r in _order(view["rows"], vocab) if is_loose(r, vocab)]
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["state"], []).append(r)
    order = [s for s in LOOSE_ORDER if s in by]
    order += [w for w, _m in vocab["state"] if w in by and w not in order]
    order += [s for s in by if s not in order]
    lines = []
    for st in order:
        lines.append(f"== {st} ({len(by[st])})")
        if summary:
            continue
        for r in by[st]:
            flag = "  [NO OWNER]" if _no_owner(r) else ""
            lines.append(f"{r['row_id']} · {r['title'][:90]} · owner={r['owner']}{flag} · {(r['evidence'] or '')[:70]}")
    unowned = sum(1 for r in rows if _no_owner(r))
    terminal = sorted(w for w, m in vocab["state"] if m != "open")
    lines += ["", f"{len(rows)} loose end(s); {unowned} without an owner; terminal states = {terminal}"]
    return rows, "\n".join(lines)


def render_needs(view: dict[str, Any]) -> tuple[list, str]:
    vocab = view["vocab"]
    rows = [r for r in _order(view["rows"], vocab) if _meaning(vocab, r["state"]) == "needs"]
    who = "Matt" if settings()["ledger_owner_word"] == "NEEDS MATT" else "the owner"
    lines = [f"{r['row_id']} · {r['title']} · evidence={r['evidence']}" for r in rows]
    return rows, "\n".join(lines + ["", f"{len(rows)} row(s) need {who}"])


def render_board(view: dict[str, Any]) -> str:
    """The tracker's markdown board: the same sections in the same order."""
    vocab = view["vocab"]
    rows = _order(view["rows"], vocab)
    by_id = {r["row_id"]: r for r in rows}
    counts_state: dict[str, int] = {}
    counts_kind: dict[str, int] = {}
    for r in rows:
        counts_state[r["state"]] = counts_state.get(r["state"], 0) + 1
        counts_kind[r["kind"]] = counts_kind.get(r["kind"], 0) + 1
    since = _now() - timedelta(hours=24)
    # The tracker lists this section by id.
    changed = sorted((r for r in rows if isinstance(r["updated_at"], datetime) and r["updated_at"] >= since),
                     key=lambda r: r["row_id"])
    lines = [f"# Orchestrator Tracker (generated {_z(_now())})", "", f"Total rows: {len(rows)}", "",
             "## Counts by state"]
    states = [w for w, _m in vocab["state"]]
    lines += [f"- {s}: {counts_state.get(s, 0)}" for s in states + [s for s in counts_state if s not in states]]
    lines += ["", "## Counts by kind"]
    kinds = list(vocab["kind"])
    lines += [f"- {k}: {counts_kind.get(k, 0)}" for k in kinds + [k for k in counts_kind if k not in kinds]]
    lines += ["", "## Rows (grouped by kind, then parent; in the order added within a group; never reordered "
              "by state)", ""]
    current_kind = current_parent = None
    for r in rows:
        if r["kind"] != current_kind:
            current_kind, current_parent = r["kind"], None
            lines += [f"### {current_kind}", ""]
        parent = r["parent"] or ""
        if parent != current_parent:
            current_parent = parent
            if parent:
                prow = by_id.get(parent)
                lines.append(f"**parent: {parent}: {prow['title'] if prow else '(parent not found)'}**")
            else:
                lines.append("**(no parent / root)**")
        lines.append(f"- {r['row_id']} · {r['state']} · {r['title']} · {r['evidence']} · owner={r['owner']} · "
                     f"updated={_updated(r)}")
    lines += ["", f"## Changed since {_z(since)}", ""]
    if not changed:
        lines.append("(no rows changed in the last 24h)")
    else:
        lines += [f"- {r['row_id']} · {r['state']} · {r['title']} · updated={_updated(r)}" for r in changed]
    lines.append("")
    return "\n".join(lines)


def check(view: dict[str, Any]) -> dict[str, Any]:
    """Unknown state or kind and no owner fail (exit 1, as the tracker did); a
    missing parent and a row open too long only warn."""
    vocab, rows = view["vocab"], _order(view["rows"], view["vocab"])
    states, kinds = {w for w, _m in vocab["state"]}, set(vocab["kind"])
    ids = {r["row_id"] for r in rows}
    failures, warnings = [], []
    stale = settings()["ledger_stale_after"]
    cutoff = _now() - timedelta(seconds=stale) if stale else None
    for r in rows:
        if not r["state"]:
            failures.append(f"{r['row_id']}: missing state")
        elif r["state"] not in states:
            failures.append(f"{r['row_id']}: state '{r['state']}' is not one of this project's words")
        if r["kind"] not in kinds:
            failures.append(f"{r['row_id']}: kind '{r['kind']}' is not one of this project's words")
        if not r["owner"]:
            failures.append(f"{r['row_id']}: missing owner")
        if r["parent"] and r["parent"] not in ids:
            warnings.append(f"{r['row_id']}: parent {r['parent']} not found")
        if cutoff and is_loose(r, vocab) and isinstance(r["updated_at"], datetime) and r["updated_at"] < cutoff:
            days = int((_now() - r["updated_at"]).total_seconds() // 86400)
            warnings.append(f"{r['row_id']}: no change for {days} days")
    return {"ok": not failures, "rows": len(rows), "failures": failures, "warnings": warnings}


def _history_json(h: dict[str, Any]) -> dict[str, Any]:
    if h.get("raw") is not None:
        return h["raw"]
    if h.get("shape") == "event":
        return {"at": h["at_text"], "event": h.get("event") or "", "by": author_words(h.get("author"))}
    return {"ts": h["at_text"], "from": h["from_state"], "to": h["to_state"], "note": h["note"],
            "by": author_words(h.get("author")), "changes": h.get("changes") or {}}


def row_json(row: dict[str, Any], history: list[dict[str, Any]], project: str) -> dict[str, Any]:
    out = {"id": row["row_id"], "title": row["title"], "kind": row["kind"], "parent": row["parent"],
           "state": row["state"], "evidence": row["evidence"], "owner": row["owner"], "updated": _updated(row),
           "link": row.get("link") or "", "refs": row.get("refs") or {}, "project": project,
           "history": [_history_json(h) for h in history]}
    if row.get("queued"):
        out["queued"] = True
    return out


# ---- removal by reference (khipu_forget) -------------------------------------------


def parse_ref(ref: Any) -> tuple[str, str] | None:
    """``ledger:<project>#<row id>`` -> (project, row id), or None."""
    if not isinstance(ref, str) or not ref.startswith("ledger:") or "#" not in ref:
        return None
    project, _sep, row_id = ref[len("ledger:"):].rpartition("#")
    return (project, row_id) if project and row_id else None


def remove_ref(ref: str, *, note: str = "", children: str | None = None, source: str = "mcp") -> dict[str, Any]:
    parsed = parse_ref(ref)
    if parsed is None:
        raise LedgerError("a ledger id looks like ledger:<project>#<row id>", "bad_ref")
    project, row_id = parsed
    return write(new_op("remove", project, row_id, note=note, children=children, source=source))


# ---- doctor and the nightly -------------------------------------------------------


def doctor_block() -> dict[str, Any]:
    """Counts per project, waiting writes, removed rows waiting to purge,
    purged markers, and whether the offline copy carries the ledger. The
    outbox gates doctor (``ledger_outbox_ok``); everything else only warns."""
    out: dict[str, Any] = {"outbox": outbox_status(), "warnings": []}
    try:
        from khipu import hub_snapshot

        out["in_offline_copy"] = hub_snapshot.ledger_in_snapshot()
    except Exception as exc:  # noqa: BLE001
        out["in_offline_copy"] = False
        out["offline_copy_error"] = f"{type(exc).__name__}: {exc}"
    # The nightly dump is a pg_dump of the whole database, so every ledger
    # table is in it by construction; there is no table list to keep.
    out["in_dump"] = "whole database"
    out["offline_copy_waiting"] = replica_pending()
    if out["offline_copy_waiting"]:
        out["warnings"].append(f"ledger: {out['offline_copy_waiting']} change(s) not in the offline copy yet "
                               "(copied at the next ledger call that reaches the database)")
    try:
        conn = _connect()
    except Exception as exc:  # noqa: BLE001
        out["hub"] = {"ok": False, "error": type(exc).__name__}
        return out
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.ledger_rows') IS NOT NULL")
            if not cur.fetchone()[0]:
                out["hub"] = {"ok": False, "error": "the ledger tables are not in the database yet"}
                out["warnings"].append("ledger: the ledger tables are not in the database yet (khipu migrate)")
                return out
            cur.execute("SELECT project, count(*) FILTER (WHERE removed_at IS NULL), "
                        "count(*) FILTER (WHERE removed_at IS NOT NULL) FROM ledger_rows GROUP BY project "
                        "ORDER BY project")
            projects = {p: {"rows": live, "removed": gone, "history": 0} for p, live, gone in cur.fetchall()}
            cur.execute("SELECT project, count(*) FROM ledger_history GROUP BY project")
            for p, n in cur.fetchall():
                projects.setdefault(p, {"rows": 0, "removed": 0})["history"] = n
            cur.execute("SELECT count(*) FROM ledger_removed")
            out["purged_markers"] = cur.fetchone()[0]
            cur.execute("SELECT count(*), max(at) FROM ledger_imports")
            n_imports, last_import = cur.fetchone()
        conn.rollback()
        out["hub"] = {"ok": True}
        out["projects"] = projects
        out["removed_waiting_to_purge"] = sum(p["removed"] for p in projects.values())
        out["imports"] = {"count": n_imports, "last": _z(last_import) if last_import else None}
        if projects and not out.get("in_offline_copy"):
            out["warnings"].append("ledger: the offline copy has no ledger yet (it is added at the next refresh)")
    finally:
        conn.close()
    return out


def nightly() -> dict[str, Any]:
    """The nightly's ledger step: land waiting writes, then purge rows removed
    longer ago than ``ledger_purge_after``. Fail-open."""
    try:
        conn = _connect()
    except HubAway as exc:
        return {"ok": False, "error": f"hub away: {exc}"[:200]}
    try:
        drained = drain(conn)
        purged = purge_due(conn)
        replica_catch_up(conn)
        return {"ok": not drained.get("refused"), "landed": drained["landed"], "refused": drained["refused"],
                "purged": purged.get("purged", 0)}
    except Exception as exc:  # noqa: BLE001  (a ledger miss cannot fail the night)
        if _tables_missing(exc):
            return {"ok": True, "skipped": "the ledger tables are not in the database yet"}
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
    finally:
        conn.close()


# ---- the CLI ------------------------------------------------------------------


def _project_for(args: argparse.Namespace) -> str:
    if getattr(args, "project", None):
        return _check_project(args.project)
    from khipu.identity import resolve_repo_root

    try:
        cwd = os.getcwd()
    except OSError:
        cwd = ""
    return _check_project(resolve_repo_root(cwd).get("project"))


def _emit(args: argparse.Namespace, payload: dict[str, Any], text: str | None) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, default=str))
    elif text is not None:
        print(text)


def _offline_note(view: dict[str, Any]) -> str | None:
    if not view.get("offline"):
        return None
    return (f"(the database is away: read from the offline copy as of {view.get('as_of') or 'an unknown time'}, "
            f"with {view.get('pending', 0)} waiting change(s) applied)")


def cmd(args: argparse.Namespace) -> int:
    verb = args.ledger_cmd
    try:
        refuse_secrets([v for k, v in vars(args).items() if k != "func"])
        project = _project_for(args)
        if verb in ("add", "set", "remove", "restore"):
            return _cmd_write(args, verb, project)
        if verb == "purge":
            return _cmd_purge(args, project)
        if verb == "removed":
            rows = removed_rows(None if args.all_projects else project)
            _emit(args, {"ok": True, "removed": rows},
                  "\n".join([f"{r['id']} · {r['title']} · removed {r['removed_at']} by {r['removed_by']} · "
                             f"days left={r['days_left']}" for r in rows] + ["", f"{len(rows)} removed row(s)"]))
            return 0
        if verb == "get":
            got = get(project, args.id)
            payload = row_json(got["row"], got["history"], project)
            if got.get("offline"):
                payload["offline_as_of"] = got.get("as_of")
            print(json.dumps(payload, indent=2, sort_keys=True, default=str))
            return 0
        view = load(project)
        note = _offline_note(view)
        rc = 0
        if verb == "list":
            rows, text = render_list(view, state=args.state, kind=args.kind, owner=args.owner,
                                     stale_hours=args.stale_hours)
            _emit(args, {"ok": True, "project": project, "offline": view["offline"], "as_of": view.get("as_of"),
                         "rows": [row_json(r, [], project) for r in rows]}, text)
        elif verb == "loose-ends":
            rows, text = render_loose_ends(view, summary=args.summary)
            _emit(args, {"ok": True, "project": project, "rows": [row_json(r, [], project) for r in rows]}, text)
        elif verb in ("needs-owner", "needs-matt"):
            rows, text = render_needs(view)
            _emit(args, {"ok": True, "project": project, "rows": [row_json(r, [], project) for r in rows]}, text)
        elif verb == "render":
            board = render_board(view)
            if args.out:
                Path(args.out).write_text(board, encoding="utf-8")
                _emit(args, {"ok": True, "rows": len(view["rows"]), "out": args.out},
                      f"rendered {len(view['rows'])} rows -> {args.out}")
            else:
                _emit(args, {"ok": True, "board": board}, board)
        elif verb == "check":
            result = check(view)
            if result["ok"]:
                text = f"CHECK OK: {result['rows']} rows, all have a state and kind this project knows and an owner."
            else:
                text = "\n".join([f"CHECK FAILED: {len(result['failures'])} problem(s):"]
                                 + [f"  - {f}" for f in result["failures"]])
            if result["warnings"]:
                text += "\n" + "\n".join([f"{len(result['warnings'])} warning(s):"]
                                         + [f"  - {w}" for w in result["warnings"]])
            _emit(args, {**result, "project": project, "vocab": view["vocab"]}, text)
            rc = 0 if result["ok"] else 1
        if note and not args.json:
            print(note)
        return rc
    except LedgerError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(exc), "reason": exc.reason}))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1


def _cmd_write(args: argparse.Namespace, verb: str, project: str) -> int:
    if verb == "restore" and not args.from_app:
        raise LedgerError("a removed row is restored in the Khipu app (Settings, Work, Removed rows)", "app_only")
    fields: dict[str, Any] = {}
    if verb == "add":
        fields = {"title": args.title, "kind": args.kind, "parent": args.parent or "", "state": args.state,
                  "evidence": args.evidence or "", "owner": args.owner or "none", "link": args.link or ""}
    elif verb == "set":
        fields = {"state": args.state, "evidence": args.evidence, "owner": args.owner, "title": args.title,
                  "link": args.link}
    op = new_op(verb, project, args.id, fields=fields, note=getattr(args, "note", "") or "",
                if_state=getattr(args, "if_state", None), children=getattr(args, "children", None),
                op_id=args.op_id, from_app=args.from_app)
    result = write(op)
    text = result["line"] + (f"\n{QUEUED_WORDS}" if result.get("queued") else "")
    _emit(args, {**result, "op_id": op["op_id"], "project": project}, text)
    return 0


def _cmd_purge(args: argparse.Namespace, project: str) -> int:
    if not args.from_app:
        raise LedgerError("removing a row for good is done in the Khipu app (Settings, Work, Removed rows)",
                          "app_only")
    try:
        conn = _connect()
    except HubAway as exc:
        raise LedgerError("the database is away; removing for good needs it", "away") from exc
    try:
        result = purge(conn, project, args.id)
    finally:
        conn.close()
    _emit(args, result, result["line"])
    return 0


def add_parser(sub) -> None:
    """``khipu ledger <verb>``: the tracker's verbs and flags, plus remove."""
    p = sub.add_parser("ledger", help="The ledger of what work is done and not done (tracker verbs)")
    verbs = p.add_subparsers(dest="ledger_cmd", required=True)

    def common(sp, *, write: bool = False):
        sp.add_argument("--project", default=None, help="Project key (default: from this folder, as capture does)")
        sp.add_argument("--json", action="store_true")
        if write:
            sp.add_argument("--op-id", default=None, help="Pass the same id to retry a write safely")
            sp.add_argument("--from-app", action="store_true", help=argparse.SUPPRESS)

    a = verbs.add_parser("add")
    a.add_argument("id")
    a.add_argument("--title", required=True)
    a.add_argument("--kind", required=True)
    a.add_argument("--parent", default="")
    a.add_argument("--state", default=None)
    a.add_argument("--evidence", default="")
    a.add_argument("--owner", default="none")
    a.add_argument("--note", default="")
    a.add_argument("--link", default="")
    common(a, write=True)

    s = verbs.add_parser("set")
    s.add_argument("id")
    s.add_argument("--state", default=None)
    s.add_argument("--evidence", default=None)
    s.add_argument("--owner", default=None)
    s.add_argument("--note", default="")
    s.add_argument("--title", default=None)
    s.add_argument("--link", default=None)
    s.add_argument("--if-state", default=None)
    common(s, write=True)

    g = verbs.add_parser("get")
    g.add_argument("id")
    common(g)

    lst = verbs.add_parser("list")
    lst.add_argument("--state", default=None)
    lst.add_argument("--kind", default=None)
    lst.add_argument("--owner", default=None)
    lst.add_argument("--stale-hours", type=float, default=None)
    common(lst)

    le = verbs.add_parser("loose-ends")
    le.add_argument("--summary", action="store_true")
    common(le)

    for name in ("needs-owner", "needs-matt"):
        common(verbs.add_parser(name))

    r = verbs.add_parser("render")
    r.add_argument("--out", default=None)
    common(r)

    common(verbs.add_parser("check"))

    rm = verbs.add_parser("remove")
    rm.add_argument("id")
    rm.add_argument("--note", default="")
    rm.add_argument("--children", choices=("remove", "move-up"), default=None)
    common(rm, write=True)

    rd = verbs.add_parser("removed")
    rd.add_argument("--all-projects", action="store_true")
    common(rd)

    rs = verbs.add_parser("restore", help="Restore a removed row (the app's Restore)")
    rs.add_argument("id")
    common(rs, write=True)

    pg = verbs.add_parser("purge", help="Remove a removed row for good (the app's Remove for good)")
    pg.add_argument("id")
    common(pg, write=True)

    p.set_defaults(func=cmd)
