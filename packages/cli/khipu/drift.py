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


def _postgres_backup_mode() -> str | None:
    try:
        from khipu.components_matrix import read_versions

        postgres = read_versions().get("postgres")
        if isinstance(postgres, dict):
            mode = str(postgres.get("mode") or "").strip()
            return mode or None
    except Exception:  # noqa: BLE001
        return None
    return None


def backup_health() -> dict:
    """Doctor backup-age + last restore (reads ops_events via DSN).

    Portable local Docker installs record ``pg_dump`` + ``restore_drill`` during
    Welcome bootstrap — those events satisfy ``backup_ok`` the same as Linode
    WAL-G + drill; no separate WAL-G requirement on ``local_docker`` hosts.
    """
    max_age = BACKUP_MAX_AGE_HOURS * 3600
    mode = _postgres_backup_mode()
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
                        "postgres_mode": mode,
                    }
                walg = _latest_ops_event(cur, "walg_basebackup")
                pg_dump = _latest_ops_event(cur, "pg_dump")
                restore = _latest_ops_event(cur, "restore_drill")
    except Exception as exc:  # noqa: BLE001 — doctor must never crash the CLI
        return {
            "ok": False,
            "reason": f"backup_health query failed: {exc}",
            "max_age_hours": BACKUP_MAX_AGE_HOURS,
            "postgres_mode": mode,
        }

    # Freshness: WAL-G or pg_dump within window. Local bootstrap writes pg_dump.
    candidates = [e for e in (walg, pg_dump) if e and e.get("status") == "ok"]
    if mode == "local_docker":
        candidates = [e for e in (pg_dump,) if e and e.get("status") == "ok"] or candidates
    ages = [e["age_seconds"] for e in candidates if e.get("age_seconds") is not None]
    freshest = min(ages) if ages else None
    backup_fresh = freshest is not None and freshest <= max_age
    restore_ok = bool(restore and restore.get("status") == "ok")

    reasons = []
    if not candidates:
        if mode == "local_docker":
            reasons.append(
                "no successful local pg_dump event recorded "
                "(run Welcome backup bootstrap or khipu components bootstrap-local-backup)"
            )
        else:
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
        "postgres_mode": mode,
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


# ---------------------------------------------------------------------------
# W6.2 — recall-quality metrics (memory reliability, 2026-09-03). Bounded SQL
# over the last 30 days, surfaced in `khipu status`/`khipu doctor` as a
# `recall_quality` block. Every metric carries its own {value, threshold, ok}
# so a caller can see at a glance which of the six root-cause shapes (A-F in
# the scope doc) is currently trending wrong — but per the scope, only the
# probe (W6.1) and snapshot freshness are hard gates on doctor's overall ok;
# these ratios are surfaced and warned on, never gate the exit code.
# ---------------------------------------------------------------------------

RECALL_QUALITY_WINDOW_DAYS = 30


def _metric(value, threshold, *, ok: bool, note: str = "") -> dict:
    """One {value, threshold, ok} metric row. `ok` is computed by the caller
    (direction — lte vs gte — differs per metric) so this is just the shape."""
    out = {"value": value, "threshold": threshold, "ok": ok}
    if note:
        out["note"] = note
    return out


def _has_column(cur, table: str, column: str) -> bool:
    """Consolidated onto ``db.table_columns`` (shares the process cache and
    the information_schema round trip with embed/hub_snapshot)."""
    try:
        from khipu.db import has_columns

        return has_columns(cur, table, column)
    except Exception:  # noqa: BLE001 — a failed introspection reads as "not there yet"
        return False


def _table_exists(cur, table: str) -> bool:
    try:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",))
        return bool(cur.fetchone()[0])
    except Exception:  # noqa: BLE001
        return False


