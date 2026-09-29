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

import json
import re
from typing import Any

DEDUP_WINDOW_DAYS = 30

# Migration 0024's evidence/lifecycle columns and the decision_links table
# (Phase 2, session A). VALID_LINK_KINDS/STATES/SOURCES are enumerated in
# Python, not a CHECK constraint — the hub may be behind the repo, and a
# constraint a pre-migration writer cannot satisfy would turn "not migrated
# yet" into a hard failure instead of the fail-closed degrade every other
# reader/writer here uses.
VALID_LINK_KINDS = ("supersedes", "conflicts")
VALID_LINK_STATES = ("candidate", "applied", "rejected", "restored")
VALID_SUPERSEDE_SOURCES = ("manual", "agent", "auto")

# Reversal-detection thresholds (Phase 2, session C) — provisional until
# measured against the private golden set; nothing has scored them yet.
REVERSAL_CANDIDATE_THRESHOLD = 0.45   # minimum similarity to record a candidate link at all
REVERSAL_AUTO_APPLY_THRESHOLD = 0.70  # minimum similarity for auto_supersede to apply one
REVERSAL_CANDIDATE_POOL = 200         # most-recent standing decisions scored by the Python fallback


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


def _evidence_ready(cur) -> bool:
    """True when migration 0024's evidence/lifecycle columns exist on
    ``decisions`` (source_kind, evidence, superseded_at, supersede_source,
    supersede_reason, retracted_at, retract_reason). Same fail-closed posture
    as ``_decisions_ready``: every writer below degrades to the pre-0024
    ``superseded_by``-only behavior instead of raising."""
    try:
        from khipu.db import has_columns

        return has_columns(
            cur, "decisions", "source_kind", "evidence", "superseded_at",
            "supersede_source", "supersede_reason", "retracted_at", "retract_reason",
        )
    except Exception:  # noqa: BLE001 — introspection is best-effort
        return False


