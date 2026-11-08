"""Explicit bulk commitment changes and archival Undo. No inferred clean-up."""
from __future__ import annotations
import hashlib
import json
import secrets
from datetime import datetime, timezone
from typing import Any

PLAN_VERSION = 1
ALL_PROJECTS = "<all-owner-items>"
FINGERPRINT_FIELDS = ("status", "owner", "owner_label", "origin", "owner_reassigned_at", "closed_at",
                      "closed_episode", "close_reason", "closed_by", "close_rule_key", "close_evidence",
                      "tidy_batch", "text", "kind", "due_after",
                      # A restatement's newer wording and the PRs it added: a
                      # plan made before one must not close the item after it.
                      "prs", "latest_text", "pr_check")
# Who set the owner by hand, why, and the owner before (migration 0037):
# kept with each change and restored by an undo, never part of the fingerprint.
LINEAGE_FIELDS = ("owner_reassigned_by", "owner_reassign_reason", "owner_prior", "owner_prior_label")
PRIOR_FIELDS = (*FINGERPRINT_FIELDS[:12], *LINEAGE_FIELDS)
_TIMESTAMPS = ("owner_reassigned_at", "closed_at")
NOT_READY = ("This hub has no bulk change tables yet (migrations 0033 to 0036). "
             "undoing need the migrations. Nothing was changed.")


BULK_ACTIONS = ("close", "reopen", "snooze", "reassign")
BULK_PRIOR_FIELDS = (*PRIOR_FIELDS, "due_after")

def _select(cur, project: str, *, ids: list[int] | None = None, lock: bool = False) -> dict[int, dict]:
    """``{id: row}`` for the project's open items (or for ``ids``, any
    status), with the opening capture's parent session as ``lineage``.
    Columns a pre-migration hub lacks read as None."""
    from khipu.commitments import _not_forgotten_clause
    from khipu.db import has_columns, table_columns

    have = table_columns(cur, "commitments")
    wanted = ("id", "project", "opened_episode", "opened_at", "future_trigger", *FINGERPRINT_FIELDS,
              *LINEAGE_FIELDS, "origin_reviewed_at")
    cols = [c for c in wanted if c in have]
    lineage = "NULL"
    if has_columns(cur, "episodes", "parent_session_id"):
        lineage = ("(SELECT NULLIF(e.parent_session_id, '') FROM episodes e "
                   "WHERE e.id = commitments.opened_episode)")
    where = "project IS NOT DISTINCT FROM %s AND " + ("id = ANY(%s)" if ids is not None else "status = 'open'")
    params: tuple = (project, list(ids)) if ids is not None else (project,)
    if project == ALL_PROJECTS:
        where = "id = ANY(%s)" if ids is not None else "status = 'open'"
        params = (list(ids),) if ids is not None else ()
    cur.execute(f"SELECT {', '.join(cols)}, {lineage} FROM commitments WHERE {where} "
                f"AND {_not_forgotten_clause(cur)} ORDER BY id{' FOR UPDATE' if lock else ''}", params)
    out = {}
    for raw in cur.fetchall():
        row = {c: None for c in wanted}
        row.update(zip(cols, raw[:-1]))
        row["lineage"] = raw[-1]
        out[int(row["id"])] = row
    from khipu.commitments import _fenced_owed_rows

    # The hub's deleted_at lags this Mac's forget list; an unreadable list
    # plans and changes nothing tied to an episode.
    return {int(row["id"]): row for row in _fenced_owed_rows(list(out.values()))}


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return value


def fingerprint(row: dict[str, Any]) -> str:
    """A hash of the item's state (``FINGERPRINT_FIELDS``)."""
    state = {f: _plain(row.get(f)) for f in FINGERPRINT_FIELDS}
    # Of pr_check only the PRs it keeps count; its nightly status words do not.
    check = row.get("pr_check")
    state["pr_check"] = check.get("refs") if isinstance(check, dict) else None
    return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()


def _ready(cur) -> bool:
    from khipu.db import has_columns, has_table

    return (has_columns(cur, "commitments", "origin", "owner_label", "owner_reassigned_at", "close_rule_key",
                        "reopened_rule_keys", "tidy_batch")
            and has_table(cur, "commitment_tidy_items") and has_table(cur, "commitment_tidy_batches"))


def _guarded_owner(row: dict) -> str:
    """The owner the guard reads for a row ``_select`` gave (``commitments.guarded_owner``),
    never the owner the plan would give it."""
    from khipu import commitments as co

    return co.guarded_owner(row)


