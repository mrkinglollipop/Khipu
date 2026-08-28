"""Full hub read replica for airplane mode (P3).

When PostgreSQL is reachable, ``refresh()`` atomically replaces
``hub_snapshot.sqlite`` under the data dir with episodes, topics,
topic_revisions, nodes, edges, embedding_profiles, and memory_embeddings
(vectors stored as float32 blobs). Ops tables such as ``ops_events`` are
not copied.

Read paths (CLI search/graph/get, MCP search/graph/get/status) try the hub
first; on connection failure they open a **separate** readonly sqlite handle
here — ``db.connect()`` stays Postgres-only for writers.

Auto-refresh: ``maybe_refresh()`` is fail-open from ``khipu doctor`` and
``khipu snapshot refresh`` when the hub answers; it does not block forever.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import struct
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import psycopg

SNAPSHOT_NAME = "hub_snapshot.sqlite"
META_NAME = "hub_snapshot.sqlite.meta.json"
HUB_CONNECT_TIMEOUT_S = 5
REFRESH_CONNECT_TIMEOUT_S = 30

_TABLES = (
    "episodes",
    "topics",
    "topic_revisions",
    "nodes",
    "edges",
    "embedding_profiles",
    "memory_embeddings",
)

_EPISODE_COLS = (
    "id",
    "ts",
    "session_id",
    "summary",
    "topics",
    "people",
    "decisions",
    "preferences",
    "scope",
    "edges",
    "raw",
    "ingested_at",
)
_TOPIC_COLS = (
    "slug",
    "title",
    "body",
    "status",
    "created_at",
    "updated_at",
    "links",
    "frontmatter",
    "source_path",
    "content_hash",
    "deleted_at",
)
_TOPIC_REVISION_COLS = (
    "id",
    "slug",
    "revised_at",
    "body",
    "source",
    "note",
    "content_hash",
)
_NODE_COLS = (
    "id",
    "type",
    "bucket",
    "name",
    "payload",
    "source_path",
    "built_at",
    "frozen",
    "source_id",
)
_EDGE_COLS = ("src", "dst", "type", "weight", "payload", "built_at")
_PROFILE_COLS = (
    "id",
    "provider",
    "model",
    "dim",
    "normalize",
    "is_active",
    "created_at",
    "note",
)
_EMBED_COLS = (
    "profile",
    "kind",
    "ref",
    "chunk_idx",
    "chunk_text",
    "content_hash",
    "embedding",
    "built_at",
)


def snapshot_path() -> Path:
    from khipu.paths import data_dir

    return data_dir() / SNAPSHOT_NAME


def meta_path() -> Path:
    from khipu.paths import data_dir

    return data_dir() / META_NAME


def meta() -> dict[str, Any]:
    p = meta_path()
    if not p.is_file():
        return {"exists": False}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"exists": True, "readable": False}
    data.setdefault("exists", True)
    data.setdefault("path", str(snapshot_path()))
    return data


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(msg: str) -> None:
    print(f"[khipu-snapshot] {msg}", file=sys.stderr, flush=True)


def hub_connection_failed(exc: BaseException) -> bool:
    name = type(exc).__name__
    text = str(exc).lower()
    if isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError)):
        return True
    if "Operational" in name or "Connection" in name:
        return True
    if "connect" in text or "timeout" in text or "refused" in text:
        return True
    if isinstance(exc, RuntimeError) and "no khipu dsn" in text:
        return True
    return False


def try_hub_connect(
    *, connect_timeout: int = HUB_CONNECT_TIMEOUT_S
) -> psycopg.Connection:
    from khipu.db import conninfo_with_local_root_cert, resolve_dsn

    return psycopg.connect(
        conninfo_with_local_root_cert(resolve_dsn()),
        connect_timeout=connect_timeout,
    )


def open_snapshot() -> sqlite3.Connection:
    path = snapshot_path()
    if not path.is_file():
        raise FileNotFoundError(f"hub snapshot missing: {path}")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _fair_shares(total: int, n: int) -> list[int]:
    total = max(0, int(total))
    base, rem = divmod(total, n)
    return [base + (1 if i < rem else 0) for i in range(n)]


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _json_text(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, str):
        return val
    return json.dumps(val, ensure_ascii=False)


def _ts_text(val: Any) -> str | None:
    if val is None:
        return None
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val)


def _vector_to_blob(val: Any) -> bytes | None:
    if val is None:
        return None
    if isinstance(val, bytes):
        return val
    if isinstance(val, memoryview):
        return bytes(val)
    if isinstance(val, (list, tuple)):
        floats = [float(x) for x in val]
        return struct.pack(f"{len(floats)}f", *floats)
    text = str(val).strip()
    if not text:
        return None
    if text.startswith("["):
        floats = [float(x) for x in text[1:-1].split(",") if x.strip()]
        return struct.pack(f"{len(floats)}f", *floats)
    return None


def _blob_to_vector(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob[: n * 4]))


def _create_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE episodes (
            id INTEGER PRIMARY KEY,
            ts TEXT,
            session_id TEXT,
            summary TEXT NOT NULL,
            topics TEXT,
            people TEXT,
            decisions TEXT,
            preferences TEXT,
            scope TEXT,
            edges TEXT,
            raw TEXT,
            ingested_at TEXT
        );
        CREATE TABLE topics (
            slug TEXT PRIMARY KEY,
            title TEXT,
            body TEXT,
            status TEXT,
            created_at TEXT,
            updated_at TEXT,
            links TEXT,
            frontmatter TEXT,
            source_path TEXT,
            content_hash TEXT,
            deleted_at TEXT
        );
        CREATE TABLE topic_revisions (
            id INTEGER PRIMARY KEY,
            slug TEXT,
            revised_at TEXT,
            body TEXT,
            source TEXT,
            note TEXT,
            content_hash TEXT
        );
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY,
            type TEXT,
            bucket TEXT,
            name TEXT,
            payload TEXT,
            source_path TEXT,
            built_at TEXT,
            frozen INTEGER,
            source_id TEXT
        );
        CREATE TABLE edges (
            src TEXT,
            dst TEXT,
            type TEXT,
            weight REAL,
            payload TEXT,
            built_at TEXT,
            PRIMARY KEY (src, dst, type)
        );
        CREATE TABLE embedding_profiles (
            id TEXT PRIMARY KEY,
            provider TEXT,
            model TEXT,
            dim INTEGER,
            normalize TEXT,
            is_active INTEGER,
            created_at TEXT,
            note TEXT
        );
        CREATE TABLE memory_embeddings (
            profile TEXT,
            kind TEXT,
            ref TEXT,
            chunk_idx INTEGER,
            chunk_text TEXT,
            content_hash TEXT,
            embedding BLOB,
            built_at TEXT,
            PRIMARY KEY (profile, kind, ref, chunk_idx)
        );
        """
    )