def _links_ready(cur) -> bool:
    """True when migration 0024's ``decision_links`` table exists."""
    try:
        from khipu.db import has_columns

        return has_columns(cur, "decision_links", "id", "old_id", "new_id", "kind", "state")
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
    ``list_decisions``/a plain ``SELECT``). A retracted row wins over a
    superseded one — retracting a decision is the stronger claim ("this was
    wrong"), superseding is "this is what we do now"; both may be true of
    the same row, and a reader deciding what to trust needs the stronger
    signal, not whichever column happens to be checked first."""
    if row.get("retracted_at"):
        return "retracted"
    if row.get("superseded_by"):
        return "superseded"
    return "current"


def _mirror_to_snapshot(cur, decision_id: int) -> None:
    """Best-effort local-replica mirror for a decision write made on THIS
    machine — "the decisions API calls it after a successful local write so
    a correction made on this machine reaches the local lane at once"
    (docs/plans/2026-09-27-memory-reasoning-scope.md, Phase 2 session B). A
    correction made on a DIFFERENT machine reaches this replica instead via
    ``hub_snapshot.sync_decision_changes``, called from the capture drain.
    Never raises: the PG write this backs is already durable (or still
    inside the caller's own open transaction, same-session-visible either
    way) by the time this runs, so a mirror failure must never surface as
    if the decision write itself failed."""
    try:
        cols = ["id", "project", "text", "decided_at", "episode_id", "superseded_by"]
        if _evidence_ready(cur):
            cols += [
                "source_kind", "evidence", "superseded_at", "supersede_source",
                "supersede_reason", "retracted_at", "retract_reason",
            ]
        cols.append("created_at")
        cur.execute(f"SELECT {', '.join(cols)} FROM decisions WHERE id = %s", (decision_id,))
        row = cur.fetchone()
        if row is None:
            return
        payload = dict(zip(cols, row))
        from khipu import hub_snapshot

        hub_snapshot.apply_decision_changes([payload])
    except Exception as exc:  # noqa: BLE001 — the PG write already succeeded; mirror is best-effort
        _log(f"snapshot mirror for decision {decision_id} failed ({type(exc).__name__}: {exc})")


def _stale_briefs(cur, decision_id: int) -> None:
    """A brief's claims were built under the decision states of the moment;
    any state change makes the briefs citing this decision's episode stale
    (khipu.briefs). Fail-open."""
    from khipu import briefs

    briefs.mark_stale_for_decision(cur, decision_id)


def _fetch_decision_row(cur, decision_id: int) -> dict[str, Any] | None:
    cur.execute(
        "SELECT id, project, superseded_by FROM decisions WHERE id = %s",
        (decision_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {"id": row[0], "project": row[1], "superseded_by": row[2]}


def _chain_leads_to(cur, start_id: int, target_id: int, *, max_depth: int = 100) -> bool:
    """True when walking ``superseded_by`` forward from ``start_id`` reaches
    ``target_id`` — i.e. ``target_id`` already (transitively) supersedes
    ``start_id``. Used to refuse a supersede that would close a cycle.
    Depth-bounded against a corrupt chain rather than looping forever."""
    seen: set[int] = set()
    current = start_id
    for _ in range(max_depth):
        if current in seen:
            return False
        seen.add(current)
        cur.execute("SELECT superseded_by FROM decisions WHERE id = %s", (current,))
        row = cur.fetchone()
        if not row or row[0] is None:
            return False
        nxt = int(row[0])
        if nxt == target_id:
            return True
        current = nxt
    return False


def _record_applied_link(cur, old_id: int, new_id: int, *, source: str, reason: str | None) -> None:
    """Best-effort: record (or upsert) an ``applied`` supersedes link for a
    supersession made outside ``resolve_link`` — e.g. straight from the CLI
    or ``khipu_decisions_update``. Never raises; the row update this backs
    is already durable by the time this runs."""
    try:
        cur.execute(
            "INSERT INTO decision_links (old_id, new_id, kind, source, reason, state, resolved_at) "
            "VALUES (%s, %s, 'supersedes', %s, %s, 'applied', now()) "
            "ON CONFLICT (old_id, new_id, kind) DO UPDATE SET "
            "state = 'applied', source = EXCLUDED.source, reason = EXCLUDED.reason, "
            "resolved_at = now()",
            (old_id, new_id, source, reason),
        )
    except Exception as exc:  # noqa: BLE001 — the supersede itself already succeeded
        _log(f"recording applied link {old_id}->{new_id} failed ({type(exc).__name__}: {exc})")


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


def _decision_details_by_text(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``payload['decision_details']`` keyed by its own ``text`` (already the
    word-for-word match ``extract._as_decision_details`` enforced) — [] or a
    malformed payload (a hand-built dict from a caller other than extraction)
    both degrade to an empty map, never an error."""
    raw = payload.get("decision_details")
    if not isinstance(raw, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if text:
            out[text] = item
    return out


def insert_decisions_from_episode(cur, payload: dict[str, Any], episode_id: int) -> int:
    """One row per string in ``payload['decisions']``. Returns the count
    actually inserted.

    ``project`` is the episode's own resolved project (NULL stays NULL — no
    session-id fallback here, same K6 posture as commitments' scope). Dedup
    is by normalised text within that project, in a 30-day window ending at
    this capture's own ``ts`` (never "now" — a backfill mints historical
    ``ts`` values and must dedup against ITS OWN neighbourhood, not today).

    Phase 2, session C: when migration 0024's columns exist AND
    ``payload['decision_details']`` carries an entry whose ``text`` matches
    this string exactly, the row also records ``source_kind`` (from
    ``by``), ``rationale``, and ``evidence`` (the derivation tag, this
    episode, and the ``reverses`` text detection reads next). A string with
    no matching detail — the common case, and always the case with the
    `decision_details` switch off — gets exactly today's five-column insert;
    a pre-migration hub always does, detail or not.
    """
    items = payload.get("decisions") or []
    if not isinstance(items, list) or not items:
        return 0
    if not _decisions_ready(cur):
        return 0
    project = payload.get("project")
    session_id = payload.get("session_id")
    decided_at = payload.get("ts")
    evidence_ready = _evidence_ready(cur)
    details = _decision_details_by_text(payload) if evidence_ready else {}
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
        detail = details.get(text)
        if detail is None:
            cur.execute(
                """
                INSERT INTO decisions (project, text, episode_id, session_id, decided_at)
                VALUES (%s, %s, %s, %s, COALESCE(%s::timestamptz, now()))
                """,
                (project, text, episode_id, session_id, decided_at),
            )
        else:
            source_kind = detail.get("by") or None
            rationale = detail.get("rationale") or None
            evidence = {
                "derivation": "extract-details-v1",
                "episode_id": episode_id,
                "reverses": detail.get("reverses") or None,
            }
            cur.execute(
                """
                INSERT INTO decisions
                    (project, text, episode_id, session_id, decided_at,
                     source_kind, rationale, evidence)
                VALUES (%s, %s, %s, %s, COALESCE(%s::timestamptz, now()), %s, %s, %s::jsonb)
                """,
                (project, text, episode_id, session_id, decided_at,
                 source_kind, rationale, json.dumps(evidence)),
            )
        if cur.rowcount > 0:
            inserted += 1
    if inserted:
        _log(f"recorded {inserted} decision(s) for episode {episode_id} (project={project!r})")
    return inserted


def _trgm_available(cur) -> bool:
    """Same probe ``khipu.embed.literal_trgm_status`` uses for migration
    0015's indexes — pg_trgm installed means ``similarity()`` is callable.
    False (never raises) on a hub without the extension AND on any fake/
    SQLite cursor with no ``pg_extension`` catalog, which is the intended
    degrade into the Python fallback below."""
    try:
        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
        return cur.fetchone() is not None
    except Exception:  # noqa: BLE001 — absence of the catalog IS the fallback signal
        return False


def _standing_before_clause(cur, evidence_ready: bool) -> str:
    """Shared WHERE fragment for both reversal-match finders below: standing
    (not superseded, not retracted once evidence is ready), not forgotten,
    same project, decided strictly before the new decision, excluding the
    new decision itself. Every caller binds its params in this exact order:
    (project, exclude_id, before)."""
    clauses = ["project IS NOT DISTINCT FROM %s", "id != %s", "decided_at < %s",
               "superseded_by IS NULL"]
    if evidence_ready:
        clauses.append("retracted_at IS NULL")
    forgotten = _not_forgotten_clause(cur, "decisions")
    if forgotten:
        clauses.append(forgotten)
    return " AND ".join(clauses)


def _best_reversal_match_sql(cur, *, project, exclude_id, before, reverses_text,
                              evidence_ready) -> tuple[int, float] | None:
    """pg_trgm path: let the hub's trigram index rank every standing decision
    in the project against ``reverses_text`` and return the single best
    match. No 200-row cap here — that bound is the Python fallback's own,
    named in ``BUILD`` item 3."""
    where = _standing_before_clause(cur, evidence_ready)
    cur.execute(
        f"SELECT id, similarity(text, %s) AS score FROM decisions "
        f"WHERE {where} ORDER BY score DESC LIMIT 1",
        (reverses_text, project, exclude_id, before),
    )
    row = cur.fetchone()
    if row is None or row[1] is None:
        return None
    return int(row[0]), float(row[1])


def _best_reversal_match_python(cur, *, project, exclude_id, before, reverses_text,
                                 evidence_ready) -> tuple[int, float] | None:
    """Fallback when pg_trgm is unavailable: the 200 most recent standing
    decisions of the project, scored in Python with the same token-overlap
    ratio ``khipu.commitments`` already uses for paraphrase dedup (shared,
    not copied, so the two never drift apart)."""
    from khipu.capture import _jaccard

    where = _standing_before_clause(cur, evidence_ready)
    cur.execute(
        f"SELECT id, text FROM decisions WHERE {where} "
        f"ORDER BY decided_at DESC LIMIT {REVERSAL_CANDIDATE_POOL}",
        (project, exclude_id, before),
    )
    best: tuple[int, float] | None = None
    for did, text in cur.fetchall():
        score = _jaccard(reverses_text, text or "")
        if best is None or score > best[1]:
            best = (int(did), score)
    return best


def detect_reversals_from_episode(cur, payload: dict[str, Any], episode_id: int) -> int:
    """Conservative capture-time reversal detection (Evidence rule #7,
    docs/plans/2026-09-27-memory-reasoning-scope.md): for each decision this
    capture just inserted whose ``decision_details`` entry names an earlier
    decision it ``reverses`` (in the model's own words, never invented), find
    the closest-matching STANDING decision in the SAME project decided
    BEFORE it and record one ``supersedes`` candidate link. Detection never
    changes authoritative state on its own — a candidate becomes a
    supersession only via confirmation, or here, when ``auto_supersede`` is
    on AND the match clears the higher bar AND the reversal was the USER's
    decision (never the assistant's own say-so alone).

    Returns the number of candidate links recorded. No-op — 0, never raises
    — on a pre-migration hub, an episode with no ``decision_details``, or a
    detail with no ``reverses``. A capture whose ``decisions``/``decision_
    details`` text was deduped away this call (an existing row, not one
    ``insert_decisions_from_episode`` just created for THIS episode) is
    skipped too: nothing new was inserted for it to link from.
    """
    details = payload.get("decision_details")
    if not isinstance(details, list) or not details:
        return 0
    if not _links_ready(cur):
        return 0
    evidence_ready = _evidence_ready(cur)
    trgm = _trgm_available(cur)
    try:
        from khipu import features

        auto = features.enabled("auto_supersede")
    except Exception:  # noqa: BLE001 — the switch registry must never block capture
        auto = False
    candidates = 0
    for item in details:
        if not isinstance(item, dict):
            continue
        reverses = str(item.get("reverses") or "").strip()
        text = str(item.get("text") or "").strip()
        if not reverses or not text:
            continue
        cur.execute(
            "SELECT id, project, decided_at FROM decisions "
            "WHERE episode_id = %s AND text = %s ORDER BY id DESC LIMIT 1",
            (episode_id, text),
        )
        row = cur.fetchone()
        if row is None:
            continue  # deduped into an earlier row this capture; nothing new to link
        new_id, project, decided_at = int(row[0]), row[1], row[2]
        finder = _best_reversal_match_sql if trgm else _best_reversal_match_python
        match = finder(cur, project=project, exclude_id=new_id, before=decided_at,
                        reverses_text=reverses, evidence_ready=evidence_ready)
        if match is None:
            continue
        old_id, score = match
        if score < REVERSAL_CANDIDATE_THRESHOLD:
            continue
        link_id = add_link(cur, old_id, new_id, kind="supersedes", confidence=score,
                            source="auto", reason=reverses)
        if link_id is None:
            continue
        candidates += 1
        by = str(item.get("by") or "").strip().lower()
        if auto and by == "user" and score >= REVERSAL_AUTO_APPLY_THRESHOLD:
            resolve_link(cur, link_id, "confirm", source="auto")
    return candidates


def list_decisions(cur, *, project: str | None = None, since: Any = None,
                    limit: int = 50, include_superseded: bool = True,
                    status: str | None = None) -> list[dict[str, Any]]:
    """``status`` (Phase 2A): "all" | "standing" | "superseded" | "retracted".
    Omitted, it falls back to ``include_superseded`` exactly as before —
    every existing caller (``standing_decisions``, the CLI's old default)
    keeps working unchanged. Each row gains ``state`` (``state_of``). A
    decision whose episode was forgotten is excluded, same as every other
    reader (a row with no episode_id, or on a pre-migration hub with no
    ``episodes.deleted_at`` yet, is never excluded)."""
    status = (status or "").strip().lower() or None
    if status is not None and status not in ("all", "standing", "superseded", "retracted"):
        raise ValueError("status must be 'all', 'standing', 'superseded', or 'retracted'")
    evidence_ready = _evidence_ready(cur)
    clauses: list[str] = []
    params: list[Any] = []
    if project:
        clauses.append("project = %s")
        params.append(project)
    if since is not None:
        clauses.append("decided_at >= %s")
        params.append(since)
    if status == "standing":
        clauses.append("superseded_by IS NULL")
        if evidence_ready:
            clauses.append("retracted_at IS NULL")
    elif status == "superseded":
        clauses.append("superseded_by IS NOT NULL")
    elif status == "retracted":
        clauses.append("retracted_at IS NOT NULL" if evidence_ready else "FALSE")
    elif status is None and not include_superseded:
        clauses.append("superseded_by IS NULL")
    # status == "all", or status is None with include_superseded=True: no extra clause.
    forgotten_clause = _not_forgotten_clause(cur, "decisions")
    if forgotten_clause:
        clauses.append(forgotten_clause)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    cols = ["id", "project", "text", "rationale", "decided_at", "episode_id",
            "session_id", "superseded_by", "created_at"]
    if evidence_ready:
        cols += ["source_kind", "evidence", "superseded_at", "supersede_source",
                 "supersede_reason", "retracted_at", "retract_reason"]
    cur.execute(
        f"""
        SELECT {', '.join(cols)}
        FROM decisions
        {where}
        ORDER BY decided_at DESC
        LIMIT %s
        """,
        params,
    )
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    for row in rows:
        row["state"] = state_of(row)
    return rows


def supersede(cur, old_id: int, new_id: int, *, source: str = "manual",
              reason: str | None = None, force: bool = False) -> bool:
    """Mark ``old_id`` superseded by ``new_id``. A reversal or correction
    then outranks the original in every reader instead of sitting beside it
    forever (O2's root complaint).

    Refuses: self-reference, an unknown id, a cycle (``new_id`` already
    transitively superseded by ``old_id``), and — unless ``force=True`` —
    two decisions in different projects, all raising ``ValueError``. An
    old row that is ALREADY superseded is the one refusal that returns
    ``False`` instead of raising (today's contract: a caller checking
    "did this already happen" gets an answer, not an exception).
    ``source``/``reason`` are recorded when migration 0024's columns exist;
    pre-migration this still supersedes via the original ``superseded_by``
    pointer alone. Records an ``applied`` link in ``decision_links`` when
    that table exists."""
    if source not in VALID_SUPERSEDE_SOURCES:
        raise ValueError(f"source must be one of {VALID_SUPERSEDE_SOURCES}")
    if old_id == new_id:
        raise ValueError("a decision cannot supersede itself")
    old = _fetch_decision_row(cur, old_id)
    if old is None:
        raise ValueError(f"no such decision: {old_id}")
    new = _fetch_decision_row(cur, new_id)
    if new is None:
        raise ValueError(f"no such decision: {new_id}")
    if old.get("superseded_by") is not None:
        # Not a raise (unlike the other refusals below): calling supersede
        # again on an already-superseded row is the ordinary "did this
        # already happen" check a caller makes without tracking state
        # itself — same contract the pre-0024 version of this function had.
        return False
    if not force and old.get("project") != new.get("project"):
        raise ValueError(
            f"decision {old_id} (project {old.get('project')!r}) and {new_id} "
            f"(project {new.get('project')!r}) are in different projects; pass force=True to override"
        )
    if _chain_leads_to(cur, new_id, old_id):
        raise ValueError(f"supersede would create a cycle between {old_id} and {new_id}")
    if _evidence_ready(cur):
        cur.execute(
            "UPDATE decisions SET superseded_by = %s, superseded_at = now(), "
            "supersede_source = %s, supersede_reason = %s "
            "WHERE id = %s AND superseded_by IS NULL",
            (new_id, source, reason, old_id),
        )
    else:
        cur.execute(
            "UPDATE decisions SET superseded_by = %s WHERE id = %s AND superseded_by IS NULL",
            (new_id, old_id),
        )
    ok = cur.rowcount > 0
    if ok and _links_ready(cur):
        _record_applied_link(cur, old_id, new_id, source=source, reason=reason)
    if ok:
        _mirror_to_snapshot(cur, old_id)
        _stale_briefs(cur, old_id)
    return ok


def restore(cur, decision_id: int) -> bool:
    """Undo a supersession: clears ``superseded_by`` (and, when migration
    0024 is applied, ``superseded_at``/``supersede_source``/``supersede_reason``).
    Every supersession can be restored (Evidence rule #7). Any ``applied``
    link recording that supersession is marked ``restored`` so the history
    stays visible rather than looking like it never happened."""
    row = _fetch_decision_row(cur, decision_id)
    if row is None:
        raise ValueError(f"no such decision: {decision_id}")
    new_id = row.get("superseded_by")
    if _evidence_ready(cur):
        cur.execute(
            "UPDATE decisions SET superseded_by = NULL, superseded_at = NULL, "
            "supersede_source = NULL, supersede_reason = NULL "
            "WHERE id = %s AND superseded_by IS NOT NULL",
            (decision_id,),
        )
    else:
        cur.execute(
            "UPDATE decisions SET superseded_by = NULL WHERE id = %s AND superseded_by IS NOT NULL",
            (decision_id,),
        )
    ok = cur.rowcount > 0
    if ok and new_id is not None and _links_ready(cur):
        cur.execute(
            "UPDATE decision_links SET state = 'restored', resolved_at = now() "
            "WHERE old_id = %s AND new_id = %s AND kind = 'supersedes' AND state = 'applied'",
            (decision_id, new_id),
        )
    if ok:
        _mirror_to_snapshot(cur, decision_id)
        _stale_briefs(cur, decision_id)
    return ok


def retract(cur, decision_id: int, reason: str | None = None) -> bool:
    """Mark a decision retracted ("this was wrong", not merely replaced).
    Pre-migration (no ``retracted_at`` column) this is a no-op returning
    False — the caller (CLI, ``khipu_decisions_update``) surfaces that as
    "evidence fields were not recorded", same posture as ``supersede``."""
    if not _evidence_ready(cur):
        return False
    cur.execute(
        "UPDATE decisions SET retracted_at = now(), retract_reason = %s "
        "WHERE id = %s AND retracted_at IS NULL",
        (reason, decision_id),
    )
    ok = cur.rowcount > 0
    if ok:
        _mirror_to_snapshot(cur, decision_id)
        _stale_briefs(cur, decision_id)
    return ok


def unretract(cur, decision_id: int) -> bool:
    if not _evidence_ready(cur):
        return False
    cur.execute(
        "UPDATE decisions SET retracted_at = NULL, retract_reason = NULL "
        "WHERE id = %s AND retracted_at IS NOT NULL",
        (decision_id,),
    )
    ok = cur.rowcount > 0
    if ok:
        _mirror_to_snapshot(cur, decision_id)
        _stale_briefs(cur, decision_id)
    return ok


def create_decision(cur, *, project: str | None, text: str, session_id: str | None = None,
                     source_kind: str | None = None, decided_at: Any = None,
                     evidence: Any = None) -> int:
    """Insert a new decision row, reusing the same 30-day normalised-text
    duplicate check ``insert_decisions_from_episode`` uses — a supersession
    whose replacement text restates something already on file within the
    window returns the EXISTING row's id instead of adding a second one.
    Raises ``ValueError`` for empty text."""
    text = str(text or "").strip()
    if not text:
        raise ValueError("text is required")
    norm = normalize_text(text)
    dup = _find_recent_duplicate(cur, project, norm, decided_at) if norm else None
    if dup is not None:
        return dup
    if _evidence_ready(cur):
        cur.execute(
            "INSERT INTO decisions (project, text, session_id, decided_at, source_kind, evidence) "
            "VALUES (%s, %s, %s, COALESCE(%s::timestamptz, now()), %s, %s::jsonb) "
            "RETURNING id",
            (project, text, session_id, decided_at, source_kind,
             json.dumps(evidence) if evidence is not None else None),
        )
    else:
        cur.execute(
            "INSERT INTO decisions (project, text, session_id, decided_at) "
            "VALUES (%s, %s, %s, COALESCE(%s::timestamptz, now())) RETURNING id",
            (project, text, session_id, decided_at),
        )
    return int(cur.fetchone()[0])


def add_link(cur, old_id: int, new_id: int, *, kind: str = "supersedes",
             confidence: float | None = None, source: str = "manual",
             reason: str | None = None) -> int | None:
    """Insert (or return the id of) a candidate link — a detector's proposal
    that changes nothing until ``resolve_link`` confirms it (Evidence rule
    #7). None on a pre-migration hub (no ``decision_links`` table)."""
    if kind not in VALID_LINK_KINDS:
        raise ValueError(f"kind must be one of {VALID_LINK_KINDS}")
    if source not in VALID_SUPERSEDE_SOURCES:
        raise ValueError(f"source must be one of {VALID_SUPERSEDE_SOURCES}")
    if not _links_ready(cur):
        return None
    cur.execute(
        "INSERT INTO decision_links (old_id, new_id, kind, confidence, source, reason) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (old_id, new_id, kind) DO NOTHING "
        "RETURNING id",
        (old_id, new_id, kind, confidence, source, reason),
    )
    row = cur.fetchone()
    if row is not None:
        return int(row[0])
    cur.execute(
        "SELECT id FROM decision_links WHERE old_id = %s AND new_id = %s AND kind = %s",
        (old_id, new_id, kind),
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


def list_links(cur, *, state: str | None = None, project: str | None = None,
               limit: int = 50) -> list[dict[str, Any]]:
    """Links (candidate by default filter left to the caller — pass
    ``state="candidate"`` for the usual "what needs a decision" view), each
    carrying both decisions' text so a caller never has to join itself.
    [] on a pre-migration hub."""
    if not _links_ready(cur):
        return []
    if state is not None and state not in VALID_LINK_STATES:
        raise ValueError(f"state must be one of {VALID_LINK_STATES}")
    clauses: list[str] = []
    params: list[Any] = []
    if state:
        clauses.append("l.state = %s")
        params.append(state)
    if project:
        clauses.append("(o.project = %s OR n.project = %s)")
        params.extend([project, project])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    cur.execute(
        f"""
        SELECT l.id, l.old_id, l.new_id, l.kind, l.confidence, l.source, l.reason,
               l.state, l.created_at, l.resolved_at, o.text, n.text
        FROM decision_links l
        JOIN decisions o ON o.id = l.old_id
        JOIN decisions n ON n.id = l.new_id
        {where}
        ORDER BY l.created_at DESC
        LIMIT %s
        """,
        params,
    )
    cols = ("id", "old_id", "new_id", "kind", "confidence", "source", "reason",
            "state", "created_at", "resolved_at", "old_text", "new_text")
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def candidate_link_count() -> dict[str, Any]:
    """``khipu doctor``'s ``decision_links`` block: how many candidates
    (state ``candidate``) are waiting for a person or an agent to confirm or
    reject them. Opens its own connection, same posture as
    ``decision_states_for_episode``. Visibility only — the doctor caller
    never folds this into ``ok``, so a pre-migration hub or a connection
    failure both just report what they found rather than pretending to be a
    correctness gate."""
    from khipu.db import connect

    try:
        with connect() as conn:
            with conn.cursor() as cur:
                if not _links_ready(cur):
                    return {"ok": True, "applicable": False, "candidates": 0}
                cur.execute("SELECT COUNT(*) FROM decision_links WHERE state = 'candidate'")
                row = cur.fetchone()
                return {"ok": True, "applicable": True, "candidates": int(row[0]) if row else 0}
    except Exception as exc:  # noqa: BLE001 — a failed check must not look like a pass
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def resolve_link(cur, link_id: int, action: str, *, source: str = "manual") -> dict[str, Any]:
    """Confirm or reject one candidate link. Confirming a ``supersedes``
    candidate applies the supersession via ``supersede`` (cross-project
    forced, since a detector's own scope check already ran when it proposed
    the link) with ``source`` taken from the caller, not the link's original
    detector. Raises ``ValueError`` for an unknown link or one that is not
    (still) a candidate — a resolved link is resolved once."""
    if action not in ("confirm", "reject"):
        raise ValueError("action must be 'confirm' or 'reject'")
    if not _links_ready(cur):
        return {"ok": False, "error": "decision_links table not migrated"}
    cur.execute(
        "SELECT old_id, new_id, kind, state FROM decision_links WHERE id = %s",
        (link_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"no such link: {link_id}")
    old_id, new_id, kind, state = row
    if state != "candidate":
        raise ValueError(f"link {link_id} is not a candidate (state={state!r})")
    if action == "reject":
        cur.execute(
            "UPDATE decision_links SET state = 'rejected', resolved_at = now() WHERE id = %s",
            (link_id,),
        )
        return {"ok": True, "id": link_id, "action": "reject"}
    applied = False
    if kind == "supersedes":
        applied = supersede(cur, int(old_id), int(new_id), source=source, force=True)
    cur.execute(
        "UPDATE decision_links SET state = 'applied', resolved_at = now() WHERE id = %s",
        (link_id,),
    )
    return {"ok": True, "id": link_id, "action": "confirm", "applied": applied}


def decision_states_for_episode(episode_id: int) -> dict[str, Any]:
    """``decision_states``/``validity`` for ``khipu_get``'s episode payload:
    ``{"decision_states": [{"id", "text", "state", "superseded_by"}, ...],
    "validity": {"current": n, "superseded": n, "retracted": n}}``. Opens its
    own connection (same posture as ``khipu.activity.episode_detail``).
    Never raises; a pre-migration hub or an episode with no decisions gets
    empty/zeroed results."""
    empty = {"decision_states": [], "validity": {"current": 0, "superseded": 0, "retracted": 0}}
    from khipu.db import connect

    try:
        with connect() as conn:
            with conn.cursor() as cur:
                if not _decisions_ready(cur):
                    return empty
                cols = ["id", "text", "superseded_by"]
                if _evidence_ready(cur):
                    cols.append("retracted_at")
                cur.execute(
                    f"SELECT {', '.join(cols)} FROM decisions WHERE episode_id = %s ORDER BY id",
                    (episode_id,),
                )
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001 — additive enrichment, never blocks khipu_get
        _log(f"decision_states_for_episode failed ({type(exc).__name__}: {exc})")
        return empty
    states = []
    counts = {"current": 0, "superseded": 0, "retracted": 0}
    for row in rows:
        state = state_of(row)
        counts[state] += 1
        states.append({"id": row["id"], "text": row["text"], "state": state,
                        "superseded_by": row.get("superseded_by")})
    return {"decision_states": states, "validity": counts}


def enrich_search_results(cur, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
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


def standing_decisions(cur, *, project: str, since: Any, limit: int = 5) -> list[dict[str, Any]]:
    """Non-superseded, non-retracted decisions for ``project`` decided since
    ``since`` — the "Decisions still standing" block in the W4 pushed slice.
    Phase 2, session B: uses ``status="standing"`` (superseded_by IS NULL
    AND, once migration 0024's columns exist, retracted_at IS NULL) instead
    of the older ``include_superseded=False`` shortcut, which only ever
    excluded superseded rows — a retracted-but-not-superseded decision must
    not keep standing here either. Never raises; a pre-migration hub returns
    an empty list."""
    if not project or not _decisions_ready(cur):
        return []
    try:
        return list_decisions(cur, project=project, since=since, limit=limit, status="standing")
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