def _one_episode_session_ratio(cur) -> dict:
    cur.execute(
        """
        WITH s AS (
            SELECT session_id, COUNT(*) AS n
            FROM episodes
            WHERE ts >= now() - interval '%s days' AND session_id IS NOT NULL
            GROUP BY session_id
        )
        SELECT COUNT(*) FILTER (WHERE n = 1), COUNT(*) FROM s
        """
        % RECALL_QUALITY_WINDOW_DAYS
    )
    one, total = cur.fetchone()
    ratio = (one / total) if total else None
    threshold = 0.85
    ok = ratio is None or ratio <= threshold
    return _metric(ratio, threshold, ok=ok,
                    note=f"sessions_with_one_episode={one} sessions_total={total}")


def _cross_session_pairs_5min(cur) -> dict:
    has_project = _has_column(cur, "episodes", "project")
    has_parent = _has_column(cur, "episodes", "parent_session_id")
    if not (has_project and has_parent):
        return _metric(None, 0.01, ok=True,
                        note="episodes.project/parent_session_id not present yet (pre-0008)")
    cur.execute(
        """
        WITH recent AS (
            SELECT id, session_id, ts, project, parent_session_id
            FROM episodes
            WHERE ts >= now() - interval '%s days'
        )
        SELECT COUNT(*)
        FROM recent a
        JOIN recent b
          ON a.id < b.id
         AND a.session_id IS DISTINCT FROM b.session_id
         AND abs(extract(epoch FROM (a.ts - b.ts))) <= 300
        WHERE NOT (a.project IS NOT NULL AND a.project = b.project)
          AND NOT (a.parent_session_id IS NOT NULL AND a.parent_session_id = b.session_id)
          AND NOT (b.parent_session_id IS NOT NULL AND b.parent_session_id = a.session_id)
          AND NOT (a.parent_session_id IS NOT NULL AND a.parent_session_id = b.parent_session_id)
        """
        % RECALL_QUALITY_WINDOW_DAYS
    )
    pairs = int(cur.fetchone()[0])
    cur.execute(
        "SELECT COUNT(*) FROM episodes WHERE ts >= now() - interval '%s days'"
        % RECALL_QUALITY_WINDOW_DAYS
    )
    episodes_n = int(cur.fetchone()[0])
    ratio = (pairs / episodes_n) if episodes_n else None
    threshold = 0.01
    ok = ratio is None or ratio <= threshold
    return _metric(pairs, threshold, ok=ok,
                    note=f"ratio={round(ratio, 4) if ratio is not None else None} episodes={episodes_n}")


def _dangling_topic_ratio(cur) -> dict:
    if not _has_column(cur, "episodes", "tags"):
        return _metric(None, 0.05, ok=True, note="episodes.tags not present yet (pre-0010)")
    cur.execute(
        """
        SELECT COALESCE(SUM(jsonb_array_length(COALESCE(tags, '[]'::jsonb))), 0),
               COALESCE(SUM(jsonb_array_length(COALESCE(topics, '[]'::jsonb))), 0)
        FROM episodes
        WHERE ts >= now() - interval '%s days'
        """
        % RECALL_QUALITY_WINDOW_DAYS
    )
    tag_n, topic_n = (int(x) for x in cur.fetchone())
    denom = tag_n + topic_n
    ratio = (tag_n / denom) if denom else None
    threshold = 0.05
    ok = ratio is None or ratio <= threshold
    return _metric(ratio, threshold, ok=ok, note=f"tags={tag_n} topics={topic_n}")


def _junk_path_ratio(cur) -> dict:
    from khipu import hygiene

    report = hygiene.report_junk_paths(cur, sample_limit=0)
    total = report.get("total_path_nodes", 0)
    failing = report.get("failing", 0)
    ratio = (failing / total) if total else None
    threshold = 0.05
    ok = ratio is None or ratio <= threshold
    return _metric(ratio, threshold, ok=ok, note=f"failing={failing} total={total}")


