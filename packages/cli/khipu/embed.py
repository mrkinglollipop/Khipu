"""Vectors for real — P3 step 3 (2026-08-17) + Gemini Embedding 2 profile (2026-08-19)
+ native media (PNG/JPEG) under Embedding 2 (2026-08-20).

Embeds episodes, topics, and opted-in media into ``memory_embeddings`` under a
named profile (``embedding_profiles``), per the plan.md "Embedding profiles"
rules: profile-tagged rows, never overwritten in place, one active pointer for
search.

Entrypoints:
  backfill(..., profile=None)  — missing/changed episode+topic chunks
  backfill_media(...)          — PNG/JPEG under sources with embed_media
  activate(profile)            — flip the one-active pointer (coverage gate; text only)
  embed_on_capture(payload)    — one episode under the *active* profile; fail-open
  semantic_search(query, k)    — cosine oversample + token RRF over the *active* profile
  coverage(profile=None)       — per-kind coverage for active or named profile

Providers:
  gemini-embedding-001 @768 — no task prefixes (task_type era); text only
  gemini-embedding-2 @768   — document/query prefixes for text; native image parts
                              for media (no text prefixes). Always outputDimensionality
                              768. One content per request (v2 aggregates multi-part).
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Iterable

from khipu.search_text import hybrid_rerank
from khipu.snippets import FETCH_LIMIT, LABEL_LIMIT, SNIPPET_LIMIT, clip_snippet

MODEL_001 = "gemini-embedding-001"
MODEL_2 = "gemini-embedding-2"
DIM = 768
PROFILE_001 = f"{MODEL_001}@{DIM}"
PROFILE_2 = f"{MODEL_2}@{DIM}"
# Legacy aliases — tests and callers that still import PROFILE_ID / MODEL.
MODEL = MODEL_001
PROFILE_ID = PROFILE_001

# Profile id → Gemini model name. Unknown ids refuse rather than guess.
_PROFILE_MODELS: dict[str, str] = {
    PROFILE_001: MODEL_001,
    PROFILE_2: MODEL_2,
}

CHUNK_CHARS = 6000
CHUNK_OVERLAP = 300
BATCH = 64            # Gemini batchEmbedContents cap is 100; stay under
MAX_TEXT_CHARS = 8000  # per-item safety, mirrors embed_mirror.embed_text
# Google Embedding 2 image table (docs 2026-06-22): PNG and JPEG only.
MEDIA_MIME: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
# Skip oversized inline payloads; File API would be a later cut.
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MEDIA_PILOT_WITHOUT_YES = 1000


def _log(msg: str) -> None:
    print(f"[khipu-embed] {msg}", file=sys.stderr, flush=True)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(1024 * 1024)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def mime_for_image_path(path: Path) -> str | None:
    return MEDIA_MIME.get(path.suffix.lower())


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


def prefix_document(text: str, *, title: str = "") -> str:
    """Asymmetric retrieval document prefix for gemini-embedding-2."""
    return f"title: {title or ''} | text: {text}"


def prefix_query(query: str) -> str:
    """Asymmetric retrieval query prefix for gemini-embedding-2."""
    return f"task: search result | query: {query}"


def model_for_profile(profile: str) -> str:
    model = _PROFILE_MODELS.get(profile)
    if not model:
        raise ValueError(
            f"unknown embedding profile {profile!r}; known: {sorted(_PROFILE_MODELS)}"
        )
    return model


def uses_task_prefixes(profile: str) -> bool:
    return profile == PROFILE_2


# ---- provider -----------------------------------------------------------------

def _gemini_key() -> str:
    from khipu.keychain import resolve_gemini_key

    key = resolve_gemini_key()
    if not key:
        raise RuntimeError("Gemini API key not found (Keychain / env / file)")
    return key


def embed_batch(
    texts: list[str],
    *,
    profile: str = PROFILE_001,
    retries: int = 4,
) -> list[list[float]]:
    """Embed up to BATCH texts; L2-normalized; dim-checked.

    ``texts`` must already include any v2 task prefixes — callers store the
    unprefixed chunk_text separately.
    """
    if not texts:
        return []
    model = model_for_profile(profile)
    key = _gemini_key()
    # Header auth, not ?key=. The query-string form puts a live API key inside a
    # URL that any future logging, proxy, or exception-formatting change would
    # surface — HTTPError carries .url, and this module logs exceptions on the
    # capture path. The header form is equivalent to Gemini and leaks nothing
    # (audit 2026-08-17).
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:batchEmbedContents"
    # One content per request — gemini-embedding-2 aggregates multi-part inputs
    # into a single vector; we need one vector per chunk.
    body = {
        "requests": [
            {
                "model": f"models/{model}",
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


def embed_one(text: str, *, profile: str = PROFILE_001) -> list[float]:
    return embed_batch([text], profile=profile)[0]


def embed_batch_images(
    images: list[tuple[bytes, str]],
    *,
    profile: str = PROFILE_2,
    retries: int = 4,
) -> list[list[float]]:
    """Embed PNG/JPEG bytes as native Gemini Embedding 2 image parts.

    Each item is ``(raw_bytes, mime_type)``. Always requests ``outputDimensionality``
    768. Does **not** apply text task prefixes. One image → one request → one vector
    (v2 aggregates multi-part content).
    """
    if not images:
        return []
    if profile != PROFILE_2 and not profile.endswith("@768"):
        # Media is locked to Embedding 2 @768 for this cut.
        model_for_profile(profile)
    model = model_for_profile(profile)
    key = _gemini_key()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:batchEmbedContents"
    requests_body = []
    for raw, mime in images:
        if mime not in ("image/png", "image/jpeg"):
            raise ValueError(f"unsupported image mime {mime!r}; PNG/JPEG only")
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} bytes inline limit")
        requests_body.append(
            {
                "model": f"models/{model}",
                "content": {
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": mime,
                                "data": base64.b64encode(raw).decode("ascii"),
                            }
                        }
                    ]
                },
                "outputDimensionality": DIM,
            }
        )
    data = json.dumps({"requests": requests_body}).encode("utf-8")
    delay = 2.0
    payload: dict[str, Any] = {}
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": key},
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                _log(f"embed-image HTTP {e.code}, retry in {delay:.0f}s")
                time.sleep(delay)
                delay *= 2
                continue
            raise RuntimeError(f"embed-image HTTP {e.code}: {err[:400]}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < retries:
                _log(f"embed-image network error ({type(e).__name__}), retry in {delay:.0f}s")
                time.sleep(delay)
                delay *= 2
                continue
            raise RuntimeError(
                f"embed-image network error after {retries} retries: {type(e).__name__}: {e}"
            ) from e
    vecs = [item["values"] for item in payload["embeddings"]]
    if len(vecs) != len(images):
        raise RuntimeError(f"embed-image returned {len(vecs)} vectors for {len(images)} images")
    for v in vecs:
        if len(v) != DIM:
            raise RuntimeError(f"expected dim {DIM}, got {len(v)}")
    return [_l2(v) for v in vecs]


def _iter_media_files(root: Path) -> Iterable[Path]:
    """Yield PNG/JPEG files under ``root`` (skip dirs that are not readable)."""
    if not root.is_dir():
        return
    try:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                p = Path(dirpath) / name
                if mime_for_image_path(p):
                    yield p
    except OSError as e:
        _log(f"walk skipped {root}: {e}")


def _upsert_media_asset(
    cur,
    *,
    source_id: str,
    path: str,
    sha256: str,
    mime: str,
    nbytes: int,
) -> str:
    cur.execute(
        "SELECT id, sha256 FROM media_assets WHERE source_id = %s AND path = %s",
        (source_id, path),
    )
    row = cur.fetchone()
    if row:
        mid, old_hash = row[0], row[1]
        if old_hash != sha256:
            cur.execute(
                "UPDATE media_assets SET sha256 = %s, mime = %s, bytes = %s WHERE id = %s",
                (sha256, mime, nbytes, mid),
            )
        return mid
    mid = str(uuid.uuid4())
    cur.execute(
        """
        INSERT INTO media_assets (id, source_id, path, sha256, mime, bytes)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (mid, source_id, path, sha256, mime, nbytes),
    )
    return mid


