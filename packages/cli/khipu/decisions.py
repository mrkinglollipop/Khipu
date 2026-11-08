"""Read archived decision validity for search and episode detail.

No registry extraction or mutation runs here. Episode decision strings remain
part of searchable memory; archived lifecycle states remain authoritative.
"""
from __future__ import annotations
import re
from typing import Any

DEDUP_WINDOW_DAYS = 30

def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _redacted_value(value: Any) -> Any:
    from khipu.redact import redact_secrets

    if isinstance(value, str):
        return redact_secrets(value)[0]
    if isinstance(value, dict):
        return {key: _redacted_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redacted_value(item) for item in value]
    return value


def _decisions_ready(cur) -> bool:
    """True when migration 0019 (the ``decisions`` table) has been applied.

    Fail-closed, same posture as ``khipu.commitments``'s readiness checks: a
    pre-migration hub returns no archived validity rows.
    """
    try:
        from khipu.db import has_columns

        return has_columns(cur, "decisions", "id", "project", "text", "decided_at")
    except Exception:  # noqa: BLE001 — introspection is best-effort
        return False


def _evidence_ready(cur) -> bool:
    """True when migration 0024's evidence/lifecycle columns exist on
    ``decisions`` (source_kind, evidence, superseded_at, supersede_source,
    supersede_reason, retracted_at, retract_reason). Same fail-closed posture
    as ``_decisions_ready``: reads degrade to the pre-0024
    ``superseded_by``-only state instead of raising."""
    try:
        from khipu.db import has_columns

        return has_columns(
            cur, "decisions", "source_kind", "evidence", "superseded_at",
            "supersede_source", "supersede_reason", "retracted_at", "retract_reason",
        )
    except Exception:  # noqa: BLE001 — introspection is best-effort
        return False


def _not_forgotten_clause(cur, table: str) -> str | None:
    """SQL fragment excluding a ``table`` row whose ``episode_id`` points at
    a forgotten (soft-deleted) episode — None when ``episodes.deleted_at``
    does not exist yet, so the caller adds no clause at all pre-migration
    (K5/W5.6 predates 0019/0020, so this should always be present in
    practice, but every other reader here gates on column presence rather
    than assuming it). A row with no episode_id is never excluded."""
    try:
        from khipu.db import has_columns

        if not has_columns(cur, "episodes", "deleted_at"):
            return None
    except Exception:  # noqa: BLE001 — introspection is best-effort
        return None
    return (
        f"({table}.episode_id IS NULL OR NOT EXISTS ("
        f"SELECT 1 FROM episodes e WHERE e.id = {table}.episode_id "
        f"AND e.deleted_at IS NOT NULL))"
    )


def state_of(row: dict[str, Any]) -> str:
    """current | superseded | retracted for one decision row (as returned by
    a plain ``SELECT``). A retracted row wins over a
    superseded one — retracting a decision is the stronger claim ("this was
    wrong"), superseding is "this is what we do now"; both may be true of
    the same row, and a reader deciding what to trust needs the stronger
    signal, not whichever column happens to be checked first."""
    if row.get("retracted_at"):
        return "retracted"
    if row.get("superseded_by"):
        return "superseded"
    return "current"


def decision_rows_for_episodes(cur, episode_ids: list[int], *, with_text: bool = False) -> dict[int, list[dict[str, Any]]]:
    # forget-fence source: decision rows for the episode ids its caller passes; callers fence those ids
    """Decision rows per episode, each with its ``state`` (current |
    superseded | retracted). Empty when the decisions table is absent."""
    if not episode_ids or not _decisions_ready(cur):
        return {}
    retracted = "retracted_at" if _evidence_ready(cur) else "NULL"
    text = "text" if with_text else "length(text)"
    cur.execute(
        f"SELECT episode_id, id, {text}, superseded_by, {retracted} "
        "FROM decisions WHERE episode_id = ANY(%s::bigint[]) ORDER BY id",
        (list(episode_ids),),
    )
    out: dict[int, list[dict[str, Any]]] = {}
    for episode_id, did, body, superseded_by, retracted_at in cur.fetchall():
        row = {"id": did, "superseded_by": superseded_by, "retracted_at": retracted_at}
        row["text" if with_text else "chars"] = body
        row["state"] = state_of(row)
        out.setdefault(int(episode_id), []).append(row)
    # Backfill deduplicates repeated strings into an earlier episode's row.
    # Retracted rows stay visible even when their source episode was forgotten:
    # forgetting the original must not resurrect a retracted restatement as
    # "(validity unknown)" for a later episode that still carries the text.
    body = "d.text" if with_text else "length(d.text)"
    evidence = _evidence_ready(cur)
    retracted = "d.retracted_at" if evidence else "NULL"
    live = _not_forgotten_clause(cur, "d")
    if live and evidence:
        live = f"({live} OR d.retracted_at IS NOT NULL)"
    cur.execute(
        f"SELECT e.id, d.id, {body}, d.superseded_by, {retracted} "
        "FROM episodes e CROSS JOIN LATERAL jsonb_array_elements_text(e.decisions) legacy(text) "
        "JOIN LATERAL (SELECT d.* FROM decisions d "
        "WHERE d.project IS NOT DISTINCT FROM e.project "
        "AND lower(regexp_replace(trim(d.text), '\\s+', ' ', 'g')) = "
        "lower(regexp_replace(trim(legacy.text), '\\s+', ' ', 'g')) "
        "AND d.decided_at <= e.ts "
        f"AND d.decided_at >= e.ts - interval '{DEDUP_WINDOW_DAYS} days' "
        + (f"AND {live} " if live else "") +
        "ORDER BY d.decided_at DESC, d.id DESC LIMIT 1) d ON true "
        "WHERE e.id = ANY(%s::bigint[]) "
        "AND NOT EXISTS (SELECT 1 FROM decisions own WHERE own.episode_id = e.id "
        "AND lower(regexp_replace(trim(own.text), '\\s+', ' ', 'g')) = "
        "lower(regexp_replace(trim(legacy.text), '\\s+', ' ', 'g'))) ORDER BY e.id, d.id",
        (list(episode_ids),),
    )
    for episode_id, did, body, superseded_by, retracted_at in cur.fetchall():
        mine = out.setdefault(int(episode_id), [])
        if any(r["id"] == did for r in mine):
            continue
        row = {"id": did, "superseded_by": superseded_by, "retracted_at": retracted_at}
        row["text" if with_text else "chars"] = body
        row["state"] = state_of(row)
        mine.append(row)
    return out


