# --bypass-harness (sonnet lane) — authored directly by the dispatched on-sub
# Sonnet build agent for Session C (brief: "do the work directly"); Cursor is
# suspended for Claude Code sessions, so there is no further lane to route to.
"""Library search legs and ``khipu get`` for library ids (Session C).

Scope: docs/plans/2026-10-07-library-sources-byoe.md ("What ships" item 4).

A library is a folder of text files indexed as a search space of its own
(``khipu.library``). ``embed.hybrid_search`` calls this module to add three
ranked lists next to the memory ones, which then ride the existing fusion,
relevance gate and rerank untouched:

* **cosine**, one list per distinct embedding profile among the enabled
  libraries. The query is embedded once per profile (``input_type="query"``,
  through ``embed._query_vec``, so the same ``memory_query_cache`` keyed by
  profile and the same 10 s / 2 retry budget as memory). The SQL repeats the
  profile predicate and the ``embedding::vector(<dim>)`` cast on both sides of
  ``<=>`` so the per-profile partial HNSW index is used, and takes the
  nearest ``LIMIT n`` BEFORE joining to documents: a 329k-row library costs an
  index probe, never a scan.
* **keyword**, three statements. Full-text search over chunk text
  (``tsv @@ plainto_tsquery('simple', q)`` ranked by ``ts_rank``; ``tsv`` is a
  stored generated column with a GIN index, migration 0026) is the main one.
  ILIKE over ``library_documents.title`` / ``author`` (a few thousand rows) is
  always cheap. The unindexed ILIKE substring scan over chunk text runs only
  for a query with a quoted phrase or an id-shaped token (``needs_literal``).
  The two chunk statements run under a ``statement_timeout`` inside a
  savepoint; one that does not finish is dropped alone and named in
  ``degraded_legs`` (``library_fts``, ``library_literal``).
* **lexical** is not a separate query: ``hybrid_search`` already ranks token
  overlap over the union of cosine and literal rows' ``rank_text``.

Every leg fails open. A provider that cannot embed the query (missing key,
timeout, HTTP error) drops only that profile's cosine list and is named
``library_embed:<profile>`` in ``degraded_legs``; literal and lexical still
run. A hub without the library tables, or a cursor that cannot run these
statements, simply has no libraries.

Row shape (a hit)::

    {"kind": "library",
     "id": "library:<name>:<doc_id>#<chunk_idx>",   # title-only: no '#<idx>'
     "label": "<author>, <title>", "snippet": <clipped chunk>,
     "path": <root-relative rel_path>, "source": "<name>",
     "score": ..., "cosine": ... (when a cosine leg produced it)}

``get`` resolves those ids (``khipu get library:...`` and ``khipu_get``).
"""
from __future__ import annotations

import re
import sys
import time
from typing import Any

# Candidates per leg. The default (kind=None) search keeps libraries on a short
# leash so a huge corpus never slows or crowds memory results; an explicit
# kind="library" search takes the full oversample.
DEFAULT_LEG_LIMIT = 30
EXPLICIT_LEG_LIMIT = 200
# The chunk-text ILIKE has no index; this bounds it.
LITERAL_TIMEOUT_MS_DEFAULT = 1500
LITERAL_TIMEOUT_MS_EXPLICIT = 4000
# pgvector's hnsw.ef_search defaults to 40 and caps rows returned by an index
# scan at that number; raised to the LIMIT (best effort) so a wider request
# actually returns that many.
MAX_EF_SEARCH = 1000

_ID_RE = re.compile(r"^library:([a-z0-9_-]{1,40}):([0-9]+)(?:#([0-9]+))?$")

# True once library_sources has been seen on this process; saves a to_regclass
# round trip on every later search. (A "no" is never cached: the hub can be
# migrated while the process lives.)
_TABLES_SEEN = False


def _log(msg: str) -> None:
    print(f"[khipu-library-search] {msg}", file=sys.stderr, flush=True)


# ---- ids and labels -------------------------------------------------------------


def format_id(name: str, doc: int, chunk_idx: int | None = None) -> str:
    base = f"library:{name}:{int(doc)}"
    return base if chunk_idx is None else f"{base}#{int(chunk_idx)}"


def is_library_id(ident: str) -> bool:
    return str(ident or "").strip().lower().startswith("library:")


def parse_id(ident: str) -> tuple[str, int, int | None]:
    """``library:<name>:<doc>[#<idx>]`` -> (name, doc_id, chunk_idx | None)."""
    m = _ID_RE.match(str(ident or "").strip())
    if not m:
        raise ValueError(
            "library id must look like library:<name>:<doc_id> or "
            "library:<name>:<doc_id>#<chunk_idx>"
        )
    return m.group(1), int(m.group(2)), (int(m.group(3)) if m.group(3) is not None else None)


