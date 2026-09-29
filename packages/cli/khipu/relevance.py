# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""The one absolute relevance policy, behind the ``relevance_floor`` switch.

``recall_prompt._apply_score_floor`` is relative to the top row, so a prompt
about something memory has never seen still keeps its three nearest
neighbours: nearest-neighbour search always returns something. This module
judges each row on its own evidence instead. A row is kept when

- it names at least one query token as a whole word (``lexical_hits`` >= 1), or
- its raw cosine similarity is at or above ``COSINE_FLOOR``, or
- nothing is known against it: no keyword count and no cosine value at all
  (a graph-candidate row, a literal-only hub row).

One function serves every query-driven reader (the local prompt lane, the
budgeted hub lane, explicit hybrid/semantic search), the way ``validity``
serves every reader, so the three cannot drift apart. Literal mode never
calls it. Pure functions over already-fetched rows: no database, no network.
"""
from __future__ import annotations

from typing import Any, Sequence

# Provisional. Raw cosine on the active embedding profile has a high baseline
# (unrelated queries measured ~0.65-0.73 on the production profile, see
# embed.CONFIDENCE_COSINE_*), so this value is a starting point to be tuned
# with ``khipu recall eval --relevance-floor on``, not a measured optimum.
# Overridable by the ``relevance.cosine_floor`` config key.
COSINE_FLOOR = 0.62


def enabled() -> bool:
    """The ``relevance_floor`` switch. Fails open to off: a broken config read
    must never cost a caller its search."""
    try:
        from khipu import features

        return features.enabled("relevance_floor")
    except Exception:  # noqa: BLE001 — a switch lookup must not sink a search
        return False


def cosine_floor() -> float:
    """``relevance.cosine_floor`` from config.json when it is a number in
    (0, 1], else ``COSINE_FLOOR``. A bool, a string or an out-of-range value
    is ignored, never raised."""
    try:
        from khipu.config import load_config

        section = load_config().get("relevance")
        raw = section.get("cosine_floor") if isinstance(section, dict) else None
    except Exception:  # noqa: BLE001 — config trouble means the default
        return COSINE_FLOOR
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return COSINE_FLOOR
    return float(raw) if 0.0 < float(raw) <= 1.0 else COSINE_FLOOR


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def keeps(row: dict[str, Any], floor: float) -> bool:
    """The policy for one row."""
    hits = _number(row.get("lexical_hits"))
    if hits is not None and hits >= 1:
        return True
    cosine = _number(row.get("cosine"))
    if cosine is not None:
        return cosine >= floor
    return True


def apply(rows: Sequence[dict[str, Any]], *, floor: float | None = None) -> tuple[list[dict[str, Any]], int]:
    """``(kept rows in their original order, number removed)``."""
    limit = cosine_floor() if floor is None else floor
    kept = [r for r in rows if keeps(r, limit)]
    return kept, len(rows) - len(kept)


def payload_block(removed: int, floor: float) -> dict[str, Any]:
    """The additive ``relevance_floor`` key of an explicit search payload."""
    return {"applied": True, "removed": int(removed), "floor": floor}
