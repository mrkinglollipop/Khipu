"""Recent capture / mirror activity (read path for capture_v2 → PG episodes)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from khipu.db import connect
from khipu.snippets import SNIPPET_LIMIT, clip_snippet
from khipu.summary_kind import VERBATIM_ONLY_SQL
from khipu.text_pages import PAGE_BYTES_DEFAULT, db_start, fetch_bytes, page_args, page_utf8


def _deranked_topic_statuses() -> frozenset[str]:
    """Lazy import: khipu.recency owns the canonical de-rank set (R6); this
    module borrows it rather than duplicating the three status strings."""
    from khipu.recency import DERANKED_STATUSES

    return DERANKED_STATUSES


def _live_where(cur) -> str:
    """``WHERE deleted_at IS NULL `` when the episodes table has the column: a
    forgotten episode is never listed or counted for display. A pre-0010 hub
    has no column and keeps its old behavior."""
    from khipu.db import has_columns

    return "WHERE deleted_at IS NULL " if has_columns(cur, "episodes", "deleted_at") else ""


def _recent_rows(cur, limit: int) -> list[dict]:
    # forget-fence source: activity_payload and recent_episodes' callers fence these rows
    """Recent live episodes on a cursor the caller already owns, so a payload
    that needs several queries opens one connection instead of one per query."""
    cur.execute(
        f"""
        SELECT id, ts, ingested_at, session_id, scope,
               summary,
               topics, decisions, preferences,
               now() - ingested_at AS mirror_age
        FROM episodes
        {_live_where(cur)}
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
    # forget-fence source: khipu_status and session start fence these rows
    with connect() as conn:
        with conn.cursor() as cur:
            return _recent_rows(cur, limit)


def episode_detail(episode_id: int) -> dict | None:
    # forget-fence source: khipu get and khipu activity --show fence the episode and its linked rows
    """A forgotten (soft-deleted) episode is not found here (B3 in
    docs/research/hindsight-plan-review-2026-09-28.md: this used to return a
    forgotten episode in full), gated on ``episodes.deleted_at`` being
    present so a pre-migration hub keeps its old behavior instead of every
    lookup failing."""
    with connect() as conn:
        with conn.cursor() as cur:
            from khipu.db import has_columns

            has_verbatim = has_columns(cur, "episodes", "verbatim")
            verbatim_col = "verbatim" if has_verbatim else "NULL::jsonb AS verbatim"
            live_clause = "AND deleted_at IS NULL " if has_columns(cur, "episodes", "deleted_at") else ""
            cur.execute(
                f"""
                SELECT id, ts, ingested_at, session_id, scope, summary,
                       topics, people, decisions, preferences, edges, raw, {verbatim_col}
                FROM episodes
                WHERE id = %s {live_clause}
                """,
                (episode_id,),
            )
            r = cur.fetchone()
    if not r:
        return None
    from khipu.summary_kind import NO_SUMMARY_LABEL, VERBATIM_ONLY, is_verbatim_only

    no_summary = is_verbatim_only(r[5], r[11])
    return {
        "id": r[0],
        "ts": r[1].isoformat() if r[1] else None,
        "ingested_at": r[2].isoformat() if r[2] else None,
        "session_id": r[3],
        "scope": r[4],
        # An episode the summariser left empty says so, rather than showing a
        # stand-in sentence as if a model had written it.
        "summary": NO_SUMMARY_LABEL if no_summary else r[5],
        **({"summary_kind": VERBATIM_ONLY} if no_summary else {}),
        "topics": r[6],
        "people": r[7],
        "decisions": r[8],
        "preferences": r[9],
        "edges": r[10],
        "raw": r[11],
        "verbatim": r[12],
    }


