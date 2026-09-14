# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""Deliverables index (O4) — "have I already built this?" answerable.

2,193 ``path:`` graph nodes come from topic bodies today; none from episodes
(root cause O4). ``khipu.extract.extract_deliverables`` pulls files
written/created, PR/issue URLs, and release tags out of a capture window by
regex (same tier as the K2 verbatim tier); this module gives each one its
own row (migration 0020) and answers "have I already built this?" for the
prompt-time recall hook (``khipu.recall_prompt``).

Every write here is called from the same SAVEPOINT-guarded capture step
``khipu.commitments``/``khipu.decisions`` use and must stay fail-open.
"""
from __future__ import annotations

from typing import Any, Sequence

VALID_KINDS = ("file", "pr", "issue", "release")


def _log(msg: str) -> None:
    import sys

    print(f"[khipu-deliverables] {msg}", file=sys.stderr)


def _deliverables_ready(cur) -> bool:
    """True when migration 0020 (the ``deliverables`` table) has been
    applied. Fail-closed, same posture as ``khipu.decisions``/
    ``khipu.commitments``'s readiness checks."""
    try:
        from khipu.db import has_columns

        return has_columns(cur, "deliverables", "id", "project", "kind")
    except Exception:  # noqa: BLE001 — introspection is best-effort
        return False


def insert_deliverables_from_episode(cur, payload: dict[str, Any], episode_id: int) -> int:
    """One row per item in ``payload['deliverables']`` (the shape
    ``khipu.extract.extract_deliverables`` returns). Dedup: the same
    (project, kind, path, url) already on file is skipped — a file rewritten
    across several sessions is one deliverable, not a growing pile."""
    items = payload.get("deliverables") or []
    if not isinstance(items, list) or not items:
        return 0
    if not _deliverables_ready(cur):
        return 0
    project = payload.get("project")
    inserted = 0
    for raw in items:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "").strip().lower()
        if kind not in VALID_KINDS:
            continue
        path = str(raw.get("path") or "").strip() or None
        url = str(raw.get("url") or "").strip() or None
        title = str(raw.get("title") or "").strip() or None
        if not path and not url and not title:
            continue
        cur.execute(
            "SELECT id FROM deliverables WHERE project IS NOT DISTINCT FROM %s "
            "AND kind = %s AND path IS NOT DISTINCT FROM %s AND url IS NOT DISTINCT FROM %s "
            "LIMIT 1",
            (project, kind, path, url),
        )
        if cur.fetchone():
            continue
        cur.execute(
            "INSERT INTO deliverables (project, kind, path, url, title, episode_id) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (project, kind, path, url, title, episode_id),
        )
        if cur.rowcount > 0:
            inserted += 1
    if inserted:
        _log(f"recorded {inserted} deliverable(s) for episode {episode_id} (project={project!r})")
    return inserted


def recent_deliverables(cur, *, project: str, limit: int = 200) -> list[dict[str, Any]]:
    if not project or not _deliverables_ready(cur):
        return []
    cur.execute(
        "SELECT id, project, kind, path, url, title, episode_id, created_at "
        "FROM deliverables WHERE project = %s ORDER BY created_at DESC LIMIT %s",
        (project, limit),
    )
    cols = ("id", "project", "kind", "path", "url", "title", "episode_id", "created_at")
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---- the recall-hook matcher (unit-tested without a DB) ---------------------

def token_match_count(text: str, tokens: Sequence[str]) -> int:
    """How many ``tokens`` appear (word-bounded, case-insensitive) in
    ``text``. Reuses the same counter search already ranks hits with, so
    "matches" means the same thing here as it does everywhere else."""
    from khipu.search_text import token_hit_count

    return token_hit_count(text, tokens)


def best_match_for_tokens(
    rows: Sequence[dict[str, Any]], tokens: Sequence[str], *, min_hits: int = 2
) -> dict[str, Any] | None:
    """The deliverable row whose path+title together hit the most ``tokens``
    (>= ``min_hits``), or None. Ties break toward the newest ``created_at``
    (rows are expected pre-sorted newest-first, so the first max found wins)."""
    if not tokens:
        return None
    best: dict[str, Any] | None = None
    best_hits = 0
    for row in rows:
        text = " ".join(str(v) for v in (row.get("path"), row.get("title")) if v)
        if not text:
            continue
        hits = token_match_count(text, tokens)
        if hits >= min_hits and hits > best_hits:
            best, best_hits = row, hits
    return best


def format_produced_line(row: dict[str, Any]) -> str:
    """"You produced <path> on <date> (episode N)"."""
    when = str(row.get("created_at") or "")[:10]
    target = row.get("path") or row.get("url") or row.get("title") or "it"
    episode = row.get("episode_id")
    return f"You produced {target} on {when} (episode {episode})"


def deliverable_line_for_prompt(
    cur, prompt_tokens: Sequence[str], *, project: str | None, min_hits: int = 2
) -> str | None:
    """The "You produced …" line for a prompt, or None. Never raises."""
    if not project or not prompt_tokens:
        return None
    try:
        rows = recent_deliverables(cur, project=project)
        match = best_match_for_tokens(rows, prompt_tokens, min_hits=min_hits)
    except Exception as exc:  # noqa: BLE001 — the recall hook must still return its hits
        _log(f"deliverable_line_for_prompt failed ({type(exc).__name__}: {exc})")
        return None
    return format_produced_line(match) if match else None
