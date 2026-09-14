"""Decisions registry (O2) — decisions that outlive the episode.

``episodes.decisions`` is an immutable JSONB array of strings today: nothing
can carry a date, a rationale, or say a later decision replaced an earlier
one (root cause O2, 36,967 decision strings across 6,815 episodes on the
live hub with none of that). This module gives each decision string its own
row (migration 0019) with a lifecycle: opened at capture, listed, and
optionally superseded by a later one.

Every write here is called from the same SAVEPOINT-guarded capture step
``khipu.commitments`` uses (``khipu.capture.write_pg`` / ``_merge_into_episode``)
and must stay fail-open: an exception here must never take down an episode
insert that already succeeded.
"""
from __future__ import annotations

import re
from typing import Any

DEDUP_WINDOW_DAYS = 30


def _log(msg: str) -> None:
    import sys

    print(f"[khipu-decisions] {msg}", file=sys.stderr)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _decisions_ready(cur) -> bool:
    """True when migration 0019 (the ``decisions`` table) has been applied.

    Fail-closed, same posture as ``khipu.commitments``'s readiness checks: a
    pre-migration hub simply skips this step instead of raising mid-capture.
    """
    try:
        from khipu.db import has_columns

        return has_columns(cur, "decisions", "id", "project", "text", "decided_at")
    except Exception:  # noqa: BLE001 — introspection is best-effort
        return False


