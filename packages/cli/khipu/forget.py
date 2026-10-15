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
  6. its lines in every legacy log, .bak copy and retained forget backup;
     a crash-left temporary file holding one is reported, never edited
  7. the local sqlite replica, if one exists (deleted_at + its embeddings);
     a crash-left refresh temp holding the row is reported, never edited
  8. every brief citing it → stale (migration 0025)
  9. queued capture jobs that are exact copies of its sources → removed under
     the outbox lock; a job that is unreadable, matches only loosely, or that
     another live episode holds is kept and reported with the reason

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
import os
import re
import stat
import time
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

from khipu.lines import split_lines

LOCAL_HARNESSES = ("claude_code", "cursor", "codex", "aegis")

# What a forget keeps, said plainly in every result. Nothing deletes a
# forgotten episode's row: there is no purge command and no restore command.
KEPT = ("The hub and each Mac's local replica keep this episode's row and text, marked "
        "forgotten (deleted_at set); search, recall and get skip it. No command purges "
        "it yet, and no command restores it.")
NO_ROOT_WHY = "no memory root is configured, so legacy logs could not be checked"
GATEWAY_NO_ROOT_WHY = ("this server is the HTTPS gateway, a hub-only server that keeps no "
                       "legacy logs, so there are none to check")
# Set by the gateway itself at startup (mcp_server.mark_gateway_active). Only
# this marker, never a missing setting, says a server keeps no legacy logs.
GATEWAY_ACTIVE_ENV = "KHIPU_GATEWAY_ACTIVE"


