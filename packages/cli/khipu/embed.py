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
COMMITMENT_CATCHUP_LIMIT = 5  # fix 5c: bounded, same spirit as the episode catch-up's limit=5 caller
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
        # Tombstoned episodes (khipu episode forget) must never be re-embedded;
        # pre-0010 hubs have no deleted_at column.
        from khipu.db import has_columns

        live = " WHERE deleted_at IS NULL" if has_columns(cur, "episodes", "deleted_at") else ""
        cur.execute(
            f"SELECT id, summary, decisions, preferences, topics, people FROM episodes{live} ORDER BY id"
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
                         (SELECT 1 FROM episodes e WHERE e.id::text = m.ref
                            AND e.deleted_at IS NULL))
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


def activate_welcome_embed(*, provider: str, profile: str | None = None) -> dict[str, Any]:
    """First-run embed activation — empty corpus is allowed (force=True)."""
    choice = (provider or "skip").strip().lower()
    if choice == "skip":
        return {"ok": True, "skipped": True}
    if choice != "cloud":
        return {
            "ok": True,
            "skipped": True,
            "note": "local embed profile stored; activate after backfill",
        }
    target = (profile or PROFILE_2).strip()
    return activate(target, force=True)


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
        # W2.4: keep the sqlite hub replica current without a full dump, so a
        # search that falls back to the snapshot (hub unreachable) sees this
        # episode too. Its own try/except so a snapshot hiccup never turns an
        # already-successful embed-on-capture into a reported failure.
        try:
            from datetime import datetime, timezone

            from khipu.hub_snapshot import upsert_episode

            episode_row = {
                "id": int(eid),
                "ts": ts,
                "session_id": payload.get("session_id"),
                "summary": summary,
                "topics": payload.get("topics"),
                "people": payload.get("people"),
                "decisions": payload.get("decisions"),
                "preferences": payload.get("preferences"),
                "scope": payload.get("scope"),
                # fix 9: identity columns ride the same incremental upsert so
                # a just-captured episode is filterable by project/session_id/
                # harness (fix 7) on the sqlite replica without waiting for
                # the next full `khipu snapshot refresh`.
                "harness": payload.get("harness"),
                "repo_root": payload.get("repo_root"),
                "project": payload.get("project"),
                "parent_session_id": payload.get("parent_session_id"),
                "transcript_range": payload.get("transcript_range"),
            }
            built_at = datetime.now(timezone.utc).isoformat()
            embedding_rows = [
                {
                    "profile": profile, "kind": "episode", "ref": eid, "chunk_idx": i,
                    "chunk_text": c, "content_hash": _md5(c), "embedding": v,
                    "built_at": built_at,
                }
                for i, (c, v) in enumerate(zip(chunks, vecs))
            ]
            snap = upsert_episode(episode_row, embedding_rows)
            if not snap.get("ok"):
                _log(f"snapshot upsert skipped: {snap.get('error')}")
        except Exception as exc:  # noqa: BLE001 — fail-open, one log line
            _log(f"snapshot upsert failed: {type(exc).__name__}: {exc}")
        return True
    except Exception as exc:  # noqa: BLE001 — fail-open: capture is already durable
        _log(f"embed-on-capture skipped: {type(exc).__name__}: {exc}")
        return False


def _cosine_candidates(
    query: str, *, limit: int, kind: str | None = None
) -> list[dict[str, Any]]:
    """Raw cosine-ordered candidates over the active profile (best first).

    Extracted from ``semantic_search`` (W2.1) so the hybrid engine
    (``hybrid_search``) can use this as one ranked list among several,
    without semantic_search's own token-overlap fusion baked in. Each row
    keeps ``rank_text`` (the embedded chunk window) so a caller can build a
    token-overlap-ordered list from the same candidates without a second
    query. Raises ``RuntimeError`` (from ``_active_profile``) when no
    embedding profile is active — callers decide whether that means
    "degrade" or "fail".

    ``kind=None`` means "any of the generic-search-eligible kinds", never
    "any kind at all" — ``memory_embeddings`` rows with ``kind = 'commitment'``
    (migration 0009 widened the constraint to allow them) are always
    excluded here regardless of ``kind``: a commitment's own label lookup
    below is episode-shaped and would resolve wrong, and commitments have
    their own dedicated surface (``khipu_owed``), not generic search.
    """
    from khipu.db import connect

    query = (query or "").strip()
    if not query:
        return []
    fetch = max(1, min(int(limit), 200))
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
                  AND m.kind != 'commitment'
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
    return out


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
    query = (query or "").strip()
    if not query:
        return []
    want = max(1, min(int(limit), 50))
    fetch = min(50, max(want * 5, 25))
    out = _cosine_candidates(query, limit=fetch, kind=kind)
    ranked = hybrid_rerank(out, query, limit=want)
    for row in ranked:
        row.pop("rank_text", None)
    return ranked


