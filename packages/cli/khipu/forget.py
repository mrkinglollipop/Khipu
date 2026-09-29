"""Complete forgetting (2026-09-05; extended Phase 2, session A).

``khipu episode forget ID`` used to tombstone the row and drop its vectors
and stop there: the commitments it opened stayed open, and its line in the
legacy ``episodes.jsonl`` stayed on disk for the nightly reconcile to read.
This module is the one place every forget goes through (the CLI, the recall
probe's cleanup, the ``khipu_forget`` MCP tool) so all of it happens:

  1. episodes.deleted_at = now()            (search, activity, doctor skip it)
  2. its episode vectors                    (memory_embeddings)
  3. commitments it opened → closed, close_reason 'forgotten', plus their vectors
  4. its decision rows → retracted, retract_reason 'forgotten' (migration 0024)
  5. its deliverable rows → deleted
  6. its line in the legacy file, after a backup copy of the file
  7. the local sqlite replica, if one exists (deleted_at + its embeddings)
  8. every brief citing it → stale (migration 0025)

B3 in docs/research/hindsight-plan-review-2026-09-28.md: forgetting used to
stop at step 3 — a forgotten episode's decisions and deliverables stayed
readable through every other reader. Steps 4-5 close that; every reader
(khipu.decisions, khipu.deliverables) also independently excludes a
forgotten episode's rows, so this cascade is belt-and-suspenders cleanup,
not the only thing standing between a forgotten row and a reader.

Topic nodes and edges in the graph are shared by every episode that mentions
the topic and carry no episode provenance, so they are left alone and the
result says so.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any

LOCAL_HARNESSES = ("claude_code", "cursor", "codex", "aegis")


def _deliverables_ready(cur) -> bool:
    """Same check as ``khipu.deliverables._deliverables_ready``, duplicated
    (not imported) — this module already stands alone from every writer it
    cascades into, and a readiness probe is one line."""
    try:
        from khipu.db import has_columns

        return has_columns(cur, "deliverables", "id", "project", "kind")
    except Exception:  # noqa: BLE001 — introspection is best-effort
        return False


def _decision_evidence_ready(cur) -> bool:
    """Same check as ``khipu.decisions._evidence_ready`` (migration 0024),
    duplicated — see ``_deliverables_ready`` above."""
    try:
        from khipu.db import has_columns

        return has_columns(cur, "decisions", "retracted_at", "retract_reason")
    except Exception:  # noqa: BLE001 — introspection is best-effort
        return False


def _briefs_ready(cur) -> bool:
    """Same check as ``khipu.briefs._ready`` (migration 0025), duplicated —
    see ``_deliverables_ready`` above."""
    try:
        from khipu.db import has_columns

        return has_columns(cur, "briefs", "id", "source_episode_ids", "state", "superseded_at")
    except Exception:  # noqa: BLE001 — introspection is best-effort
        return False


def forget_episode(cur, episode_id: int) -> dict[str, Any]:
    """The hub half. Caller commits. Returns the counts and the (ts, summary
    md5) identity the legacy file uses, or ``{"ok": False}`` when unknown."""
    cur.execute("SELECT ts, summary, session_id FROM episodes WHERE id = %s", (episode_id,))
    row = cur.fetchone()
    if row is None:
        return {"ok": False, "id": episode_id, "error": f"no such episode: {episode_id}"}
    ts, summary, session_id = row
    cur.execute(
        "UPDATE episodes SET deleted_at = now() WHERE id = %s AND deleted_at IS NULL",
        (episode_id,),
    )
    soft_deleted = cur.rowcount > 0
    cur.execute(
        "DELETE FROM memory_embeddings WHERE kind = 'episode' AND ref = %s",
        (str(episode_id),),
    )
    embeddings_removed = cur.rowcount
    cur.execute(
        "DELETE FROM memory_embeddings WHERE kind = 'commitment' AND ref IN "
        "(SELECT id::text FROM commitments WHERE opened_episode = %s)",
        (episode_id,),
    )
    commitment_vectors_removed = cur.rowcount
    cur.execute(
        "UPDATE commitments SET status = 'closed', closed_at = now(), "
        "close_reason = 'forgotten' WHERE opened_episode = %s AND status IN ('open', 'stale')",
        (episode_id,),
    )
    commitments_closed = cur.rowcount

    deliverables_removed = 0
    if _deliverables_ready(cur):
        cur.execute("DELETE FROM deliverables WHERE episode_id = %s", (episode_id,))
        deliverables_removed = cur.rowcount

    decisions_retracted = 0
    if _decision_evidence_ready(cur):
        cur.execute(
            "UPDATE decisions SET retracted_at = now(), retract_reason = 'forgotten' "
            "WHERE episode_id = %s AND retracted_at IS NULL",
            (episode_id,),
        )
        decisions_retracted = cur.rowcount

    briefs_staled = 0
    if _briefs_ready(cur):
        cur.execute(
            "UPDATE briefs SET state = 'stale' WHERE state = 'current' AND superseded_at IS NULL "
            "AND source_episode_ids && %s::bigint[]",
            ([episode_id],),
        )
        briefs_staled = cur.rowcount

    ts_iso = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
    return {
        "ok": True,
        "id": episode_id,
        "session_id": session_id,
        "soft_deleted": soft_deleted,
        "embeddings_removed": embeddings_removed,
        "commitments_closed": commitments_closed,
        "commitment_vectors_removed": commitment_vectors_removed,
        "deliverables_removed": deliverables_removed,
        "decisions_retracted": decisions_retracted,
        "briefs_staled": briefs_staled,
        "graph": "topic nodes are shared across episodes; none removed",
        "identity": {"ts": ts_iso, "summary_md5": hashlib.md5((summary or "").encode("utf-8")).hexdigest()},
    }


def _line_matches(line: str, ts_iso: str, summary_md5: str) -> bool:
    try:
        obj = json.loads(line)
    except ValueError:
        return False
    if not isinstance(obj, dict):
        return False
    if hashlib.md5((obj.get("summary") or "").encode("utf-8")).hexdigest() != summary_md5:
        return False
    raw_ts = str(obj.get("ts") or "")
    # The file stores what the hook minted (2026-09-05T12:20:17Z); PG returns
    # an aware datetime. Compare on the first 19 characters (to the second)
    # after normalising the zone suffix.
    a = raw_ts.replace("Z", "+00:00")[:19]
    b = ts_iso.replace("Z", "+00:00")[:19]
    return a == b


def forget_in_legacy_file(memory_root: Path | None, ts_iso: str, summary_md5: str) -> dict[str, Any]:
    """Remove the episode's line from ``memory_root/episodes.jsonl``. The
    file is copied to ``.khipu-forget-backups/`` first; a rewrite that
    would remove nothing leaves the file untouched and makes no backup."""
    if memory_root is None:
        return {"file": None, "removed": 0, "reason": "memory_root not configured"}
    path = Path(memory_root) / "episodes.jsonl"
    if not path.is_file():
        return {"file": str(path), "removed": 0, "reason": "no legacy file"}
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError as exc:
        return {"file": str(path), "removed": 0, "reason": f"unreadable: {exc}"}
    keep = [ln for ln in lines if not _line_matches(ln, ts_iso, summary_md5)]
    removed = len(lines) - len(keep)
    if not removed:
        return {"file": str(path), "removed": 0}
    backup_dir = Path(memory_root) / ".khipu-forget-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = backup_dir / f"episodes.jsonl.{stamp}"
    shutil.copy2(path, backup)
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(keep), encoding="utf-8")
    tmp.replace(path)
    return {"file": str(path), "removed": removed, "backup": str(backup)}


def forget_everywhere(episode_id: int, *, memory_root: Path | None = None) -> dict[str, Any]:
    """Hub + legacy file. Opens its own connection and commits."""
    from khipu.db import connect

    with connect() as conn:
        with conn.cursor() as cur:
            out = forget_episode(cur, episode_id)
        conn.commit()
    if not out.get("ok"):
        return out
    try:
        from khipu.hub_snapshot import forget_episode_in_snapshot

        out["snapshot"] = forget_episode_in_snapshot(episode_id)
    except Exception as exc:  # noqa: BLE001 — the hub write already succeeded
        out["snapshot"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if memory_root is None:
        try:
            from khipu.config import path_setting

            memory_root = path_setting("memory_root")
        except Exception:  # noqa: BLE001
            memory_root = None
    ident = out.get("identity") or {}
    out["legacy_file"] = forget_in_legacy_file(memory_root, ident.get("ts", ""), ident.get("summary_md5", ""))
    return out
