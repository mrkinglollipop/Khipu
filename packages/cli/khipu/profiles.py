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
import os
import sys
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


# ---- per-profile vector index -------------------------------------------------

# Both vector tables hold an UNTYPED ``vector`` column (profiles of different
# widths share a table), so each profile gets its own partial expression HNSW
# index, created by code because a migration cannot know future dimensions.
INDEXED_TABLES = ("memory_embeddings", "library_embeddings")
_INDEX_PREFIX = {"memory_embeddings": "idx_memory_hnsw_", "library_embeddings": "idx_library_hnsw_"}
# 0004/0005 named these two; 0026 recreates them as expression indexes under
# the same names, so ensure_profile_index must reuse them, not add a twin.
_LEGACY_INDEX = {
    ("memory_embeddings", "gemini-embedding-001@768"): "idx_memory_embeddings_hnsw_gemini768",
    ("memory_embeddings", "gemini-embedding-2@768"): "idx_memory_embeddings_hnsw_gemini2_768",
}


def profile_index_name(profile: str, table: str = "library_embeddings") -> str:
    """Postgres identifier for one profile's index on ``table`` (63-byte cap)."""
    if table not in _INDEX_PREFIX:
        raise ValueError(f"no per-profile index for table {table!r}")
    legacy = _LEGACY_INDEX.get((table, profile))
    if legacy:
        return legacy
    safe = re.sub(r"[^a-z0-9_]+", "_", profile.lower()).strip("_") or "profile"
    name = f"{_INDEX_PREFIX[table]}{safe}"
    # An id that is not the model@dim shape (an old row, a hand-made one) can
    # sanitise to the same text as another; a hash of the raw id keeps them apart.
    if len(name) > 60 or not _ID_RE.match(profile):
        name = f"{name[:50]}_{hashlib.sha1(profile.encode()).hexdigest()[:9]}"
    return name


def library_index_name(profile: str) -> str:
    return profile_index_name(profile, "library_embeddings")


