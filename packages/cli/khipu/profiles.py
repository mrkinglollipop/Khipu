# --bypass-harness (sonnet lane) — authored directly by the dispatched on-sub
# Sonnet build agent (brief: do not delegate); no further agent to route to.
"""Embedding profiles: what a profile id means, and how to add one.

A profile is a row of ``embedding_profiles`` (provider, model, dim, normalize,
endpoint). ``khipu.embed`` dispatches on ``provider`` for both query and batch
embedding; this module owns the record, the validation, and the lookup.

The two Gemini ids are seeded here so nothing depends on the table being
populated (tests, an unmigrated hub). Every other profile is read from the hub
once per process and remembered; ``resolve_spec`` is the one door.

Provider values: ``gemini`` | ``voyage`` | ``openai-compatible``.
"""
from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

PROVIDERS = ("gemini", "voyage", "openai-compatible")
NORMALIZE_VALUES = ("l2", "none")

# ``model@dim`` with a conservative model charset. The id is spliced into DDL
# (the per-profile HNSW index, which cannot take bind parameters), so this
# pattern is also the injection guard: no quote, space, semicolon or comment
# marker can match it.
_ID_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._:/-]*)@([1-9][0-9]{0,5})$")
_LOOPBACK = ("localhost", "127.0.0.1", "::1")

# pgvector's HNSW index on vector(n) tops out at 2000 dimensions.
MAX_INDEXED_DIM = 2000


@dataclass(frozen=True)
class ProfileSpec:
    id: str
    provider: str
    model: str
    dim: int
    normalize: str = "l2"
    endpoint: str | None = None


SEED_PROFILES: dict[str, ProfileSpec] = {
    "gemini-embedding-001@768": ProfileSpec(
        "gemini-embedding-001@768", "gemini", "gemini-embedding-001", 768),
    "gemini-embedding-2@768": ProfileSpec(
        "gemini-embedding-2@768", "gemini", "gemini-embedding-2", 768),
}

_LOCK = threading.Lock()
_LEARNED: dict[str, ProfileSpec] = {}


def register_spec(spec: ProfileSpec) -> None:
    """Remember ``spec`` for this process (a seeded id is never shadowed)."""
    if spec.id in SEED_PROFILES:
        return
    with _LOCK:
        _LEARNED[spec.id] = spec


def clear_learned() -> None:
    with _LOCK:
        _LEARNED.clear()


def cached_spec(profile: str) -> ProfileSpec | None:
    """Seed or already-learned spec; never touches the database."""
    seeded = SEED_PROFILES.get(profile)
    if seeded is not None:
        return seeded
    with _LOCK:
        return _LEARNED.get(profile)


def known_ids() -> list[str]:
    with _LOCK:
        return sorted({*SEED_PROFILES, *_LEARNED})


def _has_endpoint_column(cur) -> bool:
    from khipu.db import has_columns

    return has_columns(cur, "embedding_profiles", "endpoint")


def load_spec(cur, profile: str) -> ProfileSpec | None:
    """Read one profile from the hub through ``cur`` and remember it."""
    endpoint_col = "endpoint" if _has_endpoint_column(cur) else "NULL"
    cur.execute(
        f"SELECT id, provider, model, dim, normalize, {endpoint_col}"
        " FROM embedding_profiles WHERE id = %s",
        (profile,),
    )
    row = cur.fetchone()
    if not row:
        return None
    spec = ProfileSpec(
        id=row[0], provider=row[1], model=row[2], dim=int(row[3]),
        normalize=row[4] or "l2", endpoint=row[5] or None,
    )
    register_spec(spec)
    return spec


def resolve_spec(profile: str, cur=None) -> ProfileSpec:
    """Seed -> learned -> the hub (through ``cur``, else one short connection).
    Unknown ids refuse rather than guess."""
    spec = cached_spec(profile)
    if spec is not None:
        return spec
    try:
        if cur is not None:
            spec = load_spec(cur, profile)
        else:
            from khipu.db import connect

            with connect() as conn:
                with conn.cursor() as c:
                    spec = load_spec(c, profile)
    except Exception as exc:  # noqa: BLE001 - a hub we cannot read is "unknown"
        raise ValueError(
            f"unknown embedding profile {profile!r} (could not read "
            f"embedding_profiles: {type(exc).__name__})"
        ) from exc
    if spec is None:
        raise ValueError(
            f"unknown embedding profile {profile!r}; known: {known_ids()}"
        )
    return spec


# ---- validation ---------------------------------------------------------------

def parse_profile_id(profile_id: str) -> tuple[str, int]:
    m = _ID_RE.match(profile_id or "")
    if not m:
        raise ValueError(
            f"profile id {profile_id!r} must look like model@dim, e.g. voyage-3@1024"
        )
    return m.group(1), int(m.group(2))


def normalize_endpoint(endpoint: str) -> str:
    """Validate and canonicalise an OpenAI-compatible base URL: https, or http
    on loopback only; no credentials, query or fragment; a trailing slash and a
    trailing ``/v1`` are dropped (the adapter appends ``/v1/embeddings``)."""
    raw = (endpoint or "").strip()
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"endpoint {endpoint!r} must be an http(s) URL")
    if parts.scheme == "http" and parts.hostname not in _LOOPBACK:
        raise ValueError(
            "endpoint must be https; plain http is allowed only for localhost, "
            "127.0.0.1 or ::1"
        )
    if parts.username or parts.password:
        raise ValueError("endpoint must not carry credentials; keys live in the Keychain")
    if parts.query or parts.fragment:
        raise ValueError("endpoint must not carry a query string or fragment")
    path = parts.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[: -len("/v1")]
    return f"{parts.scheme}://{parts.netloc}{path}"