def _commitments_counts(cur) -> tuple[dict, dict]:
    if not _table_exists(cur, "commitments"):
        na = _metric(None, None, ok=True, note="commitments table not present yet (pre-0009)")
        return na, na
    cur.execute("SELECT status, COUNT(*) FROM commitments GROUP BY status")
    counts = {status: int(n) for status, n in cur.fetchall()}
    open_n = counts.get("open", 0)
    stale_n = counts.get("stale", 0)
    # Recommended posture (scope §7 item 4): err toward leaving commitments
    # open rather than auto-closing wrong — so a nonzero stale count is
    # expected background noise, not itself unhealthy. It only warns once
    # stale rows outnumber open ones, which would suggest auto-close (or
    # someone reviewing `khipu owed --status stale`) has stopped happening.
    open_metric = _metric(open_n, None, ok=True)
    stale_ok = stale_n <= max(open_n, 1)
    stale_metric = _metric(stale_n, "stale <= open (heuristic)", ok=stale_ok)
    return open_metric, stale_metric


def _query_log_window(days: int) -> list[dict]:
    """Read-only pass over query_log.jsonl (khipu.query_log owns the writer;
    this only consumes the public log_path()/format it already documents)."""
    from datetime import datetime, timedelta, timezone

    from khipu.query_log import log_path

    path = log_path()
    if not path.is_file():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            ts_raw = entry.get("ts")
            try:
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if ts >= cutoff:
                out.append(entry)
    except OSError:
        return out
    return out


def _query_log_metrics(days: int = RECALL_QUALITY_WINDOW_DAYS) -> tuple[dict, dict]:
    entries = _query_log_window(days)
    total = len(entries)
    zero = sum(1 for e in entries if e.get("result_count") == 0)
    rate = (zero / total) if total else None
    zero_threshold = 0.2
    zero_ok = rate is None or rate <= zero_threshold
    zero_metric = _metric(rate, zero_threshold, ok=zero_ok, note=f"zero={zero} total={total}")

    slice_errors = sum(
        1 for e in entries if e.get("mode") == "slice" and e.get("result_count") == 0
    )
    slice_metric = _metric(slice_errors, 0, ok=slice_errors == 0)
    return zero_metric, slice_metric


def recall_quality(*, hub_snapshot: dict | None = None) -> dict:
    """The `recall_quality` block for `khipu status`/`khipu doctor` (W6.2).

    Opens its own connection (matching `backup_health()`'s style) so callers
    can drop this in without threading a cursor through. `hub_snapshot`, when
    given, is the block cmd_status/cmd_doctor already computed via
    `hub_snapshot.snapshot_freshness()` — reused here rather than recomputed.
    """
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                one_episode = _one_episode_session_ratio(cur)
                cross_session = _cross_session_pairs_5min(cur)
                dangling_topics = _dangling_topic_ratio(cur)
                junk_paths = _junk_path_ratio(cur)
                commitments_open, commitments_stale = _commitments_counts(cur)
    except Exception as exc:  # noqa: BLE001 — a failed check must not look like a pass
        err = f"{type(exc).__name__}: {exc}"
        na = _metric(None, None, ok=False, note=err)
        return {
            "error": err,
            "one_episode_session_ratio": na,
            "cross_session_pairs_5min": na,
            "dangling_topic_ratio": na,
            "junk_path_ratio": na,
            "commitments_open": na,
            "commitments_stale": na,
            "query_zero_result_rate": na,
            "slice_error_count": na,
            "snapshot_behind_ingest_seconds": na,
        }
    zero_result, slice_errors = _query_log_metrics()
    behind = None
    if hub_snapshot is not None:
        behind = _metric(
            hub_snapshot.get("behind_ingest_seconds"),
            None,
            ok=bool(hub_snapshot.get("ok", True)),
            note=hub_snapshot.get("reason") or "",
        )
    else:
        behind = _metric(None, None, ok=True, note="hub_snapshot not supplied by caller")
    return {
        "window_days": RECALL_QUALITY_WINDOW_DAYS,
        "one_episode_session_ratio": one_episode,
        "cross_session_pairs_5min": cross_session,
        "dangling_topic_ratio": dangling_topics,
        "junk_path_ratio": junk_paths,
        "commitments_open": commitments_open,
        "commitments_stale": commitments_stale,
        "query_zero_result_rate": zero_result,
        "slice_error_count": slice_errors,
        "snapshot_behind_ingest_seconds": behind,
    }


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
