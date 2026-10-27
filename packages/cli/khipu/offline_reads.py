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

import json
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

    rows = owed_rows(project, status)
    if rows is None:
        return None
    results = commitments.finish_owed_rows(rows, limit)
    return {"status": status, "project": project or None, "results": results, **hub_snapshot.stale_fields()}


def owed_canonical(project: str | None) -> str | None:
    """The canonical name the replica reads ``project`` as (``project`` itself
    with no alias, or when the alias table is missing). The same resolution
    :func:`owed_rows` labels its rows with, for the listing's top-level field."""
    if not project:
        return project
    con = _open()
    if con is None:
        return project
    try:
        return hub_snapshot.snapshot_project_group(con, project)[0] or project
    finally:
        con.close()


def owed_resolve_project(project: str | None) -> str | None:
    """``project`` as the replica's commitments name it (a short name resolved);
    unchanged with no replica. Raises ``identity.AmbiguousProject``."""
    if not project:
        return project
    con = _open()
    if con is None:
        return project
    try:
        return hub_snapshot.snapshot_resolve_project(con, project)
    finally:
        con.close()


def _cited_forgotten(con, rows: list[dict[str, Any]]) -> set[Any]:
    """Ids of ``rows`` whose ``settle_suggestion`` cites, as its evidence, an
    episode the replica holds forgotten or queued for erasure, or a decision
    of one, or cites a decision without naming its source episode: that
    suggestion quotes forgotten (or unverifiable) words, so it is not shown
    (``commitments.settle_suggestion_sql`` on the hub)."""
    rejected = hub_snapshot._rejected_episode_ids(con)
    has_decisions = {"id", "episode_id"} <= hub_snapshot._snapshot_table_columns(con, "decisions")
    out: set[Any] = set()
    for row in rows:
        try:
            doc = json.loads(row.get("settle_suggestion") or "null")
        except (TypeError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        evidence = doc.get("evidence") if isinstance(doc.get("evidence"), dict) else {}
        cited = {doc.get("decision_id"), evidence.get("decision_id")} - {None}
        episodes = {doc.get("episode")} - {None}
        if cited and not episodes:
            # Quotes a decision without naming its source episode: unverifiable.
            out.add(row.get("id"))
            continue
        if not rejected:
            continue
        if cited and has_decisions:
            marks = ", ".join("?" * len(cited))
            episodes |= {r[0] for r in con.execute(
                f"SELECT episode_id FROM decisions WHERE CAST(id AS TEXT) IN ({marks})",
                [str(c) for c in cited])}
        try:
            if {int(e) for e in episodes if e is not None} & rejected:
                out.add(row.get("id"))
        except (TypeError, ValueError):
            out.add(row.get("id"))
    return out


def owed_rows(project: str | None, status: str) -> list[dict[str, Any]] | None:
    """The replica's raw commitment rows of one scope (owners not yet
    resolved), for ``owed`` and the paged read (``commitments.page_owed``)."""
    con = _open()
    if con is None:
        return None
    try:
        if not hub_snapshot._snapshot_table_columns(con, "commitments"):
            return None
        sql = "SELECT * FROM commitments WHERE status = ?"
        params: list[Any] = [status]
        canonical = None
        if project:
            # An aliased key reads as its canonical project, as on the hub.
            canonical, group = hub_snapshot.snapshot_project_group(con, project)
            sql += f" AND project IN ({', '.join('?' * len(group))})"
            params.extend(group)
        sql += hub_snapshot.forget_fence_sql(con, "commitments", "opened_episode")
        rows = [dict(r) for r in con.execute(sql, params).fetchall()]
        gone = _cited_forgotten(con, rows)
        if canonical:
            for row in rows:
                if row.get("project"):
                    row["project"] = canonical
    except sqlite3.Error:
        return None
    finally:
        con.close()
    for row in rows:
        for col in ("opened_at", "due_after", "closed_at", "last_seen_at"):
            row[col] = hub_snapshot._utc(row.get(col))
        if row.get("owner_reassigned_at"):
            row["owner_reassigned_at"] = hub_snapshot._utc(row["owner_reassigned_at"])
        for col in ("linked_ids", "prs", "settle_suggestion", "pr_check"):
            if col not in row:
                continue
            try:
                value = json.loads(row[col]) if row[col] is not None else None
                if col in ("linked_ids", "prs"):
                    row[col] = [int(v) for v in value] if isinstance(value, list) else None
                else:
                    row[col] = value if isinstance(value, dict) else None
            except (TypeError, ValueError):
                row[col] = None
            if col == "linked_ids" and row[col] is None:
                row[col] = []
        if row.get("id") in gone:
            row["settle_suggestion"] = None
        row["future_trigger"] = bool(row.get("future_trigger"))
        row["stale"] = True
    return rows


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
        canonical = None
        if project:
            canonical, group = hub_snapshot.snapshot_project_group(con, project)
            sql += f" AND project IN ({', '.join('?' * len(group))})"
            params.extend(group)
        sql += hub_snapshot.forget_fence_sql(con, "decisions", "episode_id")
        rows = [dict(r) for r in con.execute(sql, params).fetchall()]
        if canonical:
            for row in rows:
                if row.get("project"):
                    row["project"] = canonical
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