def _is_users(row: dict) -> bool:
    from khipu import commitments as co

    return _guarded_owner(row) == co.OWNER_USER


def _gate(cur, rows: dict[int, dict], ids: list[int], extra_users: set[int] | None = None,
          passed: str | None = None):
    """``(passed, gate, mine)``: the owner gate when any of ``ids`` is the
    user's item now (or in ``extra_users``). ``passed`` is a gate the caller
    already passed (an Undo from the owner-actions log)."""
    from khipu import commitments as co

    mine = [cid for cid in ids if _is_users(rows[cid]) or cid in (extra_users or set())]
    if not mine:
        return True, None, mine
    if passed in co.OWNER_GATE_PASSES:
        return True, passed, mine
    gate = co.bulk_owner_gate(len(ids))
    return gate is not None, gate, mine


def _batch_id(prefix: str) -> str:
    return f"{prefix}-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)


def bulk_batch(cur, ids: list[int], action: str, change, *, reason: str,
               gate: str | None, origin_correction: bool = False, all_projects: bool = False) -> tuple[dict[int, bool], list[str]]:
    # forget-fence: owner bulk write; returns changed flags and batch ids (rows via the fenced _select)
    """Apply ``change(cid) -> bool`` (a close, reopen, snooze or reassign) to
    ``ids`` as undoable batches, one per project, in the caller's
    transaction. Every bulk action keeps the same record: the batch, and per
    changed item in ``commitment_tidy_items`` its action, the reason and its
    state before (``BULK_PRIOR_FIELDS``, the due date included), so ``undo``
    (and owner-action Undo) restores each item exactly. Returns
    ``({id: changed}, [batch ids])``."""
    if action not in BULK_ACTIONS:
        raise ValueError(f"action must be one of {', '.join(BULK_ACTIONS)}")
    cur.execute("SELECT id, project FROM commitments WHERE id = ANY(%s) ORDER BY id", (list(ids),))
    by_project: dict[str | None, list[int]] = {}
    for cid, project in cur.fetchall():
        by_project.setdefault(project, []).append(int(cid))
    if all_projects:
        by_project = {ALL_PROJECTS: list(ids)}
    changed: dict[int, bool] = {}
    batches: list[str] = []
    for project, pids in by_project.items():
        before = _select(cur, project, ids=pids, lock=True)
        if not before:
            continue
        batch = _batch_id(action)
        from khipu import owner_direction

        owner_direction.record_owed_batch(batch)
        cur.execute("INSERT INTO commitment_tidy_batches (id, project, plan_version, planned_at, owner_gate) "
                    "VALUES (%s, %s, %s, NULL, %s)", (batch, project or "", PLAN_VERSION, gate))
        count = 0
        for cid in pids:
            row = before.get(cid)
            if row is None or not change(cid):
                changed[cid] = False
                continue
            cur.execute("UPDATE commitments SET tidy_batch = %s WHERE id = %s", (batch, cid))
            after = _select(cur, project, ids=[cid])[cid]
            prior = {f: _plain(row.get(f)) for f in BULK_PRIOR_FIELDS}
            if origin_correction:
                prior["origin_correction_stamp"] = _plain(after.get("origin_reviewed_at"))
            cur.execute(
                "INSERT INTO commitment_tidy_items (batch_id, commitment_id, action, reason, prior, "
                "after_fingerprint) VALUES (%s, %s, %s, %s, %s::jsonb, %s)",
                (batch, cid, action, reason[:400],
                 json.dumps(prior, default=str), fingerprint(after)))
            changed[cid] = True
            count += 1
        cur.execute("UPDATE commitment_tidy_batches SET counts = %s::jsonb WHERE id = %s",
                    (json.dumps({action: count}), batch))
        batches.append(batch)
    return changed, batches


def batch_project(batch: str) -> str | None:
    """The project a batch belongs to ("" for items with none), or None when
    there is no such batch. Read only."""
    from khipu import offline
    from khipu.db import connect

    with connect(statement_timeout_s=offline.setting("hub_statement_timeout")) as conn:
        with conn.cursor() as cur:
            if not _ready(cur):
                return None
            cur.execute("SELECT project FROM commitment_tidy_batches WHERE id = %s", (str(batch or "").strip(),))
            found = cur.fetchone()
    return None if found is None else str(found[0])


def _undo_selection(ids: list[int] | None) -> list[int] | None:
    if ids is None:
        return None
    if not isinstance(ids, list) or not ids or any(type(cid) is not int or cid <= 0 for cid in ids):
        raise ValueError("ids must be a nonempty list of whole numbers above 0")
    return sorted(set(ids))


