"""Vectors for real — P3 step 3 (2026-08-17).

Embeds episodes and topics into ``memory_embeddings`` under the ACTIVE profile
(``embedding_profiles.is_active``), per the plan.md "Embedding profiles" rules:
profile-tagged rows, never overwritten in place, one active pointer for search.

Three entrypoints:
  backfill(...)              — every episode/topic whose (profile, kind, ref) chunk is
                               missing or whose content_hash changed. Batched.
  embed_on_capture(payload)  — one episode, called from ``khipu capture`` after the PG
                               write. Fail-open by design: the capture is already
                               durable; a vector miss is healed by the next backfill.
  semantic_search(query, k)  — cosine top-k over the active profile.

Provider: gemini-embedding-001 @ output_dimensionality=768, L2-normalized before
store and before query (both directions, plan lock). Uses ``batchEmbedContents``
so a full-corpus backfill (~4.6k chunks) is a few dozen calls, not thousands.
Text is chunked at ~6k chars with a small overlap so long topic pages don't get
truncated to their first screen.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Iterable

MODEL = "gemini-embedding-001"
DIM = 768
PROFILE_ID = f"{MODEL}@{DIM}"
CHUNK_CHARS = 6000
CHUNK_OVERLAP = 300
BATCH = 64            # Gemini batchEmbedContents cap is 100; stay under
MAX_TEXT_CHARS = 8000  # per-item safety, mirrors embed_mirror.embed_text


def _log(msg: str) -> None:
    print(f"[khipu-embed] {msg}", file=sys.stderr, flush=True)


def _l2(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"


def chunk_text(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= CHUNK_CHARS:
        return [text]
    out: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + CHUNK_CHARS)
        out.append(text[start:end])
        if end == len(text):
            break
        start = end - CHUNK_OVERLAP
    return out


# ---- provider -----------------------------------------------------------------

def _gemini_key() -> str:
    from khipu.keychain import resolve_gemini_key

    key = resolve_gemini_key()
    if not key:
        raise RuntimeError("Gemini API key not found (Keychain / env / file)")
    return key


def embed_batch(texts: list[str], *, retries: int = 4) -> list[list[float]]:
    """Embed up to BATCH texts in one call; L2-normalized; dim-checked."""
    if not texts:
        return []
    key = _gemini_key()
    # Header auth, not ?key=. The query-string form puts a live API key inside a
    # URL that any future logging, proxy, or exception-formatting change would
    # surface — HTTPError carries .url, and this module logs exceptions on the
    # capture path. The header form is equivalent to Gemini and leaks nothing
    # (audit 2026-08-17).
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:batchEmbedContents"
    body = {
        "requests": [
            {
                "model": f"models/{MODEL}",
                "content": {"parts": [{"text": t[:MAX_TEXT_CHARS]}]},
                "outputDimensionality": DIM,
            }
            for t in texts
        ]
    }
    data = json.dumps(body).encode("utf-8")
    delay = 2.0
    payload: dict[str, Any] = {}
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": key},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                _log(f"embed HTTP {e.code}, retry in {delay:.0f}s")
                time.sleep(delay)
                delay *= 2
                continue
            raise RuntimeError(f"embed HTTP {e.code}: {err[:400]}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # Only HTTP status codes were retried, so a DNS blip or a dropped
            # connection failed a whole batch outright while a 503 got four
            # tries. The backfill walks thousands of chunks; transient network
            # failure is the common case, not the exotic one (audit 2026-08-17).
            if attempt < retries:
                _log(f"embed network error ({type(e).__name__}), retry in {delay:.0f}s")
                time.sleep(delay)
                delay *= 2
                continue
            raise RuntimeError(f"embed network error after {retries} retries: {type(e).__name__}: {e}") from e
    vecs = [item["values"] for item in payload["embeddings"]]
    if len(vecs) != len(texts):
        raise RuntimeError(f"embed returned {len(vecs)} vectors for {len(texts)} texts")
    for v in vecs:
        if len(v) != DIM:
            raise RuntimeError(f"expected dim {DIM}, got {len(v)}")
    return [_l2(v) for v in vecs]


def embed_one(text: str) -> list[float]:
    return embed_batch([text])[0]


# ---- corpus -------------------------------------------------------------------

def _active_profile(cur) -> str:
    cur.execute("SELECT id FROM embedding_profiles WHERE is_active LIMIT 1")
    row = cur.fetchone()
    if not row:
        raise RuntimeError("no active embedding profile (apply 0004_embedding_profiles.sql)")
    return row[0]


def episode_text(row: dict[str, Any]) -> str:
    """What we embed for an episode: summary + decisions/preferences/topics as context."""
    parts = [row.get("summary") or ""]
    for key in ("decisions", "preferences", "topics", "people"):
        vals = row.get(key) or []
        if isinstance(vals, list) and vals:
            parts.append(f"{key}: " + "; ".join(str(v) for v in vals))
    return "\n".join(p for p in parts if p).strip()


def topic_text(slug: str, title: str | None, body: str | None) -> str:
    return f"{title or slug}\n\n{body or ''}".strip()


def _iter_sources(cur, *, kind: str | None = None) -> Iterable[tuple[str, str, str]]:
    """Yield (kind, ref, text) for every embeddable row."""
    if kind in (None, "episode"):
        cur.execute(
            "SELECT id, summary, decisions, preferences, topics, people FROM episodes ORDER BY id"
        )
        for eid, summary, decisions, prefs, topics, people in cur.fetchall():
            text = episode_text(
                {"summary": summary, "decisions": decisions, "preferences": prefs,
                 "topics": topics, "people": people}
            )
            if text:
                yield "episode", str(eid), text
    if kind in (None, "topic"):
        cur.execute("SELECT slug, title, body FROM topics WHERE deleted_at IS NULL ORDER BY slug")
        for slug, title, body in cur.fetchall():
            text = topic_text(slug, title, body)
            if text:
                yield "topic", slug, text


def _existing_hashes(cur, profile: str) -> dict[tuple[str, str, int], str]:
    cur.execute(
        "SELECT kind, ref, chunk_idx, content_hash FROM memory_embeddings WHERE profile = %s",
        (profile,),
    )
    return {(k, r, i): h for k, r, i, h in cur.fetchall()}


def _upsert_chunks(
    cur, profile: str, rows: list[tuple[str, str, int, str, str, list[float]]]
) -> None:
    for kind, ref, idx, text, h, vec in rows:
        cur.execute(
            """
            INSERT INTO memory_embeddings
              (profile, kind, ref, chunk_idx, chunk_text, content_hash, embedding, built_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s::vector, now())
            ON CONFLICT (profile, kind, ref, chunk_idx) DO UPDATE SET
              chunk_text = EXCLUDED.chunk_text,
              content_hash = EXCLUDED.content_hash,
              embedding = EXCLUDED.embedding,
              built_at = now()
            """,
            (profile, kind, ref, idx, text, h, _vec_literal(vec)),
        )


def backfill(
    *, kind: str | None = None, limit: int | None = None, dry_run: bool = False
) -> dict[str, int]:
    """Embed every missing / changed chunk under the active profile. Idempotent."""
    from khipu.db import connect

    stats = {"scanned": 0, "chunks": 0, "embedded": 0, "skipped_unchanged": 0,
             "batches": 0, "orphans_removed": 0}
    with connect() as conn:
        with conn.cursor() as cur:
            profile = _active_profile(cur)
            # ref is polymorphic (episodes.id | topics.slug) so it cannot carry an FK;
            # sweep vectors whose source row is gone or tombstoned so coverage never
            # over-reports (plan rule 4: a status that can't lie).
            if not dry_run:
                cur.execute(
                    """
                    DELETE FROM memory_embeddings m
                    WHERE m.profile = %s AND (
                      (m.kind = 'episode' AND NOT EXISTS
                         (SELECT 1 FROM episodes e WHERE e.id::text = m.ref))
                      OR (m.kind = 'topic' AND NOT EXISTS
                         (SELECT 1 FROM topics t WHERE t.slug = m.ref AND t.deleted_at IS NULL))
                    )
                    """,
                    (profile,),
                )
                stats["orphans_removed"] = cur.rowcount
                conn.commit()
            have = _existing_hashes(cur, profile)
            todo: list[tuple[str, str, int, str, str]] = []
            for k, ref, text in _iter_sources(cur, kind=kind):
                stats["scanned"] += 1
                for i, chunk in enumerate(chunk_text(text)):
                    stats["chunks"] += 1
                    h = _md5(chunk)
                    if have.get((k, ref, i)) == h:
                        stats["skipped_unchanged"] += 1
                        continue
                    todo.append((k, ref, i, chunk, h))
                if limit and len(todo) >= limit:
                    todo = todo[:limit]
                    break
            _log(f"profile={profile} to_embed={len(todo)} unchanged={stats['skipped_unchanged']}")
            if dry_run:
                stats["would_embed"] = len(todo)
                return stats
            for start in range(0, len(todo), BATCH):
                batch = todo[start : start + BATCH]
                vecs = embed_batch([t for _, _, _, t, _ in batch])
                _upsert_chunks(
                    cur, profile,
                    [(k, r, i, t, h, v) for (k, r, i, t, h), v in zip(batch, vecs)],
                )
                conn.commit()
                stats["embedded"] += len(batch)
                stats["batches"] += 1
                if stats["batches"] % 10 == 0:
                    _log(f"  {stats['embedded']}/{len(todo)}")
    return stats


def embed_on_capture(payload: dict[str, Any]) -> bool:
    """Embed one just-captured episode by identity. Fail-open; returns True on success."""
    if os.environ.get("KHIPU_EMBED_ON_CAPTURE", "1").strip().lower() in {"0", "false", "off"}:
        return False
    try:
        from khipu.db import connect

        summary = (payload.get("summary") or "").strip()
        ts = payload.get("ts")
        if not summary or not ts:
            return False
        with connect() as conn:
            with conn.cursor() as cur:
                profile = _active_profile(cur)
                cur.execute(
                    "SELECT id FROM episodes WHERE ts = %s::timestamptz AND md5(summary) = %s",
                    (ts, _md5(summary)),
                )
                row = cur.fetchone()
                if not row:
                    _log("embed-on-capture: episode not found by identity; backfill will catch it")
                    return False
                eid = str(row[0])
                chunks = chunk_text(episode_text(payload))
                vecs = embed_batch(chunks)
                _upsert_chunks(
                    cur, profile,
                    [("episode", eid, i, c, _md5(c), v)
                     for i, (c, v) in enumerate(zip(chunks, vecs))],
                )
            conn.commit()
        _log(f"embed-on-capture ok episode={eid} chunks={len(chunks)}")
        return True
    except Exception as exc:  # noqa: BLE001 — fail-open: capture is already durable
        _log(f"embed-on-capture skipped: {type(exc).__name__}: {exc}")
        return False


def semantic_search(
    query: str, *, limit: int = 10, kind: str | None = None
) -> list[dict[str, Any]]:
    """Cosine top-k over the active profile. Returns kind/id/score/snippet + label."""
    from khipu.db import connect

    query = (query or "").strip()
    if not query:
        return []
    qlit = _vec_literal(embed_one(query))
    with connect() as conn:
        with conn.cursor() as cur:
            profile = _active_profile(cur)
            cur.execute(
                """
                SELECT m.kind, m.ref, m.chunk_idx,
                       1 - (m.embedding <=> %(q)s::vector) AS score,
                       left(m.chunk_text, 200) AS snippet,
                       CASE m.kind
                         WHEN 'topic' THEN
                           (SELECT COALESCE(t.title, t.slug) FROM topics t WHERE t.slug = m.ref)
                         ELSE
                           (SELECT left(e.summary, 80) FROM episodes e WHERE e.id::text = m.ref)
                       END AS label
                FROM memory_embeddings m
                WHERE m.profile = %(p)s
                  AND (%(kind)s::text IS NULL OR m.kind = %(kind)s)
                ORDER BY m.embedding <=> %(q)s::vector
                LIMIT %(lim)s
                """,
                {"q": qlit, "p": profile, "kind": kind, "lim": max(1, min(int(limit), 50))},
            )
            return [
                {"kind": k, "id": r, "chunk_idx": i, "score": round(float(s), 4),
                 "label": lbl, "snippet": snip}
                for k, r, i, s, snip, lbl in cur.fetchall()
            ]


def coverage() -> dict[str, Any]:
    """Per-kind coverage for the active profile — the 'status UI that can't lie'."""
    from khipu.db import connect

    with connect() as conn:
        with conn.cursor() as cur:
            profile = _active_profile(cur)
            cur.execute("SELECT COUNT(*) FROM episodes")
            eps = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM topics WHERE deleted_at IS NULL")
            tops = cur.fetchone()[0]
            cur.execute(
                "SELECT kind, COUNT(DISTINCT ref), COUNT(*) FROM memory_embeddings "
                "WHERE profile = %s GROUP BY kind",
                (profile,),
            )
            by = {k: {"refs": int(r), "chunks": int(c)} for k, r, c in cur.fetchall()}
            cur.execute(
                "SELECT id, model, dim, is_active FROM embedding_profiles ORDER BY created_at"
            )
            profiles = [
                {"id": i, "model": m, "dim": d, "active": a} for i, m, d, a in cur.fetchall()
            ]
    e = by.get("episode", {"refs": 0, "chunks": 0})
    t = by.get("topic", {"refs": 0, "chunks": 0})
    def _pct(done: int, total: int) -> float:
        """Never round a gap away. 4335/4336 printed as "100.0" during the
        2026-08-17 audit and hid an unembedded episode; only true completeness
        may read 100."""
        if not total:
            return 0.0
        if done >= total:
            return 100.0
        return min(99.9, int(1000 * done / total) / 10)

    return {
        "active_profile": profile,
        "profiles": profiles,
        "episodes": {"total": eps, "embedded": e["refs"], "missing": max(0, eps - e["refs"]),
                     "chunks": e["chunks"], "pct": _pct(e["refs"], eps)},
        "topics": {"total": tops, "embedded": t["refs"], "missing": max(0, tops - t["refs"]),
                   "chunks": t["chunks"], "pct": _pct(t["refs"], tops)},
    }


