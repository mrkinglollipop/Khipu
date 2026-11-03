"""Nightly retention for the hub's receipt tables.

What reads each table decides how long a row must stay:

- ``owner_writes_applied``: a replay asks whether a queued write's id landed
  (``owner_writes._landed``). A queued write older than
  ``owner_writes.REPLAY_HORIZON_DAYS`` that the hub does not hold is refused
  for the owner to check instead of being sent, so a row older than that
  horizon can go. The window here is never shorter than the horizon plus a
  week, whatever ``receipt_keep_days`` says.
- ``ops_events``: doctor reads the newest event of a kind (and the newest ok
  one). Older rows go; the newest of each kind, and the newest ok of each
  kind, never do.

Left alone, on purpose: ``ledger_ops`` (an op id is bound to its write for
good, and a waiting ledger write has no age limit, so its binding is the only
thing that stops a retry writing twice) and ``commitment_tidy_batches`` and
``_items`` (an undo reads them whenever the owner asks, and the origin check
reads whether an item was ever tidied). ``fact_changes`` has its own step
(``provenance.prune_changes``).

Deletes run in small batches, each its own transaction, so no lock is held
long; a run stops after ``MAX_BATCHES`` per table and the next night goes on.
"""
from __future__ import annotations

from typing import Any

BATCH = 1000
MAX_BATCHES = 50
DEFAULT_KEEP_DAYS = 90

_OWNER_WRITES = (
    "DELETE FROM owner_writes_applied WHERE write_id IN ("
    "SELECT write_id FROM owner_writes_applied WHERE applied_at < now() - make_interval(days => %s) "
    "ORDER BY applied_at LIMIT %s)")
_OPS_EVENTS = (
    "DELETE FROM ops_events WHERE id IN ("
    "SELECT id FROM ops_events WHERE created_at < now() - make_interval(days => %s) "
    "AND id NOT IN (SELECT DISTINCT ON (kind) id FROM ops_events ORDER BY kind, created_at DESC, id DESC) "
    "AND id NOT IN (SELECT DISTINCT ON (kind) id FROM ops_events WHERE status = 'ok' "
    "ORDER BY kind, created_at DESC, id DESC) "
    "ORDER BY created_at LIMIT %s)")


def _has_table(cur, name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{name}",))
    return bool(cur.fetchone()[0])


def _delete_batches(conn, sql: str, days: int, out: dict[str, Any], key: str) -> None:
    for _ in range(MAX_BATCHES):
        with conn.cursor() as cur:
            cur.execute(sql, (int(days), BATCH))
            removed = int(cur.rowcount or 0)
        conn.commit()
        out[key] += removed
        if removed < BATCH:
            return
    out["remaining_possible"] = True


def prune(conn, *, keep_days: int = DEFAULT_KEEP_DAYS) -> dict[str, Any]:
    """Delete receipts older than the window (0 keeps everything). Commits."""
    from khipu import owner_writes

    out: dict[str, Any] = {"ok": True, "owner_writes_applied": 0, "ops_events": 0, "keep_days": int(keep_days)}
    if keep_days <= 0:
        out["disabled"] = True
        return out
    receipts_days = max(int(keep_days), owner_writes.REPLAY_HORIZON_DAYS + 7)
    out["owner_writes_keep_days"] = receipts_days
    for table, sql, days in (("owner_writes_applied", _OWNER_WRITES, receipts_days),
                             ("ops_events", _OPS_EVENTS, int(keep_days))):
        with conn.cursor() as cur:
            present = _has_table(cur, table)
        conn.rollback()
        if not present:
            out.setdefault("skipped", []).append(table)
            continue
        _delete_batches(conn, sql, days, out, table)
    if out.get("remaining_possible"):
        out["deferred"] = "receipt prune: more old rows remain, the next night goes on"
    return out