def undo(batch: str, *, project: str | None = None, owner_gate: str | None = None,
         ids: list[int] | None = None,
         write_id: str | None = None) -> dict[str, Any]:
    """Undo one applied batch (see the module docstring). With ``project``,
    a batch of another project is refused before anything changes.
    ``owner_gate`` is how the owner gate already passed for this Undo (the
    owner-actions log's Undo of a bulk change made by direction). ``ids``
    restricts the inverse to selected, still-applied members of this batch;
    omitted, it retains the whole-batch behavior."""
    from khipu import commitments as co, offline
    from khipu.db import connect

    try:
        selected = _undo_selection(ids)
    except ValueError as exc:
        return {"ok": False, "restored": 0, "error": str(exc) + "; nothing was changed"}
    batch = str(batch or "").strip()
    with connect(statement_timeout_s=offline.setting("hub_statement_timeout")) as conn:
        with conn.cursor() as cur:
            if not _ready(cur):
                return {"ok": False, "restored": 0, "error": NOT_READY}
            cur.execute("SELECT project FROM commitment_tidy_batches WHERE id = %s FOR UPDATE", (batch,))
            found = cur.fetchone()
            if found is None:
                return {"ok": False, "restored": 0, "error": f"no clean-up batch {batch!r}; nothing was changed"}
            if project is not None and found[0] != project:
                return {"ok": False, "restored": 0, "batch": batch, "project": found[0],
                        "error": f"batch {batch!r} is for project {found[0]!r}, not {project!r}; "
                                 "nothing was changed"}
            if selected is not None:
                cur.execute("SELECT commitment_id FROM commitment_tidy_items WHERE batch_id = %s "
                            "AND undone_at IS NULL AND commitment_id = ANY(%s)", (batch, selected))
                if {int(r[0]) for r in cur.fetchall()} != set(selected):
                    return {"ok": False, "restored": 0, "batch": batch,
                            "error": "ids must name still-applied items in this batch; nothing was changed"}
            project = found[0] or None
            cur.execute("SELECT commitment_id, action, prior, after_fingerprint FROM commitment_tidy_items "
                        "WHERE batch_id = %s AND undone_at IS NULL ORDER BY commitment_id", (batch,))
            entries = [(int(c), a, p if isinstance(p, dict) else json.loads(p), fp) for c, a, p, fp in cur.fetchall()]
            if selected is not None:
                entries = [entry for entry in entries if entry[0] in selected]
            if any("origin_correction_stamp" in prior for _, _, prior, _ in entries):
                if owner_gate not in ("app", "direction"):
                    from khipu import routing
                    if owner_gate is None and routing._caller()["app"]:
                        owner_gate = "app"
                    else:
                        return {"ok": False, "restored": 0, "batch": batch,
                                "needs_owner": [cid for cid, _, _, _ in entries],
                                "code": "owner_confirmation_required",
                                "error": "Correction Undo requires verified app or prompt-bound owner direction"}
            if write_id:
                from khipu import owner_writes

                if not owner_writes.record_on_hub(cur, "owner_undo", {"write_id": write_id}):
                    return {"ok": True, "batch": batch, "restored": 0, "already_applied": True}
            rows = _select(cur, project, ids=[e[0] for e in entries], lock=True)
            left_alone, todo = [], []
            for cid, action, prior, fp in entries:
                row = rows.get(cid)
                if row is None:
                    left_alone.append({"id": cid, "why": "no longer on the hub, or forgotten"})
                elif (fingerprint(row) != fp or
                      ("origin_correction_stamp" in prior and
                       _plain(row.get("origin_reviewed_at")) != prior["origin_correction_stamp"])):
                    left_alone.append({"id": cid, "why": "changed after the clean-up"})
                else:
                    todo.append((cid, action, prior))
            prior_users = {cid for cid, _, prior in todo if prior.get("owner") == co.OWNER_USER}
            passed, gate, mine = _gate(cur, rows, [t[0] for t in todo], prior_users, owner_gate)
            if not passed:
                conn.rollback()
                return {"ok": False, "restored": 0, "batch": batch, "needs_owner": mine,
                        "error": co.bulk_owner_refusal()}

            restored = 0
            restored_ids = []
            lineage_ready = co._lineage_columns_ready(cur)
            redo_from = _redo_states(cur, batch)
            for cid, action, prior in todo:
                undone_from = {f: _plain(rows[cid].get(f)) for f in BULK_PRIOR_FIELDS}
                if not _put(cur, cid, prior, lineage_ready, reopen_rule=action == "close"):
                    left_alone.append({"id": cid, "why": "could not be restored; an open item with the same "
                                                         "text may exist"})
                    continue
                cur.execute("UPDATE commitment_tidy_items SET undone_at = now() WHERE batch_id = %s "
                            "AND commitment_id = %s", (batch, cid))
                redo_from[str(cid)] = {"state": undone_from,
                                       "fingerprint": fingerprint(_select(cur, project, ids=[cid])[cid])}
                restored += 1
                restored_ids.append(cid)
            report = {"restored": restored, "left_alone": left_alone, "owner_gate": gate}
            cur.execute("UPDATE commitment_tidy_batches SET undone_at = CASE WHEN EXISTS "
                        "(SELECT 1 FROM commitment_tidy_items WHERE batch_id = %s AND undone_at IS NULL) "
                        "THEN NULL ELSE now() END, undo_report = %s::jsonb "
                        "WHERE id = %s", (batch, json.dumps(dict(report, redo=redo_from), default=str), batch))
    _log(f"{batch}: undo restored {restored}, left alone {len(left_alone)}")
    return {"ok": True, "batch": batch, "project": project or "", **report}