def label_for(author: str | None, title: str | None, rel_path: str | None) -> str:
    author = (author or "").strip()
    title = (title or "").strip()
    if author and title:
        return f"{author}, {title}"
    return title or author or (rel_path or "")


def _row(
    name: str, doc: int, chunk_idx: int | None, *, title, author, rel_path,
    snippet_src: str | None, rank_text: str, score: float | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    from khipu.snippets import LABEL_LIMIT, SNIPPET_LIMIT, clip_snippet

    label = label_for(author, title, rel_path)
    row: dict[str, Any] = {
        "kind": "library",
        "id": format_id(name, doc, chunk_idx),
        "label": clip_snippet(label, LABEL_LIMIT),
        "snippet": clip_snippet(snippet_src if snippet_src else label, SNIPPET_LIMIT),
        "path": rel_path or "",
        "source": name,
        "rank_text": rank_text or "",
    }
    if chunk_idx is not None:
        row["chunk_idx"] = int(chunk_idx)
    if score is not None:
        row["score"] = round(float(score), 4)
    if profile:
        # The model whose cosine `score`/`cosine` is: raw cosine is only
        # comparable within one model, and the relevance floor is per profile.
        row["profile"] = profile
    return row


# ---- which libraries ------------------------------------------------------------


def enabled_libraries(cur, conn, source: str | None = None) -> list[tuple[str, str]]:
    """``[(name, profile)]`` for every enabled library (optionally just
    ``source``). Empty when the hub has no library tables or the statement
    fails; a failure that leaves a Postgres transaction aborted is rolled
    back so the rest of the search keeps working."""
    global _TABLES_SEEN
    try:
        if not _TABLES_SEEN:
            cur.execute("SELECT to_regclass('public.library_sources') IS NOT NULL")
            row = cur.fetchone()
            if not (row and row[0]):
                return []
            _TABLES_SEEN = True
        cur.execute(
            "SELECT name, profile FROM library_sources"
            " WHERE enabled AND (%(src)s::text IS NULL OR name = %(src)s)"
            " ORDER BY name",
            {"src": source},
        )
        return [(str(r[0]), str(r[1])) for r in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001 - libraries are additive; memory search must go on
        _log(f"library list skipped: {type(exc).__name__}")
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return []


# ---- cosine leg -----------------------------------------------------------------


def _ef_search(limit: int) -> int:
    return max(40, min(int(limit), MAX_EF_SEARCH))


def cosine_sql(dim: int) -> str:
    """The nearest-neighbour statement for one profile of width ``dim``.

    The inner SELECT is the index-eligible shape: the profile predicate plus
    ``ORDER BY embedding::vector(<dim>) <=> q::vector(<dim>) LIMIT n``. The
    join to documents (and the source restriction) happens on those n rows
    only, so a library sharing its profile with another is filtered after the
    index scan, never instead of it.
    """
    v = f"vector({int(dim)})"
    return f"""
        SELECT d.source, d.id, n.chunk_idx, n.score,
               left(c.chunk_text, %(fetch)s) AS snippet,
               left(c.chunk_text, %(rank_fetch)s) AS rank_src,
               d.title, d.author, d.rel_path
        FROM (
            SELECT e.document, e.chunk_idx,
                   1 - (e.embedding::{v} <=> %(q)s::{v}) AS score
            FROM library_embeddings e
            WHERE e.profile = %(p)s
            ORDER BY e.embedding::{v} <=> %(q)s::{v}
            LIMIT %(lim)s
        ) n
        JOIN library_documents d ON d.id = n.document
        JOIN library_chunks c ON c.document = n.document AND c.chunk_idx = n.chunk_idx
        WHERE d.source = ANY(%(sources)s)
        ORDER BY n.score DESC
        LIMIT %(lim)s
        """


def cosine_lists(
    cur, conn, query: str, libs: list[tuple[str, str]], *, limit: int,
    timing: dict[str, Any], degraded_legs: list[str],
) -> list[list[dict[str, Any]]]:
    """One cosine-ordered list (best first) per distinct profile in ``libs``."""
    from khipu import embed as em
    from khipu.embed import CHUNK_CHARS, FETCH_LIMIT

    by_profile: dict[str, list[str]] = {}
    for name, profile in libs:
        by_profile.setdefault(profile, []).append(name)

    out: list[list[dict[str, Any]]] = []
    embed_ms = cosine_ms = 0.0
    for profile, names in by_profile.items():
        t0 = time.monotonic()
        try:
            api_q = em.prefix_query(query) if em.uses_task_prefixes(profile) else query
            qvec, state = em._query_vec(cur, conn, profile, api_q)
        except Exception as exc:  # noqa: BLE001 - any provider failure degrades, never raises
            degraded_legs.append(f"library_embed:{profile}")
            timing["library_embed_error"] = f"{profile}: {exc}"[:160]
            embed_ms += (time.monotonic() - t0) * 1000
            continue
        embed_ms += (time.monotonic() - t0) * 1000
        timing[f"library_embed_cache:{profile}"] = state
        t1 = time.monotonic()
        try:
            cur.execute("SAVEPOINT khipu_lib_cos")
            try:
                cur.execute(f"SET LOCAL hnsw.ef_search = {_ef_search(limit)}")
            except Exception:  # noqa: BLE001 - no pgvector GUC (scratch DB) is not fatal
                pass
            cur.execute(
                cosine_sql(len(qvec)),
                {"q": em._vec_literal(qvec), "p": profile, "lim": int(limit),
                 "fetch": FETCH_LIMIT, "rank_fetch": CHUNK_CHARS, "sources": names},
            )
            fetched = cur.fetchall()
            rows = []
            for src, doc, idx, score, snip, rank_src, title, author, rel in fetched:
                rows.append(_row(
                    str(src), int(doc), int(idx), title=title, author=author,
                    rel_path=rel, snippet_src=snip, rank_text=rank_src or snip or "",
                    score=score, profile=profile,
                ))
            out.append(rows)
        except Exception as exc:  # noqa: BLE001
            degraded_legs.append(f"library_cosine:{profile}")
            timing["library_cosine_error"] = f"{profile}: {type(exc).__name__}: {exc}"[:160]
        finally:
            try:
                cur.execute("ROLLBACK TO SAVEPOINT khipu_lib_cos")
                cur.execute("RELEASE SAVEPOINT khipu_lib_cos")
            except Exception:  # noqa: BLE001
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
        cosine_ms += (time.monotonic() - t1) * 1000
    timing["library_embed_ms"] = round(embed_ms, 1)
    timing["library_cosine_ms"] = round(cosine_ms, 1)
    return out


# ---- keyword legs ---------------------------------------------------------------

_QUOTED_RE = re.compile(r'"[^"]+"')
_ID_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:/#-]{6,}$")


def needs_literal(query: str) -> bool:
    """True when a substring scan is the right tool: the query has a quoted
    phrase, or a token shaped like an id (``a:b``, ``a__b``, a path, a hash,
    letters mixed with digits). Everything else is answered by the full-text
    leg alone, so the unindexed ILIKE scan never runs for ordinary queries."""
    from khipu.cli import _id_shaped

    q = (query or "").strip()
    if _QUOTED_RE.search(q) or _id_shaped(q):
        return True
    for tok in q.split():
        if not _ID_TOKEN_RE.match(tok):
            continue
        if any(c in tok for c in ":_/#"):
            return True
        if any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok):
            return True
    return False


def _bounded(cur, conn, label: str, sql: str, params: dict[str, Any], timeout_ms: int) -> list[tuple]:
    """Run ``sql`` under a statement_timeout inside a savepoint. The savepoint
    is always rolled back, which reverts the SET LOCAL and clears an abort, so
    a timeout costs this statement only. Raises whatever the statement raised."""
    cur.execute(f"SAVEPOINT {label}")
    try:
        cur.execute(f"SET LOCAL statement_timeout = {max(1, int(timeout_ms))}")
        cur.execute(sql, params)
        return list(cur.fetchall())
    finally:
        try:
            cur.execute(f"ROLLBACK TO SAVEPOINT {label}")
            cur.execute(f"RELEASE SAVEPOINT {label}")
        except Exception:  # noqa: BLE001
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass


FTS_SQL = """
    SELECT d.source, d.id, c.chunk_idx, left(c.chunk_text, %(rank_fetch)s),
           d.title, d.author, d.rel_path,
           ts_rank(c.tsv, plainto_tsquery('simple', %(query)s)) AS rank
    FROM library_chunks c
    JOIN library_documents d ON d.id = c.document
    WHERE c.tsv @@ plainto_tsquery('simple', %(query)s)
      AND d.source = ANY(%(sources)s)
    ORDER BY rank DESC, d.id ASC, c.chunk_idx ASC
    LIMIT %(lim)s
    """


def literal_rows(
    cur, conn, query: str, names: list[str], *, limit: int, timeout_ms: int,
    timing: dict[str, Any], degraded_legs: list[str],
) -> list[dict[str, Any]]:
    """Keyword hits, best first: a title/author ILIKE (a small table), a
    full-text search over chunk text (``tsv @@ plainto_tsquery('simple', q)``
    ranked by ``ts_rank``, GIN-indexed by migration 0026), and, only when
    ``needs_literal`` (a quoted phrase or an id-shaped token), the ILIKE
    substring scan over chunk text. The chunk statements run under
    ``timeout_ms``; each leg that fails is dropped alone and named in
    ``degraded_legs`` (``library_fts``, ``library_literal``)."""
    from khipu.cli import _ilike_token_params, _token_match_sql
    from khipu.embed import CHUNK_CHARS, FETCH_LIMIT
    from khipu.search_text import search_tokens

    t0 = time.monotonic()
    term = (query or "").strip()
    tokens = search_tokens(term) or ([term] if term else [])
    if not tokens or not names:
        timing["library_literal_ms"] = 0.0
        return []
    n = len(tokens)
    params: dict[str, Any] = {**_ilike_token_params(tokens), "sources": names, "lim": int(limit),
                              "fetch": FETCH_LIMIT, "rank_fetch": CHUNK_CHARS, "query": term}
    titles: list[tuple[int, dict[str, Any]]] = []   # (hits, row)
    chunks: list[dict[str, Any]] = []               # FTS rows in ts_rank order
    ilike: list[tuple[int, int, dict[str, Any]]] = []  # (phrase, hits, row)
    qlower = term.lower()

    def _phrase(text: str) -> int:
        return 1 if qlower and qlower in (text or "").lower() else 0

    def _chunk(row):
        src, doc, idx, text, title, author, rel = row[:7]
        return _row(str(src), int(doc), int(idx), title=title, author=author, rel_path=rel,
                    snippet_src=text, rank_text=text or "")

    # Title / author: a small table, always cheap, runs first so a chunk-leg
    # timeout never costs the title hits.
    try:
        where, score = _token_match_sql(("COALESCE(d.title, '')", "COALESCE(d.author, '')"), n)
        cur.execute(
            f"""
            SELECT d.source, d.id, d.title, d.author, d.rel_path, ({score}) AS hits
            FROM library_documents d
            WHERE d.source = ANY(%(sources)s) AND ({where})
            ORDER BY hits DESC, d.id ASC
            LIMIT %(lim)s
            """,
            params,
        )
        for src, doc, title, author, rel, hits in cur.fetchall():
            titles.append((int(hits), _row(
                str(src), int(doc), None, title=title, author=author, rel_path=rel,
                snippet_src=None, rank_text=f"{title or ''}\n{author or ''}",
            )))
    except Exception as exc:  # noqa: BLE001
        degraded_legs.append("library_literal_title")
        timing["library_literal_error"] = f"title: {type(exc).__name__}: {exc}"[:160]
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass

    try:
        for r in _bounded(cur, conn, "khipu_lib_fts", FTS_SQL, params, timeout_ms):
            chunks.append(_chunk(r))
    except Exception as exc:  # noqa: BLE001 - timeout, column not migrated, cursor limits
        degraded_legs.append("library_fts")
        timing["library_fts_error"] = f"{type(exc).__name__}: {exc}"[:160]

    if needs_literal(term):
        try:
            where, score = _token_match_sql(("c.chunk_text",), n)
            rows = _bounded(
                cur, conn, "khipu_lib_lit",
                f"""
                SELECT d.source, d.id, c.chunk_idx, left(c.chunk_text, %(rank_fetch)s),
                       d.title, d.author, d.rel_path, ({score}) AS hits
                FROM library_chunks c
                JOIN library_documents d ON d.id = c.document
                WHERE d.source = ANY(%(sources)s) AND ({where})
                ORDER BY hits DESC, d.id ASC, c.chunk_idx ASC
                LIMIT %(lim)s
                """,
                params, timeout_ms,
            )
            for r in rows:
                ilike.append((_phrase(r[3] or ""), int(r[7]), _chunk(r)))
        except Exception as exc:  # noqa: BLE001
            degraded_legs.append("library_literal")
            timing["library_literal_error"] = f"chunks: {type(exc).__name__}"[:160]

    # Order: exact substring hits first (they were asked for by quote or id),
    # then titles naming every query token, then the full-text order, then the
    # remaining titles. Duplicates keep their first (best) position.
    ilike.sort(key=lambda t: (-t[0], -t[1], t[2]["id"]))
    full_titles = [r for h, r in titles if h >= n]
    rest_titles = [r for h, r in titles if h < n]
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in [*(r for _, _, r in ilike), *full_titles, *chunks, *rest_titles]:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        merged.append(r)
    timing["library_literal_ms"] = round((time.monotonic() - t0) * 1000, 1)
    return merged[: int(limit)]


# ---- the leg hybrid_search calls --------------------------------------------------


def library_candidates(
    conn, cur, query: str, *, mode: str, kind: str | None, source: str | None,
    oversample: int, timing: dict[str, Any], degraded_legs: list[str],
) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]], list[str]]:
    """``(cosine_lists, literal_rows, library_names)`` for one search.

    ``mode`` is hybrid / semantic / literal exactly as ``hybrid_search``
    understands it: semantic runs only the cosine leg, literal only the ILIKE
    leg, hybrid both. ``kind == "library"`` takes the full oversample and the
    longer literal budget; the default search stays on the short leash.
    ``source`` names one library (``ValueError`` when no enabled library has
    that name)."""
    libs = enabled_libraries(cur, conn, source)
    if source and not libs:
        raise ValueError(f"no enabled library named {source!r}; see `khipu library list`")
    if not libs:
        return [], [], []
    explicit = kind == "library"
    leg_limit = min(int(oversample), EXPLICIT_LEG_LIMIT) if explicit else DEFAULT_LEG_LIMIT
    names = [n for n, _ in libs]
    cos: list[list[dict[str, Any]]] = []
    lit: list[dict[str, Any]] = []
    if mode in ("hybrid", "semantic"):
        cos = cosine_lists(cur, conn, query, libs, limit=leg_limit,
                           timing=timing, degraded_legs=degraded_legs)
    if mode in ("hybrid", "literal"):
        lit = literal_rows(
            cur, conn, query, names, limit=leg_limit,
            timeout_ms=LITERAL_TIMEOUT_MS_EXPLICIT if explicit else LITERAL_TIMEOUT_MS_DEFAULT,
            timing=timing, degraded_legs=degraded_legs,
        )
    return cos, lit, names


