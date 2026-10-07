# --bypass-harness (sonnet lane) — authored directly by the dispatched on-sub
# agent (brief: do not delegate); no further agent to route to.
"""What the Embeddings screen asks of the CLI that is not a backfill:

* ``estimate``       how many chunks, characters, tokens, dollars and seconds a
                     re-embed would take, before anyone presses the button;
* ``list_detailed``  every profile with where it is used, coverage per space,
                     key presence, median query time and list price;
* ``check_key``       one tiny embedding call, for the Add-a-model dialog.

Nothing here writes to the hub. No function returns a key; ``check_key`` scrubs
any header value out of an error before it leaves.
"""
from __future__ import annotations

import time
from typing import Any

from khipu import embed, embed_jobs, library, profiles


# ---- estimate -------------------------------------------------------------------

MIN_RATE_CHUNKS = 20  # a finished job smaller than this says nothing about throughput


def parse_space(space: str) -> tuple[str, str | None]:
    """``"memory"`` -> ``("memory", None)``; ``"library:NAME"`` -> ``("library", NAME)``."""
    space = (space or "memory").strip()
    if space == "memory":
        return "memory", None
    if space.startswith("library:"):
        return "library", library.validate_name(space[len("library:"):])
    raise ValueError(f"space {space!r} must be 'memory' or 'library:NAME'")


def measured_rate(profile: str) -> tuple[float | None, str | None]:
    """Chunks per second this profile achieved in its finished jobs (the last
    five that embedded at least MIN_RATE_CHUNKS), with where the number came
    from. ``(None, None)`` when no job has measured it yet."""
    from datetime import datetime

    chunks = 0
    seconds = 0.0
    used = 0
    for row in embed_jobs.list_jobs():
        if row.get("profile") != profile or row.get("state") != "done":
            continue
        done = int(row.get("done") or 0)
        try:
            span = (datetime.fromisoformat(row["updated_at"])
                    - datetime.fromisoformat(row["started_at"])).total_seconds()
        except (KeyError, ValueError, TypeError):
            continue
        if done < MIN_RATE_CHUNKS or span <= 0:
            continue
        chunks += done
        seconds += span
        used += 1
        if used >= 5:
            break
    if not used:
        return None, None
    return chunks / seconds, f"{used} finished job{'s' if used != 1 else ''} for this profile"


