"""`khipu hygiene blank-field`: empty one stored field on listed episodes, with
a backup taken first and an exact undo (owner clean-ups of 2026-11-10/11).

Two fields, each emptied in every place an episode stores it on the hub:

* ``preferences``: the ``episodes.preferences`` column, ``raw.preferences``
  (``raw`` is the stored capture payload) and ``preferences`` in each capture
  merged into the row (``raw.merged_from[].payload``). Each becomes ``[]``.
* ``owner_lines``: the key ``verbatim.owner_lines`` is removed from the
  ``episodes.verbatim`` column ("only my words" searches it), from
  ``raw.verbatim`` and from each merged capture's ``payload.verbatim``.
  ``verbatim.quotes`` and every other key stay.

Every JSON edit is a jsonb operator in SQL, so the keys left alone stay
byte-equal (no Python round trip of numbers). The command selects nothing of
its own: the ids come from ``--ids-file``. A forgotten, missing or already
empty row is skipped and counted. Without ``--apply`` nothing changes.

``--apply`` writes the backup file (refusing an existing one), reads it back
and checks it, then changes the rows, all inside one transaction that rolls
back on any error. The backup holds, per changed row, the exact old text of
each changed column and a hash of the text written, so ``--restore`` puts the
old value back only where the column still holds exactly what the run wrote.

How the change spreads: the hub's 0032 trigger moves ``changed_at`` on the
UPDATE, and every Mac's replica pull reads rows by ``changed_at``, so each
replica takes the new row on its next pull (its FTS follows by trigger).
Vectors built from the old preferences text keep it in ``chunk_text`` until
``khipu embed backfill`` (the nightly runs it) re-embeds the changed chunks;
the output counts those rows as ``vectors_to_refresh``. Owner lines are never
embedded. Nothing here prints a stored string.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FIELDS = ("preferences", "owner_lines")
BACKUP_KIND = "khipu-blank-field-backup"
BACKUP_VERSION = 1


class BlankFieldError(Exception):
    """A refusal or failure the command reports as one JSON line."""


# ---- SQL: the blanked form of each place -------------------------------------

def _payload_sql(p: str, field: str) -> str:
    """``p`` (a jsonb expression for one capture payload) with ``field`` emptied."""
    if field == "preferences":
        return (f"(CASE WHEN jsonb_typeof({p}) = 'object' AND ({p}) ? 'preferences' "
                f"THEN jsonb_set({p}, '{{preferences}}', '[]'::jsonb) ELSE {p} END)")
    return (f"(CASE WHEN jsonb_typeof({p}) = 'object' AND jsonb_typeof(({p})->'verbatim') = 'object' "
            f"THEN ({p}) #- '{{verbatim,owner_lines}}' ELSE {p} END)")


def _raw_sql(r: str, field: str) -> str:
    """``raw`` with ``field`` emptied at the top and in every merged capture."""
    top = _payload_sql(r, field)
    entry = _payload_sql("(e->'payload')", field)
    merged = ("(SELECT COALESCE(jsonb_agg(CASE WHEN jsonb_typeof(e) = 'object' "
              f"AND jsonb_typeof(e->'payload') = 'object' THEN jsonb_set(e, '{{payload}}', {entry}) "
              f"ELSE e END ORDER BY i), '[]'::jsonb) FROM jsonb_array_elements(({r})->'merged_from') "
              "WITH ORDINALITY AS t(e, i))")
    return (f"(CASE WHEN jsonb_typeof({r}) = 'object' AND jsonb_typeof(({r})->'merged_from') = 'array' "
            f"THEN jsonb_set({top}, '{{merged_from}}', {merged}) ELSE {top} END)")


def _new_sql(column: str, field: str) -> str:
    if column == "raw":
        return _raw_sql("raw", field)
    if column == "preferences":
        return "'[]'::jsonb"
    # verbatim, for owner_lines
    return ("(CASE WHEN jsonb_typeof(verbatim) = 'object' THEN verbatim - 'owner_lines' "
            "ELSE verbatim END)")


def _columns(cur, field: str) -> list[str]:
    from khipu.db import has_columns

    want = ["preferences", "raw"] if field == "preferences" else ["verbatim", "raw"]
    return [c for c in want if has_columns(cur, "episodes", c)]


# ---- what a row holds ----------------------------------------------------------

def _items(value: Any) -> list[Any]:
    return [v for v in value if v not in (None, "")] if isinstance(value, list) else []


def _payload_items(payload: Any, field: str) -> list[Any]:
    if not isinstance(payload, dict):
        return []
    if field == "preferences":
        return _items(payload.get("preferences"))
    verbatim = payload.get("verbatim")
    return _items(verbatim.get("owner_lines")) if isinstance(verbatim, dict) else []


def _held_strings(field: str, old: dict[str, str | None]) -> int:
    """How many distinct values of ``field`` the row holds across its places.
    Read only to count; the write never goes through Python's JSON."""
    found: list[Any] = []
    for column, text in old.items():
        value = json.loads(text) if text is not None else None
        if column == "preferences":
            found += _items(value)
        elif column == "verbatim":
            found += _items(value.get("owner_lines")) if isinstance(value, dict) else []
        elif column == "raw":
            found += _payload_items(value, field)
            merged = value.get("merged_from") if isinstance(value, dict) else None
            for entry in merged if isinstance(merged, list) else ():
                if isinstance(entry, dict):
                    found += _payload_items(entry.get("payload"), field)
    return len({json.dumps(v, sort_keys=True, ensure_ascii=False) for v in found})