def _find_recent_duplicate(cur, project: str | None, norm_text: str, decided_at: Any) -> int | None:
    """id of an existing decision in ``project`` with the same normalised text,
    decided within ``DEDUP_WINDOW_DAYS`` before (or at) ``decided_at`` — or
    None. A reversal restated in the same conversation, or a fact repeated
    across nearby captures, is one decision, not a growing pile of identical
    rows."""
    cur.execute(
        f"""
        SELECT id FROM decisions
        WHERE project IS NOT DISTINCT FROM %s
          AND lower(regexp_replace(text, '\\s+', ' ', 'g')) = %s
          AND decided_at >= COALESCE(%s::timestamptz, now()) - interval '{DEDUP_WINDOW_DAYS} days'
          AND decided_at <= COALESCE(%s::timestamptz, now())
        LIMIT 1
        """,
        (project, norm_text, decided_at, decided_at),
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


def insert_decisions_from_episode(cur, payload: dict[str, Any], episode_id: int) -> int:
    """One row per string in ``payload['decisions']``. Returns the count
    actually inserted.

    ``project`` is the episode's own resolved project (NULL stays NULL — no
    session-id fallback here, same K6 posture as commitments' scope). Dedup
    is by normalised text within that project, in a 30-day window ending at
    this capture's own ``ts`` (never "now" — a backfill mints historical
    ``ts`` values and must dedup against ITS OWN neighbourhood, not today).
    """
    items = payload.get("decisions") or []
    if not isinstance(items, list) or not items:
        return 0
    if not _decisions_ready(cur):
        return 0
    project = payload.get("project")
    session_id = payload.get("session_id")
    decided_at = payload.get("ts")
    inserted = 0
    for raw in items:
        text = str(raw or "").strip()
        if not text:
            continue
        norm = normalize_text(text)
        if not norm:
            continue
        if _find_recent_duplicate(cur, project, norm, decided_at) is not None:
            continue
        cur.execute(
            """
            INSERT INTO decisions (project, text, episode_id, session_id, decided_at)
            VALUES (%s, %s, %s, %s, COALESCE(%s::timestamptz, now()))
            """,
            (project, text, episode_id, session_id, decided_at),
        )
        if cur.rowcount > 0:
            inserted += 1
    if inserted:
        _log(f"recorded {inserted} decision(s) for episode {episode_id} (project={project!r})")
    return inserted


def list_decisions(cur, *, project: str | None = None, since: Any = None,
                    limit: int = 50, include_superseded: bool = True) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if project:
        clauses.append("project = %s")
        params.append(project)
    if since is not None:
        clauses.append("decided_at >= %s")
        params.append(since)
    if not include_superseded:
        clauses.append("superseded_by IS NULL")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    cur.execute(
        f"""
        SELECT id, project, text, rationale, decided_at, episode_id, session_id,
               superseded_by, created_at
        FROM decisions
        {where}
        ORDER BY decided_at DESC
        LIMIT %s
        """,
        params,
    )
    cols = ("id", "project", "text", "rationale", "decided_at", "episode_id",
            "session_id", "superseded_by", "created_at")
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def supersede(cur, old_id: int, new_id: int) -> bool:
    """Mark ``old_id`` superseded by ``new_id``. A reversal or correction
    then outranks the original in every reader instead of sitting beside it
    forever (O2's root complaint)."""
    cur.execute(
        "UPDATE decisions SET superseded_by = %s WHERE id = %s AND superseded_by IS NULL",
        (new_id, old_id),
    )
    return cur.rowcount > 0


def enrich_search_results(cur, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Additive ``decisions_current`` / ``decisions_superseded`` counts on
    episode-kind search rows. Never raises; a pre-migration hub or a row with
    no matching decisions simply gets zeros."""
    episode_ids: list[int] = []
    for r in results:
        if r.get("kind") == "episode":
            try:
                episode_ids.append(int(r.get("id")))
            except (TypeError, ValueError):
                continue
    out = [dict(r) for r in results]
    if not episode_ids or not _decisions_ready(cur):
        return out
    try:
        cur.execute(
            """
            SELECT episode_id,
                   COUNT(*) FILTER (WHERE superseded_by IS NULL) AS current,
                   COUNT(*) FILTER (WHERE superseded_by IS NOT NULL) AS superseded
            FROM decisions
            WHERE episode_id = ANY(%s)
            GROUP BY episode_id
            """,
            (episode_ids,),
        )
        counts = {int(eid): (int(cur_n), int(sup_n)) for eid, cur_n, sup_n in cur.fetchall()}
    except Exception as exc:  # noqa: BLE001 — enrichment is additive, never a search failure
        _log(f"decisions enrichment failed ({type(exc).__name__}: {exc})")
        return out
    for row in out:
        if row.get("kind") != "episode":
            continue
        try:
            eid = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        cur_n, sup_n = counts.get(eid, (0, 0))
        row["decisions_current"] = cur_n
        row["decisions_superseded"] = sup_n
    return out


def standing_decisions(cur, *, project: str, since: Any, limit: int = 5) -> list[dict[str, Any]]:
    """Non-superseded decisions for ``project`` decided since ``since`` — the
    "Decisions still standing" block in the W4 pushed slice. Never raises;
    a pre-migration hub returns an empty list."""
    if not project or not _decisions_ready(cur):
        return []
    try:
        return list_decisions(
            cur, project=project, since=since, limit=limit, include_superseded=False
        )
    except Exception as exc:  # noqa: BLE001 — the slice degrades, never fails
        _log(f"standing_decisions failed ({type(exc).__name__}: {exc})")
        return []


# ---- backfill (O2) ----------------------------------------------------------

def backfill_decisions(cur, *, apply: bool = False, limit: int | None = None) -> dict[str, Any]:
    """Walk existing episodes' ``decisions`` JSONB arrays into the table.

    Batch, idempotent (the same 30-day-window dedup ``insert_decisions_from_episode``
    uses at capture time — a second run finds every row the first run wrote and
    skips it), logs counts. ``apply=False`` is a dry run: it still queries the
    dedup check for a best-effort count, but writes nothing.
    """
    if not _decisions_ready(cur):
        return {"ok": False, "error": "decisions table not migrated"}
    sql = (
        "SELECT id, project, session_id, decisions, ts FROM episodes "
        "WHERE decisions IS NOT NULL AND jsonb_array_length(decisions) > 0 "
        "ORDER BY ts ASC"
    )
    if limit:
        cur.execute(sql + " LIMIT %s", (limit,))
    else:
        cur.execute(sql)
    rows = cur.fetchall()
    episodes_scanned = 0
    decisions_inserted = 0
    for eid, project, session_id, decisions_json, ts in rows:
        episodes_scanned += 1
        items = decisions_json if isinstance(decisions_json, list) else []
        if not items:
            continue
        decided_at = ts.isoformat() if hasattr(ts, "isoformat") else ts
        payload = {
            "project": project, "session_id": session_id,
            "ts": decided_at, "decisions": items,
        }
        if apply:
            decisions_inserted += insert_decisions_from_episode(cur, payload, eid)
        else:
            for raw in items:
                text = str(raw or "").strip()
                if not text:
                    continue
                norm = normalize_text(text)
                if norm and _find_recent_duplicate(cur, project, norm, decided_at) is None:
                    decisions_inserted += 1
    report = {
        "episodes_scanned": episodes_scanned,
        "decisions_inserted": decisions_inserted,
        "dry_run": not apply,
    }
    _log(f"backfill {'applied' if apply else 'dry-run'}: {report}")
    return report