def backfill_media(
    *,
    dry_run: bool = False,
    yes: bool = False,
    limit: int | None = None,
    profile: str | None = None,
    source_id: str | None = None,
) -> dict[str, Any]:
    """Embed PNG/JPEG under sources with ``embed_media`` and a walkable root.

    Requires ``--yes`` when more than MEDIA_PILOT_WITHOUT_YES files would be
    scanned. Activate / doctor text coverage ignore media missing.
    """
    from khipu.db import connect
    from khipu.sources import sources_with_embed_media

    stats: dict[str, Any] = {
        "scanned": 0,
        "embedded": 0,
        "skipped_unchanged": 0,
        "skipped_oversized": 0,
        "skipped_missing": 0,
        "batches": 0,
        "profile": None,
        "would_embed": 0,
        "sources": [],
        "needs_yes": False,
    }
    sources = sources_with_embed_media()
    if source_id:
        sources = [s for s in sources if s.get("id") == source_id]
    candidates: list[tuple[str, Path, str]] = []  # source_id, path, rel_label
    for s in sources:
        sid = str(s["id"])
        root = Path(str(s["root"]))
        stats["sources"].append(sid)
        if not root.is_dir():
            _log(f"path-unreachable (skip, no purge): {sid} -> {root}")
            continue
        for p in _iter_media_files(root):
            try:
                rel = str(p.relative_to(root))
            except ValueError:
                rel = p.name
            candidates.append((sid, p, rel))
            if limit is not None and len(candidates) >= limit:
                break
        if limit is not None and len(candidates) >= limit:
            break

    stats["scanned"] = len(candidates)
    if len(candidates) > MEDIA_PILOT_WITHOUT_YES and not yes and not dry_run:
        stats["needs_yes"] = True
        stats["error"] = (
            f"{len(candidates)} images exceed pilot of {MEDIA_PILOT_WITHOUT_YES}; "
            "re-run with --yes"
        )
        return stats

    with connect() as conn:
        with conn.cursor() as cur:
            profile = _resolve_profile(cur, profile or PROFILE_2)
            if profile != PROFILE_2:
                # Still allow named @768 Gemini-2 only for this cut.
                if "gemini-embedding-2" not in profile:
                    raise ValueError(
                        f"media backfill requires gemini-embedding-2@768; got {profile!r}"
                    )
            stats["profile"] = profile
            existing = _existing_hashes(cur, profile)
            todo: list[tuple[str, Path, str, str, str, bytes]] = []
            # (source_id, path, rel, mime, sha, raw)
            for sid, path, rel in candidates:
                mime = mime_for_image_path(path)
                if not mime:
                    continue
                try:
                    nbytes = path.stat().st_size
                except OSError:
                    stats["skipped_missing"] += 1
                    continue
                if nbytes > MAX_IMAGE_BYTES:
                    stats["skipped_oversized"] += 1
                    continue
                try:
                    raw = path.read_bytes()
                except OSError:
                    stats["skipped_missing"] += 1
                    continue
                sha = _sha256_bytes(raw)
                mid_probe = None
                cur.execute(
                    "SELECT id FROM media_assets WHERE source_id = %s AND path = %s",
                    (sid, rel),
                )
                prow = cur.fetchone()
                if prow:
                    mid_probe = prow[0]
                    if existing.get(("media", mid_probe, 0)) == sha:
                        stats["skipped_unchanged"] += 1
                        continue
                todo.append((sid, path, rel, mime, sha, raw))

            stats["would_embed"] = len(todo)
            if dry_run:
                return stats

            batch: list[tuple[str, Path, str, str, str, bytes]] = []
            for item in todo:
                batch.append(item)
                if len(batch) >= min(BATCH, 16):  # images are heavier; keep batches smaller
                    _flush_media_batch(cur, profile, batch, stats)
                    conn.commit()
                    batch = []
            if batch:
                _flush_media_batch(cur, profile, batch, stats)
                conn.commit()
    return stats