def _sha(text: str | None) -> str | None:
    return None if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_ids(path: str | os.PathLike) -> list[int]:
    """One episode id per line; blank lines ignored; anything else refuses."""
    ids: list[int] = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BlankFieldError(f"cannot read the ids file: {exc.strerror or exc}") from exc
    for n, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        if not line.isdigit():
            raise BlankFieldError(f"ids file line {n} is not an episode id")
        ids.append(int(line))
    return list(dict.fromkeys(ids))


def _announce(cur, raw_text: str | None) -> None:
    """Announce the source identities of the receipts ``raw`` names, for the
    0047 update check: a ``merged_from`` entry that is not contained in the
    old one reads as a new merge, which must be announced and is refused when
    any of its identities is forgotten. A hub without 0047 is not asked."""
    if raw_text is None:
        return
    cur.execute("SELECT to_regproc('khipu_raw_receipt_identities') IS NOT NULL")
    if not cur.fetchone()[0]:
        return
    cur.execute("SELECT set_config('khipu.source_identities', "
                "array_to_string(khipu_raw_receipt_identities(%s::jsonb), ','), true)", (raw_text,))


def _payloads(raw_text: str | None) -> list[dict[str, Any]]:
    """The capture payload ``raw`` holds and each capture merged into it."""
    raw = json.loads(raw_text) if raw_text is not None else None
    if not isinstance(raw, dict):
        return []
    merged = raw.get("merged_from")
    return [raw] + [e["payload"] for e in (merged if isinstance(merged, list) else ())
                    if isinstance(e, dict) and isinstance(e.get("payload"), dict)]


def _hub_identity(conn) -> str:
    """The hub's incarnation (cluster, timeline, database), as the replica
    copy records it; read in its own transaction before the work starts."""
    from khipu.hub_snapshot import _INCARNATION_READ

    try:
        with conn.cursor() as cur:
            cur.execute(_INCARNATION_READ)
            value = cur.fetchone()[0]
        conn.rollback()
        return str(value)
    except Exception:  # noqa: BLE001 - the identity is a label; an unreadable one is said so
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            name = cur.fetchone()[0]
        conn.rollback()
        return f"unknown-incarnation:{name}"


# ---- the backup file -----------------------------------------------------------

