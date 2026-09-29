# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""The one validity policy (Phase 2, session B; "Evidence and changing-fact
rules" #8, docs/plans/2026-09-27-memory-reasoning-scope.md): "One validity
policy, in one module, serves every reader. It covers both existing
mechanisms (topics.superseded_by and decisions.superseded_by) and normalizes
free-text topic status through a table; an unknown status is treated as
current."

Every function here is a pure function over already-fetched data — no DB
connection, no cursor, no network. A caller (embed.hybrid_search,
recall_prompt's local/budgeted lanes, activity.project_slice) is responsible
for gathering ``episode_counts``/``topic_meta`` however is cheapest on its
own leg (a single indexed query, or reusing a metadata pass it already ran)
and handing them to ``annotate``.

State is one of five values everywhere in this module: current, mixed,
superseded, retracted, unknown. "unknown" means the caller had no validity
data to offer (an older replica, a lane that skipped its counts query under
deadline pressure) — never confused with "current" (checked, found nothing
superseded).
"""
from __future__ import annotations

from typing import Any

# Free-text topic status normalisation (non-blocking finding 2, docs/research/
# hindsight-plan-review-2026-09-28.md: 17 distinct values in production, not
# an equality test). Not-current: superseded, retired, abandoned — everything
# else, including unknown text and empty, is current. recency.DERANKED_STATUSES
# is an alias of this set (same members) so the de-rank and the validity
# policy can never drift apart.
NOT_CURRENT_TOPIC_STATUSES = frozenset({"superseded", "retired", "abandoned"})

# episode_state's five states, plus "unknown" for a caller with no counts.
STATES = ("current", "mixed", "superseded", "retracted", "unknown")

# annotate() sets an episode row's `status` (the slot a row already has,
# never touched on a topic row) to this label when its state is not current.
_EPISODE_STATUS_LABEL = {
    "superseded": "superseded",
    "mixed": "partly superseded",
    "retracted": "retracted",
}

# revision_token()'s one-letter-per-state token for a non-current, non-unknown
# state row.
_STATE_TOKENS = {"superseded": "s", "mixed": "m", "retracted": "r"}

# is_historical()'s closed cue list — conservative by design: a prompt not
# matching one of these, and carrying no since/until filter, is never treated
# as historical, even if it plausibly reads that way to a person.
_HISTORY_CUES = (
    "previously", "originally", "used to", "history", "earlier",
    "back then", "as of", "at the time", "why did we",
)


def normalize_topic_status(status: str | None) -> str:
    """"current" | "not_current" for a topic's free-text status. Empty and
    unrecognized text are current — the normalisation table's whole point is
    that an unknown value is never silently treated as a de-rank signal."""
    s = (status or "").strip().lower()
    return "not_current" if s in NOT_CURRENT_TOPIC_STATUSES else "current"


def topic_state(status: str | None, superseded_by: Any) -> str:
    """"current" | "superseded" for one topic row. ``superseded_by`` (a slug)
    wins outright when set; otherwise the normalised ``status`` decides. A
    topic has no retracted/mixed concept (one row, one page) — those two
    states are reserved for episode_state."""
    if superseded_by:
        return "superseded"
    if normalize_topic_status(status) == "current":
        return "current"
    return "superseded"


def episode_state(current: int, superseded: int, retracted: int) -> str:
    """"current" | "mixed" | "superseded" | "retracted" from an episode's own
    decision-row counts. An episode with no decision rows (all zero) is
    current. Mixed means at least one current decision AND at least one
    superseded or retracted one — a mixed episode must never be discarded
    wholesale (scope: "Evidence and changing-fact rules"). With no current
    decision, retracted wins over superseded (state_of's same precedence:
    retracting is the stronger claim)."""
    current = int(current or 0)
    superseded = int(superseded or 0)
    retracted = int(retracted or 0)
    if current > 0 and (superseded > 0 or retracted > 0):
        return "mixed"
    if current > 0:
        return "current"
    if retracted > 0:
        return "retracted"
    if superseded > 0:
        return "superseded"
    return "current"


def annotate(
    rows: list[dict[str, Any]],
    episode_counts: dict[str, tuple[int, int, int]] | None = None,
    topic_meta: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Add ``validity`` to each episode/topic row; never touches any other
    kind. Rows are copied, not mutated in place.

    ``episode_counts`` maps ``str(episode_id) -> (current, superseded,
    retracted)``. ``None`` means the caller had no counts available at all
    (an older replica, a skipped budgeted-lane query) — every episode row
    gets ``state: "unknown"``. A non-``None`` dict with no entry for a given
    id means the caller DID check and found no decision rows — that row is
    "current" (episode_state's own rule), not "unknown".

    ``topic_meta`` maps ``str(slug) -> {"status": ..., "superseded_by":
    ...}``. ``None`` (or a slug missing from it) means no data — that topic
    row gets ``state: "unknown"``.

    Sets ``status`` on an EPISODE row only when its state is not current:
    "superseded", "partly superseded", or "retracted" — this is the ONLY
    place any of these functions writes back onto a row. A topic row's
    existing ``status`` field is only ever read here, never written.
    """
    if not rows:
        return rows
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        kind = item.get("kind")
        if kind == "episode":
            if episode_counts is None:
                item["validity"] = {
                    "state": "unknown", "current": None,
                    "superseded": None, "retracted": None,
                }
            else:
                eid = item.get("id")
                cur_n, sup_n, ret_n = episode_counts.get(str(eid), (0, 0, 0)) if eid is not None else (0, 0, 0)
                state = episode_state(cur_n, sup_n, ret_n)
                item["validity"] = {
                    "state": state, "current": cur_n,
                    "superseded": sup_n, "retracted": ret_n,
                }
                label = _EPISODE_STATUS_LABEL.get(state)
                if label:
                    item["status"] = label
        elif kind == "topic":
            meta = topic_meta.get(str(item.get("id"))) if topic_meta is not None else None
            if meta is None:
                item["validity"] = {"state": "unknown", "superseded_by": None}
            else:
                superseded_by = meta.get("superseded_by")
                state = topic_state(meta.get("status"), superseded_by)
                item["validity"] = {"state": state, "superseded_by": superseded_by}
        out.append(item)
    return out


def is_historical(query: str | None, since: Any = None, until: Any = None) -> bool:
    """True when a time filter is already present, or the query names one of
    a short closed set of history cues. Conservative: anything else, ``False``
    — a false negative (treating a historical question as current) only
    costs a de-rank that would otherwise have been skipped; a false positive
    silently disables ranking on an ordinary query."""
    if since or until:
        return True
    q = (query or "").strip().lower()
    if not q:
        return False
    return any(cue in q for cue in _HISTORY_CUES)


def apply_ranking(rows: list[dict[str, Any]], historical: bool = False) -> list[dict[str, Any]]:
    """When the ``validity_ranking`` switch is on and ``historical`` is
    False, multiply the score of episode rows whose state is superseded or
    retracted by ``recency.STATUS_DERANK`` and re-sort stably. Mixed episodes
    are never de-ranked (a mixed episode is still current work, just not
    exclusively). Topic rows are left to ``recency.apply_project_and_status``,
    which already de-ranks them — never applied twice.

    With the switch off (its default) or a historical query, ``rows`` comes
    back exactly as given — this is the one place the switch itself is
    checked, so no caller has to duplicate that check.
    """
    if not rows:
        return rows
    try:
        from khipu import features

        if not features.enabled("validity_ranking"):
            return rows
    except Exception:  # noqa: BLE001 — a switch-lookup failure must never rank differently
        return rows
    if historical:
        return rows

    from khipu.recency import STATUS_DERANK

    changed = False
    for row in rows:
        if row.get("kind") != "episode":
            continue
        validity = row.get("validity")
        if not isinstance(validity, dict) or validity.get("state") not in ("superseded", "retracted"):
            continue
        try:
            score = float(row.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        row["score"] = round(score * STATUS_DERANK, 6)
        changed = True
    if not changed:
        return rows
    return sorted(rows, key=lambda r: -(r.get("score") or 0.0))


def revision_token(row: dict[str, Any]) -> str:
    """Empty string for a current/unknown row with no explicit revision bump;
    otherwise a short, stable token so ``recall_prompt._hit_ids`` can key a
    dedup batch on more than kind+id.

    A row whose ``validity`` carries an explicit ``revision`` counter above 1
    (a row updated without necessarily changing state — e.g. new evidence
    attached to a still-current decision) always gets a non-empty token, the
    revision number itself; ``annotate`` does not populate this field today,
    but a future caller that tracks per-row revisions can. Absent that,
    the token is derived from ``state`` alone: one letter for a non-current,
    non-unknown state, empty otherwise.
    """
    validity = row.get("validity")
    if not isinstance(validity, dict):
        return ""
    revision = validity.get("revision")
    if revision is not None:
        try:
            revision = int(revision)
        except (TypeError, ValueError):
            revision = None
    if revision is not None and revision > 1:
        return str(revision)
    state = validity.get("state")
    if state in (None, "current", "unknown"):
        return ""
    return _STATE_TOKENS.get(state, "x")
