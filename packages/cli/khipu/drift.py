"""Drift sampling + doctor helpers."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from khipu.db import connect


def episode_files(memory_root: Path) -> list[Path]:
    """Every episode log the legacy tree holds: ``episodes.jsonl`` plus any
    ``episodes_*.jsonl`` sibling (rotated/legacy years), excluding ``.bak*``.

    2026-08-17: ``episodes_2026.jsonl`` (May 1–19) was never rotated into the
    main file and the reconcile read only ``episodes.jsonl`` by name — 52 of
    the oldest episodes were absent from PG. Enumerating here (and using this
    in BOTH the reconcile and the drift check) makes "the files hold more
    than PG" a red doctor, not a silent gap. Order: main file first.
    """
    main = memory_root / "episodes.jsonl"
    out = [main] if main.is_file() else []
    for p in sorted(memory_root.glob("episodes_*.jsonl")):
        if ".bak" in p.name or p == main:
            continue
        out.append(p)
    return out


# Known non-memory rows that live in the legacy logs. Matched on content, never
# by file name, so a real episode in an oddly named file is still counted.
#   - episodes_2020.jsonl holds one {"ts": "2020-01-01…", "summary": "smoke"} written
#     by the cursor-fork smoke test; it is not history.
def is_smoke_row(obj: dict) -> bool:
    return (obj.get("summary") or "").strip() == "smoke" and str(obj.get("ts", "")).startswith("2020-")


def file_episode_count(memory_root: Path) -> int:
    return len(file_episode_keys(memory_root))


def file_episode_keys(memory_root: Path) -> list[tuple[str, str]]:
    """(ts, md5(summary)) identity for every file episode — the same key the
    reconcile upserts on (uq_episodes_ts_summary_md5), so file ⊆ PG is checked
    on identity, not on counts. Malformed lines are skipped (they cannot have
    been mirrored either)."""
    keys: list[tuple[str, str]] = []
    for path in episode_files(memory_root):
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                ts = obj.get("ts")
                if not ts or is_smoke_row(obj):
                    continue
                summary = obj.get("summary") or ""
                keys.append((ts, hashlib.md5(summary.encode("utf-8")).hexdigest()))
    return keys


def episodes_missing_in_pg(cur, keys: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """File-episode keys with no PG row — the directional drift signal.

    P2b replaced count-delta drift (pg - file) with this check: PG legitimately
    holds MORE than the file (hub writes, archived-out episodes survive the
    upsert-only reconcile), so only "file episode absent from PG" is drift.
    Anti-join runs in PG so ts normalization uses the same ::timestamptz cast
    as the insert path. Returns the missing (ts, md5) pairs (empty = green) —
    reconcile uses the pairs to insert exactly the missing lines.
    """
    missing: list[tuple[str, str]] = []
    batch = 500
    for i in range(0, len(keys), batch):
        chunk = keys[i : i + batch]
        placeholders = ", ".join(["(%s, %s)"] * len(chunk))
        params = [x for pair in chunk for x in pair]
        cur.execute(
            f"""
            SELECT v.ts, v.md FROM (VALUES {placeholders}) AS v(ts, md)
            WHERE NOT EXISTS (
                SELECT 1 FROM episodes e
                WHERE e.ts = v.ts::timestamptz AND md5(e.summary) = v.md
            )
            """,
            params,
        )
        missing.extend((r[0], r[1]) for r in cur.fetchall())
    return missing


def file_topic_hashes(memory_root: Path, limit: int | None = None) -> tuple[dict[str, str], list[str]]:
    """sha256 of every topic file, plus the ones that could not be read.

    ``limit`` used to default to 25 and the walk was alphabetical, so the drift
    check examined the same first 4% of 622 topics on every run and the other
    597 were never compared to Postgres at all (audit 2026-08-17). Hashing all
    of them measures at 0.09 s, so the sample was buying nothing; ``limit`` is
    kept only for callers that explicitly want a cheap partial pass.

    An unreadable file is REPORTED, not raised: this feeds ``khipu doctor``, and
    one bad file used to abort the whole health report with a traceback.

    The hash comes from ``mirror.topic_content_hash`` — the same function the
    writer uses — so this check can never disagree with Postgres over encoding.
    """
    from khipu.mirror import read_topic_text, topic_content_hash

    topics = memory_root / "topics"
    out: dict[str, str] = {}
    unreadable: list[str] = []
    if not topics.is_dir():
        return out, unreadable
    for path in sorted(topics.glob("*.md")):
        if path.name.startswith("_"):
            continue
        text = read_topic_text(path)
        if text is None:
            unreadable.append(path.name)
            continue
        out[path.stem] = topic_content_hash(text)
        if limit is not None and len(out) >= limit:
            break
    return out, unreadable


def sample_drift(memory_root: Path, sample: int | None = None) -> dict:
    """Full file-vs-PG comparison. `sample` caps the topic pass for a caller
    that wants a cheap partial run; None (the default) checks every topic."""
    file_keys = file_episode_keys(memory_root)
    file_hashes, unreadable_topics = file_topic_hashes(memory_root, limit=sample)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM episodes")
            pg_eps = int(cur.fetchone()[0])
            missing = episodes_missing_in_pg(cur, file_keys)
            cur.execute(
                "SELECT COUNT(*) FILTER (WHERE deleted_at IS NULL), "
                "COUNT(*) FILTER (WHERE deleted_at IS NOT NULL) FROM topics"
            )
            pg_topics, pg_tombstoned = (int(x) for x in cur.fetchone())
            cur.execute(
                "SELECT MAX(ts), MAX(ingested_at) FROM episodes"
            )
            max_ts, max_ing = cur.fetchone()
            # One round trip for every slug instead of one query per slug: with
            # the full topic set that is 622 queries turned into 1.
            mismatches = []
            pg_topics_by_slug: dict[str, tuple] = {}
            if file_hashes:
                cur.execute(
                    "SELECT slug, content_hash, deleted_at FROM topics WHERE slug = ANY(%s)",
                    (list(file_hashes),),
                )
                pg_topics_by_slug = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
            for slug, digest in file_hashes.items():
                # A tombstoned row counts as missing: the file exists, so the
                # tombstone is stale drift (next reconcile un-tombstones it).
                row = pg_topics_by_slug.get(slug)
                if row is None:
                    mismatches.append({"slug": slug, "issue": "missing_in_pg"})
                elif row[1] is not None:
                    mismatches.append({"slug": slug, "issue": "tombstoned_in_pg"})
                elif row[0] != digest:
                    mismatches.append({"slug": slug, "issue": "hash_mismatch"})
    return {
        "episodes_file": len(file_keys),
        "episodes_pg": pg_eps,
        # Informational only since P2b — PG ⊇ file is healthy (hub writes,
        # archived episodes). The gate is episodes_missing_in_pg == 0.
        "episodes_delta": pg_eps - len(file_keys),
        "episodes_missing_in_pg": len(missing),
        "episodes_missing_sample": [ts for ts, _ in missing[:5]],
        "topics_pg": pg_topics,
        "topics_tombstoned": pg_tombstoned,
        "topics_checked": len(file_hashes),
        "topic_sample": sample,
        "topic_files_unreadable": unreadable_topics,
        "topic_mismatches": mismatches,
        "latest_episode_ts": max_ts.isoformat() if max_ts else None,
        "latest_ingested_at": max_ing.isoformat() if max_ing else None,
    }


BACKUP_MAX_AGE_HOURS = 36


def _latest_ops_event(cur, kind: str) -> dict | None:
    cur.execute(
        """
        SELECT status, detail, created_at, now() - created_at
        FROM ops_events
        WHERE kind = %s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (kind,),
    )
    row = cur.fetchone()
    if not row:
        return None
    status, detail, created_at, age = row
    return {
        "status": status,
        "detail": detail,
        "created_at": created_at.isoformat() if created_at else None,
        "age_seconds": age.total_seconds() if age is not None else None,
    }


