"""Fact provenance: every stored wording value has one claimant.

Scope: docs/plans/2026-10-23-fact-provenance.md. A commitment's, decision's
or deliverable's wording is a pure function of its live claims (rows in
``fact_claims``, migration 0047). This module is the only way Python code
records a claim or recomputes a fact:

* :func:`record_claim` adds one claim (a capture episode's, the owner's).
  A replayed capture writes nothing new.
* :func:`project` recomputes one fact from its claims by calling the hub's
  ``khipu_project_fact()``, the single implementation the migration's own
  initial projection also used, and records the fact in ``fact_changes`` for
  this transaction. The hub refuses, at COMMIT, any wording change to a fact
  that was not projected in the same transaction (SQLSTATE 08K01).

Capture dedup, forget and backfill change wording only by adding or deleting
claims and then calling :func:`project`; nothing re-reads raw captures to
work out who said what.

:func:`ready` is the positive-only readiness check (section 5.1): a hub
without the table answers False every time it is asked, so a long-lived
process sees the migration on its next transaction; once True it stays True.
"""
from __future__ import annotations

from typing import Any

KINDS = ("commitment", "decision", "deliverable")
FIELDS: dict[str, tuple[str, ...]] = {
    "commitment": ("text", "restatement", "pr", "pr_ref"),
    "decision": ("text", "rationale", "evidence"),
    "deliverable": ("title", "support"),
}
CLAIMANTS = ("episode", "owner", "legacy_unproven")
# What the hub raises for a writer that bypasses the projector or forgets a
# claimed episode. Class 08: psycopg raises it as plain OperationalError.
OLD_WRITER_SQLSTATE = "08K01"
OLD_WRITER_MESSAGE = "khipu: this writer predates fact provenance; upgrade it"
PROJECTION_RESULTS = ("projected", "retired", "deleted", "missing")

_READY = False


def ready(cur) -> bool:
    """Whether the hub has migration 0047's ``fact_claims``. Only a yes is
    cached (section 5.1)."""
    global _READY
    if _READY:
        return True
    cur.execute("SELECT to_regclass('public.fact_claims') IS NOT NULL")
    row = cur.fetchone()
    # Only a real boolean yes counts: a cursor that answers anything else
    # (a test double, a stale result) is not a hub with the table.
    _READY = bool(row) and row[0] is True
    return _READY


def _reset_ready_cache() -> None:
    """Tests: forget a cached yes (a fresh database per test)."""
    global _READY
    _READY = False


def _check(kind: str, field: str | None = None, claimant: str | None = None) -> None:
    if kind not in KINDS:
        raise ValueError(f"unknown fact kind {kind!r}")
    if field is not None and field not in FIELDS[kind]:
        raise ValueError(f"{kind} has no claim field {field!r}")
    if claimant is not None and claimant not in CLAIMANTS:
        raise ValueError(f"unknown claimant {claimant!r}")


def record_claim(cur, kind: str, fact_id: int, field: str, value: Any, *,
                 episode_id: int | None = None, claimant: str | None = None) -> bool:
    """Record that ``episode_id`` (or the owner, with ``claimant="owner"``)
    supplied ``value`` for ``field`` of fact ``(kind, fact_id)``. Returns
    whether a new claim was written (False for a replay).

    ``claimant`` defaults to ``"episode"`` when ``episode_id`` is given and
    to ``"owner"`` otherwise. A ``None`` value records nothing. Values are
    stored as text: a PR number as its decimal digits, decision evidence as
    its JSON text. The hub refuses an episode claim for a forgotten or
    missing episode (check_violation) after taking FOR SHARE on its row, so
    call this with the episode lock order: episode rows first, then facts.
    The caller commits, and must :func:`project` the fact in the same
    transaction."""
    if claimant is None:
        claimant = "episode" if episode_id is not None else "owner"
    _check(kind, field, claimant)
    if (claimant == "episode") != (episode_id is not None):
        raise ValueError("an episode claim needs episode_id, and only an episode claim has one")
    if value is None:
        return False
    if field == "evidence" and not isinstance(value, str):
        import json

        value = json.dumps(value)
    cur.execute(
        "INSERT INTO fact_claims (kind, fact_id, field, value, claimant, episode_id) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (kind, int(fact_id), field, str(value), claimant, episode_id),
    )
    return cur.rowcount > 0


