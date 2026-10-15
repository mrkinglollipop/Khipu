# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
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
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

# psycopg is imported where the hub is actually reached: the per-prompt hook
# reads only the sqlite replica, and the driver's import (200-450 ms on a busy
# machine) used to land inside that hook's wall-clock limit.
if TYPE_CHECKING:
    import psycopg
    from khipu.profiles import ProfileSpec

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
    "decisions",
    "deliverables",
)

# Schema version 2 (Phase 2, session B): the replica gains `decisions` and
# `topics.superseded_by`/`event_at` so validity can be read offline. A reader
# probes columns/tables on the ACTUAL sqlite file (`_snapshot_table_columns`/
# `_snapshot_has_table`) rather than trusting this constant — this is only
# what `refresh()`/`_create_schema()` write for a NEW dump. Version 1 is the
# pre-this-phase shape (still has project/deleted_at/harness/status etc. —
# those predate this phase); a reader given a version-1 replica reports
# validity as unknown rather than raising.
# Version 3 adds `deliverables`, so the per-prompt "You produced ..." line
# never needs the hub. Writers of different versions can share one file (an
# older build's refresh replaces it wholesale, a newer build's incremental
# writes land on an older file), so readers and incremental writers probe for
# the table and never trust this number.
# Version 4 adds the external-content FTS5 tables `episodes_fts`/`topics_fts`
# (trigram) and their triggers; `search_snapshot` uses them when present and
# falls back to LIKE on a file without them.
# Version 5 adds the ledger (projects, words, live rows, their history), so
# `khipu ledger` reads while the hub is away. Removed rows are never in it.
SNAPSHOT_SCHEMA_VERSION = 5

