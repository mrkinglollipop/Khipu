"""Recent capture / mirror activity (read path for capture_v2 → PG episodes)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from khipu.db import connect
from khipu.snippets import SNIPPET_LIMIT, clip_snippet


def _recent_rows(cur, limit: int) -> list[dict]:
    """Recent episodes on a cursor the caller already owns, so a payload that
    needs several queries opens one connection instead of one per query."""
    cur.execute(
        """
        SELECT id, ts, ingested_at, session_id, scope,
               summary,
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
                "summary": clip_snippet(r[5], SNIPPET_LIMIT),
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


def topic_detail(slug: str) -> dict | None:
    """Full topic page for MCP ``khipu_get``. Tombstones are not found."""
    slug = (slug or "").strip()
    if not slug:
        return None
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT slug, title, body, status, created_at, updated_at,
                       links, source_path
                FROM topics
                WHERE slug = %s AND deleted_at IS NULL
                """,
                (slug,),
            )
            r = cur.fetchone()
    if not r:
        return None
    return {
        "slug": r[0],
        "title": r[1],
        "body": r[2],
        "status": r[3],
        "created_at": r[4].isoformat() if r[4] else None,
        "updated_at": r[5].isoformat() if r[5] else None,
        "links": r[6],
        "source_path": r[7],
    }


def media_detail(asset_id: str) -> dict | None:
    """media_assets row for MCP ``khipu_get``. Missing table → not found."""
    asset_id = (asset_id or "").strip()
    if not asset_id:
        return None
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.media_assets') IS NOT NULL")
            if not cur.fetchone()[0]:
                return None
            cur.execute(
                """
                SELECT id, source_id, path, sha256, mime, bytes, created_at
                FROM media_assets
                WHERE id = %s
                """,
                (asset_id,),
            )
            r = cur.fetchone()
    if not r:
        return None
    return {
        "id": r[0],
        "source_id": r[1],
        "path": r[2],
        "sha256": r[3],
        "mime": r[4],
        "bytes": r[5],
        "created_at": r[6].isoformat() if r[6] else None,
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


def project_slice(
    *,
    project: str | None,
    repo_root: str | None = None,
    host_session_id: str | None = None,
    commitment_limit: int = 5,
    episode_limit: int = 5,
    topic_limit: int = 3,
) -> dict[str, Any]:
    """W4 pushed-slice reads for a resolved repo: open commitments, recent
    episodes for the project, and the topic pages those episodes actually
    link to (their already-hygiene-resolved ``topics`` array — real graph
    edges, not guessed slugs). Order matches the plan's acceptance shape:
    commitments, then episodes, then topics; ``recall_rule`` renders them and
    owns the cwd-token fallback and the token budget.

    ``host_session_id`` (this session's own lineage id, ``harness:hostid``)
    widens the episode match beyond ``project`` alone: a dispatched sibling
    whose project inheritance (capture.write_pg) missed still surfaces here
    via its ``parent_session_id``.

    Raises on any PG failure or missing connection — the caller
    (``recall_rule._pushed_memory_slice``) is responsible for the
    hub_snapshot degrade and the visible ``stale`` line; this function stays
    a plain read with no fail-open of its own, same posture as every other
    function in this module.
    """
    from khipu import commitments as _commitments
    from khipu.embed import _episode_schema_flags

    owed: list[dict[str, Any]] = []
    episodes: list[dict[str, Any]] = []
    topics: list[dict[str, Any]] = []
    with connect() as conn:
        with conn.cursor() as cur:
            if project:
                owed = _commitments.list_owed(
                    cur, project=project, status="open", limit=commitment_limit
                )

            flags = _episode_schema_flags(cur)
            project_expr = "COALESCE(project, scope)" if flags["project"] else "scope"
            clauses: list[str] = []
            params: list[Any] = []
            if project:
                clauses.append(f"{project_expr} = %s")
                params.append(project)
            if host_session_id and flags.get("parent_session_id"):
                clauses.append("parent_session_id = %s")
                params.append(host_session_id)
            if clauses:
                deleted_clause = "AND deleted_at IS NULL " if flags["deleted_at"] else ""
                cur.execute(
                    f"""
                    SELECT id, ts, summary, topics
                    FROM episodes
                    WHERE ({' OR '.join(clauses)}) {deleted_clause}
                    ORDER BY ts DESC
                    LIMIT %s
                    """,
                    (*params, episode_limit),
                )
                cols = ("id", "ts", "summary", "topics")
                episodes = [dict(zip(cols, row)) for row in cur.fetchall()]

            topic_slugs: list[str] = []
            for ep in episodes:
                for t in ep.get("topics") or []:
                    slug = str(t).strip()
                    if slug and slug not in topic_slugs:
                        topic_slugs.append(slug)
            topic_slugs = topic_slugs[:topic_limit]
            if topic_slugs:
                cur.execute(
                    """
                    SELECT slug, title, COALESCE(updated_at, created_at)
                    FROM topics
                    WHERE slug = ANY(%s) AND deleted_at IS NULL
                    """,
                    (topic_slugs,),
                )
                now = datetime.now(timezone.utc)
                for slug, title, when in cur.fetchall():
                    age_days = (now - when).days if when is not None else None
                    topics.append({"slug": slug, "title": title, "age_days": age_days})

            # W4.3: harness-native notes (khipu.notes.reconcile) carry no
            # episode link at all — they never went through a capture — so
            # they can only be found by the project the reconcile job wrote
            # into their frontmatter, not by walking an episode's topics.
            if project and len(topics) < topic_limit:
                seen_slugs = {t["slug"] for t in topics}
                remaining = topic_limit - len(topics)
                now = datetime.now(timezone.utc)
                cur.execute(
                    """
                    SELECT slug, title, COALESCE(updated_at, created_at)
                    FROM topics
                    WHERE deleted_at IS NULL AND slug LIKE 'note:%%'
                      AND frontmatter->>'project' = %s
                    ORDER BY COALESCE(updated_at, created_at) DESC NULLS LAST
                    LIMIT %s
                    """,
                    (project, remaining + len(seen_slugs)),
                )
                for slug, title, when in cur.fetchall():
                    if slug in seen_slugs:
                        continue
                    age_days = (now - when).days if when is not None else None
                    topics.append({"slug": slug, "title": title, "age_days": age_days})
                    seen_slugs.add(slug)
                    if len(topics) >= topic_limit:
                        break
    return {"commitments": owed, "episodes": episodes, "topics": topics}