def project(cur, kind: str, fact_id: int) -> str:
    """Recompute fact ``(kind, fact_id)`` from its live claims (section 3.2)
    and record it in ``fact_changes`` for this transaction. Returns
    ``"projected"``, ``"retired"`` (no live or owner claim: closed or
    retracted as ``forgotten``), ``"deleted"`` (a retired deliverable) or
    ``"missing"`` (no such row). Locks the fact row FOR UPDATE."""
    _check(kind)
    cur.execute("SELECT khipu_project_fact(%s, %s)", (kind, int(fact_id)))
    return str(cur.fetchone()[0])


def claims(cur, kind: str, fact_id: int) -> list[dict[str, Any]]:
    """Every claim on one fact, oldest first. Hub only: legacy_unproven
    values are listed here and must never be shown to a reader."""
    _check(kind)
    cur.execute(
        "SELECT field, value, claimant, episode_id FROM fact_claims "
        "WHERE kind = %s AND fact_id = %s ORDER BY created_at, id",
        (kind, int(fact_id)),
    )
    return [dict(zip(("field", "value", "claimant", "episode_id"), r)) for r in cur.fetchall()]


def is_old_writer_refusal(exc: BaseException) -> bool:
    """Whether ``exc`` is the hub's 08K01 refusal of a writer that bypassed
    the projector (or forgot a claimed episode)."""
    return getattr(getattr(exc, "diag", None), "sqlstate", None) == OLD_WRITER_SQLSTATE \
        or getattr(exc, "sqlstate", None) == OLD_WRITER_SQLSTATE


# ---- Legacy witness backfill (section 4) ----------------------------------------
#
# Migration 0047 attributes only what the writer inventory proves. A legacy
# restater (an episode that deduplicated into a fact before claims existed)
# left its words in latest_text, prs or pr_check.refs, now legacy_unproven
# claims the projection never shows, or left nothing at all when a later
# restater overwrote them. The backfill streams each project's live episodes,
# computes the value capture's own writer would have stored for each item
# (the same value functions), and gives the episode a claim on a fact only
# when that value EXACTLY equals a value already on the fact. Kind and the
# current owner are never compared (capture's dedup did not compare kind, and
# reassign changes the owner). No fuzzy matching: a restater whose words were
# overwritten cannot be proven, and its fact retires when every provable
# claimant is forgotten (the accepted limit in section 4).

BACKFILL_BATCH = 500
# project() under a name the backfill's ``project`` arguments do not shadow.
_reproject = project


def _scope_key(project: str | None) -> str:
    return project or ""


def backfilled(cur, project: str | None) -> bool:
    """Whether the witness backfill has covered ``project``."""
    cur.execute("SELECT backfilled_at IS NOT NULL FROM provenance_projects WHERE project = %s",
                (_scope_key(project),))
    row = cur.fetchone()
    return bool(row and row[0])


def pending_projects(cur) -> list[str | None]:
    """Projects with live episodes the backfill has not covered yet (None
    stands for episodes with no project)."""
    cur.execute(
        "SELECT DISTINCT coalesce(e.project, '') FROM episodes e WHERE e.deleted_at IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM provenance_projects p "
        "WHERE p.project = coalesce(e.project, '') AND p.backfilled_at IS NOT NULL) ORDER BY 1")
    return [r[0] or None for r in cur.fetchall()]


