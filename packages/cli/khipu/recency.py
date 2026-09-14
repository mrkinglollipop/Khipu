"""Retention by decay in search ranking (audit 2026-09-04): "Nothing ages; old
rows compete for search slots forever." Rows are never deleted for age — this
module only nudges fresher rows ahead of equally-relevant older ones by adding
a small, exponentially-decaying bonus to the fused score.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

HALF_LIFE_DAYS = float(os.environ.get("KHIPU_SEARCH_HALF_LIFE_DAYS", "90"))

try:
    # RRF_K is khipu.search_text's reciprocal-rank-fusion constant. One
    # first-place term of that fusion is 1 / (RRF_K + 1) — so a fresh row
    # (age 0) gains at most what one more top rank in one fusion leg would
    # have gained it, and a row several half-lives old gains effectively
    # nothing. Old rows are never removed; they just stop being able to
    # outrank a newer row of equal relevance. Derived from the live constant
    # rather than hardcoded so the two never drift apart.
    from khipu.search_text import RRF_K as _RRF_K

    RECENCY_WEIGHT = 1.0 / (_RRF_K + 1)
except Exception:  # noqa: BLE001 — import shape may change; fall back safely.
    RECENCY_WEIGHT = 1.0 / 61.0


def age_days(ts: Any, now: datetime | None = None) -> float | None:
    """Age of ``ts`` in days, or None if ``ts`` is missing/unparseable.

    ``ts`` may be a ``datetime`` (aware or naive — naive is treated as UTC)
    or an ISO-8601 string (a trailing "Z" is accepted). Never raises.
    """
    if ts is None:
        return None
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        if isinstance(ts, str):
            s = ts.strip()
            if not s:
                return None
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
        elif isinstance(ts, datetime):
            dt = ts
        else:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (now - dt).total_seconds() / 86400.0
    except Exception:  # noqa: BLE001 — unparseable ts must never raise here.
        return None


PROJECT_BOOST = 1.25
# Topic housekeeping status (R6): free text today, but these three values are
# the ones capture/mirror/notes actually write for "this is not current".
DERANKED_STATUSES = frozenset({"superseded", "retired", "abandoned"})
STATUS_DERANK = 0.5


def apply_project_and_status(
    rows: list[dict[str, Any]], *, project: str | None = None
) -> list[dict[str, Any]]:
    """Ranking-only project boost (R5) + topic status de-rank (R6).

    ``project`` is a BOOST, never a hard filter: a hit from a different
    project still shows, it just loses the tie-break it would otherwise win
    against an equally-relevant hit from ``project`` (R5's reproduced bug —
    a contradicting decision from the wrong project outranking the real one
    twice). Matching is the same case-insensitive substring test the hard
    ``project=`` filter already uses, so a hit already carrying a resolved
    ``project`` field (episodes always; topics when their frontmatter names
    one) benefits identically either way.

    A topic row whose ``status`` is superseded/retired/abandoned (R6 — never
    read by search before this) is de-ranked, not dropped: it can still
    surface, just behind a current page that says the same thing. Applied
    multiplicatively so it composes with the project boost and with
    ``apply_recency``'s additive bonus in any order.

    Rows are mutated in place and returned re-sorted by score desc (stable
    on ties). Never raises — a bad row's score defaults to 0.0 rather than
    aborting the whole list.
    """
    if not rows:
        return rows
    proj_norm = (project or "").strip().lower()
    for row in rows:
        try:
            score = float(row.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        try:
            if proj_norm:
                row_project = str(row.get("project") or "").strip().lower()
                if row_project and proj_norm in row_project:
                    score *= PROJECT_BOOST
            if row.get("kind") == "topic":
                status = str(row.get("status") or "").strip().lower()
                if status in DERANKED_STATUSES:
                    score *= STATUS_DERANK
        except Exception:  # noqa: BLE001 — one bad row must not sink the search
            pass
        row["score"] = round(score, 6)
    return sorted(rows, key=lambda r: -(r.get("score") or 0.0))


def apply_recency(
    rows: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    half_life_days: float | None = None,
) -> list[dict[str, Any]]:
    """Add a recency bonus to each row's score and re-sort by score desc.

    ``half_life_days`` defaults to ``HALF_LIFE_DAYS``; <= 0 disables decay
    entirely (rows returned unchanged, in their original order). A row with
    no parseable ``ts`` gets no bonus and no ``recency`` key. Ties (equal
    score) keep their original relative order — Python's sort is stable.
    Never raises.
    """
    half_life = HALF_LIFE_DAYS if half_life_days is None else half_life_days
    try:
        half_life = float(half_life)
    except Exception:  # noqa: BLE001
        return rows
    if half_life <= 0:
        return rows
    now = now or datetime.now(timezone.utc)

    for row in rows:
        try:
            age = age_days(row.get("ts"), now)
            if age is None:
                continue
            bonus = RECENCY_WEIGHT * (0.5 ** (age / half_life))
            row["score"] = round((row.get("score") or 0.0) + bonus, 6)
            row["recency"] = round(bonus, 6)
        except Exception:  # noqa: BLE001 — a single bad row must not sink the search.
            continue

    return sorted(rows, key=lambda r: -(r.get("score") or 0.0))
