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


WRITER_SETTING = "khipu.fact_writer"


def declare_writer(cur) -> None:
    """Declare, for this transaction only, that its fact steps record
    claims. The hub refuses at COMMIT an episode written or merged with fact
    items in a transaction that never declared it (0047's writer gate): an
    old writer whose repeat of facts already on file changes no fact column
    (Astra review 2026-10-23 #5). Call it in the step itself, so a step that
    a test or an old module skips never declares, and again when a fact gap
    is recorded (its savepoint rollback undoes the step's declaration)."""
    cur.execute("SELECT set_config(%s, 'on', true)", (WRITER_SETTING,))


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


# ---- Fact-gap replay (invariant I4) ---------------------------------------------

GAP_REPLAY_LIMIT = 200


def _gap_step(cur, step: str, payload: dict[str, Any], episode_id: int) -> None:
    """Re-run one capture fact step, exactly the calls capture made in it
    (``capture.write_pg`` and its merge path)."""
    name = step[len("merge "):] if step.startswith("merge ") else step
    if name == "commitments":
        from khipu import commitments

        commitments.open_from_episode(cur, payload, episode_id)
        commitments.auto_close(cur, payload, episode_id)
        commitments.close_session_plan(cur, payload, episode_id)
    elif name == "decisions":
        from khipu import decisions

        decisions.insert_decisions_from_episode(cur, payload, episode_id)
    elif name == "decision detection":
        from khipu import decisions

        decisions.detect_reversals_from_episode(cur, payload, episode_id)
    elif name == "close rules":
        from khipu import close_rules

        close_rules.run(cur, payload=payload, episode_id=episode_id)
    elif name == "deliverables":
        from khipu import deliverables

        deliverables.insert_deliverables_from_episode(cur, payload, episode_id)
    else:
        raise ValueError(f"unknown fact step {step!r}")


# The capture steps that read what an earlier step wrote (capture's order,
# ``capture.run_fact_steps`` and the merge path): reversal detection reads the
# decisions just inserted, and the close rules read the commitments and the
# decisions. When the earlier step failed, the later ones ran against
# incomplete state and returned normally, so no gap names them; a replay of
# the earlier step re-runs them after it, in capture's order, in the same
# transaction (third Astra review #4). Each is idempotent.
DEPENDENT_STEPS: dict[str, tuple[str, ...]] = {
    "commitments": ("close rules",),
    "decisions": ("decision detection", "close rules"),
}


def dependent_steps(step: str) -> tuple[str, ...]:
    """The steps a replay of ``step`` re-runs after it (merge steps keep
    their ``merge `` prefix)."""
    prefix = "merge " if step.startswith("merge ") else ""
    name = step[len(prefix):]
    return tuple(prefix + dep for dep in DEPENDENT_STEPS.get(name, ()))


def _gap_payload(raw: Any, step: str, source_id: str | None) -> dict[str, Any] | None:
    """The capture a gap's step ran on: the episode's own (its stored raw),
    or for a merge step the merged capture ``source_id`` names."""
    raw = raw if isinstance(raw, dict) else {}
    if not step.startswith("merge "):
        return {k: v for k, v in raw.items() if k != "merged_from"}
    for entry in raw.get("merged_from") or []:
        if isinstance(entry, dict) and entry.get("source_id") == source_id and isinstance(entry.get("payload"), dict):
            return entry["payload"]
    return None