def _loop_values(item: Any) -> tuple[str, ...]:
    """The values capture's writer would store for one open-loop item: its
    words (a restatement's ``latest_text``) and, when the due phrase did not
    parse, the anchor's stored text with the phrase kept on it."""
    from khipu.commitments import _normalize_open_loop, _parse_due_after

    norm = _normalize_open_loop(item)
    if norm is None:
        return ()
    text = norm["text"]
    due_sql, _ = _parse_due_after(norm["due_after"])
    phrase = str(norm["due_after"] or "").strip()
    if due_sql == "NULL" and phrase:
        return (text, f"{text} (due: {phrase})")
    return (text,)


def _decision_value(item: Any) -> str | None:
    """The text capture's decision writer stores for one item (exactly
    ``insert_decisions_from_episode``'s value), or None for one it skips."""
    from khipu.decisions import _redacted_value, normalize_text

    text = _redacted_value(str(item or "").strip())
    if not text or not normalize_text(text):
        return None
    return text


def _deliverable_key(item: Any):
    """``insert_deliverables_from_episode``'s dedup key and title for one
    item, or None for one it skips."""
    from khipu.deliverables import VALID_KINDS

    if not isinstance(item, dict):
        return None
    kind = str(item.get("kind") or "").strip().lower()
    if kind not in VALID_KINDS:
        return None
    path = str(item.get("path") or "").strip() or None
    url = str(item.get("url") or "").strip() or None
    title = str(item.get("title") or "").strip() or None
    if not path and not url and not title:
        return None
    return kind, path, url, title


def _json_list(value: Any) -> list:
    if isinstance(value, str):
        import json

        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


def _prove_prs(cur, fact_id: int, episode_id: int, words: str, project: str | None) -> int:
    """The PR numbers and refs ``words`` name that are already on the fact
    (any claimant): this episode claims each. Returns the count recorded."""
    from khipu.close_rules import parse_prs, pr_requirement_labels

    cur.execute("SELECT field, value FROM fact_claims WHERE kind = 'commitment' AND fact_id = %s "
                "AND field IN ('pr', 'pr_ref')", (fact_id,))
    held = {(f, v) for f, v in cur.fetchall()}
    if not held:
        return 0
    added = 0
    for n in parse_prs(words):
        if ("pr", str(n)) in held:
            added += record_claim(cur, "commitment", fact_id, "pr", n, episode_id=episode_id)
    for label in pr_requirement_labels(words, project):
        if ("pr_ref", label) in held:
            added += record_claim(cur, "commitment", fact_id, "pr_ref", label, episode_id=episode_id)
    return added


def _witness_commitments(cur, project: str | None, items: list[tuple[int, str]], counts: dict) -> set[int]:
    """Claims for open-loop values that exactly equal a text or restatement
    value on a commitment in the project (retired facts excluded)."""
    if not items:
        return set()
    values = sorted({v for _e, v in items})
    cur.execute(
        "SELECT fc.fact_id, fc.value, fc.field, fc.episode_id FROM commitments c "
        "JOIN fact_claims fc ON fc.kind = 'commitment' AND fc.fact_id = c.id "
        "WHERE c.project IS NOT DISTINCT FROM %s AND fc.field IN ('text', 'restatement') "
        "AND fc.value = ANY(%s) AND NOT (c.status = 'closed' AND c.close_reason IS NOT DISTINCT FROM 'forgotten')",
        (project, values),
    )
    by_value: dict[str, list[tuple[int, str, int | None]]] = {}
    for fid, value, field, by in cur.fetchall():
        by_value.setdefault(value, []).append((int(fid), field, by))
    touched: set[int] = set()
    for eid, value in items:
        rows = by_value.get(value) or []
        if not rows:
            continue
        # The episode already holds this item on a fact (it opened its own,
        # e.g. an owner-separated twin with the same words, or a capture
        # after 0047 claimed it): it supports that one only.
        own = sorted({fid for fid, _field, by in rows if by == eid})
        for fid in own or sorted({fid for fid, _f, _b in rows}):
            added = 0
            if not own:
                added += record_claim(cur, "commitment", fid, "restatement", value, episode_id=eid)
            added += _prove_prs(cur, fid, eid, value, project)
            if added:
                counts["claims"] += added
                touched.add(fid)
    return touched