def _flush_media_batch(
    cur,
    profile: str,
    batch: list[tuple[str, Path, str, str, str, bytes]],
    stats: dict[str, Any],
) -> None:
    images = [(raw, mime) for _sid, _p, _rel, mime, _sha, raw in batch]
    vecs = embed_batch_images(images, profile=profile)
    rows: list[tuple[str, str, int, str, str, list[float]]] = []
    for (sid, _path, rel, mime, sha, raw), vec in zip(batch, vecs):
        mid = _upsert_media_asset(
            cur,
            source_id=sid,
            path=rel,
            sha256=sha,
            mime=mime,
            nbytes=len(raw),
        )
        label = rel
        rows.append(("media", mid, 0, label, sha, vec))
    _upsert_chunks(cur, profile, rows)
    stats["embedded"] += len(batch)
    stats["batches"] += 1


# ---- corpus -------------------------------------------------------------------

def _active_profile(cur) -> str:
    cur.execute("SELECT id FROM embedding_profiles WHERE is_active LIMIT 1")
    row = cur.fetchone()
    if not row:
        raise RuntimeError("no active embedding profile (apply 0004_embedding_profiles.sql)")
    return row[0]


def _resolve_profile(cur, profile: str | None) -> str:
    if profile:
        profile = profile.strip()
        cur.execute("SELECT id FROM embedding_profiles WHERE id = %s", (profile,))
        if not cur.fetchone():
            raise ValueError(
                f"embedding profile {profile!r} not in embedding_profiles "
                f"(apply 0005_gemini_embedding_2.sql if targeting {PROFILE_2})"
            )
        # Refuse unknown model wiring even if a rogue row exists.
        model_for_profile(profile)
        return profile
    return _active_profile(cur)


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