def replay_gaps(conn, *, limit: int = GAP_REPLAY_LIMIT) -> dict[str, Any]:
    """The nightly's re-run of fact steps kept as gaps (I4): for each open
    ``fact_gaps`` group (episode, step, merged capture), lock the episode,
    re-run that step on its capture and mark the gap resolved, all in one
    transaction (commits each). Idempotent: the step's own dedup turns a
    second run into claims already held. A forgotten episode or source is
    never replayed; its gap is resolved as ``forgotten``. A replay that
    fails rolls back, and its gap stays open (doctor keeps reporting it)
    with the error and one more attempt counted, for the next night. A hub
    without 0047 has none.

    Two runners (two Macs' nightlies) never both replay one group: each
    claims the group's open gap rows ``FOR UPDATE SKIP LOCKED`` after the
    episode lock and before any side effect, and skips a group another
    runner holds or has resolved (second Astra review #3). Groups are
    taken fewest attempts first, then oldest, so groups that keep failing
    move behind ones never tried and cannot starve them (#4). A replayed
    step is followed by the steps that depend on it (``dependent_steps``),
    which had run against its missing rows (third review #4)."""
    from khipu.tombstones import hub_source_forgotten

    with conn.cursor() as cur:
        if not ready(cur):
            conn.rollback()
            return {"ok": True, "skipped": "the hub has no fact provenance yet (migration 0047)"}
        cur.execute(
            "SELECT episode_id, step, source_id, array_agg(id ORDER BY id) FROM fact_gaps "
            "WHERE resolved_at IS NULL GROUP BY episode_id, step, source_id "
            "ORDER BY max(attempts), min(id) LIMIT %s", (int(limit),))
        groups = cur.fetchall()
    conn.commit()
    out = {"ok": True, "replayed": 0, "forgotten": 0, "failed": 0, "unreplayable": 0, "skipped": 0}
    for episode_id, step, source_id, ids in groups:
        ids = [int(i) for i in ids]
        resolution = "replayed"
        try:
            with conn.cursor() as cur:
                # Episode first (the lock order in 3.4), as capture holds it.
                cur.execute("SELECT raw, deleted_at FROM episodes WHERE id = %s FOR SHARE", (episode_id,))
                row = cur.fetchone()
                # Then claim the group's still-open gaps. Locked by another
                # runner, or resolved since the selection: that runner owns
                # it, so this one skips it without a side effect.
                cur.execute("SELECT id FROM fact_gaps WHERE id = ANY(%s) AND resolved_at IS NULL "
                            "ORDER BY id FOR UPDATE SKIP LOCKED", (ids,))
                claimed = sorted(int(r[0]) for r in cur.fetchall())
                cur.execute("SELECT id FROM fact_gaps WHERE id = ANY(%s) AND resolved_at IS NULL ORDER BY id",
                            (ids,))
                still_open = sorted(int(r[0]) for r in cur.fetchall())
                if not claimed or claimed != still_open:
                    conn.rollback()
                    out["skipped"] += 1
                    continue
                ids = claimed
                payload = None if row is None or row[1] is not None else _gap_payload(row[0], step, source_id)
                if row is None or row[1] is not None:
                    resolution = "forgotten"
                elif payload is None:
                    resolution = None  # the merged capture is not in raw: kept open
                    out["unreplayable"] += 1
                elif hub_source_forgotten(cur, payload, None):
                    resolution = "forgotten"
                else:
                    _gap_step(cur, step, payload, int(episode_id))
                    for dependent in dependent_steps(step):
                        _gap_step(cur, dependent, payload, int(episode_id))
                if resolution is not None:
                    cur.execute(
                        "UPDATE fact_gaps SET resolved_at = now(), resolution = %s, attempts = attempts + 1 "
                        "WHERE id = ANY(%s) AND resolved_at IS NULL", (resolution, ids))
                    out[resolution] += 1
                else:
                    cur.execute(
                        "UPDATE fact_gaps SET resolution = %s, attempts = attempts + 1 WHERE id = ANY(%s)",
                        ("unreplayable: the merged capture is not in the episode's raw", ids))
            conn.commit()
        except Exception as exc:  # noqa: BLE001 — the gap stays open for the next night
            conn.rollback()
            out["failed"] += 1
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE fact_gaps SET resolution = %s, attempts = attempts + 1 WHERE id = ANY(%s)",
                        (f"replay failed: {type(exc).__name__}: {exc}"[:500], ids))
                conn.commit()
            except Exception:  # noqa: BLE001 — counted above; nothing else to keep
                conn.rollback()
    return out


# ---- The change token's retention (section 6.1) -----------------------------------

CHANGES_KEEP_DAYS = 30