_DELIVERABLE_COLS = (
    "id",
    "project",
    "kind",
    "path",
    "url",
    "title",
    "episode_id",
    "created_at",
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
    # Phase 2, session B: the second existing supersession mechanism
    # (0023_organisation.sql) and the topic's own event time
    # (0022_event_at.sql) — both predate this phase on the hub, neither was
    # exported to the replica before now.
    "superseded_by",
    "event_at",
)
_DECISION_COLS = (
    "id",
    "project",
    "text",
    "decided_at",
    "episode_id",
    "superseded_by",
    "created_at",
    # 0024_decision_evidence.sql columns — present only once that migration
    # has been applied to the hub; _pg_columns' existing "only columns that
    # exist" filter (see _insert_decisions) already handles a hub that is
    # behind.
    "source_kind",
    "evidence",
    "superseded_at",
    "supersede_source",
    "supersede_reason",
    "retracted_at",
    "retract_reason",
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
    data: dict[str, Any] = {}
    try:
        if p.is_file():
            loaded = json.loads(p.read_text(encoding="utf-8"))
            data = loaded if isinstance(loaded, dict) else {"readable": False}
    except (OSError, ValueError):
        data = {"readable": False}
    # The image is authoritative: a copied/removed sidecar must not make an
    # incremental write look like a fresh full replica.
    try:
        con = sqlite3.connect(f"file:{snapshot_path()}?mode=ro", uri=True)
        try:
            if _snapshot_table_columns(con, "snapshot_meta"):
                row = con.execute("SELECT value FROM snapshot_meta WHERE key = 'refreshed_at'").fetchone()
                if row and row[0]:
                    data["refreshed_at"] = row[0]
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        pass
    data.setdefault("exists", snapshot_path().is_file() or p.is_file())
    data.setdefault("path", str(snapshot_path()))
    return data


_SYNC_CURSOR_KEYS = ("last_decision_sync_at", "last_decision_sync_id")


def _read_sidecar() -> dict[str, Any]:
    """The sidecar as written, without the image's own refreshed_at."""
    try:
        loaded = json.loads(meta_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_meta(data: Mapping[str, Any]) -> None:
    dest = meta_path()
    fd, name = tempfile.mkstemp(prefix=".hub_snapshot.meta.", dir=dest.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(name, dest)
    finally:
        Path(name).unlink(missing_ok=True)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(msg: str) -> None:
    print(f"[khipu-snapshot] {msg}", file=sys.stderr, flush=True)


def hub_connection_failed(exc: BaseException) -> bool:
    name = type(exc).__name__
    text = str(exc).lower()
    import psycopg

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
    import psycopg

    from khipu.db import conninfo_with_local_root_cert, resolve_dsn

    return psycopg.connect(
        conninfo_with_local_root_cert(resolve_dsn()),
        connect_timeout=connect_timeout,
    )


def open_snapshot() -> sqlite3.Connection:
    path = snapshot_path()
    if not path.is_file():
        raise FileNotFoundError(f"hub snapshot missing: {path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    cols = _snapshot_table_columns(con, "episodes")
    pending = _pending_erasure_ids(path)
    if "id" in cols:
        blocked = ", ".join(str(i) for i in sorted(pending)) or "-1"
        live = "deleted_at IS NULL AND " if "deleted_at" in cols else ""
        con.execute(
            "CREATE TEMP VIEW episodes AS SELECT * FROM main.episodes "
            f"WHERE {live}id NOT IN ({blocked})"
        )
    embedding_cols = _snapshot_table_columns(con, "memory_embeddings")
    if {"kind", "ref"} <= embedding_cols:
        blocked_refs = ", ".join(f"'{i}'" for i in sorted(pending)) or "'-1'"
        tombstone_filter = ""
        if {"id", "deleted_at"} <= cols:
            tombstone_filter = (
                " AND NOT EXISTS (SELECT 1 FROM main.episodes e "
                "WHERE e.id = m.ref AND e.deleted_at IS NOT NULL)"
            )
        con.execute(
            "CREATE TEMP VIEW memory_embeddings AS "
            "SELECT m.rowid AS rowid, m.* FROM main.memory_embeddings m "
            "WHERE m.kind != 'episode' OR "
            f"(m.ref NOT IN ({blocked_refs}){tombstone_filter})"
        )
    from khipu.paths import isolated_dir

    if isolated_dir() is not None:
        # After the TEMP views: an isolated run never changes the replica.
        con.execute("PRAGMA query_only=ON")
    return con


def _pending_erasure_dir(path: Path | None = None) -> Path:
    path = path or snapshot_path()
    return path.parent / ".hub_snapshot.pending-erasure"


def _pending_erasure_ids(path: Path | None = None) -> set[int]:
    try:
        return {int(p.name) for p in _pending_erasure_dir(path).iterdir() if p.name.isdigit()}
    except FileNotFoundError:
        return set()


def _queue_erasure(episode_id: int, path: Path | None = None) -> bool:
    directory = _pending_erasure_dir(path)
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        marker = directory / str(int(episode_id))
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        return True
    except OSError:
        return False


def _apply_pending_erasures(con: sqlite3.Connection, path: Path) -> None:
    ids = _pending_erasure_ids(path)
    for episode_id in ids:
        con.execute("UPDATE episodes SET deleted_at = COALESCE(deleted_at, ?) WHERE id = ?", (_utcnow_iso(), episode_id))
        con.execute("DELETE FROM memory_embeddings WHERE kind = 'episode' AND ref = ?", (str(episode_id),))
        if _snapshot_table_columns(con, "deliverables"):
            con.execute("DELETE FROM deliverables WHERE episode_id = ?", (episode_id,))


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


def decision_counts_snapshot(
    con: sqlite3.Connection, episode_ids: Sequence[Any]
) -> dict[str, tuple[int, int, int]] | None:
    """current/superseded/retracted decision counts per episode id, from the
    local replica's ``decisions`` table — ONE indexed query (``idx_snapshot_
    decisions_episode_id``), bounded to the caller's own already-small hit
    set. ``None`` (not ``{}``) when the table does not exist on this replica
    (a version-1 dump, or any other reason ``PRAGMA table_info`` comes back
    empty) — a caller must tell that apart from "checked, found nothing" (an
    empty dict), which ``khipu.validity.annotate`` treats as unknown vs.
    current respectively. Never raises: a bad connection object degrades to
    the same ``None`` a missing table would."""
    if not episode_ids:
        return {}
    cols = _snapshot_table_columns(con, "decisions")
    if not cols:
        return None
    try:
        has_retract = "retracted_at" in cols
        retract_expr = "retracted_at IS NOT NULL" if has_retract else "0"
        placeholders = ",".join("?" for _ in episode_ids)
        rows = con.execute(
            f"""
            SELECT CAST(episode_id AS TEXT),
                   SUM(CASE WHEN NOT ({retract_expr}) AND superseded_by IS NULL THEN 1 ELSE 0 END),
                   SUM(CASE WHEN NOT ({retract_expr}) AND superseded_by IS NOT NULL THEN 1 ELSE 0 END),
                   SUM(CASE WHEN ({retract_expr}) THEN 1 ELSE 0 END)
            FROM decisions
            WHERE CAST(episode_id AS TEXT) IN ({placeholders})
            GROUP BY episode_id
            """,
            [str(e) for e in episode_ids],
        ).fetchall()
        return {eid: (int(c or 0), int(s or 0), int(r or 0)) for eid, c, s, r in rows}
    except Exception:  # noqa: BLE001 — validity is best-effort metadata, never a search failure
        return None


def recent_deliverables_snapshot(
    con: sqlite3.Connection, *, project: str, limit: int = 200
) -> list[dict[str, Any]] | None:
    """The same rows, order and forgotten-episode exclusion as
    ``khipu.deliverables.recent_deliverables``, read from the replica.
    ``None`` (not ``[]``) when this replica has no usable ``deliverables``
    table (an older dump, or an unreadable one) so a caller can tell "cannot
    answer" from "checked, found nothing"."""
    cols = ("id", "project", "kind", "path", "url", "title", "episode_id", "created_at")
    if not set(cols) <= _snapshot_table_columns(con, "deliverables"):
        return None
    if not project:
        return []
    extra = ""
    if "deleted_at" in _snapshot_table_columns(con, "episodes"):
        extra = (
            " AND (deliverables.episode_id IS NULL OR NOT EXISTS ("
            "SELECT 1 FROM main.episodes e WHERE e.id = deliverables.episode_id "
            "AND e.deleted_at IS NOT NULL))"
        )
    pending = _pending_erasure_ids(snapshot_path())
    if pending:
        blocked = ", ".join(str(int(i)) for i in sorted(pending))
        extra += f" AND (episode_id IS NULL OR episode_id NOT IN ({blocked}))"
    try:
        rows = con.execute(
            f"SELECT {', '.join(cols)} FROM deliverables WHERE project = ?{extra} "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (project, limit),
        ).fetchall()
    except sqlite3.Error:
        return None
    return [dict(zip(cols, row)) for row in rows]


def topic_validity_meta_snapshot(
    con: sqlite3.Connection, slugs: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """``{slug: {"status": ..., "superseded_by": ...}}`` for ``khipu.validity.
    annotate``'s ``topic_meta`` argument, read straight from the replica's
    ``topics`` table — used by ``search_stale_payload``, whose rows (unlike
    the local-lane's, which already carry status from ``snapshot_row_
    metadata``) have neither field on them yet. ``superseded_by`` is absent
    on a version-1 replica; probed, never assumed."""
    if not slugs:
        return {}
    has_superseded_by = "superseded_by" in _snapshot_table_columns(con, "topics")
    superseded_expr = "superseded_by" if has_superseded_by else "NULL"
    placeholders = ",".join("?" for _ in slugs)
    try:
        rows = con.execute(
            f"SELECT slug, status, {superseded_expr} FROM topics WHERE slug IN ({placeholders})",
            list(slugs),
        ).fetchall()
    except Exception:  # noqa: BLE001 — validity metadata is best-effort
        return {}
    return {slug: {"status": status, "superseded_by": superseded_by} for slug, status, superseded_by in rows}


def apply_decision_changes(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Upsert decision rows into the local replica's ``decisions`` table —
    the write half of validity's local-lane visibility (Phase 2, session B):
    ``khipu.decisions`` calls this right after a successful supersede/
    restore/retract on THIS machine so the local lane sees the correction at
    once; ``sync_decision_changes`` below calls it for corrections made
    elsewhere. Same lock/fail-open contract as ``upsert_episode``: a no-op
    ``{"ok": False}`` when the snapshot is missing, too old to have a
    ``decisions`` table, or a full dump is in progress — never a raise.
    """
    if not rows:
        return {"ok": True, "decisions": 0}
    path = snapshot_path()
    if not path.is_file():
        return {"ok": False, "error": "hub snapshot missing"}
    lock = _acquire_refresh_lock()
    if lock is None:
        return {"ok": False, "error": "hub snapshot refresh in progress"}
    try:
        con = sqlite3.connect(str(path))
        try:
            written = _write_decision_rows(con, rows)
            if written is None:
                return {"ok": False, "error": "replica has no decisions table (version 1)"}
            con.commit()
        finally:
            con.close()
        return {"ok": True, "decisions": written}
    except Exception as exc:  # noqa: BLE001 — the PG write already succeeded
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        _release_refresh_lock(lock)


def _write_decision_rows(con: sqlite3.Connection, rows: Sequence[Mapping[str, Any]]) -> int | None:
    """Insert-or-replace ``rows`` into ``con``'s ``decisions`` table. Caller
    owns the transaction (commit/rollback) and any locking — this never
    commits or acquires a lock itself, so ``upsert_episode`` (already
    holding the refresh lock) and ``apply_decision_changes`` (which acquires
    its own) can both use it without a reentrant-lock risk. Returns ``None``
    (not 0) when the replica has no ``decisions`` table at all, so a caller
    can tell that apart from "zero rows written"."""
    cols_present = _snapshot_table_columns(con, "decisions")
    if not cols_present:
        return None
    written = 0
    ts_cols = {"decided_at", "created_at", "superseded_at", "retracted_at"}
    for row in rows:
        rid = row.get("id")
        if rid is None:
            continue
        cols = [c for c in _DECISION_COLS if c in row and c in cols_present]
        if "id" not in cols:
            cols = ["id", *cols]
        vals = []
        for c in cols:
            v = row.get(c)
            if c == "evidence":
                v = _json_text(v)
            elif c in ts_cols:
                v = _ts_text(v)
            vals.append(v)
        con.execute("DELETE FROM decisions WHERE id = ?", (rid,))
        con.execute(
            f"INSERT INTO decisions ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})",
            vals,
        )
        written += 1
    return written


def sync_decision_changes(*, limit: int = 500) -> dict[str, Any]:
    """Pull decision rows changed on the hub since this replica's last sync
    and mirror them locally — "corrections made elsewhere" (Phase 2, session
    B): the capture drain, which already talks to the hub for every capture,
    calls this so a supersession made on a DIFFERENT machine reaches this
    machine's local recall lane without a dedicated sync job. No hook may
    call this directly (a per-prompt/session-start hook must never open a
    hub connection on its own).

    Bounded to ``limit`` rows per call, oldest-changed first, ordered so a
    caller that never quite catches up still makes forward progress instead
    of re-scanning the same window. Its own watermark
    (``last_decision_sync_at``) lives in the meta file, separate from
    ``refreshed_at`` (a full dump does not imply this has run, and vice
    versa). Fail-open throughout: any failure — hub unreachable, replica
    missing or too old — returns ``{"ok": False, ...}``, never raises.
    """
    path = snapshot_path()
    if not path.is_file():
        return {"ok": False, "error": "hub snapshot missing"}
    con = sqlite3.connect(str(path))
    try:
        if not _snapshot_table_columns(con, "decisions"):
            return {"ok": False, "error": "replica has no decisions table (version 1)"}
    finally:
        con.close()
    m = meta()
    since = m.get("last_decision_sync_at")
    try:
        since_id = int(m.get("last_decision_sync_id") or 0)
    except (TypeError, ValueError):
        since_id = 0
    try:
        with try_hub_connect() as pg:
            with pg.cursor() as cur:
                cols = [c for c in _DECISION_COLS if c in _pg_columns(cur, "decisions")]
                if "id" not in cols:
                    return {"ok": False, "error": "hub decisions table not migrated"}
                watermark_expr = "created_at"
                if "superseded_at" in cols:
                    watermark_expr = f"GREATEST({watermark_expr}, COALESCE(superseded_at, {watermark_expr}))"
                if "retracted_at" in cols:
                    watermark_expr = f"GREATEST({watermark_expr}, COALESCE(retracted_at, {watermark_expr}))"
                sel = ", ".join(cols)
                params: list[Any] = []
                where = ""
                if since:
                    where = f"WHERE ({watermark_expr}, id) > (%s, %s)"
                    params.extend((since, since_id))
                cur.execute(
                    f"SELECT {sel}, {watermark_expr} AS _watermark FROM decisions "
                    f"{where} ORDER BY _watermark ASC, id ASC LIMIT %s",
                    (*params, limit),
                )
                fetched = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 — fail-open, same posture as maybe_refresh
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if not fetched:
        return {"ok": True, "decisions": 0}
    rows = [dict(zip(cols, r[:-1])) for r in fetched]
    out = apply_decision_changes(rows)
    if out.get("ok"):
        new_watermark = fetched[-1][-1]
        m["last_decision_sync_at"] = _ts_text(new_watermark) or _utcnow_iso()
        m["last_decision_sync_id"] = rows[-1].get("id", 0)
        try:
            _write_meta(m)
        except OSError:
            pass
    out.setdefault("decisions", len(rows))
    return out


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


def _create_schema(con: sqlite3.Connection, *, version: int = SNAPSHOT_SCHEMA_VERSION) -> None:
    """``version=4`` (default): the current shape, always what ``refresh()``
    writes for a real dump. ``version=3``: the same without the FTS5 tables.
    ``version=2``: the same without ``deliverables``.
    ``version=1``: the pre-Phase-2B shape (no
    ``decisions`` table, no ``topics.superseded_by``/``event_at``, no
    ``snapshot_meta``) — for tests exercising a reader's tolerance of an
    older replica. Every reader in this module probes the ACTUAL file
    (``_snapshot_table_columns``/``_snapshot_has_table``), never this
    parameter, so a version-1 replica behaves exactly as a real one dumped
    before this phase would."""
    topic_extra_cols = ",\n            superseded_by TEXT,\n            event_at TEXT" if version >= 2 else ""
    con.executescript(
        f"""
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
            deleted_at TEXT{topic_extra_cols}
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
        CREATE INDEX IF NOT EXISTS idx_edges_src_type ON edges (src, type);
        CREATE INDEX IF NOT EXISTS idx_edges_dst_type ON edges (dst, type);
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
    if version >= 2:
        con.executescript(
            """
            CREATE TABLE decisions (
                id INTEGER PRIMARY KEY,
                project TEXT,
                text TEXT,
                decided_at TEXT,
                episode_id INTEGER,
                superseded_by INTEGER,
                created_at TEXT,
                source_kind TEXT,
                evidence TEXT,
                superseded_at TEXT,
                supersede_source TEXT,
                supersede_reason TEXT,
                retracted_at TEXT,
                retract_reason TEXT
            );
            CREATE INDEX idx_snapshot_decisions_episode_id ON decisions (episode_id);

            CREATE TABLE snapshot_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        con.execute(
            "INSERT OR REPLACE INTO snapshot_meta (key, value) VALUES ('schema_version', ?)",
            (str(version),),
        )
    if version >= 3:
        con.executescript(
            """
            CREATE TABLE deliverables (
                id INTEGER PRIMARY KEY,
                project TEXT,
                kind TEXT,
                path TEXT,
                url TEXT,
                title TEXT,
                episode_id INTEGER,
                created_at TEXT
            );
            CREATE INDEX idx_snapshot_deliverables_project ON deliverables (project);
            """
        )
    if version >= 5:
        con.executescript(_LEDGER_SCHEMA_SQL)
    if version >= 4:
        try:
            con.executescript(_FTS_SCHEMA_SQL)
        except sqlite3.OperationalError as exc:
            # A Python whose SQLite lacks FTS5 still writes a usable replica;
            # search_snapshot finds no FTS tables and stays on LIKE.
            _log(f"replica FTS tables not created: {exc}")


_LEDGER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ledger_projects (
    project TEXT PRIMARY KEY, name TEXT, hidden INTEGER, created_at TEXT
);
CREATE TABLE IF NOT EXISTS ledger_vocab (
    project TEXT, field TEXT, word TEXT, meaning TEXT, position INTEGER,
    PRIMARY KEY (project, field, word)
);
CREATE TABLE IF NOT EXISTS ledger_rows (
    project TEXT, row_id TEXT, title TEXT, kind TEXT, parent TEXT, state TEXT, owner TEXT,
    evidence TEXT, link TEXT, refs TEXT, seq INTEGER, created_at TEXT, updated_at TEXT,
    version INTEGER, PRIMARY KEY (project, row_id)
);
CREATE TABLE IF NOT EXISTS ledger_history (
    id INTEGER PRIMARY KEY, project TEXT, row_id TEXT, at TEXT, at_text TEXT, author TEXT,
    shape TEXT, from_state TEXT, to_state TEXT, changes TEXT, note TEXT, event TEXT, raw TEXT,
    source TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshot_ledger_history_row ON ledger_history (project, row_id, id);
CREATE TABLE IF NOT EXISTS ledger_gone (
    project TEXT, row_id TEXT, version INTEGER, PRIMARY KEY (project, row_id)
);
"""
# A purged id never comes back, so its marker outranks every version.
LEDGER_PURGED_VERSION = 2**62
_LEDGER_ROW_COLS = ("project", "row_id", "title", "kind", "parent", "state", "owner", "evidence", "link",
                    "refs", "seq", "created_at", "updated_at", "version")
_LEDGER_HIST_COLS = ("id", "project", "row_id", "at", "at_text", "author", "shape", "from_state", "to_state",
                     "changes", "note", "event", "raw", "source")
_LEDGER_JSON_COLS = frozenset({"refs", "author", "changes", "raw"})
_LEDGER_TS_COLS = frozenset({"created_at", "updated_at", "at"})


def _ledger_value(col: str, val: Any) -> Any:
    if col in _LEDGER_JSON_COLS:
        return _json_text(val)
    if col in _LEDGER_TS_COLS:
        return _ts_text(val)
    return val


def _insert_ledger(cur, con: sqlite3.Connection) -> int:
    """The ledger's live rows (never a removed one), their history, and each
    project's words. Empty (0, no error) on a hub without migration 0028."""
    if "row_id" not in _pg_columns(cur, "ledger_rows"):
        return 0
    con.executescript(_LEDGER_SCHEMA_SQL)
    cur.execute("SELECT project, name, hidden, created_at FROM ledger_projects")
    for project, name, hidden, created in cur.fetchall():
        con.execute("INSERT INTO ledger_projects VALUES (?, ?, ?, ?)", (project, name, int(bool(hidden)),
                                                                         _ts_text(created)))
    cur.execute("SELECT project, field, word, meaning, position FROM ledger_vocab")
    con.executemany("INSERT INTO ledger_vocab VALUES (?, ?, ?, ?, ?)", cur.fetchall())
    cur.execute(f"SELECT {', '.join(_LEDGER_ROW_COLS)} FROM ledger_rows WHERE removed_at IS NULL")
    rows = cur.fetchall()
    con.executemany(
        f"INSERT INTO ledger_rows ({', '.join(_LEDGER_ROW_COLS)}) VALUES ({', '.join('?' * len(_LEDGER_ROW_COLS))})",
        [[_ledger_value(c, v) for c, v in zip(_LEDGER_ROW_COLS, r, strict=True)] for r in rows],
    )
    sel = ", ".join(f"h.{c}" for c in _LEDGER_HIST_COLS)
    cur.execute(f"SELECT {sel} FROM ledger_history h JOIN ledger_rows r ON r.project = h.project "
                "AND r.row_id = h.row_id WHERE r.removed_at IS NULL ORDER BY h.id")
    con.executemany(
        f"INSERT INTO ledger_history ({', '.join(_LEDGER_HIST_COLS)}) "
        f"VALUES ({', '.join('?' * len(_LEDGER_HIST_COLS))})",
        [[_ledger_value(c, v) for c, v in zip(_LEDGER_HIST_COLS, h, strict=True)] for h in cur.fetchall()],
    )
    # Removed and purged ids, so a write-through that read the row before it
    # went cannot put it back after this dump.
    cur.execute("SELECT project, row_id, version FROM ledger_rows WHERE removed_at IS NOT NULL")
    gone = cur.fetchall()
    cur.execute("SELECT project, row_id FROM ledger_removed")
    gone += [(p, r, LEDGER_PURGED_VERSION) for p, r in cur.fetchall()]
    con.executemany("INSERT OR REPLACE INTO ledger_gone VALUES (?, ?, ?)", gone)
    return len(rows)


def ledger_write_snapshot(project: str, proj: Sequence[Any] | None, vocab: Mapping[str, list],
                          rows: Sequence[Mapping[str, Any]], history: Sequence[Mapping[str, Any]],
                          row_ids: Sequence[str], gone: Mapping[str, int] | None = None) -> dict[str, Any]:
    """Write ``row_ids`` of ``project`` through to the replica: each live one
    in ``rows`` (with its whole ``history``), each removed one in ``gone`` at
    the version it was removed at, and any other id as purged. A row and its
    history are replaced whole, and only by a newer or equal revision: its
    version, then its last history id (a late conditional write adds history
    without a new version). A removed or purged id leaves a marker, so a writer
    that read the hub before a newer change and lands after it changes
    nothing. Under the refresh lock, like every incremental write here.
    Fail-open."""
    path = snapshot_path()
    # Missing is judged under the lock: a first refresh still dumping has no
    # file yet, but may have read the hub before this change.
    lock = _acquire_refresh_lock()
    if lock is None:
        return {"ok": False, "error": "hub snapshot refresh in progress"}
    if not path.is_file():
        _release_refresh_lock(lock)
        return {"ok": False, "error": "hub snapshot missing", "missing": True}
    gone = dict(gone or {})
    live = {r["row_id"]: r for r in rows}
    for row_id in row_ids:
        if row_id not in live and row_id not in gone:
            gone[row_id] = LEDGER_PURGED_VERSION
    try:
        con = sqlite3.connect(str(path))
        try:
            con.executescript(_LEDGER_SCHEMA_SQL)

            def held(row_id: str) -> tuple[int | None, int | None]:
                got = con.execute("SELECT version FROM ledger_rows WHERE project = ? AND row_id = ?",
                                  (project, row_id)).fetchone()
                mark = con.execute("SELECT version FROM ledger_gone WHERE project = ? AND row_id = ?",
                                   (project, row_id)).fetchone()
                return (got[0] if got else None), (mark[0] if mark else None)

            def held_last_entry(row_id: str) -> int:
                got = con.execute("SELECT MAX(id) FROM ledger_history WHERE project = ? AND row_id = ?",
                                  (project, row_id)).fetchone()
                return int(got[0] or 0) if got else 0

            last_entry: dict[str, int] = {}
            for h in history:
                last_entry[h.get("row_id")] = max(last_entry.get(h.get("row_id"), 0), int(h.get("id") or 0))

            def drop(row_id: str) -> None:
                con.execute("DELETE FROM ledger_rows WHERE project = ? AND row_id = ?", (project, row_id))
                con.execute("DELETE FROM ledger_history WHERE project = ? AND row_id = ?", (project, row_id))

            if proj is not None:
                con.execute("INSERT OR REPLACE INTO ledger_projects VALUES (?, ?, ?, ?)",
                            (project, proj[0], int(bool(proj[1])), _ts_text(proj[2])))
            con.execute("DELETE FROM ledger_vocab WHERE project = ?", (project,))
            con.executemany("INSERT INTO ledger_vocab VALUES (?, 'state', ?, ?, ?)",
                            [(project, w, m, i) for i, (w, m) in enumerate(vocab.get("state") or [])])
            con.executemany("INSERT INTO ledger_vocab VALUES (?, 'kind', ?, '', ?)",
                            [(project, w, i) for i, w in enumerate(vocab.get("kind") or [])])
            written, stale = [], 0
            for row_id, row in live.items():
                have, mark = held(row_id)
                version = int(row.get("version") or 0)
                if max(have or 0, mark or 0) > version or (
                        have == version and held_last_entry(row_id) > last_entry.get(row_id, 0)):
                    stale += 1
                    continue
                drop(row_id)
                con.execute("DELETE FROM ledger_gone WHERE project = ? AND row_id = ?", (project, row_id))
                values = {**row, "project": project}
                con.execute(
                    f"INSERT INTO ledger_rows ({', '.join(_LEDGER_ROW_COLS)}) "
                    f"VALUES ({', '.join('?' * len(_LEDGER_ROW_COLS))})",
                    [_ledger_value(c, values.get(c)) for c in _LEDGER_ROW_COLS],
                )
                written.append(row_id)
            for row_id, version in gone.items():
                have, mark = held(row_id)
                if have is not None and have > version:
                    stale += 1
                    continue
                drop(row_id)
                con.execute("INSERT OR REPLACE INTO ledger_gone VALUES (?, ?, ?)",
                            (project, row_id, max(version, mark or 0)))
            for h in history:
                if h.get("row_id") not in written:
                    continue
                values = {**h, "project": project}
                con.execute(
                    f"INSERT OR REPLACE INTO ledger_history ({', '.join(_LEDGER_HIST_COLS)}) "
                    f"VALUES ({', '.join('?' * len(_LEDGER_HIST_COLS))})",
                    [_ledger_value(c, values.get(c)) for c in _LEDGER_HIST_COLS],
                )
            con.commit()
        finally:
            con.close()
        return {"ok": True, "rows": len(written), "stale": stale}
    except Exception as exc:  # noqa: BLE001  (the hub write already landed)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        _release_refresh_lock(lock)


def ledger_in_snapshot() -> bool:
    try:
        con = sqlite3.connect(f"file:{snapshot_path()}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        return bool(_snapshot_table_columns(con, "ledger_rows"))
    finally:
        con.close()


def ledger_read_snapshot(project: str) -> dict[str, Any] | None:
    """``project``'s words, live rows and history from the replica, with the
    replica's time; None when the replica or its ledger tables are missing."""
    if not snapshot_path().is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{snapshot_path()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        if not _snapshot_table_columns(con, "ledger_rows"):
            return None
        vocab: dict[str, list] = {"state": [], "kind": []}
        for field, word, meaning in con.execute(
                "SELECT field, word, meaning FROM ledger_vocab WHERE project = ? ORDER BY field, position",
                (project,)):
            if field == "state":
                vocab["state"].append([word, meaning])
            else:
                vocab["kind"].append(word)
        cols = [c for c in _LEDGER_ROW_COLS if c != "project"]
        rows = [dict(zip(cols, r, strict=True)) for r in con.execute(
            f"SELECT {', '.join(cols)} FROM ledger_rows WHERE project = ? ORDER BY seq", (project,))]
        hcols = [c for c in _LEDGER_HIST_COLS if c != "project"]
        history = []
        for h in con.execute(f"SELECT {', '.join(hcols)} FROM ledger_history WHERE project = ? ORDER BY id",
                             (project,)):
            entry = dict(zip(hcols, h, strict=True))
            for key in _LEDGER_JSON_COLS & set(entry):
                if isinstance(entry[key], str):
                    entry[key] = json.loads(entry[key])
            history.append(entry)
        gone: dict[str, int] = {}
        if _snapshot_table_columns(con, "ledger_gone"):
            gone = dict(con.execute("SELECT row_id, version FROM ledger_gone WHERE project = ?", (project,)))
    except (sqlite3.Error, ValueError):
        return None
    finally:
        con.close()
    return {"vocab": vocab, "rows": rows, "history": history, "gone": gone, "as_of": meta().get("refreshed_at")}


_EPISODE_FTS_COLS = ("summary", "topics", "decisions", "preferences", "people")
_TOPIC_FTS_COLS = ("body", "slug", "title")


def _fts_table_sql(fts: str, base: str, cols: tuple[str, ...]) -> str:
    """External-content FTS5 table over ``base`` plus the triggers that keep
    it current, so every writer of ``base`` needs no FTS code. A ``delete``
    must pass the exact values that were indexed, which ``old.*`` does.
    Trigram phrases are substring matches, which is what ``LIKE '%tok%'``
    does; no ``remove_diacritics``, because LIKE does not strip them."""
    col_list = ", ".join(cols)
    new_vals = ", ".join(f"new.{c}" for c in cols)
    old_vals = ", ".join(f"old.{c}" for c in cols)
    return f"""
        CREATE VIRTUAL TABLE {fts} USING fts5(
            {col_list}, content='{base}', content_rowid='rowid',
            tokenize='trigram'
        );
        CREATE TRIGGER {fts}_ai AFTER INSERT ON {base} BEGIN
            INSERT INTO {fts}(rowid, {col_list}) VALUES (new.rowid, {new_vals});
        END;
        CREATE TRIGGER {fts}_ad AFTER DELETE ON {base} BEGIN
            INSERT INTO {fts}({fts}, rowid, {col_list}) VALUES ('delete', old.rowid, {old_vals});
        END;
        CREATE TRIGGER {fts}_au AFTER UPDATE ON {base} BEGIN
            INSERT INTO {fts}({fts}, rowid, {col_list}) VALUES ('delete', old.rowid, {old_vals});
            INSERT INTO {fts}(rowid, {col_list}) VALUES (new.rowid, {new_vals});
        END;
    """


_FTS_SCHEMA_SQL = (
    _fts_table_sql("episodes_fts", "episodes", _EPISODE_FTS_COLS)
    + _fts_table_sql("topics_fts", "topics", _TOPIC_FTS_COLS)
)


_EDGE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_edges_src_type ON edges (src, type)",
    "CREATE INDEX IF NOT EXISTS idx_edges_dst_type ON edges (dst, type)",
)
_EDGE_INDEXES_TRIED: set[str] = set()


def ensure_edge_indexes() -> None:
    """Add the graph-leg edge indexes to a replica dumped before they existed.

    Without them every ``edges`` lookup by ``dst`` is a full scan of ~200,000
    rows (2026-10-12). One attempt per replica path per process; a read-only,
    locked or missing replica is logged and the leg runs without them — this
    is called from the prompt path and must never raise into it."""
    path = snapshot_path()
    if str(path) in _EDGE_INDEXES_TRIED:
        return
    from khipu.paths import isolated_dir

    if isolated_dir() is not None:
        # An isolated run (the promotion gate's replay) may be reading the
        # live replica through a link: no maintenance, read only.
        return
    _EDGE_INDEXES_TRIED.add(str(path))
    try:
        # mode=rw: never create an empty replica where none exists.
        con = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=0.25)
        try:
            names = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'edges'"
            )}
            if not {"idx_edges_src_type", "idx_edges_dst_type"} <= names:
                for sql in _EDGE_INDEX_SQL:
                    con.execute(sql)
                con.commit()
        finally:
            con.close()
    except sqlite3.Error as exc:
        _log(f"replica edge indexes not created: {type(exc).__name__}: {exc}")


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
    hub_cols = _pg_columns(cur, "embedding_profiles")
    cols = [c for c in _PROFILE_COLS if c in hub_cols]
    select_cols = [*cols, *(["endpoint"] if "endpoint" in hub_cols else [])]
    sel = ", ".join(select_cols)
    cur.execute(f"SELECT {sel} FROM embedding_profiles ORDER BY id")
    rows = cur.fetchall()
    ph = ", ".join(cols)
    for row in rows:
        record = dict(zip(select_cols, row, strict=True))
        vals = []
        for col in cols:
            val = record[col]
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
        if {"id", "provider", "model", "dim"} <= record.keys():
            spec = {
                "id": record["id"], "provider": record["provider"],
                "model": record["model"], "dim": int(record["dim"]),
                "normalize": record.get("normalize") or "l2",
                "endpoint": record.get("endpoint") or None,
            }
            con.execute(
                "INSERT OR REPLACE INTO snapshot_meta (key, value) VALUES (?, ?)",
                (f"embedding_profile:{record['id']}", json.dumps(spec, sort_keys=True)),
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


def _insert_decisions(cur, con: sqlite3.Connection) -> int:
    """Phase 2, session B. Empty (returns 0, no error) on a hub still at
    migration 0019 — ``_pg_columns`` only selects columns that exist there,
    so this degrades to the six pre-0024 columns exactly like every other
    ``khipu.decisions`` reader/writer does."""
    cols = [c for c in _DECISION_COLS if c in _pg_columns(cur, "decisions")]
    if not cols:
        return 0
    sel = ", ".join(cols)
    cur.execute(f"SELECT {sel} FROM decisions ORDER BY id")
    rows = cur.fetchall()
    ph = ", ".join(cols)
    ts_cols = {"decided_at", "created_at", "superseded_at", "retracted_at"}
    for row in rows:
        vals = []
        for col, val in zip(cols, row, strict=True):
            if col == "evidence":
                vals.append(_json_text(val))
            elif col in ts_cols:
                vals.append(_ts_text(val))
            else:
                vals.append(val)
        con.execute(
            f"INSERT INTO decisions ({ph}) VALUES ({', '.join('?' * len(cols))})",
            vals,
        )
    return len(rows)


def _insert_deliverables(cur, con: sqlite3.Connection) -> int:
    """Empty (returns 0, no error) on a hub with no ``deliverables`` table.
    Rows of a forgotten episode are never exported, the same exclusion
    ``khipu.deliverables.recent_deliverables`` applies at read time."""
    hub_cols = _pg_columns(cur, "deliverables")
    if not {"id", "project", "kind"} <= hub_cols:
        return 0
    cols = [c for c in _DELIVERABLE_COLS if c in hub_cols]
    sel = ", ".join(cols)
    where = ""
    if "episode_id" in cols and "deleted_at" in _pg_columns(cur, "episodes"):
        where = (
            " WHERE (episode_id IS NULL OR NOT EXISTS ("
            "SELECT 1 FROM episodes e WHERE e.id = deliverables.episode_id "
            "AND e.deleted_at IS NOT NULL))"
        )
    cur.execute(f"SELECT {sel} FROM deliverables{where} ORDER BY id")
    rows = cur.fetchall()
    for row in rows:
        vals = [
            _ts_text(val) if col == "created_at" else val
            for col, val in zip(cols, row, strict=True)
        ]
        con.execute(
            f"INSERT INTO deliverables ({sel}) VALUES ({', '.join('?' * len(cols))})",
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
        previous_meta = _read_sidecar()
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
                    counts["decisions"] = _insert_decisions(cur, con)
                    counts["deliverables"] = _insert_deliverables(cur, con)
                    counts["ledger_rows"] = _insert_ledger(cur, con)
            refreshed_at = _utcnow_iso()
            if _snapshot_table_columns(con, "snapshot_meta"):
                con.execute("INSERT OR REPLACE INTO snapshot_meta (key, value) VALUES ('refreshed_at', ?)", (refreshed_at,))
            _apply_pending_erasures(con, dest)
            con.commit()
            con.close()
            os.replace(tmp_path, dest)
            size_bytes = dest.stat().st_size
            new_meta: dict[str, Any] = {"schema_version": SNAPSHOT_SCHEMA_VERSION, "refreshed_at": refreshed_at, "size_bytes": size_bytes, "counts": counts}
            # The decision-sync cursor is not evidence about this dump, so a
            # dump must not reset it: with the keys gone the next sync starts
            # from the oldest decision and replays months of history 500 rows
            # per drain (seen live 2026-10-11, cursor a month behind).
            for key in _SYNC_CURSOR_KEYS:
                if key in previous_meta:
                    new_meta[key] = previous_meta[key]
            _write_meta(new_meta)
            return {
                "ok": True,
                "path": str(dest),
                "refreshed_at": refreshed_at,
                "size_bytes": size_bytes,
                "counts": counts,
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
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


def _write_deliverable_rows(
    con: sqlite3.Connection, episode_id: Any, rows: Sequence[Mapping[str, Any]]
) -> None:
    """Replace one episode's deliverables on ``con``. A no-op when this
    replica has no (or a partial) ``deliverables`` table, so an older dump is
    left exactly as it was. Caller holds the refresh lock and commits."""
    if not set(_DELIVERABLE_COLS) <= _snapshot_table_columns(con, "deliverables"):
        return
    con.execute("DELETE FROM deliverables WHERE episode_id = ?", (episode_id,))
    sel = ", ".join(_DELIVERABLE_COLS)
    for row in rows:
        vals = [
            _ts_text(row.get(c)) if c == "created_at" else row.get(c)
            for c in _DELIVERABLE_COLS
        ]
        con.execute(
            f"INSERT OR REPLACE INTO deliverables ({sel}) "
            f"VALUES ({', '.join('?' * len(_DELIVERABLE_COLS))})",
            vals,
        )


def upsert_episode(
    episode_row: Mapping[str, Any],
    embedding_rows: Sequence[Mapping[str, Any]],
    decision_rows: Sequence[Mapping[str, Any]] = (),
    deliverable_rows: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Incremental snapshot update: one episode + its embedding chunks (W2.4),
    plus (Phase 2, session B) any decision rows minted for it in the same
    capture — additive and optional, so every existing caller (just
    ``episode_row``/``embedding_rows``) is unchanged. Written via
    ``apply_decision_changes`` under the SAME lock this function already
    holds, so a full dump never interleaves with either half.

    ``deliverable_rows`` (the hub's rows for this episode) REPLACE whatever
    the replica holds for it, so a re-capture cannot duplicate; ``None``
    leaves the replica's deliverables alone. Skipped on a replica with no
    ``deliverables`` table.

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
        pending_erasure = int(eid) in _pending_erasure_ids(path)
        con = sqlite3.connect(str(path))
        try:
            ep_cols = _snapshot_table_columns(con, "episodes")
            cursor = con.execute("SELECT * FROM episodes WHERE id = ?", (eid,))
            existing = cursor.fetchone()
            names = [d[0] for d in cursor.description]
            merged = dict(zip(names, existing or (), strict=False))
            # Capture payloads commonly omit identity fields. Preserve them,
            # and keep a durable tombstone monotonic across a late embed.
            identity_cols = {"project", "scope", "session_id", "harness", "repo_root", "parent_session_id"}
            merged.update({
                c: v for c, v in episode_row.items()
                if c in ep_cols and not (c in identity_cols and v is None and existing)
            })
            if existing and "deleted_at" in names and existing[names.index("deleted_at")] is not None:
                merged["deleted_at"] = existing[names.index("deleted_at")]
            if pending_erasure:
                merged["deleted_at"] = merged.get("deleted_at") or _utcnow_iso()
            con.execute("DELETE FROM episodes WHERE id = ?", (eid,))
            cols = [c for c in _EPISODE_COLS if c in merged and c in ep_cols]
            vals = []
            for c in cols:
                v = merged.get(c)
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
            if merged.get("deleted_at") is not None:
                con.execute("DELETE FROM memory_embeddings WHERE kind = 'episode' AND ref = ?", (str(eid),))
            else:
                for profile in {row["profile"] for row in embedding_rows}:
                    con.execute(
                        "DELETE FROM memory_embeddings WHERE kind = 'episode' AND ref = ? AND profile = ?",
                        (str(eid), profile),
                    )
            for erow in (() if merged.get("deleted_at") is not None else embedding_rows):
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
            if decision_rows:
                # Best-effort: an old (version-1) replica has no decisions
                # table at all — _write_decision_rows returns None, not an
                # error, for that case, and the episode/embedding write
                # above must still commit regardless.
                _write_decision_rows(con, decision_rows)
            if merged.get("deleted_at") is not None:
                _write_deliverable_rows(con, eid, ())
            elif deliverable_rows is not None:
                _write_deliverable_rows(con, eid, deliverable_rows)
            con.commit()
        finally:
            con.close()
        m = meta()
        m["last_incremental_upsert_at"] = _utcnow_iso()
        m["last_incremental_episode_id"] = eid
        _write_meta(m)
        return {"ok": True, "episode_id": eid, "embeddings": len(embedding_rows)}
    finally:
        _release_refresh_lock(lock)


def upsert_embeddings(
    embedding_rows: Sequence[Mapping[str, Any]], *,
    topic_rows: Sequence[Mapping[str, Any]] = (),
    replace_refs: Sequence[tuple[str, str, str]] = (),
) -> dict[str, Any]:
    """Incremental vectors with optional topic sources and complete ref replacement.
    Replacements affect only the named profiles, including an empty chunk set.
    Used by ``embed.embed_recent_missing``'s
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
    if not embedding_rows and not topic_rows and not replace_refs:
        return {"ok": True, "embeddings": 0}
    lock = _acquire_refresh_lock()
    if lock is None:
        return {"ok": False, "error": "hub snapshot refresh in progress"}
    try:
        con = sqlite3.connect(str(path))
        try:
            topic_cols = _snapshot_table_columns(con, "topics")
            for row in topic_rows:
                old = con.execute("SELECT deleted_at FROM topics WHERE slug = ?", (row["slug"],)).fetchone()
                if old and old[0] is not None:
                    continue
                cols = [c for c in _TOPIC_COLS if c in topic_cols and c in row]
                vals = [_json_text(row[c]) if c in ("links", "frontmatter") else
                        _ts_text(row[c]) if c.endswith("_at") else row[c] for c in cols]
                con.execute(
                    f"INSERT INTO topics ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
                    "ON CONFLICT(slug) DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in cols if c != "slug"),
                    vals,
                )
            for profile, kind, ref in replace_refs:
                con.execute("DELETE FROM memory_embeddings WHERE profile = ? AND kind = ? AND ref = ?",
                            (profile, kind, ref))
            for erow in embedding_rows:
                if erow.get("kind") == "episode" and str(erow.get("ref")) in {str(i) for i in _pending_erasure_ids(path)}:
                    continue
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
        _write_meta(m)
        return {"ok": True, "embeddings": len(embedding_rows)}
    finally:
        _release_refresh_lock(lock)


def forget_episode_in_snapshot(episode_id: int) -> dict[str, Any]:
    """Mark one episode forgotten in the local sqlite replica too (Phase 2,
    session A — ``forget.forget_everywhere``'s replica half): sets
    ``episodes.deleted_at`` and removes its ``memory_embeddings`` rows, under
    the same refresh lock as ``refresh()``/``upsert_episode()`` so a full
    dump and this incremental tombstone can never interleave.

    Fail-open, never a raise: the hub write (``forget.forget_episode``) is
    already durable by the time this runs, so a missing snapshot or a dump
    in progress just means the replica catches up at the next refresh."""
    path = snapshot_path()
    if not _queue_erasure(episode_id, path):
        return {"ok": False, "episode_id": episode_id, "error": "could not queue pending replica erasure"}
    if not path.is_file():
        return {"ok": False, "episode_id": episode_id, "pending": True, "error": "hub snapshot missing"}
    lock = _acquire_refresh_lock()
    if lock is None:
        return {"ok": False, "episode_id": episode_id, "pending": True, "error": "hub snapshot refresh in progress"}
    try:
        con = sqlite3.connect(str(path))
        try:
            cur = con.execute(
                "UPDATE episodes SET deleted_at = ? WHERE id = ?",
                (_utcnow_iso(), episode_id),
            )
            updated = cur.rowcount
            con.execute(
                "DELETE FROM memory_embeddings WHERE kind = 'episode' AND ref = ?",
                (str(episode_id),),
            )
            if _snapshot_table_columns(con, "deliverables"):
                con.execute("DELETE FROM deliverables WHERE episode_id = ?", (episode_id,))
            con.commit()
        finally:
            con.close()
        return {"ok": True, "episode_id": episode_id, "updated": bool(updated)}
    except Exception as exc:  # noqa: BLE001 — the hub write already succeeded
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
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
    return {
        "ok": age_s is not None,
        "exists": True,
        "path": str(path),
        "refreshed_at": refreshed_at,
        "size_bytes": st.st_size,
        "age_seconds": age_s,
        "counts": m.get("counts"),
        **({"reason": "refresh_evidence_unknown"} if age_s is None else {}),
    }


SNAPSHOT_BEHIND_INGEST_MAX_S = 30 * 60


def _replica_latest_ingested_at() -> datetime | None:
    """Newest ``ingested_at`` among the episodes the replica holds, or None
    when the file is missing, unreadable, empty or holds an unparseable value."""
    try:
        con = sqlite3.connect(f"file:{snapshot_path()}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT MAX(ingested_at) FROM episodes").fetchone()
        finally:
            con.close()
        return datetime.fromisoformat(str(row[0]).replace("Z", "+00:00")) if row and row[0] else None
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None


def snapshot_freshness(
    latest_ingested_at: str | None, health: dict[str, Any] | None = None
) -> dict[str, Any]:
    """``snapshot_health()`` plus ``behind_ingest_seconds`` (W2.4).

    Reads only the local replica and already-known timestamps, never PG, so
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
    # Since nothing dumps the replica every 15 minutes any more, the last full
    # dump is hours old on a healthy machine while incremental writes keep the
    # replica current. "Behind" is the hub's newest ingest against the newest
    # ingest the replica actually holds; refreshed_at (and the 24 h trust in
    # snapshot_is_fresh) is untouched, so an incremental write still cannot
    # pass for a full dump.
    held = _replica_latest_ingested_at()
    if held is not None:
        out["replica_latest_ingested_at"] = held.isoformat()
        try:
            ref = max(ref, held)
        except TypeError:
            pass
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
    """WHERE any-token-hits plus ORDER BY coverage; returns bound params.

    The params are positional and the WHERE text precedes the ORDER BY text
    in every caller's SQL, so every WHERE pattern comes before every score
    pattern. Interleaving them per token bound the first half of the tokens
    to the WHERE and the second half to the score (2026-08-27 to 2026-09-29).

    Plain ``LIKE`` is already ASCII case-insensitive in SQLite, exactly the
    folding ``LOWER()`` does without ICU, and tokens arrive lowercased;
    ``LOWER()`` on the column only copied every row's text per comparison.
    """
    wheres: list[str] = []
    scores: list[str] = []
    where_params: list[Any] = []
    score_params: list[Any] = []
    ncols = len(columns)
    for tok in tokens:
        pat = f"%{_escape_like(tok)}%"
        ors = " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in columns)
        wheres.append(f"({ors})")
        scores.append(f"(CASE WHEN {ors} THEN 1 ELSE 0 END)")
        where_params.extend([pat] * ncols)
        score_params.extend([pat] * ncols)
    return " OR ".join(wheres), " + ".join(scores), where_params + score_params


_FTS_STATE: dict[str, Any] = {"key": None, "ready": False}
_FTS_FALLBACK_LOGGED = False
# A trigram index cannot answer a token shorter than this; such a query goes
# to LIKE whole, so one query never mixes the two matchers.
_FTS_MIN_TOKEN = 3


def _fts_fallback(reason: str) -> None:
    global _FTS_FALLBACK_LOGGED
    if not _FTS_FALLBACK_LOGGED:
        _FTS_FALLBACK_LOGGED = True
        _log(f"replica keyword search on LIKE: {reason}")


def _fts_ready(con: sqlite3.Connection) -> bool:
    """Both trigram FTS tables exist on the replica ``con`` reads. One
    ``sqlite_master`` lookup per replica generation (path, mtime, size) per
    process. A table with another tokenizer would not match like LIKE, so it
    counts as absent."""
    path = snapshot_path()
    try:
        st = path.stat()
        key: tuple[Any, ...] | None = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if key is not None and _FTS_STATE["key"] == key:
        return _FTS_STATE["ready"]
    names = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name IN ('episodes_fts', 'topics_fts') AND sql LIKE '%trigram%'"
    )}
    ready = names == {"episodes_fts", "topics_fts"}
    _FTS_STATE.update(key=key, ready=ready)
    if not ready:
        _fts_fallback("replica has no trigram FTS tables")
    return ready


def _fts_match_terms(tokens: Sequence[str]) -> list[str]:
    """One quoted phrase per token: a trigram phrase is a substring match."""
    return ['"' + tok.replace('"', '""') + '"' for tok in tokens]


def _fts_coverage_sql(fts: str, nterms: int) -> str:
    """``(rid, n)`` per matching row, ``n`` = distinct tokens with a substring
    hit in any indexed column: the LIKE path's coverage score."""
    hits = " UNION ALL ".join(
        f"SELECT rowid FROM {fts} WHERE {fts} MATCH ?" for _ in range(nterms)
    )
    return f"(SELECT rowid AS rid, count(*) AS n FROM ({hits}) GROUP BY rowid)"


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


def _t3_snapshot_predicate(con, t3_thread: str | None, via: str | None) -> tuple[str, list[Any]]:
    if not (t3_thread or via):
        return "TRUE", []
    if "raw" not in _snapshot_table_columns(con, "episodes"):
        return "FALSE", []
    clauses, params = [], []
    for key, value in (("t3_thread_id", t3_thread), ("via", via)):
        if value:
            clauses.append(f"CASE WHEN json_valid(raw) THEN json_extract(raw, '$.{key}') END = ?")
            params.append(value)
    return " AND ".join(clauses), params


def _filter_t3_snapshot_rows(con, rows, t3_thread, via):
    """Recheck graph-expanded rows; graph neighbours need not share a thread."""
    if not (t3_thread or via):
        return rows
    predicate, params = _t3_snapshot_predicate(con, t3_thread, via)
    ids = [str(r["id"]) for r in rows if r.get("kind") == "episode" and not r.get("outbox")]
    allowed = set()
    if ids:
        allowed = {str(r[0]) for r in con.execute(
            f"SELECT id FROM episodes WHERE ({predicate}) "
            f"AND CAST(id AS TEXT) IN ({','.join('?' for _ in ids)})", (*params, *ids),
        )}
    return [r for r in rows if r.get("kind") == "episode" and (
        (r.get("outbox") and (not t3_thread or r.get("t3_thread_id") == t3_thread)
         and (not via or r.get("via") == via))
        or str(r["id"]) in allowed)]


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
    t3_thread: str | None = None,
    via: str | None = None,
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
    want_episode_only = bool(project or session_id or harness or t3_thread or via)
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
    # SQLite's LIKE is wildcard/case behaviour, not the callers' exact
    # Python filter semantics. Register deterministic predicates before the
    # LIMIT so a rejected row cannot consume the result budget.
    con.create_function("khipu_project_match", 2, lambda value, wanted: str(wanted or "").lower() in str(value or "").lower())
    con.create_function("khipu_prefix", 2, lambda value, prefix: str(value or "").startswith(str(prefix or "")))
    def _sql_in_range(value, lo, hi):
        parsed = _parse_snapshot_ts(value)
        if parsed is None:
            return False
        return (not lo or parsed >= datetime.fromisoformat(lo)) and (not hi or parsed <= datetime.fromisoformat(hi))
    con.create_function("khipu_in_range", 3, _sql_in_range)
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

    # FTS replaces only the any-token WHERE and the coverage score; every
    # other predicate, the tie-breaks and the LIMIT are the LIKE path's, so
    # both return the same rows in the same order.
    use_fts = (
        bool(search_tokens(query))
        and all(len(tok) >= _FTS_MIN_TOKEN for tok in tokens)
        and (topic_n > 0 or episode_n > 0)
        and _fts_ready(con)
    )
    match_terms = _fts_match_terms(tokens) if use_fts else []

    def fts_rows(sql: str, params: Sequence[Any], n: int) -> list[Any] | None:
        """Rows from the FTS statement; ``None`` on an FTS error."""
        nonlocal use_fts
        if not use_fts:
            return None
        try:
            return con.execute(sql, (*match_terms, *params, oversample(n))).fetchall()
        except sqlite3.OperationalError as exc:
            use_fts = False
            _fts_fallback(f"FTS query failed: {exc}")
            return None

    if topic_n > 0:
        topic_time_clause = ""
        topic_time_params: list[Any] = []
        if time_filtered:
            topic_time_clause = " AND khipu_in_range(COALESCE(updated_at, created_at), ?, ?)"
            topic_time_params.extend((since_dt.isoformat() if since_dt else None, until_dt.isoformat() if until_dt else None))
        sql = f"""
            SELECT 'topic' AS kind, slug AS id, COALESCE(title, slug) AS label,
                   substr(body, 1, 4000) AS snippet, COALESCE(updated_at, created_at) AS ts
            FROM topics
            WHERE deleted_at IS NULL AND ({topic_where}){topic_time_clause}
            ORDER BY {topic_order}
            LIMIT ?
        """
        rows = fts_rows(
            f"""
            SELECT 'topic' AS kind, slug AS id, COALESCE(title, slug) AS label,
                   substr(body, 1, 4000) AS snippet, COALESCE(updated_at, created_at) AS ts
            FROM {_fts_coverage_sql("topics_fts", len(match_terms))} c
            JOIN topics ON topics.rowid = c.rid
            WHERE deleted_at IS NULL{topic_time_clause}
            ORDER BY c.n DESC, slug ASC
            LIMIT ?
            """,
            topic_time_params, topic_n,
        )
        if rows is None:
            split_at = len(topic_params) // 2 if search_tokens(query) else len(topic_params)
            rows = con.execute(sql, (*topic_params[:split_at], *topic_time_params,
                                     *topic_params[split_at:], oversample(topic_n))).fetchall()
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
        # A forgotten episode is a tombstone on the hub and must stay one on
        # the offline replica (audit 2026-09-04) — topics already did this.
        ep_live = "deleted_at IS NULL AND " if "deleted_at" in ep_cols else ""
        t3_predicate, t3_params = _t3_snapshot_predicate(con, t3_thread, via)
        identity_clause = ""
        identity_params: list[Any] = []
        if project:
            identity_clause += f" AND khipu_project_match({project_expr}, ?)"
            identity_params.append(project.strip())
        if session_id:
            identity_clause += f" AND khipu_prefix({session_expr}, ?)"
            identity_params.append(session_id)
        if harness:
            identity_clause += f" AND COALESCE(NULLIF({harness_expr}, ''), substr({session_expr}, 1, instr({session_expr} || ':', ':') - 1)) = ?"
            identity_params.append(harness)
        if time_filtered:
            identity_clause += " AND khipu_in_range(ts, ?, ?)"
            identity_params.extend((since_dt.isoformat() if since_dt else None, until_dt.isoformat() if until_dt else None))
        sql = f"""
            SELECT 'episode' AS kind, CAST(id AS TEXT) AS id, summary AS label,
                   summary AS snippet, ts, {session_expr} AS sid,
                   {project_expr} AS proj, {harness_expr} AS harn
            FROM episodes
            WHERE {ep_live}({episode_where}) AND ({t3_predicate}){identity_clause}
            ORDER BY {episode_order}
            LIMIT ?
        """
        # Token matching binds WHERE and ORDER BY separately; the thread
        # predicate sits between them in SQL and must sit there in params too.
        # ``episodes`` is open_snapshot's temp view, so join on the INTEGER
        # PRIMARY KEY (the FTS rowid), not the view's rowid.
        # ``id`` in ORDER BY resolves to the CAST(id AS TEXT) alias, as it
        # does in the LIKE statement, so the tie-break is the same text order.
        rows = fts_rows(
            f"""
            SELECT 'episode' AS kind, CAST(id AS TEXT) AS id, summary AS label,
                   summary AS snippet, ts, {session_expr} AS sid,
                   {project_expr} AS proj, {harness_expr} AS harn
            FROM {_fts_coverage_sql("episodes_fts", len(match_terms))} c
            JOIN episodes ON episodes.id = c.rid
            WHERE {ep_live}({t3_predicate}){identity_clause}
            ORDER BY c.n DESC, ts DESC, id DESC
            LIMIT ?
            """,
            (*t3_params, *identity_params), episode_n,
        )
        if rows is None:
            split_at = len(episode_params) // 2 if search_tokens(query) else len(episode_params)
            rows = con.execute(sql, (*episode_params[:split_at], *t3_params,
                                     *identity_params, *episode_params[split_at:], oversample(episode_n))).fetchall()
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
        node_time_clause = ""
        node_time_params: list[Any] = []
        if time_filtered:
            node_time_clause = " AND khipu_in_range(built_at, ?, ?)"
            node_time_params.extend((since_dt.isoformat() if since_dt else None, until_dt.isoformat() if until_dt else None))
        sql = f"""
            SELECT 'node' AS kind, id AS id, COALESCE(name, id) AS label,
                   substr(COALESCE(payload, ''), 1, 4000) AS snippet, built_at
            FROM nodes
            WHERE ({node_where}){node_time_clause}
            ORDER BY {node_order}
            LIMIT ?
        """
        split_at = len(node_params) // 2 if search_tokens(query) else len(node_params)
        rows = con.execute(sql, (*node_params[:split_at], *node_time_params,
                                 *node_params[split_at:], oversample(node_n))).fetchall()
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


def merge_outbox_episodes(
    results: list[dict[str, Any]], *, query: str | None = None, kind: str | None = None,
    since: str | None = None, until: str | None = None, project: str | None = None,
    session_id: str | None = None, harness: str | None = None, t3_thread=None, via=None,
) -> list[dict[str, Any]]:
    from khipu.outbox import jobs
    from khipu.snippets import LABEL_LIMIT, SNIPPET_LIMIT, clip_snippet

    merged = list(results)
    seen: set[str] = {f"{r.get('kind')}:{r.get('id')}" for r in merged}
    from khipu.search_text import parse_time_filter, search_tokens
    if kind not in (None, "episode"):
        return merged
    since_dt = parse_time_filter(since) if since else None
    until_dt = parse_time_filter(until) if until else None
    query_tokens = [token.lower() for token in search_tokens(query or "")]
    for jp in jobs():
        try:
            job = json.loads(jp.read_text(encoding="utf-8"))
            payload = job.get("payload") or {}
            if t3_thread and payload.get("t3_thread_id") != t3_thread:
                continue
            if via and payload.get("via") != via:
                continue
            summary = (payload.get("summary") or "").strip()
            if not summary:
                continue
            if query is not None:
                raw_query = query.strip().lower()
                searchable = " ".join(
                    str(payload.get(field) or "")
                    for field in ("summary", "topics", "decisions", "preferences", "people")
                ).lower()
                raw_fallback = json.dumps(payload.get("raw"), default=str).lower()
                if query_tokens:
                    if not any(token in searchable or token in raw_fallback for token in query_tokens):
                        continue
                elif not raw_query or (raw_query not in raw_fallback and raw_query not in jp.stem.lower()):
                    continue
            row_project = str(payload.get("project") or payload.get("scope") or "")
            row_session = str(payload.get("session_id") or "")
            row_harness = str(payload.get("harness") or row_session.split(":", 1)[0])
            if project and project.strip().lower() not in row_project.lower():
                continue
            if session_id and not row_session.startswith(session_id):
                continue
            if harness and row_harness != harness:
                continue
            if since_dt is not None or until_dt is not None:
                ts = _parse_snapshot_ts(payload.get("ts"))
                if ts is None or (since_dt is not None and ts < since_dt) or (until_dt is not None and ts > until_dt):
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
                    "t3_thread_id": payload.get("t3_thread_id"),
                    "via": payload.get("via"),
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
    """A forgotten (soft-deleted) episode is not found here (B3 in
    docs/research/hindsight-plan-review-2026-09-28.md), same as the hub path
    (``activity.episode_detail``) — the replica's ``episodes`` schema always
    carries ``deleted_at`` (``_create_schema``), so no gating is needed here."""
    con = open_snapshot()
    row = con.execute(
        """
        SELECT id, ts, ingested_at, session_id, scope, summary,
               topics, people, decisions, preferences, edges
        FROM episodes WHERE id = ? AND deleted_at IS NULL
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
    t3_thread: str | None = None,
    via: str | None = None,
) -> list[dict[str, Any]]:
    """fix 7: project/session_id/harness, same episode-only semantics as
    ``search_snapshot`` — a topic/media hit is dropped outright when any of
    these is active (they have no such columns to filter on)."""
    from khipu import profiles
    from khipu.embed import embed_one, prefix_query, uses_task_prefixes
    from khipu.search_text import parse_time_filter, search_tokens, token_hit_count
    from khipu.snippets import LABEL_LIMIT, SNIPPET_LIMIT, clip_snippet

    since_dt = parse_time_filter(since) if since else None
    until_dt = parse_time_filter(until) if until else None
    tokens = search_tokens(query)
    want_episode_only = bool(project or session_id or harness or t3_thread or via)
    spec = active_snapshot_profile_spec()
    if spec is None:
        return []
    from khipu.models import show_models

    cfg = show_models().get("embed") or {}
    if (cfg.get("provider") != "local" or spec.provider != "openai-compatible"
            or not cfg.get("endpoint") or not cfg.get("model_id")
            or spec.model != cfg["model_id"]
            or _embeddings_url(spec.endpoint or "") != _embeddings_url(cfg["endpoint"])):
        raise RuntimeError("offline semantic search unavailable: configured local provider and replica profile are incompatible")
    profiles.register_spec(spec)
    api_query = prefix_query(query) if uses_task_prefixes(spec.id) else query
    vec = embed_one(
        api_query, profile=spec.id, retries=0, timeout=60.0, delay=0,
        input_type="query",
    )
    if len(vec) != spec.dim:
        raise ValueError(
            f"query embedding dimension {len(vec)} does not match snapshot "
            f"profile {spec.id!r} dimension {spec.dim}"
        )
    con = open_snapshot()
    profile = spec.id
    params: list[Any] = [profile]
    kind_clause = ""
    if kind:
        kind_clause = " AND kind = ?"
        params.append(kind)
    if t3_thread or via:
        predicate, t3_params = _t3_snapshot_predicate(con, t3_thread, via)
        kind_clause += (" AND kind = 'episode' AND ref IN "
                        f"(SELECT CAST(id AS TEXT) FROM episodes WHERE {predicate})")
        params.extend(t3_params)
    rows = con.execute(
        f"""
        SELECT kind, ref, chunk_idx, chunk_text, embedding
        FROM memory_embeddings
        WHERE profile = ?{kind_clause} AND embedding IS NOT NULL
        """,
        params,
    ).fetchall()
    time_filtered = since_dt is not None or until_dt is not None
    ep_cols = _snapshot_table_columns(con, "episodes")
    # Tombstoned episodes never come back through the offline replica either
    # (audit 2026-09-04); their vectors can outlive the forget on an old dump.
    deleted_expr = "deleted_at" if "deleted_at" in ep_cols else "NULL"
    if want_episode_only:
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
                "FROM topics WHERE slug = ? AND deleted_at IS NULL",
                (ref,),
            ).fetchone()
            if not trow:
                continue
            label = trow[0] or ref
            ts_val = trow[1]
        elif knd == "episode":
            if want_episode_only:
                erow = con.execute(
                    f"SELECT summary, ts, session_id, {project_expr}, {harness_expr}, "
                    f"{deleted_expr} FROM episodes WHERE CAST(id AS TEXT) = ?",
                    (ref,),
                ).fetchone()
                if not erow:
                    continue
                label, ts_val, row_sid, row_proj, row_harn, row_deleted = erow
                if row_deleted is not None:
                    continue
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
                    f"SELECT summary, ts, {deleted_expr} FROM episodes "
                    "WHERE CAST(id AS TEXT) = ?",
                    (ref,),
                ).fetchone()
                if not erow:
                    continue
                if erow[2] is not None:
                    continue
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
                    "cosine": score,
                    "cosine_sim": score,
                    "profile": profile,
                    "lexical_hits": token_hit_count(
                        f"{label or ''} {chunk_text or ''}", tokens
                    ),
                    "chunk_idx": chunk_idx,
                },
            )
        )
    scored.sort(key=lambda x: x[0], reverse=True)
    # Chunks are evidence, not independent entities: retain the strongest
    # chunk per logical row before applying the caller's entity budget.
    unique: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
    for score, item in scored:
        key = (str(item["kind"]), str(item["id"]))
        retained = unique.get(key)
        if retained is None:
            unique[key] = (score, item)
            continue
        strongest = retained[1]
        strongest["lexical_hits"] = max(
            int(strongest.get("lexical_hits") or 0),
            int(item.get("lexical_hits") or 0),
        )
        strongest["cosine"] = max(float(strongest.get("cosine") or 0), score)
        strongest["cosine_sim"] = max(float(strongest.get("cosine_sim") or 0), score)
    return [item for _, item in list(unique.values())[: max(1, limit)]]


# R1 follow-up (2026-09-14): the per-prompt recall lane (khipu.recall_prompt)
# was timing out against the remote hub on this Mac (measured 1.1-1.6s vs a
# 1.2s budget) even though this local replica answers a keyword search in
# ~226ms. These two helpers give that lane a full offline hybrid leg —
# cosine + lexical, RRF-fused — WITHOUT needing local_embed_configured()
# (semantic_search_snapshot above requires a local embed *server*; this
# instead takes an already-computed query vector, so the caller can get one
# from the same Gemini API the hub profile uses, cached, on its own budget).
SNAPSHOT_MAX_AGE_S = 24 * 60 * 60


def snapshot_is_fresh(*, max_age_s: int = SNAPSHOT_MAX_AGE_S) -> tuple[bool, dict[str, Any]]:
    """(is_fresh, health) — false when the snapshot is missing OR older than
    ``max_age_s``. Callers that only care about "can I use this" get one
    check; ``health`` still carries the age/reason for logging either way.
    """
    health = snapshot_health()
    if not health.get("exists"):
        return False, health
    age = health.get("age_seconds")
    if age is None or age > max_age_s:
        return False, health
    return True, health


def prompt_recall_snapshot_status() -> dict[str, Any]:
    """R1 follow-up (doctor): is the local replica usable for the per-prompt
    recall lane right now? Never red on its own — khipu.recall_prompt
    already degrades safely to the hub when this is false — but that
    degrade is otherwise invisible: every prompt then pays the slower,
    sometimes-timing-out hub round trip with no sign anywhere that it is
    happening. This is the sign.
    """
    fresh, health = snapshot_is_fresh()
    if fresh:
        return {"ok": True, "fresh": True}
    return {
        "ok": True,
        "fresh": False,
        "reason": (
            "prompt-time recall is running against the hub and may time "
            "out; run `khipu snapshot refresh`"
        ),
        "exists": bool(health.get("exists")),
        "age_seconds": health.get("age_seconds"),
    }


def active_snapshot_profile_spec() -> ProfileSpec | None:
    """Validate the active replica profile without consulting the hub."""
    from khipu import profiles

    try:
        con = open_snapshot()
    except FileNotFoundError:
        return None
    try:
        row = con.execute(
            "SELECT id, provider, model, dim, COALESCE(normalize, 'l2') "
            "FROM embedding_profiles WHERE is_active = 1 LIMIT 1"
        ).fetchone()
        if not row:
            return None
        profile_id, provider, model, dim, normalize = row
        endpoint = None
        if _snapshot_table_columns(con, "snapshot_meta"):
            saved = con.execute(
                "SELECT value FROM snapshot_meta WHERE key = ?",
                (f"embedding_profile:{profile_id}",),
            ).fetchone()
            if saved:
                try:
                    record = json.loads(saved[0])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"snapshot profile {profile_id!r} metadata is unreadable") from exc
                expected = {
                    "id": profile_id, "provider": provider, "model": model,
                    "dim": int(dim), "normalize": normalize,
                }
                if not isinstance(record, dict) or any(record.get(k) != v for k, v in expected.items()):
                    raise ValueError(f"snapshot profile {profile_id!r} metadata conflicts with its row")
                endpoint = record.get("endpoint") or None
        spec = profiles.validate_spec(
            profile_id, provider=provider, model=model, dim=int(dim),
            normalize=normalize, endpoint=endpoint,
        )
        seeded = profiles.SEED_PROFILES.get(profile_id)
        if seeded is not None and seeded != spec:
            raise ValueError(f"snapshot profile {profile_id!r} conflicts with the seeded specification")
        width = con.execute(
            "SELECT length(embedding) FROM memory_embeddings "
            "WHERE profile = ? AND embedding IS NOT NULL LIMIT 1",
            (profile_id,),
        ).fetchone()
        if width and int(width[0]) != spec.dim * 4:
            raise ValueError(
                f"snapshot profile {profile_id!r} dimension {spec.dim} conflicts "
                f"with stored vector dimension {int(width[0]) // 4}"
            )
        return spec
    finally:
        con.close()


def active_snapshot_profile() -> str | None:
    from khipu import profiles

    try:
        spec = active_snapshot_profile_spec()
    except (ValueError, TypeError, sqlite3.Error) as exc:
        _log(
            "snapshot semantic leg disabled: active embedding profile is "
            f"unavailable ({type(exc).__name__}: {exc})"
        )
        return None
    if spec is None:
        return None
    profiles.register_spec(spec)
    return spec.id


# The rows cosine_candidates_snapshot scores, kept for the life of the process.
# A one-shot hook never hits this; a long-lived one (`khipu recall serve`)
# reads ~20,000 blobs from disk once instead of on every prompt. One entry,
# keyed by the replica's identity: a refresh replaces the file and an
# incremental write moves its mtime or size, and either one misses and reloads.
_MATRIX_LOCK = threading.Lock()
_MATRIX_CACHE: dict[str, Any] = {}
_MATRIX_RELOADING = False


def _load_matrix(
    path: Path, profile: str, kind: str | None, *, deadline: float | None = None,
) -> tuple[list[tuple[Any, ...]], dict[int, tuple[str, str, int, str]]]:
    con = open_snapshot()
    if deadline is not None:
        con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    try:
        params: list[Any] = [profile]
        kind_clause = ""
        if kind:
            kind_clause = " AND kind = ?"
            params.append(kind)
        topic_live = ""
        topic_cols = _snapshot_table_columns(con, "topics")
        if "slug" in topic_cols and "deleted_at" in topic_cols:
            topic_live = " AND (kind != 'topic' OR EXISTS (SELECT 1 FROM topics t WHERE t.slug = ref AND t.deleted_at IS NULL))"
        rows = con.execute(
            # A commitment has its own dedicated surface (khipu_owed), not
            # generic search — same exclusion as embed._cosine_candidates.
            # Retain the full logical row with the vector generation.
            # A fresh text lookup after scoring can pair a replacement
            # rowid with the identity from the prior snapshot generation.
            f"SELECT rowid, kind, ref, chunk_idx, chunk_text, embedding FROM memory_embeddings "
            f"WHERE profile = ? AND kind != 'commitment'{kind_clause} AND embedding IS NOT NULL{topic_live}",
            params,
        ).fetchall()
    finally:
        con.close()
    # Stored vectors are L2-normalized on the write path (embed_batch,
    # every shipped profile has normalize='l2'), so the rows are scored as
    # read. Re-normalizing 16,000 rows in Python here cost about a second
    # per load, holding the GIL, and the daemon reloads on most prompts
    # because every incremental replica write changes the cache key
    # (2026-10-11). A profile stored without normalization is a backlog
    # item, not something to pay for on every prompt.
    by_rowid = {rowid: (knd, ref, chunk_idx, text or "") for rowid, knd, ref, chunk_idx, text, _blob in rows}
    return rows, by_rowid


def _reload_matrix(path: Path, profile: str, kind: str | None, key: tuple[Any, ...]) -> None:
    global _MATRIX_RELOADING
    try:
        rows, by_rowid = _load_matrix(path, profile, kind)
    except Exception as exc:  # noqa: BLE001 — the stale generation keeps serving
        _log(f"replica matrix background reload failed: {type(exc).__name__}: {exc}")
        rows = None
    with _MATRIX_LOCK:
        old = _MATRIX_CACHE.get("key")
        # A synchronous load for another profile may have replaced the entry
        # meanwhile; only a newer generation of the same one is swapped in.
        if rows is not None and old is not None and old[0] == key[0] and old[3:] == key[3:]:
            _MATRIX_CACHE.update({"key": key, "rows": rows, "by_rowid": by_rowid})
        _MATRIX_RELOADING = False


def _matrix_rows(
    path: Path, profile: str, kind: str | None, *, deadline: float | None = None,
) -> tuple[list[tuple[Any, ...]], dict[int, tuple[str, str, int, str]]]:
    """(rows, by_rowid) for one profile on the replica at ``path``, from the
    process cache while the file is unchanged. With no cached generation the
    load runs under the lock, so concurrent callers wait for one read instead
    of each doing their own.

    Every incremental replica write (each capture, the queue drain every 300 s)
    moves the key, and a synchronous reload costs about a second: the next
    prompt then lost its cosine leg (2026-10-12). So a changed file is served
    from the previous generation, one write behind (a write only adds rows),
    while exactly one daemon thread loads the new one outside the lock."""
    global _MATRIX_RELOADING
    try:
        st = path.stat()
        key: tuple[Any, ...] | None = (str(path), st.st_mtime_ns, st.st_size, profile, kind)
    except OSError:
        key = None
    remaining = -1 if deadline is None else max(0, deadline - time.monotonic())
    if not _MATRIX_LOCK.acquire(timeout=remaining):
        raise TimeoutError("replica matrix deadline expired")
    try:
        cached = _MATRIX_CACHE.get("key")
        if key is not None and cached == key:
            return _MATRIX_CACHE["rows"], _MATRIX_CACHE["by_rowid"]
        if key is not None and cached is not None and cached[0] == key[0] and cached[3:] == key[3:]:
            if not _MATRIX_RELOADING:
                _MATRIX_RELOADING = True
                threading.Thread(
                    target=_reload_matrix, args=(path, profile, kind, key), daemon=True,
                ).start()
            return _MATRIX_CACHE["rows"], _MATRIX_CACHE["by_rowid"]
        rows, by_rowid = _load_matrix(path, profile, kind, deadline=deadline)
        _MATRIX_CACHE.clear()
        if key is not None:
            _MATRIX_CACHE.update({"key": key, "rows": rows, "by_rowid": by_rowid})
        return rows, by_rowid
    finally:
        _MATRIX_LOCK.release()


def cosine_candidates_snapshot(
    vec: Sequence[float], profile: str, *, limit: int, kind: str | None = None,
    deadline: float | None = None,
) -> list[dict[str, Any]]:
    """Raw cosine-ordered candidates over the LOCAL replica's ``memory_embeddings``
    (best first) — the offline analog of ``embed._cosine_candidates``. Each row
    carries ``rank_text`` (the embedded chunk) so a caller can rank token
    overlap over the same candidates without a second pass, same shape as the
    hub leg. Never raises for "no rows" — an empty/missing snapshot or profile
    just yields ``[]``; a genuine sqlite error still propagates (the caller
    decides whether that means "fall back to the hub").

    Scoring loads each vector and its matching chunk text into the generation
    cache together, then ``khipu.vector_scan.top_k_by_dot`` ranks the cached
    vectors (BLAS when a system library loads, exact Python arithmetic for the
    final scores either way). This function's signature, output rows, keys,
    4-decimal scores and order are unchanged from before.
    """
    from khipu.snippets import LABEL_LIMIT, SNIPPET_LIMIT, clip_snippet
    from khipu.vector_scan import top_k_by_dot

    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("replica scan deadline expired")
    # The load runs to completion regardless of the deadline: every incremental
    # write to the replica changes the cache key, and a load cut short at the
    # prompt deadline never warms the cache, so a busy daemon answered every
    # prompt lexical-only (2026-10-11). A cold load may cost one prompt its
    # cosine leg; the deadline still bounds the scoring below.
    rows, by_rowid = _matrix_rows(snapshot_path(), profile, kind)
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("replica scan deadline expired")
    pending = {str(i) for i in _pending_erasure_ids(snapshot_path())}
    rows = [r for r in rows if r[1] != "episode" or str(r[2]) not in pending]
    # Stored vectors are unit length (see _matrix_rows); only the query is
    # normalized here, so the dot product is the cosine.
    qn = math.sqrt(sum(x * x for x in vec))
    if qn == 0:
        return []
    qvec = tuple(x / qn for x in vec)

    wanted = max(1, int(limit))
    winners = top_k_by_dot(
        qvec, ((rowid, blob) for rowid, _knd, _ref, _idx, _text, blob in rows),
        wanted, entity_key=lambda rowid: by_rowid[rowid][:2], deadline=deadline,
        max_abs_component=1.0,
    )
    if not winners:
        return []

    out: list[dict[str, Any]] = []
    for rowid, score in winners:
        knd, ref, chunk_idx, chunk_text = by_rowid[rowid]
        out.append({
            "kind": knd, "id": ref, "chunk_idx": chunk_idx,
            "score": round(float(score), 4),
            "label": clip_snippet(chunk_text, LABEL_LIMIT),
            "snippet": clip_snippet(chunk_text, SNIPPET_LIMIT),
            "rank_text": chunk_text,
        })
    return out


def snapshot_row_metadata(
    con: sqlite3.Connection, rows: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Attach ts/project/status onto already-fused local-replica rows,
    mirroring ``embed._apply_search_filters``'s episode/topic metadata pass
    (project via COALESCE(project, scope) for episodes, status + the
    frontmatter->>'project' equivalent for topics). Drops a tombstoned or
    missing episode/topic source row outright, same as the hub leg. Other
    kinds (e.g. nodes) pass through unchanged.
    """
    out: list[dict[str, Any]] = []
    episode_ids = sorted({str(r["id"]) for r in rows if r.get("kind") == "episode"})
    topic_ids = sorted({str(r["id"]) for r in rows if r.get("kind") == "topic"})
    meta: dict[tuple[str, str], dict[str, Any]] = {}
    if episode_ids:
        episode_cols = _snapshot_table_columns(con, "episodes")
        if "id" in episode_cols:
            placeholders = ",".join("?" for _ in episode_ids)
            ts_expr = "ts" if "ts" in episode_cols else "NULL"
            if {"project", "scope"} <= episode_cols:
                project_expr = "COALESCE(project, scope)"
            elif "project" in episode_cols:
                project_expr = "project"
            elif "scope" in episode_cols:
                project_expr = "scope"
            else:
                project_expr = "NULL"
            deleted_expr = "deleted_at" if "deleted_at" in episode_cols else "NULL"
            for eid, ts, proj, deleted in con.execute(
                f"SELECT CAST(id AS TEXT), {ts_expr}, {project_expr}, {deleted_expr} "
                f"FROM episodes WHERE CAST(id AS TEXT) IN ({placeholders})",
                episode_ids,
            ).fetchall():
                meta[("episode", eid)] = {
                    "ts": ts, "project": proj, "deleted": deleted is not None,
                }
    if topic_ids:
        topic_cols = _snapshot_table_columns(con, "topics")
        if "slug" in topic_cols:
            placeholders = ",".join("?" for _ in topic_ids)
            if {"updated_at", "created_at"} <= topic_cols:
                ts_expr = "COALESCE(updated_at, created_at)"
            elif "updated_at" in topic_cols:
                ts_expr = "updated_at"
            elif "created_at" in topic_cols:
                ts_expr = "created_at"
            else:
                ts_expr = "NULL"
            status_expr = "status" if "status" in topic_cols else "NULL"
            frontmatter_expr = "frontmatter" if "frontmatter" in topic_cols else "NULL"
            deleted_expr = "deleted_at" if "deleted_at" in topic_cols else "NULL"
            superseded_expr = "superseded_by" if "superseded_by" in topic_cols else "NULL"
            for slug, ts, status, frontmatter, deleted, superseded_by in con.execute(
                f"SELECT slug, {ts_expr}, {status_expr}, {frontmatter_expr}, "
                f"{deleted_expr}, {superseded_expr} FROM topics "
                f"WHERE slug IN ({placeholders})",
                topic_ids,
            ).fetchall():
                proj = None
                if frontmatter:
                    try:
                        proj = json.loads(frontmatter).get("project")
                    except (ValueError, AttributeError, TypeError):
                        proj = None
                meta[("topic", slug)] = {
                    "ts": ts, "status": status or "active", "project": proj,
                    "deleted": deleted is not None, "superseded_by": superseded_by,
                }
    for r in rows:
        row_kind = r.get("kind")
        m = meta.get((row_kind, str(r.get("id"))))
        if m is None:
            if row_kind in {"episode", "topic"}:
                continue
            out.append(dict(r))
            continue
        if m.get("deleted"):
            continue
        item = dict(r)
        if m.get("ts"):
            item["ts"] = m["ts"]
        if m.get("project"):
            item["project"] = m["project"]
        if "status" in m:
            item["status"] = m["status"]
        if m.get("superseded_by"):
            item["superseded_by"] = m["superseded_by"]
        out.append(item)
    return out


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


def _graph_candidates_snapshot(con, results: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Switch-gated graph-candidate leg shared by both ``search_stale_
    payload`` branches (Phase 3, session A: "the replica [backend] serves
    the local prompt lane and the stale-replica payload"). ``results`` is
    used as-is for seeding (it is the caller's own already-ranked list, no
    fusion has happened yet on this path the way ``embed.hybrid_search``
    fuses several candidate lists — a single ranked list is still one valid
    input to ``fuse_ranked_lists``). Returns ``(results, degraded_legs)``;
    ``degraded_legs`` is ``[]`` unless the switch is on and the leg missed
    or errored."""
    from khipu import features as _features

    if not _features.enabled("graph_candidates"):
        return results, []
    try:
        import time as _time

        from khipu import graph_candidates as _gc
        from khipu.search_text import fuse_ranked_lists

        deadline = _time.monotonic() + _gc.REPLICA_LEG_DEADLINE_S
        cand_rows, missed = _gc.replica_candidates(con, results, deadline=deadline)
        if missed:
            return results, ["graph_candidates"]
        if cand_rows:
            results = fuse_ranked_lists([results, cand_rows], limit=max(len(results), 1) + len(cand_rows))
        return results, []
    except Exception:  # noqa: BLE001 — a candidate-leg failure must not sink the search
        return results, ["graph_candidates"]


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
    t3_thread: str | None = None,
    via: str | None = None,
    tz: str | None = None,
) -> dict[str, Any]:
    """Hub-unreachable search fallback (sqlite replica). Honours kind/since/
    until/project/session_id/harness on both the semantic and literal paths
    (W2.3 minimum bar, fix 7 for the metadata filters) — any filter this
    snapshot genuinely cannot honour is named in ``filters_dropped``, never
    silently ignored. Phase 2, session B: episode/topic rows carry
    ``validity`` too — this is a recall surface like any other.

    ``tz`` (Phase 3, session A) is accepted for the same signature as
    ``embed.hybrid_search``/``khipu_search``; this fallback path does not
    itself carry a per-row ``ts`` before enrichment, so — unlike the local
    prompt lane and the explicit hub search — it wires only the graph-
    candidates leg here, not time interpretation.
    """
    filters_dropped = _snapshot_filters_dropped(session_id=session_id, harness=harness)
    if semantic:
        results = semantic_search_snapshot(
            query, limit=limit, kind=kind, since=since, until=until,
            project=project, session_id=session_id, harness=harness,
            t3_thread=t3_thread, via=via,
        )
        con = open_snapshot()
        try:
            from khipu import relevance as _relevance
            from khipu.search_text import search_tokens
            if _relevance.enabled():
                results, _floor = _relevance.gate(results, len(search_tokens(query)))
        except Exception:
            results = []
        results, degraded_legs = _graph_candidates_snapshot(con, results)
        results = _filter_t3_snapshot_rows(con, results, t3_thread, via)
        results = enrich_search_results_snapshot(con, results)
        results = _annotate_snapshot_validity(con, results)
        out = {
            "query": query,
            "mode": "semantic",
            "results": results,
            "filters_dropped": filters_dropped,
            **stale_fields(),
        }
        if degraded_legs:
            out["degraded_legs"] = degraded_legs
        return out
    con = open_snapshot()
    results = search_snapshot(
        query, limit, kind=kind, since=since, until=until,
        project=project, session_id=session_id, harness=harness,
        t3_thread=t3_thread, via=via,
    )
    results = merge_outbox_episodes(
        results, query=query, kind=kind, since=since, until=until,
        project=project, session_id=session_id, harness=harness,
        t3_thread=t3_thread, via=via,
    )
    results, degraded_legs = _graph_candidates_snapshot(con, results)
    results = _filter_t3_snapshot_rows(con, results, t3_thread, via)
    results = enrich_search_results_snapshot(con, results)
    results = _annotate_snapshot_validity(con, results)
    out = {
        "query": query,
        "mode": "literal",
        "results": results[:limit],
        "filters_dropped": filters_dropped,
        **stale_fields(),
    }
    if degraded_legs:
        out["degraded_legs"] = degraded_legs
    return out


def _annotate_snapshot_validity(
    con: sqlite3.Connection, results: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """``khipu.validity.annotate`` for a general (not latency-bounded)
    replica search — up to two small, guarded queries, never raising: a
    validity-annotation failure must not cost the caller its search results."""
    try:
        from khipu import validity as _validity

        episode_ids = [r["id"] for r in results if r.get("kind") == "episode"]
        counts = decision_counts_snapshot(con, episode_ids) if episode_ids else {}
        topic_ids = [str(r["id"]) for r in results if r.get("kind") == "topic"]
        topic_meta = topic_validity_meta_snapshot(con, topic_ids) if topic_ids else {}
        return _validity.annotate(results, counts, topic_meta)
    except Exception:  # noqa: BLE001 — validity is additive; a search must still return
        return results
