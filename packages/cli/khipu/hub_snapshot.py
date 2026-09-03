"""Full hub read replica for airplane mode (P3).

When PostgreSQL is reachable, ``refresh()`` atomically replaces
``hub_snapshot.sqlite`` under the data dir with episodes, topics,
topic_revisions, nodes, edges, embedding_profiles, and memory_embeddings
(vectors stored as float32 blobs). Ops tables such as ``ops_events`` are
not copied.

Read paths (CLI search/graph/get, MCP search/graph/get/status) try the hub
first; on connection failure they open a **separate** readonly sqlite handle
here — ``db.connect()`` stays Postgres-only for writers.

Auto-refresh: ``maybe_refresh()`` is fail-open from ``khipu doctor`` when the
hub answers. It skips when a snapshot already exists and is younger than
``AUTO_REFRESH_MIN_AGE_S``. ``khipu snapshot refresh`` always dumps (it calls
``refresh()`` directly). Status / MCP status never dump — they only report
``snapshot_health()``.
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
# Status used to call maybe_refresh on every tab click, which dumped every
# episode + embedding over the wire and froze the desktop. Doctor may refresh,
# but not more often than this unless the operator runs `khipu snapshot refresh`.
AUTO_REFRESH_MIN_AGE_S = 15 * 60
_REFRESH_LOCK_NAME = ".hub_snapshot.refresh.lock"

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
    # fix 9: identity (0008) + hygiene (0010) columns — carried onto the
    # sqlite replica so snapshot-mode project/session_id/harness filters
    # (fix 7) and hygiene tags have something to read. dump/upsert already
    # skip any column absent on the PG side via _pg_columns/table_columns;
    # readers here guard with _snapshot_table_columns for an OLD snapshot
    # dumped before this fix (missing the columns on the sqlite side too).
    "harness",
    "repo_root",
    "project",
    "parent_session_id",
    "transcript_range",
    "tags",
    "deleted_at",
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


def _snapshot_table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    """Column names on THIS sqlite replica (fix 9: old snapshots may predate
    a migration's new columns — guard every read against that, never assume
    a column exists just because the current schema has it)."""
    try:
        return {row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:  # noqa: BLE001
        return set()


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
            ingested_at TEXT,
            harness TEXT,
            repo_root TEXT,
            project TEXT,
            parent_session_id TEXT,
            transcript_range TEXT,
            tags TEXT,
            deleted_at TEXT
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
    """Consolidated onto ``db.table_columns`` (shares the process cache and
    the information_schema round trip with embed/drift)."""
    from khipu.db import table_columns

    return table_columns(cur, table)


def _insert_episodes(cur, con: sqlite3.Connection) -> int:
    cols = [c for c in _EPISODE_COLS if c in _pg_columns(cur, "episodes")]
    sel = ", ".join(cols)
    cur.execute(f"SELECT {sel} FROM episodes ORDER BY id")
    rows = cur.fetchall()
    ph = ", ".join(cols)
    for row in rows:
        vals = []
        for col, val in zip(cols, row, strict=True):
            if col in ("topics", "people", "decisions", "preferences", "edges", "raw", "tags"):
                vals.append(_json_text(val))
            elif col in ("ts", "ingested_at", "deleted_at"):
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
    """Dump hub tables into a fresh sqlite file and atomically replace the snapshot.

    Holds a cross-process flock for the whole dump so doctor ``maybe_refresh``
    and ``khipu snapshot refresh`` serialize. If another dump is live, returns
    ``{"ok": False, "error": ...}`` instead of dumping anyway.
    """
    lock = _acquire_refresh_lock()
    if lock is None:
        return {
            "ok": False,
            "error": "hub snapshot refresh already in progress",
        }
    try:
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
                    counts["memory_embeddings"] = _insert_memory_embeddings(
                        cur, con
                    )
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
    finally:
        _release_refresh_lock(lock)


def _acquire_refresh_lock():
    """Non-blocking exclusive lock across CLI processes. None if another dump is live."""
    import fcntl

    from khipu.paths import ensure_data_dir

    path = ensure_data_dir() / _REFRESH_LOCK_NAME
    fh = path.open("a")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def _release_refresh_lock(lock) -> None:
    import fcntl

    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    lock.close()


def upsert_episode(
    episode_row: Mapping[str, Any], embedding_rows: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Incremental snapshot update: one episode + its embedding chunks (W2.4).

    Called from ``embed.embed_on_capture`` right after a successful embed, so
    a search that falls back to the sqlite replica (hub unreachable) sees a
    just-captured episode without waiting for the next full ``refresh()``.
    Takes the same cross-process lock as ``refresh()`` so an incremental
    upsert and a full dump never interleave; a dump in progress makes this a
    no-op ``{"ok": False}`` rather than blocking (the caller is fail-open).

    ``episode_row`` may carry any subset of the episode columns the sqlite
    schema knows (``_EPISODE_COLS``) — a fresh capture payload typically
    lacks ``ingested_at``/``edges``/``raw``, which is fine; those stay NULL.
    """
    path = snapshot_path()
    if not path.is_file():
        return {
            "ok": False,
            "error": "hub snapshot missing; run `khipu snapshot refresh` first",
        }
    lock = _acquire_refresh_lock()
    if lock is None:
        return {"ok": False, "error": "hub snapshot refresh in progress"}
    try:
        eid = episode_row.get("id")
        if eid is None:
            return {"ok": False, "error": "episode_row missing id"}
        con = sqlite3.connect(str(path))
        try:
            con.execute("DELETE FROM episodes WHERE id = ?", (eid,))
            cols = [c for c in _EPISODE_COLS if c in episode_row]
            vals = []
            for c in cols:
                v = episode_row.get(c)
                if c in ("topics", "people", "decisions", "preferences", "edges", "raw", "tags"):
                    v = _json_text(v)
                elif c in ("ts", "ingested_at", "deleted_at"):
                    v = _ts_text(v)
                vals.append(v)
            con.execute(
                f"INSERT INTO episodes ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' * len(cols))})",
                vals,
            )
            for erow in embedding_rows:
                ecols = [c for c in _EMBED_COLS if c in erow]
                evals = []
                for c in ecols:
                    v = erow.get(c)
                    if c == "embedding":
                        v = _vector_to_blob(v)
                    elif c == "built_at":
                        v = _ts_text(v)
                    evals.append(v)
                con.execute(
                    f"INSERT OR REPLACE INTO memory_embeddings ({', '.join(ecols)}) "
                    f"VALUES ({', '.join('?' * len(ecols))})",
                    evals,
                )
            con.commit()
        finally:
            con.close()
        m = meta()
        m["last_incremental_upsert_at"] = _utcnow_iso()
        m["last_incremental_episode_id"] = eid
        meta_path().write_text(json.dumps(m, indent=2), encoding="utf-8")
        return {"ok": True, "episode_id": eid, "embeddings": len(embedding_rows)}
    finally:
        _release_refresh_lock(lock)


