"""Recent capture / mirror activity (read path for capture_v2 → PG episodes)."""
from __future__ import annotations

from khipu.db import connect


def _recent_rows(cur, limit: int) -> list[dict]:
    """Recent episodes on a cursor the caller already owns, so a payload that
    needs several queries opens one connection instead of one per query."""
    cur.execute(
        """
        SELECT id, ts, ingested_at, session_id, scope,
               left(summary, 280) AS summary,
               topics, decisions, preferences,
               now() - ingested_at AS mirror_age
        FROM episodes
        ORDER BY COALESCE(ingested_at, ts) DESC
        LIMIT %s
        """,
        (limit,),
    )
    rows = cur.fetchall()
    out = []
    for r in rows:
        age = r[9]
        out.append(
            {
                "id": r[0],
                "ts": r[1].isoformat() if r[1] else None,
                "ingested_at": r[2].isoformat() if r[2] else None,
                "session_id": r[3],
                "scope": r[4],
                "summary": r[5],
                "topics": r[6],
                "decisions": r[7],
                "preferences": r[8],
                "mirror_age_seconds": age.total_seconds() if age is not None else None,
            }
        )
    return out


def recent_episodes(*, limit: int = 40) -> list[dict]:
    with connect() as conn:
        with conn.cursor() as cur:
            return _recent_rows(cur, limit)


def episode_detail(episode_id: int) -> dict | None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, ts, ingested_at, session_id, scope, summary,
                       topics, people, decisions, preferences, edges, raw
                FROM episodes
                WHERE id = %s
                """,
                (episode_id,),
            )
            r = cur.fetchone()
    if not r:
        return None
    return {
        "id": r[0],
        "ts": r[1].isoformat() if r[1] else None,
        "ingested_at": r[2].isoformat() if r[2] else None,
        "session_id": r[3],
        "scope": r[4],
        "summary": r[5],
        "topics": r[6],
        "people": r[7],
        "decisions": r[8],
        "preferences": r[9],
        "edges": r[10],
        "raw": r[11],
    }


def activity_payload(*, limit: int = 40) -> dict:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM episodes")
            n = int(cur.fetchone()[0])
            cur.execute(
                "SELECT MAX(ts), MAX(ingested_at), now() - MAX(ingested_at) FROM episodes"
            )
            max_ts, max_ing, lag = cur.fetchone()
            ops: list[dict] = []
            cur.execute(
                """
                SELECT to_regclass('public.ops_events') IS NOT NULL
                """
            )
            if cur.fetchone()[0]:
                cur.execute(
                    """
                    SELECT kind, status, detail, created_at
                    FROM ops_events
                    ORDER BY created_at DESC
                    LIMIT 15
                    """
                )
                for kind, status, detail, created_at in cur.fetchall():
                    ops.append(
                        {
                            "kind": kind,
                            "status": status,
                            "detail": detail,
                            "created_at": created_at.isoformat()
                            if created_at
                            else None,
                        }
                    )
            recent = _recent_rows(cur, limit)
    try:
        from khipu.keychain import secrets_status

        secrets = secrets_status()
    except Exception as exc:  # noqa: BLE001
        secrets = {"error": str(exc)}
    return {
        "episode_count": n,
        "latest_episode_ts": max_ts.isoformat() if max_ts else None,
        "latest_ingested_at": max_ing.isoformat() if max_ing else None,
        "ingest_lag_seconds": lag.total_seconds() if lag is not None else None,
        "source": (
            "Cursor/Claude capture_v2.py → fail-open khipu.mirror → episodes. "
            "App is read/inspect today; Hub-owned capture+edit is P3."
        ),
        "secrets": secrets,
        "ops_events": ops,
        "recent": recent,
    }