def backup_health() -> dict:
    """Doctor backup-age + last restore (reads ops_events via DSN)."""
    max_age = BACKUP_MAX_AGE_HOURS * 3600
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT to_regclass('public.ops_events') IS NOT NULL
                    """
                )
                if not cur.fetchone()[0]:
                    return {
                        "ok": False,
                        "reason": "ops_events table missing — apply 0002_ops_events.sql",
                        "max_age_hours": BACKUP_MAX_AGE_HOURS,
                    }
                walg = _latest_ops_event(cur, "walg_basebackup")
                pg_dump = _latest_ops_event(cur, "pg_dump")
                restore = _latest_ops_event(cur, "restore_drill")
    except Exception as exc:  # noqa: BLE001 — doctor must never crash the CLI
        return {
            "ok": False,
            "reason": f"backup_health query failed: {exc}",
            "max_age_hours": BACKUP_MAX_AGE_HOURS,
        }

    # Freshness: either WAL-G or pg_dump within window is enough
    candidates = [e for e in (walg, pg_dump) if e and e.get("status") == "ok"]
    ages = [e["age_seconds"] for e in candidates if e.get("age_seconds") is not None]
    freshest = min(ages) if ages else None
    backup_fresh = freshest is not None and freshest <= max_age
    restore_ok = bool(restore and restore.get("status") == "ok")

    reasons = []
    if not candidates:
        reasons.append("no successful walg_basebackup or pg_dump event recorded")
    elif not backup_fresh:
        reasons.append(
            f"last successful backup age {freshest:.0f}s exceeds {BACKUP_MAX_AGE_HOURS}h"
        )
    if not restore:
        reasons.append("no restore_drill event recorded yet")
    elif not restore_ok:
        reasons.append(f"last restore_drill status={restore.get('status')}")

    return {
        "ok": backup_fresh and restore_ok,
        "max_age_hours": BACKUP_MAX_AGE_HOURS,
        "freshest_backup_age_seconds": freshest,
        "walg_basebackup": walg,
        "pg_dump": pg_dump,
        "restore_drill": restore,
        "reasons": reasons,
    }


def _lag_seconds(later, earlier):
    """Seconds between two timestamps (later - earlier); None if either is missing.

    Pure arithmetic, deliberately separated from the SQL that fetches the two
    timestamps so it can be unit tested without a database (F8).
    """
    if later is None or earlier is None:
        return None
    return (later - earlier).total_seconds()


def status_payload(
    memory_root: Path | None = None,
    *,
    conflict_sample: int | None = 0,
    include_drift: bool = False,
) -> dict:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 'episodes' AS t, COUNT(*) FROM episodes
                UNION ALL SELECT 'topics', COUNT(*) FROM topics WHERE deleted_at IS NULL
                UNION ALL SELECT 'topics_tombstoned', COUNT(*) FROM topics WHERE deleted_at IS NOT NULL
                UNION ALL SELECT 'nodes', COUNT(*) FROM nodes
                UNION ALL SELECT 'edges', COUNT(*) FROM edges
                UNION ALL SELECT 'embeddings', COUNT(*) FROM memory_embeddings
                UNION ALL SELECT 'embeddings_legacy_nodes', COUNT(*) FROM embeddings
                UNION ALL SELECT 'topic_revisions', COUNT(*) FROM topic_revisions
                """
            )
            counts = {r[0]: int(r[1]) for r in cur.fetchall()}
            cur.execute("SELECT now(), MAX(ts), MAX(ingested_at) FROM episodes")
            now_ts, max_ts, max_ing = cur.fetchone()
            # Most recently *captured* episode (by ts, not ingested_at) — its own
            # ingested_at - ts is the true capture->PG mirror latency (F2). Distinct
            # from "now - MAX(ingested_at)" below, which is a liveness/idle signal
            # (time since PG last received any row), not mirror speed.
            cur.execute(
                """
                SELECT ts, ingested_at
                FROM episodes
                WHERE ts IS NOT NULL AND ingested_at IS NOT NULL
                ORDER BY ts DESC
                LIMIT 1
                """
            )
            latest_capture_row = cur.fetchone()
    latest_capture_ts, latest_capture_ingested_at = (
        latest_capture_row if latest_capture_row is not None else (None, None)
    )
    out = {
        "counts": counts,
        "latest_episode_ts": max_ts.isoformat() if max_ts else None,
        "latest_ingested_at": max_ing.isoformat() if max_ing else None,
        # Time since PG last received *any* row. Was mislabeled "ingest_lag_seconds"
        # and shown on the desktop as "Mirror lag" (F2) -- it is not mirror speed.
        "time_since_last_capture_seconds": _lag_seconds(now_ts, max_ing),
        # True mirror lag: ingested_at - ts for the most recently captured episode.
        # This is what the desktop "Mirror lag" KPI must display.
        "mirror_lag_seconds": _lag_seconds(
            latest_capture_ingested_at, latest_capture_ts
        ),
        "dsn_ok": True,
        "dsn_source": _dsn_source(),
    }
    try:
        from khipu.activity import recent_episodes

        out["recent_captures"] = recent_episodes(limit=5)
    except Exception as exc:  # noqa: BLE001 — status must stay resilient
        out["recent_captures_error"] = str(exc)
    if memory_root:
        from khipu.revisions import conflict_report

        # Default conflict_sample=0: PG revision stats only (skip NAS file hash walk).
        out["conflicts"] = conflict_report(memory_root, sample=conflict_sample)
        if include_drift:
            out["drift"] = sample_drift(memory_root)
    return out


def _dsn_source() -> str:
    if (os.environ.get("KHIPU_DATABASE_URL") or "").strip():
        return "env"
    try:
        from khipu.keychain import get_dsn

        if get_dsn():
            return "keychain"
    except Exception:
        pass
    # Ask paths for the dsn file rather than assuming ~/.config/khipu: the data
    # dir is relocatable, and resolve_dsn() already follows the pointer — so a
    # relocated dir used to connect fine while Status reported dsn_source
    # "none" (audit 2026-08-17, same shape as secrets_status).
    override = (os.environ.get("KHIPU_DSN_FILE") or "").strip()
    if override:
        path = Path(override)
    else:
        try:
            from khipu.paths import dsn_file

            path = dsn_file()
        except Exception:
            path = Path.home() / ".config/khipu/dsn"
    if path.is_file():
        return "file"
    return "none"