def _pg_columns(cur, table: str) -> set[str]:
    cur.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        """,
        (table,),
    )
    return {r[0] for r in cur.fetchall()}


def _insert_episodes(cur, con: sqlite3.Connection) -> int:
    cols = [c for c in _EPISODE_COLS if c in _pg_columns(cur, "episodes")]
    sel = ", ".join(cols)
    cur.execute(f"SELECT {sel} FROM episodes ORDER BY id")
    rows = cur.fetchall()
    ph = ", ".join(cols)
    for row in rows:
        vals = []
        for col, val in zip(cols, row, strict=True):
            if col in ("topics", "people", "decisions", "preferences", "edges", "raw"):
                vals.append(_json_text(val))
            elif col in ("ts", "ingested_at"):
                vals.append(_ts_text(val))
            else:
                vals.append(val)
        con.execute(
            f"INSERT INTO episodes ({ph}) VALUES ({', '.join('?' * len(cols))})",
            vals,
        )
    return len(rows)


def _insert_topics(cur, con: sqlite3.Connection) -> int:
    cols = [c for c in _TOPIC_COLS if c in _pg_columns(cur, "topics")]
    sel = ", ".join(cols)
    cur.execute(f"SELECT {sel} FROM topics ORDER BY slug")
    rows = cur.fetchall()
    ph = ", ".join(cols)
    for row in rows:
        vals = []
        for col, val in zip(cols, row, strict=True):
            if col in ("links", "frontmatter"):
                vals.append(_json_text(val))
            elif col in ("created_at", "updated_at", "deleted_at"):
                vals.append(_ts_text(val))
            else:
                vals.append(val)
        con.execute(
            f"INSERT INTO topics ({ph}) VALUES ({', '.join('?' * len(cols))})",
            vals,
        )
    return len(rows)


def _insert_topic_revisions(cur, con: sqlite3.Connection) -> int:
    cols = [c for c in _TOPIC_REVISION_COLS if c in _pg_columns(cur, "topic_revisions")]
    sel = ", ".join(cols)
    cur.execute(f"SELECT {sel} FROM topic_revisions ORDER BY id")
    rows = cur.fetchall()
    ph = ", ".join(cols)
    for row in rows:
        vals = []
        for col, val in zip(cols, row, strict=True):
            if col == "revised_at":
                vals.append(_ts_text(val))
            else:
                vals.append(val)
        con.execute(
            f"INSERT INTO topic_revisions ({ph}) VALUES ({', '.join('?' * len(cols))})",
            vals,
        )
    return len(rows)


def _insert_nodes(cur, con: sqlite3.Connection) -> int:
    pg_cols = _pg_columns(cur, "nodes")
    cols = [c for c in _NODE_COLS if c in pg_cols]
    sel = ", ".join(cols)
    cur.execute(f"SELECT {sel} FROM nodes ORDER BY id")
    rows = cur.fetchall()
    ph = ", ".join(cols)
    for row in rows:
        vals = []
        for col, val in zip(cols, row, strict=True):
            if col == "payload":
                vals.append(_json_text(val))
            elif col == "built_at":
                vals.append(_ts_text(val))
            elif col == "frozen":
                vals.append(1 if val else 0)
            else:
                vals.append(val)
        con.execute(
            f"INSERT INTO nodes ({ph}) VALUES ({', '.join('?' * len(cols))})",
            vals,
        )
    return len(rows)


def _insert_edges(cur, con: sqlite3.Connection) -> int:
    cols = [c for c in _EDGE_COLS if c in _pg_columns(cur, "edges")]
    sel = ", ".join(cols)
    cur.execute(f"SELECT {sel} FROM edges")
    rows = cur.fetchall()
    ph = ", ".join(cols)
    for row in rows:
        vals = []
        for col, val in zip(cols, row, strict=True):
            if col == "payload":
                vals.append(_json_text(val))
            elif col == "built_at":
                vals.append(_ts_text(val))
            else:
                vals.append(val)
        con.execute(
            f"INSERT INTO edges ({ph}) VALUES ({', '.join('?' * len(cols))})",
            vals,
        )
    return len(rows)


def _insert_profiles(cur, con: sqlite3.Connection) -> int:
    cols = [c for c in _PROFILE_COLS if c in _pg_columns(cur, "embedding_profiles")]
    sel = ", ".join(cols)
    cur.execute(f"SELECT {sel} FROM embedding_profiles ORDER BY id")
    rows = cur.fetchall()
    ph = ", ".join(cols)
    for row in rows:
        vals = []
        for col, val in zip(cols, row, strict=True):
            if col == "created_at":
                vals.append(_ts_text(val))
            elif col == "is_active":
                vals.append(1 if val else 0)
            else:
                vals.append(val)
        con.execute(
            f"INSERT INTO embedding_profiles ({ph}) VALUES ({', '.join('?' * len(cols))})",
            vals,
        )
    return len(rows)


def _insert_memory_embeddings(cur, con: sqlite3.Connection) -> int:
    cols = [c for c in _EMBED_COLS if c in _pg_columns(cur, "memory_embeddings")]
    sel_cols = []
    for c in cols:
        if c == "embedding":
            sel_cols.append("embedding::text AS embedding")
        else:
            sel_cols.append(c)
    sel = ", ".join(sel_cols)
    cur.execute(f"SELECT {sel} FROM memory_embeddings")
    rows = cur.fetchall()
    ph = ", ".join(cols)
    for row in rows:
        vals = []
        for col, val in zip(cols, row, strict=True):
            if col == "embedding":
                vals.append(_vector_to_blob(val))
            elif col == "built_at":
                vals.append(_ts_text(val))
            else:
                vals.append(val)
        con.execute(
            f"INSERT INTO memory_embeddings ({ph}) VALUES ({', '.join('?' * len(cols))})",
            vals,
        )
    return len(rows)


def refresh() -> dict[str, Any]:
    """Dump hub tables into a fresh sqlite file and atomically replace the snapshot."""
    from khipu.paths import ensure_data_dir

    ensure_data_dir()
    dest = snapshot_path()
    tmp_dir = dest.parent
    fd, tmp_name = tempfile.mkstemp(
        prefix=".hub_snapshot.", suffix=".sqlite", dir=tmp_dir
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    counts: dict[str, int] = {}
    try:
        con = sqlite3.connect(str(tmp_path))
        _create_schema(con)
        with try_hub_connect(connect_timeout=REFRESH_CONNECT_TIMEOUT_S) as pg:
            with pg.cursor() as cur:
                counts["episodes"] = _insert_episodes(cur, con)
                counts["topics"] = _insert_topics(cur, con)
                counts["topic_revisions"] = _insert_topic_revisions(cur, con)
                counts["nodes"] = _insert_nodes(cur, con)
                counts["edges"] = _insert_edges(cur, con)
                counts["embedding_profiles"] = _insert_profiles(cur, con)
                counts["memory_embeddings"] = _insert_memory_embeddings(cur, con)
        con.commit()
        con.close()
        os.replace(tmp_path, dest)
        refreshed_at = _utcnow_iso()
        size_bytes = dest.stat().st_size
        meta_path().write_text(
            json.dumps(
                {
                    "refreshed_at": refreshed_at,
                    "size_bytes": size_bytes,
                    "counts": counts,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return {
            "ok": True,
            "path": str(dest),
            "refreshed_at": refreshed_at,
            "size_bytes": size_bytes,
            "counts": counts,
        }
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def maybe_refresh(
    *, connect_timeout: int = REFRESH_CONNECT_TIMEOUT_S
) -> dict[str, Any] | None:
    """Fail-open refresh when the hub is reachable."""
    try:
        with try_hub_connect(connect_timeout=connect_timeout) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return refresh()
    except Exception as exc:  # noqa: BLE001 — fail-open by design
        _log(f"refresh skipped: {type(exc).__name__}: {exc}")
        return None


def snapshot_health() -> dict[str, Any]:
    """Age and size for doctor/status — separate from hub liveness."""
    path = snapshot_path()
    m = meta()
    if not path.is_file():
        return {
            "ok": False,
            "exists": False,
            "path": str(path),
            "refreshed_at": m.get("refreshed_at"),
            "size_bytes": None,
            "age_seconds": None,
        }
    st = path.stat()
    refreshed_at = m.get("refreshed_at")
    age_s: int | None = None
    if refreshed_at:
        try:
            ref = datetime.fromisoformat(str(refreshed_at).replace("Z", "+00:00"))
            age_s = int((datetime.now(timezone.utc) - ref).total_seconds())
        except ValueError:
            age_s = None
    return {
        "ok": True,
        "exists": True,
        "path": str(path),
        "refreshed_at": refreshed_at,
        "size_bytes": st.st_size,
        "age_seconds": age_s,
        "counts": m.get("counts"),
    }


def stale_fields() -> dict[str, Any]:
    m = meta()
    return {
        "stale": True,
        "snapshot_refreshed_at": m.get("refreshed_at"),
    }


def _episode_ilike_columns() -> tuple[str, ...]:
    return (
        "summary",
        "COALESCE(topics, '')",
        "COALESCE(decisions, '')",
        "COALESCE(preferences, '')",
        "COALESCE(people, '')",
    )


def _token_match_sqlite(
    columns: tuple[str, ...], tokens: list[str]
) -> tuple[str, str, list[Any]]:
    """WHERE any-token-hits plus ORDER BY coverage; returns bound params."""
    wheres: list[str] = []
    scores: list[str] = []
    params: list[Any] = []
    ncols = len(columns)
    for tok in tokens:
        pat = f"%{_escape_like(tok)}%"
        ors = " OR ".join(f"LOWER({c}) LIKE LOWER(?) ESCAPE '\\'" for c in columns)
        wheres.append(f"({ors})")
        scores.append(f"(CASE WHEN {ors} THEN 1 ELSE 0 END)")
        params.extend([pat] * ncols)
        params.extend([pat] * ncols)
    return " OR ".join(wheres), " + ".join(scores), params


def search_snapshot(query: str, limit: int) -> list[dict[str, Any]]:
    from khipu.search_text import search_tokens
    from khipu.snippets import LABEL_LIMIT, SNIPPET_LIMIT, clip_snippet

    topic_n, episode_n, node_n = _fair_shares(limit, 3)
    tokens = search_tokens(query)
    con = open_snapshot()
    results: list[dict[str, Any]] = []

    if not tokens:
        tokens = [(query or "").strip()] if (query or "").strip() else []
        if not tokens:
            return []
        params: list[Any] = [f"%{_escape_like(tokens[0])}%"]
        topic_where = (
            "LOWER(body) LIKE LOWER(?) ESCAPE '\\' OR LOWER(slug) LIKE LOWER(?) ESCAPE '\\' "
            "OR LOWER(COALESCE(title, '')) LIKE LOWER(?) ESCAPE '\\'"
        )
        topic_order = "slug ASC"
        episode_where = " OR ".join(
            f"LOWER({c}) LIKE LOWER(?) ESCAPE '\\'" for c in _episode_ilike_columns()
        )
        episode_order = "ts DESC, id DESC"
        node_where = (
            "LOWER(id) LIKE LOWER(?) ESCAPE '\\' "
            "OR LOWER(COALESCE(name, '')) LIKE LOWER(?) ESCAPE '\\'"
        )
        node_order = "id ASC"
        topic_params = [params[0], params[0], params[0]]
        episode_params = [params[0]] * len(_episode_ilike_columns())
        node_params = [params[0], params[0]]
    else:
        topic_where, topic_score, topic_params = _token_match_sqlite(
            ("body", "slug", "COALESCE(title, '')"), tokens
        )
        topic_order = f"({topic_score}) DESC, slug ASC"
        episode_where, episode_score, episode_params = _token_match_sqlite(
            _episode_ilike_columns(), tokens
        )
        episode_order = f"({episode_score}) DESC, ts DESC, id DESC"
        node_where, node_score, node_params = _token_match_sqlite(
            ("id", "COALESCE(name, '')"), tokens
        )
        node_order = f"({node_score}) DESC, id ASC"

    if topic_n > 0:
        sql = f"""
            SELECT 'topic' AS kind, slug AS id, COALESCE(title, slug) AS label,
                   substr(body, 1, 4000) AS snippet
            FROM topics
            WHERE deleted_at IS NULL AND ({topic_where})
            ORDER BY {topic_order}
            LIMIT ?
        """
        rows = con.execute(sql, (*topic_params, topic_n)).fetchall()
        for r in rows:
            results.append(
                {
                    "kind": r[0],
                    "id": r[1],
                    "label": clip_snippet(r[2], LABEL_LIMIT),
                    "snippet": clip_snippet(r[3], SNIPPET_LIMIT),
                }
            )

    if episode_n > 0:
        sql = f"""
            SELECT 'episode' AS kind, CAST(id AS TEXT) AS id, summary AS label,
                   summary AS snippet
            FROM episodes
            WHERE {episode_where}
            ORDER BY {episode_order}
            LIMIT ?
        """
        rows = con.execute(sql, (*episode_params, episode_n)).fetchall()
        for r in rows:
            results.append(
                {
                    "kind": r[0],
                    "id": r[1],
                    "label": clip_snippet(r[2], LABEL_LIMIT),
                    "snippet": clip_snippet(r[3], SNIPPET_LIMIT),
                }
            )

    if node_n > 0:
        sql = f"""
            SELECT 'node' AS kind, id AS id, COALESCE(name, id) AS label,
                   substr(COALESCE(payload, ''), 1, 4000) AS snippet
            FROM nodes
            WHERE {node_where}
            ORDER BY {node_order}
            LIMIT ?
        """
        rows = con.execute(sql, (*node_params, node_n)).fetchall()
        for r in rows:
            results.append(
                {
                    "kind": r[0],
                    "id": r[1],
                    "label": clip_snippet(r[2], LABEL_LIMIT),
                    "snippet": clip_snippet(r[3], SNIPPET_LIMIT),
                }
            )
    return results


def merge_outbox_episodes(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from khipu.outbox import jobs
    from khipu.snippets import LABEL_LIMIT, SNIPPET_LIMIT, clip_snippet

    merged = list(results)
    seen: set[str] = {f"{r.get('kind')}:{r.get('id')}" for r in merged}
    for jp in jobs():
        try:
            job = json.loads(jp.read_text(encoding="utf-8"))
            payload = job.get("payload") or {}
            summary = (payload.get("summary") or "").strip()
            if not summary:
                continue
            key = f"episode:outbox:{jp.stem}"
            if key in seen:
                continue
            seen.add(key)
            merged.append(
                {
                    "kind": "episode",
                    "id": f"outbox:{jp.stem}",
                    "label": clip_snippet(summary, LABEL_LIMIT),
                    "snippet": clip_snippet(summary, SNIPPET_LIMIT),
                    "outbox": True,
                }
            )
        except (OSError, ValueError, TypeError):
            continue
    return merged


def enrich_search_results_snapshot(
    con: sqlite3.Connection, results: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    from collections import defaultdict

    from khipu.topic_graph import (
        NEIGHBOR_CAP,
        collapse_semantic_topic_hits,
        extract_paths,
        topic_aliases,
    )

    rows = collapse_semantic_topic_hits(results)
    topic_slugs = [str(r["id"]) for r in rows if r.get("kind") == "topic"]
    bodies: dict[str, str] = {}
    if topic_slugs:
        ph = ", ".join("?" * len(topic_slugs))
        for slug, body in con.execute(
            f"SELECT slug, body FROM topics WHERE slug IN ({ph}) AND deleted_at IS NULL",
            topic_slugs,
        ):
            bodies[slug] = body or ""
    aliases: list[str] = []
    alias_to_slug: dict[str, str] = {}
    for slug in dict.fromkeys(topic_slugs):
        for alias in topic_aliases(slug):
            alias_to_slug[alias] = slug
            aliases.append(alias)
    neighbors_by_slug: dict[str, list[dict[str, str]]] = defaultdict(list)
    seen_nb: dict[str, set[str]] = defaultdict(set)
    if aliases:
        ph = ", ".join("?" * len(aliases))
        for src, dst, etype in con.execute(
            f"""
            SELECT src, dst, type FROM edges
            WHERE src IN ({ph}) OR dst IN ({ph})
            """,
            (*aliases, *aliases),
        ):
            slug = alias_to_slug.get(src) or alias_to_slug.get(dst)
            if not slug:
                continue
            other = dst if alias_to_slug.get(src) == slug else src
            if other in seen_nb[slug]:
                continue
            if len(neighbors_by_slug[slug]) >= NEIGHBOR_CAP:
                continue
            seen_nb[slug].add(other)
            neighbors_by_slug[slug].append({"id": other, "type": etype})
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        kind = item.get("kind")
        if kind == "topic":
            slug = str(item.get("id") or "")
            item["paths"] = extract_paths(bodies.get(slug, ""))
            item["neighbors"] = neighbors_by_slug.get(slug, [])
        elif kind == "episode":
            item["paths"] = extract_paths(str(item.get("snippet") or ""))
        out.append(item)
    return out


def graph_neighbors_snapshot(node_id: str, hops: int, limit: int) -> dict[str, Any]:
    from khipu.topic_graph import (
        PATH_PREFIX,
        TOPIC_PREFIX,
        extract_paths,
        graph_query_aliases,
        topic_slug_from_label,
    )

    hops = max(1, int(hops))
    con = open_snapshot()
    raw_id = (node_id or "").strip()
    episode_meta: dict[str, Any] | None = None
    synthetic: list[dict[str, Any]] = []
    aliases: list[str] = []

    if raw_id.isdigit():
        row = con.execute(
            "SELECT topics, summary FROM episodes WHERE id = ?",
            (int(raw_id),),
        ).fetchone()
        slugs: list[str] = []
        if row is None:
            episode_meta = {"id": int(raw_id), "missing": True, "topics": []}
        else:
            topics_raw, summary = row[0], row[1]
            try:
                topics_list = json.loads(topics_raw or "[]")
            except json.JSONDecodeError:
                topics_list = []
            for item in topics_list or []:
                slug = topic_slug_from_label(str(item))
                if slug:
                    slugs.append(slug)
            slugs = list(dict.fromkeys(slugs))
            for slug in slugs:
                aliases.extend(graph_query_aliases(slug))
            for rel in extract_paths(summary or ""):
                aliases.append(PATH_PREFIX + rel.rstrip("/"))
            aliases = list(dict.fromkeys(a for a in aliases if a))
            episode_meta = {"id": int(raw_id), "missing": False, "topics": slugs}
            synthetic = [
                {
                    "src": f"episode:{raw_id}",
                    "dst": f"{TOPIC_PREFIX}{slug}",
                    "type": "capture_topic",
                    "weight": 1.0,
                }
                for slug in slugs
            ]
    else:
        aliases = graph_query_aliases(node_id)

    if hops == 1:
        sql_edges: list[tuple[Any, ...]] = []
        if aliases:
            ph = ", ".join("?" * len(aliases))
            sql_edges = con.execute(
                f"""
                SELECT e.src, e.dst, e.type, e.weight
                FROM edges e
                WHERE e.src IN ({ph}) OR e.dst IN ({ph})
                ORDER BY e.type ASC
                LIMIT ?
                """,
                (*aliases, *aliases, limit),
            ).fetchall()
        merged = synthetic + [
            {"src": a, "dst": b, "type": t, "weight": w} for a, b, t, w in sql_edges
        ]
        out: dict[str, Any] = {
            "id": node_id,
            "hops": 1,
            "edges": merged[:limit],
            "graph_table": [],
        }
        if episode_meta is not None:
            out["episode"] = episode_meta
        return out

    walk_rows: list[tuple[Any, ...]] = []
    if aliases:
        frontier = set(aliases)
        seen_nodes: set[str] = set(aliases)
        walk: list[tuple[str, str, str, int]] = []
        for hop in range(1, hops + 1):
            if not frontier:
                break
            ph = ", ".join("?" * len(frontier))
            batch = list(frontier)
            rows = con.execute(
                f"""
                SELECT src, dst, type FROM edges
                WHERE src IN ({ph}) OR dst IN ({ph})
                """,
                (*batch, *batch),
            ).fetchall()
            next_frontier: set[str] = set()
            for src, dst, etype in rows:
                for node in batch:
                    if src == node:
                        other, via = dst, src
                    elif dst == node:
                        other, via = src, dst
                    else:
                        continue
                    if other in seen_nodes:
                        continue
                    seen_nodes.add(other)
                    walk.append((other, via, etype, hop))
                    next_frontier.add(other)
                    if len(walk) >= limit:
                        break
                if len(walk) >= limit:
                    break
            frontier = next_frontier
            if len(walk) >= limit:
                break
        walk_rows = [(a, b, t, h) for a, b, t, h in walk[:limit]]

    synthetic_walk = [
        {"node_id": e["dst"], "via": e["src"], "type": e["type"], "hops": 1}
        for e in synthetic
    ]
    out = {
        "id": node_id,
        "hops": hops,
        "walk": (
            synthetic_walk
            + [
                {"node_id": a, "via": b, "type": t, "hops": h}
                for a, b, t, h in walk_rows
            ]
        )[:limit],
    }
    if episode_meta is not None:
        out["episode"] = episode_meta
    return out


def episode_detail_snapshot(episode_id: int) -> dict[str, Any] | None:
    con = open_snapshot()
    row = con.execute(
        """
        SELECT id, ts, ingested_at, session_id, scope, summary,
               topics, people, decisions, preferences, edges, raw
        FROM episodes WHERE id = ?
        """,
        (episode_id,),
    ).fetchone()
    if not row:
        return None
    topics = json.loads(row[6] or "[]")
    return {
        "id": row[0],
        "ts": row[1],
        "ingested_at": row[2],
        "session_id": row[3],
        "scope": row[4],
        "summary": row[5],
        "topics": topics,
        "people": json.loads(row[7] or "[]"),
        "decisions": json.loads(row[8] or "[]"),
        "preferences": json.loads(row[9] or "[]"),
        "edges": json.loads(row[10] or "[]"),
        "raw": json.loads(row[11] or "null") if row[11] else None,
    }


def topic_detail_snapshot(slug: str) -> dict[str, Any] | None:
    slug = (slug or "").strip()
    if not slug:
        return None
    con = open_snapshot()
    row = con.execute(
        """
        SELECT slug, title, body, status, created_at, updated_at, links, source_path
        FROM topics WHERE slug = ? AND deleted_at IS NULL
        """,
        (slug,),
    ).fetchone()
    if not row:
        return None
    return {
        "slug": row[0],
        "title": row[1],
        "body": row[2],
        "status": row[3],
        "created_at": row[4],
        "updated_at": row[5],
        "links": json.loads(row[6] or "[]"),
        "source_path": row[7],
    }


def status_payload_snapshot() -> dict[str, Any]:
    con = open_snapshot()
    counts = {}
    for table, key in (
        ("episodes", "episodes"),
        ("topics", "topics"),
        ("nodes", "nodes"),
        ("edges", "edges"),
        ("memory_embeddings", "embeddings"),
        ("topic_revisions", "topic_revisions"),
    ):
        try:
            counts[key] = int(
                con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
        except sqlite3.Error:
            counts[key] = 0
    tomb = int(
        con.execute(
            "SELECT COUNT(*) FROM topics WHERE deleted_at IS NOT NULL"
        ).fetchone()[0]
    )
    counts["topics_tombstoned"] = tomb
    max_ts, max_ing = con.execute(
        "SELECT MAX(ts), MAX(ingested_at) FROM episodes"
    ).fetchone()
    return {
        "counts": counts,
        "latest_episode_ts": max_ts,
        "latest_ingested_at": max_ing,
        "dsn_ok": False,
        "hub_unreachable": True,
        **stale_fields(),
        "hub_snapshot": snapshot_health(),
    }


def local_embed_configured() -> bool:
    try:
        from khipu.models import show_models

        cfg = show_models().get("embed") or {}
    except Exception:
        return False
    if cfg.get("provider") != "local":
        return False
    return bool(
        (cfg.get("endpoint") or "").strip() and (cfg.get("model_id") or "").strip()
    )


def _embeddings_url(endpoint: str) -> str:
    ep = (endpoint or "").strip().rstrip("/")
    if ep.endswith("/v1"):
        return f"{ep}/embeddings"
    return f"{ep}/v1/embeddings"


def _embed_query_local(text: str, *, endpoint: str, model_id: str) -> list[float]:
    from khipu.keychain import get_openai_compat_key

    url = _embeddings_url(endpoint)
    headers = {"Content-Type": "application/json"}
    bearer = get_openai_compat_key()
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    body = json.dumps({"model": model_id, "input": text[:8000]}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    item = data.get("data", [{}])[0]
    vec = item.get("embedding")
    if not isinstance(vec, list):
        raise RuntimeError("local embed returned no embedding vector")
    return [float(x) for x in vec]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def semantic_search_snapshot(
    query: str,
    *,
    limit: int = 20,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    if not local_embed_configured():
        raise ValueError(
            "hub unreachable; semantic search requires a configured local embed "
            "provider — use keyword search without --semantic"
        )
    from khipu.models import show_models
    from khipu.snippets import LABEL_LIMIT, SNIPPET_LIMIT, clip_snippet

    cfg = show_models().get("embed") or {}
    vec = _embed_query_local(
        query,
        endpoint=cfg["endpoint"],
        model_id=cfg["model_id"],
    )
    con = open_snapshot()
    profile_row = con.execute(
        "SELECT id FROM embedding_profiles WHERE is_active = 1 LIMIT 1"
    ).fetchone()
    profile = profile_row[0] if profile_row else None
    if not profile:
        return []
    params: list[Any] = [profile]
    kind_clause = ""
    if kind:
        kind_clause = " AND kind = ?"
        params.append(kind)
    rows = con.execute(
        f"""
        SELECT kind, ref, chunk_idx, chunk_text, embedding
        FROM memory_embeddings
        WHERE profile = ?{kind_clause} AND embedding IS NOT NULL
        """,
        params,
    ).fetchall()
    scored: list[tuple[float, dict[str, Any]]] = []
    for knd, ref, chunk_idx, chunk_text, blob in rows:
        if not blob:
            continue
        score = _cosine(vec, _blob_to_vector(blob))
        label = chunk_text
        if knd == "topic":
            trow = con.execute(
                "SELECT COALESCE(title, slug) FROM topics WHERE slug = ?",
                (ref,),
            ).fetchone()
            if trow:
                label = trow[0] or ref
        elif knd == "episode":
            erow = con.execute(
                "SELECT summary FROM episodes WHERE CAST(id AS TEXT) = ?",
                (ref,),
            ).fetchone()
            if erow:
                label = erow[0] or ref
        scored.append(
            (
                score,
                {
                    "kind": knd,
                    "id": ref,
                    "label": clip_snippet(str(label), LABEL_LIMIT),
                    "snippet": clip_snippet(chunk_text or "", SNIPPET_LIMIT),
                    "score": score,
                    "chunk_idx": chunk_idx,
                },
            )
        )
    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[: max(1, limit)]]


def search_stale_payload(
    query: str, limit: int, *, semantic: bool = False, kind: str | None = None
) -> dict[str, Any]:
    if semantic:
        results = semantic_search_snapshot(query, limit=limit, kind=kind)
        con = open_snapshot()
        results = enrich_search_results_snapshot(con, results)
        return {
            "query": query,
            "mode": "semantic",
            "results": results,
            **stale_fields(),
        }
    con = open_snapshot()
    results = search_snapshot(query, limit)
    results = merge_outbox_episodes(results)
    results = enrich_search_results_snapshot(con, results)
    return {
        "query": query,
        "mode": "ilike",
        "results": results[:limit],
        **stale_fields(),
    }
