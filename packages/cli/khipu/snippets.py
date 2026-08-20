"""Word-boundary clip for search/status teasers.

SQL ``left(text, N)`` cuts mid-token ("include" → "includ"). Search hits and
status recents are teasers; full rows go through ``khipu_get`` / ``activity --show``.
"""
from __future__ import annotations

ELLIPSIS = "…"

# Search snippet / status recent-summary. Episode summaries average ~600 chars;
# this keeps a full typical summary and only clips the long tail.
SNIPPET_LIMIT = 500
# Compact list label (MCP ``label`` / ILIKE label column).
LABEL_LIMIT = 120
# SQL fetch cap so a 10k-char topic body does not ride into every search row.
FETCH_LIMIT = 4000


def clip_snippet(text: str | None, limit: int) -> str:
    """Trim to ``limit`` on a word boundary; ellipsis only when truncated."""
    text = (text or "").strip()
    if limit < 1:
        return ""
    if len(text) <= limit:
        return text
    budget = max(1, limit - len(ELLIPSIS))
    cut = text[:budget]
    sp = -1
    for i in range(len(cut) - 1, -1, -1):
        if cut[i].isspace():
            sp = i
            break
    if sp >= budget // 2:
        cut = cut[:sp]
    return cut.rstrip(" ,;:-") + ELLIPSIS