def prune_changes(conn, *, keep_days: int = CHANGES_KEEP_DAYS) -> dict[str, Any]:
    """Delete ``fact_changes`` rows older than ``keep_days``, in one
    transaction (commits). Only an xid prefix goes: the rows below the
    lowest xid of the rows kept (section 6.1, change token), so ``min(xid)``
    says no older row was removed, and a replica copy whose snapshot is
    past what was pruned is not skipped. The fact gate only ever looks at
    the current transaction's rows, so pruning old ones never affects a
    writer. A hub without 0047 has nothing to prune."""
    with conn.cursor() as cur:
        if not ready(cur):
            conn.rollback()
            return {"ok": True, "skipped": "the hub has no fact provenance yet (migration 0047)"}
        cur.execute(
            "WITH keep AS (SELECT min(xid::text::numeric) AS x FROM fact_changes "
            "              WHERE at >= now() - make_interval(days => %s)) "
            "DELETE FROM fact_changes WHERE xid::text::numeric < "
            "coalesce((SELECT x FROM keep), 'Infinity'::numeric)",
            (int(keep_days),),
        )
        removed = cur.rowcount
    conn.commit()
    return {"ok": True, "pruned": int(removed or 0)}


# ---- One operation deadline (section 6.4) ------------------------------------------
#
# A forget, an owner send and a copy each fix one monotonic deadline. Every
# statement and the COMMIT run under statement_timeout and lock_timeout set
# to the time left; the hub work runs on a daemon thread that the caller
# abandons at the deadline (best-effort cancel, socket shut down, connection
# discarded), so a stalled network cannot hold the caller. The worker never
# writes locally: the caller publishes only a result it received in time.

CONNECT_FLOOR_S = 2.0
CANCEL_TIMEOUT_S = 5.0
# D3: an abandoned transaction's locks end on the server within 10 s of its
# last statement; every statement already ends by its statement_timeout.
SESSION_SETTINGS = ("SET idle_in_transaction_session_timeout = '10s'",
                    "SET tcp_user_timeout = 10000")


class NotDone(RuntimeError):
    """The hub work did not finish before the deadline (or could not start).
    ``uncertain``: a COMMIT was sent and its outcome is unknown."""

    def __init__(self, message: str, *, uncertain: bool = False):
        super().__init__(message)
        self.uncertain = uncertain


def remaining_ms(deadline: float) -> int:
    import time

    ms = int((deadline - time.monotonic()) * 1000)
    if ms <= 0:
        raise NotDone("the operation's deadline passed")
    return ms


class BoundedCursor:
    """A cursor whose every statement runs under the time left."""

    def __init__(self, cur, deadline: float):
        self._cur, self._deadline = cur, deadline

    def bound(self) -> None:
        ms = str(remaining_ms(self._deadline))
        self._cur.execute("SELECT set_config('statement_timeout', %s, true), "
                          "set_config('lock_timeout', %s, true)", (ms, ms))

    def execute(self, query, params=None, **kw):
        self.bound()
        return self._cur.execute(query, params, **kw)

    def __getattr__(self, name):
        return getattr(self._cur, name)


class HubTx:
    """The worker's connection: ``cur`` is bounded, ``commit`` too.
    ``handoff(value)`` runs the caller's ``on_handoff(value)`` on the
    caller's thread and returns its result (its error is raised here): the
    one way hub work gets something published locally, always between
    transactions."""

    def __init__(self, conn, cur, deadline: float, channel, prelude=None):
        self.conn, self.deadline = conn, deadline
        self.cur = BoundedCursor(cur, deadline)
        self.committing = False
        self.prelude = prelude
        self._channel = channel

    def commit(self) -> None:
        self.cur.bound()
        self.committing = True
        self.conn.commit()
        self.committing = False

    def rollback(self) -> None:
        self.conn.rollback()

    def handoff(self, value):
        import threading

        reply: dict[str, Any] = {"done": threading.Event()}
        self._channel.put(("handoff", value, reply))
        reply["done"].wait()
        if "error" in reply:
            raise reply["error"]
        return reply.get("result")


