"""Hub-only source support for deduplicated facts.

Capture and forget take the same transaction lock before fact mutations.
Their own episode row may already be locked by the capture/forget caller.
Snapshots describe capture values, never an instruction to restore lifecycle or
owner state. A missing migration keeps the previous conservative cascade.
"""
from __future__ import annotations

import json
from typing import Any

_TABLES = {
    "commitment": ("commitments", "opened_episode"),
    "decision": ("decisions", "episode_id"),
    "deliverable": ("deliverables", "episode_id"),
}
_LOCK = 542496546050


class FactSupportBusy(RuntimeError):
    """Retryable refusal: a kept source is being captured or forgotten."""



def ready(cur) -> bool:
    from khipu.db import has_columns

    try:
        return has_columns(cur, "fact_support", "kind", "fact_id", "episode_id", "value", "legacy")
    except Exception:  # Minimal cursors and hubs without introspection retain the old cascade.
        return False


def lock(cur) -> bool:
    if not ready(cur):
        return False
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK,))
    return True


def record(cur, kind: str, fact_id: int, episode_id: int, value: dict[str, Any]) -> None:
    if not ready(cur):
        return
    # The shared lock is acquired by each writer before it touches fact rows.
    # Deleted sources can never add support, even when an old caller retries.
    cur.execute(
        "INSERT INTO fact_support(kind, fact_id, episode_id, value) "
        "SELECT %s, %s, id, %s::jsonb FROM episodes "
        "WHERE id = %s AND deleted_at IS NULL "
        "ON CONFLICT (kind, fact_id, episode_id) DO UPDATE SET "
        "value = EXCLUDED.value || jsonb_build_object('variants', "
        "(SELECT jsonb_agg(DISTINCT v) FROM jsonb_array_elements("
        "COALESCE(fact_support.value->'variants', '[]'::jsonb) || "
        "jsonb_build_array(fact_support.value - 'variants') || jsonb_build_array(EXCLUDED.value)) v))",
        (kind, fact_id, json.dumps(value, default=str), episode_id),
    )



def commitment_value(cur, fact_id: int, text: str, content_hash: str,
                     project: str | None, *, inserted: bool = False) -> dict[str, Any]:
    """Own words plus the requirements this writer demonstrably added."""
    from khipu.close_rules import parse_prs, pr_requirement_labels

    prs, refs = parse_prs(text), pr_requirement_labels(text, project)
    before_prs, before_refs = [], []
    if not inserted:
        cur.execute("SELECT prs, pr_check FROM commitments WHERE id = %s FOR UPDATE", (fact_id,))
        row = cur.fetchone()
        if row:
            before_prs = row[0] or []
            before_refs = (row[1] or {}).get("refs") or []
    return {"text": text, "content_hash": content_hash, "prs": prs, "pr_refs": refs,
            "added_prs": [n for n in prs if n not in before_prs],
            # Inserts populate prs, while qualified refs are added by dedup.
            "added_pr_refs": [] if inserted else [r for r in refs if r not in before_refs]}


def _requirements_after_forget(row: dict, changes: dict, removed: list[dict], kept: list[tuple]) -> None:
    """Subtract proven capture additions; leave owner/unknown metadata intact."""
    from khipu.close_rules import parse_prs, pr_requirement_labels

    known_prs = {n for v in removed for n in v.get("added_prs", [])}
    known_refs = {r for v in removed for r in v.get("added_pr_refs", [])}
    live = [v for _, value in kept for v in [value, *(value.get("variants") or [])]]
    kept_prs = {n for v in live for n in v.get("prs", [])}
    kept_refs = {r for v in live for r in v.get("pr_refs", [])}
    for field in ("text", "latest_text"):
        text = changes.get(field, row.get(field)) or ""
        kept_prs.update(parse_prs(text))
        kept_refs.update(pr_requirement_labels(text, row.get("project")))
    if row.get("prs") is not None:
        changes["prs"] = [n for n in row["prs"] if n not in known_prs or n in kept_prs]
    check = row.get("pr_check")
    if isinstance(check, dict) and isinstance(check.get("refs"), list):
        refs = [r for r in check["refs"] if r not in known_refs or r in kept_refs]
        if refs != check["refs"]:
            # Externally checked states, waiting reasons and owner metadata
            # remain as recorded; only capture-owned requirement links change.
            changes["pr_check"] = {**check, "refs": refs}


