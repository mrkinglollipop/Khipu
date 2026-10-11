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
  6. its lines in the legacy file and retained forget backups
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
    legacy_identities = [{
        "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
        "summary_md5": hashlib.md5((summary or "").encode("utf-8")).hexdigest(),
    }]
    has_merged_sources = False
    # An edited summary can still have an original legacy identity in raw.
    # For merged episodes, raw describes a retained source, not an identity
    # that forget can safely erase without a capture-identity contract.
    try:
        cur.execute("SELECT raw FROM episodes WHERE id = %s", (episode_id,))
        raw_row = cur.fetchone()
        raw = raw_row[0] if isinstance(raw_row, tuple) and len(raw_row) == 1 else None
        if isinstance(raw, dict):
            has_merged_sources = bool(raw.get("merged_from"))
            original_ts = raw.get("ts")
            original_summary = raw.get("summary")
            if (not has_merged_sources and isinstance(original_ts, str)
                    and isinstance(original_summary, str) and original_summary):
                original = {
                    "ts": original_ts,
                    "summary_md5": hashlib.md5(original_summary.encode("utf-8")).hexdigest(),
                }
                if any(original[key] != legacy_identities[0][key] for key in ("ts", "summary_md5")):
                    legacy_identities.append(original)
    except Exception:  # noqa: BLE001 — old schema/driver must still forget the hub row
        pass
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
        "identity": legacy_identities[0],
        "legacy_identities": legacy_identities,
        "has_merged_sources": has_merged_sources,
    }


def _line_matches(line: str, ts_iso: str, summary_md5: str) -> bool:
    try:
        obj = json.loads(line)
    except ValueError:
        return False
    if not isinstance(obj, dict):
        return False
    summary = obj.get("summary") or ""
    if not isinstance(summary, str) or hashlib.md5(summary.encode("utf-8")).hexdigest() != summary_md5:
        return False
    raw_ts = str(obj.get("ts") or "")
    # The file stores what the hook minted (2026-09-05T12:20:17Z); PG returns
    # an aware datetime. Compare on the first 19 characters (to the second)
    # after normalising the zone suffix.
    a = raw_ts.replace("Z", "+00:00")[:19]
    b = ts_iso.replace("Z", "+00:00")[:19]
    return a == b


def _has_unidentified_records(lines: list[str]) -> bool:
    for line in lines:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            return True
        if not isinstance(obj, dict) or not isinstance(obj.get("summary"), str) or not isinstance(obj.get("ts"), str):
            return True
    return False


def _scrub_retained_backups(memory_root: Path, ts_iso: str, summary_md5: str) -> dict[str, Any]:
    """Remove a forgotten line from every retained legacy backup.

    A backup made while forgetting one of two identities can contain the
    other; each identity pass therefore scrubs all old copies as well.
    """
    backup_dir = memory_root / ".khipu-forget-backups"
    if not backup_dir.is_dir():
        return {"ok": True, "removed": 0, "remaining": []}
    removed = 0
    remaining: list[str] = []
    for backup in backup_dir.glob("episodes.jsonl.*"):
        if not backup.is_file():
            continue
        try:
            lines = backup.read_text(encoding="utf-8").splitlines(keepends=True)
            keep = [line for line in lines if not _line_matches(line, ts_iso, summary_md5)]
            if _has_unidentified_records(keep):
                remaining.append(f"{backup}: unidentified legacy records")
            if len(keep) == len(lines):
                continue
            from khipu.paths import write_sensitive_bytes

            tmp = backup.with_name(f".{backup.name}.tmp")
            write_sensitive_bytes(tmp, "".join(keep).encode("utf-8"))
            tmp.replace(backup)
            removed += len(lines) - len(keep)
        except (OSError, UnicodeError) as exc:
            remaining.append(f"{backup}: {type(exc).__name__}")
    return {"ok": not remaining, "removed": removed, "remaining": remaining}


def forget_in_legacy_file(memory_root: Path | None, ts_iso: str, summary_md5: str) -> dict[str, Any]:
    """Remove matching lines from the legacy file and retained backups.
    A rewrite preserves a backup containing only the remaining records."""
    identity = {"ts": ts_iso, "summary_md5": summary_md5}
    if memory_root is None:
        return {"file": None, "removed": 0, "reason": "memory_root not configured", "identity": identity,
                "backups": {"ok": True, "removed": 0, "remaining": []}}
    path = Path(memory_root) / "episodes.jsonl"
    backups = _scrub_retained_backups(Path(memory_root), ts_iso, summary_md5)
    if not path.is_file():
        return {"file": str(path), "removed": 0, "reason": "no legacy file", "identity": identity,
                "backups": backups}
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeError) as exc:
        return {"file": str(path), "removed": 0, "reason": f"unreadable: {exc}", "identity": identity,
                "backups": backups}
    keep = [ln for ln in lines if not _line_matches(ln, ts_iso, summary_md5)]
    unidentified = _has_unidentified_records(keep)
    reason = {"reason": "unreadable legacy identities remain"} if unidentified else {}
    removed = len(lines) - len(keep)
    if not removed:
        return {"file": str(path), "removed": 0, "identity": identity, "backups": backups, **reason}
    backup_dir = Path(memory_root) / ".khipu-forget-backups"
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"file": str(path), "removed": 0, "reason": f"backup unavailable: {exc}", "identity": identity,
                "backups": {"ok": False, "removed": 0, "remaining": [str(backup_dir)]}}
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = backup_dir / f"episodes.jsonl.{stamp}"
    # A retained backup may never retain the line just forgotten. It exists to
    # protect the remaining file, not to create a new plaintext copy of data
    # the caller asked to erase.
    from khipu.paths import write_sensitive_bytes

    try:
        write_sensitive_bytes(backup, "".join(keep).encode("utf-8"))
        tmp = path.with_suffix(".jsonl.tmp")
        write_sensitive_bytes(tmp, "".join(keep).encode("utf-8"))
        tmp.replace(path)
        backups = _scrub_retained_backups(Path(memory_root), ts_iso, summary_md5)
    except (OSError, UnicodeError) as exc:
        return {"file": str(path), "removed": 0, "reason": f"rewrite failed: {exc}", "identity": identity,
                "backups": {"ok": False, "removed": 0, "remaining": [str(path)]}}
    return {"file": str(path), "removed": removed, "backup": str(backup), "identity": identity,
            "backups": backups, **reason}


