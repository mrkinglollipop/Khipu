"""Episodes the summariser left without a summary.

The model is told to return an empty summary when a window holds nothing
durable, yet the regex evidence tiers (verbatim, deliverables) almost always
match something. Those windows used to be stored under one fixed sentence;
229 rows carried it by 2026-10-30, embedded alike, and crowded real memories
out of recall. A window like that is now kept as a verbatim-only record: its
evidence is stored in full, its summary is empty and the payload carries
``summary_kind = "verbatim_only"``. Automatic pushes skip these records;
explicit search still finds them by their verbatim text.
"""
from __future__ import annotations

import re
from typing import Any

VERBATIM_ONLY = "verbatim_only"
# What rows written before the marker carry. Kept so old rows are recognised
# without a backfill having run.
LEGACY_PLACEHOLDER = "Session evidence retained without a model summary."
NO_SUMMARY_LABEL = "(no summary: verbatim evidence only)"

# A sibling part of an empty-summary window lands as "(part 4/7)" so it does
# not collide with its siblings on (ts, md5(summary)).
_PART_ONLY = re.compile(r"^\(part [^)]*\)$")

# For SQL over episodes (summary, raw). Push queries add "AND NOT <this>".
VERBATIM_ONLY_SQL = (
    "(btrim(COALESCE(summary, '')) = '' OR COALESCE(raw->>'summary_kind', '') = 'verbatim_only' "
    "OR summary LIKE 'Session evidence retained without a model summary.%%' "
    "OR summary ~ '^\\(part [^)]*\\)$')"
)


def summary_text_is_empty(summary: Any) -> bool:
    text = str(summary or "").strip()
    return not text or text.startswith(LEGACY_PLACEHOLDER) or bool(_PART_ONLY.match(text))


def is_verbatim_only(summary: Any = None, raw: Any = None) -> bool:
    """Whether an episode (its summary text and, when known, its raw payload)
    is a verbatim-only record: the marker first, then the old placeholder, an
    empty summary or a bare part label."""
    if isinstance(raw, dict) and raw.get("summary_kind") == VERBATIM_ONLY:
        return True
    return summary_text_is_empty(summary)


def is_verbatim_only_hit(hit: Any) -> bool:
    """A search or slice row for an episode that has no summary of its own."""
    if not isinstance(hit, dict) or hit.get("kind", "episode") != "episode":
        return False
    if hit.get("summary_kind") == VERBATIM_ONLY:
        return True
    if "summary" in hit:
        text = hit["summary"]
    elif "snippet" in hit:
        text = hit["snippet"]
    else:
        # A row that carries no summary text at all (a label-only ranking row)
        # says nothing about being empty; only a present, blank summary does.
        text = "x"
    return is_verbatim_only(text, hit.get("raw"))


def without_verbatim_only(rows: list[Any]) -> list[Any]:
    """``rows`` minus episode rows with no summary (automatic pushes only)."""
    return [r for r in rows if not is_verbatim_only_hit(r)]
