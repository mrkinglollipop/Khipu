"""Read-only historical lineage preview; app or prompt-bound owner confirmation.

The request stores the exact reviewed set. Confirmation rechecks it under locks
and never expands it. Corrections use the existing per-project undo batches in
one transaction. No model, provider or automatic tidy can apply one without owner authorization.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets

from khipu import commitments as co, owed_tidy


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


def _ownership_established(row):
    """Positive per-row proof is required, and legacy rows have none.

    Migration times and null reassignment metadata cannot establish that all
    writers recorded ownership choices (long-lived writers cached schemas).
    No current historical certificate proves untouched ownership, so quarantine
    every historical row. Never manufacture proof by backfilling this policy.
    """
    return False


def _proposal(row):
    if (row.get("status") != "open" or row.get("owner") != "user"
            or row.get("origin") != "conversation" or not row.get("has_claims")
            or row.get("conflicting_claims") or row.get("history")
            or row.get("episode_deleted") is not None):
        return None
    parent, session = row.get("lineage"), row.get("session")
    if (not isinstance(parent, str) or not parent.strip() or parent != parent.strip()
            or not session or parent == session):
        return None
    # Evidence of a hand change, or insufficient evidence to rule one out,
    # keeps the item with its current owner. Even expired snoozes are kept.
    protected = ("origin_reviewed_at", "owner_reassigned_at", "owner_reassigned_by",
                 "owner_reassign_reason", "owner_prior", "owner_prior_label", "due_after",
                 "closed_at", "closed_episode", "close_reason", "closed_by", "close_rule_key",
                 "reopened_rule_keys", "latest_text", "tidy_batch")
    if any(row.get(key) for key in protected):
        return None
    opened, cutoff = co._as_utc(row.get("opened_at")), co._as_utc(row.get("migration_at"))
    ownership_floor = co._as_utc(row.get("ownership_floor"))
    seen = co._as_utc(row.get("last_seen_at"))
    if (not _ownership_established(row) or not opened or not cutoff or not ownership_floor or opened < ownership_floor
            or opened > cutoff or (seen and seen > opened)):
        return None
    owner, label = co.resolve_owner_and_label(
        row.get("text") or "", kind=row.get("kind"), declared="user", origin="job",
        future_trigger=bool(row.get("future_trigger")) or co.has_future_trigger(row.get("text") or ""))
    if owner != "agent":
        return None
    return {"owner": owner, "owner_label": label, "origin": "job"}


def _fingerprint(row):
    return hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()


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


def _preview_document(items, unestablished=0):
    kinds, projects = {}, {}
    for item in items:
        kind, project = item["kind"], item.get("project") or ""
        kinds[kind] = kinds.get(kind, 0) + 1
        projects[project] = projects.get(project, 0) + 1
    return {"format": "khipu-job-origin-preview", "version": 1,
            "counts": {"total": len(items), "by_kind": kinds, "by_project": projects},
            "quarantined": {"ownership_unestablished": unestablished},
            "sample": items[:20], "items": items}


def _preview_item(row, after):
    return {"id": row["id"], "project": row.get("project"), "kind": row["kind"],
            "text": row["text"], "after": after, "fingerprint": _fingerprint(row)}


def _pending_preview(cur, doc):
    refs = _request_references(doc)
    rows = {row["id"]: row for row in _rows(cur, ids=[ref["id"] for ref in refs])} if refs else {}
    items = []
    for ref in refs:
        row = rows.get(ref["id"])
        if row is None or _fingerprint(row) != ref["fingerprint"]:
            continue
        after = _proposal(row)
        if after is not None and _fingerprint(after) == ref["proposal_fingerprint"]:
            items.append(_preview_item(row, after))
    return _preview_document(items)


def preview(cur, project=None):
    from khipu.config import read_only_config

    with read_only_config():
        items, unestablished = [], 0
        for row in _rows(cur, project):
            opened = co._as_utc(row.get("opened_at"))
            floor = co._as_utc(row.get("ownership_floor"))
            if not _ownership_established(row) or not opened or not floor or opened < floor:
                unestablished += 1
            after = _proposal(row)
            if after is None:
                continue
            items.append(_preview_item(row, after))
        return _preview_document(items, unestablished)


def _confirm(cur, request_id, gate):
    if gate not in ("app", "direction"):
        raise ValueError("Only verified app or prompt-bound owner authorization can apply a correction")
    # Forget locks episodes before scrubbing legacy request records.
    cur.execute("LOCK TABLE episodes, fact_claims, schema_migrations IN SHARE MODE")
    cur.execute("SELECT state, preview, result FROM commitment_origin_requests WHERE id = %s FOR UPDATE",
                (request_id,))
    found = cur.fetchone()
    if found is None:
        return {"ok": False, "error": "No such job-origin request; nothing was changed"}
    state, doc, result = found
    if state != "pending":
        if state == "applied" and isinstance(result, dict):
            # Retain the durable inverse scope in this retry's action too.
            from khipu import owner_direction
            for batch in result.get("batches", []):
                owner_direction.record_owed_batch(batch)
            return {**result, "changed": 0, "already_applied": True}
        return {"ok": False, "state": state, "changed": 0,
                "error": "The request has no recoverable applied result; nothing was changed"}
    planned = {it["id"]: it for it in _request_references(doc)}
    # Freeze lineage, claim and migration witnesses while rechecking and
    # writing. These locks precede the commitment locks, as forget does.
    rows = {r["id"]: r for r in _rows(cur, ids=list(planned), lock=True)}
    live, skipped = [], []
    for cid, item in planned.items():
        row = rows.get(cid)
        after = _proposal(row) if row is not None else None
        if (after is None or _fingerprint(after) != item["proposal_fingerprint"]
                or _fingerprint(row) != item["fingerprint"]):
            skipped.append(cid)
        else:
            live.append(cid)

    def change(cid):
        after = _proposal(rows[cid])
        cur.execute("UPDATE commitments SET origin = 'job' WHERE id = %s", (cid,))
        return co.reassign(cur, cid, after["owner"], after["owner_label"],
                           by="job-origin correction", reason="Opening capture and all witnesses prove job lineage")

    changed, batches = owed_tidy.bulk_batch(
        cur, live, "reassign", change, reason="Owner-confirmed historical job-origin correction",
        gate=gate, origin_correction=True)
    result = {"ok": True, "request": request_id, "state": "applied",
              "changed": sum(changed.values()), "batches": batches, "skipped": skipped}
    cur.execute("UPDATE commitment_origin_requests SET state = 'applied', result = %s::jsonb WHERE id = %s",
                (json.dumps(result), request_id))
    return result


def command(args):
    from khipu import cli, offline, owner_direction
    from khipu.db import connect

    want = owner_direction.intent("owed.correct_job_origin.confirm", args.confirm or "")
    if args.confirm:
        # A machine can manufacture a TTY and feed it yes. Only the existing
        # verified app or prompt-bound direction machinery authorizes this.
        if not args.from_app and not args.by_direction:
            print(json.dumps({"ok": False, "code": "owner_confirmation_required",
                              "error": "The request stays pending until the owner confirms in the verified app or directs confirmation from a recorded owner prompt"}))
            return 2
        gate = cli._owner_only(args, want)
        if isinstance(gate, int):
            return gate
    try:
        writing = bool(args.confirm or args.request)
        if writing:
            statement_timeout = offline.setting("hub_statement_timeout")
        else:
            from khipu.config import read_config_unsettled

            statement_timeout = offline.settings(config=read_config_unsettled())["hub_statement_timeout"]
        with connect(statement_timeout_s=statement_timeout,
                     record_reachability=writing) as conn:
            with conn.cursor() as cur:
                if args.confirm:
                    out = _confirm(cur, args.confirm, gate)
                elif args.requests:
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute("SELECT id, preview FROM commitment_origin_requests WHERE state = 'pending' ORDER BY requested_at")
                    stored = cur.fetchall()
                    from khipu.config import read_only_config

                    with read_only_config():
                        out = {"ok": True, "requests": [
                            {"request": rid, **_pending_preview(cur, doc)} for rid, doc in stored]}
                else:
                    if not args.request:
                        cur.execute("SET TRANSACTION READ ONLY")
                    out = preview(cur, args.project)
                    if args.request:
                        rid = "origin-" + secrets.token_hex(12)
                        cur.execute("INSERT INTO commitment_origin_requests (id, preview) VALUES (%s, %s::jsonb)",
                                    (rid, json.dumps(_request_document(out))))
                        out = {"ok": True, "request": rid, "state": "pending", **out}
    except Exception:
        if args.requests:
            out = {"ok": False, "code": "visibility_unavailable", "requests": [],
                   "error": "Pending previews could not be checked against current provenance"}
        else:
            if args.confirm:
                owner_direction._abandon()
            raise
    except BaseException:
        if args.confirm:
            owner_direction._abandon()
        raise
    if args.confirm:
        out = owner_direction.finish(out) or out
    print(json.dumps(out, default=str))
    return 0 if out.get("ok", True) else 1


def add_parser(sub):
    from khipu import owner_direction

    parser = sub.add_parser("correct-job-origin", help="Preview historical job-origin corrections; request owner confirmation")
    parser.add_argument("--project")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--request", action="store_true", help="Keep this preview pending for the owner")
    mode.add_argument("--confirm", metavar="REQUEST", help="Owner: apply a pending preview in one transaction")
    mode.add_argument("--requests", action="store_true", help="Read pending previews")
    parser.add_argument("--from-app", action="store_true", help=argparse.SUPPRESS)
    owner_direction.add_owner_flags(parser)
    parser.set_defaults(func=command)