def _abandon(conn) -> None:
    import os
    import socket
    import threading

    def cancel():
        # The non-blocking cancel (libpq 17+): the blocking one holds the
        # interpreter while a stalled network withholds its reply.
        try:
            conn.cancel_safe(timeout=CANCEL_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - best effort; never waited for
            pass

    # Without it cancel_safe falls back to that blocking cancel, so no cancel
    # is sent: the server ends the statement by its statement_timeout (D3).
    try:
        from psycopg import capabilities

        safe = capabilities.has_cancel_safe()
    except Exception:  # noqa: BLE001 - an unknown client is treated as unsafe
        safe = False
    if safe:
        threading.Thread(target=cancel, daemon=True, name="khipu-hub-cancel").start()
    try:
        sock = socket.socket(fileno=os.dup(conn.fileno()))
        try:
            sock.shutdown(socket.SHUT_RDWR)
        finally:
            sock.close()
    except Exception:  # noqa: BLE001 - already closed or never connected
        pass


def hub_call(work, *, deadline: float, connect=None, on_handoff=None, prelude: str | None = None):
    """``work(tx)`` on a fresh hub connection, bounded by ``deadline``
    (section 6.4). ``connect`` takes ``connect_timeout`` (default
    ``khipu.db.connect``). Returns ``work``'s result or raises its error;
    :class:`NotDone` when the deadline came first or no step could start.
    At the deadline the worker is abandoned, never joined: a cancel is sent
    without waiting, the socket is shut down and the connection discarded,
    and nothing it returns later is used. ``prelude``: one parameterless
    single-value SELECT run in the session-settings exchange, before
    ``work`` (no extra round trip), its value ``tx.prelude``; a session
    handed over bounded brings it from its own first exchange
    (``khipu_prelude``)."""
    import queue
    import threading
    import time

    channel: queue.Queue = queue.Queue()
    guard = threading.Lock()
    state: dict[str, Any] = {"conn": None, "tx": None, "abandoned": False}

    def run():
        conn = None
        outcome: dict[str, Any] = {}
        try:
            secs = deadline - time.monotonic()
            if secs < CONNECT_FLOOR_S:
                raise NotDone("too little time left to connect to the hub")
            if connect is None:
                from khipu.db import connect as opener
            else:
                opener = connect
            conn = opener(connect_timeout=int(secs))
            with guard:
                state["conn"] = conn
            try:
                # One exchange: both settings in one simple query, outside a
                # transaction, so they hold for the session. A session handed
                # over already bounded (a pull's, ``khipu_bounded``) has them,
                # and is left as handed: the copy's first exchange ends the
                # pull's read-only transaction itself.
                from psycopg import pq

                early = None
                if not getattr(conn, "khipu_bounded", False):
                    idle = pq.TransactionStatus.IDLE
                    if getattr(getattr(conn, "info", None), "transaction_status", idle) != idle:
                        conn.commit()  # a connection handed over mid-transaction
                    conn.autocommit = True
                    with conn.cursor() as cur:
                        cur.execute("; ".join([*SESSION_SETTINGS, *([prelude] if prelude else [])]))
                        if prelude:
                            while cur.nextset():
                                pass
                            early = cur.fetchone()[0]
                    conn.autocommit = False
                elif prelude:
                    held = getattr(conn, "khipu_prelude", None) or {}
                    if prelude in held:
                        early = held[prelude]
                    else:
                        with conn.cursor() as cur:
                            cur.execute(prelude)
                            early = cur.fetchone()[0]
            except Exception as exc:  # noqa: BLE001 - a session without them is not used
                raise NotDone(f"the hub session could not be bounded ({type(exc).__name__})") from exc
            with conn.cursor() as cur:
                state["tx"] = HubTx(conn, cur, deadline, channel, early)
                outcome["result"] = work(state["tx"])
        except BaseException as exc:  # noqa: BLE001 - handed to the caller
            outcome["error"] = exc
        finally:
            with guard:
                if conn is not None and not state["abandoned"]:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
            channel.put(("finished", outcome, None))

    threading.Thread(target=run, daemon=True, name="khipu-hub-call").start()
    while True:
        try:
            kind, value, reply = channel.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            with guard:
                state["abandoned"] = True
                if state["conn"] is not None:
                    _abandon(state["conn"])
            tx = state["tx"]
            raise NotDone("the hub did not answer before the deadline",
                          uncertain=bool(tx is not None and tx.committing)) from None
        if kind == "finished":
            if "error" in value:
                raise value["error"]
            return value["result"]
        try:
            if on_handoff is None:
                raise NotDone("nothing receives this hub work's handoff")
            reply["result"] = on_handoff(value)
        except BaseException as exc:
            # Not admitted: the worker, between transactions, stops too.
            reply["error"] = exc
            reply["done"].set()
            raise
        reply["done"].set()


# ---- Hub refusals and the identity index (section 6.1) -----------------------------

INDEX_BATCH = 500
INDEX_SETTING = "khipu.provenance_index"


def refusals_ready(cur) -> bool:
    """Whether the hub has 0047's identity index (``episode_identities``)."""
    cur.execute("SELECT to_regclass('public.episode_identities') IS NOT NULL")
    row = cur.fetchone()
    return bool(row) and row[0] is True


def stored_identities(cur, episode_ids) -> dict[int, set[str]]:
    """Every identity each episode carries, in hub form, from what the hub
    stores: the registry's own functions over its raw and each merged
    payload, each receipt's id, the natural key and legacy form 0047's
    triggers compute from the row, its source rows, and the identity rows
    already recorded. Missing episodes are absent from the result."""
    from khipu.tombstones import hub_identity_set

    ids = sorted({int(i) for i in episode_ids})
    if not ids:
        return {}
    cur.execute(
        "SELECT id, raw, khipu_row_identities(raw, harness, session_id, transcript_range, window_id) "
        "FROM episodes WHERE id = ANY(%s)", (ids,))
    out: dict[int, set[str]] = {}
    for eid, raw, row_ids in cur.fetchall():
        found = set(row_ids or [])
        if isinstance(raw, dict):
            found |= hub_identity_set(raw, redact=True)
            for entry in raw.get("merged_from") or []:
                if not isinstance(entry, dict):
                    continue
                if isinstance(entry.get("source_id"), str) and entry["source_id"]:
                    found.add(entry["source_id"])
                if isinstance(entry.get("payload"), dict):
                    found |= hub_identity_set(entry["payload"], redact=True)
        out[int(eid)] = found
    cur.execute("SELECT r.episode_id, r.receipt_ids FROM (SELECT episode_id, "
                "array_remove(array_agg(source_key) || array_agg(payload_sha256), NULL) AS receipt_ids "
                "FROM episode_sources WHERE episode_id = ANY(%s) GROUP BY episode_id) r", (ids,))
    for eid, found in cur.fetchall():
        if int(eid) in out:
            out[int(eid)].update(found or [])
    cur.execute("SELECT episode_id, identity FROM episode_identities WHERE episode_id = ANY(%s)", (ids,))
    for eid, identity in cur.fetchall():
        if int(eid) in out:
            out[int(eid)].add(identity)
    return out


def refuse(cur, identities, *, episode_id: int | None = None, also=()) -> dict[str, Any]:
    """The refusal transaction (section 6.1), in the caller's READ COMMITTED
    transaction; the caller commits. Takes the identity locks sorted,
    records the refusals, then locks every live episode recorded under them
    (and ``also``) in id order and tombstones each: its claims go, its facts
    are re-projected, its vectors and briefs follow, as a forget does. The
    refusal set is exactly ``identities``: a carrier's other identities are
    not refused. Returns the refused identities and the cascade of each
    episode it tombstoned."""
    from khipu import forget

    refused = sorted({str(i) for i in identities if i})
    cur.execute("SELECT khipu_lock_identities(%s::text[])", (refused,))
    if refused:
        cur.execute("INSERT INTO forgotten_sources (identity, episode_id) "
                    "SELECT x, %s FROM unnest(%s::text[]) AS t(x) ORDER BY x ON CONFLICT DO NOTHING",
                    (episode_id, refused))
    cur.execute("SELECT DISTINCT episode_id FROM episode_identities WHERE identity = ANY(%s)", (refused,))
    carriers = {int(r[0]) for r in cur.fetchall()} | {int(i) for i in also}
    cur.execute("SELECT id FROM episodes WHERE id = ANY(%s) AND deleted_at IS NULL ORDER BY id FOR UPDATE",
                (sorted(carriers),))
    live = [int(r[0]) for r in cur.fetchall()]
    return {"refused": refused, "tombstoned": live,
            "cascades": {eid: forget.tombstone_on_hub(cur, eid) for eid in live}}


def _copy_ready(cur) -> tuple[str, int, int] | None:
    cur.execute("SELECT to_regclass('public.fact_copy_ready') IS NOT NULL")
    if not cur.fetchone()[0]:
        return None
    cur.execute("SELECT state, target_id, indexed_through FROM fact_copy_ready FOR UPDATE")
    row = cur.fetchone()
    return (str(row[0]), int(row[1]), int(row[2])) if row else None


def _index_batch(cur, through: int, target: int, batch: int) -> dict[str, Any]:
    """One batch of the index pass, in the caller's transaction."""
    cur.execute("SELECT id FROM episodes WHERE id > %s AND id <= %s ORDER BY id LIMIT %s",
                (through, target, batch))
    ids = [int(r[0]) for r in cur.fetchall()]
    idents = stored_identities(cur, ids)
    every = sorted(set().union(*idents.values())) if idents else []
    cur.execute("SELECT khipu_lock_identities(%s::text[])", (every,))
    cur.execute("SELECT id FROM episodes WHERE id = ANY(%s) ORDER BY id FOR UPDATE", (ids,))
    cur.execute("SELECT set_config(%s, 'on', true)", (INDEX_SETTING,))
    rows = sorted((k, eid) for eid, found in idents.items() for k in found)
    if rows:
        cur.execute("INSERT INTO episode_identities (identity, episode_id) "
                    "SELECT * FROM unnest(%s::text[], %s::bigint[]) ON CONFLICT DO NOTHING",
                    ([k for k, _ in rows], [e for _, e in rows]))
    # The frozen set of forgets the owner asked for before this design: the
    # only episodes whose every identity becomes a refusal.
    cur.execute("SELECT episode_id FROM forgotten_at_cutover WHERE episode_id = ANY(%s)", (ids,))
    cutover = sorted(int(r[0]) for r in cur.fetchall())
    refuse_rows = sorted({(k, eid) for eid in cutover for k in idents.get(eid, ())})
    if refuse_rows:
        cur.execute("INSERT INTO forgotten_sources (identity, episode_id) "
                    "SELECT * FROM unnest(%s::text[], %s::bigint[]) ON CONFLICT DO NOTHING",
                    ([k for k, _ in refuse_rows], [e for _, e in refuse_rows]))
    # The duplicate sweep: every live episode now recorded under a refusal.
    cur.execute("SELECT DISTINCT ei.episode_id FROM episode_identities ei "
                "JOIN forgotten_sources f ON f.identity = ei.identity "
                "JOIN episodes e ON e.id = ei.episode_id AND e.deleted_at IS NULL "
                "WHERE ei.identity = ANY(%s)", (every,))
    carriers = sorted(int(r[0]) for r in cur.fetchall())
    from khipu import forget

    cur.execute("SELECT id FROM episodes WHERE id = ANY(%s) AND deleted_at IS NULL ORDER BY id FOR UPDATE",
                (carriers,))
    swept = [int(r[0]) for r in cur.fetchall()]
    for eid in swept:
        forget.tombstone_on_hub(cur, eid)
    last = ids[-1] if ids else target
    cur.execute("UPDATE fact_copy_ready SET indexed_through = %s", (last,))
    return {"indexed": len(ids), "refused": len(refuse_rows), "tombstoned": swept, "through": last}


def index_identities(conn, *, batch: int = INDEX_BATCH, dry_run: bool = False) -> dict[str, Any]:
    """``khipu provenance index`` (section 6.1): index every episode up to
    ``fact_copy_ready.target_id`` in batches of ``batch`` ids, one
    transaction each (an aborted batch advances nothing and is redone), then
    mark copies ready. ``dry_run`` only counts the live episodes the sweep
    would tombstone. A hub without the index has nothing to do."""
    if dry_run:
        return _index_dry_run(conn)
    out: dict[str, Any] = {"ok": True, "batches": 0, "indexed": 0, "refused": 0, "tombstoned": []}
    while True:
        try:
            with conn.cursor() as cur:
                state = _copy_ready(cur)
                if state is None:
                    conn.rollback()
                    return {**out, "skipped": "the hub has no identity index yet (migration 0047)"}
                name, target, through = state
                if through >= target:
                    if name != "ready":
                        cur.execute("UPDATE fact_copy_ready SET state = 'ready'")
                    conn.commit()
                    return {**out, "state": "ready", "indexed_through": through, "target_id": target}
                done = _index_batch(cur, through, target, batch)
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - the batch rolled back; the next run redoes it
            conn.rollback()
            return {**out, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
        out["batches"] += 1
        out["indexed"] += done["indexed"]
        out["refused"] += done["refused"]
        out["tombstoned"] += done["tombstoned"]


def _index_dry_run(conn) -> dict[str, Any]:
    """The live episodes the index pass would tombstone, from the same
    frozen set and the same identities, read without locks: an episode the
    pass has yet to index by what it stores, every other by its rows."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.fact_copy_ready') IS NOT NULL")
        if not cur.fetchone()[0]:
            conn.rollback()
            return {"ok": True, "would_tombstone": 0, "skipped": "no identity index yet (migration 0047)"}
        cur.execute("SELECT target_id, indexed_through FROM fact_copy_ready")
        target, through = (int(x) for x in cur.fetchone())

        def identities(ids: list[int]) -> dict[int, set[str]]:
            pending = [i for i in ids if through < i <= target]
            found = stored_identities(cur, pending)
            cur.execute("SELECT episode_id, identity FROM episode_identities WHERE episode_id = ANY(%s)",
                        ([i for i in ids if i not in found],))
            for eid, identity in cur.fetchall():
                found.setdefault(int(eid), set()).add(identity)
            return found

        cur.execute("SELECT identity FROM forgotten_sources")
        refused = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT episode_id FROM forgotten_at_cutover WHERE episode_id > %s ORDER BY episode_id",
                    (through,))
        cutover = [int(r[0]) for r in cur.fetchall()]
        for start in range(0, len(cutover), INDEX_BATCH):
            for found in identities(cutover[start:start + INDEX_BATCH]).values():
                refused |= found
        hits: list[int] = []
        last = 0
        while True:
            cur.execute("SELECT id FROM episodes WHERE id > %s AND deleted_at IS NULL ORDER BY id LIMIT %s",
                        (last, INDEX_BATCH))
            ids = [int(r[0]) for r in cur.fetchall()]
            if not ids:
                break
            hits += sorted(eid for eid, found in identities(ids).items() if found & refused)
            last = ids[-1]
    conn.rollback()
    return {"ok": True, "would_tombstone": len(hits), "episodes": hits}


# ---- Doctor (section 8, phase 5) -------------------------------------------------
#
# Visibility only: the block is red (``ok`` False) only when it could not be
# read, and it never gates doctor's overall ``ok``. A hub without 0047 is a
# plain state, not a fault: the schema check already names the pending update.

_MIGRATION = "0047_fact_provenance"


def _doctor_read(cur) -> dict[str, Any]:
    cur.execute("SELECT to_regclass('public.fact_claims') IS NOT NULL")
    row = cur.fetchone()
    if not (row and row[0] is True):
        return {
            "state": "not_migrated",
            "reason": ("Fact provenance (database update 0047) is not on the memory database yet; "
                       "facts are stored and forgotten as before until it is."),
        }
    out: dict[str, Any] = {"state": "active"}
    cur.execute("SELECT applied_at FROM schema_migrations WHERE version = %s", (_MIGRATION,))
    row = cur.fetchone()
    applied_at = row[0] if row else None
    out["applied_at"] = applied_at
    # I4: capture fact steps that failed and were kept as a gap.
    cur.execute("SELECT step, count(*) FROM fact_gaps WHERE resolved_at IS NULL "
                "GROUP BY step ORDER BY step")
    by_step = {step: int(n) for step, n in cur.fetchall()}
    cur.execute("SELECT count(*), min(at) FROM fact_gaps WHERE resolved_at IS NULL")
    n_gaps, oldest = cur.fetchone()
    out["fact_gaps"] = {"open": int(n_gaps or 0), "oldest_at": oldest, "by_step": by_step}
    # Section 4's witness backfill: projects with live episodes not covered yet.
    out["backfill_pending_projects"] = len(pending_projects(cur))
    # Section 4's accepted limit. Only commitments count sightings
    # (seen_count). A legacy commitment is one opened before 0047.
    # at_risk: open legacy commitments with more sightings than live proven
    # claimants (a restater whose words were overwritten); forgetting every
    # proven claimant retires them. retired: legacy commitments retired as
    # forgotten since 0047 that had more than one sighting. Forget deletes
    # the forgotten episodes' claims, so the hub no longer knows how many of
    # those sightings were proven: an upper bound on the facts the limit cost.
    limit = {"at_risk": 0, "retired": 0}
    if applied_at is not None:
        cur.execute(
            "SELECT count(*) FROM commitments c WHERE c.status IN ('open', 'stale') "
            "AND c.opened_at < %(at)s AND c.seen_count > ("
            " SELECT count(DISTINCT fc.episode_id) FROM fact_claims fc "
            " JOIN episodes e ON e.id = fc.episode_id AND e.deleted_at IS NULL "
            " WHERE fc.kind = 'commitment' AND fc.fact_id = c.id AND fc.claimant = 'episode' "
            " AND fc.field IN ('text', 'restatement'))",
            {"at": applied_at},
        )
        limit["at_risk"] = int(cur.fetchone()[0] or 0)
        cur.execute(
            "SELECT count(*) FROM commitments c WHERE c.status = 'closed' "
            "AND c.close_reason = 'forgotten' AND c.opened_at < %(at)s "
            "AND c.closed_at >= %(at)s AND c.seen_count > 1",
            {"at": applied_at},
        )
        limit["retired"] = int(cur.fetchone()[0] or 0)
    out["limit"] = limit
    return out


def doctor_check(conn=None) -> dict[str, Any]:
    """Doctor's fact-provenance block: whether 0047 is on the hub, open
    ``fact_gaps`` and section 4's limit count. Read-only; ``conn`` is the
    caller's and stays open, else the resolved hub is opened and closed here.
    No configured database is a skip; a failed read is red, never a pass."""
    block: dict[str, Any] = {"ok": True, "warnings": []}
    try:
        if conn is None:
            from khipu.db import connect, dsn_configured

            if not dsn_configured():
                return {**block, "skipped": "no database configured"}
            with connect() as owned, owned.cursor() as cur:
                block.update(_doctor_read(cur))
                owned.rollback()
        else:
            with conn.cursor() as cur:
                block.update(_doctor_read(cur))
    except Exception as e:  # noqa: BLE001  (a failed check must not look like a pass)
        return {**block, "ok": False, "error": f"{type(e).__name__}: {e}",
                "warnings": [f"Fact provenance could not be checked: {type(e).__name__}"]}
    gaps = (block.get("fact_gaps") or {}).get("open") or 0
    if gaps:
        block["warnings"].append(
            f"{gaps} capture fact step{'s' if gaps != 1 else ''} failed and "
            f"{'were' if gaps != 1 else 'was'} kept as a gap (fact_gaps), not yet resolved")
    retired = (block.get("limit") or {}).get("retired") or 0
    if retired:
        block["warnings"].append(
            f"{retired} older commitment{'s' if retired != 1 else ''} retired when "
            f"{'their' if retired != 1 else 'its'} proven sources were forgotten; a restatement "
            "from before fact provenance could not be proven and may have been lost")
    return block
