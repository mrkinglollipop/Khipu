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
from datetime import datetime, timezone
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
    live = {
        "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
        "summary_md5": hashlib.md5((summary or "").encode("utf-8")).hexdigest(),
    }
    # Every legacy line this episode may have left behind, in order: its live
    # identity, then each source row's, then the pre-0027 raw original. None is
    # erased until the one ownership check below has cleared it.
    candidates = [live]
    has_merged_sources = False
    source_rows: list[dict[str, Any]] | None = None
    unidentified_sources = 0
    from khipu.capture_identity import sources_for_episode, sources_ready

    raw = None
    try:
        cur.execute("SELECT raw FROM episodes WHERE id = %s", (episode_id,))
        raw_row = cur.fetchone()
        raw = raw_row[0] if isinstance(raw_row, tuple) and len(raw_row) == 1 else None
    except Exception:  # noqa: BLE001 — old schema/driver must still forget the hub row
        raw = None
    sources = sources_ready(cur)
    if sources:
        # Every source row, origin and merged, keeps the (ts, summary_md5) it
        # was first written under, so each legacy line this episode ever had
        # is named exactly, whatever edits and merges did to the live summary
        # since (RSN-6). The live identity above stays first.
        source_rows = sources_for_episode(cur, episode_id)
        # Recording a source row fails open, so the rows can be a subset of
        # the sources raw names. Each missing one is rebuilt from raw, or
        # counted as unidentified when raw no longer carries its payload.
        recovered, missing = _sources_missing_from_rows(raw, source_rows)
        source_rows = [*source_rows, *recovered]
        unidentified_sources += missing
        has_merged_sources = any(src["role"] == "merged" for src in source_rows)
        unidentified_sources += sum(1 for src in source_rows
                                   if src["ts"] is None or src["summary_md5"] is None)
        for src in source_rows:
            ident = {"ts": src["ts"], "summary_md5": src["summary_md5"]}
            if (isinstance(ident["ts"], str) and ident["summary_md5"]
                    and not any(_same_identity(ident, known) for known in candidates)):
                candidates.append(ident)
    # Pre-0027 hub (or a 0027 hub where raw named nothing to recover): an
    # edited summary can still have an original legacy identity in raw. For merged episodes, raw describes a retained source,
    # not an identity that forget can safely erase without a capture-identity
    # contract.
    if isinstance(raw, dict) and not source_rows:
        has_merged_sources = bool(raw.get("merged_from"))
        original = _raw_identity(raw)
        if (not has_merged_sources and original is not None
                and not any(_same_identity(original, known) for known in candidates)):
            candidates.append(original)
    # A legacy line another episode also holds is evidence of that episode.
    # Erasing it would lose that episode's line, so it stays and the forget
    # reports itself incomplete. The live identity gets no exemption: an edit
    # frees a legacy key, so another episode's original line can match it.
    held = _held_elsewhere(cur, episode_id, candidates, sources=sources)
    shared_identities = [i for i in candidates if i in held]
    legacy_identities = [i for i in candidates if i not in held]
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
        "identity": live,
        "legacy_identities": legacy_identities,
        "has_merged_sources": has_merged_sources,
        "source_rows": source_rows,
        "unidentified_sources": unidentified_sources,
        "shared_identities": shared_identities,
    }