def _is_gateway() -> bool:
    return (os.environ.get(GATEWAY_ACTIVE_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


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


def forget_episode(cur, episode_id: int, *, memory_root: Path | None = None) -> dict[str, Any]:
    """The hub half. Caller commits. Returns the counts and the (ts, summary
    md5) identity the legacy file uses, or ``{"ok": False}`` when unknown.
    With ``memory_root`` the legacy lines themselves are hashed too, so a line
    another episode holds only by payload hash is recognised as shared."""
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
    # Another episode can hold a line by payload hash alone: a source row whose
    # legacy identity was never recorded, or a merge receipt without its payload.
    hashes = _candidate_hashes(candidates, source_rows or [], raw, memory_root)
    owned = _owned_elsewhere(cur, episode_id, set().union(*hashes), sources=sources)
    shared = [i in held or bool(h & owned) for i, h in zip(candidates, hashes)]
    shared_identities = [i for i, s in zip(candidates, shared) if s]
    legacy_identities = [i for i, s in zip(candidates, shared) if not s]
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


_FRACTION = re.compile(r"(\d{2}:\d{2}:\d{2})[.,](\d+)")


def _instant(value: Any) -> datetime | None:
    """A timestamp as a UTC instant to the second, or None when unreadable.
    Naive means UTC; a trailing Z is read as +00:00. A fraction is rounded to
    microseconds the way Postgres reads it (fromisoformat truncates), so
    09:00:00.9999999 is 09:00:01."""
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        fraction = _FRACTION.search(text)
        micro = 0
        if fraction:
            text = text[:fraction.start()] + fraction.group(1) + text[fraction.end():]
            micro = int(Decimal("0." + fraction.group(2)).quantize(
                Decimal("0.000001"), rounding=ROUND_HALF_EVEN) * 1_000_000)
        try:
            value = datetime.fromisoformat(text) + timedelta(microseconds=micro)
        except (ValueError, TypeError, OverflowError):
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


def _candidate_hashes(candidates: list[dict[str, Any]], source_rows: list[dict[str, Any]],
                      raw: Any, memory_root: Path | None) -> list[set[str]]:
    """For each candidate identity, every payload hash a copy of its line is
    known by: this episode's source rows and raw payloads, and each legacy
    line carrying the identity, as written and as redacted."""
    import copy

    from khipu.capture import _redact_capture_values
    from khipu.capture_identity import payload_sha256

    out: list[set[str]] = [set() for _ in candidates]

    def add(ident: dict[str, Any], *values: Any) -> None:
        for n, cand in enumerate(candidates):
            if _same_identity(ident, cand):
                out[n].update(v for v in values if isinstance(v, str) and v)

    for src in source_rows:
        add(src, src.get("payload_sha256"))
    if isinstance(raw, dict):
        ident = _raw_identity(raw)
        if ident is not None:
            add(ident, payload_sha256(raw))
        receipts = raw.get("merged_from")
        for receipt in receipts if isinstance(receipts, list) else []:
            payload = receipt.get("payload") if isinstance(receipt, dict) else None
            ident = _raw_identity(payload) if isinstance(payload, dict) else None
            if ident is not None:
                add(ident, receipt.get("source_id"), payload_sha256(payload))
    if memory_root is None:
        return out
    keys = [(c.get("summary_md5"), _instant(c.get("ts"))) for c in candidates]
    try:
        places = legacy_locations(Path(memory_root))
    except OSError:
        return out
    for _place, path in places:
        # An unreadable file is reported by the scrub step, not here.
        try:
            lines = split_lines(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            continue
        for line in lines:
            obj = _line_object(line)
            if obj is None or _line_key(obj) not in keys:
                continue
            try:
                digests = (payload_sha256(obj),
                           payload_sha256(_redact_capture_values(copy.deepcopy(obj))))
            except UnicodeEncodeError:  # a lone surrogate elsewhere in the line
                continue
            for n, key in enumerate(keys):
                if key == _line_key(obj):
                    out[n].update(digests)
    return out


def _owned_elsewhere(cur, episode_id: int, hashes: set[str], *, sources: bool) -> set[str]:
    """The payload hashes some other live episode holds: as a raw merge
    receipt's source id or (with ``sources``) a source row's payload hash or
    source key."""
    if not hashes:
        return set()
    parts = [
        "SELECT r.v->>'source_id' AS h "
        "FROM episodes e CROSS JOIN LATERAL jsonb_array_elements(CASE "
        "WHEN jsonb_typeof(e.raw->'merged_from') = 'array' THEN e.raw->'merged_from' "
        "ELSE '[]'::jsonb END) AS r(v) "
        "WHERE e.id <> %(id)s AND e.deleted_at IS NULL AND jsonb_typeof(r.v) = 'object' "
        "AND r.v->>'source_id' = ANY(%(h)s::text[])",
    ]
    if sources:
        for column in ("payload_sha256", "source_key"):
            parts.append(
                f"SELECT s.{column} FROM episode_sources s JOIN episodes e ON e.id = s.episode_id "
                "WHERE s.episode_id <> %(id)s AND e.deleted_at IS NULL AND s.deleted_at IS NULL "
                f"AND s.{column} = ANY(%(h)s::text[])")
    cur.execute("SELECT owned.h FROM (" + " UNION ALL ".join(parts) + ") AS owned",
                {"id": episode_id, "h": sorted(hashes)})
    return {row[0] for row in cur.fetchall() if row and isinstance(row[0], str)}


def _line_object(line: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _line_key(obj: dict[str, Any]) -> tuple[str, datetime] | None:
    """A legacy line's (summary md5, UTC instant), or None when unreadable.
    The file stores what the hook minted (2026-09-05T12:20:17Z or with an
    offset); PG returns an aware datetime. Compare as UTC instants to the
    second, never as wall-clock text."""
    summary = obj.get("summary") or ""
    when = _instant(obj.get("ts"))
    if not isinstance(summary, str) or when is None:
        return None
    try:
        digest = hashlib.md5(summary.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:  # a lone surrogate is valid JSON but not UTF-8
        return None
    return digest, when


def _line_matches(line: str, ts_iso: str | None, summary_md5: str | None) -> bool:
    obj = _line_object(line)
    key = _line_key(obj) if obj is not None else None
    return key is not None and bool(summary_md5) and key == (summary_md5, _instant(ts_iso))


def _has_unidentified_records(lines: list[str]) -> bool:
    for line in lines:
        if not line.strip():
            continue
        obj = _line_object(line)
        if (obj is None or not isinstance(obj.get("summary"), str)
                or not isinstance(obj.get("ts"), str) or _line_key(obj) is None):
            return True
    return False


BACKUP_DIR = ".khipu-forget-backups"

# Worst first: a location reports the worst state any identity pass saw.
LOCATION_STATES = ("unreadable", "not checked", "copy remains", "unidentified records",
                   "removed", "clean", "not applicable")
# States that leave a forgotten line, or one nobody could look at or identify.
INCOMPLETE_STATES = frozenset(LOCATION_STATES[:4])


def legacy_locations(memory_root: Path) -> list[tuple[str, Path]]:
    """Every file under the memory root that can hold a copy of a legacy line,
    as (place, path): each active log ``drift.episode_files`` knows (rotated
    years included), ``.bak`` copies beside them, retained forget backups, and
    temporary files a crashed rewrite left behind."""
    from khipu.drift import episode_files

    root = Path(memory_root)
    out = [("legacy log", path) for path in episode_files(root)]
    listed = {path for _place, path in out}
    # Any other file named like a log is a copy someone made (.bak, .orig, ~),
    # and a scrub writes its temp as ".<name>.tmp" beside it.
    for path in sorted([*root.glob("episodes*"), *root.glob(".episodes*")]):
        if path in listed or not _present_file(path):
            continue
        if path.name.endswith(".tmp"):
            out.append(("temporary file", path))
        elif path.name == "episodes.jsonl" or (
                path.name.startswith("episodes_") and path.suffix == ".jsonl"
                and ".bak" not in path.name):
            # An active log drift could not stat: listed so its read fails loudly.
            out.append(("legacy log", path))
        else:
            out.append(("legacy copy", path))
    backup_dir = root / BACKUP_DIR
    if backup_dir.is_dir():
        for path in sorted(backup_dir.iterdir()):
            if not _present_file(path):
                continue
            if not path.name.startswith("."):
                out.append(("forget backup", path))
            elif path.name.endswith(".tmp"):
                out.append(("temporary file", path))
    return out


def _present_file(path: Path) -> bool:
    """A regular file, or an entry that cannot be stat'ed: a directory that
    lists names but denies stat must not hide a copy, so its entries count as
    present and the read that follows reports them unreadable."""
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _replace_target(path: Path) -> Path:
    """The file a rewrite must replace: a symlinked log is rewritten at its
    target, so the link survives and no copy stays behind it."""
    return Path(os.path.realpath(path))


def _read_lines(path: Path) -> list[str]:
    return split_lines(path.read_text(encoding="utf-8"), keepends=True)


def _rewrite_log(root: Path, path: Path, ts_iso: str | None, summary_md5: str | None) -> dict[str, Any]:
    """Remove matching lines from one active log. The rewrite preserves a
    backup containing only the remaining records."""
    try:
        lines = _read_lines(path)
    except (OSError, UnicodeError) as exc:
        return {"state": "unreadable", "removed": 0, "reason": f"unreadable: {exc}"}
    keep = [ln for ln in lines if not _line_matches(ln, ts_iso, summary_md5)]
    unidentified = _has_unidentified_records(keep)
    reason = {"reason": "unreadable legacy identities remain"} if unidentified else {}
    removed = len(lines) - len(keep)
    if not removed:
        return {"state": "unidentified records" if unidentified else "clean", "removed": 0, **reason}
    backup_dir = root / BACKUP_DIR
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"state": "copy remains", "removed": 0, "reason": f"backup unavailable: {exc}",
                "problem": str(backup_dir)}
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = backup_dir / f"{path.name}.{stamp}"
    # A retained backup may never retain the line just forgotten. It exists to
    # protect the remaining file, not to create a new plaintext copy of data
    # the caller asked to erase.
    from khipu.paths import write_sensitive_bytes

    try:
        write_sensitive_bytes(backup, "".join(keep).encode("utf-8"))
        target = _replace_target(path)
        tmp = target.with_name(f"{target.name}.tmp")
        write_sensitive_bytes(tmp, "".join(keep).encode("utf-8"))
        tmp.replace(target)
    except (OSError, UnicodeError) as exc:
        return {"state": "copy remains", "removed": 0, "reason": f"rewrite failed: {exc}",
                "problem": str(path)}
    return {"state": "unidentified records" if unidentified else "removed", "removed": removed,
            "backup": str(backup), **reason}


def _scrub_copy(path: Path, ts_iso: str | None, summary_md5: str | None) -> dict[str, Any]:
    """Remove matching lines in place from a .bak copy or a retained backup.
    A backup made while forgetting one of two identities can contain the
    other, so each identity pass scrubs every old copy."""
    try:
        lines = _read_lines(path)
        keep = [ln for ln in lines if not _line_matches(ln, ts_iso, summary_md5)]
        unidentified = _has_unidentified_records(keep)
        problem = {"problem": f"{path}: unidentified legacy records"} if unidentified else {}
        if len(keep) == len(lines):
            return {"state": "unidentified records" if unidentified else "clean", "removed": 0,
                    **problem}
        from khipu.paths import write_sensitive_bytes

        target = _replace_target(path)
        tmp = target.with_name(f".{target.name}.tmp")
        write_sensitive_bytes(tmp, "".join(keep).encode("utf-8"))
        tmp.replace(target)
    except (OSError, UnicodeError) as exc:
        return {"state": "unreadable", "removed": 0, "problem": f"{path}: {type(exc).__name__}"}
    return {"state": "unidentified records" if unidentified else "removed",
            "removed": len(lines) - len(keep), **problem}


def _temp_state(path: Path, ts_iso: str | None, summary_md5: str | None) -> str | None:
    """What a crash-left temporary file holds. It is never edited: a writer may
    still own it. A torn line could be the forgotten one, so it counts as
    unreadable. None when it has gone since it was listed."""
    try:
        lines = _read_lines(path)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError):
        return "unreadable"
    if any(_line_matches(ln, ts_iso, summary_md5) for ln in lines):
        return "copy remains"
    return "unreadable" if _has_unidentified_records(lines) else "clean"


def _legacy_pass(root: Path, ts_iso: str | None, summary_md5: str | None) -> dict[str, Any]:
    """One identity pass over every legacy location. With no identity it
    only reports what each location is."""
    main = root / "episodes.jsonl"
    out: dict[str, Any] = {"file": str(main), "removed": 0}
    backups: dict[str, Any] = {"ok": True, "removed": 0, "remaining": []}
    locations: list[dict[str, str]] = []
    try:
        places = legacy_locations(root)
    except OSError as exc:
        return {**out, "reason": f"unreadable: {exc}",
                "backups": {"ok": False, "removed": 0, "remaining": [str(root)]}, "locations": []}
    if not any(path == main for _place, path in places):
        out["reason"] = "no legacy file"
    temps = []
    for place, path in places:
        if place == "temporary file":
            temps.append((place, path))
            continue
        if place == "legacy log":
            result = _rewrite_log(root, path, ts_iso, summary_md5)
            if path == main:
                out.update({k: result[k] for k in ("reason", "backup") if k in result})
            if result.get("problem"):
                backups["remaining"].append(result["problem"])
        else:
            result = _scrub_copy(path, ts_iso, summary_md5)
            backups["removed"] += result["removed"]
            if result.get("problem"):
                backups["remaining"].append(result["problem"])
        out["removed"] += result["removed"]
        locations.append({"place": place, "path": str(path), "state": result["state"]})
    # Temporary files are read after the rewrites: a rewrite reuses its log's
    # temp name, so a crash-left one there is replaced by the clean rewrite.
    for place, path in temps:
        state = _temp_state(path, ts_iso, summary_md5)
        if state is not None:
            locations.append({"place": place, "path": str(path), "state": state})
    backups["ok"] = not backups["remaining"]
    return {**out, "backups": backups, "locations": locations}


def forget_in_legacy_file(memory_root: Path | None, ts_iso: str, summary_md5: str) -> dict[str, Any]:
    """Remove matching lines from every legacy log, .bak copy and retained
    backup, and report crash-left temporary files that hold one. ``file`` is
    the main log; ``locations`` names every file looked at and its state."""
    identity = {"ts": ts_iso, "summary_md5": summary_md5}
    if _instant(ts_iso) is None or not summary_md5:
        # Nothing to match on: touch no file, and say so so the forget is partial.
        return {"file": None, "removed": 0, "reason": "unreadable identity", "identity": identity,
                "backups": {"ok": True, "removed": 0, "remaining": []}, "locations": []}
    if memory_root is None:
        return {"file": None, "removed": 0, "reason": "memory_root not configured", "identity": identity,
                "backups": {"ok": True, "removed": 0, "remaining": []}, "locations": []}
    return {**_legacy_pass(Path(memory_root), ts_iso, summary_md5), "identity": identity}


def _merge_locations(*groups: list[dict[str, str]]) -> list[dict[str, str]]:
    """One entry per path (per place for an entry with no path); the worst
    state any pass saw wins."""
    merged: dict[Any, dict[str, Any]] = {}
    for group in groups:
        for loc in group:
            key = loc["path"] if loc["path"] is not None else ("place", loc["place"])
            known = merged.get(key)
            if known is None or (LOCATION_STATES.index(loc["state"])
                                 < LOCATION_STATES.index(known["state"])):
                merged[key] = dict(loc)
    return list(merged.values())


def _job_forms(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """A queued job as written and as redacted. A job queued by the legacy
    mirror path may be unredacted while its source row was keyed after
    redaction, so both forms are compared."""
    import copy

    from khipu.capture import _redact_capture_values

    return payload, _redact_capture_values(copy.deepcopy(payload))


def _job_hashes(payload: dict[str, Any]) -> set[str]:
    """Every payload hash and source key a queued job is known by."""
    from khipu.capture_identity import payload_sha256, source_key

    out: set[str] = set()
    for form in _job_forms(payload):
        out.update((payload_sha256(form), source_key(form)))
    return out


def _job_identity(payload: dict[str, Any]) -> dict[str, Any] | None:
    summary = payload.get("summary")
    if not isinstance(summary, str):
        return None
    try:
        digest = hashlib.md5(summary.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        return None
    return {"ts": str(payload.get("ts") or ""), "summary_md5": digest}


def _source_match(payload: dict[str, Any], source_rows: list[dict[str, Any]]) -> str | None:
    """How a queued job matches the episode's sources. 'exact': its source
    key or payload hash is a source row's (a job that never reached the hub
    has no row of its own, but its retry lands on the row with its key).
    'identity': it shares only the legacy (ts, summary) identity a source row
    was first written under, which another capture can also carry. None: no
    match."""
    from khipu.capture_identity import payload_sha256, source_key

    keys = {src["source_key"] for src in source_rows if src.get("source_key")}
    hashes = {src["payload_sha256"] for src in source_rows if src.get("payload_sha256")}
    for form in _job_forms(payload):
        if source_key(form) in keys or payload_sha256(form) in hashes:
            return "exact"
    ident = _job_identity(payload)
    if ident is not None and any(_same_identity(ident, src) for src in source_rows
                                 if src.get("summary_md5")):
        return "identity"
    return None


def _source_copy(payload: dict[str, Any], source_rows: list[dict[str, Any]]) -> bool:
    """Whether a queued job is a copy of one of the episode's sources, by any match."""
    return _source_match(payload, source_rows) is not None


def _outbox_entries() -> list[dict[str, Any]]:
    """Every outbox, dead and quarantine job file, and every crash-left temp
    beside them, with its bytes and payload or the error reading it. The
    caller holds the outbox lock: every writer writes its temp under it, so a
    temp seen here was left by a crash."""
    from khipu.outbox import dead_dir, outbox_dir, quarantine_dir

    entries: list[dict[str, Any]] = []
    for directory in (outbox_dir(), dead_dir(), quarantine_dir()):
        try:
            paths = sorted(directory.iterdir())
        except FileNotFoundError:
            continue
        for path in paths:
            # A crash in the atomic writer leaves ".<job>.<random>.tmp" holding
            # a whole or torn job. A quarantine ".error" file (and its temp)
            # holds only an error string.
            if path.suffix == ".json":
                place = "outbox job"
            elif path.suffix == ".tmp" and ".error." not in path.name:
                place = "outbox temp"
            else:
                continue
            if not _present_file(path):
                continue
            entry: dict[str, Any] = {"place": place, "path": path}
            try:
                entry["data"] = path.read_bytes()
                envelope = json.loads(entry["data"])
                payload = envelope.get("payload") if isinstance(envelope, dict) else None
                if not isinstance(payload, dict):
                    raise ValueError("job payload is not an object")
                entry["payload"] = payload
            except FileNotFoundError:
                continue  # drained or renamed since the listing
            except (OSError, ValueError, TypeError) as exc:
                entry["error"] = type(exc).__name__
            entries.append(entry)
    return entries


def _job_owners(episode_id: int, hashes: set[str], identities: list[dict[str, Any]],
                *, sources: bool) -> tuple[set[str], set[tuple[str, Any]]]:
    """Which of these job hashes and legacy identities another live episode
    holds, by the same ownership queries the legacy erase uses. Read with no
    outbox lock held: the lock never waits on the database."""
    from khipu.db import connect

    with connect() as conn:
        with conn.cursor() as cur:
            owned = _owned_elsewhere(cur, episode_id, hashes, sources=sources)
            held = _held_elsewhere(cur, episode_id, identities, sources=sources)
    return owned, {(i["summary_md5"], _instant(i["ts"])) for i in held}


def _classify_entries(entries: list[dict[str, Any]],
                      source_rows: list[dict[str, Any]]) -> None:
    """Add each readable entry's match, hashes and identity. A payload that
    cannot be hashed (a lone surrogate an older or foreign writer left) is
    marked unreadable on its own and never stops the other entries."""
    for entry in entries:
        if "payload" not in entry:
            continue
        try:
            match = _source_match(entry["payload"], source_rows)
            exact = match == "exact"
            entry["match"] = match
            entry["hashes"] = _job_hashes(entry["payload"]) if exact else set()
            entry["ident"] = _job_identity(entry["payload"]) if exact else None
        except (UnicodeError, ValueError, TypeError) as exc:
            entry.pop("payload")
            entry["error"] = type(exc).__name__


def _remove_note(path: Path) -> dict[str, str] | None:
    """Remove the quarantine note beside a removed quarantined job. The note
    names the job and its error; one that stays is reported, not hidden."""
    if path.parent.name != "quarantine" or path.suffix != ".json":
        return None
    note = path.with_suffix(".error")
    try:
        note.unlink(missing_ok=True)
    except OSError as exc:
        return {"place": "outbox note", "path": str(note), "state": "not checked",
                "why": (f"the quarantine note {note} beside a removed job could not be "
                        f"removed ({type(exc).__name__}), so it was kept")}
    return None


def _outbox_remainder(session_id: str | None, *, has_merged_sources: bool = False,
                      source_rows: list[dict[str, Any]] | None = None,
                      episode_id: int | None = None) -> dict[str, Any]:
    """Remove queued jobs that are exact copies of the episode's sources and
    report every job kept, with the reason.

    With the episode's source rows (a hub with 0027) a job whose source key
    or payload hash is one of theirs is removed from the queue, the dead
    folder, quarantine and crash-left temps, unless another live episode also
    holds it. A job that cannot be read, or that matches only by time and
    summary, is kept. Without source rows (pre-0027, or an episode whose
    source row was never recorded) any job from the same session counts as a
    copy and is kept, since nothing can tell it apart from another capture.
    Without ``episode_id`` ownership cannot be checked, so nothing is removed."""
    remaining: list[str] = []
    inspected: list[dict[str, str]] = []
    reason = "outbox: not cleaned (capture identity needed)"
    if not source_rows and has_merged_sources:
        return {"ok": False, "removed": 0, "remaining": [reason], "inspected": inspected}
    removed = 0
    try:
        from khipu.outbox import _locked_outbox

        if not source_rows:
            # One lock across all three directories: retry_dead() and the drain
            # move jobs between them under the same lock, so a job cannot slip
            # past the scan mid-move.
            with _locked_outbox():
                entries = _outbox_entries()
            for entry in entries:
                path, place = entry["path"], entry["place"]
                if "error" in entry:
                    why = (f"queued capture job {path} could not be read ({entry['error']}), "
                           "so it was kept")
                    remaining.append(f"{reason}; {path}: {entry['error']}")
                    inspected.append({"place": place, "path": str(path), "state": "unreadable",
                                      "why": why})
                    continue
                # Without a session id there is no way to tell this job from
                # the forgotten episode's capture, so any retained job counts.
                if not session_id or entry["payload"].get("session_id") == session_id:
                    # A job keeps the bare reason callers already match on; a
                    # temp is named, since nothing else lists it.
                    remaining.append(reason if place == "outbox job" else f"{reason}; {path}")
                    inspected.append({
                        "place": place, "path": str(path), "state": "copy remains",
                        "why": (f"queued capture job {path} is from the same session, but with no "
                                "source rows it cannot be matched exactly, so it was kept")})
                    return {"ok": False, "removed": 0, "remaining": remaining,
                            "inspected": inspected}
                inspected.append({"place": place, "path": str(path), "state": "clean"})
            return {"ok": not remaining, "removed": 0, "remaining": remaining,
                    "inspected": inspected}

        # First pass: which jobs are exact copies, and so whose ownership to ask.
        with _locked_outbox():
            first = _outbox_entries()
        _classify_entries(first, source_rows)
        exact = [e for e in first if e.get("match") == "exact"]
        asked: set[str] = set().union(*(e["hashes"] for e in exact))
        asked_idents = [e["ident"] for e in exact if e["ident"]]
        owners: tuple[set[str], set[tuple[str, Any]]] | None = None
        owner_error = ""
        if episode_id is None:
            owner_error = "no episode id was given"
        elif not exact:
            owners = (set(), set())
        else:
            try:
                owners = _job_owners(episode_id, asked, asked_idents, sources=True)
            except Exception as exc:  # noqa: BLE001 (an unchecked job is kept, never removed)
                owner_error = type(exc).__name__
        asked_keys = {(i["summary_md5"], _instant(i["ts"])) for i in asked_idents}

        # Second pass under the lock: the files as they are now. A job that
        # appeared or changed since the first pass was never checked, so it stays.
        with _locked_outbox():
            second = _outbox_entries()
            _classify_entries(second, source_rows)
            for entry in second:
                path, place = entry["path"], entry["place"]
                state, why = "clean", None
                note = None
                if "error" in entry:
                    state = "unreadable"
                    why = (f"queued capture job {path} could not be read ({entry['error']}), "
                           "so it was kept")
                else:
                    match, hashes, ident = entry["match"], entry["hashes"], entry["ident"]
                    key = (ident["summary_md5"], _instant(ident["ts"])) if ident else None
                    if match == "identity":
                        state = "copy remains"
                        why = (f"queued capture job {path} matches this episode only by time "
                               "and summary, so it was kept")
                    elif match == "exact" and owners is None:
                        state = "copy remains"
                        why = (f"queued capture job {path} could not be checked against other "
                               f"episodes ({owner_error}), so it was kept")
                    elif match == "exact" and (not hashes <= asked
                                               or (key is not None and key not in asked_keys)):
                        state = "copy remains"
                        why = (f"queued capture job {path} changed while the forget ran, "
                               "so it was kept")
                    elif match == "exact" and (hashes & owners[0] or key in owners[1]):
                        state = "copy remains"
                        why = (f"queued capture job {path} is also held by another live "
                               "episode, so it was kept")
                    elif match == "exact":
                        try:
                            path.unlink()
                        except FileNotFoundError:
                            continue
                        except OSError as exc:
                            state = "copy remains"
                            why = (f"queued capture job {path} could not be removed "
                                   f"({type(exc).__name__}), so it was kept")
                        else:
                            state = "removed"
                            removed += 1
                            note = _remove_note(path)
                loc = {"place": place, "path": str(path), "state": state}
                if why:
                    loc["why"] = why
                    remaining.append(f"outbox: {why}")
                inspected.append(loc)
                if note:
                    remaining.append(f"outbox: {note['why']}")
                    inspected.append(note)
    except Exception as exc:  # noqa: BLE001 — surface an incomplete forget
        # Nothing has shown a copy: the outbox itself could not be looked at.
        why = f"queued capture jobs could not be checked ({type(exc).__name__}), so the forget stays open"
        return {"ok": False, "removed": removed,
                "remaining": [*remaining, reason if not source_rows else f"outbox: {why}"],
                "error": f"{type(exc).__name__}: {exc}",
                "inspected": [*inspected, {"place": "outbox", "path": None,
                                           "state": "not checked", "why": why}]}
    return {"ok": not remaining, "removed": removed, "remaining": remaining,
            "inspected": inspected}


def _replica_temp_remainder(episode_id: int) -> dict[str, Any]:
    """Report, never edit, temporary replica files a crashed refresh left
    beside the replica (``.hub_snapshot.<random>.sqlite`` and its journal
    and WAL). Read under the refresh lock, so no live dump is mistaken for
    a crash-left one; without the lock they are not checked."""
    try:
        from khipu import hub_snapshot

        directory = hub_snapshot.snapshot_path().parent
        lock = hub_snapshot._acquire_refresh_lock()
    except Exception as exc:  # noqa: BLE001 (an unchecked place keeps the forget open)
        return {"ok": False, "locations": [{
            "place": "replica temp", "path": None, "state": "not checked",
            "why": f"replica temporary files could not be checked: {type(exc).__name__}"}]}
    if lock is None:
        return {"ok": False, "locations": [{
            "place": "replica temp", "path": str(directory), "state": "not checked",
            "why": "a replica refresh is running, so its temporary files could not be checked"}]}
    locations: list[dict[str, str]] = []
    try:
        try:
            names = sorted(path.name for path in directory.iterdir())
        except FileNotFoundError:
            names = []
        temps = [n for n in names if n.startswith(".hub_snapshot.")
                 and not n.startswith(".hub_snapshot.meta.")]
        states: dict[str, str] = {}
        for name in temps:
            if name.endswith(".sqlite"):
                states[name] = _replica_temp_state(directory / name, episode_id)
        for name in temps:
            if name.endswith(".sqlite"):
                state = states[name]
            elif name.endswith((".sqlite-journal", ".sqlite-wal")):
                # A journal or WAL holds pages that cannot be read on their own.
                main = states.get(name.rsplit("-", 1)[0])
                path = directory / name
                try:
                    empty = path.stat().st_size == 0
                except FileNotFoundError:
                    continue
                except OSError:
                    empty = False
                state = "clean" if empty else ("copy remains" if main == "copy remains"
                                                else "unreadable")
            else:
                continue
            locations.append({"place": "replica temp", "path": str(directory / name),
                              "state": state})
    except OSError as exc:
        locations.append({"place": "replica temp", "path": str(directory), "state": "unreadable",
                          "why": f"replica folder could not be listed: {type(exc).__name__}"})
    finally:
        hub_snapshot._release_refresh_lock(lock)
    return {"ok": not any(loc["state"] in INCOMPLETE_STATES for loc in locations),
            "locations": locations}


def _replica_temp_state(path: Path, episode_id: int) -> str:
    """'copy remains' when the temp's episodes table has the row, 'clean' when
    it has no such row (or no episodes table yet: the dump died before the
    schema), 'unreadable' when sqlite cannot open or query it read-only."""
    import sqlite3

    try:
        con = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=0.5)
    except sqlite3.Error:
        return "unreadable"
    try:
        has_table = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'episodes'").fetchone()
        if not has_table:
            return "clean"
        row = con.execute("SELECT 1 FROM episodes WHERE id = ? LIMIT 1", (episode_id,)).fetchone()
        return "copy remains" if row else "clean"
    except sqlite3.Error:
        return "unreadable"
    finally:
        con.close()


def plain_summary(out: dict[str, Any]) -> str:
    """The forget result in plain words: done or not, each place that still
    holds a copy or could not be checked, and what is kept by design."""
    eid = out.get("id")
    if out.get("complete"):
        lines = [f"Episode {eid} is forgotten everywhere Khipu keeps a copy."]
    elif out.get("partial"):
        lines = [f"Episode {eid} is not completely forgotten yet."]
    else:
        return str(out.get("error") or f"Episode {eid} could not be forgotten.")
    removed_jobs = int((out.get("outbox") or {}).get("removed") or 0)
    if removed_jobs:
        lines.append(f"- removed {removed_jobs} queued capture "
                     f"{'job' if removed_jobs == 1 else 'jobs'} that held a copy")
    outbox_named = False
    for loc in out.get("locations") or []:
        if loc.get("state") in INCOMPLETE_STATES:
            lines.append("- " + (loc.get("why") or f"{loc.get('place')} {loc.get('path')}: "
                                                   f"{loc.get('state')}"))
            outbox_named = outbox_named or (str(loc.get("place")).startswith("outbox")
                                            and bool(loc.get("why")))
        elif loc.get("state") == "not applicable" and loc.get("why"):
            lines.append(f"- not applicable: {loc['why']}")
    partial = out.get("partial") or []
    if "snapshot" in partial:
        lines.append("- this Mac's replica was not updated; it catches up at the next refresh")
    if "outbox" in partial and not outbox_named:
        lines.append("- a queued capture job still holds a copy")
    if "legacy_shared" in partial:
        lines.append("- another live episode holds the same legacy line, so it was kept")
    lines.append(KEPT)
    return "\n".join(lines)


def forget_everywhere(episode_id: int, *, memory_root: Path | None = None,
                      gateway: bool | None = None) -> dict[str, Any]:
    """Hub + legacy file. Opens its own connection and commits. ``gateway``
    defaults to the marker the HTTPS gateway sets at startup: a gateway with
    no memory root keeps no legacy logs, so they are not applicable there."""
    from khipu.db import connect

    root_resolved = True
    if memory_root is None:
        try:
            from khipu.config import path_setting

            memory_root = path_setting("memory_root")
        except Exception:  # noqa: BLE001
            memory_root = None
            root_resolved = False
    with connect() as conn:
        with conn.cursor() as cur:
            out = forget_episode(cur, episode_id, memory_root=memory_root)
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
    # An all-shared episode has an empty list: never fall back to its live line.
    identities = (out["legacy_identities"] if "legacy_identities" in out
                  else [out.get("identity") or {}])
    no_legacy_logs = (memory_root is None and root_resolved
                      and (_is_gateway() if gateway is None else bool(gateway)))
    out["legacy_files"] = [] if no_legacy_logs else [
        forget_in_legacy_file(memory_root, ident.get("ts", ""), ident.get("summary_md5", ""))
        for ident in identities
    ]
    out["legacy_file"] = (out["legacy_files"][0] if out["legacy_files"]
                          else {"file": None, "removed": 0})
    legacy_locs = _merge_locations(*(item.get("locations", []) for item in out["legacy_files"]))
    if no_legacy_logs:
        legacy_locs = [{"place": "legacy logs", "path": None, "state": "not applicable",
                        "why": GATEWAY_NO_ROOT_WHY}]
    elif memory_root is None:
        legacy_locs = [{"place": "legacy logs", "path": None, "state": "not checked",
                        "why": NO_ROOT_WHY}]
    elif not out["legacy_files"]:
        # Nothing to erase (every identity is shared), but every location is
        # still listed, and one that cannot be read still counts.
        legacy_locs = _legacy_pass(Path(memory_root), None, None)["locations"]
    blocked = [loc.get("why") or f"{loc['place']} {loc['path']}: {loc['state']}"
               for loc in legacy_locs if loc["state"] in INCOMPLETE_STATES]
    if (not root_resolved or blocked
            or any(str(item.get("reason") or "").startswith(("unreadable", "backup", "rewrite"))
                   for item in out["legacy_files"])):
        partial.append("legacy_file")
    if any(not item.get("backups", {}).get("ok", True) for item in out["legacy_files"]):
        partial.append("legacy_backups")
    if out.get("unidentified_sources", 0) > 0 and "legacy_file" not in partial:
        partial.append("legacy_file")
    out["outboxes"] = [_outbox_remainder(out.get("session_id"),
                                       has_merged_sources=out.get("has_merged_sources", False),
                                       source_rows=out.get("source_rows"),
                                       episode_id=episode_id)]
    out["replica_temps"] = _replica_temp_remainder(episode_id)
    if not out["replica_temps"]["ok"]:
        partial.append("replica_temp")
    out["locations"] = _merge_locations(
        legacy_locs, *(item.get("inspected", []) for item in out["outboxes"]),
        out["replica_temps"]["locations"])
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
        "legacy_file": [*(item.get("reason") for item in out["legacy_files"] if item.get("reason")),
                        *blocked],
        "legacy_backups": [item.get("backups", {}).get("remaining", []) for item in out["legacy_files"]],
        "outbox": [{key: item[key] for key in ("error", "remaining") if item.get(key)}
                   for item in out["outboxes"] if item.get("error") or item.get("remaining")],
        "replica_temp": [loc.get("why") or f"{loc['path']}: {loc['state']}"
                         for loc in out["replica_temps"]["locations"]
                         if loc["state"] in INCOMPLETE_STATES],
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
    out["kept"] = KEPT
    out["message"] = plain_summary(out)
    return out