# ---- hybrid default retrieval (W2.1-W2.3) --------------------------------------

_SEMANTIC_KINDS = ("episode", "topic", "media")
_LITERAL_KINDS = ("episode", "topic", "node")


def _episode_schema_flags(cur) -> dict[str, bool]:
    """Which optional episode columns exist on THIS connection (W2.3).

    ``project``/``harness``/``parent_session_id`` (migration 0008) and
    ``deleted_at`` (migration 0010) may not be applied yet — resolved once
    per call (cached per process by ``db.table_columns``, not per row) and
    the caller falls back to ``scope`` / session_id-split / no soft-delete
    when absent. Consolidated onto ``db.table_columns`` (W-consolidation)
    so this, ``drift._has_column`` and ``hub_snapshot._pg_columns`` share one
    information_schema round trip per table.
    """
    from khipu.db import table_columns

    cols = table_columns(cur, "episodes")
    return {
        "project": "project" in cols,
        "deleted_at": "deleted_at" in cols,
        "harness": "harness" in cols,
        "parent_session_id": "parent_session_id" in cols,
    }


def _aware(ts):
    from datetime import timezone

    if ts is None:
        return None
    if getattr(ts, "tzinfo", None) is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def _neg_ts_sort_key(ts) -> float:
    """Sort key so a *newer* ts sorts first among equal-score rows (W2.2)."""
    aware = _aware(ts)
    if aware is None:
        return 0.0
    try:
        return -aware.timestamp()
    except (OverflowError, OSError, ValueError):
        return 0.0


def _apply_search_filters(
    cur,
    rows: list[dict[str, Any]],
    *,
    project: str | None = None,
    since: str | None = None,
    until: str | None = None,
    session_id: str | None = None,
    harness: str | None = None,
) -> list[dict[str, Any]]:
    """Post-fusion metadata filter + recency tiebreak, in one place for every
    mode (W2.3). project/session_id/harness only exist on episodes — a topic
    or node row is dropped when one of those is active rather than guessed at.
    ``deleted_at`` (once migration 0010 lands) always excludes tombstones.
    """
    from khipu.search_text import parse_time_filter

    if not rows:
        return rows
    since_dt = parse_time_filter(since) if since else None
    until_dt = parse_time_filter(until) if until else None

    flags = _episode_schema_flags(cur)
    episode_ids = [str(r["id"]) for r in rows if r.get("kind") == "episode"]
    topic_ids = [str(r["id"]) for r in rows if r.get("kind") == "topic"]
    node_ids = [str(r["id"]) for r in rows if r.get("kind") == "node"]

    meta: dict[tuple[str, str], dict[str, Any]] = {}
    if episode_ids:
        project_expr = "COALESCE(project, scope)" if flags["project"] else "scope"
        select_cols = f"id::text, ts, session_id, {project_expr}"
        extra_idx = 4
        deleted_idx = harness_idx = None
        if flags["deleted_at"]:
            select_cols += ", deleted_at IS NOT NULL"
            deleted_idx = extra_idx
            extra_idx += 1
        # W2.3/harness fix: filter on episodes.harness (migration 0008) when it
        # exists — the session_id-prefix split below is a fallback for a
        # pre-migration hub only, not the primary signal once the column is real.
        if flags["harness"]:
            select_cols += ", harness"
            harness_idx = extra_idx
            extra_idx += 1
        cur.execute(
            f"SELECT {select_cols} FROM episodes WHERE id::text = ANY(%s)",
            (episode_ids,),
        )
        for row in cur.fetchall():
            eid, ts, sid, proj = row[0], row[1], row[2], row[3]
            deleted = bool(row[deleted_idx]) if deleted_idx is not None else False
            harness_col = row[harness_idx] if harness_idx is not None else None
            meta[("episode", eid)] = {
                "ts": ts, "session_id": sid, "project": proj or "", "deleted": deleted,
                "harness": harness_col,
            }
    if topic_ids:
        cur.execute(
            "SELECT slug, COALESCE(updated_at, created_at) FROM topics WHERE slug = ANY(%s)",
            (topic_ids,),
        )
        for slug, ts in cur.fetchall():
            meta[("topic", slug)] = {"ts": ts}
    if node_ids:
        cur.execute("SELECT id, built_at FROM nodes WHERE id = ANY(%s)", (node_ids,))
        for nid, ts in cur.fetchall():
            meta[("node", nid)] = {"ts": ts}

    want_episode_only = bool(project or session_id or harness)
    out: list[dict[str, Any]] = []
    for r in rows:
        k, rid = r.get("kind"), str(r.get("id"))
        m = meta.get((k, rid), {})
        if k == "episode" and m.get("deleted"):
            continue
        if want_episode_only:
            if k != "episode":
                continue
            if project and project.strip().lower() not in (m.get("project") or "").lower():
                continue
            if session_id and not (m.get("session_id") or "").startswith(session_id):
                continue
            if harness:
                row_harness = m.get("harness") or (m.get("session_id") or "").split(":", 1)[0]
                if row_harness != harness:
                    continue
        ts = m.get("ts")
        if since_dt is not None and (ts is None or _aware(ts) < since_dt):
            continue
        if until_dt is not None and (ts is None or _aware(ts) > until_dt):
            continue
        r["_sort_ts"] = ts
        out.append(r)
    out.sort(key=lambda r: (-(r.get("score") or 0.0), _neg_ts_sort_key(r.get("_sort_ts"))))
    for r in out:
        r.pop("_sort_ts", None)
    return out


