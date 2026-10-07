# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""The one absolute relevance gate, behind the ``relevance_floor`` switch.

``recall_prompt._apply_score_floor`` is relative to the top row, so a prompt
about something memory has never seen still keeps its three nearest
neighbours: nearest-neighbour search always returns something. This module
decides whether a fused result list has any evidence at all, and abstains
when it has none.

A row is evidence when

- its raw cosine similarity is a number at or above the floor, or
- it names at least ``need`` of the query's content tokens as whole words,
  where ``need = min(n, max(2, ceil(n / 2)))`` for ``n`` content tokens: one
  common word never counts as coverage of a multi-word query.

A row with no cosine and too few keyword hits, or with neither field (a
graph candidate), is not evidence. The gate is all or nothing: when at least
one row is evidence the list comes back unchanged (same rows, same order);
when none is, it comes back empty. It never removes individual rows. With no
content tokens it does not run.

One function serves every query-driven reader (the local prompt lane, the
budgeted hub lane, explicit hybrid/semantic search), the way ``validity``
serves every reader, so the three cannot drift apart. Literal mode never
calls it. Pure functions over already-fetched rows: no database, no network.
"""
from __future__ import annotations

import math
import re
from typing import Any, Sequence

# Measured on the production embedding profile: the best match for 20 prompts
# on subjects memory has never seen scored 0.57 to 0.64, and the best match
# for 149 real prompts scored 0.67 to 0.82. The floor sits between the two
# ranges. Re-measure if the active profile changes; overridable by the
# ``relevance.cosine_floor`` config key.
COSINE_FLOOR = 0.65


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


def cosine_floor_status() -> dict[str, Any]:
    """The effective floor, where it came from (``file`` when config.json holds
    a usable number, else ``default``) and the built-in default, for
    ``khipu config``."""
    value = cosine_floor()
    try:
        from khipu.config import load_config

        section = load_config().get("relevance")
        raw = section.get("cosine_floor") if isinstance(section, dict) else None
    except Exception:  # noqa: BLE001
        raw = None
    stored = (not isinstance(raw, bool) and isinstance(raw, (int, float))
              and 0.0 < float(raw) <= 1.0)
    return {"value": value, "source": "file" if stored else "default",
            "default": COSINE_FLOOR}


def set_cosine_floor(value: "float | None"):
    """Write ``relevance.cosine_floor`` to config.json where ``cosine_floor()``
    reads it; ``None`` removes it (and an emptied ``relevance`` object) so the
    default applies. Raises ``ValueError`` outside (0, 1]."""
    from khipu.config import load_config, save_config

    data = load_config()
    section = data.get("relevance")
    section = dict(section) if isinstance(section, dict) else {}
    if value is None:
        section.pop("cosine_floor", None)
    else:
        if isinstance(value, bool) or not (0.0 < float(value) <= 1.0):
            raise ValueError(f"relevance.cosine_floor must be a number above 0 and at most 1, got {value!r}")
        section["cosine_floor"] = float(value)
    if section:
        data["relevance"] = section
    else:
        data.pop("relevance", None)
    return save_config(data)


# A profile id as it appears in config.json keys: model@dim, so it may carry
# '@', '-', '.', ':' and '/' (voyage-3.5@1024, org/model:tag@768).
PROFILE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}$")
BY_PROFILE_KEY = "relevance.cosine_floor_by_profile."


def _valid_floor(raw: Any) -> bool:
    return (not isinstance(raw, bool) and isinstance(raw, (int, float))
            and math.isfinite(float(raw)) and 0.0 < float(raw) <= 1.0)


def cosine_floor_by_profile() -> dict[str, float]:
    """``relevance.cosine_floor_by_profile`` from config.json: profile id ->
    floor, keeping only entries whose key is a plausible id and whose value is
    a number in (0, 1]. Anything else is ignored, never raised."""
    try:
        from khipu.config import load_config

        section = load_config().get("relevance")
        raw = section.get("cosine_floor_by_profile") if isinstance(section, dict) else None
    except Exception:  # noqa: BLE001 — config trouble means no overrides
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: float(v) for k, v in raw.items()
            if isinstance(k, str) and PROFILE_KEY_RE.match(k) and _valid_floor(v)}


def cosine_floor_for(profile: str | None, *, by_profile: dict[str, float] | None = None) -> float:
    """The floor for rows scored by ``profile``'s embedding model: its own
    entry when set, else the global ``cosine_floor()``. Raw cosine is only
    comparable within one model, so a profile whose scores sit lower (or
    higher) than the one the global floor was measured on gets its own."""
    table = cosine_floor_by_profile() if by_profile is None else by_profile
    if profile and profile in table:
        return table[profile]
    return cosine_floor()


def set_cosine_floor_for_profile(profile: str, value: "float | None"):
    """Write (or, with ``None``, remove) one profile's floor under
    ``relevance.cosine_floor_by_profile`` in config.json. ``ValueError`` for
    an id that is not a plausible profile id or a value outside (0, 1]."""
    from khipu.config import load_config, save_config

    profile = (profile or "").strip()
    if not PROFILE_KEY_RE.match(profile):
        raise ValueError(f"not a valid embedding profile id: {profile!r}")
    if value is not None and not _valid_floor(value):
        raise ValueError(
            f"{BY_PROFILE_KEY}{profile} must be a number above 0 and at most 1, got {value!r}")
    data = load_config()
    section = data.get("relevance")
    section = dict(section) if isinstance(section, dict) else {}
    table = section.get("cosine_floor_by_profile")
    table = dict(table) if isinstance(table, dict) else {}
    if value is None:
        table.pop(profile, None)
    else:
        table[profile] = float(value)
    if table:
        section["cosine_floor_by_profile"] = table
    else:
        section.pop("cosine_floor_by_profile", None)
    if section:
        data["relevance"] = section
    else:
        data.pop("relevance", None)
    return save_config(data)


def need(token_count: int) -> int:
    """Whole-token keyword hits a row needs to be evidence by coverage."""
    n = int(token_count)
    return min(n, max(2, math.ceil(n / 2)))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def is_evidence(
    row: dict[str, Any], floor: float, needed: int,
    by_profile: dict[str, float] | None = None,
) -> bool:
    """The evidence test for one row. A row that names the embedding profile
    its cosine came from (a library row) is held to that profile's floor in
    ``by_profile`` when it has one, else to ``floor``."""
    cosine = _number(row.get("cosine"))
    if by_profile:
        floor = by_profile.get(row.get("profile") or "", floor)
    if cosine is not None and cosine >= floor:
        return True
    hits = _number(row.get("lexical_hits"))
    return hits is not None and hits >= needed


def gate(
    rows: Sequence[dict[str, Any]], token_count: int, *, floor: float | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """``(rows, info)``: ``rows`` unchanged when any row is evidence, empty
    when none is. ``info`` is the payload block, or None when the gate did not
    run (no content tokens)."""
    if token_count <= 0:
        return list(rows), None
    limit = cosine_floor() if floor is None else floor
    table = cosine_floor_by_profile() if floor is None else None
    needed = need(token_count)
    evidence = sum(1 for r in rows if is_evidence(r, limit, needed, table))
    abstained = evidence == 0
    info = {"applied": True, "abstained": abstained, "evidence_rows": evidence,
            "floor": limit, "need": needed}
    return ([] if abstained else list(rows)), info