def _raw_identity(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The (ts, summary_md5) legacy identity a captured payload was mirrored
    under, or None when the payload lacks either."""
    ts, summary = payload.get("ts"), payload.get("summary")
    if not isinstance(ts, str) or not isinstance(summary, str) or not summary:
        return None
    return {"ts": ts, "summary_md5": hashlib.md5(summary.encode("utf-8")).hexdigest()}


def _sources_missing_from_rows(raw: Any, rows: list[dict[str, Any]]
                               ) -> tuple[list[dict[str, Any]], int]:
    """Sources raw names (its own origin and each merge receipt) that no
    source row covers. Returns rows rebuilt from raw for the ones it can
    identify, and how many it cannot."""
    if not isinstance(raw, dict):
        return [], 0
    from khipu.capture_identity import has_natural_key, payload_sha256, source_key

    recovered: list[dict[str, Any]] = []
    missing = 0
    if not any(row["role"] == "origin" for row in rows):
        # raw is the origin payload, but a merge rewrites raw.verbatim, so its
        # payload key is gone; the natural key and the legacy identity survive.
        ident = _raw_identity(raw)
        if ident is None:
            missing += 1
        else:
            key = source_key(raw) if has_natural_key(raw) else None
            recovered.append({"source_key": key, "role": "origin", **ident,
                              "payload_sha256": None, "recovered": True})
    hashes = {row.get("payload_sha256") for row in rows} - {None}
    receipts = raw.get("merged_from") or []
    if not isinstance(receipts, list):
        return recovered, missing + 1
    for receipt in receipts:
        if not isinstance(receipt, dict):
            missing += 1
            continue
        payload = receipt.get("payload")
        source_id = receipt.get("source_id")
        source_id = source_id if isinstance(source_id, str) else None
        if source_id in hashes:
            continue
        ident = _raw_identity(payload) if isinstance(payload, dict) else None
        if ident is None:
            missing += 1
            continue
        # A row with the same key covers the receipt only when it names the
        # same legacy line: a natural key is shared by every re-synthesis of
        # one transcript span, and the merged row loses that key conflict.
        key = source_key(payload)
        if any(row["source_key"] == key and _same_identity(ident, row) for row in rows):
            continue
        recovered.append({"source_key": key, "role": "merged", **ident,
                          "payload_sha256": source_id or payload_sha256(payload),
                          "recovered": True})
    return recovered, missing


def _instant(value: Any) -> datetime | None:
    """A timestamp as a UTC instant to the second, or None when unreadable.
    Naive means UTC; a trailing Z is read as +00:00."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    try:
        return value.astimezone(timezone.utc).replace(microsecond=0)
    except OverflowError:  # year 1 or 9999 shifted past the calendar's edge
        return None


def _same_identity(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Two legacy identities name the same file line: same summary md5 and the
    same UTC second, whatever offset either was written with."""
    ta, tb = _instant(a.get("ts")), _instant(b.get("ts"))
    return (bool(a.get("summary_md5")) and a.get("summary_md5") == b.get("summary_md5")
            and ta is not None and ta == tb)


def _held_elsewhere(cur, episode_id: int, identities: list[dict[str, Any]], *,
                    sources: bool) -> list[dict[str, Any]]:
    """The identities some other live episode also holds: as its live row, its
    raw origin, a raw merge receipt, or (with ``sources``) a source row. The
    database matches on summary md5; the time is compared here as an instant,
    so a text offset cannot hide a holder."""
    md = sorted({i["summary_md5"] for i in identities if i.get("summary_md5")})
    if not md:
        return []
    parts = [
        "SELECT md5(e.summary) AS md, e.ts AS ts, NULL::text AS ts_text FROM episodes e "
        "WHERE e.id <> %(id)s AND e.deleted_at IS NULL AND md5(e.summary) = ANY(%(md)s::text[])",
        "SELECT md5(e.raw->>'summary'), NULL::timestamptz, e.raw->>'ts' FROM episodes e "
        "WHERE e.id <> %(id)s AND e.deleted_at IS NULL "
        "AND jsonb_typeof(e.raw->'summary') = 'string' "
        "AND md5(e.raw->>'summary') = ANY(%(md)s::text[])",
        "SELECT md5(r.v->'payload'->>'summary'), NULL::timestamptz, r.v->'payload'->>'ts' "
        "FROM episodes e CROSS JOIN LATERAL jsonb_array_elements(CASE "
        "WHEN jsonb_typeof(e.raw->'merged_from') = 'array' THEN e.raw->'merged_from' "
        "ELSE '[]'::jsonb END) AS r(v) "
        "WHERE e.id <> %(id)s AND e.deleted_at IS NULL AND jsonb_typeof(r.v) = 'object' "
        "AND jsonb_typeof(r.v->'payload') = 'object' "
        "AND jsonb_typeof(r.v->'payload'->'summary') = 'string' "
        "AND md5(r.v->'payload'->>'summary') = ANY(%(md)s::text[])",
    ]
    if sources:
        parts.append(
            "SELECT s.summary_md5, s.ts, NULL::text FROM episode_sources s "
            "JOIN episodes e ON e.id = s.episode_id "
            "WHERE s.episode_id <> %(id)s AND e.deleted_at IS NULL AND s.deleted_at IS NULL "
            "AND s.summary_md5 = ANY(%(md)s::text[])")
    cur.execute("SELECT held.md, held.ts, held.ts_text FROM (" + " UNION ALL ".join(parts)
                + ") AS held", {"id": episode_id, "md": md})
    holders = set()
    for held_md, held_ts, held_text in cur.fetchall():
        when = _instant(held_ts if held_ts is not None else held_text)
        if when is not None:
            holders.add((held_md, when))
    return [i for i in identities if (i.get("summary_md5"), _instant(i.get("ts"))) in holders]


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
    # The file stores what the hook minted (2026-09-05T12:20:17Z or with an
    # offset); PG returns an aware datetime. Compare as UTC instants to the
    # second, never as wall-clock text.
    when = _instant(obj.get("ts"))
    return when is not None and when == _instant(ts_iso)


def _has_unidentified_records(lines: list[str]) -> bool:
    for line in lines:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            return True
        if (not isinstance(obj, dict) or not isinstance(obj.get("summary"), str)
                or not isinstance(obj.get("ts"), str) or _instant(obj["ts"]) is None):
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
    if _instant(ts_iso) is None or not summary_md5:
        # Nothing to match on: touch no file, and say so so the forget is partial.
        return {"file": None, "removed": 0, "reason": "unreadable identity", "identity": identity,
                "backups": {"ok": True, "removed": 0, "remaining": []}}
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


def _source_copy(payload: dict[str, Any], source_rows: list[dict[str, Any]]) -> bool:
    """Whether a queued job is a copy of one of the episode's sources: by its
    source key, its payload hash, or the legacy identity the source row was
    first written under (a job that never reached the hub has no row of its
    own, but it shares the key of the row its retry would land on)."""
    from khipu.capture_identity import payload_sha256, source_key

    import copy

    from khipu.capture import _redact_capture_values

    keys = {src["source_key"] for src in source_rows}
    hashes = {src["payload_sha256"] for src in source_rows if src.get("payload_sha256")}
    # A job queued by the legacy mirror path may be unredacted while its source
    # row was keyed after redaction, so the redacted copy is compared as well.
    redacted = _redact_capture_values(copy.deepcopy(payload))
    for form in (payload, redacted):
        if source_key(form) in keys or payload_sha256(form) in hashes:
            return True
    summary = payload.get("summary")
    if not isinstance(summary, str):
        return False
    ident = {"ts": str(payload.get("ts") or ""),
             "summary_md5": hashlib.md5(summary.encode("utf-8")).hexdigest()}
    return any(_same_identity(ident, src) for src in source_rows if src.get("summary_md5"))


def _outbox_remainder(session_id: str | None, *, has_merged_sources: bool = False,
                      source_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Report retained jobs without changing files.

    With the episode's source rows (a hub with 0027) the report names the
    exact job files that are copies of this episode's sources; erasing them
    waits for phase 3, so it still only reports. Without them (pre-0027, or
    an episode whose source row was never recorded) any job from the same
    session counts, as before."""
    remaining: list[str] = []
    reason = "outbox: not cleaned (capture identity needed)"
    if source_rows:
        reason = "outbox: copy retained (erase by source key lands in phase 3)"
    elif has_merged_sources:
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
                    if source_rows:
                        if _source_copy(payload, source_rows):
                            remaining.append(f"{reason}; {path}")
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
    # An all-shared episode has an empty list: never fall back to its live line.
    identities = (out["legacy_identities"] if "legacy_identities" in out
                  else [out.get("identity") or {}])
    out["legacy_files"] = [
        forget_in_legacy_file(memory_root, ident.get("ts", ""), ident.get("summary_md5", ""))
        for ident in identities
    ]
    out["legacy_file"] = (out["legacy_files"][0] if out["legacy_files"]
                          else {"file": None, "removed": 0})
    if not root_resolved or any(str(item.get("reason") or "").startswith(("unreadable", "backup", "rewrite"))
                                  for item in out["legacy_files"]):
        partial.append("legacy_file")
    if any(not item.get("backups", {}).get("ok", True) for item in out["legacy_files"]):
        partial.append("legacy_backups")
    if out.get("unidentified_sources", 0) > 0 and "legacy_file" not in partial:
        partial.append("legacy_file")
    out["outboxes"] = [_outbox_remainder(out.get("session_id"),
                                       has_merged_sources=out.get("has_merged_sources", False),
                                       source_rows=out.get("source_rows"))]
    out["outbox"] = {"ok": all(item.get("ok") for item in out["outboxes"]),
                     "removed": sum(int(item.get("removed") or 0) for item in out["outboxes"])}
    if not out["outbox"]["ok"]:
        partial.append("outbox")
    if out.get("shared_identities"):
        partial.append("legacy_shared")
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
    if out.get("unidentified_sources", 0) > 0:
        out["partial_errors"]["legacy_file"] = [
            *out["partial_errors"].get("legacy_file", []),
            f"source rows without a legacy identity: {out['unidentified_sources']}"]
    if out.get("shared_identities"):
        out["partial_errors"]["legacy_shared"] = out["shared_identities"]
    out["partial_errors"] = {key: value for key, value in out["partial_errors"].items() if value}
    out["ok"] = out["complete"]
    if partial:
        out["error"] = "forget incomplete; remaining stores: " + ", ".join(partial)
    return out