def _outbox_remainder(session_id: str | None, *, has_merged_sources: bool = False) -> dict[str, Any]:
    """Report retained jobs without inferring capture identity or changing files."""
    remaining: list[str] = []
    reason = "outbox: not cleaned (capture identity needed)"
    if has_merged_sources:
        return {"ok": False, "removed": 0, "remaining": [reason]}
    try:
        from khipu.outbox import _locked_outbox, outbox_dir, dead_dir, quarantine_dir

        # One lock across all three directories: retry_dead() and the drain move
        # jobs between them under the same lock, so a job cannot slip past the
        # scan mid-move.
        with _locked_outbox():
            for directory in (outbox_dir(), dead_dir(), quarantine_dir()):
                try:
                    paths = list(directory.iterdir())
                except FileNotFoundError:
                    continue
                for path in paths:
                    if path.suffix != ".json" or not path.is_file():
                        continue
                    try:
                        envelope = json.loads(path.read_bytes())
                        payload = envelope.get("payload") if isinstance(envelope, dict) else None
                        if not isinstance(payload, dict):
                            raise ValueError("job payload is not an object")
                    except (OSError, ValueError, TypeError) as exc:
                        remaining.append(f"{reason}; {path}: {type(exc).__name__}")
                        continue
                    # Without a session id there is no way to tell this job from
                    # the forgotten episode's capture, so any retained job counts.
                    if not session_id or payload.get("session_id") == session_id:
                        remaining.append(reason)
                        return {"ok": False, "removed": 0, "remaining": remaining}
    except Exception as exc:  # noqa: BLE001 — surface an incomplete forget
        return {"ok": False, "removed": 0, "remaining": [*remaining, reason],
                "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": not remaining, "removed": 0, "remaining": remaining}


def forget_everywhere(episode_id: int, *, memory_root: Path | None = None) -> dict[str, Any]:
    """Hub + legacy file. Opens its own connection and commits."""
    from khipu.db import connect

    with connect() as conn:
        with conn.cursor() as cur:
            out = forget_episode(cur, episode_id)
        conn.commit()
    if not out.get("ok"):
        return out
    partial: list[str] = []
    try:
        from khipu.hub_snapshot import forget_episode_in_snapshot, snapshot_path

        out["snapshot"] = forget_episode_in_snapshot(episode_id)
        if (out["snapshot"].get("error") == "hub snapshot missing"
                and not snapshot_path().exists()):
            out["snapshot"] = {"ok": True, "skipped": True, "reason": "no snapshot exists"}
    except Exception as exc:  # noqa: BLE001 — the hub write already succeeded
        out["snapshot"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if not out["snapshot"].get("ok"):
        partial.append("snapshot")
    root_resolved = True
    if memory_root is None:
        try:
            from khipu.config import path_setting

            memory_root = path_setting("memory_root")
        except Exception:  # noqa: BLE001
            memory_root = None
            root_resolved = False
    identities = out.get("legacy_identities") or [out.get("identity") or {}]
    out["legacy_files"] = [
        forget_in_legacy_file(memory_root, ident.get("ts", ""), ident.get("summary_md5", ""))
        for ident in identities
    ]
    out["legacy_file"] = out["legacy_files"][0]
    if not root_resolved or any(str(item.get("reason") or "").startswith(("unreadable", "backup", "rewrite"))
                                  for item in out["legacy_files"]):
        partial.append("legacy_file")
    if any(not item.get("backups", {}).get("ok", True) for item in out["legacy_files"]):
        partial.append("legacy_backups")
    out["outboxes"] = [_outbox_remainder(out.get("session_id"),
                                       has_merged_sources=out.get("has_merged_sources", False))]
    out["outbox"] = {"ok": all(item.get("ok") for item in out["outboxes"]),
                     "removed": sum(int(item.get("removed") or 0) for item in out["outboxes"])}
    if not out["outbox"]["ok"]:
        partial.append("outbox")
    out["complete"] = not partial
    out["partial"] = partial
    out["remaining"] = out["outboxes"][0].get("remaining", [])
    out["partial_errors"] = {
        "snapshot": out["snapshot"].get("error"),
        "legacy_file": [item.get("reason") for item in out["legacy_files"] if item.get("reason")],
        "legacy_backups": [item.get("backups", {}).get("remaining", []) for item in out["legacy_files"]],
        "outbox": [{key: item[key] for key in ("error", "remaining") if item.get(key)}
                   for item in out["outboxes"] if item.get("error") or item.get("remaining")],
    }
    out["partial_errors"] = {key: value for key, value in out["partial_errors"].items() if value}
    out["ok"] = out["complete"]
    if partial:
        out["error"] = "forget incomplete; remaining stores: " + ", ".join(partial)
    return out
