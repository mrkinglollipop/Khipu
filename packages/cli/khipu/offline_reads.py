"""Owed and decision reads from this Mac's replica, for when the hub is away
(offline and self-healing scope, part C).

Each returns ``None`` when there is nothing to read (no replica, or one made
before the table existed): the caller then reports the hub's own error. Rows
of a forgotten episode, or one whose erasure is still queued, are left out
through the one replica forget fence (``hub_snapshot.forget_fence_sql``).
The payload carries the stale marker MCP search uses
(``hub_snapshot.stale_fields``).
"""
from __future__ import annotations

import sqlite3
from typing import Any

from khipu import hub_snapshot


def _open() -> sqlite3.Connection | None:
    path = hub_snapshot.snapshot_path()
    if not path.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    con.row_factory = sqlite3.Row
    return con


def owed(project: str | None, status: str, limit: int) -> dict[str, Any] | None:
    from khipu import commitments

    con = _open()
    if con is None:
        return None
    try:
        if not hub_snapshot._snapshot_table_columns(con, "commitments"):
            return None
        sql = "SELECT * FROM commitments WHERE status = ?"
        params: list[Any] = [status]
        if project:
            sql += " AND project = ?"
            params.append(project)
        sql += hub_snapshot.forget_fence_sql(con, "commitments", "opened_episode")
        rows = [dict(r) for r in con.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return None
    finally:
        con.close()
    for row in rows:
        for col in ("opened_at", "due_after", "closed_at", "last_seen_at"):
            row[col] = hub_snapshot._utc(row.get(col))
        row["future_trigger"] = bool(row.get("future_trigger"))
        row["stale"] = True
    results = commitments.finish_owed_rows(rows, limit)
    return {"status": status, "project": project or None, "results": results, **hub_snapshot.stale_fields()}


def decisions(project: str | None, since: Any, status: str, limit: int) -> dict[str, Any] | None:
    from khipu import decisions as _decisions

    con = _open()
    if con is None:
        return None
    base = {"status": status, "project": project}
    if status == "conflicts":
        return {**base, "results": [], "note": "Decision conflicts need the hub; none can be listed from this Mac's copy.",
                **hub_snapshot.stale_fields()}
    try:
        cols = hub_snapshot._snapshot_table_columns(con, "decisions")
        if not cols:
            return None
        sql = "SELECT * FROM decisions WHERE 1 = 1"
        params: list[Any] = []
        if project:
            sql += " AND project = ?"
            params.append(project)
        sql += hub_snapshot.forget_fence_sql(con, "decisions", "episode_id")
        rows = [dict(r) for r in con.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return None
    finally:
        con.close()
    since_dt = hub_snapshot._utc(since) if since is not None else None
    out = []
    for row in rows:
        if since_dt is not None:
            decided = hub_snapshot._utc(row.get("decided_at"))
            if decided is None or decided < since_dt:
                continue
        row["state"] = _decisions.state_of(row)
        if status == "standing" and row["state"] != "current":
            continue
        if status == "superseded" and not row.get("superseded_by"):
            continue
        if status == "retracted" and row["state"] != "retracted":
            continue
        row["stale"] = True
        out.append(row)
    out.sort(key=lambda r: (hub_snapshot._utc(r.get("decided_at")) or hub_snapshot._utc("1970-01-01T00:00:00Z"), r["id"]),
             reverse=True)
    return {**base, "results": out[:limit], **hub_snapshot.stale_fields()}