def _legacy_values(kind: str, row: dict, payload: dict, episode_id: int):
    """Recover exact raw witnesses only; never infer a lost paraphrase link."""
    if kind == "commitment":
        from khipu import commitments as c

        if row.get("project") is None:
            # Projectless dedup is limited to session lineage.
            lineage = payload.get("parent_session_id") or payload.get("session_id")
            if not lineage or lineage != row.get("_origin_lineage"):
                return
        items = payload.get("open_loops")
        for item in items if isinstance(items, list) else []:
            norm = c._normalize_open_loop(item)
            if not norm or norm["text"] != row["text"] or norm["kind"] != row["kind"]:
                continue
            owner, label = c.resolve_owner_and_label(
                norm["text"], kind=norm["kind"], declared=norm["owner"],
                future_trigger=c.has_future_trigger(norm["text"]),
                origin=c.capture_origin(payload), declared_label=norm.get("owner_label"),
            )
            if (owner, label) != (row["owner"], row.get("owner_label")):
                continue
            if c.rejection_reason(norm["text"], owner=owner, kind=norm["kind"]):
                continue
            from khipu.close_rules import parse_prs, pr_requirement_labels

            yield {"text": norm["text"], "content_hash": row["content_hash"],
                   "prs": parse_prs(norm["text"]),
                   "pr_refs": pr_requirement_labels(norm["text"], row.get("project"))}
    elif kind == "decision":
        from khipu import decisions as d

        items = payload.get("decisions")
        for item in items if isinstance(items, list) else []:
            text = d._redacted_value(str(item or "").strip())
            if d.normalize_text(text) == d.normalize_text(row["text"]):
                detail = d._decision_details_by_text(payload).get(text) or {}
                yield {
                    "text": text, "session_id": payload.get("session_id"),
                    "decided_at": payload.get("ts"),
                    "rationale": detail.get("rationale") or None,
                    "source_kind": detail.get("by") or None,
                    "evidence": ({"derivation": "extract-details-v1", "episode_id": episode_id,
                                  "reverses": detail.get("reverses") or None} if detail else None),
                }
    else:
        items = payload.get("deliverables")
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            values = {k: str(item.get(k) or "").strip() or None for k in ("kind", "path", "url", "title")}
            values["kind"] = (values["kind"] or "").lower()
            keys = ("kind", "path", "url") if values["path"] or values["url"] else ("kind", "path", "url", "title")
            if all(values[k] == row.get(k) for k in keys):
                yield values


def _recover_legacy(cur, kind: str, row: dict, episode_id: int) -> None:
    cur.execute(
        "SELECT e.id, e.raw, e.session_id, e.parent_session_id, e.ts "
        "FROM episodes e WHERE e.deleted_at IS NULL "
        "AND e.project IS NOT DISTINCT FROM %s AND e.id <> %s "
        "AND (%s <> 'decision' OR (e.ts >= %s::timestamptz "
        "AND e.ts <= %s::timestamptz + interval '30 days')) ORDER BY e.id",
        (row.get("project"), episode_id, kind, row.get("decided_at"), row.get("decided_at")),
    )
    candidates = cur.fetchall()
    if kind == "commitment" and row.get("project") is None:
        cur.execute("SELECT session_id, parent_session_id FROM episodes WHERE id = %s", (episode_id,))
        origin = cur.fetchone()
        row["_origin_lineage"] = (origin[1] or origin[0]) if origin else None
    for eid, raw, session, parent, ts in candidates:
        if not isinstance(raw, dict):
            continue
        # Column identities and timestamps take precedence over stale raw.
        payload = {**raw, "project": row.get("project"), "session_id": session,
                   "parent_session_id": parent, "ts": ts.isoformat()}
        for value in _legacy_values(kind, row, payload, int(eid)):
            record(cur, kind, int(row["id"]), int(eid), value)



def prepare_forget(cur, episode_id: int) -> None:
    """Discover support and lock replacement parents before publishing a fence.

    A capture can own its episode FOR UPDATE while awaiting our mutex. Never
    wait for that row while holding the mutex: refuse immediately and let the
    caller roll back/retry. UPDATE locks protect replacement FKs and the
    deliverable parent change stamp until commit. No hub tombstone or local registry write has happened at this point.
    """
    if not ready(cur):
        return
    from psycopg.errors import LockNotAvailable

    cur.execute("SAVEPOINT fact_support_prepare")
    try:
        for kind, (table, parent) in _TABLES.items():
            cur.execute(
                f"SELECT to_jsonb(f) FROM {table} f WHERE f.{parent} = %s "
                "OR EXISTS (SELECT 1 FROM fact_support s WHERE s.kind = %s "
                "AND s.fact_id = f.id AND s.episode_id = %s) ORDER BY f.id",
                (episode_id, kind, episode_id),
            )
            for (row,) in cur.fetchall():
                fid = int(row["id"])
                cur.execute(
                    "SELECT legacy FROM fact_support WHERE kind = %s AND fact_id = %s AND episode_id = %s",
                    (kind, fid, episode_id),
                )
                source = cur.fetchone()
                if row[parent] == episode_id and (source is None or source[0]):
                    _recover_legacy(cur, kind, row, episode_id)
                cur.execute(
                    "SELECT e.id FROM episodes e WHERE e.deleted_at IS NULL AND e.id <> %s "
                    "AND EXISTS (SELECT 1 FROM fact_support s WHERE s.kind = %s AND s.fact_id = %s "
                    "AND s.episode_id = e.id) ORDER BY e.id FOR UPDATE NOWAIT",
                    (episode_id, kind, fid),
                )
                cur.fetchall()
    except LockNotAvailable as exc:
        cur.execute("ROLLBACK TO SAVEPOINT fact_support_prepare")
        raise FactSupportBusy(
            "kept source episode is busy; roll back and retry forget after its capture or forget completes"
        ) from exc
    finally:
        cur.execute("RELEASE SAVEPOINT fact_support_prepare")