def embed_recent_missing(limit: int = 10) -> dict[str, int]:
    """Embed the newest episodes that have no vector under the active profile.

    The tail sync embeds rows *it* inserts, but a row the legacy mirror leg wrote
    lands in PG already-present and stayed unembedded until the nightly — so a
    just-captured episode was invisible to semantic search for up to a day (found
    by audit 2026-08-17). Bounded and cheap enough to run on every Stop."""
    from khipu.db import connect

    out = {"embedded": 0, "chunks": 0}
    with connect() as conn:
        with conn.cursor() as cur:
            profile = _active_profile(cur)
            cur.execute(
                "SELECT e.id, e.summary, e.decisions, e.preferences, e.topics, e.people"
                " FROM episodes e WHERE NOT EXISTS ("
                "  SELECT 1 FROM memory_embeddings m"
                "  WHERE m.profile = %s AND m.kind = 'episode' AND m.ref = e.id::text)"
                " ORDER BY e.ts DESC LIMIT %s",
                (profile, limit),
            )
            todo: list[tuple[str, str, int, str, str]] = []
            for eid, summary, decisions, prefs, topics, people in cur.fetchall():
                text = episode_text({"summary": summary, "decisions": decisions,
                                     "preferences": prefs, "topics": topics, "people": people})
                if not text:
                    continue
                out["embedded"] += 1
                for i, chunk in enumerate(chunk_text(text)):
                    todo.append(("episode", str(eid), i, chunk, _md5(chunk)))
            for start in range(0, len(todo), BATCH):
                batch = todo[start : start + BATCH]
                vecs = embed_batch([t for _, _, _, t, _ in batch])
                _upsert_chunks(cur, profile,
                               [(k, r, i, t, h, v) for (k, r, i, t, h), v in zip(batch, vecs)])
                conn.commit()
                out["chunks"] += len(batch)
    return out
