"""Fact provenance: every stored wording value has one claimant.

Scope: docs/plans/2026-10-23-fact-provenance.md. A commitment's, decision's
or deliverable's wording is a pure function of its live claims (rows in
``fact_claims``, migration 0047). This module is the only way Python code
records a claim or recomputes a fact:

* :func:`record_claim` adds one claim (a capture episode's, the owner's).
  A replayed capture writes nothing new.
* :func:`project` recomputes one fact from its claims by calling the hub's
  ``khipu_project_fact()``, the single implementation the migration's own
  initial projection also used, and records the fact in ``fact_changes`` for
  this transaction. The hub refuses, at COMMIT, any wording change to a fact
  that was not projected in the same transaction (SQLSTATE 08K01).

Capture dedup, forget and backfill change wording only by adding or deleting
claims and then calling :func:`project`; nothing re-reads raw captures to
work out who said what.

:func:`ready` is the positive-only readiness check (section 5.1): a hub
without the table answers False every time it is asked, so a long-lived
process sees the migration on its next transaction; once True it stays True.
"""
from __future__ import annotations

from typing import Any

KINDS = ("commitment", "decision", "deliverable")
FIELDS: dict[str, tuple[str, ...]] = {
    "commitment": ("text", "restatement", "pr", "pr_ref"),
    "decision": ("text", "rationale", "evidence"),
    "deliverable": ("title", "support"),
}
CLAIMANTS = ("episode", "owner", "legacy_unproven")
# What the hub raises for a writer that bypasses the projector or forgets a
# claimed episode. Class 08: psycopg raises it as plain OperationalError.
OLD_WRITER_SQLSTATE = "08K01"
OLD_WRITER_MESSAGE = "khipu: this writer predates fact provenance; upgrade it"
PROJECTION_RESULTS = ("projected", "retired", "deleted", "missing")

_READY = False


def ready(cur) -> bool:
    """Whether the hub has migration 0047's ``fact_claims``. Only a yes is
    cached (section 5.1)."""
    global _READY
    if _READY:
        return True
    cur.execute("SELECT to_regclass('public.fact_claims') IS NOT NULL")
    row = cur.fetchone()
    # Only a real boolean yes counts: a cursor that answers anything else
    # (a test double, a stale result) is not a hub with the table.
    _READY = bool(row) and row[0] is True
    return _READY


def _reset_ready_cache() -> None:
    """Tests: forget a cached yes (a fresh database per test)."""
    global _READY
    _READY = False


def _check(kind: str, field: str | None = None, claimant: str | None = None) -> None:
    if kind not in KINDS:
        raise ValueError(f"unknown fact kind {kind!r}")
    if field is not None and field not in FIELDS[kind]:
        raise ValueError(f"{kind} has no claim field {field!r}")
    if claimant is not None and claimant not in CLAIMANTS:
        raise ValueError(f"unknown claimant {claimant!r}")


def record_claim(cur, kind: str, fact_id: int, field: str, value: Any, *,
                 episode_id: int | None = None, claimant: str | None = None) -> bool:
    """Record that ``episode_id`` (or the owner, with ``claimant="owner"``)
    supplied ``value`` for ``field`` of fact ``(kind, fact_id)``. Returns
    whether a new claim was written (False for a replay).

    ``claimant`` defaults to ``"episode"`` when ``episode_id`` is given and
    to ``"owner"`` otherwise. A ``None`` value records nothing. Values are
    stored as text: a PR number as its decimal digits, decision evidence as
    its JSON text. The hub refuses an episode claim for a forgotten or
    missing episode (check_violation) after taking FOR SHARE on its row, so
    call this with the episode lock order: episode rows first, then facts.
    The caller commits, and must :func:`project` the fact in the same
    transaction."""
    if claimant is None:
        claimant = "episode" if episode_id is not None else "owner"
    _check(kind, field, claimant)
    if (claimant == "episode") != (episode_id is not None):
        raise ValueError("an episode claim needs episode_id, and only an episode claim has one")
    if value is None:
        return False
    if field == "evidence" and not isinstance(value, str):
        import json

        value = json.dumps(value)
    cur.execute(
        "INSERT INTO fact_claims (kind, fact_id, field, value, claimant, episode_id) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (kind, int(fact_id), field, str(value), claimant, episode_id),
    )
    return cur.rowcount > 0


def project(cur, kind: str, fact_id: int) -> str:
    """Recompute fact ``(kind, fact_id)`` from its live claims (section 3.2)
    and record it in ``fact_changes`` for this transaction. Returns
    ``"projected"``, ``"retired"`` (no live or owner claim: closed or
    retracted as ``forgotten``), ``"deleted"`` (a retired deliverable) or
    ``"missing"`` (no such row). Locks the fact row FOR UPDATE."""
    _check(kind)
    cur.execute("SELECT khipu_project_fact(%s, %s)", (kind, int(fact_id)))
    return str(cur.fetchone()[0])


def claims(cur, kind: str, fact_id: int) -> list[dict[str, Any]]:
    """Every claim on one fact, oldest first. Hub only: legacy_unproven
    values are listed here and must never be shown to a reader."""
    _check(kind)
    cur.execute(
        "SELECT field, value, claimant, episode_id FROM fact_claims "
        "WHERE kind = %s AND fact_id = %s ORDER BY created_at, id",
        (kind, int(fact_id)),
    )
    return [dict(zip(("field", "value", "claimant", "episode_id"), r)) for r in cur.fetchall()]


def is_old_writer_refusal(exc: BaseException) -> bool:
    """Whether ``exc`` is the hub's 08K01 refusal of a writer that bypassed
    the projector (or forgot a claimed episode)."""
    return getattr(getattr(exc, "diag", None), "sqlstate", None) == OLD_WRITER_SQLSTATE \
        or getattr(exc, "sqlstate", None) == OLD_WRITER_SQLSTATE