def topic_detail(slug: str, *, offset: int = 0,
                 max_bytes: int = PAGE_BYTES_DEFAULT,
                 wiki: bool = False) -> dict | None:
    """One page of a topic for MCP ``khipu_get`` and ``khipu get``: the body
    from UTF-8 byte ``offset``, at most ``max_bytes`` of it, with the topic's
    identity and paging fields on every page (``text_pages.page_utf8``).
    When briefs are on and a current brief exists for the slug, that brief
    body is what is paged — same text reflect offers under ``topic:<slug>`` —
    unless ``wiki`` is true, which always returns the raw topic page.
    A brief page sets ``source`` to ``"brief"`` and ``content_hash`` to the
    brief id so ``(content_hash, updated_at)`` paging guards catch a
    brief↔wiki flip. Tombstones are not found."""
    slug = (slug or "").strip()
    if not slug:
        return None
    offset, max_bytes = page_args(offset, max_bytes)
    with connect() as conn:
        with conn.cursor() as cur:
            brief_raw = None
            brief_id = None
            if not wiki:
                try:
                    from khipu import briefs, features

                    if features.enabled("briefs"):
                        brief = briefs.read_brief(cur, slug)
                        if (brief.get("found") and brief.get("state") == "current"
                                and (brief.get("body") or "").strip()):
                            brief_raw = brief["body"].encode("utf-8")
                            brief_id = brief.get("brief_id")
                except Exception:  # noqa: BLE001 — fall through to the wiki page
                    brief_raw = None
                    brief_id = None
            if brief_raw is not None:
                cur.execute(
                    """
                    SELECT t.slug, t.title, t.status, t.created_at, t.updated_at,
                           t.links, t.source_path, t.content_hash
                    FROM topics t
                    WHERE t.slug = %s AND t.deleted_at IS NULL
                    """,
                    (slug,),
                )
                r = cur.fetchone()
                if not r:
                    return None
                page = page_utf8(
                    brief_raw[offset:offset + fetch_bytes(max_bytes)],
                    offset,
                    max_bytes,
                    len(brief_raw),
                )
                return {
                    "id": r[0],
                    "slug": r[0],
                    "title": r[1],
                    "status": r[2],
                    "created_at": r[3].isoformat() if r[3] else None,
                    "updated_at": r[4].isoformat() if r[4] else None,
                    "links": r[5],
                    "source_path": r[6],
                    # Brief id, not the wiki hash: mixed brief/wiki pages must
                    # fail the (content_hash, updated_at) paging guard.
                    "content_hash": str(brief_id) if brief_id is not None else r[7],
                    "source": "brief",
                    **page,
                }
            cur.execute(
                """
                SELECT t.slug, t.title,
                       substring(b.raw FROM %s FOR %s), octet_length(b.raw),
                       t.status, t.created_at, t.updated_at, t.links,
                       t.source_path, t.content_hash
                FROM topics t
                CROSS JOIN LATERAL (SELECT convert_to(t.body, 'UTF8') AS raw) b
                WHERE t.slug = %s AND t.deleted_at IS NULL
                """,
                (db_start(offset), fetch_bytes(max_bytes), slug),
            )
            r = cur.fetchone()
    if not r:
        return None
    page = page_utf8(r[2], offset, max_bytes, r[3])
    return {
        "id": r[0],
        "slug": r[0],
        "title": r[1],
        "status": r[4],
        "created_at": r[5].isoformat() if r[5] else None,
        "updated_at": r[6].isoformat() if r[6] else None,
        "links": r[7],
        "source_path": r[8],
        "content_hash": r[9],
        **page,
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
            cur.execute(f"SELECT COUNT(*) FROM episodes {_live_where(cur)}")
            n = int(cur.fetchone()[0])
            # Capture liveness counts every row: forgetting the newest capture
            # must not make the pipeline look stalled.
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
            # The hub's deleted_at lags this Mac's forget list; an
            # unreadable list shows no episode.
            from khipu.tombstones import withhold

            recent = withhold(_recent_rows(cur, limit), fail_closed=False, episode_key="id")
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


def _annotate_episode_validity(cur, episodes: list[dict]) -> list[dict]:
    # forget-fence: adds decision counts to the caller's rows; adds no row
    """``khipu.validity.annotate`` for the session-start slice's own recent-
    episode rows — one bounded query (WHERE episode_id = ANY(...), already
    small: at most ``episode_limit``), gated on ``khipu.db.has_columns``, the
    same posture as every other reader in this module. Never raises: a
    validity-lookup failure degrades to "unknown", never costs the slice its
    episode rows."""
    if not episodes:
        return episodes
    from khipu.db import has_columns
    from khipu import validity as _validity

    if not has_columns(cur, "decisions", "id", "project", "text", "decided_at"):
        return _validity.annotate(episodes, None, None)
    try:
        evidence_ready = has_columns(cur, "decisions", "retracted_at")
        retract_expr = "retracted_at IS NOT NULL" if evidence_ready else "FALSE"
        ep_ids = [e["id"] for e in episodes]
        cur.execute(
            f"""
            SELECT episode_id,
                   COUNT(*) FILTER (WHERE NOT ({retract_expr}) AND superseded_by IS NULL),
                   COUNT(*) FILTER (WHERE NOT ({retract_expr}) AND superseded_by IS NOT NULL),
                   COUNT(*) FILTER (WHERE {retract_expr})
            FROM decisions
            WHERE episode_id = ANY(%s)
            GROUP BY episode_id
            """,
            (ep_ids,),
        )
        counts = {str(eid): (int(c), int(s), int(r)) for eid, c, s, r in cur.fetchall()}
    except Exception:  # noqa: BLE001 — the slice degrades, never fails
        return _validity.annotate(episodes, None, None)
    return _validity.annotate(episodes, counts, None)


_SLICE_TABLES = ("episodes", "commitments", "decisions", "decision_links", "topics", "project_aliases",
                 "project_pairs")


def project_slice(
    *,
    project: str | None,
    repo_root: str | None = None,
    host_session_id: str | None = None,
    commitment_limit: int = 5,
    episode_limit: int = 5,
    topic_limit: int = 3,
    decision_limit: int = 5,
    job: bool = False,
) -> dict[str, Any]:
    # forget-fence source: session start fences the slice (recall_rule._fenced_slice)
    """W4 pushed-slice reads for a resolved repo: open commitments, recent
    episodes for the project, the topic pages those episodes actually link
    to (their already-hygiene-resolved ``topics`` array — real graph edges,
    not guessed slugs), and (O2) decisions still standing in the last 14
    days. Order matches the plan's acceptance shape: commitments, then
    episodes, then topics; ``recall_rule`` renders them and owns the
    cwd-token fallback and the token budget.

    ``host_session_id`` (this session's own lineage id, ``harness:hostid``)
    widens the episode match beyond ``project`` alone: a dispatched sibling
    whose project inheritance (capture.write_pg) missed still surfaces here
    via its ``parent_session_id``.

    Raises on any PG failure or missing connection — the caller
    (``recall_rule._pushed_memory_slice``) is responsible for the
    hub_snapshot degrade and the visible ``stale`` line; this function stays
    a plain read with no fail-open of its own, same posture as every other
    function in this module.

    Items for the orchestrator (owner ``agent``, from its jobs) follow the
    ``commitment_limit`` others, at most ``owed_orchestrator_slice_max`` of
    them (0: no limit), and only for a conversation: a ``job`` session sees
    none (owed scope, decision 2).
    """
    from khipu import commitments as _commitments
    from khipu import decisions as _decisions
    from khipu.embed import _episode_schema_flags

    def mark_same_session_lineage(rows: list[dict[str, Any]]) -> None:
        # forget-fence: marks the caller's rows; adds no row
        """Mark assistant rows from this session or one of its direct children.

        Project Owed deliberately contains every assistant's work.  The
        session-start rendering only calls a row "(yours)" when the episode
        that opened it belongs to this session lineage.
        """
        if not host_session_id:
            return
        assistants = [r for r in rows if r.get("owner") == _commitments.OWNER_ASSISTANT]
        if not assistants:
            return
        missing = [r["id"] for r in assistants if not (r.get("session_id") or r.get("parent_session_id"))]
        lineage: dict[int, tuple[str | None, str | None]] = {
            int(r["id"]): (r.get("session_id"), r.get("parent_session_id")) for r in assistants
        }
        if missing:
            flags = _episode_schema_flags(cur)
            parent = "e.parent_session_id" if flags.get("parent_session_id") else "NULL::text"
            cur.execute(
                f"SELECT c.id, e.session_id, {parent} FROM commitments c "
                "JOIN episodes e ON e.id = c.opened_episode WHERE c.id = ANY(%s)",
                (missing,),
            )
            lineage.update({int(cid): (session_id, parent_id) for cid, session_id, parent_id in cur.fetchall()})
        for row in assistants:
            session_id, parent_id = lineage.get(int(row["id"]), (None, None))
            row["same_session_lineage"] = host_session_id in (session_id, parent_id)

    owed: list[dict[str, Any]] = []
    episodes: list[dict[str, Any]] = []
    topics: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    from khipu.db import prime_table_columns
    from khipu.identity import group_memo

    with connect() as conn, group_memo():
        # The helpers below ask about these tables' columns one table at a
        # time; one query answers them all (a round trip each to the hub).
        prime_table_columns(conn, *_SLICE_TABLES)
        with conn.cursor() as cur:
            if project:
                # O3: a snoozed row (due_after in the future) is parked — it
                # must not reappear in the session-start slice before its
                # due date, even though `khipu owed`/the desktop still list
                # it so it can be managed.
                rows = _commitments.list_owed(
                    cur, project=project, status="open", limit=None,
                    hide_snoozed=True,
                )
                mark_same_session_lineage(rows)
                owed = [r for r in rows if r.get("owner") != _commitments.OWNER_AGENT][:commitment_limit]
                if not job:
                    from khipu.config import count_setting

                    cap = count_setting("owed_orchestrator_slice_max")
                    agents = [r for r in rows if r.get("owner") == _commitments.OWNER_AGENT]
                    owed += agents[:cap] if cap else agents
                from datetime import timedelta

                decisions = _decisions.standing_decisions(
                    cur, project=project,
                    since=datetime.now(timezone.utc) - timedelta(days=14),
                    limit=decision_limit,
                )

            flags = _episode_schema_flags(cur)
            project_expr = "COALESCE(project, scope)" if flags["project"] else "scope"
            clauses: list[str] = []
            params: list[Any] = []
            from khipu.identity import project_group

            group = project_group(cur, project) if project else []
            if project:
                clauses.append(f"{project_expr} = %s" if len(group) == 1 else f"{project_expr} = ANY(%s)")
                params.append(group[0] if len(group) == 1 else group)
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
                      AND NOT {VERBATIM_ONLY_SQL}
                    ORDER BY ts DESC
                    LIMIT %s
                    """,
                    (*params, episode_limit),
                )
                cols = ("id", "ts", "summary", "topics")
                episodes = [dict(zip(cols, row)) for row in cur.fetchall()]
                episodes = _annotate_episode_validity(cur, episodes)

            topic_slugs: list[str] = []
            topic_episodes: dict[str, list[Any]] = {}
            for ep in episodes:
                for t in ep.get("topics") or []:
                    slug = str(t).strip()
                    if slug and slug not in topic_slugs:
                        topic_slugs.append(slug)
                    if slug:
                        topic_episodes.setdefault(slug, []).append(ep.get("id"))
            # Over-fetch, THEN trim (audit 2026-09-04). Trimming the candidate
            # slugs to topic_limit before the existence lookup meant a capture
            # whose first N topics were tags or deleted pages returned fewer
            # rows than asked for — sometimes none — while real topic pages sat
            # just past the cut. Bounded so a chatty episode list can't send an
            # unbounded ANY() array.
            topic_slugs = topic_slugs[: max(topic_limit * 8, topic_limit)]
            if topic_slugs:
                # R7: event_at (the topic's own timestamp) before updated_at
                # (ingest time) — a note mirrored today that nobody touched
                # today must not show "0d old" in the slice.
                cur.execute(
                    """
                    SELECT slug, title, COALESCE(event_at, updated_at, created_at), status
                    FROM topics
                    WHERE slug = ANY(%s) AND deleted_at IS NULL
                    """,
                    (topic_slugs,),
                )
                now = datetime.now(timezone.utc)
                order = {s: i for i, s in enumerate(topic_slugs)}
                found = []
                for slug, title, when, status in cur.fetchall():
                    age_days = (now - when).days if when is not None else None
                    # The forget fence drops a topic only reached through
                    # forgotten episodes (recall_rule._fenced_slice).
                    found.append({"slug": slug, "title": title, "age_days": age_days,
                                  "status": status or "active",
                                  "via_episodes": topic_episodes.get(slug, [])})
                # R6: a superseded/retired/abandoned page never wins a slot
                # over a current one in the same small slice, same posture
                # as the search de-rank — it can still show, just last.
                found.sort(key=lambda t: (
                    t["status"] in _deranked_topic_statuses(), order.get(t["slug"], len(order))
                ))
                topics.extend(found[:topic_limit])

            # W4.3: harness-native notes (khipu.notes.reconcile) carry no
            # episode link at all — they never went through a capture — so
            # they can only be found by the project the reconcile job wrote
            # into their frontmatter, not by walking an episode's topics.
            if project and len(topics) < topic_limit:
                seen_slugs = {t["slug"] for t in topics}
                remaining = topic_limit - len(topics)
                now = datetime.now(timezone.utc)
                note_match = "%s" if len(group) == 1 else "ANY(%s)"
                cur.execute(
                    f"""
                    SELECT slug, title, COALESCE(event_at, updated_at, created_at), status
                    FROM topics
                    WHERE deleted_at IS NULL AND slug LIKE 'note:%%'
                      AND frontmatter->>'project' = {note_match}
                    ORDER BY COALESCE(event_at, updated_at, created_at) DESC NULLS LAST
                    LIMIT %s
                    """,
                    (group[0] if len(group) == 1 else group, remaining + len(seen_slugs)),
                )
                for slug, title, when, status in cur.fetchall():
                    if slug in seen_slugs:
                        continue
                    age_days = (now - when).days if when is not None else None
                    topics.append({"slug": slug, "title": title, "age_days": age_days,
                                    "status": status or "active"})
                    seen_slugs.add(slug)
                    if len(topics) >= topic_limit:
                        break
            pairs: list[dict[str, Any]] = []
            if project:
                from khipu.project_group import asked_pairs

                pairs = asked_pairs(cur, project)
    return {"commitments": owed, "episodes": episodes, "topics": topics, "decisions": decisions, "pairs": pairs}