def upsert_embeddings(embedding_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Incremental snapshot update: embedding rows only, no episode/topic row
    (fix 5c) — e.g. commitment embeddings from ``embed.embed_recent_missing``'s
    bounded catch-up, which has no single episode row to key off like
    ``upsert_episode`` does. Same locking/fail-open contract as
    ``upsert_episode``: a no-op ``{"ok": False}`` when the snapshot is
    missing or a full dump is in progress, never a raise."""
    path = snapshot_path()
    if not path.is_file():
        return {
            "ok": False,
            "error": "hub snapshot missing; run `khipu snapshot refresh` first",
        }
    if not embedding_rows:
        return {"ok": True, "embeddings": 0}
    lock = _acquire_refresh_lock()
    if lock is None:
        return {"ok": False, "error": "hub snapshot refresh in progress"}
    try:
        con = sqlite3.connect(str(path))
        try:
            for erow in embedding_rows:
                ecols = [c for c in _EMBED_COLS if c in erow]
                evals = []
                for c in ecols:
                    v = erow.get(c)
                    if c == "embedding":
                        v = _vector_to_blob(v)
                    elif c == "built_at":
                        v = _ts_text(v)
                    evals.append(v)
                con.execute(
                    f"INSERT OR REPLACE INTO memory_embeddings ({', '.join(ecols)}) "
                    f"VALUES ({', '.join('?' * len(ecols))})",
                    evals,
                )
            con.commit()
        finally:
            con.close()
        m = meta()
        m["last_incremental_upsert_at"] = _utcnow_iso()
        meta_path().write_text(json.dumps(m, indent=2), encoding="utf-8")
        return {"ok": True, "embeddings": len(embedding_rows)}
    finally:
        _release_refresh_lock(lock)


def maybe_refresh(
    *, connect_timeout: int = REFRESH_CONNECT_TIMEOUT_S, force: bool = False
) -> dict[str, Any] | None:
    """Fail-open refresh when the hub is reachable.

    Skip when a snapshot already exists and is younger than
    ``AUTO_REFRESH_MIN_AGE_S``, unless ``force`` is true. The age check runs
    before any hub connect — a 30s connect timeout is not a throttle.
    """
    if not force:
        health = snapshot_health()
        age = health.get("age_seconds")
        if health.get("exists") and isinstance(age, int) and 0 <= age < AUTO_REFRESH_MIN_AGE_S:
            _log(f"refresh skipped: snapshot age {age}s < {AUTO_REFRESH_MIN_AGE_S}s")
            return None
    try:
        with try_hub_connect(connect_timeout=connect_timeout) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        # Lock lives in refresh() — do not acquire here (nested LOCK_NB would fail).
        out = refresh()
        if not out.get("ok"):
            _log(f"refresh skipped: {out.get('error', 'refresh failed')}")
            return None
        return out
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
        except (TypeError, ValueError):
            age_s = None
    if age_s is None:
        # Missing/unparseable refreshed_at must not look like "no snapshot" —
        # a young file with bad meta is still a throttle hit, not a dump.
        mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        age_s = int((datetime.now(timezone.utc) - mtime).total_seconds())
    return {
        "ok": True,
        "exists": True,
        "path": str(path),
        "refreshed_at": refreshed_at,
        "size_bytes": st.st_size,
        "age_seconds": age_s,
        "counts": m.get("counts"),
    }


SNAPSHOT_BEHIND_INGEST_MAX_S = 30 * 60


def snapshot_freshness(
    latest_ingested_at: str | None, health: dict[str, Any] | None = None
) -> dict[str, Any]:
    """``snapshot_health()`` plus ``behind_ingest_seconds`` (W2.4).

    Pure function of already-known timestamps — no DB access — so
    ``cmd_status``/``cmd_doctor``/MCP ``khipu_status`` (which already have PG's
    ``latest_ingested_at`` from ``status_payload``) can call this instead of
    plain ``snapshot_health()`` while PG is reachable, without this module
    reaching into PG itself. ``ok`` flips false with
    ``reason: "snapshot_behind_ingest"`` once the gap exceeds
    ``SNAPSHOT_BEHIND_INGEST_MAX_S`` (30 min).
    """
    if health is None:
        health = snapshot_health()
    out = dict(health)
    refreshed_at = health.get("refreshed_at")
    if not latest_ingested_at or not refreshed_at:
        out["behind_ingest_seconds"] = None
        return out
    try:
        ing = datetime.fromisoformat(str(latest_ingested_at).replace("Z", "+00:00"))
        ref = datetime.fromisoformat(str(refreshed_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        out["behind_ingest_seconds"] = None
        return out
    behind = (ing - ref).total_seconds()
    out["behind_ingest_seconds"] = behind
    if behind > SNAPSHOT_BEHIND_INGEST_MAX_S:
        out["ok"] = False
        out["reason"] = "snapshot_behind_ingest"
    return out


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


def _id_shaped(term: str) -> bool:
    """Local copy of ``cli._id_shaped`` (W2.2) — this module must not import
    ``khipu.cli`` (that module already imports from here, and CLI/MCP fall
    back to this snapshot search specifically when the hub they'd otherwise
    reach ``cli`` through is down)."""
    t = term or ""
    return ":" in t or "__" in t


def _parse_snapshot_ts(val: Any) -> datetime | None:
    if not val:
        return None
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def search_snapshot(
    query: str,
    limit: int,
    *,
    kind: str | None = None,
    since: str | None = None,
    until: str | None = None,
    project: str | None = None,
    session_id: str | None = None,
    harness: str | None = None,
) -> list[dict[str, Any]]:
    """ILIKE-equivalent search over the sqlite replica (W2.3: honours at
    least kind/since/until when the hub is unreachable; fix 7 adds project/
    session_id/harness so a fallback search doesn't silently drop them).

    project/session_id/harness only exist on episodes — same rule as
    ``embed._apply_search_filters``: when any is active, topic/node rows
    are excluded outright rather than guessed at (an explicit non-episode
    ``kind`` combined with one of these filters yields no results, matching
    the PG-path behaviour exactly)."""
    from khipu.search_text import parse_time_filter, search_tokens
    from khipu.snippets import LABEL_LIMIT, SNIPPET_LIMIT, clip_snippet

    if kind is not None and kind not in ("topic", "episode", "node"):
        raise ValueError("kind must be 'topic', 'episode', or 'node'")
    want_episode_only = bool(project or session_id or harness)
    if want_episode_only:
        active_kinds = ["episode"] if kind in (None, "episode") else []
    else:
        want_nodes = kind == "node" or (kind is None and _id_shaped(query))
        active_kinds = (
            [kind] if kind else (["topic", "episode", "node"] if want_nodes else ["topic", "episode"])
        )
    shares = dict(zip(active_kinds, _fair_shares(limit, len(active_kinds)))) if active_kinds else {}
    topic_n = shares.get("topic", 0)
    episode_n = shares.get("episode", 0)
    node_n = shares.get("node", 0)
    since_dt = parse_time_filter(since) if since else None
    until_dt = parse_time_filter(until) if until else None

    def _in_range(ts_text: Any) -> bool:
        if since_dt is None and until_dt is None:
            return True
        ts = _parse_snapshot_ts(ts_text)
        if ts is None:
            return False
        if since_dt is not None and ts < since_dt:
            return False
        if until_dt is not None and ts > until_dt:
            return False
        return True

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

    # since/until filtering happens in Python after fetch (sqlite ISO-text
    # comparison across mixed offset formats is not reliable), so oversample
    # the LIMIT when either bound is active.
    time_filtered = since_dt is not None or until_dt is not None

    def oversample(n: int) -> int:
        return n * 4 if time_filtered and n > 0 else n

    if topic_n > 0:
        sql = f"""
            SELECT 'topic' AS kind, slug AS id, COALESCE(title, slug) AS label,
                   substr(body, 1, 4000) AS snippet, COALESCE(updated_at, created_at) AS ts
            FROM topics
            WHERE deleted_at IS NULL AND ({topic_where})
            ORDER BY {topic_order}
            LIMIT ?
        """
        rows = con.execute(sql, (*topic_params, oversample(topic_n))).fetchall()
        for r in rows:
            if not _in_range(r[4]):
                continue
            results.append(
                {
                    "kind": r[0],
                    "id": r[1],
                    "label": clip_snippet(r[2], LABEL_LIMIT),
                    "snippet": clip_snippet(r[3], SNIPPET_LIMIT),
                }
            )

    if episode_n > 0:
        ep_cols = _snapshot_table_columns(con, "episodes")
        project_expr = "COALESCE(project, scope)" if "project" in ep_cols else "scope"
        harness_expr = "harness" if "harness" in ep_cols else "NULL"
        # session_id has been a base column since the first snapshot schema;
        # guard anyway (fix 9) rather than assume an ancient dump has it.
        session_expr = "session_id" if "session_id" in ep_cols else "NULL"
        sql = f"""
            SELECT 'episode' AS kind, CAST(id AS TEXT) AS id, summary AS label,
                   summary AS snippet, ts, {session_expr} AS sid,
                   {project_expr} AS proj, {harness_expr} AS harn
            FROM episodes
            WHERE {episode_where}
            ORDER BY {episode_order}
            LIMIT ?
        """
        rows = con.execute(sql, (*episode_params, oversample(episode_n))).fetchall()
        for r in rows:
            if not _in_range(r[4]):
                continue
            row_sid = r[5] or ""
            if project and project.strip().lower() not in (r[6] or "").lower():
                continue
            if session_id and not row_sid.startswith(session_id):
                continue
            if harness:
                row_harness = r[7] or row_sid.split(":", 1)[0]
                if row_harness != harness:
                    continue
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
                   substr(COALESCE(payload, ''), 1, 4000) AS snippet, built_at
            FROM nodes
            WHERE {node_where}
            ORDER BY {node_order}
            LIMIT ?
        """
        rows = con.execute(sql, (*node_params, oversample(node_n))).fetchall()
        for r in rows:
            if not _in_range(r[4]):
                continue
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
    since: str | None = None,
    until: str | None = None,
    project: str | None = None,
    session_id: str | None = None,
    harness: str | None = None,
) -> list[dict[str, Any]]:
    """fix 7: project/session_id/harness, same episode-only semantics as
    ``search_snapshot`` — a topic/media hit is dropped outright when any of
    these is active (they have no such columns to filter on)."""
    if not local_embed_configured():
        raise ValueError(
            "hub unreachable; semantic search requires a configured local embed "
            "provider — use keyword search without --semantic"
        )
    from khipu.models import show_models
    from khipu.search_text import parse_time_filter
    from khipu.snippets import LABEL_LIMIT, SNIPPET_LIMIT, clip_snippet

    since_dt = parse_time_filter(since) if since else None
    until_dt = parse_time_filter(until) if until else None
    want_episode_only = bool(project or session_id or harness)
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
    time_filtered = since_dt is not None or until_dt is not None
    if want_episode_only:
        ep_cols = _snapshot_table_columns(con, "episodes")
        project_expr = "COALESCE(project, scope)" if "project" in ep_cols else "scope"
        harness_expr = "harness" if "harness" in ep_cols else "NULL"
    scored: list[tuple[float, dict[str, Any]]] = []
    for knd, ref, chunk_idx, chunk_text, blob in rows:
        if not blob:
            continue
        if want_episode_only and knd != "episode":
            continue
        score = _cosine(vec, _blob_to_vector(blob))
        label = chunk_text
        ts_val = None
        if knd == "topic":
            trow = con.execute(
                "SELECT COALESCE(title, slug), COALESCE(updated_at, created_at) "
                "FROM topics WHERE slug = ?",
                (ref,),
            ).fetchone()
            if trow:
                label = trow[0] or ref
                ts_val = trow[1]
        elif knd == "episode":
            if want_episode_only:
                erow = con.execute(
                    f"SELECT summary, ts, session_id, {project_expr}, {harness_expr} "
                    f"FROM episodes WHERE CAST(id AS TEXT) = ?",
                    (ref,),
                ).fetchone()
                if not erow:
                    continue
                label, ts_val, row_sid, row_proj, row_harn = erow
                row_sid = row_sid or ""
                if project and project.strip().lower() not in (row_proj or "").lower():
                    continue
                if session_id and not row_sid.startswith(session_id):
                    continue
                if harness and (row_harn or row_sid.split(":", 1)[0]) != harness:
                    continue
                label = label or ref
            else:
                erow = con.execute(
                    "SELECT summary, ts FROM episodes WHERE CAST(id AS TEXT) = ?",
                    (ref,),
                ).fetchone()
                if erow:
                    label = erow[0] or ref
                    ts_val = erow[1]
        if time_filtered:
            ts = _parse_snapshot_ts(ts_val)
            if ts is None:
                continue
            if since_dt is not None and ts < since_dt:
                continue
            if until_dt is not None and ts > until_dt:
                continue
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


def _snapshot_filters_dropped(*, session_id: str | None, harness: str | None) -> list[str]:
    """fix 7: which requested filters this snapshot genuinely cannot honour
    at all (never silent) — project always has a `scope` fallback and
    harness always has a session_id-split fallback, so in practice this is
    only non-empty against a pathologically old snapshot missing even the
    base ``session_id`` column."""
    dropped: list[str] = []
    try:
        con = open_snapshot()
    except FileNotFoundError:
        return dropped
    ep_cols = _snapshot_table_columns(con, "episodes")
    has_session = "session_id" in ep_cols
    if session_id and not has_session:
        dropped.append("session_id")
    if harness and not has_session and "harness" not in ep_cols:
        dropped.append("harness")
    return dropped


def search_stale_payload(
    query: str,
    limit: int,
    *,
    semantic: bool = False,
    kind: str | None = None,
    since: str | None = None,
    until: str | None = None,
    project: str | None = None,
    session_id: str | None = None,
    harness: str | None = None,
) -> dict[str, Any]:
    """Hub-unreachable search fallback (sqlite replica). Honours kind/since/
    until/project/session_id/harness on both the semantic and literal paths
    (W2.3 minimum bar, fix 7 for the metadata filters) — any filter this
    snapshot genuinely cannot honour is named in ``filters_dropped``, never
    silently ignored."""
    filters_dropped = _snapshot_filters_dropped(session_id=session_id, harness=harness)
    if semantic:
        results = semantic_search_snapshot(
            query, limit=limit, kind=kind, since=since, until=until,
            project=project, session_id=session_id, harness=harness,
        )
        con = open_snapshot()
        results = enrich_search_results_snapshot(con, results)
        return {
            "query": query,
            "mode": "semantic",
            "results": results,
            "filters_dropped": filters_dropped,
            **stale_fields(),
        }
    con = open_snapshot()
    results = search_snapshot(
        query, limit, kind=kind, since=since, until=until,
        project=project, session_id=session_id, harness=harness,
    )
    results = merge_outbox_episodes(results)
    results = enrich_search_results_snapshot(con, results)
    return {
        "query": query,
        "mode": "literal",
        "results": results[:limit],
        "filters_dropped": filters_dropped,
        **stale_fields(),
    }