def _fair_fill(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Rank by score first; per-kind fairness only fills the tail (W2.2).

    Takes the top ``limit`` rows as scored. If any kind present anywhere in
    ``rows`` has zero rows in that top slice, backfills up to ``limit // 4``
    of its best-scoring rows from beyond the cut — dropping the worst-scoring
    rows in the top slice to make room, so the total stays ``<= limit``.
    """
    limit = max(1, int(limit))
    top = rows[:limit]
    rest = rows[limit:]
    if not rest:
        return top
    present = {r.get("kind") for r in top}
    all_kinds = {r.get("kind") for r in rows}
    missing = all_kinds - present
    backfill_cap = limit // 4
    if not missing or backfill_cap <= 0:
        return top
    added: list[dict[str, Any]] = []
    for k in missing:
        added.extend([r for r in rest if r.get("kind") == k][:backfill_cap])
    if not added:
        return top
    keep_n = max(0, len(top) - len(added))
    result = top[:keep_n] + added
    result.sort(key=lambda r: -(r.get("score") or 0.0))
    return result[:limit]


def hybrid_search(
    query: str,
    *,
    limit: int = 12,
    mode: str = "hybrid",
    kind: str | None = None,
    project: str | None = None,
    since: str | None = None,
    until: str | None = None,
    session_id: str | None = None,
    harness: str | None = None,
) -> dict[str, Any]:
    """Default retrieval engine (W2.1-W2.3): fused hybrid, or single-mode.

    mode='hybrid' (default): cosine oversample + token overlap (over the
    embedded text) + literal ILIKE, fused by ``search_text.fuse_ranked_lists``
    (generalized RRF), ranked by score with per-kind fairness filling only
    the tail (``_fair_fill``). If no embedding profile is active, the cosine
    and token-overlap lists are skipped and the result degrades to literal
    only, with ``degraded: "no-embedding"`` in the payload.
    mode='literal': ``cli._literal_candidates`` — a single flat pool ranked
    globally by (phrase-boost, hit count, ts), not the fair-share-partitioned
    ``cli._search_query`` (that per-kind split is right for a stand-alone
    listing but wrong as fusion input — see the bugfix note on
    ``_literal_candidates``).
    mode='semantic': the legacy ``semantic_search`` 2-list fuse (cosine +
    token overlap over the cosine oversample only), no literal list — this is
    what ``semantic: true`` aliases to. Raises (does not degrade) when no
    profile is active, same as before.

    Bugfix (live-reported): the token-overlap list is ranked over the UNION
    of cosine candidates and (hybrid mode only) literal candidates — using
    each row's full ``rank_text`` — not the cosine oversample alone. A row
    the embedding ranked low but that names the query verbatim used to be
    invisible to the lexical list entirely.

    Filters (kind/project/since/until/session_id/harness) are honoured on
    every mode via a single post-fusion pass (``_apply_search_filters``).
    Nodes are excluded from hybrid/literal results by default (W2.2) — see
    ``cli._id_shaped`` / ``cli._literal_candidates``.
    """
    from khipu.cli import _literal_candidates
    from khipu.hub_snapshot import try_hub_connect
    from khipu.search_text import fuse_ranked_lists, search_tokens, token_hit_count
    from khipu.topic_graph import enrich_search_results

    query = (query or "").strip()
    if not query:
        raise ValueError("query is required")
    mode = (mode or "hybrid").strip().lower()
    if mode not in ("hybrid", "literal", "semantic"):
        raise ValueError("mode must be 'hybrid', 'literal', or 'semantic'")
    if kind is not None:
        allowed = _SEMANTIC_KINDS if mode == "semantic" else _LITERAL_KINDS
        if kind not in allowed:
            raise ValueError(f"kind must be one of {allowed}")

    limit = max(1, int(limit))
    # Larger literal pool (bugfix): 50 minimum, not 40 — the fair-share bug's
    # replacement (a single globally-ranked pool) needs enough headroom that
    # a strong episode hit is never pushed out by weak-but-numerous topic hits.
    oversample = max(limit * 4, 50)
    degraded: str | None = None

    cosine_rows: list[dict[str, Any]] = []
    # kind="node" (only valid for hybrid/literal, never semantic — checked
    # above) has nothing to contribute to the cosine list: nodes are never
    # embedded. Skip cosine entirely rather than let semantic_kind=None fall
    # through to "no kind filter" and pollute a node-only request with every
    # other kind.
    if mode in ("hybrid", "semantic") and not (kind and kind not in _SEMANTIC_KINDS):
        semantic_kind = kind if kind in _SEMANTIC_KINDS else None
        try:
            cosine_rows = _cosine_candidates(query, limit=oversample, kind=semantic_kind)
        except RuntimeError:
            if mode == "semantic":
                raise
            cosine_rows = []
            degraded = "no-embedding"

    with try_hub_connect() as conn:
        with conn.cursor() as cur:
            literal_rows: list[dict[str, Any]] = []
            if mode in ("hybrid", "literal"):
                literal_kind = kind if kind in _LITERAL_KINDS else None
                literal_rows = _literal_candidates(cur, query, oversample, kind=literal_kind)

            lists: list[list[dict[str, Any]]] = []
            if cosine_rows:
                lists.append(list(cosine_rows))
                tokens = search_tokens(query)
                if tokens:
                    union: dict[tuple[str, str], dict[str, Any]] = {
                        (r["kind"], str(r["id"])): r for r in cosine_rows
                    }
                    if mode == "hybrid":
                        for r in literal_rows:
                            union.setdefault((r["kind"], str(r["id"])), r)
                    lex_rows = sorted(
                        union.values(),
                        key=lambda r: -token_hit_count(r.get("rank_text") or "", tokens),
                    )
                    lists.append(lex_rows)
            if literal_rows and mode in ("hybrid", "literal"):
                lists.append(list(literal_rows))

            for row_list in lists:
                for r in row_list:
                    r.pop("rank_text", None)
            if not lists:
                out: dict[str, Any] = {"query": query, "mode": mode, "results": []}
                if degraded:
                    out["degraded"] = degraded
                return out
            fused = fuse_ranked_lists(lists, limit=oversample)
            fused = _apply_search_filters(
                cur, fused, project=project, since=since, until=until,
                session_id=session_id, harness=harness,
            )
            fused = _fair_fill(fused, limit)
            fused = enrich_search_results(cur, fused)

    out = {"query": query, "mode": mode, "results": fused}
    if degraded:
        out["degraded"] = degraded
    return out


def coverage(*, profile: str | None = None) -> dict[str, Any]:
    """Per-kind coverage for active or named profile — the 'status UI that can't lie'."""
    from khipu.db import connect

    with connect() as conn:
        with conn.cursor() as cur:
            profile = _resolve_profile(cur, profile)
            # A forgotten episode (deleted_at set) has its embeddings removed on
            # purpose; counting it as "missing" turned doctor red after every
            # recall probe cleanup. Pre-0010 hubs have no deleted_at column.
            from khipu.db import has_columns

            if has_columns(cur, "episodes", "deleted_at"):
                cur.execute("SELECT COUNT(*) FROM episodes WHERE deleted_at IS NULL")
            else:
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
            # Embeddings still attached to a tombstoned episode are orphans, not
            # coverage: count only refs that point at a live row, or "embedded"
            # can exceed "total" and the ratio lies.
            if has_columns(cur, "episodes", "deleted_at") and "episode" in by:
                cur.execute(
                    "SELECT COUNT(DISTINCT m.ref), COUNT(*) FROM memory_embeddings m"
                    " JOIN episodes e ON e.id::text = m.ref AND e.deleted_at IS NULL"
                    " WHERE m.profile = %s AND m.kind = 'episode'",
                    (profile,),
                )
                r, ch = cur.fetchone()
                by["episode"] = {"refs": int(r), "chunks": int(ch)}
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
            from khipu.db import has_columns

            # A forgotten episode has no embedding on purpose; without this
            # guard the catch-up re-embedded every tombstone on the next hook.
            live = "e.deleted_at IS NULL AND " if has_columns(cur, "episodes", "deleted_at") else ""
            cur.execute(
                "SELECT e.id, e.summary, e.decisions, e.preferences, e.topics, e.people"
                f" FROM episodes e WHERE {live}NOT EXISTS ("
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

            # fix 5c: populate commitment embeddings HERE (the hook's bounded
            # catch-up step), not inline on the capture decision path —
            # commitments.auto_close would otherwise pay for an embed API
            # call on the hot capture path just to find out no embeddings
            # exist yet. Small and bounded (COMMITMENT_CATCHUP_LIMIT), same
            # shape as the episode pass above.
            out["commitments_embedded"] = 0
            out["commitments_chunks"] = 0
            try:
                cur.execute(
                    "SELECT c.id, c.text FROM commitments c WHERE c.status = 'open' "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM memory_embeddings m"
                    "  WHERE m.profile = %s AND m.kind = 'commitment' AND m.ref = c.id::text)"
                    " ORDER BY c.opened_at DESC LIMIT %s",
                    (profile, COMMITMENT_CATCHUP_LIMIT),
                )
                commitment_rows = cur.fetchall()
            except Exception as exc:  # noqa: BLE001 — pre-0009 hub: table doesn't exist yet
                _log(f"commitment embed catch-up skipped: {type(exc).__name__}: {exc}")
                commitment_rows = []
            snapshot_rows: list[dict[str, Any]] = []
            for cid, ctext in commitment_rows:
                text = (ctext or "").strip()
                if not text:
                    continue
                out["commitments_embedded"] += 1
                chunks = chunk_text(text)
                api = _api_texts(profile, [("", c) for c in chunks])
                vecs = embed_batch(api, profile=profile)
                from datetime import datetime, timezone

                built_at = datetime.now(timezone.utc).isoformat()
                rows = [("commitment", str(cid), i, c, _md5(c), v)
                        for i, (c, v) in enumerate(zip(chunks, vecs))]
                _upsert_chunks(cur, profile, rows)
                conn.commit()
                out["commitments_chunks"] += len(rows)
                snapshot_rows.extend(
                    {"profile": profile, "kind": "commitment", "ref": str(cid), "chunk_idx": i,
                     "chunk_text": c, "content_hash": _md5(c), "embedding": v, "built_at": built_at}
                    for i, (c, v) in enumerate(zip(chunks, vecs))
                )
            if snapshot_rows:
                # Keep the sqlite hub replica current, same as embed_on_capture
                # does for episodes (W2.4) — best-effort, never fails the pass.
                try:
                    from khipu.hub_snapshot import upsert_embeddings

                    snap = upsert_embeddings(snapshot_rows)
                    if not snap.get("ok"):
                        _log(f"commitment snapshot upsert skipped: {snap.get('error')}")
                except Exception as exc:  # noqa: BLE001
                    _log(f"commitment snapshot upsert failed: {type(exc).__name__}: {exc}")
    return out