def write_backup(path: Path, backup: dict[str, Any]) -> None:
    """Create ``path`` (never over an existing file), flush it to disk, read it
    back and check it equals ``backup``. Raises on any failure, removing a
    file it created that did not check out."""
    data = json.dumps(backup, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise BlankFieldError("the backup file already exists; choose a new path") from exc
    except OSError as exc:
        raise BlankFieldError(f"cannot create the backup file: {exc.strerror or exc}") from exc
    try:
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            back = json.loads(Path(path).read_bytes().decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise BlankFieldError(f"the backup could not be written and read back: {exc}") from exc
        if back != backup:
            raise BlankFieldError("the backup read back differs from what was written")
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def read_backup(path: str | os.PathLike) -> dict[str, Any]:
    try:
        backup = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BlankFieldError(f"cannot read the backup file: {exc}") from exc
    ok = (isinstance(backup, dict) and backup.get("kind") == BACKUP_KIND
          and backup.get("version") == BACKUP_VERSION and backup.get("field") in FIELDS
          and isinstance(backup.get("rows"), list)
          and all(isinstance(r, dict) and isinstance(r.get("id"), int) and isinstance(r.get("old"), dict)
                  and isinstance(r.get("new_sha256"), dict) and set(r["old"]) == set(r["new_sha256"])
                  and set(r["old"]) <= {"preferences", "raw", "verbatim"}
                  for r in backup["rows"]))
    if not ok:
        raise BlankFieldError("the backup file is not a blank-field backup")
    return backup


# ---- blank ----------------------------------------------------------------------

def blank(conn, field: str, ids: list[int], *, backup_path: Path | None, apply: bool) -> dict[str, Any]:
    # forget-fence: owner maintenance writer; reads listed ids to empty one field, returns counts only
    """Dry run (``apply`` False) or the change. Commits on success, rolls back
    on any error (re-raised). Returns counts, never a stored string."""
    if field not in FIELDS:
        raise BlankFieldError(f"--field must be one of {', '.join(FIELDS)}")
    if apply and backup_path is None:
        raise BlankFieldError("--apply needs --backup")
    if apply and backup_path.exists():
        raise BlankFieldError("the backup file already exists; choose a new path")
    hub = _hub_identity(conn) if apply else None
    out: dict[str, Any] = {"ok": True, "field": field, "dry_run": not apply, "ids_given": len(ids)}
    try:
        with conn.cursor() as cur:
            cols = _columns(cur, field)
            if not cols:
                raise BlankFieldError("this hub stores no such field")
            from khipu.db import has_columns

            deleted = "deleted_at" if has_columns(cur, "episodes", "deleted_at") else "NULL::timestamptz"
            sel = ", ".join([f"{c}::text, ({_new_sql(c, field)})::text" for c in cols])
            cur.execute(f"SELECT id, {deleted} IS NOT NULL, {sel} FROM episodes "
                        "WHERE id = ANY(%s) ORDER BY id FOR UPDATE", (ids,))
            rows = cur.fetchall()
            found = {int(r[0]) for r in rows}
            forgotten = sum(1 for r in rows if r[1])
            change: list[dict[str, Any]] = []
            strings = 0
            for r in rows:
                if r[1]:
                    continue
                old = {c: r[2 + 2 * i] for i, c in enumerate(cols)}
                new = {c: r[3 + 2 * i] for i, c in enumerate(cols)}
                held = _held_strings(field, old)
                if not held:
                    continue
                strings += held
                moved = [c for c in cols if old[c] != new[c]]
                change.append({"id": int(r[0]), "old": {c: old[c] for c in moved},
                               "new": {c: new[c] for c in moved}})
            out.update({
                "live": len(rows) - forgotten, "holding": len(change), "strings": strings,
                "skipped_missing": len(set(ids) - found), "skipped_forgotten": forgotten,
                "skipped_empty": len(rows) - forgotten - len(change),
            })
            if field == "preferences" and change:
                cur.execute("SELECT count(DISTINCT ref) FROM memory_embeddings WHERE kind = 'episode' "
                            "AND ref = ANY(%s)", ([str(c["id"]) for c in change],))
                out["vectors_to_refresh"] = int(cur.fetchone()[0])
            if not apply:
                conn.rollback()
                return out
            if change:
                write_backup(backup_path, {
                    "kind": BACKUP_KIND, "version": BACKUP_VERSION, "field": field, "hub": hub,
                    "at": datetime.now(timezone.utc).isoformat(),
                    "rows": [{"id": c["id"], "old": c["old"],
                              "new_sha256": {k: _sha(v) for k, v in c["new"].items()}} for c in change],
                })
                for c in change:
                    _announce(cur, c["new"].get("raw"))
                    sets = ", ".join(f"{k} = %s::jsonb" for k in c["new"])
                    cur.execute(f"UPDATE episodes SET {sets} WHERE id = %s",
                                (*c["new"].values(), c["id"]))
                    if cur.rowcount != 1:
                        raise BlankFieldError("a row changed under the run; nothing was changed")
                out["backup"] = str(backup_path)
            out["changed"] = len(change)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return out


# ---- restore --------------------------------------------------------------------

def restore(conn, backup_path: str | os.PathLike, *, apply: bool) -> dict[str, Any]:
    # forget-fence: owner maintenance writer; puts back backed-up values on listed ids, returns counts only
    """Put back exactly what the backup holds, on rows that still hold what
    the run wrote (by hash, column by column) and are not forgotten."""
    from khipu import memory_lock

    backup = read_backup(backup_path)
    # Shared through the commit, like every gated writer: never interleaves
    # with a forget on this Mac.
    with memory_lock.shared():
        return _restore_locked(conn, backup, apply=apply)


def _restore_locked(conn, backup: dict[str, Any], *, apply: bool) -> dict[str, Any]:
    # forget-fence: owner maintenance writer; runs inside restore's memory lock, returns counts only
    from khipu.tombstones import hub_source_forgotten

    hub = _hub_identity(conn)
    if not str(backup.get("hub") or "").startswith("unknown-") and not hub.startswith("unknown-") \
            and backup.get("hub") != hub:
        raise BlankFieldError("the backup was taken on another hub; nothing was changed")
    rows = {int(r["id"]): r for r in backup["rows"]}
    out: dict[str, Any] = {"ok": True, "restore": True, "field": backup["field"], "dry_run": not apply,
                           "rows_in_backup": len(rows)}
    try:
        with conn.cursor() as cur:
            from khipu.db import has_columns

            cols = [c for c in ("preferences", "raw", "verbatim") if has_columns(cur, "episodes", c)]
            deleted = "deleted_at" if has_columns(cur, "episodes", "deleted_at") else "NULL::timestamptz"
            sel = ", ".join(f"{c}::text" for c in cols)
            cur.execute(f"SELECT id, {deleted} IS NOT NULL, {sel} FROM episodes "
                        "WHERE id = ANY(%s) ORDER BY id FOR UPDATE", (list(rows),))
            current = {int(r[0]): (r[1], dict(zip(cols, r[2:], strict=True))) for r in cur.fetchall()}
            put: list[dict[str, Any]] = []
            missing = forgotten = changed = 0
            for eid, entry in rows.items():
                if eid not in current:
                    missing += 1
                    continue
                gone, now = current[eid]
                if gone:
                    forgotten += 1
                    continue
                if any(c not in now or _sha(now[c]) != entry["new_sha256"][c] for c in entry["old"]):
                    changed += 1
                    continue
                # This Mac's forget list or the hub's forgotten_sources.
                if any(hub_source_forgotten(cur, p) for p in _payloads(entry["old"].get("raw"))):
                    forgotten += 1
                    continue
                put.append(entry)
            out.update({"restorable": len(put), "skipped_missing": missing,
                        "skipped_forgotten": forgotten, "skipped_changed_since": changed})
            if not apply:
                conn.rollback()
                return out
            for entry in put:
                _announce(cur, entry["old"].get("raw"))
                sets = ", ".join(f"{k} = %s::jsonb" for k in entry["old"])
                cur.execute(f"UPDATE episodes SET {sets} WHERE id = %s", (*entry["old"].values(), entry["id"]))
            out["restored"] = len(put)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return out


def main(args) -> int:
    """The CLI entry: prints one JSON line. 0 on success, 2 on a refusal,
    1 on an error (rolled back)."""
    from khipu.db import connect

    apply = bool(getattr(args, "apply", False))
    try:
        if getattr(args, "restore", None):
            with connect() as conn:
                out = restore(conn, args.restore, apply=apply)
        else:
            if not getattr(args, "field", None) or not getattr(args, "ids_file", None):
                raise BlankFieldError("usage: khipu hygiene blank-field --field preferences|owner_lines "
                                      "--ids-file FILE --backup FILE [--apply] | --restore FILE [--apply]")
            ids = read_ids(args.ids_file)
            backup = Path(args.backup) if getattr(args, "backup", None) else None
            with connect() as conn:
                out = blank(conn, args.field, ids, backup_path=backup, apply=apply)
    except BlankFieldError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "changed": 0}))
        return 2
    except Exception as exc:  # noqa: BLE001 - reported as one line; the transaction rolled back
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}; rolled back, nothing changed",
                          "changed": 0}))
        return 1
    print(json.dumps(out))
    return 0
