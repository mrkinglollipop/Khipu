"""Recall hit counts (Phase 5 G6): the query log records each search's top
hit ids but nothing aggregates them per topic, so "what does recall actually
use" was unanswerable. ``topic_hits(slug, hits_30d, last_hit_at)``
(0023_organisation.sql) is the answer; this module keeps it current two ways,
the same changed-only/full split every other Phase 4/5 writer in this package
uses (khipu.notes.reconcile's changed_only vs full, most directly):

  - ``apply_incremental(cur)`` — Stop-hook-cheap: reads only the query log
    bytes written since a stored byte offset and ADDS their hits. Never
    decays a topic that goes quiet (that's what recompute is for), so this
    alone can only ever over-count relative to the true 30-day picture.
  - ``recompute(cur)`` — nightly, full: rescans the active log AND the one
    rotated file (a hit near a rotation can straddle both) for a correct
    30-day window, replacing every topic's row — a topic with none in the
    window reads as 0, not a stale leftover count. Also resets the
    incremental offset, so tonight's recompute and tomorrow's incremental
    catch-up never double-count the same lines.

Both are fail-open (never raise) and both pre-filter to slugs that actually
exist in ``topics`` before writing — ``topic_hits.slug`` is
``REFERENCES topics (slug) ON DELETE CASCADE``, and a hit logged for a topic
tombstoned/purged since would otherwise abort the whole batch on a foreign-key
violation.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from khipu import query_log

WINDOW_DAYS = 30


def _offset_state_path() -> Path:
    from khipu.paths import ensure_data_dir

    d = ensure_data_dir() / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d / "topic-hits-offset.json"


def _read_offset() -> int:
    try:
        data = json.loads(_offset_state_path().read_text(encoding="utf-8"))
        return int(data.get("offset", 0))
    except (OSError, ValueError, TypeError):
        return 0


def _write_offset(offset: int) -> None:
    try:
        tmp = _offset_state_path().with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"offset": int(offset)}), encoding="utf-8")
        tmp.replace(_offset_state_path())
    except OSError:
        pass


def _parse_ts(raw: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _iter_topic_hits_from_text(text: str):
    """Yield ``(slug, ts_or_None)`` for every topic hit in ``text`` — one
    ``query_log.jsonl`` line per search, each carrying up to 3 ``top`` hits
    (``khipu.query_log.log_query``). A malformed line is skipped, never
    raised — the log is append-only free text, not a contract."""
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        ts = _parse_ts(entry.get("ts"))
        for hit in entry.get("top") or []:
            if hit.get("kind") != "topic":
                continue
            slug = hit.get("id")
            if slug:
                yield str(slug), ts


def _existing_slugs(cur, slugs: list[str]) -> set[str]:
    if not slugs:
        return set()
    cur.execute("SELECT slug FROM topics WHERE slug = ANY(%s)", (slugs,))
    return {row[0] for row in cur.fetchall()}


def apply_incremental(cur) -> dict[str, Any]:
    """Stop-hook-cheap: only the bytes written to the active query log since
    the last call. Never raises; a missing/corrupt log or offset reads as
    zero new hits, and the offset resets to 0 (re-reading from the start)
    when the log has rotated (shrunk) under it rather than silently missing
    lines."""
    path = query_log.log_path()
    out: dict[str, Any] = {"ok": True, "new_bytes": 0, "topics_touched": 0}
    try:
        if not path.is_file():
            return out
        size = path.stat().st_size
        offset = _read_offset()
        if offset > size:
            offset = 0
        with path.open("r", encoding="utf-8") as fh:
            fh.seek(offset)
            new_text = fh.read()
        out["new_bytes"] = len(new_text.encode("utf-8"))
        counts: dict[str, tuple[int, datetime | None]] = {}
        for slug, ts in _iter_topic_hits_from_text(new_text):
            n, last = counts.get(slug, (0, None))
            if ts is not None and (last is None or ts > last):
                last = ts
            counts[slug] = (n + 1, last)
        live = _existing_slugs(cur, list(counts))
        for slug, (n, last) in counts.items():
            if slug not in live:
                continue
            cur.execute(
                """
                INSERT INTO topic_hits (slug, hits_30d, last_hit_at)
                VALUES (%s, %s, %s::timestamptz)
                ON CONFLICT (slug) DO UPDATE SET
                  hits_30d = topic_hits.hits_30d + EXCLUDED.hits_30d,
                  last_hit_at = GREATEST(
                    COALESCE(topic_hits.last_hit_at, EXCLUDED.last_hit_at),
                    COALESCE(EXCLUDED.last_hit_at, topic_hits.last_hit_at)
                  )
                """,
                (slug, n, last.isoformat() if last else None),
            )
        out["topics_touched"] = len(live)
        _write_offset(size)
    except Exception as exc:  # noqa: BLE001 — aggregation must never break the caller
        out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return out


def recompute(cur, *, days: int = WINDOW_DAYS) -> dict[str, Any]:
    """Nightly, full: the correct rolling window, scanning the active query
    log AND the one rotated file (``query_log.jsonl.1`` — a hit near a
    rotation boundary can straddle both). Replaces every topic's row: one
    with no hits in the window reads as 0, not a stale leftover count from
    an earlier, larger window. Also resets the incremental offset to the
    active log's current size, so the next Stop-hook catch-up starts
    counting from exactly where this recompute left off."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, int(days)))
    active = query_log.log_path()
    rotated = active.with_name(active.name + query_log.ROTATED_SUFFIX)
    counts: dict[str, tuple[int, datetime | None]] = {}
    for path in (rotated, active):
        try:
            text = path.read_text(encoding="utf-8") if path.is_file() else ""
        except OSError:
            text = ""
        for slug, ts in _iter_topic_hits_from_text(text):
            if ts is None or ts < cutoff:
                continue
            n, last = counts.get(slug, (0, None))
            if last is None or ts > last:
                last = ts
            counts[slug] = (n + 1, last)
    out: dict[str, Any] = {"ok": True, "topics": 0, "zeroed": 0}
    try:
        cur.execute("SELECT slug FROM topic_hits")
        existing = {row[0] for row in cur.fetchall()}
        live = _existing_slugs(cur, list(counts))
        for slug, (n, last) in counts.items():
            if slug not in live:
                continue
            cur.execute(
                """
                INSERT INTO topic_hits (slug, hits_30d, last_hit_at)
                VALUES (%s, %s, %s::timestamptz)
                ON CONFLICT (slug) DO UPDATE SET
                  hits_30d = EXCLUDED.hits_30d, last_hit_at = EXCLUDED.last_hit_at
                """,
                (slug, n, last.isoformat() if last else None),
            )
        out["topics"] = len(live)
        stale_zero = existing - set(counts.keys())
        for slug in stale_zero:
            cur.execute("UPDATE topic_hits SET hits_30d = 0 WHERE slug = %s", (slug,))
        out["zeroed"] = len(stale_zero)
        _write_offset(active.stat().st_size if active.is_file() else 0)
    except Exception as exc:  # noqa: BLE001 — aggregation must never break the caller
        out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return out