def _put(cur, cid: int, state: dict, lineage_ready: bool, *, reopen_rule: bool) -> bool:
    """Write ``state`` (``BULK_PRIOR_FIELDS``) back onto item ``cid``; False,
    with nothing written, when the hub refuses it (the same text open again).
    ``reopen_rule`` remembers an undone rule close in ``reopened_rule_keys``."""
    cur.execute("SAVEPOINT owed_change_put")
    try:
        cur.execute(
            "UPDATE commitments SET status = %s, owner = %s, owner_label = %s, origin = %s, "
            "owner_reassigned_at = %s::timestamptz, closed_at = %s::timestamptz, closed_episode = %s, "
            "close_reason = %s, closed_by = %s, close_rule_key = %s, close_evidence = %s::jsonb, "
            "tidy_batch = %s, reopened_rule_keys = CASE WHEN %s AND close_rule_key IS NOT NULL "
            "AND NOT (close_rule_key = ANY(reopened_rule_keys)) "
            "THEN array_append(reopened_rule_keys, close_rule_key) ELSE reopened_rule_keys END, "
            # A clean-up batch kept no due date and never moved one.
            "due_after = CASE WHEN %s THEN %s::timestamptz ELSE due_after END"
            + (", owner_reassigned_by = %s, owner_reassign_reason = %s, owner_prior = %s, "
               "owner_prior_label = %s" if lineage_ready else "")
            + " WHERE id = %s",
            (state["status"], state["owner"], state["owner_label"], state["origin"],
             state["owner_reassigned_at"], state["closed_at"], state["closed_episode"],
             state["close_reason"], state["closed_by"], state["close_rule_key"],
             None if state["close_evidence"] is None else json.dumps(state["close_evidence"]),
             state["tidy_batch"], reopen_rule, "due_after" in state, state.get("due_after"),
             *([state.get(f) for f in LINEAGE_FIELDS] if lineage_ready else []),
             cid))
    except Exception:  # noqa: BLE001 — e.g. the same text is open again
        cur.execute("ROLLBACK TO SAVEPOINT owed_change_put")
        return False
    cur.execute("RELEASE SAVEPOINT owed_change_put")
    return True


def _redo_states(cur, batch: str) -> dict:
    """``{id: {state, fingerprint}}``: each undone item's state before its
    undo and its fingerprint just after, kept on the batch for ``redo``."""
    cur.execute("SELECT undo_report FROM commitment_tidy_batches WHERE id = %s", (batch,))
    found = cur.fetchone()
    report = found[0] if found else None
    if isinstance(report, str):
        report = json.loads(report)
    redo_from = (report or {}).get("redo") if isinstance(report, dict) else None
    return dict(redo_from) if isinstance(redo_from, dict) else {}


