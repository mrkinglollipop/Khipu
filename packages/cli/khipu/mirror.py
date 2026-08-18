"""Fail-open write-through mirror: legacy capture → Khipu Postgres."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _log(msg: str) -> None:
    print(f"[khipu-mirror] {msg}", file=sys.stderr)


def _ensure_path() -> None:
    from khipu.paths import repo_root

    root = repo_root()
    for p in (root / "packages" / "cli", root / ".python_libs"):
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)


def topic_content_hash(text: str) -> str:
    """The one definition of a topic's content_hash.

    The writer, the drift check and the conflict report had each grown their own
    sha256 call. PR #43 changed the drift check to hash raw bytes while the
    writer kept hashing decoded text, and text mode translates CRLF to LF — so a
    topic file with Windows line endings would report a hash_mismatch that no
    reconcile could ever clear, because the writer would keep storing the
    translated hash the checker refused to match. Decoded text is canonical
    here because Postgres already holds the writer's hashes.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_topic_text(path: Path) -> str | None:
    """A topic file's text, or None when it is missing or not readable as UTF-8.

    A caller that walks a directory must distinguish None-because-unreadable
    from absent: dropping an unreadable file out of the reconcile's ``seen``
    list tombstones it, and a tombstone removes the topic from search, from
    MEMORY.md, and (via embed.backfill) deletes its vectors.
    """
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def parse_topic_file(path: Path) -> dict[str, str] | None:
    """Parse a topic markdown file into the one canonical topic shape (F3).

    Both the capture-time mirror and the nightly reconcile must go through this
    parser — the P2 audit's F3 was exactly the drift between the two writers'
    shapes (frontmatter stripped vs raw, title parsed vs slug). content_hash is
    over the FULL file text (frontmatter included) so it stays comparable with
    the drift/conflict checks, which hash the file as-is.
    """
    text = read_topic_text(path)
    if text is None:
        return None
    slug = path.stem
    digest = topic_content_hash(text)
    title = slug
    status = "active"
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].splitlines():
                if line.startswith("title:"):
                    title = line.split(":", 1)[1].strip().strip("\"'")
                if line.startswith("status:"):
                    status = line.split(":", 1)[1].strip()
            body = parts[2].lstrip("\n")
    return {
        "slug": slug,
        "title": title,
        "status": status,
        "body": body,
        "digest": digest,
    }


