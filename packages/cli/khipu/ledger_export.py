"""``ledger export --tracker-json``: a project's live rows and their history
in the old tracker file's shape.

Rows that were once brought in from an old tracker keep, in
``refs["imported"]``, what the ledger's columns could not hold, and the
export gives each such row back as it was. The importer is gone; those rows
and their notes stay.
"""
from __future__ import annotations

from typing import Any

from khipu import ledger

_TEXT_FIELDS = ("title", "kind", "parent", "state", "evidence", "owner")
# Entries Khipu wrote during an import carry an op id with this prefix; they
# are Khipu's own record, never part of the old file.
MARKER_OP = "import:"
# Khipu's own note on a row the old file also holds: never given back.
SEEN_REF = "tracker_seen"


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