def estimate(profile_id: str, space: str = "memory", *, stale: bool = False) -> dict[str, Any]:
    """What embedding ``space`` under ``profile_id`` would cost from now.

    Counts the chunks that have no vector under the profile; for memory also
    those whose text changed since (what ``embed backfill`` always re-embeds),
    for a library only with ``stale=True`` (what ``library backfill --stale``
    does). Tokens are characters / 4; price is the list price when known."""
    from khipu.db import connect

    kind, lib_name = parse_space(space)
    with connect() as conn:
        with conn.cursor() as cur:
            spec = profiles.resolve_spec(profile_id, cur)
            if kind == "memory":
                plan, missing, stale_keys = embed.memory_gaps(cur, spec.id)
                todo = list(missing) + list(stale_keys)
                chunks = len(todo)
                chars = sum(plan[k][1] for k in todo)
            else:
                library.get_source(cur, lib_name)
                cond = library._TODO_STALE if stale else library._TODO_MISSING
                cur.execute(
                    "SELECT COUNT(*), COALESCE(SUM(LENGTH(c.chunk_text)), 0)"
                    + library._JOIN_E + cond,
                    (spec.id, lib_name),
                )
                chunks, chars = (int(x or 0) for x in cur.fetchone())
    tokens = -(-chars // 4)
    price, note = profiles.price_info(spec)
    rate, rate_source = measured_rate(spec.id)
    return {
        "profile": spec.id,
        "provider": spec.provider,
        "space": space.strip() or "memory",
        "chunks": chunks,
        "chars": chars,
        "approx_tokens": tokens,
        "price_usd": None if price is None else round(tokens / 1_000_000 * price, 8),
        "price_note": note,
        "approx_seconds": None if rate is None else int(round(chunks / rate)),
        "rate_source": rate_source,
    }


# ---- profiles list --------------------------------------------------------------

def _cov(total: int, embedded: int, missing: int, stale: int) -> dict[str, Any]:
    return {"total": total, "embedded": embedded, "missing": missing, "stale": stale,
            "pct": embed.cov_pct(max(0, embedded - stale), total)}


def _memory_plan_on_own_connection():
    from khipu.db import connect

    with connect() as conn, conn.cursor() as cur:
        return embed.memory_chunk_plan(cur, cached=True)


def _memory_hashes_on_own_connection():
    from khipu.db import connect

    with connect() as conn, conn.cursor() as cur:
        return embed._existing_hashes_many(cur, None)


def list_detailed(conn=None, *, parallel: bool = False) -> list[dict[str, Any]]:
    """``profiles.list_profiles`` rows plus ``in_use_by``, ``coverage`` (a key
    per space: ``memory`` and ``library:NAME`` for each library embedded with
    the profile), ``key_present``, ``median_query_ms_24h`` and
    ``price_per_million_usd``. Memory coverage is shown for every profile, so a
    re-embed in flight has a progress number before the profile is active.

    The number of statements does not grow with the number of profiles: one
    read for every profile's embedded hashes, grouped reads for the libraries.
    ``parallel=True`` counts the libraries (the slow part on a large corpus) and
    builds the memory chunk plan and reads the embedded hashes, each on a connection of their own, while this
    one reads the profiles. ``conn=None`` opens that one after the others have
    started, so the four connection set-ups overlap."""
    pool = None
    lib_future = plan_future = have_future = None
    if parallel:
        from concurrent.futures import ThreadPoolExecutor

        pool = ThreadPoolExecutor(max_workers=3)
        lib_future = pool.submit(library.doctor_block)
        plan_future = pool.submit(_memory_plan_on_own_connection)
        have_future = pool.submit(_memory_hashes_on_own_connection)
    opened = None
    try:
        if conn is None:
            from khipu.db import connect

            conn = opened = connect()
        with conn.cursor() as cur:
            rows = profiles.list_profiles(cur)
            mem: dict[str, dict[str, Any]] = {}
            if rows:
                plan = (plan_future.result() if plan_future is not None
                        else embed.memory_chunk_plan(cur, cached=True))
                gaps = embed.memory_gaps_many(
                    cur, [r["id"] for r in rows], plan,
                    have_future.result() if have_future is not None else None)
                for row in rows:
                    missing, stale = gaps[row["id"]]
                    mem[row["id"]] = _cov(len(plan), len(plan) - len(missing), len(missing), len(stale))
        libs = lib_future.result() if lib_future is not None else library.summary(conn)
    finally:
        if pool is not None:
            pool.shutdown(wait=True)
        if opened is not None:
            opened.close()
    medians = embed.median_query_ms()
    out = []
    for row in rows:
        spec = profiles.cached_spec(row["id"]) or profiles.ProfileSpec(
            row["id"], row["provider"], row["model"], row["dim"], row["normalize"], row["endpoint"])
        price, _note = profiles.price_info(spec)
        used = ["memory"] if row["is_active"] else []
        coverage = {"memory": mem[row["id"]]}
        for lib in libs:
            if lib["profile"] == row["id"]:
                used.append(lib["name"])
                coverage[f"library:{lib['name']}"] = _cov(
                    lib["chunks"], lib["embedded"], lib["missing"], lib["stale"])
        out.append({
            **row,
            "in_use_by": used,
            "coverage": coverage,
            "key_present": profiles.key_present(row["provider"]),
            "median_query_ms_24h": medians.get(row["id"]),
            "price_per_million_usd": price,
        })
    return out


# ---- test-key -------------------------------------------------------------------

_DEFAULT_MODEL = {"gemini": embed.MODEL_2, "voyage": "voyage-3"}


def _scrub(message: str, headers: dict[str, str]) -> str:
    """Remove every header value (and a bearer token's bare value) from ``message``."""
    for value in headers.values():
        for secret in {value, value.rsplit(" ", 1)[-1]}:
            if len(secret) >= 8:
                message = message.replace(secret, "***")
    return message


def check_key(provider: str, *, endpoint: str | None = None, model: str | None = None) -> dict[str, Any]:
    """Embed one short string with ``provider`` using its stored key and report
    ``{ok, ms, dim}`` or ``{ok: false, error}``. Costs one call from the daily
    embed budget and a fraction of a cent; the key is never printed."""
    if provider not in profiles.PROVIDERS:
        return {"ok": False, "error": f"unknown provider {provider!r}; known: {list(profiles.PROVIDERS)}"}
    model = (model or _DEFAULT_MODEL.get(provider) or "").strip()
    if not model:
        return {"ok": False, "error": "provider openai-compatible needs --model"}
    try:
        if provider == "openai-compatible":
            if not endpoint:
                return {"ok": False, "error": "provider openai-compatible needs --endpoint"}
            endpoint = profiles.normalize_endpoint(endpoint)
        elif endpoint:
            return {"ok": False, "error": f"provider {provider} has a fixed endpoint; --endpoint is for openai-compatible"}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    # The width is what we are here to learn, so the spec carries a placeholder
    # and the request goes through the builders directly (embed_batch would
    # reject a vector that is not the placeholder's width).
    spec = profiles.ProfileSpec(f"{model}@1", provider, model, 1, "none", endpoint)
    headers: dict[str, str] = {}
    t0 = time.monotonic()
    try:
        url, body, headers = embed._BUILDERS[provider](spec, ["Khipu embedding test"], "query")
        payload = embed._post_json(
            url, body, headers, provider=provider, retries=0,
            timeout=embed.QUERY_EMBED_TIMEOUT_S, delay=0.0,
        )
        vecs = embed._parse_vectors(provider, payload)
        if not vecs or not vecs[0]:
            return {"ok": False, "error": "the provider answered without a vector"}
    except Exception as exc:  # noqa: BLE001 - the receipt carries the failure
        return {"ok": False, "error": _scrub(f"{type(exc).__name__}: {exc}", headers)[:500]}
    return {"ok": True, "provider": provider, "model": model,
            "ms": int(round((time.monotonic() - t0) * 1000)), "dim": len(vecs[0])}