# ---- khipu get library:... ---------------------------------------------------------


def _iso(ts: Any) -> str | None:
    if ts is None:
        return None
    return ts.isoformat() if hasattr(ts, "isoformat") else str(ts)


def get(ident: str) -> dict[str, Any]:
    """Resolve a library id.

    ``library:<name>:<doc>`` -> the document row plus ``chunk_count``.
    ``library:<name>:<doc>#<idx>`` -> that chunk's text, the document, and the
    text of the chunks either side (``previous`` / ``next``, null at the
    ends), so a caller can read around a hit. ``ValueError`` when the id is
    malformed or names nothing.
    """
    name, doc, idx = parse_id(ident)
    from khipu.db import connect

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT d.id, d.source, d.rel_path, d.title, d.author, d.tags, d.bytes,"
                " d.mtime, d.content_hash, d.scanned_at, s.root"
                " FROM library_documents d JOIN library_sources s ON s.name = d.source"
                " WHERE d.id = %s AND d.source = %s",
                (doc, name),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"library document not found: library:{name}:{doc}")
            cur.execute("SELECT COUNT(*) FROM library_chunks WHERE document = %s", (doc,))
            count_row = cur.fetchone()
            chunk_count = int(count_row[0]) if count_row and count_row[0] is not None else 0
            document = {
                "id": format_id(name, doc),
                "source": name,
                "doc_id": int(row[0]),
                "title": row[3],
                "author": row[4],
                "tags": list(row[5] or []),
                "rel_path": row[2],
                "root": row[10],
                "bytes": int(row[6] or 0),
                "mtime": _iso(row[7]),
                "content_hash": row[8],
                "scanned_at": _iso(row[9]),
            }
            if idx is None:
                return {"kind": "library", "id": format_id(name, doc), "source": name,
                        "document": document, "chunk_count": chunk_count}
            cur.execute(
                "SELECT chunk_idx, chunk_text FROM library_chunks"
                " WHERE document = %s AND chunk_idx = ANY(%s)",
                (doc, [idx - 1, idx, idx + 1]),
            )
            chunks = {int(i): t for i, t in cur.fetchall()}
    if idx not in chunks:
        raise ValueError(f"library chunk not found: {format_id(name, doc, idx)}")

    def _nb(i: int) -> dict[str, Any] | None:
        if i not in chunks:
            return None
        return {"chunk_idx": i, "id": format_id(name, doc, i), "text": chunks[i]}

    return {
        "kind": "library", "id": format_id(name, doc, idx), "source": name,
        "chunk_idx": idx, "chunk_count": chunk_count, "text": chunks[idx],
        "document": document, "previous": _nb(idx - 1), "next": _nb(idx + 1),
    }
