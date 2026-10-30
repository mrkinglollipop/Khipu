"""``khipu capture`` — the one capture entrypoint every harness hook calls (P3 step 2).

Reads a capture payload (same JSON shape ``capture_v2.py`` accepts) from stdin or
``--payload-file`` and routes it by ``capture_mode``:

  legacy  → shell out to capture_v2 only. Khipu writes nothing here; capture_v2's own
            fail-open mirror still runs if KHIPU_MIRROR permits (unchanged behavior).
  dual    → write PG FIRST and DURABLY (a PG failure is a real exit code, not a
            warning — that is what makes Khipu a second *writer* rather than a mirror),
            then shell out to capture_v2 for the file wiki with KHIPU_MIRROR=0 so it
            does not mirror the same episode a second time.
  hub     → write PG only. The file wiki is maintained by the reverse-mirror
            (``khipu regen-memory`` today; full materialize is P3 end-state).

Identity is minted ONCE here (``ts``, seconds precision, ``Z``) and passed to both
writers, so the PG row and the file line share the (ts, md5(summary)) key the
reconcile upserts on — the P2b dual-mint bug can't come back through this path.

Exit codes mirror capture_v2: 0 ok · 64 no payload · 65 bad payload · 70 PG write
failed (dual/hub) · capture_v2's own code if the file leg fails.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from khipu import wire
from khipu.config import capture_mode

# ---- W1.4 ingest dedup ------------------------------------------------------
#
# Two shapes of duplicate measured 2026-09-03: (a) the exact same transcript
# window re-queued (a crash between offset-advance and job removal, a retried
# drain) and (b) two independent captures of the same real conversation in
# the same project within a few minutes (a dispatched child session, the
# Stop/compaction overlap the audit found). (a) is an identity match — skip
# outright. (b) is a similarity match — merge into the earlier row rather
# than insert a second episode for one conversation.

DEDUP_WINDOW_MINUTES = 5
DEDUP_CANDIDATE_LIMIT = 20
DEDUP_JACCARD_THRESHOLD = 0.6  # fallback only; cosine uses config dedup_similarity


def _md5_hex(text: str) -> str:
    return hashlib.md5((text or "").encode("utf-8")).hexdigest()


def _dedup_text(payload_like: dict[str, Any]) -> str:
    parts = [payload_like.get("summary") or ""]
    decisions = payload_like.get("decisions") or []
    if isinstance(decisions, list):
        parts.append(" ".join(str(d) for d in decisions))
    return " ".join(parts)


def _jaccard(a: str, b: str) -> float:
    ta = set(re.findall(r"[a-z0-9]+", a.lower()))
    tb = set(re.findall(r"[a-z0-9]+", b.lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _source_receipt(payload: dict[str, Any]) -> str:
    from khipu.capture_identity import payload_sha256

    return payload_sha256(payload)


def _receipts(payload: dict[str, Any], source: dict[str, Any] | None) -> list[str]:
    """Merge receipts this capture may already be recorded under: the source
    snapshot's (taken before enrichment, written since the capture identity
    fix) and the enriched payload's (what older merges stored)."""
    out = [source["payload_sha256"]] if source else []
    legacy = _source_receipt(payload)
    return out + [legacy] if legacy not in out else out


def _source_key_is_safe(payload: dict[str, Any]) -> bool:
    """The source key may answer only when it is no broader than the column
    match that follows it. Without a natural key it is the payload hash (an
    exact replay). With one, it needs a harness, and a window_id needs its part:
    the 0027 backfill found historical episodes with a window_id but no part
    that share one key, where the column match still inserts each of them."""
    from khipu.capture_identity import has_natural_key

    if not has_natural_key(payload):
        return True
    if not payload.get("harness"):
        return False
    return not payload.get("window_id") or bool(payload.get("part"))


# How a merge matched its target, recorded on its receipt. Only an identity
# match may later transfer ownership; a receipt without the field was written
# before receipts recorded it and counts as similarity.
MATCH_IDENTITY = "identity"
MATCH_SIMILARITY = "similarity"


class ClaimDeferred(RuntimeError):
    """A required origin claim failed after its savepoint rolled back. The
    write must be retried (the outbox keeps the job and never buries it), or
    the queued owner, the only ownership evidence, would be discarded."""


class FactStepDeferred(RuntimeError):
    """A fact step (commitments, decisions, deliverables, and the close and
    reversal steps that lock the same rows) met a transient lock or timeout
    error, or the hub refused an unprojected wording write (SQLSTATE 08K01,
    fact provenance migration 0047). The whole capture transaction rolls
    back and the outbox retries the job; it is never buried, because a
    partial commit would lose the item's claims (fact-provenance scope,
    invariant I4)."""


# The errors a fact step defers on rather than failing open (scope 3.4): a
# deadlock, a serialization failure, a lock timeout and a statement timeout.
# By class name and SQLSTATE, so a test double needs no psycopg import.
_FACT_DEFERRAL_CLASSES = frozenset({
    "DeadlockDetected", "SerializationFailure", "LockNotAvailable", "QueryCanceled",
})
_FACT_DEFERRAL_SQLSTATES = frozenset({"40P01", "40001", "55P03", "57014"})


def _fact_deferral(exc: BaseException) -> str | None:
    """The deferral reason for a fact-step error, or None when it fails open."""
    from khipu import provenance

    if isinstance(exc, FactStepDeferred):
        return str(exc)
    if provenance.is_old_writer_refusal(exc):
        return f"refused unprojected write ({type(exc).__name__})"
    names = {cls.__name__ for cls in type(exc).__mro__}
    state = getattr(getattr(exc, "diag", None), "sqlstate", None) or getattr(exc, "sqlstate", None)
    if names & _FACT_DEFERRAL_CLASSES or state in _FACT_DEFERRAL_SQLSTATES:
        return type(exc).__name__
    return None


def _defer_if_transient(exc: BaseException, step: str) -> None:
    """Raise :class:`FactStepDeferred` for an error the outbox must retry."""
    reason = _fact_deferral(exc)
    if reason is not None:
        _log(f"{step} step deferred ({reason}); the capture will be retried")
        raise FactStepDeferred(f"{step}: {reason}: {exc}") from exc


def _record_fact_gap(cur, episode_id: int | None, step: str, exc: BaseException, *,
                     source_id: str | None = None) -> None:
    """After a fact step failed open (its savepoint rolled back), record the
    gap in the same transaction, so doctor reports it and the nightly
    re-runs it (invariant I4). Only on a hub with migration 0047; a gap that
    cannot be recorded defers the whole capture instead of losing the items."""
    if episode_id is None:
        return
    from khipu import provenance

    try:
        ready = provenance.ready(cur)
    except Exception:  # noqa: BLE001 — a failed probe is not a hub with the table
        ready = False
    if not ready:
        return
    try:
        cur.execute(
            "INSERT INTO fact_gaps (episode_id, step, error, source_id) VALUES (%s, %s, %s, %s)",
            (episode_id, step, f"{type(exc).__name__}: {str(exc)[:500]}", source_id),
        )
        # A recorded gap is this writer's participation (0047's writer gate):
        # the step's own declaration rolled back with its savepoint.
        provenance.declare_writer(cur)
    except Exception as gap_exc:  # noqa: BLE001 — classified: the capture is retried
        raise FactStepDeferred(f"{step}: fact gap not recorded ({type(gap_exc).__name__})") from gap_exc


class CaptureEvidenceConflict(RuntimeError):
    """A legacy uniqueness collision would discard distinct incoming evidence.

    Preserve it in the outbox for explicit resolution, without changing the
    original episode, its owner or the caller's timestamp/summary choices.
    """


def _receipt_match(cur, payload: dict[str, Any],
                   source: dict[str, Any] | None, *,
                   live_only: bool = False) -> tuple[int, str] | None:
    # forget-fence: capture writer: dedup and merge lookups; returns ids or the write's outcome
    """The episode this capture was already merged into, and how that merge
    matched: identity only when its receipt says so."""
    live = " AND deleted_at IS NULL" if live_only else ""
    receipts = _receipts(payload, source)
    for match in (MATCH_IDENTITY, None):
        for receipt_id in receipts:
            entry = {"source_id": receipt_id}
            if match:
                entry["match"] = match
            cur.execute(f"SELECT id FROM episodes WHERE raw @> %s::jsonb{live} LIMIT 1",
                        (json.dumps({"merged_from": [entry]}),))
            row = cur.fetchone()
            if row:
                return int(row[0]), match or MATCH_SIMILARITY
    return None