def _witness_decisions(cur, project: str | None, items: list[tuple[int, str]], counts: dict) -> set[int]:
    if not items:
        return set()
    values = sorted({v for _e, v in items})
    cur.execute(
        "SELECT fc.fact_id, fc.value, fc.episode_id FROM decisions d "
        "JOIN fact_claims fc ON fc.kind = 'decision' AND fc.fact_id = d.id AND fc.field = 'text' "
        "WHERE d.project IS NOT DISTINCT FROM %s AND fc.value = ANY(%s) "
        "AND d.retract_reason IS DISTINCT FROM 'forgotten'",
        (project, values),
    )
    by_value: dict[str, list[tuple[int, int | None]]] = {}
    for fid, value, by in cur.fetchall():
        by_value.setdefault(value, []).append((int(fid), by))
    touched: set[int] = set()
    for eid, value in items:
        rows = by_value.get(value) or []
        if any(by == eid for _fid, by in rows):
            continue
        for fid in sorted({fid for fid, _b in rows}):
            if record_claim(cur, "decision", fid, "text", value, episode_id=eid):
                counts["claims"] += 1
                touched.add(fid)
    return touched


def _witness_deliverables(cur, project: str | None, items: list[tuple[int, tuple]], counts: dict) -> set[int]:
    touched: set[int] = set()
    for eid, (kind, path, url, title) in items:
        title_clause = "AND title IS NOT DISTINCT FROM %s " if not path and not url else ""
        params = (project, kind, path, url, title) if title_clause else (project, kind, path, url)
        cur.execute(
            "SELECT id FROM deliverables WHERE project IS NOT DISTINCT FROM %s "
            "AND kind = %s AND path IS NOT DISTINCT FROM %s AND url IS NOT DISTINCT FROM %s "
            + title_clause + "ORDER BY id",
            params,
        )
        for (fid,) in cur.fetchall():
            if title:
                added = record_claim(cur, "deliverable", fid, "title", title, episode_id=eid)
            else:
                added = record_claim(cur, "deliverable", fid, "support", "", episode_id=eid)
            if added:
                counts["claims"] += 1
                touched.add(int(fid))
    return touched


def backfill_project(cur, project: str | None, *, batch: int = BACKFILL_BATCH,
                     between_batches=None) -> dict[str, Any]:
    """Witness backfill of one project (section 4). Streams the project's
    live episodes ``batch`` at a time, selecting only their open loops,
    decisions and deliverables (their own and their merged captures'), so
    memory is bounded by the batch, not the project. Each re-projected fact
    is recorded. Marks the project backfilled. ``between_batches`` (the
    nightly passes its connection's commit) runs after each batch; forget
    passes none and keeps everything in its own transaction."""
    counts = {"project": project, "episodes": 0, "claims": 0, "projected": 0}
    last = 0
    while True:
        cur.execute(
            "SELECT e.id, "
            "jsonb_path_query_array(e.raw, 'lax $.open_loops[*]') "
            "|| jsonb_path_query_array(e.raw, 'lax $.merged_from[*].payload.open_loops[*]'), "
            "jsonb_path_query_array(e.raw, 'lax $.decisions[*]') "
            "|| jsonb_path_query_array(e.raw, 'lax $.merged_from[*].payload.decisions[*]'), "
            "jsonb_path_query_array(e.raw, 'lax $.deliverables[*]') "
            "|| jsonb_path_query_array(e.raw, 'lax $.merged_from[*].payload.deliverables[*]') "
            "FROM episodes e WHERE e.deleted_at IS NULL AND e.project IS NOT DISTINCT FROM %s AND e.id > %s "
            "ORDER BY e.id LIMIT %s",
            (project, last, batch),
        )
        rows = cur.fetchall()
        if not rows:
            break
        last = int(rows[-1][0])
        counts["episodes"] += len(rows)
        loops: list[tuple[int, str]] = []
        decided: list[tuple[int, str]] = []
        delivered: list[tuple[int, tuple]] = []
        for eid, lo, de, dv in rows:
            eid = int(eid)
            for item in _json_list(lo):
                loops += [(eid, v) for v in _loop_values(item)]
            for item in _json_list(de):
                value = _decision_value(item)
                if value is not None:
                    decided.append((eid, value))
            for item in _json_list(dv):
                key = _deliverable_key(item)
                if key is not None:
                    delivered.append((eid, key))
        rows = None
        touched = [("commitment", f) for f in sorted(_witness_commitments(cur, project, loops, counts))]
        touched += [("decision", f) for f in sorted(_witness_decisions(cur, project, decided, counts))]
        touched += [("deliverable", f) for f in sorted(_witness_deliverables(cur, project, delivered, counts))]
        for kind, fid in touched:
            if _reproject(cur, kind, fid) in ("projected", "retired", "deleted"):
                counts["projected"] += 1
        if between_batches is not None:
            between_batches()
    cur.execute(
        "INSERT INTO provenance_projects (project, backfilled_at) VALUES (%s, now()) "
        "ON CONFLICT (project) DO UPDATE SET backfilled_at = EXCLUDED.backfilled_at",
        (_scope_key(project),),
    )
    return counts


