"""Retention by decay in search ranking (audit 2026-09-04): "Nothing ages; old
rows compete for search slots forever." Rows are never deleted for age — this
module only nudges fresher rows ahead of equally-relevant older ones by adding
a small, exponentially-decaying bonus to the fused score.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

# Phase 2, session B: khipu.validity is the single source of truth for the
# not-current status set; recency's own DERANKED_STATUSES below is an alias
# of it (same members) so the de-rank here and the validity policy can never
# drift apart.
from khipu.validity import NOT_CURRENT_TOPIC_STATUSES as DERANKED_STATUSES

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
# DERANKED_STATUSES itself is imported above, aliased from khipu.validity.
STATUS_DERANK = 0.5
# P5 G5: a harness-native note's own `type` (feedback/user/project/reference,
# set by khipu.notes._note_topic_dict and carried on the row by
# khipu.embed's topic metadata join) — feedback (a hard-won lesson) and user
# (an explicit preference/instruction) are the two kinds worth surfacing
# ahead of a same-scoring project/reference note for THIS project; applied
# only when the row already won the project boost above (`type` without a
# matching project is not "for the current project").
TYPE_BOOSTED = frozenset({"feedback", "user"})
TYPE_BOOST = 1.15
# Catch-all pages (a merged raw-memories dump, a memory summary, a status
# board) name every word and embed near every query, so they topped unrelated
# searches (2026-10-29). Measured on this Mac's notes: median 2.2 KB, 90th
# percentile 6.3 KB; the catch-alls were 27 KB to 1 MB. The body length rides
# on the row as ``page_chars`` from the metadata pass; a row without it is
# never treated as broad.
BROAD_PAGE_CHARS = 16_000
BROAD_DERANK = 0.5
# khipu.extract writes this summary when the model returned none. Every such
# episode embeds as near-identical text and renders as a line that says
# nothing, so automatic push de-ranks it; explicit search still finds it.
PLACEHOLDER_SUMMARY = "Session evidence retained without a model summary."
PUSH_PLACEHOLDER_DERANK = 0.25
# Automatic push only: a row whose project is known and is not the session's.
# A de-rank, never an exclusion, so a strong cross-project match still shows.
CROSS_PROJECT_DERANK = 0.6


def is_broad_page(row: dict[str, Any]) -> bool:
    if row.get("kind") != "topic":
        return False
    try:
        return int(row.get("page_chars") or 0) >= BROAD_PAGE_CHARS
    except (TypeError, ValueError):
        return False


def is_placeholder_episode(row: dict[str, Any]) -> bool:
    if row.get("kind") != "episode":
        return False
    if row.get("placeholder_summary") is True:
        return True
    return any(str(row.get(k) or "").strip().startswith(PLACEHOLDER_SUMMARY)
               for k in ("label", "snippet"))


def _group_norm(project_group: Any) -> frozenset[str]:
    try:
        return frozenset(str(p).strip().lower() for p in (project_group or ()) if p)
    except TypeError:
        return frozenset()


def _project_matches(row: dict[str, Any], proj_norm: str, group: frozenset[str] = frozenset()) -> bool:
    """The session's project: the same substring test as the ``project=``
    filter, or an exact member of its alias group (a name recorded before a
    rename), matched exactly as the search filter matches alias members."""
    row_project = str(row.get("project") or "").strip().lower()
    return bool(row_project) and (proj_norm in row_project or row_project in group)


def apply_push_policy(
    rows: list[dict[str, Any]], *, project: str | None = None,
    project_group: Any = None,
) -> list[dict[str, Any]]:
    """Selection for automatic push (the per-prompt hook and khipu_status's
    prior_work), on top of ``apply_project_and_status``. Explicit search never
    calls this, so everything it leaves out stays searchable.

    Leaves out topics that are not current (superseded/retired/abandoned or
    ``superseded_by`` set) and catch-all pages (``is_broad_page``). De-ranks
    placeholder-summary episodes and rows from another known project. Rows
    are mutated in place and returned re-sorted by score. Never raises.
    """
    if not rows:
        return rows
    from khipu.validity import topic_state

    proj_norm = (project or "").strip().lower()
    group = _group_norm(project_group)
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            if row.get("kind") == "topic":
                validity = row.get("validity")
                state = validity.get("state") if isinstance(validity, dict) else None
                if state == "superseded" or topic_state(row.get("status"), row.get("superseded_by")) == "superseded":
                    continue
                if is_broad_page(row):
                    continue
            score = float(row.get("score") or 0.0)
            if is_placeholder_episode(row):
                score *= PUSH_PLACEHOLDER_DERANK
            if proj_norm and row.get("project") and not _project_matches(row, proj_norm, group):
                score *= CROSS_PROJECT_DERANK
            row["score"] = round(score, 6)
        except Exception:  # noqa: BLE001 — one bad row must not sink the push
            pass
        out.append(row)
    return sorted(out, key=lambda r: -(r.get("score") or 0.0))


def apply_project_and_status(
    rows: list[dict[str, Any]], *, project: str | None = None,
    project_group: Any = None,
) -> list[dict[str, Any]]:
    """Ranking-only project boost (R5) + topic status de-rank (R6).

    ``project`` is optional. When supplied it is a BOOST, never a hard filter: a hit from a different
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

    ``project_group`` (automatic push only) is the project's alias group; a
    row recorded under one of those names matches too.

    Rows are mutated in place and returned re-sorted by score desc (stable
    on ties). Never raises — a bad row's score defaults to 0.0 rather than
    aborting the whole list.
    """
    if not rows:
        return rows
    proj_norm = (project or "").strip().lower()
    group = _group_norm(project_group)
    for row in rows:
        try:
            score = float(row.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        try:
            if proj_norm:
                if _project_matches(row, proj_norm, group):
                    score *= PROJECT_BOOST
                    if row.get("kind") == "topic":
                        note_type = str(row.get("type") or "").strip().lower()
                        if note_type in TYPE_BOOSTED:
                            score *= TYPE_BOOST
            if row.get("kind") == "topic":
                status = str(row.get("status") or "").strip().lower()
                if status in DERANKED_STATUSES or row.get("superseded_by"):
                    score *= STATUS_DERANK
                if is_broad_page(row):
                    score *= BROAD_DERANK
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
            bonus = RECENCY_WEIGHT * (0.5 ** (max(0.0, age) / half_life))
            row["score"] = round((row.get("score") or 0.0) + bonus, 6)
            row["recency"] = round(bonus, 6)
        except Exception:  # noqa: BLE001 — a single bad row must not sink the search.
            continue

    return sorted(rows, key=lambda r: -(r.get("score") or 0.0))


def rank_for_push(
    rows: list[dict[str, Any]], *, project: str | None = None,
    project_group: Any = None, now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Automatic push's whole score pass, the one the replica lane and both
    hub lanes run so they order the same rows the same way: project boost and
    status de-rank, then the push policy, then recency last (the review of
    2026-10-30 found the hub fallback applying recency before the policy, so
    the cross-project de-rank also shrank the recency bonus there). A recency
    bonus already on a row is taken off first, so a pass is never counted
    twice. Never raises."""
    for row in rows or ():
        try:
            bonus = row.pop("recency", None)
            if bonus:
                row["score"] = round(float(row.get("score") or 0.0) - float(bonus), 6)
        except Exception:  # noqa: BLE001 — one bad row must not sink the push
            continue
    rows = apply_project_and_status(rows, project=project, project_group=project_group)
    rows = apply_push_policy(rows, project=project, project_group=project_group)
    return apply_recency(rows, now=now)