def _dedup_exact_window(cur, payload: dict[str, Any], *,
                        source: dict[str, Any] | None = None,
                        live_only: bool = False) -> int | None:
    # forget-fence: capture writer: dedup and merge lookups; returns ids or the write's outcome
    """K3: every sibling PART of one split window shares the same (harness,
    session_id, transcript_range) — that alone would match the FIRST part
    inserted and silently skip every part after it as an "exact window"
    duplicate. window_id (+ part, carried in raw) makes each sibling its own
    identity. Pre-migration hub (no window_id column): falls back to the
    pre-K3 match, same as a payload with no window_id. Merge receipts are
    matched first, by ``dedup_before_insert``.

    A hub with episode_sources (0027) answers by the capture's source key
    first; the column match below stays as the pre-0027 path and for sources
    recorded before the key existed. ``live_only`` matches live episodes only."""
    from khipu.capture_identity import episode_for_source, source_key, sources_ready
    from khipu.db import has_table

    live = " AND deleted_at IS NULL" if live_only else ""
    if sources_ready(cur) and _source_key_is_safe(payload):
        key = source["source_key"] if source else source_key(payload)
        if live_only:
            cur.execute("SELECT s.episode_id FROM episode_sources s JOIN episodes e "
                        "ON e.id = s.episode_id AND e.deleted_at IS NULL WHERE s.source_key = %s",
                        (key,))
            row = cur.fetchone()
            if row is None and has_table(cur, "episode_identities"):
                # The source row stays on a collateral tombstone, so a live
                # re-upload of the source was recorded under its episode key;
                # the identity index still records the source's key on it
                # (Astra 7A re-review #2).
                cur.execute("SELECT i.episode_id FROM episode_identities i JOIN episodes e "
                            "ON e.id = i.episode_id AND e.deleted_at IS NULL "
                            "WHERE i.identity = %s ORDER BY i.episode_id LIMIT 1", (key,))
                row = cur.fetchone()
            recorded = int(row[0]) if row else None
        else:
            recorded = episode_for_source(cur, key)
        if recorded is not None:
            return recorded
    harness = payload.get("harness")
    session_id = payload.get("session_id")
    tr = payload.get("transcript_range")
    if not harness or not session_id or not tr:
        return None
    window_id = payload.get("window_id")
    if window_id:
        try:
            from khipu.db import has_columns

            has_window_col = has_columns(cur, "episodes", "window_id")
        except Exception:  # noqa: BLE001 — schema probe never blocks the write
            has_window_col = False
        if has_window_col:
            cur.execute(
                "SELECT id FROM episodes WHERE harness = %s AND session_id = %s "
                f"AND transcript_range = %s AND window_id = %s AND raw->>'part' = %s{live} LIMIT 1",
                (harness, session_id, tr, window_id, payload.get("part")),
            )
            row = cur.fetchone()
            return int(row[0]) if row else None
    cur.execute(
        "SELECT id FROM episodes WHERE harness = %s AND session_id = %s "
        f"AND transcript_range = %s{live} LIMIT 1",
        (harness, session_id, tr),
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


# A checkout observation that fails to reach the hub waits here, one small
# JSON list per checkout key, oldest first, and is landed in order before the
# next observation for that key (record_checkout_observation, _observe_checkout).
# Consecutive observations of the same state keep only the oldest (its time is
# the one a transition needs); past the cap the newest are dropped, since the
# next live observation re-reports the current state anyway.
CHECKOUT_SPOOL_CAP = 20


def _spool_path(key: str) -> Path:
    from khipu.session_capture import state_dir

    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
    return state_dir() / "checkout_spool" / f"{safe}.json"


def _spool_key(obs: Any) -> str | None:
    key = obs.get("checkout_key") if isinstance(obs, dict) else None
    return str(key) if key else None


def _spool_read(key: str) -> list[dict[str, Any]]:
    try:
        items = json.loads(_spool_path(key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [o for o in items if isinstance(o, dict)] if isinstance(items, list) else []


def _spool_edit(key: str, change) -> None:
    """Read-modify-write one key's spool under an exclusive file lock, written
    atomically (temp file, then rename). Best-effort: never raises."""
    import fcntl
    import tempfile

    path = _spool_path(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_suffix(".lock"), "a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            items = change(_spool_read(key))
            if not items:
                path.unlink(missing_ok=True)
                return
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".spool.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(items, fh)
            os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001
        _log(f"checkout spool for {key} not written ({type(exc).__name__}: {exc})")


def _spool_append(obs: dict[str, Any]) -> None:
    key = _spool_key(obs)
    if not key:
        return

    def add(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Concurrent drains spool in completion order, not observation order:
        # sort by observed_at so the flush never lands a newer state first and
        # then drops an older one as stale. Coalesce only after sorting.
        same = ("root_commit", "project", "project_kind")
        merged = sorted([*items, obs], key=lambda o: float(o.get("observed_at") or 0))
        out: list[dict[str, Any]] = []
        for item in merged:
            if out and all(out[-1].get(k) == item.get(k) for k in same):
                continue
            out.append(item)
        return out[:CHECKOUT_SPOOL_CAP]

    _spool_edit(key, add)


def _observation_failed(res: Any) -> bool:
    return not (isinstance(res, dict) and res.get("ok"))


def _land_observations(observations: list[dict[str, Any]]) -> bool:
    """Record observations in order in one transaction of their own; all or none."""
    try:
        from khipu.db import connect
        from khipu.identity import record_checkout_observation as decide

        with connect() as conn, conn.cursor() as cur:
            for obs in observations:
                if _observation_failed(decide(cur, obs)):
                    conn.rollback()
                    return False
            conn.commit()
        return True
    except Exception:  # noqa: BLE001
        return False


def flush_checkout_spool(key: str | None) -> bool:
    """Land this checkout's spooled observations, oldest first. True when none
    are left waiting (an empty spool needs no hub)."""
    if not key:
        return True
    pending = _spool_read(key)
    if not pending:
        return True
    if not _land_observations(pending):
        return False
    _spool_edit(key, lambda items: items[len(pending):] if items[:len(pending)] == pending else items)
    return not _spool_read(key)


def _observe_checkout(cur, payload: dict[str, Any]) -> None:
    """The hook's checkout observation (``payload["checkout"]``) goes to the hub's
    rename decision (identity.record_checkout_observation). Runs before the alias
    lookup so this very capture lands under the new name. It is popped so the
    episode never stores it; ``capture`` keeps its own copy for the outbox, so a
    rolled-back write is retried with it. Older spooled observations for the
    same checkout land first; when they cannot, or this one fails, it joins the
    spool instead, so observations always land in order. Never raises."""
    from khipu.identity import observation_fields

    obs = payload.pop("checkout", None)
    if not isinstance(obs, dict):
        return
    payload["root_commit"] = obs.get("root_commit")
    if observation_fields(obs) is None:
        return
    if not flush_checkout_spool(_spool_key(obs)):
        _spool_append(obs)
        return
    try:
        from khipu.identity import record_checkout_observation

        res = record_checkout_observation(cur, obs)
    except Exception as exc:  # noqa: BLE001
        res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if _observation_failed(res):
        _spool_append(obs)


def record_checkout_observation(obs: dict[str, Any]) -> None:
    """Record one observation on its own, for a job whose extraction came back
    empty and so never reaches ``write_pg``: after any spooled ones for its
    checkout, in one transaction. When the hub cannot take it (unreachable, no
    table, any failure) it is spooled locally and lands with a later one."""
    from khipu.identity import observation_fields

    key = _spool_key(obs)
    if not key or observation_fields(obs) is None:
        return
    pending = _spool_read(key)
    if _land_observations([*pending, obs]):
        if pending:
            _spool_edit(key, lambda items: items[len(pending):] if items[:len(pending)] == pending else items)
        return
    _spool_append(obs)


def _note_project_root(cur, payload: dict[str, Any]) -> None:
    """Remember the root commit this (resolved) project name was seen with.
    Best-effort: a hub before migration 0039 has no table, and never fails a capture."""
    root = payload.get("root_commit")
    if not (root and payload.get("project")):
        return
    try:
        cur.execute("SAVEPOINT project_roots_note", ())
        try:
            cur.execute(
                "INSERT INTO project_roots (project, root_commit) VALUES (%s, %s) "
                "ON CONFLICT (project, root_commit) DO UPDATE SET last_seen = now()",
                (payload["project"], str(root)))
            cur.execute("RELEASE SAVEPOINT project_roots_note", ())
        except Exception:  # noqa: BLE001
            cur.execute("ROLLBACK TO SAVEPOINT project_roots_note", ())
            cur.execute("RELEASE SAVEPOINT project_roots_note", ())
    except Exception:  # noqa: BLE001
        pass


def _inherit_project(cur, payload: dict[str, Any]) -> None:
    # forget-fence: capture writer: dedup and merge lookups; returns ids or the write's outcome
    """W1.2b: identity.resolve_repo_root cannot resolve a scratchpad/`/tmp`
    cwd — repo_root and project both come back None, which is exactly the
    dispatched-child-session shape the origin audit found (episode 11286).
    When lineage IS known (parent_session_id), inherit project/repo_root from
    the most recent episode in the last 24h that shares it — either as that
    row's OWN parent_session_id (a sibling dispatched from the same host) or
    as that row's session_id (the parent episode itself). Mutates payload in
    place; never raises — a miss here just leaves project/repo_root None and
    the caller falls through to the (harness, session_id, transcript_range)
    exact-window dedup only, same as today."""
    if payload.get("project") or payload.get("repo_root"):
        return
    parent = payload.get("parent_session_id")
    if not parent:
        return
    # SubagentStop stores a bare parent id; parent episodes are harness:id.
    harness = str(payload.get("harness") or "").strip()
    if not harness:
        sid = str(payload.get("session_id") or "")
        if ":" in sid:
            harness = sid.split(":", 1)[0]
    prefixed = f"{harness}:{parent}" if harness and ":" not in str(parent) else parent
    try:
        cur.execute(
            """
            SELECT project, repo_root
            FROM episodes
            WHERE (parent_session_id = %s OR session_id IN (%s, %s))
              AND deleted_at IS NULL
              AND project IS NOT NULL
              AND ts >= now() - interval '24 hours'
            ORDER BY ts DESC
            LIMIT 1
            """,
            (parent, parent, prefixed),
        )
        row = cur.fetchone()
    except Exception:  # noqa: BLE001 — inheritance is best-effort, never blocks a write
        return
    if not row:
        return
    project, repo_root = row
    if project:
        payload["project"] = project
        _log(f"inherited project={project!r} from parent_session_id={parent!r}")
    if repo_root:
        payload["repo_root"] = repo_root


def _dedup_candidates(cur, payload: dict[str, Any]) -> list[dict[str, Any]]:
    # forget-fence: capture writer: dedup and merge lookups; returns ids or the write's outcome
    """Candidate rows for the similarity leg, grouped the same way a caller
    would filter for "this same conversation": by project when it is known,
    else by parent_session_id (a dispatched child with no resolvable project
    still shares lineage with its siblings). Neither known → no candidates;
    only the exact-window skip in dedup_before_insert still applies.

    K3: siblings of one split window are DIFFERENT parts of a conversation,
    not near-duplicates of it — a row sharing this payload's window_id is
    excluded, so the merge path can never fold two parts of one window into
    each other."""
    ts = payload.get("ts")
    project = payload.get("project")
    parent = payload.get("parent_session_id")
    if not ts or (not project and not parent):
        return []
    group_col, group_val = ("project", project) if project else ("parent_session_id", parent)
    window_id = payload.get("window_id")
    exclude_sql, params = "", [group_val, ts, ts]
    # Similar text in another T3 thread is a separate conversation. A merge
    # across threads would discard the incoming capture's provenance.
    thread = payload.get("t3_thread_id")
    if thread:
        exclude_sql += " AND raw->>'t3_thread_id' = %s"
        params.append(thread)
    else:
        exclude_sql += " AND raw->>'t3_thread_id' IS NULL"
    if window_id:
        try:
            from khipu.db import has_columns

            has_window_col = has_columns(cur, "episodes", "window_id")
        except Exception:  # noqa: BLE001 — schema probe never blocks the write
            has_window_col = False
        if has_window_col:
            exclude_sql += " AND window_id IS DISTINCT FROM %s"
            params.append(window_id)
    params.append(DEDUP_CANDIDATE_LIMIT)
    try:
        cur.execute(
            f"""
            SELECT id, summary, decisions, preferences, topics
            FROM episodes
            WHERE {group_col} = %s
              AND deleted_at IS NULL
              AND ts BETWEEN %s::timestamptz - interval '5 minutes'
                         AND %s::timestamptz + interval '5 minutes'
              {exclude_sql}
            ORDER BY ts DESC
            LIMIT %s
            """,
            params,
        )
    except Exception:  # noqa: BLE001 — dedup is best-effort, never blocks a write
        return []
    cols = ("id", "summary", "decisions", "preferences", "topics")
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _dedup_cosine_best(
    cur, payload: dict[str, Any], candidates: list[dict[str, Any]], threshold: float
) -> tuple[int, float, str] | None:
    """Best cosine match at/above threshold, or None (no key, no embeddings
    yet for these candidates, or any failure) — caller falls back to Jaccard."""
    if not candidates:
        return None
    try:
        from khipu import provider_text
        from khipu.embed import _active_profile, _vec_literal, embed_one

        profile = _active_profile(cur)
        vec = embed_one(provider_text.live_input(_dedup_text(payload), "payload"), profile=profile)
    except Exception:  # noqa: BLE001
        return None
    ids = [str(c["id"]) for c in candidates]
    try:
        cur.execute(
            """
            SELECT ref, 1 - (embedding <=> %s::vector) AS score
            FROM memory_embeddings
            WHERE profile = %s AND kind = 'episode' AND chunk_idx = 0 AND ref = ANY(%s)
            ORDER BY score DESC
            LIMIT 1
            """,
            (_vec_literal(vec), profile, ids),
        )
        row = cur.fetchone()
    except Exception:  # noqa: BLE001
        return None
    if not row or row[1] is None:
        return None
    ref, score = row
    score = float(score)
    if score < threshold:
        return None
    return int(ref), score, "cosine"


def _dedup_jaccard_best(
    payload: dict[str, Any], candidates: list[dict[str, Any]], threshold: float
) -> tuple[int, float, str] | None:
    text = _dedup_text(payload)
    best: tuple[int, float, str] | None = None
    for c in candidates:
        score = _jaccard(text, _dedup_text(c))
        if score >= threshold and (best is None or score > best[1]):
            best = (int(c["id"]), score, "jaccard")
    return best


def _union_json_list(existing: Any, new_items: Any) -> list[Any]:
    out = list(existing or [])

    def _key(v: Any) -> Any:
        return json.dumps(v, sort_keys=True, default=str) if isinstance(v, (dict, list)) else v

    seen = {_key(x) for x in out}
    for item in new_items or []:
        k = _key(item)
        if k not in seen:
            out.append(item)
            seen.add(k)
    return out


def _append_summary(existing: str, incoming: str) -> str:
    """K5: a merge used to drop the incoming capture's own summary outright —
    now it becomes a new paragraph on the target, deduped by exact text (the
    same near-duplicate conversation restated across captures must not grow
    the episode every time it is)."""
    existing = (existing or "").strip()
    incoming = (incoming or "").strip()
    if not incoming:
        return existing
    paragraphs = [p for p in existing.split("\n\n") if p.strip()] if existing else []
    if incoming in paragraphs:
        return existing
    paragraphs.append(incoming)
    return "\n\n".join(paragraphs)


def _merge_into_episode(
    cur, target_id: int, payload: dict[str, Any], *, matched_via: str, score: float,
    source: dict[str, Any] | None = None, match: str = MATCH_SIMILARITY,
) -> bool:
    # forget-fence: capture writer: dedup and merge lookups; returns ids or the write's outcome
    """Fold a near-duplicate capture into the episode it matched.

    Audit 2026-09-04: the merge used to union topics/decisions/preferences
    only, so ``people`` and ``tags`` named by the second capture were dropped
    on the floor; it opened no commitments for the merged payload (an
    ``open_loops`` entry that arrived on the merged side was lost outright,
    unlike the insert path at ``write_pg``); and it left the target row's
    vectors pointing at the pre-merge text, so the new decisions were
    unsearchable semantically until the nightly backfill. And (K5) the
    incoming capture's own SUMMARY — not just its lists — was dropped on the
    floor too. All are fixed here, each fail-open — the row update is the
    durable part and must never be taken down by an additive step.
    """
    from khipu.db import has_columns

    try:
        has_tags = has_columns(cur, "episodes", "tags")
    except Exception:  # noqa: BLE001 — schema probe never blocks the merge
        has_tags = False
    cols = "topics, decisions, preferences, people, raw, ts, summary"
    if has_tags:
        cols += ", tags"
    has_verbatim = has_columns(cur, "episodes", "verbatim")
    if has_verbatim:
        cols += ", verbatim"
    live = " AND deleted_at IS NULL" if has_columns(cur, "episodes", "deleted_at") else ""
    cur.execute(f"SELECT {cols} FROM episodes WHERE id = %s{live} FOR UPDATE", (target_id,))
    row = cur.fetchone()
    if not row:
        return False
    topics, decisions, preferences, people, raw, target_ts, target_summary = row[:7]
    tags = row[7] if has_tags else None
    from khipu.capture_identity import record_source, source_identity
    from khipu.tombstones import hub_source_forgotten

    # The receipt and the merged source row both come from the snapshot
    # write_pg took before enrichment, so they depend on the incoming capture
    # alone; a caller without one (tests, older paths) snapshots here.
    source = source or source_identity(payload)
    # Checked under the target's row lock, which forget takes first.
    if hub_source_forgotten(cur, payload, source):
        return False
    source_id = source["payload_sha256"]
    receipts = set(_receipts(payload, source))
    for prior in (raw or {}).get("merged_from") or []:
        if prior.get("source_id") in receipts or prior.get("payload") == payload:
            return True
    new_topics = _union_json_list(topics, payload.get("topics"))
    new_decisions = _union_json_list(decisions, payload.get("decisions"))
    new_preferences = _union_json_list(preferences, payload.get("preferences"))
    new_people = _union_json_list(people, payload.get("people"))
    new_tags = _union_json_list(tags, payload.get("tags")) if has_tags else None
    new_summary = _append_summary(target_summary, payload.get("summary"))
    raw = dict(raw or {})
    raw.setdefault("ts", target_ts.isoformat() if hasattr(target_ts, "isoformat") else target_ts)
    raw.setdefault("summary", target_summary)
    verbatim = dict((row[8 if has_tags else 7] if has_verbatim else raw.get("verbatim")) or {})
    for key, value in (payload.get("verbatim") or {}).items():
        if isinstance(value, list):
            verbatim[key] = _union_json_list(verbatim.get(key), value)
        elif key == "note" and isinstance(value, str):
            verbatim[key] = _append_summary(verbatim.get(key), value)
        elif key not in verbatim:
            verbatim[key] = value
    raw["verbatim"] = verbatim
    merged_from = list(raw.get("merged_from") or [])
    merged_from.append({
        "source_id": source_id,
        "session_id": payload.get("session_id"),
        "ts": payload.get("ts"),
        "matched_via": matched_via,
        "match": match,
        "score": round(score, 4),
        "payload": payload,
    })
    raw["merged_from"] = merged_from
    sets = ["topics = %s::jsonb", "decisions = %s::jsonb", "preferences = %s::jsonb",
            "people = %s::jsonb", "raw = %s::jsonb", "summary = %s"]
    params: list[Any] = [
        json.dumps(new_topics, ensure_ascii=False),
        json.dumps(new_decisions, ensure_ascii=False),
        json.dumps(new_preferences, ensure_ascii=False),
        json.dumps(new_people, ensure_ascii=False),
        json.dumps(raw, ensure_ascii=False),
        new_summary,
    ]
    if has_tags:
        sets.append("tags = %s::jsonb")
        params.append(json.dumps(new_tags, ensure_ascii=False))
    if has_verbatim:
        sets.append("verbatim = %s::jsonb")
        params.append(json.dumps(verbatim, ensure_ascii=False))
    params.append(target_id)
    from khipu.mirror import _savepoint
    from khipu.tombstones import is_forgotten_refusal

    # The receipt and the merged source row are one write: the hub refuses
    # either for a forgotten source (0030), and the refusal undoes both, so
    # the caller falls through to an insert the hub refuses the same way.
    savepoint = _savepoint(cur, "khipu_merge_write")
    try:
        from khipu.tombstones import announce_hub_identities

        announce_hub_identities(cur, payload, source)
        cur.execute(f"UPDATE episodes SET {', '.join(sets)} WHERE id = %s", params)
        record_source(cur, target_id, "merged", source,
                      ts=payload.get("ts"), summary=payload.get("summary"))
    except Exception as exc:
        if not is_forgotten_refusal(exc):
            raise
        if savepoint:
            cur.execute("ROLLBACK TO SAVEPOINT khipu_merge_write")
        return False
    if savepoint:
        cur.execute("RELEASE SAVEPOINT khipu_merge_write")

    # The merged capture's own open/closed loops belong to the target episode
    # — same call the insert path makes in write_pg, same SAVEPOINT fail-open.
    try:
        cur.execute("SAVEPOINT capture_merge_commitments")
    except Exception:  # noqa: BLE001 — no savepoint support (fake cur / autocommit)
        pass
    try:
        from khipu import commitments

        commitments.open_from_episode(cur, payload, target_id)
        commitments.auto_close(cur, payload, target_id)
        commitments.close_session_plan(cur, payload, target_id)
    except Exception as exc:  # noqa: BLE001 — the merged row stays; fail-open
        _defer_if_transient(exc, "commitments")
        try:
            cur.execute("ROLLBACK TO SAVEPOINT capture_merge_commitments")
        except Exception:  # noqa: BLE001
            pass
        else:
            _record_fact_gap(cur, target_id, "merge commitments", exc, source_id=source_id)
        _log(f"merge commitments step failed ({type(exc).__name__}: {exc})")

    # O2: the merged capture's own decision strings belong to the target
    # episode too, same SAVEPOINT fail-open posture as commitments above.
    try:
        cur.execute("SAVEPOINT capture_merge_decisions")
    except Exception:  # noqa: BLE001 — no savepoint support (fake cur / autocommit)
        pass
    try:
        from khipu import decisions as _decisions

        _decisions.insert_decisions_from_episode(cur, payload, target_id)
    except Exception as exc:  # noqa: BLE001 — the merged row stays; fail-open
        _defer_if_transient(exc, "decisions")
        try:
            cur.execute("ROLLBACK TO SAVEPOINT capture_merge_decisions")
        except Exception:  # noqa: BLE001
            pass
        else:
            _record_fact_gap(cur, target_id, "merge decisions", exc, source_id=source_id)
        _log(f"merge decisions step failed ({type(exc).__name__}: {exc})")

    # Phase 2, session C: conservative reversal detection reads the row
    # insert_decisions_from_episode just wrote — its own SAVEPOINT so a
    # detection failure can never take the merged decision row down with it.
    try:
        cur.execute("SAVEPOINT capture_merge_decision_detection")
    except Exception:  # noqa: BLE001 — no savepoint support (fake cur / autocommit)
        pass
    try:
        from khipu import decisions as _decisions

        _decisions.detect_reversals_from_episode(cur, payload, target_id)
    except Exception as exc:  # noqa: BLE001 — the merged row stays; fail-open
        _defer_if_transient(exc, "decision detection")
        try:
            cur.execute("ROLLBACK TO SAVEPOINT capture_merge_decision_detection")
        except Exception:  # noqa: BLE001
            pass
        else:
            _record_fact_gap(cur, target_id, "merge decision detection", exc, source_id=source_id)
        _log(f"merge decision detection step failed ({type(exc).__name__}: {exc})")

    # Owed close rules (slice 2): this capture's project, with this capture
    # as the later evidence. After the decisions step, which they read.
    try:
        cur.execute("SAVEPOINT capture_merge_close_rules")
    except Exception:  # noqa: BLE001 — no savepoint support (fake cur / autocommit)
        pass
    try:
        from khipu import close_rules

        close_rules.run(cur, payload=payload, episode_id=target_id)
    except Exception as exc:  # noqa: BLE001 — the merged row stays; fail-open
        _defer_if_transient(exc, "close rules")
        try:
            cur.execute("ROLLBACK TO SAVEPOINT capture_merge_close_rules")
        except Exception:  # noqa: BLE001
            pass
        else:
            _record_fact_gap(cur, target_id, "merge close rules", exc, source_id=source_id)
        _log(f"merge close rules step failed ({type(exc).__name__}: {exc})")

    # O4: same posture for the merged capture's own deliverables.
    try:
        cur.execute("SAVEPOINT capture_merge_deliverables")
    except Exception:  # noqa: BLE001 — no savepoint support (fake cur / autocommit)
        pass
    try:
        from khipu import deliverables as _deliverables

        _deliverables.insert_deliverables_from_episode(cur, payload, target_id)
    except Exception as exc:  # noqa: BLE001 — the merged row stays; fail-open
        _defer_if_transient(exc, "deliverables")
        try:
            cur.execute("ROLLBACK TO SAVEPOINT capture_merge_deliverables")
        except Exception:  # noqa: BLE001
            pass
        else:
            _record_fact_gap(cur, target_id, "merge deliverables", exc, source_id=source_id)
        _log(f"merge deliverables step failed ({type(exc).__name__}: {exc})")

    return True


def _reembed_merged_episode(ts: Any, summary: Any, merged: dict[str, Any], *,
                            episode_id: int | None = None) -> None:
    """Re-embed a merged target row. Fail-open: the episode is already durable
    and `khipu embed backfill` heals any miss."""
    try:
        from khipu.embed import embed_on_capture

        merged = dict(merged)
        merged["ts"] = ts.isoformat() if hasattr(ts, "isoformat") else ts
        merged["summary"] = summary or ""
        if not merged["ts"] or not merged["summary"]:
            return
        embed_on_capture(merged, episode_id=episode_id)
    except Exception as exc:  # noqa: BLE001 — vectors are additive
        _log(f"merge re-embed failed ({type(exc).__name__}: {exc})")


def dedup_before_insert(cur, payload: dict[str, Any], *,
                        source: dict[str, Any] | None = None,
                        live_only: bool = False) -> dict[str, Any]:
    """W1.4: {'action': 'skip'|'merge'|'none', ...}. Never raises — any
    failure in the similarity path degrades to 'none' (a normal insert).
    ``source`` is write_pg's pre-enrichment source identity snapshot.
    ``live_only`` ignores tombstoned episodes."""
    from khipu.config import float_setting

    receipt = _receipt_match(cur, payload, source, live_only=live_only)
    if receipt is not None:
        receipt_id, match = receipt
        _log(f"dedup: merge receipt on episode {receipt_id} ({match}); skipping insert")
        return {"action": "skip", "matched_episode": receipt_id,
                "matched_via": "receipt", "match": match}
    exact_id = _dedup_exact_window(cur, payload, source=source, live_only=live_only)
    if exact_id is not None:
        _log(f"dedup: exact window match episode {exact_id} — skipping insert")
        return {"action": "skip", "matched_episode": exact_id, "matched_via": "exact_window",
                "match": MATCH_IDENTITY}

    candidates = _dedup_candidates(cur, payload)
    if not candidates:
        return {"action": "none"}
    threshold = float_setting("dedup_similarity")
    best = _dedup_cosine_best(cur, payload, candidates, threshold)
    if best is None:
        best = _dedup_jaccard_best(payload, candidates, DEDUP_JACCARD_THRESHOLD)
    if best is None:
        return {"action": "none"}
    target_id, score, via = best
    if not _merge_into_episode(cur, target_id, payload, matched_via=via, score=score,
                               source=source):
        return {"action": "none"}
    _log(
        f"dedup: merged into episode {target_id} via {via} ({score:.2f}), "
        f"project={payload.get('project')!r}"
    )
    return {"action": "merge", "matched_episode": target_id, "matched_via": via, "score": score,
            "match": MATCH_SIMILARITY}



def _capture_v2() -> Path | None:
    from khipu.config import path_setting

    return path_setting("capture_v2")


EX_USAGE, EX_DATAERR, EX_SOFTWARE = 64, 65, 70


def _log(msg: str) -> None:
    print(f"[khipu-capture] {msg}", file=sys.stderr, flush=True)


def _mint_ts() -> str:
    # capture.py's default format: seconds precision, trailing Z.
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class CaptureIdentityConflict(ValueError):
    """A legacy replay key already belongs to different source evidence."""


def _validate_timestamp(payload: dict[str, Any]) -> None:
    ts = payload.get("ts")
    if ts is None or ts == "":
        payload["ts"] = _mint_ts()
        return
    if not isinstance(ts, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", ts
    ):
        raise ValueError("capture timestamp must be an ISO timestamp with a timezone")
    datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _redact_capture_values(value: Any) -> Any:
    """Apply the shared redactor to every text leaf before any persistence leg."""
    from khipu.redact import redact_secrets

    if isinstance(value, dict):
        for key in value:
            value[key] = _redact_capture_values(value[key])
    elif isinstance(value, list):
        value = [_redact_capture_values(item) for item in value]
    elif isinstance(value, str):
        value = redact_secrets(value)[0]
    return value


def _refresh_merged_episode(cur, episode_id: int) -> None:
    # forget-fence: capture writer: dedup and merge lookups; returns ids or the write's outcome
    """Embedding's separate connection must see the complete committed target."""
    try:
        cur.execute("SELECT to_jsonb(e) FROM episodes e WHERE id = %s", (episode_id,))
        row = cur.fetchone()
        if row:
            target = row[0]
            if target.get("deleted_at"):
                return
            merged = dict(target.get("raw") or {})
            merged.update(target)
            _reembed_merged_episode(target["ts"], target["summary"], merged,
                                    episode_id=episode_id)
    except Exception as exc:  # noqa: BLE001 — embedding is additive
        _log(f"merge refresh failed ({type(exc).__name__}: {exc})")


def load_payload(raw: str) -> dict[str, Any]:
    raw = (raw or "").strip()
    if not raw:
        raise SystemExit(EX_USAGE)
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        _log(f"invalid JSON: {exc}")
        raise SystemExit(EX_DATAERR)
    # Coerce before stripping: a payload whose summary is a list or a number
    # (a model extraction can produce either) used to raise AttributeError here
    # and exit with a traceback instead of the documented EX_DATAERR (audit
    # 2026-08-17).
    if not isinstance(payload, dict) or not isinstance(payload.get("summary"), str) \
            or not payload["summary"].strip():
        _log("payload missing a string 'summary'")
        raise SystemExit(EX_DATAERR)
    if not isinstance(payload.get("session_id"), str) or not payload["session_id"].strip():
        sid = os.environ.get("CLAUDE_SESSION_ID", "").strip()
        if sid:
            payload["session_id"] = sid
    try:
        _validate_timestamp(payload)
    except ValueError as exc:
        _log(str(exc))
        raise SystemExit(EX_DATAERR) from exc
    return payload


def _stamp_origin(cur) -> str | None:
    """The origin for a new episode row, or None to leave it unset: on a hub
    without the column (pre-0029), and when this machine has no readable
    identity, which is logged and never replaced by an invented id. A capture
    is never refused for its origin."""
    from khipu import db

    if not db.has_columns(cur, "episodes", "origin"):
        # The column list is cached per process; a long-lived server that
        # started before `khipu migrate` must see the column without a restart.
        db._TABLE_COLUMNS_CACHE.pop("episodes", None)
        if not db.has_columns(cur, "episodes", "origin"):
            return None
    from khipu.machine_identity import MachineIdentityError
    from khipu.request_context import request_origin

    try:
        return request_origin()
    except MachineIdentityError as exc:
        _log(f"origin left unset: {exc}")
        return None


def _claim_required() -> bool:
    """True when this write carries a trusted owner that must claim the row it
    lands on: an outbox replay whose envelope named an origin, or a capture on
    this machine. A live gateway caller, or the gateway process outside a
    request, never claims, so its claim can never block a write."""
    from khipu.request_context import gateway_label, gateway_process, in_replay, request_origin

    if in_replay():
        return request_origin() is not None
    return not gateway_label() and not gateway_process()


def _claim_unowned_episode(cur, episode_id: int | None, origin: str | None) -> None:
    """Give an existing unowned row the owner of the capture that landed on it
    instead of inserting (a replay or an exact-window skip; never a similarity
    merge, which is not an identity match): the row a
    file-mirror sync wrote without an origin. Only this machine, or the origin
    an outbox envelope recorded when the capture was queued, may claim: a live
    gateway caller must never claim an unowned row by replaying its
    timestamp and summary, or its session window, or by matching its text."""
    if episode_id is None or not origin or not _claim_required():
        return
    cur.execute("UPDATE episodes SET origin = %s WHERE id = %s AND origin IS NULL",
                (origin, episode_id))


def _refusals_settled(cur) -> bool | None:
    """None on a hub without 0047's identity index, where a tombstone is the
    forget. Otherwise whether its history is indexed, so the refusal table
    alone says what is forgotten (section 6.1)."""
    from khipu.db import has_table

    if not has_table(cur, "fact_copy_ready"):
        return None
    cur.execute("SELECT indexed_through >= target_id FROM fact_copy_ready")
    row = cur.fetchone()
    return bool(row and row[0])


def _forgotten_result(cur, payload: dict[str, Any], source: dict[str, Any], *,
                      dedup: dict[str, Any] | None = None) -> dict[str, Any]:
    # forget-fence: capture writer: dedup and merge lookups; returns ids or the write's outcome
    """The terminal outcome for a forgotten source: nothing written. The id
    is the tombstoned row holding its legacy identity, when there is one."""
    cur.execute("SELECT id FROM episodes WHERE ts = %s::timestamptz AND md5(summary) = %s",
                (payload["ts"], _md5_hex(payload.get("summary") or "")))
    existing = cur.fetchone()
    return {
        "episode_inserted": False,
        "topics_written": 0,
        "episode_id": int(existing[0]) if existing else None,
        "source_key": source["source_key"],
        "dedup": dedup or {"action": "none"},
        "forgotten_source": True,
    }


def run_fact_steps(cur, payload: dict[str, Any], episode_id: int) -> None:
    """The fact steps of a newly inserted capture, each in its own
    savepoint: commitments, decisions, reversal detection, close rules and
    deliverables. A transient or old-writer error raises
    :class:`FactStepDeferred` (the whole transaction must roll back and be
    retried); any other error rolls its step back and records a
    ``fact_gaps`` row (invariant I4). ``write_pg`` runs them for a capture,
    and the file recovery writers (``mirror.sync_recent_episodes``,
    ``mirror.reconcile_memory_root``) for a file line they import, so the
    episode commits with its claims (0047's writer gate)."""
    cur.execute("SAVEPOINT capture_commitments")
    try:
        from khipu import commitments

        commitments.open_from_episode(cur, payload, episode_id)
        commitments.auto_close(cur, payload, episode_id)
        commitments.close_session_plan(cur, payload, episode_id)
    except Exception as exc:  # noqa: BLE001 — episode row stays; fail-open
        _defer_if_transient(exc, "commitments")
        cur.execute("ROLLBACK TO SAVEPOINT capture_commitments")
        _record_fact_gap(cur, episode_id, "commitments", exc)
        _log(f"commitments step failed ({type(exc).__name__}: {exc})")

    # O2: each string in payload['decisions'] becomes a row.
    cur.execute("SAVEPOINT capture_decisions")
    try:
        from khipu import decisions as _decisions

        _decisions.insert_decisions_from_episode(cur, payload, episode_id)
    except Exception as exc:  # noqa: BLE001 — episode row stays; fail-open
        _defer_if_transient(exc, "decisions")
        cur.execute("ROLLBACK TO SAVEPOINT capture_decisions")
        _record_fact_gap(cur, episode_id, "decisions", exc)
        _log(f"decisions step failed ({type(exc).__name__}: {exc})")

    # Phase 2, session C: conservative reversal detection over
    # the rows the step above just inserted — its own
    # SAVEPOINT, same fail-open posture as every neighbour
    # here, so a detection failure can never take the
    # episode (or the decisions just written) down with it.
    cur.execute("SAVEPOINT capture_decision_detection")
    try:
        from khipu import decisions as _decisions

        _decisions.detect_reversals_from_episode(cur, payload, episode_id)
    except Exception as exc:  # noqa: BLE001 — episode row stays; fail-open
        _defer_if_transient(exc, "decision detection")
        cur.execute("ROLLBACK TO SAVEPOINT capture_decision_detection")
        _record_fact_gap(cur, episode_id, "decision detection", exc)
        _log(f"decision detection step failed ({type(exc).__name__}: {exc})")

    # Owed close rules (slice 2): PR and decision closes for
    # this capture's project, after the decisions they read.
    cur.execute("SAVEPOINT capture_close_rules")
    try:
        from khipu import close_rules

        close_rules.run(cur, payload=payload, episode_id=episode_id)
    except Exception as exc:  # noqa: BLE001 — episode row stays; fail-open
        _defer_if_transient(exc, "close rules")
        cur.execute("ROLLBACK TO SAVEPOINT capture_close_rules")
        _record_fact_gap(cur, episode_id, "close rules", exc)
        _log(f"close rules step failed ({type(exc).__name__}: {exc})")

    # O4: files/PR-issue-URLs/release tags from payload['deliverables'].
    cur.execute("SAVEPOINT capture_deliverables")
    try:
        from khipu import deliverables as _deliverables

        _deliverables.insert_deliverables_from_episode(cur, payload, episode_id)
    except Exception as exc:  # noqa: BLE001 — episode row stays; fail-open
        _defer_if_transient(exc, "deliverables")
        cur.execute("ROLLBACK TO SAVEPOINT capture_deliverables")
        _record_fact_gap(cur, episode_id, "deliverables", exc)
        _log(f"deliverables step failed ({type(exc).__name__}: {exc})")


def _legacy_comma_projection(original: Any, incoming: dict[str, Any]) -> bool:
    """Prove only the diagnosed legacy decision join/split and identity loss.

    The file format lost source identity. ``write_pg`` also injects empty
    tags and a null scope into its local copy before storing raw, while the
    legacy writer receives the pre-copy input. Only those exact absent
    defaults are ignored. Preferences, unknown keys, order and every supplied
    value must match. Server merge receipts are not alternate proof.
    """
    from khipu.capture_identity import SOURCE_FIELDS, payload_sha256

    if not isinstance(original, dict):
        return False
    decisions = original.get("decisions")
    if not isinstance(decisions, list) or not all(isinstance(d, str) for d in decisions):
        return False
    projected = dict(original)
    for key in SOURCE_FIELDS:
        if key not in incoming:
            projected.pop(key, None)
    if "tags" not in incoming and projected.get("tags") == []:
        projected.pop("tags")
    if "scope" not in incoming and projected.get("scope") is None:
        projected.pop("scope", None)
    projected["decisions"] = [s.strip() for s in ", ".join(decisions).split(",") if s.strip()]
    # Canonical JSON equality also distinguishes bool/int and int/float;
    # Python's container equality would accept those changed field values.
    return payload_sha256(projected) == payload_sha256(incoming)


def _legacy_comma_join(decisions: list[str]) -> list[str]:
    return [s.strip() for s in ", ".join(decisions).split(",") if s.strip()]


def _legacy_landed_line(original: Any, incoming: Any) -> bool:
    """Prove a memory-file line is a lossy projection of the hub row at its key.

    The file line drops fields the hub row keeps (edges, topic pages, loops,
    source identity), so the row may hold more. Every field the line carries
    with a value must be on the row with the same canonical JSON value;
    decisions compare after the legacy comma join on both sides. An empty line
    value (null, "", [], {}) carries nothing. A line value the row lacks or
    holds differently (a preference the row does not carry) is not landed:
    that line stays held for the owner and nothing is merged into the row.
    """
    if not isinstance(original, dict) or not isinstance(incoming, dict):
        return False

    def canonical(value: Any) -> str:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)

    for key, value in incoming.items():
        if value is None or (isinstance(value, (str, list, dict)) and not value):
            continue
        if key not in original:
            return False
        stored = original[key]
        if key == "decisions":
            if not (isinstance(value, list) and all(isinstance(d, str) for d in value)
                    and isinstance(stored, list) and all(isinstance(d, str) for d in stored)):
                return False
            value, stored = _legacy_comma_join(value), _legacy_comma_join(stored)
        if canonical(value) != canonical(stored):
            return False
    return True


def _legacy_preferences_contained(original: Any, incoming: Any) -> bool:
    """A memory-file line whose every preference is on the hub row (which may
    hold more, in another order) and whose other fields pass
    ``_legacy_landed_line``. Never a drain proof: only the owner's
    ``khipu outbox merge-line-preferences`` resolves such a line, as
    ``preferences_contained``."""
    if not isinstance(original, dict) or not isinstance(incoming, dict):
        return False
    line, stored = incoming.get("preferences"), original.get("preferences")
    if not (isinstance(line, list) and line and all(isinstance(p, str) for p in line)
            and isinstance(stored, list) and all(isinstance(p, str) for p in stored)):
        return False
    rest = {k: v for k, v in incoming.items() if k != "preferences"}
    return set(line) <= set(stored) and _legacy_landed_line(original, rest)


def _legacy_projection_kind(original: Any, incoming: dict[str, Any]) -> str | None:
    """The resolution kind a queued file line proves against the hub row, if any."""
    if _legacy_comma_projection(original, incoming):
        return "legacy_comma_projection"
    if _legacy_landed_line(original, incoming):
        return "legacy_landed_line"
    return None


def _sibling_part_summary(cur, payload: dict[str, Any]) -> str | None:
    # forget-fence: capture writer: dedup and merge lookups; returns ids or the write's outcome
    """The summary a multi-part capture lands under when a sibling part holds
    its legacy key, else None.

    Parts of one window share ``ts``, and parts the model left without a
    summary share the fallback summary, so they collided on the legacy
    ``(ts, md5(summary))`` key and were held as evidence conflicts (2026-10-27,
    parts 4/7 and 5/7 against part 2/7). A live row at the key whose capture is
    the same window (harness, session, transcript range, window id) with a
    different ``part`` is a sibling, never this capture: the summary gains the
    part so it lands as its own row. Called only after the insert met the key.
    The source key was snapshotted before and does not change; nothing else in
    the payload does either. Anything else at the key stays a conflict."""
    from khipu.capture_identity import has_natural_key

    part = str(payload.get("part") or "")
    summary = payload.get("summary")
    if not part or not isinstance(summary, str) or not has_natural_key(payload):
        return None
    cur.execute("SELECT id FROM episodes WHERE ts = %s::timestamptz AND md5(summary) = %s "
                "ORDER BY deleted_at IS NOT NULL, id DESC LIMIT 1",
                (payload["ts"], _md5_hex(summary)))
    found = cur.fetchone()
    if not found:
        return None
    cur.execute("SELECT to_jsonb(e) FROM episodes e WHERE id = %s", (int(found[0]),))
    row = cur.fetchone()
    target = row[0] if row else None
    if not isinstance(target, dict) or target.get("deleted_at"):
        return None
    raw = target.get("raw") if isinstance(target.get("raw"), dict) else {}

    def held(key: str) -> str:
        value = raw.get(key, target.get(key))
        return "" if value is None else str(value)

    held_part = held("part")
    if not held_part or held_part == part:
        return None
    if any(held(k) != ("" if payload.get(k) is None else str(payload.get(k)))
           for k in ("harness", "session_id", "transcript_range", "window_id")):
        return None
    return f"{summary} (part {part})"


def write_pg(payload: dict[str, Any], *, legacy_projection: bool = False) -> dict[str, Any]:
    # forget-fence: capture writer: dedup and merge lookups; returns ids or the write's outcome
    """Durable PG write: episode row + any topic pages already on disk. Raises on failure.

    Returns ``episode_id`` (the row this capture landed on: inserted, merged
    into, skipped as already recorded, or already holding its legacy
    identity), ``episode_inserted`` and ``source_key`` (the capture's key from
    capture_identity.source_key), plus ``topics_written`` and ``dedup``.
    Callers take the id from here; none re-selects by (ts, md5(summary)).

    Runs the same three steps for every writer that reaches here (the native
    extractor via session_capture.drain, the legacy capture_v2-shaped payload
    path in dual/hub mode, and gateway/MCP captures) — W1.4 ingest dedup, then
    W5.1 topic-vs-tag classification, then the episode insert + W3.3
    commitments open/auto-close.
    """
    from khipu.db import connect
    from khipu.mirror import _upsert_episode, _upsert_topic, parse_topic_file

    # Every writer lands here (hook, MCP tool, gateway), so this is the one
    # place a secret in a summary, decision, loop or topic page is masked
    # before it becomes a row, a page or a vector.
    _validate_timestamp(payload)
    # Origin is stamped by the server from the request context, never taken
    # from the caller (gateway provenance, decision 1).
    payload.pop("origin", None)
    _redact_capture_values(payload)
    # The source identity is snapshotted here, from the redacted payload as it
    # arrived: before project inheritance and alias resolution read hub state
    # into it, and before ingestion rewrites topics, tags and scope. Replaying
    # the same capture must always produce the same key.
    from khipu.capture_identity import payload_sha256, source_identity

    source = source_identity(payload)
    topics_written = 0
    episode_id: int | None = None
    from khipu import memory_lock

    # Shared for the whole transaction, through its commit: a forget on this
    # Mac either finished before it began or starts after it committed.
    # The hub refuses an unprojected wording write at COMMIT (SQLSTATE
    # 08K01, fact provenance): the transaction rolled back whole, and the
    # retry re-reads readiness (positive-only, never cached as no).
    from khipu import provenance

    try:
        with memory_lock.shared(), connect() as conn:
            with conn.cursor() as cur:
                from khipu.tombstones import hub_source_forgotten

                # Lock source ownership before looking it up, through commit. Two
                # independent extractors may produce different summaries for the
                # same natural window; both must not insert from an empty lookup.
                # Collisions of this 64-bit advisory key only serialize extra work.
                lock_key = int(source["source_key"][:16], 16)
                if lock_key >= 1 << 63:
                    lock_key -= 1 << 64
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
                if hub_source_forgotten(cur, payload, source):
                    return _forgotten_result(cur, payload, source)
                if legacy_projection:
                    # Only explicitly identified, unknown-owner file recovery
                    # opts in. Prove against the untouched input before dedup,
                    # enrichment, claims or fact steps can mutate the hub.
                    cur.execute("SELECT id FROM episodes WHERE ts = %s::timestamptz AND md5(summary) = %s "
                                "ORDER BY deleted_at IS NOT NULL, id DESC LIMIT 1",
                                (payload["ts"], _md5_hex(payload["summary"])))
                    existing = cur.fetchone()
                    if existing:
                        existing_id = int(existing[0])
                        cur.execute("SELECT to_jsonb(e) FROM episodes e WHERE id = %s", (existing_id,))
                        row = cur.fetchone()
                        target = row[0] if row else {}
                        original = target.get("raw") if target else None
                        if target and not target.get("deleted_at"):
                            kind = _legacy_projection_kind(original, payload)
                            if kind:
                                return {
                                    "episode_inserted": False, "topics_written": 0,
                                    "episode_id": existing_id, "source_key": source["source_key"],
                                    "dedup": {"action": "skip", "matched_via": kind},
                                    "legacy_projection_duplicate": {
                                        "kind": kind, "payload": original, "episode_id": existing_id,
                                        "original_payload_sha256": payload_sha256(original),
                                        "queued_payload_sha256": source["payload_sha256"],
                                    },
                                }
                            if not isinstance(original, dict) or payload_sha256(original) != source["payload_sha256"]:
                                raise CaptureEvidenceConflict(
                                    f"legacy episode key conflicts with distinct evidence on episode {existing_id}; "
                                    "incoming capture retained for owner resolution")
                _inherit_project(cur, payload)
                _observe_checkout(cur, payload)
                if payload.get("project"):
                    from khipu.identity import _resolve_project_alias

                    # Clients must not supply typed_project; only this path sets it.
                    payload.pop("typed_project", None)
                    # Alias enrichment is optional. Keep its failure inside a
                    # caller-owned savepoint so even a lookup that raises before
                    # recovering its own savepoint cannot abort the capture.
                    cur.execute("SAVEPOINT capture_project_alias")
                    try:
                        typed = payload["project"]
                        canonical = _resolve_project_alias(cur, typed)
                    except Exception as exc:  # noqa: BLE001 — optional enrichment, never abort the caller
                        cur.execute("ROLLBACK TO SAVEPOINT capture_project_alias")
                        cur.execute("RELEASE SAVEPOINT capture_project_alias")
                        _log(f"project alias lookup failed ({type(exc).__name__}); keeping original project")
                    else:
                        cur.execute("RELEASE SAVEPOINT capture_project_alias")
                        # Keep the typed name in raw so alias remove can restore
                        # episodes that landed under the canonical (Q3 projects).
                        if canonical and canonical != typed:
                            payload["typed_project"] = typed
                            payload["project"] = canonical
                _note_project_root(cur, payload)
                dedup = dedup_before_insert(cur, payload, source=source)
                if dedup["action"] in ("skip", "merge"):
                    # ``episode_sources`` deliberately retains the source key on
                    # a tombstoned row: a delayed outbox replay must find that
                    # tombstone instead of treating the missing live row as an
                    # invitation to insert it again. It is a terminal outcome,
                    # not an ordinary dedup skip, so callers can avoid every
                    # secondary persistence leg (legacy file, mirror, vectors).
                    # On a hub with 0047's index the refusal table checked above
                    # is the one authority instead: a tombstone left by another
                    # capture's forget (a collateral carrier) does not forget
                    # this one, so it is matched against live episodes only.
                    cur.execute("SELECT to_jsonb(e) FROM episodes e WHERE id = %s",
                                (dedup.get("matched_episode"),))
                    matched = cur.fetchone()
                    target = matched[0] if matched else {}
                    settled = (_refusals_settled(cur)
                               if isinstance(target, dict) and target.get("deleted_at") else None)
                    if settled is False:
                        raise FactStepDeferred("history is not indexed yet; the capture matches "
                                               "a tombstoned episode and waits")
                    if settled:
                        dedup = dedup_before_insert(cur, payload, source=source, live_only=True)
                        target = {}
                if dedup["action"] in ("skip", "merge"):
                    if isinstance(target, dict) and target.get("deleted_at"):
                        conn.commit()
                        return {
                            "episode_inserted": False,
                            "topics_written": 0,
                            "episode_id": dedup.get("matched_episode"),
                            "source_key": source["source_key"],
                            "dedup": dedup,
                            "forgotten_source": True,
                        }
                    # The job is done once this returns (the outbox deletes it),
                    # so this is the last chance to record its trusted owner. Only
                    # an identity match (an exact window, a window_id, or a receipt
                    # of an identity merge) claims; origin is used for
                    # authorization, so a cosine or Jaccard resemblance, or a
                    # receipt of one, must never transfer ownership. A required
                    # claim that fails defers the write so the job is retried; one
                    # that is not required never blocks it.
                    if dedup.get("match") == MATCH_IDENTITY:
                        cur.execute("SAVEPOINT capture_claim")
                        try:
                            _claim_unowned_episode(cur, dedup.get("matched_episode"),
                                                   _stamp_origin(cur))
                        except Exception as exc:  # noqa: BLE001 - classified below
                            cur.execute("ROLLBACK TO SAVEPOINT capture_claim")
                            _log(f"origin claim failed ({type(exc).__name__}: {exc})")
                            if _claim_required():
                                raise ClaimDeferred(f"{type(exc).__name__}: {exc}") from exc
                        else:
                            cur.execute("RELEASE SAVEPOINT capture_claim")
                    conn.commit()
                    if dedup["action"] == "merge":
                        _refresh_merged_episode(cur, dedup["matched_episode"])
                    return {
                        "episode_inserted": False,
                        "topics_written": 0,
                        "episode_id": dedup.get("matched_episode"),
                        "source_key": source["source_key"],
                        "dedup": dedup,
                    }

                # W5.1: classify capture topics into topic-slugs vs noise-tags
                # (khipu.hygiene.classify_topics — see its docstring for the
                # actual rule). Gated on topic_aliases + episodes.tags existing
                # (migration 0010) and wrapped in its own SAVEPOINT, same as the
                # topic-graph and commitments steps below: a missing table here
                # used to raise mid-transaction and leave PG in
                # InFailedSqlTransaction, silently failing the episode insert
                # and every step after it, on every pre-migration hub.
                from khipu import hygiene
                from khipu.db import has_columns

                hygiene_ready = has_columns(cur, "episodes", "tags") and has_columns(
                    cur, "topic_aliases", "alias", "slug"
                )
                if hygiene_ready:
                    cur.execute("SAVEPOINT capture_hygiene")
                    try:
                        resolved_topics, tags, topics_unresolved = hygiene.classify_topics(
                            cur, payload.get("topics") or []
                        )
                    except Exception as exc:  # noqa: BLE001 — degrade, never abort the write
                        resolved_topics, tags, topics_unresolved = list(payload.get("topics") or []), [], True
                        cur.execute("ROLLBACK TO SAVEPOINT capture_hygiene")
                        _log(f"topic hygiene classify raised ({type(exc).__name__}: {exc})")
                    else:
                        if topics_unresolved:
                            # classify_topics caught its own PG error and degraded in
                            # Python, but a caught exception does not reset PG's
                            # SERVER-side aborted-transaction state — roll back to
                            # the savepoint to clear it before anything else runs.
                            cur.execute("ROLLBACK TO SAVEPOINT capture_hygiene")
                            _log("topic hygiene classify degraded; rolled back to savepoint")
                        else:
                            cur.execute("RELEASE SAVEPOINT capture_hygiene")
                else:
                    # Pre-migration hub: skip classification outright rather than
                    # attempt it and abort the write — every non-blank topic
                    # passes through unchanged, same as today's un-migrated
                    # behaviour, no tags minted (nothing to classify against).
                    seen: set[str] = set()
                    resolved_topics = []
                    for t in payload.get("topics") or []:
                        s = str(t or "").strip().lower()
                        if s and s not in seen:
                            seen.add(s)
                            resolved_topics.append(s)
                    tags, topics_unresolved = [], False
                payload = dict(payload)
                payload["topics"] = resolved_topics
                payload["tags"] = list(dict.fromkeys([*(payload.get("tags") or []), *tags]))
                if topics_unresolved:
                    payload["topics_unresolved"] = True
                # K6: scope is a free-text FALLBACK, not a project — normalise it
                # at write so a worktree path or a run-on sentence never lands in
                # the column every NULL-project reader (search, the pushed
                # slice) falls back to.
                from khipu.identity import normalize_scope

                payload["scope"] = normalize_scope(payload.get("scope"))

                origin = _stamp_origin(cur)
                upserted: dict[str, Any] = {}
                inserted = _upsert_episode(cur, payload, source=source, out=upserted, origin=origin)
                if upserted.get("forgotten_source"):
                    return _forgotten_result(cur, payload, source, dedup=dedup)
                episode_id = upserted.get("episode_id")
                sibling_summary = None if inserted else _sibling_part_summary(cur, payload)
                if sibling_summary:
                    payload["summary"] = sibling_summary
                    upserted = {}
                    inserted = _upsert_episode(cur, payload, source=source, out=upserted, origin=origin)
                    if upserted.get("forgotten_source"):
                        return _forgotten_result(cur, payload, source, dedup=dedup)
                    episode_id = upserted.get("episode_id")
                if not inserted:
                    # The insert hit the legacy unique key, so the row holding that
                    # key is where this capture landed (the key stays unique until
                    # phase 3 moves uniqueness to the source).
                    cur.execute(
                        "SELECT id FROM episodes WHERE ts = %s::timestamptz AND md5(summary) = %s "
                        "ORDER BY deleted_at IS NOT NULL, id DESC LIMIT 1",
                        (payload["ts"], _md5_hex(payload["summary"])),
                    )
                    existing = cur.fetchone()
                    episode_id = int(existing[0]) if existing else None
                    cur.execute("SELECT to_jsonb(e) FROM episodes e WHERE id = %s",
                                (episode_id,))
                    row = cur.fetchone()
                    target = row[0] if row else {}
                    # Legacy backfills can have only an episode key and no payload
                    # hash, so source dedup misses them even on an exact replay.
                    # With 0047's index only a tombstone of a hub whose history
                    # is not indexed yet holds the key (the insert trigger
                    # skipped the row): the capture waits for the pass.
                    if target.get("deleted_at") and _refusals_settled(cur) is not None:
                        raise FactStepDeferred("history is not indexed yet; the capture's legacy "
                                               "key is held by a tombstone and waits")
                    if target.get("deleted_at"):
                        conn.commit()
                        return {
                            "episode_inserted": False,
                            "topics_written": 0,
                            "episode_id": episode_id,
                            "source_key": source["source_key"],
                            "dedup": dedup,
                            "forgotten_source": True,
                        }
                    try:
                        _claim_unowned_episode(cur, episode_id, origin)
                    except Exception as exc:  # noqa: BLE001 - only a required claim runs SQL
                        raise ClaimDeferred(f"{type(exc).__name__}: {exc}") from exc
                    stored_raw = target.get("raw") or {}
                    # Known immutable sources/receipts were already matched above.
                    # At this legacy collision, only matching canonical raw content can
                    # prove another replay. Enrichment can make that proof absent;
                    # retaining an unproven capture is safer than discarding fields
                    # omitted from an ever-incomplete evidence allowlist.
                    if not target or payload_sha256(stored_raw) != source["payload_sha256"]:
                        raise CaptureEvidenceConflict(
                            f"legacy episode key conflicts with distinct evidence on episode {episode_id}; "
                            "incoming capture retained for resolution")
                from khipu.config import path_setting
                from khipu.topic_graph import persist_capture_graph

                cur.execute("SAVEPOINT capture_graph")
                try:
                    persist_capture_graph(cur, payload)
                except Exception as exc:  # noqa: BLE001 — episode row stays; graph is additive
                    cur.execute("ROLLBACK TO SAVEPOINT capture_graph")
                    _log(f"topic graph mint failed ({type(exc).__name__}: {exc})")

                if inserted:
                    if episode_id is not None:
                        run_fact_steps(cur, payload, episode_id)

                memory_root = path_setting("memory_root")
                # In dual mode capture_v2 has not run yet, so topic pages named in the
                # payload may not exist on disk; upsert whatever is already there and let
                # the nightly reconcile / capture_v2's file write catch the rest.
                # No file wiki configured (hub-only install): the episode is the
                # whole capture; there are no topic files to fold in.
                for tp in (payload.get("topic_pages") or []) if memory_root else []:
                    slug = (tp.get("slug") or "").strip() if isinstance(tp, dict) else ""
                    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
                        continue
                    topics_root = (memory_root / "topics").resolve()
                    path = (topics_root / f"{slug}.md").resolve()
                    if not path.is_relative_to(topics_root):
                        continue
                    parsed = parse_topic_file(path)
                    if parsed:
                        _redact_capture_values(parsed)
                    if parsed and _upsert_topic(
                        cur, parsed, str(path), source="capture", note="khipu capture"
                    ):
                        topics_written += 1
            conn.commit()
        return {
            "episode_inserted": inserted,
            "topics_written": topics_written,
            "episode_id": episode_id,
            "source_key": source["source_key"],
            "dedup": dedup,
        }
    except FactStepDeferred:
        raise
    except Exception as exc:
        if provenance.is_old_writer_refusal(exc):
            _log(f"capture refused by the fact gate ({type(exc).__name__}); the capture will be retried")
            raise FactStepDeferred(f"commit: refused unprojected write: {exc}") from exc
        # A deadlock, serialization failure, lock or statement timeout raised
        # anywhere in the transaction (a merge's target lock against a hub
        # refusal, design 6.1, or at COMMIT): it rolled back whole, and the
        # outbox retries it and never buries it.
        reason = _fact_deferral(exc)
        if reason is not None:
            _log(f"capture deferred ({reason}); the capture will be retried")
            raise FactStepDeferred(f"transaction: {reason}: {exc}") from exc
        raise


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Darwin answers EPERM while the group holds only a zombie that has
        # not been reaped yet: something is still there, so keep waiting.
        return True
    return True


def _kill_group(proc: subprocess.Popen, grace: float = 30.0) -> None:
    """SIGKILL every process in the writer's group, then wait until none is
    left, so the caller releases its locks only after the last one is gone."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.communicate()
    deadline = time.monotonic() + grace
    while _group_alive(proc.pid):
        if time.monotonic() >= deadline:
            raise RuntimeError(f"legacy writer group {proc.pid} survived SIGKILL")
        time.sleep(0.02)


def _run_legacy_group(argv: list[str], *, input: str, env: dict[str, str],
                      timeout: float, pass_fds: tuple[int, ...]) -> subprocess.CompletedProcess:
    """Run the legacy file writer in its own process group and return only
    when no process in that group remains.

    The real legacy script starts the mechanical writer without forwarding
    the lock descriptors, so descriptor inheritance cannot cover that
    grandchild: if the intermediary dies, the grandchild keeps writing.
    Waiting on the whole group keeps the caller's locks held until it is
    done. On timeout or cancel the whole group is killed and waited for.
    A process that leaves the group on purpose (setsid) is not covered.
    """
    from khipu import writer_groups

    deadline = time.monotonic() + timeout
    # The group is recorded durably before the writer runs, so a forget can
    # stop it even if this process is killed while it runs. The child waits
    # on a gate pipe until the record exists; if this process dies first,
    # the gate closes unopened and the child exits without running the writer.
    gate_r, gate_w = os.pipe()
    try:
        proc = subprocess.Popen([sys.executable, "-c", _GATE, str(gate_r), *argv],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env,
                                pass_fds=(*pass_fds, gate_r), start_new_session=True)
    except BaseException:
        os.close(gate_w)
        raise
    finally:
        os.close(gate_r)
    try:
        try:
            started = writer_groups.start_time(proc.pid)
            if started is None:
                raise OSError(f"legacy writer group {proc.pid} start time unavailable")
            writer_groups.record(proc.pid, started)
            os.write(gate_w, b"1")
        finally:
            os.close(gate_w)
        out, err = proc.communicate(input.encode("utf-8"), timeout=timeout)
        while _group_alive(proc.pid):
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(argv, timeout)
            time.sleep(0.05)
    except BaseException:
        _kill_group(proc)
        raise
    finally:
        writer_groups.clear(proc.pid)
    return subprocess.CompletedProcess(argv, proc.returncode, out.decode("utf-8", "replace"),
                                       err.decode("utf-8", "replace"))


# Waits for the go byte on the gate descriptor, then becomes the writer.
_GATE = (
    "import os, sys\n"
    "fd = int(sys.argv[1])\n"
    "go = os.read(fd, 1)\n"
    "os.close(fd)\n"
    "if go != b'1':\n"
    "    os._exit(75)\n"
    "os.execvp(sys.argv[2], sys.argv[2:])\n"
)


def run_capture_v2(payload: dict[str, Any], *, suppress_mirror: bool) -> int:
    from khipu.paths import isolated_dir

    if isolated_dir() is not None:
        # The promotion gate's capture: the hub row is the thing under test,
        # and the live file wiki must not gain (or later lose) its line.
        _log("isolated run (KHIPU_ISOLATED_DIR): file wiki leg skipped")
        return 0
    capture_v2 = _capture_v2()
    if capture_v2 is None:
        _log("capture_v2 not configured (khipu config --set capture_v2 PATH); "
             "file wiki not written")
        return EX_SOFTWARE
    if not capture_v2.is_file():
        _log(f"capture_v2 not found at {capture_v2} (khipu config --set capture_v2 PATH)")
        return EX_SOFTWARE
    env = dict(os.environ)
    if suppress_mirror:
        env["KHIPU_MIRROR"] = "0"
    # The registry flock and the memory lock are the file writer's write
    # locks: hold them until every process the legacy writer started is gone,
    # so a forget cannot complete after this final check and before any of
    # them appends its line.
    try:
        from khipu.tombstones import RegistryError, subprocess_lock_handoff, write_guard

        with write_guard(payload) as allowed:
            if not allowed:
                _log("forgotten source → file wiki leg skipped")
                return 0
            handoff_env, pass_fds = subprocess_lock_handoff()
            # Do not propagate a caller's stale/fabricated lock marker. Only
            # the owned guard above can export a descriptor to this child.
            for name in ("KHIPU_TOMBSTONE_LOCK_FD", "KHIPU_TOMBSTONE_LOCK_DEV",
                         "KHIPU_TOMBSTONE_LOCK_INO", "KHIPU_TOMBSTONE_REGISTRY"):
                env.pop(name, None)
            env.update(handoff_env)
            try:
                proc = _run_legacy_group(
                    [sys.executable, str(capture_v2)],
                    input=wire.dumps(payload),
                    env=env,
                    timeout=300,
                    pass_fds=pass_fds,
                )
            except subprocess.TimeoutExpired:
                # The PG row is already durable when we get here in dual/hub, and the
                # PG-failure path has already queued to the outbox — so a hung file leg
                # is a bad exit code, never an uncaught traceback out of the hook.
                _log(f"capture_v2 timed out after 300s ({capture_v2})")
                return EX_SOFTWARE
            except OSError as exc:
                _log(f"capture_v2 could not run: {type(exc).__name__}: {exc}")
                return EX_SOFTWARE
    except RegistryError as exc:
        _log(f"tombstone registry unavailable; file wiki leg refused ({exc})")
        return EX_SOFTWARE
    if proc.stdout.strip():
        print(proc.stdout.rstrip())
    if proc.stderr.strip():
        print(proc.stderr.rstrip(), file=sys.stderr)
    return proc.returncode


def capture(payload: dict[str, Any], *, mode: str | None = None,
            result: dict[str, Any] | None = None) -> int:
    """Route one capture by mode. ``result``, when given, receives write_pg's
    stats (``episode_id``, ``episode_inserted``, ``source_key``) on a hub
    write, so a caller that reports the episode never looks it up again.

    Holds the memory lock shared for every leg (hub row, outbox, legacy file,
    vectors), so a forget on this Mac never interleaves with one capture."""
    from khipu import memory_lock

    try:
        with memory_lock.shared():
            return _capture(payload, mode=mode, result=result)
    except memory_lock.MemoryLockError as exc:
        _log(f"memory lock unavailable; capture refused ({exc})")
        return EX_SOFTWARE


def _capture(payload: dict[str, Any], *, mode: str | None,
             result: dict[str, Any] | None) -> int:
    mode = (mode or capture_mode()).lower()
    try:
        _validate_timestamp(payload)
    except ValueError as exc:
        _log(str(exc))
        return EX_DATAERR
    _redact_capture_values(payload)
    if payload.get("scope") == "trivial":
        _log("scope=trivial — skipping per protocol")
        return 0

    # Hook jobs already carry the cached mapping. Explicit CLI/MCP captures
    # enter here without a job, and must retain the same thread provenance.
    if not payload.get("t3_thread_id"):
        from khipu import t3 as _t3
        if not hasattr(_t3, 'thread_for_session'):
            # A long-lived MCP server can still hold the t3 from before the
            # deploy that added this name; t3 is stdlib-only, so reloading is safe.
            import importlib
            importlib.reload(_t3)
        from khipu.t3 import thread_for_session

        sid = str(payload.get("session_id") or "")
        prefix, separator, native = sid.partition(":")
        if separator and prefix in {"claude_code", "codex", "cursor", "aegis", "antigravity"}:
            sid = native
        thread = thread_for_session(sid)
        if thread:
            payload.update(t3_thread_id=thread, via="t3")

    if mode == "legacy":
        _log("mode=legacy → capture_v2 only")
        return run_capture_v2(payload, suppress_mirror=False)

    # dual + hub: PG first, durably. If PG is unreachable the payload goes to the
    # outbox (replayed by the next Stop hook / nightly / `khipu outbox drain`)
    # and the file line is still written, so nothing is ever lost and nothing
    # is ever double-written (the replay is the same identity upsert).
    # write_pg pops the checkout observation off the payload; a rolled-back
    # write must still carry it into the outbox, so the retry gets a copy.
    checkout = payload.get("checkout")
    try:
        stats = write_pg(payload)
        if result is not None:
            result.update(stats)
        if stats.get("forgotten_source"):
            _log("capture source belongs to a forgotten episode; skipping persistence legs")
            return 0
    except CaptureIdentityConflict as exc:
        _log(str(exc))
        return EX_SOFTWARE
    except Exception as exc:  # noqa: BLE001 — surfaced, queued, never swallowed
        from khipu.outbox import enqueue

        _log(f"PG write FAILED ({type(exc).__name__}: {exc}) → outbox")
        retry = payload if checkout is None else {**payload, "checkout": checkout}
        try:
            enqueue(retry, reason=f"{type(exc).__name__}")
            queued = True
        except Exception as qexc:  # noqa: BLE001 — outbox itself unwritable
            _log(f"outbox enqueue FAILED ({type(qexc).__name__}: {qexc})")
            queued = False
        # The file leg keeps its own mirror ON here: if PG comes back mid-way it
        # lands the row now, and if not it re-queues the same identity (no dup).
        if mode == "hub" and os.environ.get("KHIPU_HUB_FILE_MIRROR", "1").strip().lower() in {"0", "false", "off"}:
            _log(f"hub delivery pending; durable_outbox={queued}")
            return EX_SOFTWARE
        rc = run_capture_v2(payload, suppress_mirror=False)
        if rc != 0:
            return rc
        return 0 if queued else EX_SOFTWARE
    _log(
        f"mode={mode} pg ok episode_inserted={stats['episode_inserted']} "
        f"topics_written={stats['topics_written']} ts={payload.get('ts')}"
    )
    # Vectors ride the capture (P3 step 3). Fail-open by design — the row is
    # already durable and `khipu embed backfill` heals any miss.
    if stats["episode_inserted"]:
        from khipu.embed import embed_on_capture

        embed_on_capture(payload, episode_id=stats.get("episode_id"))

    # dual: legacy file wiki is a peer; hub: PG is the record and the file is
    # its reverse mirror (plan: "Hub owns writes ... and can reverse-mirror to
    # files"). Same call either way — the legacy consumers (nightly consolidate,
    # topic amend, MEMORY.md) keep working in both modes. KHIPU_HUB_FILE_MIRROR=0
    # turns the reverse mirror off in hub for a future all-PG world.
    if mode == "hub" and os.environ.get("KHIPU_HUB_FILE_MIRROR", "1").strip().lower() in {"0", "false", "off"}:
        return 0
    rc = run_capture_v2(payload, suppress_mirror=True)
    return rc


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="khipu capture")
    ap.add_argument("--payload-file", help="Read JSON payload from a file instead of stdin")
    ap.add_argument(
        "--mode",
        choices=("legacy", "dual", "hub"),
        help="Override capture_mode for this run (default: Hub config)",
    )
    args = ap.parse_args(argv)
    raw = Path(args.payload_file).read_text(encoding="utf-8") if args.payload_file else sys.stdin.read()
    payload = load_payload(raw)
    return capture(payload, mode=args.mode)


if __name__ == "__main__":
    raise SystemExit(main())
