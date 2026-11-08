"""Read-only historical lineage preview; app or prompt-bound owner confirmation.

The request stores the exact reviewed set. Confirmation rechecks it under locks
and never expands it. Corrections use the existing per-project undo batches in
one transaction. No model, provider or automatic tidy can apply one without owner authorization.
"""
from __future__ import annotations

import hashlib
import json

from khipu import commitments as co


def _rows(cur, project=None, *, ids=None, lock=False):
    from khipu.db import has_columns

    if not has_columns(cur, "commitments", "origin_reviewed_at"):
        raise ValueError("Job-origin correction needs migration 0049; nothing was changed")
    where, params = [co._not_forgotten_clause(cur).replace("commitments.", "c.")], []
    if project is not None:
        where.append("c.project IS NOT DISTINCT FROM %s")
        params.append(project)
    if ids is not None:
        where.append("c.id = ANY(%s)")
        params.append(ids)
    else:
        where.extend(["c.status = 'open'", "c.owner = 'user'", "c.origin = 'conversation'"])
    # Every surviving wording witness must agree with the opening job's
    # parent. A conversation restatement or an unproven/owner claim is a veto.
    cur.execute(
        "SELECT to_jsonb(c), e.session_id, e.parent_session_id, e.deleted_at, "
        "(SELECT applied_at FROM schema_migrations WHERE version = '0035_commitment_owner_reassigned'), "
        "(SELECT applied_at FROM schema_migrations WHERE version = '0049_commitment_origin_requests'), "
        "EXISTS (SELECT 1 FROM fact_claims f WHERE f.kind = 'commitment' AND f.fact_id = c.id), "
        "EXISTS (SELECT 1 FROM fact_claims f LEFT JOIN episodes w ON w.id = f.episode_id "
        "WHERE f.kind = 'commitment' AND f.fact_id = c.id AND "
        "(f.claimant != 'episode' OR w.id IS NULL OR w.deleted_at IS NOT NULL "
        "OR NULLIF(btrim(w.parent_session_id), '') IS NULL "
        "OR w.parent_session_id IS DISTINCT FROM e.parent_session_id "
        "OR EXISTS (SELECT 1 FROM episodes peer WHERE peer.session_id = w.session_id "
        "AND peer.parent_session_id IS DISTINCT FROM w.parent_session_id))) "
        "OR EXISTS (SELECT 1 FROM episodes peer WHERE peer.session_id = e.session_id "
        "AND peer.parent_session_id IS DISTINCT FROM e.parent_session_id), "
        "EXISTS (SELECT 1 FROM commitment_tidy_items t WHERE t.commitment_id = c.id) "
        "FROM commitments c LEFT JOIN episodes e ON e.id = c.opened_episode WHERE "
        + " AND ".join(where) + " ORDER BY c.id" + (" FOR UPDATE OF c" if lock else ""), tuple(params))
    rows = []
    for row, session, parent, deleted, ownership_floor, cutoff, claims, conflict, history in cur.fetchall():
        row.update(lineage=parent, session=session, episode_deleted=deleted,
                   ownership_floor=ownership_floor, migration_at=cutoff, has_claims=claims, conflicting_claims=conflict, history=history)
        rows.append(row)
    # The hub's deleted_at lags this Mac's forget list; an unreadable list
    # plans and changes nothing tied to an episode.
    return co._fenced_owed_rows(rows)


def _request_references(doc):
    """Keep exact review bindings without retaining wording or display metadata."""
    entries = doc.get("items") if isinstance(doc, dict) else None
    if not isinstance(entries, list):
        return []
    refs, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        cid, fingerprint = entry.get("id"), entry.get("fingerprint")
        if type(cid) is not int or cid <= 0 or cid in seen:
            continue
        proposal = entry.get("proposal_fingerprint")
        if proposal is None and isinstance(entry.get("after"), dict):
            proposal = _fingerprint(entry["after"])
        if any(not isinstance(value, str) or len(value) != 64
               or any(char not in "0123456789abcdef" for char in value)
               for value in (fingerprint, proposal)):
            continue
        seen.add(cid)
        refs.append({"id": cid, "fingerprint": fingerprint, "proposal_fingerprint": proposal})
    return refs


def _request_document(doc):
    return {"format": "khipu-job-origin-preview", "version": 2,
            "items": _request_references(doc)}


def scrub_request_previews(cur):
    """Forget also removes wording from pre-reference request records."""
    from khipu.db import has_columns, has_table

    if not has_table(cur, "commitment_origin_requests"):
        return
    if not has_columns(cur, "commitment_origin_requests", "id", "preview"):
        raise ValueError("Job-origin request records cannot be safely scrubbed")
    cur.execute("SELECT id, preview FROM commitment_origin_requests ORDER BY id FOR UPDATE")
    for rid, doc in cur.fetchall():
        safe = _request_document(doc)
        if doc != safe:
            cur.execute("UPDATE commitment_origin_requests SET preview = %s::jsonb WHERE id = %s",
                        (json.dumps(safe), rid))


def _fingerprint(row):
    return hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()
