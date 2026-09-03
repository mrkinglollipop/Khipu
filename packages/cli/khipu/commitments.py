"""First-class commitments (W3) — open loops that outlive a single episode.

``decisions`` today are immutable strings in a JSONB array: nothing can be
opened, closed, superseded (root cause D). This module gives capture-time
``open_loops`` / ``closed_loops`` a lifecycle in the ``commitments`` table
(migration 0009): opened by an episode, auto-closed by a later episode's
closed_loop / decision / explicit ``done:`` prefix in the same project, or
aged into ``stale`` after 30 days untouched.

Every function here is called from the capture write path
(``khipu.capture.write_pg``) and must stay fail-open: an exception here must
never take down an episode insert that already succeeded.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

from khipu.capture import _jaccard  # shared with capture's own dedup (no byte copy)

VALID_KINDS = ("followup", "blocker", "question", "promise")
STALE_AFTER_DAYS = 30
DONE_MATCH_MIN_SCORE = 0.2  # fix 4: a minimal bar even for an explicit "done:" close
_DONE_PREFIX_RE = re.compile(r"^\s*done\s*:\s*", re.I)
_ISO_Z_RE = re.compile(r"Z$")
_RELATIVE_DUE_RE = re.compile(
    r"^\s*(?:in\s+)?(\d+)\s*(day|days|week|weeks|month|months)\s*$", re.I
)


def _log(msg: str) -> None:
    import sys

    print(f"[khipu-commitments] {msg}", file=sys.stderr)


def content_hash(scope: str | None, text: str) -> str:
    norm = re.sub(r"\s+", " ", (text or "").strip().lower())
    return hashlib.sha256(f"{scope or ''}\x00{norm}".encode("utf-8")).hexdigest()


def _coalesce_scope(payload: dict[str, Any]) -> str | None:
    """W3.3 grouping key (fix 3): ``project``, else ``parent_session_id``,
    else ``session_id`` — a capture with no resolved project (a scratchpad/
    `/tmp` cwd, a dispatched child session) still dedups/closes/lists against
    its OWN prior commitments instead of every such writer competing for one
    unscoped NULL bucket. Stored as the row's ``project`` column so
    ``list_owed``/``auto_close`` use the exact same key back."""
    for key in ("project", "parent_session_id", "session_id"):
        val = payload.get(key)
        if val:
            return str(val)
    return None


def _parse_due_after(raw: Any) -> tuple[str, Any]:
    """Lenient ``due_after`` parsing (fix 1): returns ``(sql_expr, param)`` to
    splice into the commitments INSERT. ISO 8601 / ``YYYY-MM-DD`` -> a literal
    timestamptz bound as a parameter. A bare ``N days|weeks|months`` or
    ``in N days`` -> ``now() + interval`` computed IN SQL (the transaction's
    own clock, not Python's). Anything else (free text like "next week" or
    "after the release", or empty) -> NULL. The model's phrase is never lost
    either way — the caller keeps it in the commitment's own ``text``/note.

    Binding unparseable text straight into the timestamptz column is exactly
    what used to raise ``InvalidDatetimeFormat`` and kill the whole
    commitments step (reproduced live, episode 11308) — this never binds
    anything but a real parsed value or NULL.
    """
    text = str(raw or "").strip()
    if not text:
        return "NULL", None
    iso_candidate = _ISO_Z_RE.sub("+00:00", text)
    try:
        datetime.fromisoformat(iso_candidate)
    except ValueError:
        pass
    else:
        return "%s::timestamptz", text
    m = _RELATIVE_DUE_RE.match(text)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        unit_sql = "days" if unit.startswith("day") else "weeks" if unit.startswith("week") else "months"
        return f"now() + interval '{n} {unit_sql}'", None
    return "NULL", None


def _normalize_open_loop(item: Any) -> dict[str, Any] | None:
    if isinstance(item, str):
        text = item.strip()
        if not text:
            return None
        return {"text": text, "kind": "followup", "due_after": None, "owner": None}
    if not isinstance(item, dict):
        return None
    text = str(item.get("text") or "").strip()
    if not text:
        return None
    kind = str(item.get("kind") or "followup").strip().lower()
    if kind not in VALID_KINDS:
        kind = "followup"
    return {
        "text": text,
        "kind": kind,
        "due_after": item.get("due_after") or None,
        "owner": item.get("owner") or None,
    }


def _has_open_duplicate(cur, scope: str | None, content_h: str) -> bool:
    """fix 3: the partial unique index ``uq_commitments_open_content(project,
    content_hash)`` cannot dedup a NULL ``project`` (standard SQL NULL != NULL
    means ON CONFLICT never fires for two NULL-scoped rows) — this in-code
    check is the fallback for exactly that case."""
    cur.execute(
        "SELECT 1 FROM commitments WHERE status = 'open' AND content_hash = %s "
        "AND project IS NOT DISTINCT FROM %s LIMIT 1",
        (content_h, scope),
    )
    return cur.fetchone() is not None


def open_from_episode(cur, payload: dict[str, Any], episode_id: int) -> int:
    """Insert one open commitment per ``open_loops`` item, deduped by
    ``(scope, content_hash)`` among currently-open rows, where ``scope`` is
    ``project`` if known else ``parent_session_id``/``session_id`` (fix 3) —
    stored in the row's ``project`` column so every later read uses the same
    key. Returns the count actually inserted."""
    items = payload.get("open_loops") or []
    if not isinstance(items, list) or not items:
        return 0
    scope = _coalesce_scope(payload)
    inserted = 0
    for raw in items:
        norm = _normalize_open_loop(raw)
        if norm is None:
            continue
        # content_hash and dedup key off the UNDECORATED text — the due
        # phrase is presentational, not part of the commitment's identity.
        h = content_hash(scope, norm["text"])
        if scope is None and _has_open_duplicate(cur, scope, h):
            continue
        due_sql, due_param = _parse_due_after(norm["due_after"])
        stored_text = norm["text"]
        due_phrase = str(norm["due_after"] or "").strip()
        if due_sql == "NULL" and due_phrase:
            # fix 1: the phrase didn't parse to a real date — never bind it
            # into the timestamptz column, but don't lose it either; it
            # survives as a parenthetical on the commitment's own text.
            stored_text = f"{stored_text} (due: {due_phrase})"
        base = [stored_text, scope, norm["owner"], norm["kind"], episode_id]
        params = (*base, due_param, h) if due_param is not None else (*base, h)
        cur.execute(
            f"""
            INSERT INTO commitments
              (text, project, owner, kind, opened_episode, due_after, content_hash)
            VALUES (%s, %s, %s, %s, %s, {due_sql}, %s)
            ON CONFLICT (project, content_hash) WHERE status = 'open' DO NOTHING
            """,
            params,
        )
        if cur.rowcount > 0:
            inserted += 1
    if inserted:
        _log(f"opened {inserted} commitment(s) for episode {episode_id} (scope={scope!r})")
    return inserted


def _open_commitments(cur, scope: str | None) -> list[dict[str, Any]]:
    cur.execute(
        "SELECT id, text FROM commitments WHERE status = 'open' AND project IS NOT DISTINCT FROM %s",
        (scope,),
    )
    return [{"id": r[0], "text": r[1]} for r in cur.fetchall()]


def _candidate_close_texts(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """(text, kind) pairs from closed_loops and decisions, where kind is
    ``"done"`` (a ``done:``-prefixed statement — matched by text only, never
    an API call) or ``"loop"`` (a closed_loop the extractor listed as a loop
    that closed — matched by text OR, when commitments are embedded, by
    cosine). Only ``done:``-prefixed decisions are candidates; a plain
    decision never closes a commitment so an unrelated decision cannot close
    one on a text or paraphrase fluke."""
    out: list[tuple[str, str]] = []
    for item in payload.get("closed_loops") or []:
        text = item.get("text") if isinstance(item, dict) else item
        text = str(text or "").strip()
        if not text:
            continue
        m = _DONE_PREFIX_RE.match(text)
        out.append((_DONE_PREFIX_RE.sub("", text), "done" if m else "loop"))
    for item in payload.get("decisions") or []:
        text = str(item or "").strip()
        if not text:
            continue
        if _DONE_PREFIX_RE.match(text):
            out.append((_DONE_PREFIX_RE.sub("", text), "done"))
    return out


def _match_score(a: str, b: str) -> float:
    """Best of Jaccard and substring/containment (fix 4) — used for the
    explicit ``done: <text>`` close, which has no similarity config of its
    own but must still match the CLOSED text against candidate commitment
    text rather than closing an arbitrary open row."""
    al, bl = a.strip().lower(), b.strip().lower()
    if al and bl and (al in bl or bl in al):
        return 1.0
    return _jaccard(a, b)


def _has_commitment_embeddings(cur, commitment_ids: list[int]) -> bool:
    """Cheap existence check (fix 5b): skip the embed API call entirely when
    no candidate commitment has a stored embedding yet — the embed catch-up
    (embed.embed_recent_missing) populates these out of band, not here."""
    if not commitment_ids:
        return False
    try:
        from khipu.embed import _active_profile

        profile = _active_profile(cur)
        cur.execute(
            "SELECT 1 FROM memory_embeddings WHERE profile = %s AND kind = 'commitment' "
            "AND chunk_idx = 0 AND ref = ANY(%s) LIMIT 1",
            (profile, [str(i) for i in commitment_ids]),
        )
        return cur.fetchone() is not None
    except Exception:  # noqa: BLE001 — fail-open to "no embeddings, use Jaccard"
        return False


def _cosine_scores(cur, text: str, commitment_ids: list[int]) -> dict[int, float]:
    """Best-effort cosine between ``text`` and each open commitment's stored
    embedding. Returns {} on any failure (no key, no embeddings yet, PG
    error) so the caller falls back to Jaccard — never raises."""
    if not commitment_ids:
        return {}
    try:
        from khipu.embed import _active_profile, _vec_literal, embed_one

        profile = _active_profile(cur)
        vec = embed_one(text, profile=profile)
        cur.execute(
            """
            SELECT ref, 1 - (embedding <=> %s::vector) AS score
            FROM memory_embeddings
            WHERE profile = %s AND kind = 'commitment' AND chunk_idx = 0
              AND ref = ANY(%s)
            """,
            (_vec_literal(vec), profile, [str(i) for i in commitment_ids]),
        )
        return {int(ref): float(score) for ref, score in cur.fetchall() if score is not None}
    except Exception:  # noqa: BLE001 — fail-open to Jaccard
        return {}


def auto_close(cur, payload: dict[str, Any], episode_id: int) -> int:
    """Match every closed_loop / done-decision against this scope's open
    commitments (``scope``: project, else parent_session_id/session_id — fix
    3); close the best match at or above the configured similarity, or for
    an explicit ``done:`` prefix, the best text match at or above
    ``DONE_MATCH_MIN_SCORE`` (fix 4 — never the first unordered row, and
    never anything if nothing clears the bar). Returns count closed.
    """
    scope = _coalesce_scope(payload)
    candidates = _candidate_close_texts(payload)
    if not candidates:
        return 0
    open_rows = _open_commitments(cur, scope)
    if not open_rows:
        return 0
    from khipu.config import float_setting

    threshold = float_setting("commitment_close_similarity")
    ids = [r["id"] for r in open_rows]
    already_closed: set[int] = set()
    closed = 0
    for text, kind in candidates:
        remaining_ids = [i for i in ids if i not in already_closed]
        # Cosine (semantic paraphrase) is available for a closed_loop when the
        # scope's commitments are embedded; a "done:" close is text-only (fix
        # 5a) and never spends an API call, and with no stored commitment
        # embeddings (fix 5b) there is nothing to score against.
        if kind != "done" and remaining_ids and _has_commitment_embeddings(cur, remaining_ids):
            cosine = _cosine_scores(cur, text, remaining_ids)
        else:
            cosine: dict[int, float] = {}
        best_id = None
        best_score = 0.0
        best_via = ""
        for row in open_rows:
            cid = row["id"]
            if cid in already_closed:
                continue
            # Two signals, two bars. A containment/lexical text match closes at
            # the low DONE_MATCH_MIN_SCORE bar — a short closed_loop phrase is a
            # subset of a longer commitment, so containment (folded into
            # _match_score), not raw Jaccard, is what matches it. A cosine match
            # needs the strict commitment_close_similarity bar so a merely
            # related paraphrase cannot close the wrong item.
            text_score = _match_score(text, row["text"])
            cos = cosine.get(cid)
            score = 0.0
            via = ""
            if text_score >= DONE_MATCH_MIN_SCORE:
                score = text_score
                via = "explicit done (best match)" if kind == "done" else "text"
            if cos is not None and cos >= threshold and cos > score:
                score, via = cos, "cosine"
            if via and score > best_score:
                best_id, best_score, best_via = cid, score, via
        if best_id is None:
            continue
        cur.execute(
            """
            UPDATE commitments
            SET status = 'closed', closed_episode = %s, closed_at = now(),
                close_reason = %s
            WHERE id = %s AND status = 'open'
            """,
            (episode_id, f"{best_via} match ({best_score:.2f}): {text[:200]!r}", best_id),
        )
        if cur.rowcount > 0:
            already_closed.add(best_id)
            closed += 1
            _log(f"closed commitment {best_id} via {best_via} ({best_score:.2f}) at episode {episode_id}")
    return closed


def mark_stale(cur) -> int:
    """Flip open commitments older than STALE_AFTER_DAYS to 'stale'. Never
    silently dropped — stale stays queryable via `khipu owed --status stale`.
    """
    cur.execute(
        """
        UPDATE commitments
        SET status = 'stale'
        WHERE status = 'open' AND opened_at < now() - interval '%s days'
        """ % STALE_AFTER_DAYS
    )
    return cur.rowcount


def list_owed(cur, *, project: str | None = None, parent_session_id: str | None = None,
              session_id: str | None = None, status: str = "open",
              limit: int = 50) -> list[dict[str, Any]]:
    """``project`` if given, else ``parent_session_id``/``session_id`` (fix
    3) — the same coalesced key ``open_from_episode``/``auto_close`` store
    commitments under, so a caller with only session context (no resolved
    project) can still find its own scope's commitments."""
    scope = project or parent_session_id or session_id
    clauses = ["status = %s"]
    params: list[Any] = [status]
    if scope:
        clauses.append("project = %s")
        params.append(scope)
    params.append(limit)
    cur.execute(
        f"""
        SELECT id, text, project, owner, kind, opened_episode, opened_at,
               due_after, status, closed_episode, closed_at, close_reason
        FROM commitments
        WHERE {' AND '.join(clauses)}
        ORDER BY opened_at DESC
        LIMIT %s
        """,
        params,
    )
    cols = ("id", "text", "project", "owner", "kind", "opened_episode", "opened_at",
            "due_after", "status", "closed_episode", "closed_at", "close_reason")
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def set_status(cur, commitment_id: int, status: str) -> bool:
    if status not in ("open", "closed", "stale"):
        raise ValueError(f"status must be open/closed/stale, got {status!r}")
    if status == "open":
        cur.execute(
            "UPDATE commitments SET status = 'open', closed_episode = NULL, "
            "closed_at = NULL, close_reason = NULL WHERE id = %s",
            (commitment_id,),
        )
    else:
        extra = ", closed_at = now()" if status == "closed" else ""
        cur.execute(
            f"UPDATE commitments SET status = %s{extra} WHERE id = %s",
            (status, commitment_id),
        )
    return cur.rowcount > 0