def ensure_backfilled(cur, project: str | None) -> dict[str, Any] | None:
    """Forget's step before it deletes an episode's claims (section 4): the
    project's witness backfill, in the caller's transaction, when the
    project has not been covered yet. Returns its counts, or None when it
    already was."""
    if backfilled(cur, project):
        return None
    return backfill_project(cur, project)


def backfill(conn, *, batch: int = BACKFILL_BATCH) -> dict[str, Any]:
    """``khipu provenance backfill`` and the nightly: every pending project,
    committing after each batch and each project. A hub without 0047 has
    nothing to backfill."""
    with conn.cursor() as cur:
        if not ready(cur):
            conn.rollback()
            return {"ok": True, "skipped": "the hub has no fact provenance yet (migration 0047)"}
        projects = pending_projects(cur)
    conn.commit()
    done = []
    for name in projects:
        with conn.cursor() as cur:
            counts = backfill_project(cur, name, batch=batch, between_batches=conn.commit)
        conn.commit()
        done.append(counts)
    return {"ok": True, "projects": len(done),
            "claims": sum(c["claims"] for c in done),
            "projected": sum(c["projected"] for c in done),
            "episodes": sum(c["episodes"] for c in done)}


# ---- The replica change stream's retention (section 6) -------------------------

CHANGES_KEEP_DAYS = 30


def prune_changes(conn, *, keep_days: int = CHANGES_KEEP_DAYS) -> dict[str, Any]:
    """Delete ``fact_changes`` rows older than ``keep_days`` and raise
    ``fact_changes_floor`` to the highest xid removed, in one transaction
    (commits). A replica whose stream cursor is at or below the floor may
    have missed a change and asks for a full dump. The fact gate only ever
    looks at the current transaction's rows, so pruning old ones never
    affects a writer. A hub without 0047 has nothing to prune."""
    with conn.cursor() as cur:
        if not ready(cur):
            conn.rollback()
            return {"ok": True, "skipped": "the hub has no fact provenance yet (migration 0047)"}
        cur.execute(
            "WITH gone AS (DELETE FROM fact_changes WHERE at < now() - make_interval(days => %s) "
            "RETURNING xid) SELECT count(*), max(xid::text::numeric) FROM gone",
            (int(keep_days),),
        )
        removed, through = cur.fetchone()
        if removed:
            cur.execute(
                "INSERT INTO fact_changes_floor (one, pruned_through) VALUES (true, %s::text::xid8) "
                "ON CONFLICT (one) DO UPDATE SET pruned_through = "
                "greatest(fact_changes_floor.pruned_through, excluded.pruned_through)",
                (str(through),),
            )
    conn.commit()
    return {"ok": True, "pruned": int(removed or 0)}