def _iter_sources(cur, *, kind: str | None = None) -> Iterable[tuple[str, str, str, str]]:
    """Yield (kind, ref, text, title) for every embeddable row.

    ``title`` is used only for gemini-embedding-2 document prefixes; stored
    ``chunk_text`` stays the unprefixed ``text``.
    """
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
                yield "episode", str(eid), text, ""
    if kind in (None, "topic"):
        cur.execute("SELECT slug, title, body FROM topics WHERE deleted_at IS NULL ORDER BY slug")
        for slug, title, body in cur.fetchall():
            text = topic_text(slug, title, body)
            if text:
                yield "topic", slug, text, (title or slug or "")


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


def _api_texts(
    profile: str, chunks: list[tuple[str, str]]
) -> list[str]:
    """Map (title, unprefixed_chunk) → API payload texts for this profile."""
    if uses_task_prefixes(profile):
        return [prefix_document(chunk, title=title) for title, chunk in chunks]
    return [chunk for _, chunk in chunks]


def backfill(
    *,
    kind: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    profile: str | None = None,
) -> dict[str, Any]:
    """Embed every missing / changed chunk under active or named profile. Idempotent."""
    from khipu.db import connect

    stats: dict[str, Any] = {
        "scanned": 0, "chunks": 0, "embedded": 0, "skipped_unchanged": 0,
        "batches": 0, "orphans_removed": 0, "profile": None,
    }
    with connect() as conn:
        with conn.cursor() as cur:
            profile = _resolve_profile(cur, profile)
            stats["profile"] = profile
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
            # (kind, ref, idx, unprefixed_chunk, hash, title)
            todo: list[tuple[str, str, int, str, str, str]] = []
            for k, ref, text, title in _iter_sources(cur, kind=kind):
                stats["scanned"] += 1
                for i, chunk in enumerate(chunk_text(text)):
                    stats["chunks"] += 1
                    h = _md5(chunk)
                    if have.get((k, ref, i)) == h:
                        stats["skipped_unchanged"] += 1
                        continue
                    todo.append((k, ref, i, chunk, h, title))
                if limit and len(todo) >= limit:
                    todo = todo[:limit]
                    break
            _log(f"profile={profile} to_embed={len(todo)} unchanged={stats['skipped_unchanged']}")
            if dry_run:
                stats["would_embed"] = len(todo)
                return stats
            for start in range(0, len(todo), BATCH):
                batch = todo[start : start + BATCH]
                # todo rows: (kind, ref, idx, chunk, hash, title)
                api = _api_texts(profile, [(title, chunk) for _k, _r, _i, chunk, _h, title in batch])
                vecs = embed_batch(api, profile=profile)
                _upsert_chunks(
                    cur, profile,
                    [(k, r, i, chunk, h, v)
                     for (k, r, i, chunk, h, _title), v in zip(batch, vecs)],
                )
                conn.commit()
                stats["embedded"] += len(batch)
                stats["batches"] += 1
                if stats["batches"] % 10 == 0:
                    _log(f"  {stats['embedded']}/{len(todo)}")
    return stats