def ensure_profile_index(
    cur, profile: str, table: str, *, dim: int | None = None, quiet: bool = False,
) -> str | None:
    """Create (idempotently) the partial expression HNSW index for ``profile``
    on ``table`` (``memory_embeddings`` or ``library_embeddings``).

    The dimension is ``dim`` if given, else the profile's record (seed, cache,
    or its ``embedding_profiles`` row); the id is never parsed, because old
    rows may carry any id. A query uses the index only when it repeats both
    the cast and the predicate::

        SELECT ... FROM <table>
         WHERE profile = '<id>'
         ORDER BY embedding::vector(<dim>) <=> %s::vector(<dim>) LIMIT n

    ``table`` is allow-listed and the id is quote-escaped, since both are
    spliced into DDL. The existence check is a SELECT, so a nightly backfill
    that finds the index present runs no DDL.

    ``quiet=True`` is for callers whose real job must not fail over an index
    (activate, backfill, profiles add): the work runs under a savepoint, any
    failure (no profile row, no pgvector, dim above 2000, privileges) is
    logged as one line and swallowed, the table is searched sequentially, and
    None is returned. Returns the index name on success.
    """
    if table not in INDEXED_TABLES:
        raise ValueError(f"no per-profile index for table {table!r}")
    if quiet:
        cur.execute("SAVEPOINT khipu_profile_index")
        try:
            name = ensure_profile_index(cur, profile, table, dim=dim)
        except Exception as exc:  # noqa: BLE001 - an index is never worth the caller's job
            cur.execute("ROLLBACK TO SAVEPOINT khipu_profile_index")
            print(f"[khipu-embed] no index for {profile} on {table}: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            return None
        cur.execute("RELEASE SAVEPOINT khipu_profile_index")
        return name
    if dim is None:
        spec = cached_spec(profile) or load_spec(cur, profile)
        if spec is None:
            raise ValueError(f"no embedding_profiles row for {profile!r}; cannot tell its dimension")
        dim = spec.dim
    dim = int(dim)
    if dim < 1:
        raise ValueError(f"{profile}: dimension must be positive, got {dim}")
    if dim > MAX_INDEXED_DIM:
        raise ValueError(
            f"{profile}: pgvector indexes at most {MAX_INDEXED_DIM} dimensions "
            f"on vector(n); {dim} would need halfvec"
        )
    name = profile_index_name(profile, table)
    cur.execute(
        "SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = %s", (name,)
    )
    if cur.fetchone():
        return name
    literal = profile.replace("'", "''")
    # An HNSW build over a large table needs its graph in memory or it crawls
    # (pgvector logs "graph no longer fits into maintenance_work_mem"); a hub's
    # default is 64 MB. KHIPU_INDEX_MEMORY (e.g. 1200MB) raises it for this
    # transaction only, so an import on a small box can be sized by hand.
    mem = (os.environ.get("KHIPU_INDEX_MEMORY") or "").strip()
    if mem and re.fullmatch(r"\d{1,5}(MB|GB)", mem):
        cur.execute(f"SET LOCAL maintenance_work_mem = '{mem}'")
    cur.execute(
        f"CREATE INDEX IF NOT EXISTS {name} ON {table}"
        f" USING hnsw ((embedding::vector({dim})) vector_cosine_ops)"
        f" WHERE profile = '{literal}'"
    )
    return name


def ensure_library_index(cur, profile: str, *, dim: int | None = None) -> str | None:
    return ensure_profile_index(cur, profile, "library_embeddings", dim=dim)


# ---- prices, keys, deletion (the Embeddings screen) ---------------------------
# --bypass-harness (sonnet lane): dispatched agent, brief says do not delegate.

# USD per million input tokens. List prices as of 2026-10, informational: the
# provider's own pricing page is the authority, and a free allowance or a
# discount is not modelled. A (provider, model) pair that is not here is
# "price unknown", never guessed.
PRICE_TABLE_NOTE = "list prices as of 2026-10, informational"
PRICE_PER_MILLION_USD: dict[tuple[str, str], float] = {
    ("gemini", "gemini-embedding-2"): 0.20,
    ("gemini", "gemini-embedding-001"): 0.20,
    ("voyage", "voyage-3"): 0.06,
    ("voyage", "voyage-3-large"): 0.18,
    ("voyage", "voyage-3-lite"): 0.02,
    ("openai", "text-embedding-3-small"): 0.02,
    ("openai", "text-embedding-3-large"): 0.13,
}
_OPENAI_HOST = "api.openai.com"


def _host(endpoint: str | None) -> str | None:
    try:
        return urlsplit(endpoint or "").hostname
    except ValueError:
        return None


def is_loopback_endpoint(endpoint: str | None) -> bool:
    return _host(endpoint) in _LOOPBACK


def price_info(spec: ProfileSpec) -> tuple[float | None, str]:
    """``(usd per million tokens | None, note)`` for ``spec``. A loopback
    OpenAI-compatible server is free; OpenAI's own endpoint uses the OpenAI rows
    of the table; anything else is unknown."""
    if spec.provider == "openai-compatible":
        if is_loopback_endpoint(spec.endpoint):
            return 0.0, "local server, no charge"
        if _host(spec.endpoint) == _OPENAI_HOST and ("openai", spec.model) in PRICE_PER_MILLION_USD:
            return PRICE_PER_MILLION_USD[("openai", spec.model)], PRICE_TABLE_NOTE
        return None, "price unknown for this provider"
    price = PRICE_PER_MILLION_USD.get((spec.provider, spec.model))
    if price is None:
        return None, "price unknown for this provider"
    return price, PRICE_TABLE_NOTE


def key_present(provider: str) -> bool:
    """Is a key stored for ``provider`` (Keychain, environment, or key file)?
    Presence only: the value is never put in the result. An OpenAI-compatible
    server may need none; this reports whether one is stored."""
    from khipu import keychain

    try:
        if provider == "gemini":
            return bool(keychain.resolve_gemini_key())
        if provider == "voyage":
            return bool(keychain.resolve_voyage_key())
        if provider == "openai-compatible":
            return bool(keychain.get_openai_compat_key())
    except Exception:  # noqa: BLE001 - no key / unreadable Keychain both mean "not present"
        return False
    return False


def profile_users(cur, profile: str) -> list[str]:
    """Where ``profile`` is in use: ``"memory"`` when it is the active pointer,
    then the name of every library embedded with it."""
    users: list[str] = []
    cur.execute("SELECT is_active FROM embedding_profiles WHERE id = %s", (profile,))
    row = cur.fetchone()
    if row and row[0]:
        users.append("memory")
    if _to_regclass(cur, "library_sources"):
        cur.execute("SELECT name FROM library_sources WHERE profile = %s ORDER BY name", (profile,))
        users.extend(r[0] for r in cur.fetchall())
    return users


def _drop_index_quiet(cur, name: str) -> bool:
    cur.execute("SAVEPOINT khipu_drop_index")
    try:
        cur.execute(f"DROP INDEX IF EXISTS public.{name}")
    except Exception as exc:  # noqa: BLE001 - a leftover index is harmless, the rows are the point
        cur.execute("ROLLBACK TO SAVEPOINT khipu_drop_index")
        print(f"[khipu-embed] could not drop index {name}: {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
        return False
    cur.execute("RELEASE SAVEPOINT khipu_drop_index")
    return True


def delete_profile(cur, profile: str) -> dict[str, Any]:
    """Delete a profile nothing uses: its vectors in both spaces, its cached
    query vectors, its per-profile indexes, then the profile row. Refuses (with
    the users named) when it is active for memory or a library is embedded with
    it. The caller commits."""
    spec = load_spec(cur, profile)
    if spec is None:
        raise ValueError(f"unknown embedding profile {profile!r}")
    users = profile_users(cur, profile)
    if users:
        raise ValueError(
            f"profile {profile!r} is in use by {', '.join(users)}; move those to another "
            "profile first, then delete"
        )
    deleted = {"memory": 0, "library": 0, "query_cache": 0}
    for key, table in (("library", "library_embeddings"), ("memory", "memory_embeddings"),
                       ("query_cache", "memory_query_cache")):
        if not _to_regclass(cur, table):
            continue
        cur.execute(f"DELETE FROM {table} WHERE profile = %s", (profile,))
        deleted[key] = int(cur.rowcount or 0)
    indexes = [profile_index_name(profile, t) for t in INDEXED_TABLES]
    for name in indexes:
        _drop_index_quiet(cur, name)
    cur.execute("DELETE FROM embedding_profiles WHERE id = %s", (profile,))
    with _LOCK:
        _LEARNED.pop(profile, None)
    deleted["total"] = deleted["memory"] + deleted["library"] + deleted["query_cache"]
    return {"ok": True, "profile": profile, "deleted_rows": deleted, "dropped_indexes": indexes}