def remove(cur, episode_id: int) -> None:
    """Remove this source, retaining kept facts and owner/lifecycle changes.

    Reparent before the ordinary cascade and reader fences run. Fields whose
    current value differs from the forgotten capture's snapshot are owner or
    later changes and are preserved. Final-source removal uses the old cascade.
    """
    if not ready(cur):
        return
    for kind, (table, parent) in _TABLES.items():
        cur.execute(
            f"SELECT to_jsonb(f) FROM {table} f WHERE f.{parent} = %s "
            "OR EXISTS (SELECT 1 FROM fact_support s WHERE s.kind = %s "
            "AND s.fact_id = f.id AND s.episode_id = %s) ORDER BY f.id FOR UPDATE",
            (episode_id, kind, episode_id),
        )
        for (row,) in cur.fetchall():
            fid = int(row["id"])
            cur.execute(
                "SELECT value, legacy FROM fact_support WHERE kind = %s AND fact_id = %s AND episode_id = %s",
                (kind, fid, episode_id),
            )
            removed = cur.fetchone()
            cur.execute(
                "SELECT s.episode_id, s.value FROM fact_support s "
                "JOIN episodes e ON e.id = s.episode_id "
                "WHERE s.kind = %s AND s.fact_id = %s AND s.episode_id <> %s "
                "AND e.deleted_at IS NULL ORDER BY e.ts, e.id",
                (kind, fid, episode_id),
            )
            kept = cur.fetchall()
            if not kept:
                continue
            first_id, first = kept[0]
            _, latest = kept[-1]
            before = removed[0] if removed else row
            variants = [before, *(before.get("variants") or [])]
            changes = {}
            if row[parent] == episode_id:
                changes[parent] = int(first_id)
            fields = {
                "commitment": ("text",),
                "decision": ("text", "session_id", "decided_at", "rationale", "source_kind", "evidence"),
                "deliverable": ("kind", "path", "url", "title"),
            }[kind]
            for field in fields:
                provenance = kind == "decision" and field in ("session_id", "decided_at") and row[parent] == episode_id
                if field in first and (provenance or any(row.get(field) == v.get(field) for v in variants)):
                    changes[field] = first[field]
            if kind == "commitment":
                if "text" in changes and any(row.get("content_hash") == v.get("content_hash") for v in variants):
                    changes["content_hash"] = first.get("content_hash", row["content_hash"])
                # A forgotten restatement may own latest_text even when the
                # original source remains. Do not retain that alternate wording.
                if any(row.get("latest_text") in (v.get("text"), v.get("latest_text")) for v in variants):
                    text = latest.get("text")
                    changes["latest_text"] = text if text != changes.get("text", row["text"]) else None
                if changes.get("text") == row.get("latest_text"):
                    changes["latest_text"] = None
                _requirements_after_forget(row, changes, variants, kept)
                if removed is not None:
                    changes["seen_count"] = max(1, int(row.get("seen_count") or 1) - 1)
                if (changes.get("text", row["text"]) != row["text"]
                        or changes.get("latest_text", row.get("latest_text")) != row.get("latest_text")):
                    cur.execute("DELETE FROM memory_embeddings WHERE kind = 'commitment' AND ref = %s", (str(fid),))
            if changes:
                sets, params = [], []
                for field, value in changes.items():
                    sets.append(f"{field} = %s" + ("::jsonb" if field in ("evidence", "pr_check") else ""))
                    params.append(json.dumps(value) if field in ("evidence", "pr_check") else value)
                cur.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE id = %s", (*params, fid))
                if kind == "deliverable" and any(value != row.get(field) for field, value in changes.items()):
                    # Deliverables ride their parent's episode pull rather than
                    # an independent watermark. Publish the index change while
                    # preserving the episode's authored ts and raw payload.
                    cur.execute(
                        "UPDATE episodes SET changed_at = clock_timestamp() "
                        "WHERE id = %s AND deleted_at IS NULL",
                        (changes.get(parent, row[parent]),),
                    )
    cur.execute("DELETE FROM fact_support WHERE episode_id = %s", (episode_id,))