def activate(profile: str, *, force: bool = False) -> dict[str, Any]:
    """Flip the one-active pointer to ``profile`` after coverage is complete.

    Refuses when the target profile has missing episode/topic refs unless
    ``force=True``. Search uses exactly one active profile.
    """
    from khipu.db import connect

    profile = (profile or "").strip()
    model_for_profile(profile)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM embedding_profiles WHERE id = %s", (profile,))
            if not cur.fetchone():
                raise ValueError(f"unknown embedding profile {profile!r}")
            cov = coverage(profile=profile)
            missing = (
                cov["episodes"]["missing"] + cov["topics"]["missing"]
            )
            if missing and not force:
                raise RuntimeError(
                    f"refusing to activate {profile}: "
                    f"{cov['episodes']['missing']} episodes and "
                    f"{cov['topics']['missing']} topics still missing vectors "
                    f"(pass force=True to override)"
                )
            cur.execute("UPDATE embedding_profiles SET is_active = false WHERE is_active")
            cur.execute(
                "UPDATE embedding_profiles SET is_active = true WHERE id = %s",
                (profile,),
            )
            conn.commit()
    return {"ok": True, "active_profile": profile, "coverage": cov, "forced": bool(force)}


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
                api = _api_texts(profile, [("", c) for c in chunks])
                vecs = embed_batch(api, profile=profile)
                _upsert_chunks(
                    cur, profile,
                    [("episode", eid, i, c, _md5(c), v)
                     for i, (c, v) in enumerate(zip(chunks, vecs))],
                )
            conn.commit()
        _log(f"embed-on-capture ok episode={eid} chunks={len(chunks)} profile={profile}")
        return True
    except Exception as exc:  # noqa: BLE001 — fail-open: capture is already durable
        _log(f"embed-on-capture skipped: {type(exc).__name__}: {exc}")
        return False