def validate_spec(
    profile_id: str, *, provider: str, model: str, dim: int,
    endpoint: str | None = None, normalize: str = "l2",
) -> ProfileSpec:
    """Return a ProfileSpec or raise ValueError saying which rule failed."""
    id_model, id_dim = parse_profile_id(profile_id)
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}; known: {list(PROVIDERS)}")
    if normalize not in NORMALIZE_VALUES:
        raise ValueError(f"normalize must be one of {list(NORMALIZE_VALUES)}")
    if not model or id_model != model or id_dim != int(dim):
        raise ValueError(
            f"profile id {profile_id!r} must equal model@dim ({model}@{dim})"
        )
    if provider == "openai-compatible":
        if not endpoint:
            raise ValueError("provider openai-compatible needs --endpoint")
        endpoint = normalize_endpoint(endpoint)
    elif endpoint:
        raise ValueError(
            f"provider {provider} has a fixed endpoint; --endpoint is for openai-compatible"
        )
    return ProfileSpec(profile_id, provider, model, int(dim), normalize, endpoint or None)


# ---- the hub ------------------------------------------------------------------

def add_profile(cur, spec: ProfileSpec, *, note: str | None = None) -> dict[str, Any]:
    """Insert ``spec`` inactive. Profiles are never overwritten: an existing id
    with the same record is a no-op, with a different record it is refused."""
    existing = load_spec(cur, spec.id)
    if existing is not None:
        if existing == spec:
            return {"ok": True, "created": False, "id": spec.id}
        raise ValueError(
            f"profile {spec.id!r} already exists with a different record "
            f"({existing.provider}, {existing.model}, dim {existing.dim}, "
            f"endpoint {existing.endpoint}); profiles are never overwritten"
        )
    if _has_endpoint_column(cur):
        cur.execute(
            "INSERT INTO embedding_profiles"
            " (id, provider, model, dim, normalize, is_active, note, endpoint)"
            " VALUES (%s, %s, %s, %s, %s, false, %s, %s)",
            (spec.id, spec.provider, spec.model, spec.dim, spec.normalize, note, spec.endpoint),
        )
    else:
        if spec.endpoint:
            raise RuntimeError(
                "embedding_profiles has no endpoint column; apply 0026_library.sql first"
            )
        cur.execute(
            "INSERT INTO embedding_profiles"
            " (id, provider, model, dim, normalize, is_active, note)"
            " VALUES (%s, %s, %s, %s, %s, false, %s)",
            (spec.id, spec.provider, spec.model, spec.dim, spec.normalize, note),
        )
    register_spec(spec)
    return {"ok": True, "created": True, "id": spec.id}


def _to_regclass(cur, name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{name}",))
    row = cur.fetchone()
    return bool(row and row[0])


def list_profiles(cur) -> list[dict[str, Any]]:
    """Every profile with its active flag and row counts per space."""
    endpoint_col = "endpoint" if _has_endpoint_column(cur) else "NULL"
    cur.execute(
        f"SELECT id, provider, model, dim, normalize, {endpoint_col}, is_active"
        " FROM embedding_profiles ORDER BY created_at, id"
    )
    rows = cur.fetchall()
    counts: dict[str, dict[str, int]] = {}
    for space, table in (("memory", "memory_embeddings"), ("library", "library_embeddings")):
        if not _to_regclass(cur, table):
            continue
        cur.execute(f"SELECT profile, COUNT(*) FROM {table} GROUP BY profile")
        for pid, n in cur.fetchall():
            counts.setdefault(pid, {})[space] = int(n)
    out = []
    for pid, provider, model, dim, norm, endpoint, active in rows:
        spaces = counts.get(pid, {})
        out.append({
            "id": pid, "provider": provider, "model": model, "dim": int(dim),
            "normalize": norm, "endpoint": endpoint or None, "is_active": bool(active),
            "rows": {"memory": spaces.get("memory", 0), "library": spaces.get("library", 0)},
        })
    return out


# ---- library index ------------------------------------------------------------

def library_index_name(profile: str) -> str:
    """Postgres identifier for one profile's library HNSW index (63-byte cap)."""
    safe = re.sub(r"[^a-z0-9]+", "_", profile.lower()).strip("_")
    name = f"idx_library_hnsw_{safe}"
    if len(name) > 60:
        name = f"{name[:50]}_{hashlib.sha1(profile.encode()).hexdigest()[:9]}"
    return name


def ensure_library_index(cur, profile: str) -> str:
    """Create (idempotently) the partial expression HNSW index for ``profile``.

    ``library_embeddings.embedding`` has no declared dimension, so pgvector can
    only index it through a cast, and a partial index per profile keeps each
    profile's vectors in their own graph. A query uses the index only when it
    repeats both the cast and the predicate::

        SELECT ... FROM library_embeddings
         WHERE profile = '<id>'
         ORDER BY embedding::vector(<dim>) <=> %s::vector(<dim>) LIMIT n

    ``profile`` is spliced into DDL, so it is re-validated against the id
    pattern here, not trusted from the caller. Returns the index name.
    """
    _model, dim = parse_profile_id(profile)
    if dim > MAX_INDEXED_DIM:
        raise ValueError(
            f"{profile}: pgvector indexes at most {MAX_INDEXED_DIM} dimensions "
            f"on vector(n); {dim} would need halfvec"
        )
    name = library_index_name(profile)
    cur.execute(
        f"CREATE INDEX IF NOT EXISTS {name} ON library_embeddings"
        f" USING hnsw ((embedding::vector({dim})) vector_cosine_ops)"
        f" WHERE profile = '{profile}'"
    )
    return name