def decision_states_for_episode(episode_id: int) -> dict[str, Any]:
    # forget-fence: decision states for the one episode khipu_get has already fenced
    """``decision_states``/``validity`` for ``khipu_get``'s episode payload:
    ``{"decision_states": [{"id", "text", "state", "superseded_by"}, ...],
    "validity": {"current": n, "superseded": n, "retracted": n}}``. Opens its
    own connection (same posture as ``khipu.activity.episode_detail``).
    Includes decisions capture deduplicated onto this episode (same attach
    path as ``decision_rows_for_episodes``), so a restatement still shows state.
    Never raises; a pre-migration hub or an episode with no decisions gets
    empty/zeroed results."""
    empty = {"decision_states": [], "validity": {"current": 0, "superseded": 0, "retracted": 0}}
    from khipu.db import connect

    try:
        with connect() as conn:
            with conn.cursor() as cur:
                if not _decisions_ready(cur):
                    return empty
                by_ep = decision_rows_for_episodes(cur, [int(episode_id)], with_text=True)
    except Exception as exc:  # noqa: BLE001 — additive enrichment, never blocks khipu_get
        _log(f"decision_states_for_episode failed ({type(exc).__name__}: {exc})")
        return empty
    rows = by_ep.get(int(episode_id), [])
    states = []
    counts = {"current": 0, "superseded": 0, "retracted": 0}
    for row in rows:
        state = row["state"]
        counts[state] += 1
        states.append({"id": row["id"], "text": row["text"], "state": state,
                        "superseded_by": row.get("superseded_by")})
    return {"decision_states": states, "validity": counts}


def enrich_search_results(cur, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # forget-fence: adds decision counts to the caller's search rows; adds no row
    """Additive ``decisions_current`` / ``decisions_superseded`` /
    ``decisions_retracted`` counts on episode-kind search rows (the third
    added Phase 2, session B so ``khipu.validity.episode_state`` has the same
    three-way breakdown here that the local-replica lane already computes).
    Never raises; a pre-migration hub or a row with no matching decisions
    simply gets zeros. A row is counted into exactly one bucket, matching
    ``state_of``'s own precedence (retracted wins over superseded)."""
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
        forgotten_clause = _not_forgotten_clause(cur, "decisions")
        extra = f" AND {forgotten_clause}" if forgotten_clause else ""
        retracted_expr = "retracted_at IS NOT NULL" if _evidence_ready(cur) else "FALSE"
        cur.execute(
            f"""
            SELECT episode_id,
                   COUNT(*) FILTER (WHERE NOT ({retracted_expr}) AND superseded_by IS NULL) AS current,
                   COUNT(*) FILTER (WHERE NOT ({retracted_expr}) AND superseded_by IS NOT NULL) AS superseded,
                   COUNT(*) FILTER (WHERE {retracted_expr}) AS retracted
            FROM decisions
            WHERE episode_id = ANY(%s){extra}
            GROUP BY episode_id
            """,
            (episode_ids,),
        )
        counts = {
            int(eid): (int(cur_n), int(sup_n), int(ret_n))
            for eid, cur_n, sup_n, ret_n in cur.fetchall()
        }
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
        cur_n, sup_n, ret_n = counts.get(eid, (0, 0, 0))
        row["decisions_current"] = cur_n
        row["decisions_superseded"] = sup_n
        row["decisions_retracted"] = ret_n
    return out



def _log(message: str) -> None:
    import sys
    print(f"[khipu-decision-history] {message}", file=sys.stderr)
