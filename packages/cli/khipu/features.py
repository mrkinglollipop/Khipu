# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""Khipu's switch registry and capability signal (Phase 0, session B).

A closed set of named switches for every optional behavior the memory-
reasoning work adds (ranking changes, candidate generation, extraction
prompts, model use — "Engineering rules added in review" § Switches,
docs/plans/2026-09-27-memory-reasoning-scope.md): each defaults off, and
with every switch off, existing outputs are unchanged except for added keys.

Precedence mirrors ``khipu.config``'s ``FLOAT_SETTINGS``/``LIST_SETTINGS``:
environment (``KHIPU_FEATURE_<NAME>``) wins, then ``features`` in
config.json, then the default (False). Unlike those, ``enabled()`` raises
``KeyError`` for a name outside ``FEATURES`` rather than silently returning a
default — a typo in calling code (``features.enabled("desicion_details")``)
must never read as "the switch is merely off".

Nothing here imports anything heavy (``khipu.config`` is itself
stdlib-only), so it is cheap to call from a hook on every prompt. It never
raises except the documented ``KeyError``.
"""
from __future__ import annotations

import os

from khipu import __version__

# Name -> one-line description. Closed set: enabled()/set_enabled() reject
# anything else outright; states() reports an unknown name found in config.json
# or the environment instead of silently accepting or dropping it.
FEATURES: dict[str, str] = {
    "decision_details": "Surface extended decision fields — rationale, source type, event/recorded/effective time, revisions.",
    "auto_supersede": "Apply a capture-time supersession candidate automatically instead of leaving it for confirmation.",
    "validity_ranking": "Have recall honor validity/supersession state instead of ranking every row as equally current.",
    "graph_candidates": "Let bounded graph traversal contribute candidate results to recall.",
    "time_interpretation": "Parse natural-language time in a query and use it to filter/rank results.",
    "rerank": "Run an independently switchable reranking stage over candidate results.",
    "briefs": "Generate source-backed incremental topic briefs instead of the legacy topic-page pipeline.",
    "reflect": "Run an optional cited-reflection step over dependable evidence.",
}

_ENV_PREFIX = "KHIPU_FEATURE_"
_TRUE_WORDS = {"1", "true", "on", "yes"}
_FALSE_WORDS = {"0", "false", "off", "no"}

# Stable capability strings this build supports regardless of any switch
# above (the scope's "capability signal": "a real server version and a
# `capabilities` list ... so a client can test for a feature instead of
# inferring it"). Adding one here is additive; never remove or rename one a
# shipped client may already be testing for.
_CAPABILITIES = (
    "search.hybrid",
    "search.filters",
    "prior_work.items",
    "prior_work.budget",
    "owed",
    "forget",
    "forget.cascade",
    "decisions.tools",
    "decisions.evidence",
    "tools.annotations",
    "launchers.read_only",
    # Phase 2, session B.
    "validity.markers",
    "prior_work.outcome",
    "prior_work.project",
    "replica.schema_version",
    # Phase 2, session C.
    "decisions.details",
    "decisions.detection",
    # Phase 3, session A.
    "search.time_interpretation",
    "search.graph_candidates",
)


def parse_bool(raw: str) -> bool | None:
    """1/true/on/yes and 0/false/off/no, case-insensitive; anything else is
    None — ignored by ``enabled()``'s environment leg, rejected by the CLI."""
    v = (raw or "").strip().lower()
    if v in _TRUE_WORDS:
        return True
    if v in _FALSE_WORDS:
        return False
    return None


def _check_known(name: str) -> None:
    if name not in FEATURES:
        raise KeyError(f"unknown feature {name!r}; known: {sorted(FEATURES)}")


def enabled(name: str) -> bool:
    """Environment, then config.json ``features``, then False (default off).
    Raises ``KeyError`` for a name outside ``FEATURES``."""
    _check_known(name)
    raw = os.environ.get(_ENV_PREFIX + name.upper())
    if raw is not None:
        parsed = parse_bool(raw)
        if parsed is not None:
            return parsed
    from khipu.config import load_config

    stored = (load_config().get("features") or {}).get(name)
    return stored if isinstance(stored, bool) else False


def _source(name: str) -> str:
    raw = os.environ.get(_ENV_PREFIX + name.upper())
    if raw is not None and parse_bool(raw) is not None:
        return "env"
    from khipu.config import load_config

    stored = (load_config().get("features") or {}).get(name)
    return "file" if isinstance(stored, bool) else "default"


def set_enabled(name: str, value: bool):
    """Persist ``name`` to config.json's ``features`` object. Raises
    ``KeyError`` for a name outside ``FEATURES``."""
    _check_known(name)
    from khipu.config import load_config, save_config

    data = load_config()
    feats = data.get("features")
    feats = dict(feats) if isinstance(feats, dict) else {}
    feats[name] = bool(value)
    data["features"] = feats
    return save_config(data)


def _unknown_names() -> list[str]:
    from khipu.config import load_config

    cfg = load_config().get("features")
    cfg = cfg if isinstance(cfg, dict) else {}
    found = {k for k in cfg if k not in FEATURES}
    for env_name in os.environ:
        if not env_name.startswith(_ENV_PREFIX):
            continue
        candidate = env_name[len(_ENV_PREFIX):].lower()
        if candidate not in FEATURES:
            found.add(candidate)
    return sorted(found)


def states() -> dict:
    """Every known switch with its resolved value and where that value came
    from, plus any unknown name found in config.json or the environment —
    reported, never silently accepted or dropped."""
    return {
        "features": {name: {"enabled": enabled(name), "source": _source(name)}
                     for name in sorted(FEATURES)},
        "unknown": _unknown_names(),
    }


def capabilities() -> list[str]:
    """Stable capability strings this build supports, regardless of switches."""
    return sorted(_CAPABILITIES)


def contract() -> dict:
    """The capability signal carried on MCP ``initialize`` and in
    ``khipu_status`` (scope: "a real server version and a `capabilities`
    list ... so a client can test for a feature instead of inferring it")."""
    return {
        "version": __version__,
        "capabilities": capabilities(),
        "features": {name: enabled(name) for name in sorted(FEATURES)},
    }
