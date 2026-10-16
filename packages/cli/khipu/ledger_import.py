"""Importing an old tracker file into the ledger, and exporting a project back
in the tracker's shape (Work view phase 2; docs/plans/2026-10-15-work-view.md,
"Importing an existing tracker").

A tracker file is one JSON object, ``{"rows": {id: row}}``. It is opened once,
read-only, through ``khipu.safe_read`` (no link followed, one name, a private or
login folder refused before anything is opened), and a name repeated in one
JSON object is refused rather than one copy dropped. No size limit: the
no-loss rule wins. Khipu never writes, moves or deletes it.

An import is one transaction holding the project's ledger lock
(``ledger.lock_project``) from its first read to its commit, so no other
writer lands in between. It writes every row and every history entry, keeping
each entry exactly as found (``ledger_history.raw``), then checks the whole
project against the file: the rows and entries it wrote are exactly the
file's, both ways, and every other row reads as it did. Anything that differs
rolls the whole import back. The only allowed difference is a string Khipu's
secret redaction masked (a value or a key), which the report names.

What a tracker row holds that the ledger's columns cannot (a missing field, a
value that is not text, a time written another way, a key the ledger has no
column for) is kept in the row's ``refs["imported"]`` so the export gives the
row back as it was.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from datetime import datetime
from typing import Any

from khipu import ledger, safe_read
from khipu.ledger import LedgerError

TRACKER_KEYS = ("id", "title", "kind", "parent", "state", "evidence", "owner", "updated", "history")
_TEXT_FIELDS = ("title", "kind", "parent", "state", "evidence", "owner")
IMPORT_AUTHOR = {"kind": "import"}
# Entries Khipu writes during an import carry an op id with one of these
# prefixes; they are Khipu's own record, never part of the old file.
MARKER_OP = "import:"
BOTH_OP = "import-both:"
CATCH_UP_OP = "import-catch-up:"
# A row the ledger added itself that the old file also holds keeps, in its
# ``refs`` under this key, the digest of the file's version a catch-up last
# noted as changed in both places (an imported row keeps it in its import
# record). Khipu's own note: never given back by the export.
SEEN_REF = "tracker_seen"
_SHOWN = 20  # ids listed by name in a refusal before "and N more"

# A word new to the standard list gets the meaning its name suggests; the
# owner can change it in the app (Settings, Work). Anything else is open.
_DONE_WORDS = frozenset({"done", "complete", "completed", "verified", "shipped", "merged", "closed", "finished"})
_DROPPED_WORDS = frozenset({"dropped", "cancelled", "canceled", "abandoned", "wontfix", "won't do", "wont do"})


# ---- reading the file --------------------------------------------------------------


def _mask(value: Any, where: str, masked: list[str]) -> Any:
    """``value`` with every key-shaped string masked, the keys of objects
    included; ``masked`` gets the place of each one. A masked key that would
    meet another key of the same object is numbered, so no value is lost."""
    from khipu.redact import redact_secrets

    if isinstance(value, str):
        out, hits = redact_secrets(value, force=True)
        if hits:
            masked.append(where)
        return out
    if isinstance(value, dict):
        out_d: dict[str, Any] = {}
        names = [(k, *redact_secrets(k, force=True)) for k in value]
        taken = {k for k, _name, hits in names if not hits}
        for k, name, hits in names:
            if hits:
                base, n = name, 1
                while name in taken:
                    n += 1
                    name = f"{base} ({n})"
                taken.add(name)
                masked.append(f"{where}.{name}")
            out_d[name] = _mask(value[k], f"{where}.{name}", masked)
        return out_d
    if isinstance(value, list):
        return [_mask(v, f"{where}[{i}]", masked) for i, v in enumerate(value)]
    return value


def _listed(ids: list[str]) -> str:
    shown = ", ".join(ids[:_SHOWN])
    return shown + (f" and {len(ids) - _SHOWN} more" if len(ids) > _SHOWN else "")


class _Repeated(ValueError):
    def __init__(self, key: str):
        super().__init__(key)
        self.key = key


def _no_repeats(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """A JSON object, refused when a name appears in it twice: the parser
    would otherwise keep only the last, losing the first without a word."""
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _Repeated(key)
        out[key] = value
    return out


def read_tracker(path: str | os.PathLike) -> dict[str, Any]:
    """The tracker file at ``path``: its name (never its folder), digest, rows
    with key-shaped strings masked, and what was masked. Refuses a file it
    cannot take whole, naming why; nothing is skipped silently. No size
    limit: the no-loss rule wins."""
    from khipu.redact import redact_secrets
    from khipu.routing_sources import _short_error

    # The path goes to safe_read exactly as given: only it parses a path.
    try:
        _folder, file_name = safe_read.parse(path)
        data, _st = safe_read.read_file(path)
    except safe_read.Refused:
        raise LedgerError("Not read: the file is inside a private folder or a login folder, or is reached through a link.",
                          "refused") from None
    except OSError as exc:
        raise LedgerError(f"Couldn't read the tracker file: {_short_error(exc)}", "unreadable") from None
    try:
        doc = json.loads(data.decode("utf-8"), object_pairs_hook=_no_repeats)
    except _Repeated as exc:
        name = exc.key if not redact_secrets(exc.key, force=True)[1] else "a key-shaped name"
        raise LedgerError(f"Nothing was imported: the name {name[:80]!r} appears more than once in one object of "
                          "the file, so one of them would be lost.", "repeated") from None
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise LedgerError("Couldn't read the tracker file: not readable JSON", "unreadable") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("rows"), dict):
        raise LedgerError('This is not a tracker file: it has no "rows" object', "not_tracker")
    limits = ledger.settings()
    problems: list[str] = []
    masked: list[str] = []
    rows: dict[str, dict] = {}
    for key, row in doc["rows"].items():
        if redact_secrets(key, force=True)[1]:
            problems.append(f"{key[:6]}...: the id looks like a key or password")
            continue
        try:
            ledger._check_id(key, limits)
        except LedgerError as exc:
            problems.append(f"{key[:80]}: {exc}")
            continue
        if not isinstance(row, dict):
            problems.append(f"{key}: the row is not an object")
            continue
        if "id" in row and row["id"] != key:
            problems.append(f"{key}: the row says its id is something else")
            continue
        if "history" in row and not isinstance(row["history"], list):
            problems.append(f"{key}: its history is not a list")
            continue
        rows[key] = _mask(row, key, masked)
    if problems:
        raise LedgerError("Nothing was imported; these rows can't be taken as they are: " + "; ".join(problems[:_SHOWN])
                          + (f"; and {len(problems) - _SHOWN} more" if len(problems) > _SHOWN else ""), "bad_rows")
    other = sorted(k for k in doc if k != "rows")
    # The name goes into reports and ledger_imports.file_name: a key-shaped
    # name is masked like any other string from the file.
    return {"name": redact_secrets(file_name, force=True)[0], "digest": hashlib.sha256(data).hexdigest(), "rows": rows, "masked": masked,
            "other_keys": [redact_secrets(k, force=True)[0] for k in other]}


# ---- counting ----------------------------------------------------------------------


def _word_of(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def tracker_counts(rows: dict[str, dict]) -> dict[str, Any]:
    """Rows, history entries, rows per state, per kind and with a parent, and
    the rows whose parent is not in the file."""
    by_state: Counter = Counter()
    by_kind: Counter = Counter()
    with_parent = history = 0
    missing: list[str] = []
    for row_id, row in rows.items():
        by_state[_word_of(row.get("state"))] += 1
        by_kind[_word_of(row.get("kind"))] += 1
        history += len(row.get("history") or [])
        if row.get("parent"):
            with_parent += 1
            if not isinstance(row["parent"], str) or row["parent"] not in rows:
                missing.append(row_id)
    return {"rows": len(rows), "history": history, "by_state": dict(by_state), "by_kind": dict(by_kind),
            "with_parent": with_parent, "missing_parent": missing}


# ---- one row in, one row out ----------------------------------------------------


def _stored(row: dict[str, Any], now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    """The ledger columns for a tracker row, and ``refs["imported"]``: what a
    column cannot hold, so ``export_rows`` gives the row back as it was."""
    orig: dict[str, Any] = {}
    cols: dict[str, Any] = {}
    for field in _TEXT_FIELDS:
        default = "none" if field == "owner" else ""
        value = row.get(field)
        cols[field] = value if isinstance(value, str) else default
        if field not in row:
            orig[field] = {"absent": True, "stored": cols[field]}
        elif not isinstance(value, str):
            orig[field] = {"value": value, "stored": cols[field]}
    updated = ledger._parse_time(row.get("updated")) if isinstance(row.get("updated"), str) else None
    cols["updated_at"] = updated or now
    if "updated" not in row:
        orig["updated"] = {"absent": True, "stored": ledger._z(cols["updated_at"])}
    elif row["updated"] != ledger._z(cols["updated_at"]):
        orig["updated"] = {"value": row["updated"], "stored": ledger._z(cols["updated_at"])}
    if "id" not in row:
        orig["id"] = {"absent": True}
    if "history" not in row:
        orig["history"] = {"absent": True}
    # The row as the file had it (history aside), so a catch-up can tell a
    # row the old file changed from one only the ledger changed.
    imported: dict[str, Any] = {"file": _row_digest(row)}
    if orig:
        imported["orig"] = orig
    extra = {k: v for k, v in row.items() if k not in TRACKER_KEYS}
    if extra:
        imported["extra"] = extra
    return cols, imported


def _row_digest(row: dict[str, Any]) -> str:
    fields = {k: v for k, v in row.items() if k != "history"}
    return hashlib.sha256(_entry_key(fields).encode("utf-8")).hexdigest()


def _entry_columns(entry: Any, fallback: datetime) -> dict[str, Any]:
    """A history entry kept verbatim in ``raw``, with the columns Khipu reads
    filled from it: a transition ``{ts, from, to, note}`` or an event
    ``{at, event}`` (compatibility rule 9). Anything else is kept as an event."""
    def text(key: str) -> str | None:
        value = entry.get(key) if isinstance(entry, dict) else None
        return value if isinstance(value, str) else None

    transition = isinstance(entry, dict) and any(k in entry for k in ("ts", "from", "to"))
    at_text = text("ts") if transition else text("at")
    return {"at": ledger._parse_time(at_text) or fallback, "at_text": at_text or "",
            "shape": "transition" if transition else "event",
            "from_state": text("from") if transition else None, "to_state": text("to") if transition else None,
            "note": (text("note") or "") if transition else "",
            "event": None if transition else (text("event") if isinstance(entry, dict) else json.dumps(entry)),
            "raw": entry}


def _entry_key(entry: Any) -> str:
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str)


def _tracker_row(row: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
    imported = (row.get("refs") or {}).get("imported") or {}
    orig = imported.get("orig") or {}
    values = {"id": row["row_id"], **{f: row[f] for f in _TEXT_FIELDS}, "updated": ledger._updated(row)}
    out: dict[str, Any] = {}
    for field, value in values.items():
        o = orig.get(field)
        if o is not None and o.get("stored", value) == value:
            if not o.get("absent"):
                out[field] = o.get("value")
        else:
            out[field] = value
    # An entry from an old file is given back as found, a JSON null included.
    out["history"] = [h["raw"] if h["has_raw"] else ledger._history_json(h) for h in history]
    if not out["history"] and (orig.get("history") or {}).get("absent"):
        del out["history"]
    if row.get("link"):
        out["link"] = row["link"]
    refs = {k: v for k, v in (row.get("refs") or {}).items() if k not in ("imported", SEEN_REF)}
    if refs:
        out["refs"] = refs
    out.update(imported.get("extra") or {})
    return out


_HIST_COLS = ("row_id", "at_text", "author", "shape", "from_state", "to_state", "changes", "note", "event", "raw",
              "op_id", "has_raw")


def export_rows(cur, project: str, *, ids: list[str] | None = None, raw_only: bool = False) -> dict[str, dict]:
    """The project's live rows in the tracker's shape, in the board's order.
    ``raw_only`` keeps only history entries that came from an old file, which
    is what an import's round trip compares. Otherwise every entry is given,
    except the "Imported from" line each import adds: that is Khipu's note of
    the import, never a change to the row."""
    where, params = ("AND row_id = ANY(%s)", (project, ids)) if ids is not None else ("", (project,))
    vocab = ledger._vocab(cur, project)
    cur.execute(f"SELECT {', '.join(ledger._ROW_COLS)} FROM ledger_rows WHERE project = %s AND removed_at IS NULL "
                f"{where}", params)
    rows = [ledger._row_dict(r) for r in cur.fetchall()]
    which = "AND raw IS NOT NULL" if raw_only else "AND (op_id IS NULL OR op_id NOT LIKE %s)"
    hist_params = params if raw_only else (*params, f"{MARKER_OP}%")
    cols = ", ".join(c if c != "has_raw" else "raw IS NOT NULL" for c in _HIST_COLS)
    cur.execute(f"SELECT {cols} FROM ledger_history WHERE project = %s {where} {which} ORDER BY id", hist_params)
    history: dict[str, list] = {}
    for values in cur.fetchall():
        h = dict(zip(_HIST_COLS, values, strict=True))
        history.setdefault(h["row_id"], []).append(h)
    return {r["row_id"]: _tracker_row(r, history.get(r["row_id"], [])) for r in ledger._order(rows, vocab)}


# ---- the project's words ----------------------------------------------------------


def _meaning_for(word: str) -> str:
    plain = " ".join(word.split()).lower()
    if plain in _DONE_WORDS:
        return "done"
    if plain in _DROPPED_WORDS:
        return "dropped"
    if plain.startswith("needs"):
        return "needs"
    return "open"


def _found(rows: dict[str, dict], field: str) -> list[str]:
    seen: dict[str, None] = {}
    for row in rows.values():
        if isinstance(row.get(field), str) and row[field]:
            seen.setdefault(row[field], None)
    return list(seen)


def _vocab_for(rows: dict[str, dict], existing: dict[str, list]) -> tuple[dict[str, list], dict[str, list]]:
    """The project's words after this import, and the words it adds. A new
    project starts from the standard list, with the needs-you word the file
    uses; each word the file uses that is not in the list is added as found
    (compatibility rule 7). A word is never rewritten in a row."""
    states, kinds = _found(rows, "state"), _found(rows, "kind")
    if existing["state"]:
        vocab = {"state": [list(s) for s in existing["state"]], "kind": list(existing["kind"])}
    else:
        vocab = ledger.default_vocab()
        file_needs = [s for s in states if " ".join(s.split()).upper() in ledger.OWNER_WORDS]
        for pair in vocab["state"]:
            if pair[1] == "needs" and file_needs and pair[0] not in states:
                pair[0] = file_needs[0]
        file_kinds = [k for k in kinds if k in ledger.NEEDS_KIND_WORDS]
        vocab["kind"] = [file_kinds[0] if k in ledger.NEEDS_KIND_WORDS and file_kinds and k not in kinds else k
                         for k in vocab["kind"]]
    known_states = {w for w, _m in vocab["state"]}
    added: dict[str, list] = {"state": [], "kind": []}
    for word in states:
        if word not in known_states:
            vocab["state"].append([word, _meaning_for(word)])
            added["state"].append([word, _meaning_for(word)])
    for word in kinds:
        if word not in vocab["kind"]:
            vocab["kind"].append(word)
            added["kind"].append(word)
    return vocab, added


def _save_vocab(cur, project: str, vocab: dict[str, list], added: dict[str, list], new_project: bool) -> None:
    if new_project:
        ledger.ensure_project(cur, project, vocab)
        return
    cur.execute("SELECT field, coalesce(max(position), -1) FROM ledger_vocab WHERE project = %s GROUP BY field",
                (project,))
    top = dict(cur.fetchall())
    for pos, (word, meaning) in enumerate(added["state"], start=top.get("state", -1) + 1):
        cur.execute("INSERT INTO ledger_vocab (project, field, word, meaning, position) VALUES (%s, 'state', %s, %s, %s)"
                    " ON CONFLICT DO NOTHING", (project, word, meaning, pos))
    for pos, word in enumerate(added["kind"], start=top.get("kind", -1) + 1):
        cur.execute("INSERT INTO ledger_vocab (project, field, word, meaning, position) VALUES (%s, 'kind', %s, '', %s)"
                    " ON CONFLICT DO NOTHING", (project, word, pos))


# ---- writing ------------------------------------------------------------------------


def _board_order(rows: dict[str, dict], vocab: dict[str, list]) -> list[str]:
    """The tracker's own render order: kind, then parent (a root row heads
    its own children's group), then id. Rows take ``seq`` in this order, so
    ``ledger._order`` gives the board back as the tracker drew it."""
    kinds = {k: i for i, k in enumerate(vocab["kind"])}

    def key(row_id: str) -> tuple:
        row = rows[row_id]
        kind = row.get("kind") if isinstance(row.get("kind"), str) else ""
        parent = row.get("parent") if isinstance(row.get("parent"), str) else ""
        return (kinds.get(kind, len(kinds)), "" if kind in kinds else kind, parent or row_id, row_id)

    return sorted(rows, key=key)


def _insert_history(cur, project: str, entries: list[tuple[str, dict[str, Any], str | None]]) -> None:
    """``entries`` as (row id, columns, op id). ``raw`` is set only for an
    entry from an old file (Jsonb of JSON null included); Khipu's own entries
    leave it NULL."""
    from psycopg.types.json import Jsonb

    cur.executemany(
        "INSERT INTO ledger_history (project, row_id, at, at_text, author, shape, from_state, to_state, changes, "
        "note, event, raw, op_id, source) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'import')",
        [(project, row_id, c["at"], c["at_text"], Jsonb(IMPORT_AUTHOR), c["shape"], c.get("from_state"),
          c.get("to_state"), Jsonb(c.get("changes") or {}), c.get("note") or "", c.get("event"),
          Jsonb(c["raw"]) if "raw" in c else None, op_id)
         for row_id, c, op_id in entries],
    )


def _insert_rows(cur, project: str, ids: list[str], rows: dict[str, dict], now: datetime) -> None:
    from psycopg.types.json import Jsonb

    cur.execute("SELECT next_seq FROM ledger_projects WHERE project = %s FOR UPDATE", (project,))
    seq = cur.fetchone()[0]
    values = []
    entries: list[tuple[str, dict[str, Any], str | None]] = []
    for offset, row_id in enumerate(ids):
        cols, imported = _stored(rows[row_id], now)
        refs = {"imported": imported} if imported else {}
        values.append((project, row_id, cols["title"], cols["kind"], cols["parent"], cols["state"], cols["owner"],
                       cols["evidence"], Jsonb(refs), seq + offset, cols["updated_at"], cols["updated_at"]))
        for entry in rows[row_id].get("history") or []:
            entries.append((row_id, _entry_columns(entry, cols["updated_at"]), None))
    cur.executemany(
        "INSERT INTO ledger_rows (project, row_id, title, kind, parent, state, owner, evidence, refs, seq, "
        "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", values)
    cur.execute("UPDATE ledger_projects SET next_seq = next_seq + %s WHERE project = %s", (len(ids), project))
    _insert_history(cur, project, entries)


def _note_entry(event: str, now: datetime) -> dict[str, Any]:
    return {"at": now, "at_text": ledger._z(now), "shape": "event", "event": event}


# ---- the import -------------------------------------------------------------------


def _compare(expected: dict[str, dict], got: dict[str, dict]) -> list[str]:
    """Where the export differs from the file, as ``id.field``."""
    out = [f"{i}: missing from the ledger" for i in expected if i not in got]
    for row_id, row in expected.items():
        back = got.get(row_id)
        if back is None:
            continue
        for key in sorted(set(row) | set(back)):
            if row.get(key, KeyError) != back.get(key, KeyError):
                out.append(f"{row_id}.{key}")
    return out


def _snapshot(cur, project: str) -> dict[str, Any]:
    """The whole project as it stands in this transaction: every row, live
    and removed, with all its columns (``all``); every row's whole history in
    order with all its columns, entries from an old file included (``hist``);
    and each live row in the tracker's shape with the entries that came from
    an old file (``rows``)."""
    cur.execute(f"SELECT {', '.join(ledger._ROW_COLS)} FROM ledger_rows WHERE project = %s", (project,))
    every = {}
    for values in cur.fetchall():
        row = dict(zip(ledger._ROW_COLS, values, strict=True))
        every[row["row_id"]] = row
    cols = ", ".join(c if c != "has_raw" else "raw IS NOT NULL" for c in _ALL_HIST)
    cur.execute(f"SELECT row_id, {cols} FROM ledger_history WHERE project = %s ORDER BY id", (project,))
    hist: dict[str, list[dict]] = {}
    for row_id, *values in cur.fetchall():
        hist.setdefault(row_id, []).append(dict(zip(_ALL_HIST, values, strict=True)))
    return {"ids": set(every), "all": every, "hist": hist, "rows": export_rows(cur, project, raw_only=True)}


_ALL_HIST = ("id", "at", "at_text", "author", "shape", "from_state", "to_state", "changes", "note", "event", "raw",
             "has_raw", "op_id", "source")


def _fields(row: dict[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in (row or {}).items() if k != "history"}


def _ids_differ(before: dict[str, Any], after: dict[str, Any], new: set[str], what: str) -> list[str]:
    want = before["ids"] | new
    return ([f"{i}: in the ledger but not {what}" for i in sorted(after["ids"] - want)]
            + [f"{i}: missing from the ledger" for i in sorted(want - after["ids"])])


def _unchanged(before: dict[str, Any], after: dict[str, Any], skip: set[str]) -> list[str]:
    """Every row, live or removed, and every row's history, that the import
    had no business changing, compared column by column and entry by entry
    in order, both ways."""
    out = []
    for row_id in sorted((set(before["all"]) | set(before["hist"]) | set(after["hist"])) - skip):
        if before["all"].get(row_id) != after["all"].get(row_id) \
                or before["hist"].get(row_id, []) != after["hist"].get(row_id, []):
            out.append(f"{row_id}: changed, though the import does not touch it")
    return out


def _verify(cur, project: str, expected: dict[str, dict], counts: dict[str, Any],
            before: dict[str, Any], order: list[str] | None = None) -> dict[str, Any]:
    """The whole project against the file, inside the import's transaction:
    the rows that were not here before are exactly the file's rows, in
    ``order`` (the order they were written; the file's own when not given),
    each reading back field by field with its history in order, then one
    "Imported from" line; every row that was here before, live or removed,
    reads as it did, history and all."""
    after = _snapshot(cur, project)
    new = set(expected) - before["ids"]
    got = {i: r for i, r in after["rows"].items() if i in new}
    differs = _ids_differ(before, after, new, "in the file")
    differs += _compare(expected, got)
    want = [i for i in (order if order is not None else expected) if i in new]
    stored = sorted((i for i in want if i in after["all"]), key=lambda i: after["all"][i]["seq"])
    if stored != [i for i in want if i in after["all"]]:
        differs.append("rows: stored out of order")
    for row_id in sorted(new):
        hist = after["hist"].get(row_id, [])
        own = [h for h in hist if not h["has_raw"]]
        if len(own) != 1 or hist[-1] is not own[0] or not (own[0]["op_id"] or "").startswith(MARKER_OP):
            differs.append(f"{row_id}.history: {len(own)} entries of Khipu's, not one import line at the end")
    differs += _unchanged(before, after, new)
    ledger_counts = tracker_counts(got)
    mismatched = [k for k in ("rows", "history", "by_state", "by_kind", "with_parent") if counts[k] != ledger_counts[k]]
    return {"ok": not differs and not mismatched, "counts_in_ledger": ledger_counts, "counts_differ": mismatched,
            "fields_differ": differs[:_SHOWN], "fields_differ_count": len(differs)}


def _record(conn, project: str, src: dict[str, Any], report: dict[str, Any], result: str) -> int:
    from psycopg.types.json import Jsonb

    with conn.cursor() as cur:
        cur.execute("INSERT INTO ledger_imports (project, file_name, digest, counts, result) "
                    "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                    (project, src["name"], src["digest"], Jsonb(report), result))
        return cur.fetchone()[0]


def _existing(cur, project: str, ids: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(rows here by id with their removed time, purged markers by id)."""
    cur.execute("SELECT row_id, removed_at FROM ledger_rows WHERE project = %s AND row_id = ANY(%s)", (project, ids))
    here = dict(cur.fetchall())
    cur.execute("SELECT row_id, purged_at FROM ledger_removed WHERE project = %s AND row_id = ANY(%s)", (project, ids))
    return here, dict(cur.fetchall())


def run_import(path: str | os.PathLike, project: str, *, dry_run: bool = False, catch_up: bool = False,
               conn=None) -> dict[str, Any]:
    """Import the tracker at ``path`` into ``project``; see the module text.
    ``catch_up`` brings in only what changed in the file since the last
    import. Returns the report; ``result`` is one of done, caught up, nothing
    new, dry run, refused, rolled back."""
    ledger.refuse_secrets(project)
    project = ledger._check_project(project)
    src = read_tracker(path)
    counts = tracker_counts(src["rows"])
    report: dict[str, Any] = {"ok": True, "project": project, "file": src["name"], "digest": src["digest"],
                              "counts_in_file": counts, "masked": src["masked"], "other_keys": src["other_keys"],
                              "catch_up": catch_up}
    owned = conn is None
    try:
        if owned:
            conn = ledger._connect()
    except ledger.HubAway:
        if dry_run:
            return {**report, "result": "dry run", "hub_checked": False,
                    "words": "The database is away, so ids already in the ledger were not checked."}
        raise LedgerError("the database is away; importing needs it", "away") from None
    try:
        with ledger._ledger_lock():
            # Waiting writes land first, so an id they add is seen here.
            ledger.drain(conn)
            out = _import_locked(conn, project, src, report, dry_run=dry_run, catch_up=catch_up)
        affected = out.pop("affected", None)
        if affected:
            ledger._replica_sync(conn, project, affected)
        return out
    except Exception as exc:
        if ledger._tables_missing(exc):
            raise LedgerError("the ledger tables are not in the database yet; run khipu migrate", "no_tables") from exc
        raise
    finally:
        if owned:
            conn.close()


def _import_locked(conn, project: str, src: dict[str, Any], report: dict[str, Any], *, dry_run: bool,
                   catch_up: bool) -> dict[str, Any]:
    """One transaction holding the project's ledger lock from the first read
    to the commit: what is here, the plan, the writes and the check all see
    the same ledger, and no other writer lands in between."""
    try:
        with conn.cursor() as cur:
            ledger.lock_project(cur, project)
            out = _import_in(cur, project, src, report, dry_run=dry_run, catch_up=catch_up)
        if out.pop("commit", False):
            conn.commit()
        else:
            conn.rollback()
        return out
    except _Mismatch as exc:
        conn.rollback()
        _record_rollback(conn, project, src, report)
        return {**report, "ok": False, "result": "rolled back", "reason": "mismatch",
                "words": exc.words + _differences(report["check"])}
    except BaseException:
        conn.rollback()
        raise


def _import_in(cur, project: str, src: dict[str, Any], report: dict[str, Any], *, dry_run: bool,
               catch_up: bool) -> dict[str, Any]:
    rows, ids = src["rows"], list(src["rows"])
    cur.execute("SELECT digest, result FROM ledger_imports WHERE project = %s AND result IN ('done', 'caught up') "
                "ORDER BY id DESC", (project,))
    landed = cur.fetchall()
    here, purged = _existing(cur, project, ids)
    existing = ledger._vocab(cur, project)
    if src["digest"] in {d for d, _r in landed}:
        return {**report, "result": "nothing new", "words": "Nothing new: every row and history entry is already here."}
    if catch_up and not landed:
        return {**report, "ok": False, "result": "refused", "reason": "no_import",
                "words": f"Nothing was imported into {project} yet, so there is nothing to catch up."}
    if catch_up:
        return _catch_up(cur, project, src, report, here, purged, existing, dry_run=dry_run)
    # A file imported before and brought again: with nothing new it is the
    # no-op a re-import must be; anything new waits for --catch-up.
    if landed and here:
        plan = _catch_up(cur, project, src, dict(report), here, purged, existing, dry_run=True)
        if plan["result"] == "nothing new":
            return plan
        taken = sorted(here)
        return {**report, "ok": False, "result": "refused", "reason": "exists", "existing_ids": taken,
                "words": f"Nothing was imported: these ids already exist in {project}: {_listed(taken)}. If this "
                         "tracker was imported before and changed since, bring the changes in with --catch-up; "
                         "otherwise choose a different project or a new one."}
    if purged:
        gone = sorted(purged)
        return {**report, "ok": False, "result": "refused", "reason": "purged", "purged_ids": gone,
                "words": "Nothing was imported: " + "; ".join(
                    f"row {i} was removed for good on {ledger._date_words(purged[i])}" for i in gone[:_SHOWN])}
    if here:
        taken = sorted(here)
        return {**report, "ok": False, "result": "refused", "reason": "exists", "existing_ids": taken,
                "words": f"Nothing was imported: these ids already exist in {project}: {_listed(taken)}. "
                         "Choose a different project or a new one."}
    vocab, added = _vocab_for(rows, existing)
    report["new_words"] = added
    order = _board_order(rows, vocab)
    if dry_run:
        return {**report, "result": "dry run", "hub_checked": True,
                "words": f"Ready to import {counts_words(report['counts_in_file'])}. Nothing was written."}
    from psycopg.types.json import Jsonb

    now = ledger._now()
    before = _snapshot(cur, project)
    _save_vocab(cur, project, vocab, added, not existing["state"])
    _insert_rows(cur, project, order, rows, now)
    cur.execute("INSERT INTO ledger_imports (project, file_name, digest, result) VALUES (%s, %s, %s, "
                "'in progress') RETURNING id", (project, src["name"], src["digest"]))
    import_id = cur.fetchone()[0]
    event = f"Imported from {src['name']} on {ledger._date_words(now)}"
    _insert_history(cur, project, [(i, _note_entry(event, now), f"{MARKER_OP}{import_id}") for i in order])
    check = _verify(cur, project, rows, report["counts_in_file"], before, order)
    report["check"] = check
    if not check["ok"]:
        raise _Mismatch("Nothing was imported: the check after writing found differences, so it was undone. ")
    cur.execute("UPDATE ledger_imports SET counts = %s, result = 'done' WHERE id = %s",
                (Jsonb({**report, "result": "done"}), import_id))
    ledger._mark_lag(project, order)
    return {**report, "result": "done", "import_id": import_id, "affected": order, "commit": True,
            "words": f"Imported {counts_words(report['counts_in_file'])}; every count matches and the round trip "
                     "gives the file back" + (" except the masked fields listed." if src["masked"] else ".")}


class _Mismatch(Exception):
    """The check after writing found a difference; the import is undone."""

    def __init__(self, words: str):
        super().__init__(words)
        self.words = words


def _record_rollback(conn, project: str, src: dict[str, Any], report: dict[str, Any]) -> None:
    try:
        _record(conn, project, src, {**report, "result": "rolled back"}, "rolled back")
        conn.commit()
    except Exception:  # noqa: BLE001  (the rollback itself already happened)
        conn.rollback()


def _differences(check: dict[str, Any]) -> str:
    parts = []
    if check.get("counts_differ"):
        parts.append("counts that differ: " + ", ".join(check["counts_differ"]))
    if check.get("fields_differ"):
        parts.append(f"{check['fields_differ_count']} field(s) that differ, such as "
                     + ", ".join(check["fields_differ"][:5]))
    return ("It found " + "; ".join(parts) + ".") if parts else ""


def counts_words(counts: dict[str, Any]) -> str:
    return f"{counts['rows']} rows and {counts['history']} history entries"


# ---- catching up --------------------------------------------------------------------


_FIELD_COLS = ("title", "kind", "parent", "state", "evidence", "owner")


def _catch_up(cur, project: str, src: dict[str, Any], report: dict[str, Any], here: dict[str, Any],
              purged: dict[str, Any], existing: dict[str, list], *, dry_run: bool) -> dict[str, Any]:
    """Bring in what the old file gained since the last import: new rows,
    history entries the ledger does not hold (matched exactly), and new field
    values. A row changed in the ledger too since Khipu last brought it in
    keeps the ledger's values, gets the file's entries as history, and is
    listed as changed in both places for the owner to settle; Khipu never
    picks a winner. Runs inside the import's locked transaction."""
    from psycopg.types.json import Jsonb

    rows = src["rows"]
    removed = sorted(i for i, at in here.items() if at is not None)
    gone = sorted(i for i in purged if i not in here)
    live = [i for i in rows if i in here and here[i] is None]
    new_ids = [i for i in rows if i not in here and i not in purged]
    now = ledger._now()
    plan: dict[str, Any] = {"new_rows": new_ids, "new_entries": 0, "updated": [], "both": [],
                            "skipped_removed": removed, "skipped_purged": gone}
    before = _snapshot(cur, project)
    current = before["rows"]
    # Changed here since Khipu last took the file's values for the row (the
    # first import's line, or a catch-up's). A "changed in both places"
    # line, or file entries brought in beside it, take nothing, so an
    # unresolved conflict keeps blocking updates until the owner settles it.
    cur.execute("SELECT DISTINCT h.row_id FROM ledger_history h WHERE h.project = %s AND h.row_id = ANY(%s) "
                "AND h.source <> 'import' AND h.id > coalesce((SELECT max(i.id) FROM ledger_history i "
                "WHERE i.project = h.project AND i.row_id = h.row_id AND i.source = 'import' "
                "AND (i.op_id LIKE %s OR i.op_id LIKE %s)), 0)",
                (project, live, f"{MARKER_OP}%", f"{CATCH_UP_OP}%"))
    touched = {r[0] for r in cur.fetchall()}
    cur.execute(f"SELECT row_id, coalesce(refs->'imported'->>'file', refs->>'{SEEN_REF}') FROM ledger_rows "
                "WHERE project = %s AND row_id = ANY(%s)", (project, live))
    seen_as = dict(cur.fetchall())
    adds: dict[str, list] = {}
    history_only: list[str] = []
    rewritten: list[str] = []
    for row_id in live:
        # An old tracker only appends history. The ledger's whole ordered
        # history from the file must be where the file's history begins;
        # what follows it is new. Anything else (an entry reordered, changed
        # or taken out) is refused, never matched up as a delta.
        had = [_entry_key(e) for e in (current.get(row_id) or {}).get("history") or []]
        theirs_hist = rows[row_id].get("history") or []
        if [_entry_key(e) for e in theirs_hist[:len(had)]] != had:
            rewritten.append(row_id)
            continue
        fresh = theirs_hist[len(had):]
        if fresh:
            adds[row_id] = fresh
            plan["new_entries"] += len(fresh)
        # The file changed this row when it no longer reads as it did at the
        # last import (or, with no record of that, as the ledger has it).
        theirs = _fields(rows[row_id])
        if seen_as.get(row_id):
            changed = _row_digest(rows[row_id]) != seen_as[row_id]
        else:
            changed = _fields(current.get(row_id)) != theirs
        if changed:
            (plan["both"] if row_id in touched else plan["updated"]).append(row_id)
        elif fresh and row_id in touched:
            plan["both"].append(row_id)
            history_only.append(row_id)
    if rewritten:
        return {**report, "ok": False, "result": "refused", "reason": "history_rewritten", "rewritten": rewritten,
                "words": "Nothing was brought in: in the old tracker, the history of these rows no longer starts "
                         "with what was brought in before (an entry was moved, changed or taken out), and Khipu "
                         f"only brings in entries added at the end: {_listed(rewritten)}."}
    plan["new_entries"] += sum(len(rows[i].get("history") or []) for i in new_ids)
    report["plan"] = plan
    if not (new_ids or plan["new_entries"] or plan["updated"] or plan["both"]):
        return {**report, "result": "nothing new", "words": "Nothing new: every row and history entry is already here."
                + _skipped_words(plan)}
    vocab, added = _vocab_for({i: rows[i] for i in new_ids + plan["updated"]}, existing)
    report["new_words"] = added
    if dry_run:
        return {**report, "result": "dry run", "hub_checked": True, "words": _plan_words(plan) + " Nothing was written."}
    affected = new_ids + list(adds) + [i for i in plan["updated"] + plan["both"] if i not in adds]
    _save_vocab(cur, project, vocab, added, False)
    if new_ids:
        _insert_rows(cur, project, _board_order({i: rows[i] for i in new_ids}, vocab), rows, now)
    for row_id, entries in adds.items():
        fallback = ledger._parse_time(rows[row_id].get("updated")) or now
        _insert_history(cur, project, [(row_id, _entry_columns(e, fallback), None) for e in entries])
    cur.execute("INSERT INTO ledger_imports (project, file_name, digest, result) VALUES (%s, %s, %s, "
                "'in progress') RETURNING id", (project, src["name"], src["digest"]))
    import_id = cur.fetchone()[0]
    for row_id in plan["updated"]:
        _bring_fields(cur, project, row_id, rows[row_id], now, f"{CATCH_UP_OP}{import_id}", src["name"])
    for row_id in plan["both"]:
        if row_id in history_only:
            event = ("Changed in both places: the old tracker has new history for this row, brought in above; "
                     "this ledger's values were kept. Settle it in the app.")
        else:
            event = (f"Changed in both places: the old tracker says {_word_of(rows[row_id].get('state'))}; this "
                     "ledger was not changed. Settle it in the app.")
        _insert_history(cur, project, [(row_id, _note_entry(event, now), f"{BOTH_OP}{import_id}")])
        # Seen now: the same change is not raised again by the next catch-up.
        # A row the ledger added itself has no import record to note it in.
        cur.execute("UPDATE ledger_rows SET refs = jsonb_set(refs, '{imported,file}', %s, true) "
                    "WHERE project = %s AND row_id = %s AND refs ? 'imported'",
                    (Jsonb(_row_digest(rows[row_id])), project, row_id))
        cur.execute("UPDATE ledger_rows SET refs = jsonb_set(refs, %s, %s, true) "
                    "WHERE project = %s AND row_id = %s AND NOT refs ? 'imported'",
                    ([SEEN_REF], Jsonb(_row_digest(rows[row_id])), project, row_id))
    check = _verify_catch_up(cur, project, rows, plan, adds, before)
    report["check"] = check
    if not check["ok"]:
        raise _Mismatch("Nothing was brought in: the check after writing found differences, so it was undone. ")
    cur.execute("UPDATE ledger_imports SET counts = %s, result = 'caught up' WHERE id = %s",
                (Jsonb({**report, "result": "caught up"}), import_id))
    ledger._mark_lag(project, affected)
    return {**report, "result": "caught up", "import_id": import_id, "affected": affected, "commit": True,
            "words": _plan_words(plan).replace("Would bring in", "Brought in", 1)}


def _bring_fields(cur, project: str, row_id: str, row: dict[str, Any], now: datetime, op_id: str,
                  file_name: str) -> None:
    """The file's values for a row nobody changed in the ledger since it was
    imported, with the old and new value of each in a history entry."""
    from psycopg.types.json import Jsonb

    before = ledger._lock_row(cur, project, row_id)
    cols, imported = _stored(row, now)
    changes = {f: {"old": before[f], "new": cols[f]} for f in _FIELD_COLS if before[f] != cols[f]}
    if ledger._z(before["updated_at"]) != ledger._z(cols["updated_at"]):
        changes["updated"] = {"old": ledger._z(before["updated_at"]), "new": ledger._z(cols["updated_at"])}
    refs = {**{k: v for k, v in (before["refs"] or {}).items() if k != "imported"},
            **({"imported": imported} if imported else {})}
    cur.execute("UPDATE ledger_rows SET title = %s, kind = %s, parent = %s, state = %s, owner = %s, evidence = %s, "
                "refs = %s, updated_at = %s, version = version + 1 WHERE project = %s AND row_id = %s",
                (cols["title"], cols["kind"], cols["parent"], cols["state"], cols["owner"], cols["evidence"],
                 Jsonb(refs), cols["updated_at"], project, row_id))
    entry = {"at": now, "at_text": ledger._z(now), "shape": "transition",
             "from_state": before["state"] if "state" in changes else None,
             "to_state": cols["state"] if "state" in changes else None, "changes": changes,
             "note": f"Brought in from {file_name}: the old tracker changed after it was imported"}
    _insert_history(cur, project, [(row_id, entry, op_id)])


def _verify_catch_up(cur, project: str, rows: dict[str, dict], plan: dict[str, Any], adds: dict[str, list],
                     before: dict[str, Any]) -> dict:
    """The whole project against what the catch-up meant to do, inside its
    transaction, every row live or removed: the rows now are the rows before
    plus the new ones; a new row reads back as the file has it, with no entry
    of Khipu's; a row the catch-up touched keeps its whole history as it was,
    in order, followed by exactly the file's new entries in the file's order
    and then one entry of Khipu's if it was updated and one if it changed in
    both places; an updated row reads as the file has it, any other keeps
    its values (a row changed in both places notes the file's version in
    ``refs``); every other row and its history read as they did."""
    after = _snapshot(cur, project)
    new = set(plan["new_rows"])
    touched = set(adds) | set(plan["updated"]) | set(plan["both"])
    differs = _ids_differ(before, after, new, "brought in")
    differs += _compare({i: rows[i] for i in plan["new_rows"]}, {i: after["rows"][i] for i in new
                                                                 if i in after["rows"]})
    differs += [f"{i}.history: entries of Khipu's, not 0" for i in sorted(new)
                if any(not h["has_raw"] for h in after["hist"].get(i, []))]
    for row_id in sorted(touched):
        had, has = before["hist"].get(row_id, []), after["hist"].get(row_id, [])
        tail = has[len(had):]
        added = [_entry_key(e) for e in adds.get(row_id, [])]
        ops = [CATCH_UP_OP] * (row_id in plan["updated"]) + [BOTH_OP] * (row_id in plan["both"])
        if has[:len(had)] != had:
            differs.append(f"{row_id}.history: the entries it had changed")
        elif [_entry_key(h["raw"]) for h in tail[:len(added)] if h["has_raw"]] != added \
                or len(tail) != len(added) + len(ops) \
                or any(h["has_raw"] or not (h["op_id"] or "").startswith(op) for h, op in zip(tail[len(added):], ops)):
            differs.append(f"{row_id}.history: not the file's {len(added)} new entries in order and {len(ops)} of "
                           "Khipu's")
        was, now_row = before["all"][row_id], after["all"].get(row_id)
        if now_row is None:
            continue
        if row_id in plan["updated"]:
            fixed = ("seq", "created_at", "link", "removed_at", "removed_by", "removed_note")
            if any(was[k] != now_row[k] for k in fixed) or _fields(after["rows"].get(row_id)) != _fields(rows[row_id]):
                differs.append(f"{row_id}: fields")
            continue
        def plain(row: dict[str, Any]) -> dict[str, Any]:
            refs = dict(row["refs"] or {})
            if row_id in plan["both"] and isinstance(refs.get("imported"), dict):
                refs["imported"] = {k: v for k, v in refs["imported"].items() if k != "file"}
            if row_id in plan["both"]:
                refs.pop(SEEN_REF, None)
            return {**row, "refs": refs}
        if plain(was) != plain(now_row):
            differs.append(f"{row_id}: fields")
    # Every live row the file has, touched or not: its whole history from
    # the file reads back as the file lists it, every entry in order.
    for row_id in sorted(i for i in rows if i in after["rows"]):
        if [_entry_key(e) for e in after["rows"][row_id].get("history") or []] \
                != [_entry_key(e) for e in rows[row_id].get("history") or []]:
            differs.append(f"{row_id}.history: not the file's entries in the file's order")
    differs += _unchanged(before, after, new | touched)
    return {"ok": not differs, "fields_differ": differs[:_SHOWN], "fields_differ_count": len(differs),
            "counts_differ": []}


def _skipped_words(plan: dict[str, Any]) -> str:
    out = ""
    if plan["skipped_removed"]:
        out += f" Not brought back (removed in the ledger): {_listed(plan['skipped_removed'])}."
    if plan["skipped_purged"]:
        out += f" Not brought back (removed for good): {_listed(plan['skipped_purged'])}."
    return out


def _plan_words(plan: dict[str, Any]) -> str:
    out = (f"Would bring in {len(plan['new_rows'])} new row(s), {plan['new_entries']} new history entries and "
           f"{len(plan['updated'])} changed row(s).")
    if plan["both"]:
        out += f" Changed in both places, for you to settle in the app: {_listed(plan['both'])}."
    return out + _skipped_words(plan)


# ---- the CLI's words -------------------------------------------------------------------


def report_text(report: dict[str, Any]) -> str:
    counts = report["counts_in_file"]
    lines = [f"{report['file']} -> {report['project']}: {report['result']}", report.get("words", ""), "",
             f"In the file: {counts['rows']} rows, {counts['history']} history entries, "
             f"{counts['with_parent']} with a parent",
             "  by state: " + ", ".join(f"{k}={v}" for k, v in counts["by_state"].items()),
             "  by kind: " + ", ".join(f"{k}={v}" for k, v in counts["by_kind"].items())]
    if counts["missing_parent"]:
        lines.append(f"  parent not found: {_listed(counts['missing_parent'])}")
    if report["masked"]:
        lines.append(f"Masked because they look like a key or password: {_listed(report['masked'])}")
    if report.get("other_keys"):
        lines.append("Not rows, so not imported: " + ", ".join(report["other_keys"]))
    added = report.get("new_words") or {}
    if added.get("state") or added.get("kind"):
        lines.append("Words new to this project: " + ", ".join(
            [f"{w} (counts as {m})" for w, m in added.get("state", [])] + list(added.get("kind", []))))
    return "\n".join(lines)