def redo(batch: str, *, project: str | None = None, owner_gate: str | None = None,
         ids: list[int] | None = None,
         write_id: str | None = None) -> dict[str, Any]:
    """Put an undone batch's changes back: each item ``undo`` restored and
    that has not changed since gets the state it had before that undo; an
    item changed since is left alone and listed. The owner-actions log's
    Undo of an undo made by direction; passes the owner gate like ``undo``."""
    from khipu import commitments as co, offline
    from khipu.db import connect

    try:
        selected = _undo_selection(ids)
    except ValueError as exc:
        return {"ok": False, "reapplied": 0, "error": str(exc) + "; nothing was changed"}
    batch = str(batch or "").strip()
    with connect(statement_timeout_s=offline.setting("hub_statement_timeout")) as conn:
        with conn.cursor() as cur:
            if not _ready(cur):
                return {"ok": False, "reapplied": 0, "error": NOT_READY}
            cur.execute("SELECT project FROM commitment_tidy_batches WHERE id = %s FOR UPDATE", (batch,))
            found = cur.fetchone()
            if found is None:
                return {"ok": False, "reapplied": 0, "error": f"no clean-up batch {batch!r}; nothing was changed"}
            if project is not None and found[0] != project:
                return {"ok": False, "reapplied": 0, "batch": batch, "project": found[0],
                        "error": f"batch {batch!r} is for project {found[0]!r}, not {project!r}; "
                                 "nothing was changed"}
            if write_id:
                from khipu import owner_writes

                if not owner_writes.record_on_hub(cur, "owner_undo", {"write_id": write_id}):
                    return {"ok": True, "batch": batch, "reapplied": 0, "already_applied": True}
            project = found[0] or None
            redo_from = _redo_states(cur, batch)
            # A generic Undo-of-Undo must not provide a second application
            # path around the historical correction's explicit owner gate.
            cur.execute("SELECT EXISTS (SELECT 1 FROM commitment_tidy_items "
                        "WHERE batch_id = %s AND prior ? 'origin_correction_stamp')", (batch,))
            if cur.fetchone()[0]:
                conn.rollback()
                return {"ok": False, "reapplied": 0, "code": "owner_confirmation_required",
                        "error": "Historical job-origin corrections cannot be replayed through a clean-up Undo"}
            cur.execute("SELECT commitment_id FROM commitment_tidy_items WHERE batch_id = %s "
                        "AND undone_at IS NOT NULL ORDER BY commitment_id", (batch,))
            ids = [int(r[0]) for r in cur.fetchall() if str(r[0]) in redo_from]
            if selected is not None:
                if not set(selected).issubset(ids):
                    conn.rollback()
                    return {"ok": False, "reapplied": 0, "batch": batch,
                            "error": "ids must name undone items in this batch; nothing was changed"}
                ids = selected
            rows = _select(cur, project, ids=ids, lock=True)
            left_alone, todo = [], []
            for cid in ids:
                row = rows.get(cid)
                if row is None:
                    left_alone.append({"id": cid, "why": "no longer on the hub, or forgotten"})
                elif fingerprint(row) != redo_from[str(cid)].get("fingerprint"):
                    left_alone.append({"id": cid, "why": "changed after the undo"})
                else:
                    todo.append(cid)
            then_users = {cid for cid in todo if redo_from[str(cid)]["state"].get("owner") == co.OWNER_USER}
            passed, gate, mine = _gate(cur, rows, todo, then_users, owner_gate)
            if not passed:
                conn.rollback()
                return {"ok": False, "reapplied": 0, "batch": batch, "needs_owner": mine,
                        "error": co.bulk_owner_refusal()}
            reapplied = 0
            lineage_ready = co._lineage_columns_ready(cur)
            for cid in todo:
                if not _put(cur, cid, redo_from[str(cid)]["state"], lineage_ready, reopen_rule=False):
                    left_alone.append({"id": cid, "why": "could not be put back; an open item with the same "
                                                         "text may exist"})
                    continue
                done = _select(cur, project, ids=[cid])[cid]
                cur.execute("UPDATE commitment_tidy_items SET undone_at = NULL, after_fingerprint = %s "
                            "WHERE batch_id = %s AND commitment_id = %s", (fingerprint(done), batch, cid))
                redo_from.pop(str(cid))
                reapplied += 1
            cur.execute("UPDATE commitment_tidy_batches SET undone_at = NULL, "
                        "undo_report = jsonb_set(COALESCE(undo_report, '{}'::jsonb), '{redo}', %s::jsonb) "
                        "WHERE id = %s", (json.dumps(redo_from, default=str), batch))
    _log(f"{batch}: redo put back {reapplied}, left alone {len(left_alone)}")
    return {"ok": True, "batch": batch, "project": project or "", "reapplied": reapplied,
            "left_alone": left_alone, "owner_gate": gate}



def _log(message: str) -> None:
    import sys
    print(f"[khipu-owed-changes] {message}", file=sys.stderr)