def semantic_search(
    query: str, *, limit: int = 10, kind: str | None = None
) -> list[dict[str, Any]]:
    """Cosine oversample over the active profile, then token-overlap RRF.

    Returns kind/id/score/snippet + label. Cosine alone packed relevant and
    irrelevant episodes into a ~0.02 band; fusing query-term hits on the
    embedded chunk window (``CHUNK_CHARS``, not the ``FETCH_LIMIT`` teaser
    fetch) lifts rows that actually name the question without a second embed
    call.
    """
    from khipu.db import connect

    query = (query or "").strip()
    if not query:
        return []
    want = max(1, min(int(limit), 50))
    fetch = min(50, max(want * 5, 25))
    with connect() as conn:
        with conn.cursor() as cur:
            profile = _active_profile(cur)
            api_q = prefix_query(query) if uses_task_prefixes(profile) else query
            qlit = _vec_literal(embed_one(api_q, profile=profile))
            cur.execute("SELECT to_regclass('public.media_assets') IS NOT NULL")
            has_media = bool(cur.fetchone()[0])
            media_label = (
                "(SELECT COALESCE(a.path, m.chunk_text) FROM media_assets a WHERE a.id = m.ref)"
                if has_media
                else "m.chunk_text"
            )
            # Teasers stay at FETCH_LIMIT. RRF rank_text uses CHUNK_CHARS so
            # extract fields appended after a long summary are not clipped off
            # the lexical side (FETCH_LIMIT still clips some extract headers
            # on long summaries; CHUNK_CHARS covers all single-chunk episode
            # embeddings today).
            cur.execute(
                f"""
                SELECT m.kind, m.ref, m.chunk_idx,
                       1 - (m.embedding <=> %(q)s::vector) AS score,
                       left(m.chunk_text, %(fetch)s) AS snippet,
                       left(m.chunk_text, %(rank_fetch)s) AS rank_src,
                       CASE m.kind
                         WHEN 'topic' THEN
                           (SELECT COALESCE(t.title, t.slug) FROM topics t WHERE t.slug = m.ref)
                         WHEN 'media' THEN
                           {media_label}
                         ELSE
                           (SELECT e.summary FROM episodes e WHERE e.id::text = m.ref)
                       END AS label
                FROM memory_embeddings m
                WHERE m.profile = %(p)s
                  AND (%(kind)s::text IS NULL OR m.kind = %(kind)s)
                ORDER BY m.embedding <=> %(q)s::vector
                LIMIT %(lim)s
                """,
                {"q": qlit, "p": profile, "kind": kind, "lim": fetch,
                 "fetch": FETCH_LIMIT, "rank_fetch": CHUNK_CHARS},
            )
            out = []
            for k, r, i, s, snip, rank_src, lbl in cur.fetchall():
                # Episodes: snippet from the stored summary, not chunk_text
                # (which appends decisions/topics and used to mid-word clip at 200).
                # RRF ranks on rank_src (full embed window) so extract fields
                # that were already embedded can lift a hit the teaser never names.
                snippet_src = lbl if k == "episode" and lbl else snip
                out.append({
                    "kind": k, "id": r, "chunk_idx": i, "score": round(float(s), 4),
                    "label": clip_snippet(lbl or snip, LABEL_LIMIT),
                    "snippet": clip_snippet(snippet_src, SNIPPET_LIMIT),
                    "rank_text": rank_src or snip or "",
                })
            ranked = hybrid_rerank(out, query, limit=want)
            for row in ranked:
                row.pop("rank_text", None)
            return ranked


def coverage(*, profile: str | None = None) -> dict[str, Any]:
    """Per-kind coverage for active or named profile — the 'status UI that can't lie'."""
    from khipu.db import connect

    with connect() as conn:
        with conn.cursor() as cur:
            profile = _resolve_profile(cur, profile)
            cur.execute("SELECT COUNT(*) FROM episodes")
            eps = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM topics WHERE deleted_at IS NULL")
            tops = cur.fetchone()[0]
            cur.execute("SELECT to_regclass('public.media_assets') IS NOT NULL")
            has_media = bool(cur.fetchone()[0])
            medias = 0
            if has_media:
                cur.execute("SELECT COUNT(*) FROM media_assets")
                medias = cur.fetchone()[0]
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
            cur.execute("SELECT id FROM embedding_profiles WHERE is_active LIMIT 1")
            active_row = cur.fetchone()
            active = active_row[0] if active_row else None
    e = by.get("episode", {"refs": 0, "chunks": 0})
    t = by.get("topic", {"refs": 0, "chunks": 0})
    m = by.get("media", {"refs": 0, "chunks": 0})
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
        "profile": profile,
        "active_profile": active,
        "profiles": profiles,
        "episodes": {"total": eps, "embedded": e["refs"], "missing": max(0, eps - e["refs"]),
                     "chunks": e["chunks"], "pct": _pct(e["refs"], eps)},
        "topics": {"total": tops, "embedded": t["refs"], "missing": max(0, tops - t["refs"]),
                   "chunks": t["chunks"], "pct": _pct(t["refs"], tops)},
        # Denominator = media_assets rows (registered files). Partial media does
        # not fail activate() or doctor embed_coverage_ok (episode+topic only).
        "media": {"total": medias, "embedded": m["refs"], "missing": max(0, medias - m["refs"]),
                  "chunks": m["chunks"], "pct": _pct(m["refs"], medias)},
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
                api = _api_texts(profile, [("", t) for _, _, _, t, _ in batch])
                vecs = embed_batch(api, profile=profile)
                _upsert_chunks(cur, profile,
                               [(k, r, i, t, h, v) for (k, r, i, t, h), v in zip(batch, vecs)])
                conn.commit()
                out["chunks"] += len(batch)
    return out
