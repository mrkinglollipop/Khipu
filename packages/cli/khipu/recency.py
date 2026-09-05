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