def _upsert_topic(
    cur,
    parsed: dict[str, str],
    source_path: str,
    *,
    source: str,
    note: str,
) -> bool:
    """Upsert one parsed topic; write a revision row only when content changed.

    Clears deleted_at unconditionally: a file that exists (we are holding its
    parse) is by definition not deleted — this is the un-tombstone path when a
    previously deleted topic file reappears.
    Returns True when a revision row was written (content changed).
    """
    cur.execute("SELECT content_hash FROM topics WHERE slug = %s", (parsed["slug"],))
    prev = cur.fetchone()
    cur.execute(
        """
        INSERT INTO topics
          (slug, title, body, status, updated_at, frontmatter, source_path, content_hash)
        VALUES
          (%s, %s, %s, %s, now(), '{}'::jsonb, %s, %s)
        ON CONFLICT (slug) DO UPDATE SET
          title = EXCLUDED.title,
          body = EXCLUDED.body,
          status = EXCLUDED.status,
          updated_at = now(),
          source_path = EXCLUDED.source_path,
          content_hash = EXCLUDED.content_hash,
          deleted_at = NULL
        """,
        (
            parsed["slug"],
            parsed["title"],
            parsed["body"],
            parsed["status"],
            source_path,
            parsed["digest"],
        ),
    )
    changed = prev is None or prev[0] != parsed["digest"]
    if changed:
        cur.execute(
            """
            INSERT INTO topic_revisions (slug, body, source, note, content_hash)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (parsed["slug"], parsed["body"], source, note, parsed["digest"]),
        )
    return changed


def _upsert_episode(cur, payload: dict[str, Any]) -> bool:
    """Insert one episode; no-op when the (ts, md5(summary)) identity exists.

    Bare ON CONFLICT DO NOTHING is unambiguous here: id is never supplied, so
    the only unique constraint reachable is uq_episodes_ts_summary_md5
    (0003_reconcile_upsert.sql). Returns True when a row was inserted.
    """
    cur.execute(
        """
        INSERT INTO episodes
          (ts, session_id, summary, topics, people, decisions,
           preferences, scope, edges, raw)
        VALUES
          (%s::timestamptz, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
           %s::jsonb, %s, %s::jsonb, %s::jsonb)
        ON CONFLICT DO NOTHING
        """,
        (
            payload.get("ts") or datetime.now(timezone.utc).isoformat(),
            payload.get("session_id"),
            payload.get("summary") or "",
            json.dumps(payload.get("topics") or []),
            json.dumps(payload.get("people") or []),
            json.dumps(payload.get("decisions") or []),
            json.dumps(payload.get("preferences") or []),
            payload.get("scope"),
            json.dumps(payload.get("edges") or []),
            json.dumps(payload, ensure_ascii=False),
        ),
    )
    return cur.rowcount > 0


def mirror_episode(payload: dict[str, Any]) -> bool:
    """Insert one episode row. Returns True on success (or already mirrored)."""
    _ensure_path()
    from khipu.db import connect

    summary = (payload.get("summary") or "").strip()
    if not summary:
        return False
    with connect() as conn:
        with conn.cursor() as cur:
            _upsert_episode(cur, payload)
        conn.commit()
    return True


def mirror_topic_file(path: Path) -> bool:
    """Upsert a topic markdown file into topics (+ revision row on change)."""
    _ensure_path()
    from khipu.db import connect

    parsed = parse_topic_file(path)
    if parsed is None:
        return False
    with connect() as conn:
        with conn.cursor() as cur:
            _upsert_topic(
                cur,
                parsed,
                str(path),
                source="mirror",
                note="capture/consolidate write-through",
            )
        conn.commit()
    return True


def mirror_after_capture(
    payload: dict[str, Any],
    *,
    topic_paths: list[Path] | None = None,
) -> float | None:
    """
    Fail-open mirror after legacy capture succeeded.
    Returns elapsed seconds on success, None if skipped/failed.
    """
    if os.environ.get("KHIPU_MIRROR", "1").strip().lower() in {"0", "false", "off"}:
        return None
    t0 = time.perf_counter()
    try:
        mirror_episode(payload)
        for p in topic_paths or []:
            try:
                mirror_topic_file(Path(p))
            except Exception as e:  # noqa: BLE001 — fail-open per topic
                _log(f"topic mirror failed ({p}): {e}")
        # Embed-on-mirror (P3 step 3): the mirror is the legacy/dual fail-open
        # leg; embed only when explicitly enabled so capture_v2 stays cheap.
        if os.environ.get("KHIPU_MIRROR_EMBED", "0").strip().lower() in {"1", "true", "on"}:
            from khipu.embed import embed_on_capture

            embed_on_capture(payload)
        elapsed = time.perf_counter() - t0
        _log(f"ok episode mirror in {elapsed:.3f}s")
        return elapsed
    except Exception as e:  # noqa: BLE001 — fail-open, but never silent: queue it
        _log(f"WARN fail-open (legacy capture kept): {e}")
        try:
            from khipu.outbox import enqueue

            enqueue(payload, reason=f"mirror {type(e).__name__}")
        except Exception as qe:  # noqa: BLE001
            _log(f"WARN outbox enqueue failed: {qe}")
        return None


def sync_recent_episodes(memory_root: Path, *, tail_lines: int = 40) -> dict[str, int]:
    """Cheap tail sync for the harness Stop hook (P3 step 4, 2026-08-17).

    The legacy capture hooks (capture_v2) do the LLM extraction and write the
    file line; their fail-open PG mirror can miss (no Keychain in the hook's
    env, network blip — the Codex case). Rather than run a SECOND extraction
    (double LLM cost, double-capture risk), the Khipu hook re-reads only the
    last ``tail_lines`` of episodes.jsonl and inserts whatever is missing in
    PG by identity, then embeds them. Same idempotent anti-join as the full
    reconcile, ~100 ms instead of ~40 s. Never touches topics or tombstones.
    """
    _ensure_path()
    from khipu.db import connect
    from khipu.drift import episodes_missing_in_pg

    path = memory_root / "episodes.jsonl"
    stats = {"tail": 0, "inserted": 0, "embedded": 0}
    if not path.is_file():
        return stats
    # Read only the tail without loading the whole file.
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        block = min(size, 256 * 1024)
        f.seek(size - block)
        raw = f.read().decode("utf-8", errors="replace")
    lines = [ln for ln in raw.splitlines() if ln.strip()][-tail_lines:]
    objs: list[dict[str, Any]] = []
    keys: list[tuple[str, str]] = []
    for ln in lines:
        try:
            obj = json.loads(ln)
        except ValueError:
            continue  # first line may be a torn partial from the seek
        ts = obj.get("ts")
        if not ts:
            continue
        objs.append(obj)
        keys.append((ts, hashlib.md5((obj.get("summary") or "").encode("utf-8")).hexdigest()))
    stats["tail"] = len(keys)
    if not keys:
        return stats
    inserted_payloads: list[dict[str, Any]] = []
    with connect() as conn:
        with conn.cursor() as cur:
            missing = set(episodes_missing_in_pg(cur, keys))
            for obj, key in zip(objs, keys):
                if key in missing and _upsert_episode(cur, obj):
                    stats["inserted"] += 1
                    inserted_payloads.append(obj)
        conn.commit()
    if inserted_payloads:
        try:
            from khipu.embed import embed_on_capture

            stats["embedded"] = sum(1 for p in inserted_payloads if embed_on_capture(p))
        except Exception as e:  # noqa: BLE001 — vectors are fail-open
            _log(f"tail-sync embed skipped: {e}")
    return stats


# Circuit breaker for the tombstone sweep: a deletion this large is far more
# likely to be a bad read of topics/ than a real bulk retirement.
MASS_TOMBSTONE_RATIO = float(os.environ.get("KHIPU_MASS_TOMBSTONE_RATIO", "0.2"))
MASS_TOMBSTONE_MIN = int(os.environ.get("KHIPU_MASS_TOMBSTONE_MIN", "10"))


def reconcile_memory_root(memory_root: Path) -> dict[str, int]:
    """Idempotent files → PG sync: upsert episodes/topics, tombstone deletions.

    P2b replacement for the truncate-and-reload reconcile (audit F1). PG-only
    rows survive (hub-mode writes, archived-out episodes), episode ids stay
    stable, and topic_revisions is never wiped.

    Deletion semantics (plan.md P2b, decided 2026-08-10):
      - A topic file deliberately deleted from THIS memory root's topics/ dir
        gets its PG row tombstoned (deleted_at = now()), never hard-deleted.
      - The sweep only runs when topics/ is reachable, and only over rows whose
        source_path lies under this root — an unmounted volume or a topic owned
        by another Mac's root is left intact ("Asymmetric disks" rule).
      - A tombstoned topic whose file reappears is un-tombstoned by the upsert.
    """
    _ensure_path()
    from khipu.db import connect

    from khipu.drift import episode_files

    episode_paths = episode_files(memory_root)
    topics_dir = memory_root / "topics"
    stats = {
        "episodes": 0,
        "episodes_new": 0,
        "topics": 0,
        "topics_changed": 0,
        "topics_unreadable": 0,
        "tombstoned": 0,
    }
    with connect() as conn:
        with conn.cursor() as cur:
            if episode_paths:
                # Anti-join first, insert only the missing lines: a plain
                # per-line ON CONFLICT upsert burns one sequence id per
                # attempted row (nextval fires before the conflict check) and
                # one network round-trip per line — ~4k wasted ids and ~3 min
                # per nightly run against the database.
                from khipu.drift import episodes_missing_in_pg, is_smoke_row

                objs: list[dict[str, Any]] = []
                keys: list[tuple[str, str]] = []
                for episodes_path in episode_paths:
                    with episodes_path.open(encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                obj = json.loads(line)
                            except ValueError:
                                continue
                            ts = obj.get("ts")
                            if is_smoke_row(obj):
                                continue
                            if not ts:
                                # No identity → skip. Inserting with now() (the
                                # old behavior) re-inserts the line on EVERY run.
                                stats.setdefault("episodes_skipped_no_ts", 0)
                                stats["episodes_skipped_no_ts"] += 1
                                continue
                            summary = obj.get("summary") or ""
                            objs.append(obj)
                            keys.append(
                                (ts, hashlib.md5(summary.encode("utf-8")).hexdigest())
                            )
                            stats["episodes"] += 1
                missing = set(episodes_missing_in_pg(cur, keys))
                for obj, key in zip(objs, keys):
                    if key in missing and _upsert_episode(cur, obj):
                        stats["episodes_new"] += 1
            if topics_dir.is_dir():
                seen: list[str] = []
                for path in sorted(topics_dir.glob("*.md")):
                    if path.name.startswith("_"):
                        continue
                    parsed = parse_topic_file(path)
                    if parsed is None:
                        # glob() just listed it, so None means present-but-
                        # unreadable. It still counts as seen: leaving it out
                        # would hand it to the tombstone sweep below and delete
                        # a topic whose only sin is bad bytes.
                        stats["topics_unreadable"] += 1
                        seen.append(path.stem)
                        continue
                    if _upsert_topic(
                        cur,
                        parsed,
                        str(path),
                        source="reconcile",
                        note="nightly reconcile upsert",
                    ):
                        stats["topics_changed"] += 1
                    seen.append(parsed["slug"])
                    stats["topics"] += 1
                # Tombstone sweep. `topics_dir.is_dir()` alone is NOT enough of a
                # guard: a volume that mounts but presents an EMPTY topics/ dir
                # passes it, `seen` comes back empty, and `NOT (slug = ANY('{}'))`
                # is true for every row — one bad read tombstones the entire
                # corpus. That is not just a flag: every read filters
                # `deleted_at IS NULL`, so topics vanish from search and
                # MEMORY.md, and `embed.backfill` then DELETES their vectors,
                # making recovery a paid re-embed of the whole corpus.
                # So the sweep is circuit-broken (audit 2026-08-17): it refuses
                # to run on an empty read, and refuses a deletion that is too
                # large to be plausible. A real bulk retirement can still be
                # applied by re-running with KHIPU_ALLOW_MASS_TOMBSTONE=1.
                cur.execute(
                    "SELECT COUNT(*) FROM topics WHERE deleted_at IS NULL AND source_path LIKE %s",
                    (str(topics_dir) + "/%",),
                )
                live_under_root = int(cur.fetchone()[0])
                would_delete = max(0, live_under_root - len(set(seen)))
                override = os.environ.get("KHIPU_ALLOW_MASS_TOMBSTONE", "").strip().lower() in {"1", "true", "yes"}
                if not seen and live_under_root:
                    stats["tombstoned"] = 0
                    stats["tombstone_skipped"] = (
                        f"topics/ read as empty while PG holds {live_under_root} live "
                        "topic(s) under this root — refusing to tombstone the corpus"
                    )
                elif not override and would_delete > MASS_TOMBSTONE_MIN and \
                        live_under_root and would_delete > live_under_root * MASS_TOMBSTONE_RATIO:
                    stats["tombstoned"] = 0
                    stats["tombstone_skipped"] = (
                        f"{would_delete} of {live_under_root} topics would be tombstoned "
                        f"(> {MASS_TOMBSTONE_RATIO:.0%}) — refusing; set "
                        "KHIPU_ALLOW_MASS_TOMBSTONE=1 if this is intended"
                    )
                else:
                    cur.execute(
                        """
                        UPDATE topics
                        SET deleted_at = now(), updated_at = now()
                        WHERE deleted_at IS NULL
                          AND source_path LIKE %s
                          AND NOT (slug = ANY(%s))
                        """,
                        (str(topics_dir) + "/%", seen),
                    )
                    stats["tombstoned"] = cur.rowcount
        conn.commit()
    return stats
