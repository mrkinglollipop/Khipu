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

import functools
import hashlib
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
import uuid
from contextlib import closing, contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Sequence

from khipu.text_pages import (
    PAGE_BYTES_DEFAULT,
    db_start,
    fetch_bytes,
    page_args,
    page_utf8,
)

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
# Every replica session's statements end on the server by this; a full dump's
# whole-table reads get the dump's own deadline instead.
HUB_STATEMENT_TIMEOUT_S = float(os.environ.get("KHIPU_HUB_STATEMENT_TIMEOUT_S", "120"))
# A full dump's hub reads, whole: past it the session is abandoned (cancel
# sent, socket shut down), the temp removed and the refresh lock released
# (_dump_deadline).
REFRESH_DEADLINE_S = float(os.environ.get("KHIPU_REFRESH_DEADLINE_S", "900"))
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
# Version 6 adds `commitments`, so `khipu_owed` answers while the hub is away
# (offline and self-healing scope, slice 5). Rows of a forgotten episode are
# never in it.
# Version 7 adds the commitment owner's lineage (origin, label, and who
# reassigned it, why, and from whom), the links between similar items of
# different owners and the newest wording, so an offline read shows the same
# owner as the hub. A replica of version 6 gains them on its next pull.
# Version 8 adds `project_aliases`, so an offline read groups a renamed
# project's keys the way the hub does. A replica of version 7 gets them on
# its next full refresh; a reader on a file without the table sees no groups.
# Version 9 adds the close-rule fields used by offline owed filters. Older
# replicas gain these nullable columns on their next commitments pull.
SNAPSHOT_SCHEMA_VERSION = 9

_COMMITMENT_COLS = (
    "id",
    "text",
    "project",
    "owner",
    "kind",
    "opened_episode",
    "opened_at",
    "due_after",
    "status",
    "closed_episode",
    "closed_at",
    "close_reason",
    # 0012 / 0013 columns; a hub that is behind dumps without them.
    "last_seen_at",
    "seen_count",
    "future_trigger",
    # 0033 / 0035 / 0037 columns: what row_owner reads, so the replica's
    # owner is the hub's.
    "origin",
    "owner_label",
    "owner_reassigned_at",
    "owner_reassigned_by",
    "owner_reassign_reason",
    "owner_prior",
    "owner_prior_label",
    "linked_ids",
    "latest_text",
    "prs",
    "settle_suggestion",
    "pr_check",
)
# The version 7 columns, added to an older replica's table before a pull
# writes rows into it.
_COMMITMENT_LINEAGE_COLS = (
    ("origin", "TEXT"),
    ("owner_label", "TEXT"),
    ("owner_reassigned_at", "TEXT"),
    ("owner_reassigned_by", "TEXT"),
    ("owner_reassign_reason", "TEXT"),
    ("owner_prior", "TEXT"),
    ("owner_prior_label", "TEXT"),
    ("linked_ids", "TEXT"),
    ("latest_text", "TEXT"),
)

_COMMITMENT_CLOSE_COLS = (
    ("prs", "TEXT"),
    ("settle_suggestion", "TEXT"),
    ("pr_check", "TEXT"),
)

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
    data.pop("ledger_caught_up_at", None)
    # The image is authoritative: a copied/removed sidecar must not make an
    # incremental write look like a fresh full replica, nor strand the
    # dump's hub time that freshness is measured from.
    try:
        con = sqlite3.connect(f"file:{snapshot_path()}?mode=ro", uri=True)
        try:
            if _snapshot_table_columns(con, "snapshot_meta"):
                for key in ("refreshed_at", "hub_dumped_at", "ledger_caught_up_at"):
                    row = con.execute("SELECT value FROM snapshot_meta WHERE key = ?", (key,)).fetchone()
                    if row and _utc(row[0]) is not None:
                        data[key] = row[0]
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        pass
    data.setdefault("exists", snapshot_path().is_file() or p.is_file())
    data.setdefault("path", str(snapshot_path()))
    return data


# The sidecar key a pull writes when it finished with every part ok.
PULL_CAUGHT_UP_KEY = "pull_caught_up_at"


def caught_up_at(m: Mapping[str, Any] | None = None, *, ledger: bool = False) -> str | None:
    """How current the replica is: the later of its last full dump
    (``refreshed_at``) and the start of its last incremental pull that
    finished with every part ok, made against this same dump. A pull that
    copied nothing counts too: it asked the hub and found nothing new.
    Ledger readers use only their own stamp, stored in the image: the start
    of the last ledger read (a dump's, a refresh's or a pull's).
    The one source for the replica's age; tests/test_replica_caught_up.py
    enumerates its readers."""
    m = meta() if m is None else m
    if ledger:
        # Never the later of it and the dump's end: a dump's snapshot is as
        # old as its start (``ledger_caught_up_at``). A copy an older Khipu
        # dumped has the hub's time at the dump's start, or only its end.
        for key in ("ledger_caught_up_at", "hub_dumped_at", "refreshed_at"):
            if _utc(m.get(key)) is not None:
                return m.get(key)
        return None
    stamps = [(_utc(m.get("refreshed_at")), m.get("refreshed_at"))]
    side = {} if ledger else _read_sidecar()
    # A sidecar left from another dump (copied, or the dump replaced the
    # image but not the sidecar) says nothing about this copy.
    if side.get("pull_dump") is not None and side.get("pull_dump") == m.get("hub_dumped_at"):
        stamps.append((_utc(side.get(PULL_CAUGHT_UP_KEY)), side.get(PULL_CAUGHT_UP_KEY)))
    known = [(at, raw) for at, raw in stamps if at is not None]
    return max(known, key=lambda t: t[0])[1] if known else None


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
    from khipu import hub_errors

    return hub_errors.unreachable_error(exc)


def try_hub_connect(
    *, connect_timeout: int = HUB_CONNECT_TIMEOUT_S,
    statement_timeout_s: float = HUB_STATEMENT_TIMEOUT_S,
) -> psycopg.Connection:
    """A replica session whose every statement ends by ``statement_timeout_s``
    on the server and whose socket is given up when the network goes silent
    (keepalives), so a stalled hub cannot hold a replica writer for good."""
    import psycopg

    from khipu import db
    from khipu.db import conninfo_with_local_root_cert, resolve_dsn

    from khipu import hub_reach

    try:
        conn = psycopg.connect(
            db._with_statement_timeout(conninfo_with_local_root_cert(resolve_dsn()),
                                       statement_timeout_s),
            connect_timeout=connect_timeout,
            **db._keepalive_kwargs(),
        )
    except Exception as exc:
        if hub_connection_failed(exc):
            hub_reach.note_unreachable(f"{type(exc).__name__}: {exc}")
        raise
    hub_reach.note_reachable()
    return conn


def open_snapshot(*, shared: bool = False) -> sqlite3.Connection:
    path = snapshot_path()
    if not path.is_file():
        raise FileNotFoundError(f"hub snapshot missing: {path}")
    # Only the matrix reader opts into sharing, serialized by _MATRIX_LOCK.
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=not shared)
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
            "WHERE (m.kind != 'episode' OR "
            f"(m.ref NOT IN ({blocked_refs}){tombstone_filter})){_fact_vector_epoch_sql(con, 'm')}"
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


def forget_fence_sql(con: sqlite3.Connection, table: str, episode_col: str) -> str:
    """The one forget fence for a replica read of rows linked to an episode
    (commitments, decisions, deliverables): an ``" AND (...)"`` clause that
    leaves out rows of an episode marked forgotten in the replica and rows of
    an episode whose erasure is queued but not yet applied (the same ids
    ``open_snapshot``'s views hide). ``table`` is the name or alias the query
    uses for the linked table. Rows with no episode, or whose episode the
    replica does not hold, stay. It reads ``main.episodes``, never the view,
    so it holds on a connection from ``open_snapshot`` or a plain one.
    It also shows no row at all while the replica's fact rows were fenced
    against an older refusal set (``_fact_epoch_sql``). Every such read uses
    it (tests/test_replica_forget_fence.py)."""
    col = f"{table}.{episode_col}"
    parts: list[str] = []
    pending = _pending_erasure_ids()
    if pending:
        parts.append(f"{col} NOT IN ({', '.join(str(int(i)) for i in sorted(pending))})")
    if "deleted_at" in _snapshot_table_columns(con, "episodes"):
        parts.append(f"NOT EXISTS (SELECT 1 FROM main.episodes fence_e WHERE fence_e.id = {col} "
                     "AND fence_e.deleted_at IS NOT NULL)")
    # No row while the replica's fact rows were fenced against an older
    # refusal set: they are dropped and rebuilt (design section 6).
    epoch = _fact_epoch_sql(con)
    if not parts:
        return epoch
    return f" AND ({col} IS NULL OR ({' AND '.join(parts)})){epoch}"


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


def _missed_patch_dir(path: Path | None = None) -> Path:
    path = path or snapshot_path()
    return path.parent / ".hub_snapshot.missed-patches"


def _missed_patch_ids(path: Path | None = None) -> set[int]:
    try:
        return {int(p.name) for p in _missed_patch_dir(path).iterdir() if p.name.isdigit()}
    except FileNotFoundError:
        return set()


def _queue_missed_patch(episode_id: int, path: Path | None = None) -> bool:
    """Record an episode whose replica write was dropped, so the next pull
    reads it from the hub again. One empty file per id: writing it twice is
    one marker, and it needs no lock a running dump could be holding."""
    directory = _missed_patch_dir(path)
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        marker = directory / str(int(episode_id))
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            # A miss queued again is a newer miss: a pull that read the hub
            # before now must not clear it. Stamped through the open file, so
            # a cleaner renaming the name meanwhile still sees the stamp.
            os.utime(fd if os.utime in os.supports_fd else marker)
        finally:
            os.close(fd)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _clear_missed_patches(ids: Iterable[int] | None = None, *, path: Path | None = None,
                          written_before: float) -> None:
    """Drop markers written at or before ``written_before`` (``ids``: only
    those). A marker written after the caller read the hub is a newer miss
    and stays."""
    wanted = None if ids is None else {int(i) for i in ids}
    directory = _missed_patch_dir(path)
    try:
        markers = list(directory.iterdir())
    except FileNotFoundError:
        return
    for marker in markers:
        if marker.name.startswith(_CLEARING_PREFIX):
            _restore_stale_clearing(marker)
            continue
        if not marker.name.isdigit() or (wanted is not None and int(marker.name) not in wanted):
            continue
        # Take the marker out of the queue by renaming it first, then judge
        # it: a writer that queues the id again after the rename makes a new
        # file under the old name, which this never touches.
        claimed = directory / f"{_CLEARING_PREFIX}{marker.name}-{os.getpid()}-{uuid.uuid4().hex}"
        try:
            os.rename(str(marker), str(claimed))
        except OSError:
            continue
        try:
            if claimed.stat().st_mtime > written_before:
                # Re-queued before the rename: put it back. A newer marker
                # already under the name is the same miss, so that is fine.
                try:
                    os.link(str(claimed), str(marker))
                except FileExistsError:
                    pass
            claimed.unlink()
        except OSError:
            continue


_CLEARING_PREFIX = ".clearing-"


def _restore_stale_clearing(claimed: Path) -> None:
    """A claimed marker whose cleaner died before judging it goes back in the
    queue: an extra pull of one episode is harmless, a lost miss is not."""
    try:
        id_part, pid_part, _rest = claimed.name[len(_CLEARING_PREFIX):].split("-", 2)
        pid = int(pid_part)
        if not id_part.isdigit() or pid == os.getpid():
            return
        os.kill(pid, 0)
        return
    except ProcessLookupError:
        pass
    except (ValueError, OSError):
        return
    try:
        os.link(str(claimed), str(claimed.parent / id_part))
    except FileExistsError:
        pass
    except OSError:
        return
    try:
        claimed.unlink()
    except OSError:
        pass


def _purged_topics_dir(path: Path | None = None) -> Path:
    path = path or snapshot_path()
    return path.parent / ".hub_snapshot.purged-topics"


def _purged_topic_marker(path: Path, slug: str) -> Path:
    # A slug may hold "/" or ":", so the file is named by its hash and holds it.
    return _purged_topics_dir(path) / hashlib.sha256(slug.encode("utf-8")).hexdigest()


@contextmanager
def _purge_marker_lock(path: Path):
    """Serialize marker publication and removal, independently of sqlite's
    refresh lock. A purge must be able to fence readers while that lock is busy.
    The stable lock lives outside the directory whose entries are markers."""
    import fcntl

    fd = os.open(path.parent / ".hub_snapshot.purge-markers.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _mark_purged_topic(slug: str, path: Path | None = None, *, hub_at: datetime | None = None) -> bool:
    """Publish a purge fence before touching sqlite, even when its refresh
    lock is busy. Hub time takes precedence over this Mac's clock. Atomic
    replacement keeps every open descriptor an immutable marker version."""
    path = path or snapshot_path()
    marker = _purged_topic_marker(path, slug)
    pending = None
    try:
        marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".hub_snapshot.purge-marker-", dir=path.parent)
        pending = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            stamp = _utc(hub_at)
            body = slug if stamp is None else json.dumps({"slug": slug, "hub_at": stamp.isoformat()})
            file.write(body)
            file.flush()
            os.fsync(file.fileno())
        with _purge_marker_lock(path):
            os.replace(pending, marker)
            dfd = os.open(marker.parent, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        return True
    except OSError:
        return False
    finally:
        if pending is not None:
            try:
                pending.unlink(missing_ok=True)
            except OSError:
                pass


def _decode_marker(text: str) -> tuple[str, datetime | None]:
    """Older markers hold only the bare slug, without a hub timestamp."""
    if text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict) and isinstance(data.get("slug"), str):
                return data["slug"], _utc(data.get("hub_at"))
        except ValueError:
            pass
    return text, None


def _read_marker(marker: Path) -> tuple[str, datetime | None]:
    return _decode_marker(marker.read_text(encoding="utf-8"))


def _purge_marker_version(path: Path, slug: str) -> tuple[datetime, tuple[Any, ...]] | None:
    """Capture freshness and deletion identity from the SAME open version.
    Writers replace rather than truncate, so content cannot change under us.
    Legacy file times are rounded to database/datetime microsecond precision."""
    try:
        with _purged_topic_marker(path, slug).open(encoding="utf-8") as file:
            stat = os.fstat(file.fileno())
            text = file.read()
        _slug, hub_at = _decode_marker(text)
        version = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, text)
        stamp = hub_at or datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        return stamp, version
    except (OSError, UnicodeError):
        return None


def _clear_purge_marker_version(path: Path, slug: str, version: tuple[Any, ...]) -> None:
    # The lock covers both comparison and unlink: a purge can publish before
    # or after this critical section, never into the check/delete gap.
    with _purge_marker_lock(path):
        current = _purge_marker_version(path, slug)
        if current is not None and current[1] == version:
            _purged_topic_marker(path, slug).unlink(missing_ok=True)


def _purged_topic_slugs(path: Path | None = None) -> set[str]:
    try:
        markers = list(_purged_topics_dir(path).iterdir())
    except FileNotFoundError:
        return set()
    out = set()
    for marker in markers:
        try:
            out.add(_read_marker(marker)[0])
        except (OSError, UnicodeError):
            continue
    return out


def _purge_marker_time(path: Path, slug: str) -> float | None:
    """The marker file's modification time, or None when it is gone (a
    concurrent dump cleared it)."""
    try:
        return _purged_topic_marker(path, slug).stat().st_mtime
    except OSError:
        return None


def _purge_marker_stamp(path: Path, slug: str) -> datetime | None:
    """Hub purge time, or the same marker version's file time for legacy markers."""
    marker = _purge_marker_version(path, slug)
    return None if marker is None else marker[0]


def _clear_purged_topics(path: Path | None = None, *, dumped_after: float) -> None:
    """Drop only markers reflected by the dump, without racing a new purge.
    The hub delete commits before publication, so a dump begun at or after
    the marker cannot hold the topic unless it was legitimately recreated."""
    path = path or snapshot_path()
    try:
        with _purge_marker_lock(path):
            try:
                markers = list(_purged_topics_dir(path).iterdir())
            except FileNotFoundError:
                return
            for marker in markers:
                try:
                    if marker.stat().st_mtime <= dumped_after:
                        marker.unlink()
                except OSError:
                    continue
    except OSError:
        return


def _erase_episode_commitments(con: sqlite3.Connection, episode_id: int) -> None:
    """Delete the commitments an episode opened and their vectors (the
    vector holds the commitment's text). Other episodes' commitments stay."""
    if not {"id", "opened_episode"} <= _snapshot_table_columns(con, "commitments"):
        return
    ids = [str(row[0]) for row in con.execute(
        "DELETE FROM commitments WHERE opened_episode = ? RETURNING id", (episode_id,))]
    if {"kind", "ref"} <= _snapshot_table_columns(con, "memory_embeddings"):
        for batch in _chunks(ids):
            con.execute("DELETE FROM memory_embeddings WHERE kind = 'commitment' "
                        f"AND ref IN ({', '.join('?' for _ in batch)})", batch)


def _apply_pending_erasures(con: sqlite3.Connection, path: Path) -> None:
    ids = _pending_erasure_ids(path)
    for episode_id in ids:
        if con.execute("UPDATE episodes SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
                       (_utcnow_iso(), episode_id)).rowcount:
            # A tombstone written: no fact row shows until the next copy.
            _invalidate_facts(con)
        con.execute("DELETE FROM memory_embeddings WHERE kind = 'episode' AND ref = ?", (str(episode_id),))
        if _snapshot_table_columns(con, "deliverables"):
            con.execute("DELETE FROM deliverables WHERE episode_id = ?", (episode_id,))
        _erase_episode_commitments(con, episode_id)


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
        fence = forget_fence_sql(con, "decisions", "episode_id")
        rows = con.execute(
            f"""
            SELECT CAST(episode_id AS TEXT),
                   SUM(CASE WHEN NOT ({retract_expr}) AND superseded_by IS NULL THEN 1 ELSE 0 END),
                   SUM(CASE WHEN NOT ({retract_expr}) AND superseded_by IS NOT NULL THEN 1 ELSE 0 END),
                   SUM(CASE WHEN ({retract_expr}) THEN 1 ELSE 0 END)
            FROM decisions
            WHERE CAST(episode_id AS TEXT) IN ({placeholders}){fence}
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
    extra = forget_fence_sql(con, "deliverables", "episode_id")
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


# ---- Per-table cursors (replica freshness, phase 1) -------------------------
# Each kind of change the replica can receive has one row in the image's own
# ``snapshot_meta`` table, key ``cursor:<table>``, JSON value: ``synced_at``
# (the last write that reached it, dump or incremental), ``newest`` (the
# newest hub timestamp it holds, UTC ISO), ``newest_id`` where the table has
# one, and for topics ``reconciled_at`` (the last full manifest pass, i.e.
# the last dump). The row is written in the same transaction as the data it
# describes, so it can never claim a write that did not commit. A write that
# could not reach the file (lock held, sqlite error) cannot record anything
# inside it, so its error lives in the sidecar under ``write_errors``.
FRESHNESS_TABLES = ("episodes", "tombstones", "topics", "decisions", "deliverables", "vectors")
_CURSOR_PREFIX = "cursor:"


def _utc(val: Any) -> datetime | None:
    if isinstance(val, datetime):
        dt = val
    else:
        dt = _parse_snapshot_ts(val)
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _newest_ts(values: Any) -> str | None:
    """The latest instant among ``values`` (datetimes or ISO text in any
    offset), as UTC ISO text; None when none parse. Compared as instants,
    never as strings: the hub's text carries its session's offset."""
    best: datetime | None = None
    for v in values:
        dt = _utc(v)
        if dt is not None and (best is None or dt > best):
            best = dt
    return best.isoformat() if best is not None else None


def _read_cursors(con: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    if not _snapshot_table_columns(con, "snapshot_meta"):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in con.execute(
        "SELECT key, value FROM snapshot_meta WHERE key LIKE 'cursor:%'"
    ).fetchall():
        try:
            row = json.loads(value)
        except (TypeError, ValueError):
            continue
        if isinstance(row, dict):
            out[str(key)[len(_CURSOR_PREFIX):]] = row
    return out


def _advance_cursor(
    con: sqlite3.Connection,
    table: str,
    *,
    synced_at: str,
    newest: Any = None,
    newest_id: Any = None,
    reconciled_at: str | None = None,
) -> None:
    """Move one cursor row forward on ``con`` (caller commits). ``newest``
    and ``newest_id`` only ever grow: an incremental write of an old row
    never pulls the cursor back. A file with no ``snapshot_meta`` (version 1)
    is left alone."""
    if not _snapshot_table_columns(con, "snapshot_meta"):
        return
    key = f"{_CURSOR_PREFIX}{table}"
    row: dict[str, Any] = {}
    found = con.execute("SELECT value FROM snapshot_meta WHERE key = ?", (key,)).fetchone()
    if found:
        try:
            loaded = json.loads(found[0])
            row = loaded if isinstance(loaded, dict) else {}
        except (TypeError, ValueError):
            row = {}
    row["synced_at"] = synced_at
    later = _newest_ts([row.get("newest"), newest])
    if later is not None:
        row["newest"] = later
    ids = [i for i in (row.get("newest_id"), newest_id) if isinstance(i, int) and not isinstance(i, bool)]
    if ids:
        row["newest_id"] = max(ids)
    if reconciled_at is not None:
        row["reconciled_at"] = reconciled_at
    con.execute(
        "INSERT OR REPLACE INTO snapshot_meta (key, value) VALUES (?, ?)",
        (key, json.dumps(row, sort_keys=True)),
    )


def _record_write_error(tables: Sequence[str], error: str) -> None:
    """Note in the sidecar that a write meant for ``tables`` never reached the
    replica. Best effort: losing this note loses evidence, never data. A
    full dump that starts after the note clears it (the dump read the hub
    after the lost write was committed there)."""
    try:
        side = _read_sidecar()
        errors = side.get("write_errors") if isinstance(side.get("write_errors"), dict) else {}
        at = _utcnow_iso()
        for t in tables:
            errors[t] = {"at": at, "error": str(error)[:300]}
        side["write_errors"] = errors
        _write_meta(side)
    except Exception:  # noqa: BLE001 — evidence only; the caller's own result stands
        pass


def _dump_cursors(con: sqlite3.Connection, at: str) -> None:
    """Every cursor row for a file that ``refresh()`` just filled, read from
    the rows it holds. Not on any hot path: this runs once per dump."""

    def column(table: str, col: str) -> list[Any]:
        if col not in _snapshot_table_columns(con, table):
            return []
        return [r[0] for r in con.execute(f"SELECT {col} FROM {table} WHERE {col} IS NOT NULL")]

    def max_id(table: str) -> int | None:
        if "id" not in _snapshot_table_columns(con, table):
            return None
        row = con.execute(f"SELECT MAX(id) FROM {table}").fetchone()
        return int(row[0]) if row and row[0] is not None else None

    _advance_cursor(con, "episodes", synced_at=at, newest=_newest_ts(column("episodes", "ingested_at")),
                    newest_id=max_id("episodes"))
    _advance_cursor(con, "tombstones", synced_at=at,
                    newest=_newest_ts([*column("episodes", "deleted_at"), *column("topics", "deleted_at")]))
    _advance_cursor(con, "topics", synced_at=at, newest=_newest_ts(column("topics", "updated_at")),
                    reconciled_at=at)
    _advance_cursor(con, "decisions", synced_at=at,
                    newest=_newest_ts([*column("decisions", "created_at"), *column("decisions", "superseded_at"),
                                       *column("decisions", "retracted_at")]),
                    newest_id=max_id("decisions"))
    if _snapshot_table_columns(con, "deliverables"):
        _advance_cursor(con, "deliverables", synced_at=at,
                        newest=_newest_ts(column("deliverables", "created_at")), newest_id=max_id("deliverables"))
    _advance_cursor(con, "vectors", synced_at=at, newest=_newest_ts(column("memory_embeddings", "built_at")))


def _write_decision_rows(con: sqlite3.Connection, rows: Sequence[Mapping[str, Any]]) -> int | None:
    """Insert-or-replace ``rows`` into ``con``'s ``decisions`` table, for a
    fact copy (``_write_copy``). Caller owns the transaction and the locks.
    Returns ``None`` (not 0) when the replica has no ``decisions`` table at
    all. Rows of an episode this replica holds tombstoned or queued for
    erasure are left out."""
    cols_present = _snapshot_table_columns(con, "decisions")
    if not cols_present:
        return None
    written = 0
    rejected = _rejected_episode_ids(con)
    ts_cols = {"decided_at", "created_at", "superseded_at", "retracted_at"}
    for row in rows:
        rid = row.get("id")
        if rid is None:
            continue
        con.execute("DELETE FROM decisions WHERE id = ?", (rid,))
        if row.get("episode_id") in rejected:
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
        con.execute(
            f"INSERT INTO decisions ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})",
            vals,
        )
        written += 1
    return written


def sync_decision_changes() -> dict[str, Any]:
    """The capture drain's decision sync: one fact copy (``copy_facts``),
    so a correction made on a different machine reaches this machine's
    local recall lane. No hook may call this directly (a per-prompt or
    session-start hook must never open a hub connection on its own).
    Fail-open: never raises."""
    return copy_facts()


# The hub episode columns, beyond raw, that name a capture's source.
_ANCHOR_SOURCE_COLS = ("harness", "session_id", "transcript_range", "window_id", "part",
                       "repo_root", "project", "parent_session_id")


# ---- The replica's episode fence ---------------------------------------------------
#
# One gate for every import of episodes and their vectors into the replica
# (``_replica_write_gate``): the pull's episode patches, vectors, the full
# dump, a backup import and the scrub. It holds the forgotten-source
# registry's lease (the shared memory lock first) from the source check
# through the SQLite commit, so a publication on another process waits for
# the commit. Episodes are checked on the way in by every source identity
# they represent. Fact rows never pass through it: they come only from the
# fact copy below.

_SCRUB_DIGEST_KEY = "episode_scrub_digest"
_FACT_TABLES = ("commitments", "decisions", "deliverables")
_FACT_VECTOR_KINDS = ("commitment", "decision", "deliverable")
_SCRUB_BATCH = 500


class _ReplicaBusy(RuntimeError):
    """A full dump holds the refresh lock; the write is not made."""


def _anchor_identities(row: Mapping[str, Any], receipts: Iterable[Sequence[Any]] = ()) -> frozenset[str]:
    """Every source identity one episode row represents, as registry tags
    (``tombstones.identity_strings``): its raw and column identities (the
    forms forget publishes, ``_tombstone_payloads_for_episode``), each
    merged capture's payload and receipt hash, and its source receipts
    ``(source_key, payload_sha256)``. Stored rows are already redacted.
    ``RegistryError`` when a payload has no identity."""
    from khipu.tombstones import identity_strings

    payloads = _tombstone_payloads_for_episode(row)
    raw = row.get("raw")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    found: set[str] = set()
    merged = raw.get("merged_from") if isinstance(raw, dict) else None
    for entry in merged if isinstance(merged, list) else ():
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("source_id"), str) and entry["source_id"]:
            found.add(f"h:{entry['source_id']}")
        if isinstance(entry.get("payload"), dict):
            payloads.append(entry["payload"])
    for payload in payloads:
        found |= identity_strings(payload, redact=False)
    for key, sha in receipts:
        if key:
            found.add(f"k:{key}")
        if sha:
            found.add(f"h:{sha}")
    return frozenset(found)


def _read_receipts(cur, episode_ids: Iterable[int]) -> dict[int, list[tuple[Any, Any]]]:
    """The hub's retained source receipts (``episode_sources``, migration
    0027) of ``episode_ids``, deleted ones too."""
    want = sorted({int(i) for i in episode_ids})
    if not want or not {"episode_id", "source_key", "payload_sha256"} <= _pg_columns(cur, "episode_sources"):
        return {}
    out: dict[int, list[tuple[Any, Any]]] = {}
    for part in _chunks(want):
        cur.execute("SELECT episode_id, source_key, payload_sha256 FROM episode_sources "
                    "WHERE episode_id = ANY(%s)", (part,))
        for eid, key, sha in cur.fetchall():
            out.setdefault(int(eid), []).append((key, sha))
    return out


def _anchor_cols(cur) -> list[str]:
    hub_cols = _pg_columns(cur, "episodes")
    return [c for c in dict.fromkeys(("id", "ts", "summary", "raw", "deleted_at", *_ANCHOR_SOURCE_COLS))
            if c in hub_cols]


def read_anchor_identities(cur, episode_ids: Iterable[int]) -> dict[int, frozenset[str]]:
    """Every source identity each of ``episode_ids`` represents on the hub,
    read on ``cur`` (so in its snapshot), for the replica's episode fence.
    An id the hub does not hold is left out."""
    want = sorted({int(i) for i in episode_ids})
    if not want:
        return {}
    cols = _anchor_cols(cur)
    receipts = _read_receipts(cur, want)
    out: dict[int, frozenset[str]] = {}
    for part in _chunks(want):
        cur.execute(f"SELECT {', '.join(cols)} FROM episodes WHERE id = ANY(%s)", (part,))
        for r in cur.fetchall():
            row = dict(zip(cols, r))
            out[int(row["id"])] = _anchor_identities(row, receipts.get(int(row["id"]), ()))
    return out


def _registry_rejected_anchors(anchors: Mapping[Any, Any], known: set[int], *, gate: Any = None) -> set[int]:
    """The episodes the forgotten-source registry refuses, beyond the ids
    already in ``known``: forgotten by id, or by any identity they
    represent. ``gate`` is the registry snapshot of the caller's
    ``_replica_write_gate``. ``RegistryError`` propagates: an unreadable
    registry aborts the write, never reads as empty."""
    if not anchors:
        return set()
    if gate is None:
        from khipu.tombstones import read_gate

        gate = read_gate()
    if gate.empty:
        return set()
    return {int(e) for e, ids in anchors.items()
            if int(e) not in known and (int(e) in gate.episode_ids or gate.refuses(ids or ()))}


def _json_list_value(text: Any) -> list[Any] | None:
    if text is None:
        return None
    try:
        value = json.loads(text) if isinstance(text, str) else text
    except ValueError:
        return None
    return value if isinstance(value, list) else None


def _meta_get(con: sqlite3.Connection, key: str) -> str | None:
    if not _snapshot_table_columns(con, "snapshot_meta"):
        return None
    row = con.execute("SELECT value FROM snapshot_meta WHERE key = ?", (key,)).fetchone()
    return None if row is None or row[0] is None else str(row[0])


def _meta_put(con: sqlite3.Connection, key: str, value: str | None) -> None:
    if not _snapshot_table_columns(con, "snapshot_meta"):
        return
    if value is None:
        con.execute("DELETE FROM snapshot_meta WHERE key = ?", (key,))
    else:
        con.execute("INSERT OR REPLACE INTO snapshot_meta (key, value) VALUES (?, ?)", (key, value))


class _SourceFence:
    """One replica write's episode fence: the registry snapshot its gate
    holds, the episodes refused so far, and the connection it guards.
    ``live``: a capture's or an outbox replay's patch, which never runs the
    whole-replica scrub."""

    def __init__(self, con: sqlite3.Connection, gate: Any, path: Path, *, live: bool = False):
        self.con, self.gate, self.path, self.live = con, gate, path, live
        self.rejected = _rejected_episode_ids(con)
        self.scrubbed = 0

    def start(self, *, scrub: bool = False) -> None:
        """A dump, a backup import or an explicit scrub (``scrub``) applies
        the queued erasures and scrubs every episode; any other write that
        is not live scrubs them when the registry changed since the last
        scrub."""
        if scrub:
            _apply_pending_erasures(self.con, self.path)
            self.rejected |= _rejected_episode_ids(self.con)
            self.scrub()
        elif not self.live and _meta_get(self.con, _SCRUB_DIGEST_KEY) != self.gate.digest:
            self.scrub()

    def scrub(self) -> None:
        self.scrubbed += _scrub_forgotten(self.con, self.gate)
        self.rejected |= _rejected_episode_ids(self.con)
        _meta_put(self.con, _SCRUB_DIGEST_KEY, self.gate.digest)

    def check(self, anchors: Mapping[Any, Any]) -> set[int]:
        """Check episodes (``{id: identities}``, read from the hub with the
        rows) by every identity they represent. Refused ids are queued for
        erasure and erased (``refuse``)."""
        if not anchors:
            return set()
        anchors = {int(e): frozenset(v or ()) for e, v in anchors.items()}
        refused = _registry_rejected_anchors(anchors, self.rejected, gate=self.gate)
        self.refuse(refused)
        return refused

    def refuse(self, ids: Iterable[int]) -> None:
        """Refuse episodes newly: queue their erasure and erase what the
        replica holds of them. A queued erasure changes the registry
        fingerprint, so readers show no fact row until the next copy."""
        new = sorted({int(i) for i in ids} - self.rejected)
        if not new:
            return
        for eid in new:
            if not _queue_erasure(eid, self.path):
                raise OSError("could not record rejected replica source")
            self.rejected.add(eid)
        _apply_pending_erasures(self.con, self.path)

    def admits(self, episode_id: Any) -> bool:
        if episode_id is None:
            return True
        eid = int(episode_id)
        return eid not in self.rejected and eid not in self.gate.episode_ids

    def finish(self) -> None:
        """Before the commit: re-read the registry. A publication since the
        write started (from this very process, which alone can publish while
        the lease is held) is scrubbed for, unless live."""
        from khipu.tombstones import read_gate

        current = read_gate()
        if current.digest != self.gate.digest:
            self.gate = current
            self.rejected |= _rejected_episode_ids(self.con)
            if not self.live:
                self.scrub()


@contextmanager
def _replica_write_gate(path: Path | None = None, *, target: Path | None = None,
                        con: sqlite3.Connection | None = None, refresh_lock_held: bool = False,
                        scrub: bool = False, on_commit: Any = None, live: bool = False):
    """The one way into the replica for imported episodes: the registry's
    ``write_gate`` (memory lock first), then the refresh lock without
    waiting (``_ReplicaBusy``), one SQLite transaction on ``target`` (the
    replica, or a staged copy; or the caller's open ``con``) guarded by a
    ``_SourceFence``, the pre-commit registry re-check, the commit, then
    ``on_commit`` (a dump's replace) before the lease is released.
    ``RegistryError`` propagates."""
    from khipu.tombstones import write_gate

    path = path or snapshot_path()
    with write_gate() as gate:
        lock = None
        if not refresh_lock_held:
            lock = _acquire_refresh_lock()
            if lock is None:
                raise _ReplicaBusy("hub snapshot refresh in progress")
        try:
            own = con is None
            db = sqlite3.connect(str(target or path)) if own else con
            try:
                fence = _SourceFence(db, gate, path, live=live)
                fence.start(scrub=scrub)
                yield fence
                fence.finish()
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                if own:
                    db.close()
            if on_commit is not None:
                on_commit()
        finally:
            if lock is not None:
                _release_refresh_lock(lock)


# ---- The fact copy (design section 6.2) --------------------------------------------
#
# Every match of a refusal to an episode happens on the hub (section 6.1);
# the replica keeps a plain copy of the hub's commitments, decisions,
# deliverables and commitment vectors. ``copy_facts`` is the only writer of
# those rows: the pull, the dump, the capture drain's decision sync, the
# post-forget refresh and a backup import all call it. It works under one
# local lock, ``fact-copy.lock``, from opening its hub snapshot through its
# SQLite commit, so copies on one Mac are serial and a published copy is
# never older than the one it replaces. That lock is outermost: no caller
# holds the memory lock or the registry's ``write_gate`` when taking it.
#
# The stamp is the only visibility switch: readers show no fact row unless
# its registry fingerprint is the current one when SQLite runs the statement
# (``_fact_epoch_sql``). Learning a remote forget (a tombstone a pull, a live
# patch, a dump or an applied erasure writes), a changed endpoint or a new
# hub incarnation clears it and bumps the invalidation revision in one
# SQLite transaction; a copy whose revision moved before its commit
# publishes nothing, so a copy read before the invalidation cannot re-stamp.

# The stamp: the registry fingerprint (the readers' switch) and, beside it,
# the incarnation, destination and snapshot of the copy that set it.
_FACT_DIGEST_KEY = "fact_refusal_digest"
_FACT_STAMP_KEY = "fact_stamp"
_FACT_REVISION_KEY = "fact_revision"
# The forgets the hub lacks, as the last copy found them (6.2).
_FACT_UNSENT_KEY = "fact_unsent"
# The registry strings the last published copy found on the hub. Any other
# registry string is pending (published here, not yet seen on the hub), and
# listed even when no copy can run (6.2).
_FACT_CERTIFIED_KEY = "fact_certified"
FACT_COPY_LOCK_NAME = "fact-copy.lock"
# Tables and keys of the retired replica designs, dropped at the first copy.
_RETIRED_FACT_TABLES = ("replica_fact_sources", "replica_source_index", "replica_fence_applied",
                        "replica_anchor_sources")
_RETIRED_FACT_KEYS = ("fact_stream_xid", "fact_state", "fact_generation")


def _refusal_digest(gate: Any, path: Path | None = None) -> str:
    """The registry fingerprint: the registry's digest and the queued
    erasures."""
    pending = ",".join(str(i) for i in sorted(_pending_erasure_ids(path)))
    return hashlib.sha256(f"{gate.digest}|{pending}".encode("utf-8")).hexdigest()


def _current_refusal_digest(path: Path | None = None) -> str | None:
    """The current registry fingerprint, ``None`` when the registry cannot
    be read (a reader then shows no fact row)."""
    from khipu.tombstones import read_gate

    try:
        return _refusal_digest(read_gate(), path)
    except Exception:  # noqa: BLE001 - unknowable reads as "not current"
        return None


_EPOCH_UNREADABLE = "unreadable"


def _con_path(con: sqlite3.Connection) -> Path | None:
    try:
        main = next((r[2] for r in con.execute("PRAGMA database_list") if r[1] == "main"), "")
    except sqlite3.Error:
        main = ""
    return Path(main) if main else None


# One empty file per stamp clearing that is not confirmed (an endpoint save,
# a pull that met a new incarnation: ``_hide_until_copy``); while any exists
# readers show no fact row, and only a copy that began after it removes it.
_ENDPOINT_CHANGES_DIR = "fact-endpoint-changes"
# The saved endpoint's fingerprint (``_destination_of``, never the
# password), its source and a revision (``_record_destination``), recorded
# by every endpoint save and by each copy that resolved the endpoint without
# an environment override, so readers never ask the Keychain
# (``_reader_destination``).
_DESTINATION_FILE = "fact-endpoint"
# The endpoint generation, kept apart from the record and only ever raised
# (``_endpoint_generation``): an endpoint save raises it before writing or
# removing the record, so a copy that read it earlier records nothing.
_GENERATION_FILE = "fact-endpoint-generation"
# What a reader compares when no endpoint fingerprint is recorded; no stamp
# names it, so no fact row shows.
_UNRESOLVED = "unresolved"
_DEFAULT_DSN_FILE = Path.home() / ".config" / "khipu" / "dsn"  # khipu.db.DEFAULT_DSN_FILE
_LEGACY_DSN_FILE = Path.home() / ".config" / "alzy" / "dsn"  # khipu.db.LEGACY_DSN_FILE


def _env_dsn() -> str | None:
    for name in ("KHIPU_DATABASE_URL", "ALZY_DATABASE_URL"):
        if value := (os.environ.get(name) or "").strip():
            return value
    return None


def _reader_destination(replica: Path | None = None) -> str:
    """The configured endpoint's fingerprint for a reader, never through
    the Keychain (a subprocess the prompt hook must not start): the
    environment's endpoint; with the Keychain off, the DSN files, as
    ``db.resolve_dsn`` reads them (through it when ``khipu.db`` is loaded;
    the prompt hook never imports the driver); otherwise the recorded
    endpoint (``_DESTINATION_FILE``): for one the DSN files gave, those
    files as they read now, so an edit needs no save to count; for one the
    Keychain gave, its recorded fingerprint (only an endpoint save changes
    it, and records it). ``_UNRESOLVED`` without a record."""
    if dsn := _env_dsn():
        return _destination_of(dsn)
    from khipu.keychain import keychain_available

    if not keychain_available():
        if "khipu.db" in sys.modules:
            return _hub_destination()
        dsn = _saved_dsn(ask_keychain=False)
        return _destination_of(dsn) if dsn else "unconfigured"
    record = _endpoint_record(replica or snapshot_path())
    if record is None:
        return _UNRESOLVED
    if record["source"] == "file":
        dsn = _file_dsn()
        return _destination_of(dsn) if dsn else "unconfigured"
    return record["destination"]


def _endpoint_record(replica: Path) -> dict[str, Any] | None:
    """The recorded endpoint (``_record_destination``); None when missing
    or unreadable, or written before it carried a source and revision."""
    try:
        value = json.loads((replica.parent / _DESTINATION_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("destination"), str) \
            or value.get("source") not in ("file", "keychain") or type(value.get("revision")) is not int:
        return None
    return value


def _endpoint_source(destination: str) -> str:
    """Where the resolved endpoint ``destination`` came from, as a reader
    can check it without the Keychain: ``file`` when the DSN files name it
    (or the Keychain is off), else ``keychain``."""
    from khipu.keychain import keychain_available

    if not keychain_available():
        return "file"
    dsn = _file_dsn()
    return "file" if dsn and _destination_of(dsn) == destination else "keychain"


def _endpoint_generation(replica: Path) -> int:
    """The endpoint generation: the larger of the record's revision and
    ``_GENERATION_FILE`` (0 when neither exists). Removing the record never
    lowers it. ``OSError`` when the counter cannot be read."""
    held = _endpoint_record(replica)
    try:
        text = (replica.parent / _GENERATION_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        counter = 0
    else:
        try:
            counter = int(text.strip())
        except ValueError as exc:
            raise OSError(f"the endpoint generation is unreadable ({exc})") from exc
    return max(counter, held["revision"] if held else 0)


@contextmanager
def _destination_lock(replica: Path):
    """The exclusive lock on the replica's folder every endpoint record and
    generation change is made under."""
    import fcntl

    lock = os.open(str(replica.parent), os.O_RDONLY)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield
    finally:
        os.close(lock)


def _write_endpoint_file(replica: Path, name: str, text: str) -> None:
    """Replace ``name`` beside the replica atomically (fsynced)."""
    fd, temp = tempfile.mkstemp(prefix="." + name + ".", dir=replica.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, replica.parent / name)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _record_destination(replica: Path, destination: str, source: str, *, expect: int | None = None) -> bool:
    """Record the endpoint's fingerprint and source beside the replica (an
    atomic file, written under ``_destination_lock``). An endpoint save
    (``expect`` None) raises the generation first and records it as the
    revision. A copy passes the generation it read before resolving the
    endpoint, and writes nothing when a save or an invalidation landed since
    (False), so a delayed copy never restores an older endpoint, even after
    the record was removed."""
    if not replica.parent.is_dir():
        return False
    with _destination_lock(replica):
        generation = _endpoint_generation(replica)
        if expect is not None and generation != expect:
            return False
        if expect is None:
            generation += 1
            _write_endpoint_file(replica, _GENERATION_FILE, f"{generation}\n")
        record = {"destination": destination, "source": source, "revision": generation}
        if record != _endpoint_record(replica):
            _write_endpoint_file(replica, _DESTINATION_FILE, json.dumps(record, sort_keys=True) + "\n")
        return True


def _invalidate_destination(replica: Path) -> bool:
    """An endpoint save that could not record its endpoint: under
    ``_destination_lock``, raise the generation (so no copy that read the
    old one records) and remove the record (readers show no fact row).
    True only when both landed; ``OSError`` when the record stays."""
    with _destination_lock(replica):
        try:
            _write_endpoint_file(replica, _GENERATION_FILE, f"{_endpoint_generation(replica) + 1}\n")
            raised = True
        except OSError as exc:
            _record_write_error(_FACT_TABLES, f"endpoint generation: {type(exc).__name__}: {exc}")
            raised = False
        (replica.parent / _DESTINATION_FILE).unlink(missing_ok=True)
    return raised


def _saved_dsn(*, ask_keychain: bool) -> str | None:
    """``db.resolve_dsn``'s answer without the environment, the driver or
    its legacy-secret migration: the Keychain (legacy service too) when
    ``ask_keychain``, then the DSN files (``_file_dsn``); ``None`` when none
    is set."""
    if ask_keychain:
        try:
            from khipu import keychain

            for service in (keychain.SERVICE, keychain.LEGACY_SERVICE):
                if val := keychain.get_password(keychain.DSN_ACCOUNT, service=service):
                    return val
        except Exception:  # noqa: BLE001 - db.resolve_dsn falls through to the file too
            pass
    return _file_dsn()


def _file_dsn() -> str | None:
    """The DSN files' answer as ``db.resolve_dsn`` reads them: the DSN file
    (``KHIPU_DSN_FILE`` or the configured one), then the config and legacy
    files unless the environment names where the DSN lives."""
    from khipu.paths import explicit_dsn_location as _explicit_dsn_location

    env = (os.environ.get("KHIPU_DSN_FILE") or "").strip() or (os.environ.get("ALZY_DSN_FILE") or "").strip()
    candidates = [Path(env) if env else _configured_dsn_file()]
    if not _explicit_dsn_location():
        candidates += [_DEFAULT_DSN_FILE, _LEGACY_DSN_FILE]
    for path in candidates:
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return None


def _endpoint_changes(replica: Path) -> list[str]:
    try:
        return sorted(os.listdir(replica.parent / _ENDPOINT_CHANGES_DIR))
    except OSError:
        return []


def _configured_dsn_file() -> Path:
    from khipu.paths import dsn_file

    return dsn_file()


def _certified(digest: Any, text: Any, path: Path | None) -> bool:
    """Whether a replica's stamp certifies its fact rows for a reader now
    (6.2): a complete stamp (incarnation, destination, snapshot), its
    destination the configured endpoint, its fingerprint the registry's."""
    if digest is None or text is None:
        return False
    try:
        stamp = json.loads(text)
    except (TypeError, ValueError):
        return False
    if not isinstance(stamp, dict) or not all(
            isinstance(stamp.get(k), str) and stamp[k] for k in ("incarnation", "destination", "snapshot")):
        return False
    return stamp["destination"] == _reader_destination(path) and not _endpoint_changes(path or snapshot_path()) \
        and digest == _current_refusal_digest(path)


def _install_fact_epoch(con: sqlite3.Connection) -> None:
    """Register ``khipu_fact_certified(digest, stamp)`` on ``con``
    (``_certified``), computed when SQLite evaluates it (each statement, a
    TEMP view's every use included), never frozen into the SQL text. The
    queued erasures are those beside this connection's own file."""
    path = _con_path(con)
    con.create_function("khipu_fact_certified", 2, lambda d, t: int(_certified(d, t, path)))


def _fact_epoch_sql(con: sqlite3.Connection) -> str:
    """An ``" AND (...)"`` clause, true only while the replica's stamp
    certifies its facts when the statement runs (``_certified``), the stamp
    read in the reader's own SQLite snapshot of the rows. A replica with no
    complete stamp (cleared, never copied, restored from a backup, written
    before 7B, or with no ``snapshot_meta``) shows no fact row."""
    if not _snapshot_table_columns(con, "snapshot_meta"):
        return " AND 0"
    _install_fact_epoch(con)
    return (" AND (SELECT khipu_fact_certified("
            f"(SELECT value FROM main.snapshot_meta WHERE key = '{_FACT_DIGEST_KEY}'), "
            f"(SELECT value FROM main.snapshot_meta WHERE key = '{_FACT_STAMP_KEY}')))")


def _fact_vector_epoch_sql(con: sqlite3.Connection, alias: str = "") -> str:
    """The same clause for ``memory_embeddings`` rows: a fact vector shows
    only while the fact rows do; other kinds are untouched."""
    epoch = _fact_epoch_sql(con)
    kind = f"{alias}.kind" if alias else "kind"
    kinds = ", ".join(f"'{k}'" for k in _FACT_VECTOR_KINDS)
    return f" AND ({kind} NOT IN ({kinds}) OR (1 = 1{epoch}))"


def _revision(con: sqlite3.Connection) -> int:
    try:
        return int(_meta_get(con, _FACT_REVISION_KEY) or 0)
    except ValueError:
        return 0


def _invalidate_facts(con: sqlite3.Connection, *, revision: int | None = None) -> None:
    """Clear the stamp and bump the invalidation revision, in the caller's
    SQLite transaction: readers show no fact row, and no copy that read the
    hub before now can publish (6.2, N1). ``revision``: the one to bump
    from (a dump's or an import's new file takes the old file's)."""
    if not _snapshot_table_columns(con, "snapshot_meta"):
        return
    _meta_put(con, _FACT_DIGEST_KEY, None)
    _meta_put(con, _FACT_STAMP_KEY, None)
    _meta_put(con, _FACT_REVISION_KEY, str((_revision(con) if revision is None else revision) + 1))


def _file_revision(path: Path) -> int:
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as con:
            return _revision(con)
    except sqlite3.Error:
        return 0


def _fact_rows() -> dict[str, tuple[str, tuple, set[str]]]:
    """The rows no backup carries (6.2), per table: a predicate true for
    them, its parameters, and the columns it needs (a table without them
    has no such rows): what ``_drop_facts`` deletes, which
    ``export_replica_copy`` never copies."""
    meta_keys = (_FACT_DIGEST_KEY, _FACT_STAMP_KEY, _FACT_UNSENT_KEY, _FACT_CERTIFIED_KEY, *_RETIRED_FACT_KEYS)
    return {
        **{table: ("1", (), set()) for table in _FACT_TABLES},
        "memory_embeddings": (f"kind IN ({', '.join('?' * len(_FACT_VECTOR_KINDS))})", _FACT_VECTOR_KINDS,
                              {"kind"}),
        "snapshot_meta": (f"key IN ({', '.join('?' * len(meta_keys))})", meta_keys, {"key"}),
        _ROW_VERSIONS_TABLE: ("tbl IN ('commitments', 'decisions', 'deliverables') "
                              "OR (tbl = 'vectors' AND instr(key, char(31) || 'commitment' || char(31)) > 0)", (),
                              {"tbl", "key"}),
    }


def _drop_facts(con: sqlite3.Connection) -> None:
    """Remove every fact row, fact vector, the stamp and the listing, in the
    caller's transaction (a restored replica). Episodes, topics and the
    rest stay."""
    for table in _FACT_TABLES:
        if _snapshot_table_columns(con, table):
            con.execute(f"DELETE FROM {table}")
    if {"kind"} <= _snapshot_table_columns(con, "memory_embeddings"):
        con.execute("DELETE FROM memory_embeddings WHERE kind IN "
                    f"({', '.join('?' * len(_FACT_VECTOR_KINDS))})", _FACT_VECTOR_KINDS)
    _drop_retired(con)
    for key in (_FACT_DIGEST_KEY, _FACT_STAMP_KEY, _FACT_UNSENT_KEY, _FACT_CERTIFIED_KEY):
        _meta_put(con, key, None)


def _drop_retired(con: sqlite3.Connection) -> None:
    for table in _RETIRED_FACT_TABLES:
        con.execute(f"DROP TABLE IF EXISTS {table}")
    for key in _RETIRED_FACT_KEYS:
        _meta_put(con, key, None)
    if _snapshot_table_columns(con, _ROW_VERSIONS_TABLE):
        con.execute(f"DELETE FROM {_ROW_VERSIONS_TABLE} WHERE tbl IN ('commitments', 'decisions', 'deliverables') "
                    "OR (tbl = 'vectors' AND instr(key, char(31) || 'commitment' || char(31)) > 0)")


def facts_hidden(path: Path | None = None) -> bool:
    """Whether this Mac's replica shows no commitments, decisions or
    deliverables: its stamp is missing or names another registry
    fingerprint (a forget the next copy has not certified yet)."""
    path = path or snapshot_path()
    if not path.is_file():
        return False
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as con:
            digest, text = _meta_get(con, _FACT_DIGEST_KEY), _meta_get(con, _FACT_STAMP_KEY)
    except sqlite3.Error:
        return True
    return not _certified(digest, text, path)


def note_sent(tags: Sequence[str], deadline: float) -> None:
    """The owner send committed ``tags`` on the hub: they leave the list
    and count as seen there, in one admitted publication (6.4, D4). The
    stamp is untouched; facts show again only after a copy. Fail-open: a
    later copy lists what is left."""
    path = snapshot_path()
    if not tags or not path.is_file():
        return
    try:
        with _admitted(path, deadline) as (con, _gate):
            listing = json.loads(_meta_get(con, _FACT_UNSENT_KEY) or "{}")
            if listing.get("strings"):
                listing["strings"] = [t for t in listing["strings"] if t not in set(tags)]
                _meta_put(con, _FACT_UNSENT_KEY, json.dumps(listing, sort_keys=True))
            seen = set(json.loads(_meta_get(con, _FACT_CERTIFIED_KEY) or "[]")) | set(tags)
            _meta_put(con, _FACT_CERTIFIED_KEY, json.dumps(sorted(seen)))
    except Exception as exc:  # noqa: BLE001 - reconciled by the next copy
        _record_write_error(_FACT_TABLES, f"owner send listing: {type(exc).__name__}: {exc}")


class EndpointChangeUnrecorded(RuntimeError):
    """An endpoint save could hide the old endpoint's facts in no way."""


def endpoint_changed() -> None:
    """A configuration write changed the hub endpoint (N3): its fingerprint
    is recorded for readers (``_reader_destination``) with a new revision,
    then this Mac's stamp is cleared and the revision bumped at once, before
    any copy. A record that cannot be written is removed with the
    generation raised (``_invalidate_destination``: readers then show no
    fact row, and no delayed copy records the old endpoint); when neither
    that nor the clearing lands, the save fails closed:
    ``EndpointChangeUnrecorded``."""
    path = snapshot_path()
    dsn = _saved_dsn(ask_keychain=True)
    destination = _destination_of(dsn) if dsn else "unconfigured"
    try:
        recorded = _record_destination(path, destination, _endpoint_source(destination))
    except OSError as exc:
        _record_write_error(_FACT_TABLES, f"endpoint fingerprint: {type(exc).__name__}: {exc}")
        try:
            recorded = _invalidate_destination(path)
        except OSError as exc:
            _record_write_error(_FACT_TABLES, f"endpoint fingerprint removal: {type(exc).__name__}: {exc}")
            recorded = False
    if path.is_file() and not _hide_until_copy(path, "endpoint change") and not recorded:
        raise EndpointChangeUnrecorded("the endpoint change could not be recorded beside the replica; "
                                       "its facts may still show the old hub's (see the write errors)")


def _hide_until_copy(path: Path, what: str) -> bool:
    """Clear the stamp and bump the invalidation revision now. A durable
    marker is written first and removed only once the clearing commits, so
    a replica too busy to clear leaves facts hidden until a copy that began
    after it (``_copy_once``); a marker that cannot be written does not stop
    the clearing. False when neither marker nor clearing landed."""
    marker = path.parent / _ENDPOINT_CHANGES_DIR / uuid.uuid4().hex
    marked = False
    try:
        marker.parent.mkdir(mode=0o700, exist_ok=True)
        marker.touch(mode=0o600)
        marked = True
    except OSError as exc:
        _record_write_error(_FACT_TABLES, f"{what} marker: {type(exc).__name__}: {exc}")
    try:
        with _admitted(path, time.monotonic() + 5) as (con, _gate):
            _invalidate_facts(con)
    except Exception as exc:  # noqa: BLE001 - the marker (or the endpoint check) still hides the old copy
        _record_write_error(_FACT_TABLES, f"{what}: {type(exc).__name__}: {exc}")
        return marked
    if marked:
        marker.unlink(missing_ok=True)
    return True


def _learn_incarnation(incarnation: str | None) -> None:
    """A pull read the hub's incarnation (D2): another than the stamp's
    hides the held copy at once, whatever the pull does next."""
    path = snapshot_path()
    if incarnation is None or not path.is_file():
        return
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as con:
            held = _read_stamp(con)
    except sqlite3.Error:
        held = None
    if held is not None and held.get("incarnation") != str(incarnation):
        _hide_until_copy(path, "incarnation change")


def unsent_forgets(path: Path | None = None) -> dict[str, Any]:
    """The forgets the hub lacks (6.2): ``strings``, the registry tags the
    last copy found no hub refusal for and every registry tag no published
    copy has seen on the hub yet (a forget whose hub step failed lists its
    strings at once, with no copy), and ``ids`` (bare ids whose hub episode
    is live, each with that episode's summary). With no replica, or one that
    cannot be read, nothing counts as seen on the hub: every registry tag is
    pending."""
    from khipu.tombstones import read_gate

    path = path or snapshot_path()
    empty: dict[str, Any] = {"strings": [], "ids": []}
    text = certified = None
    if path.is_file():
        try:
            with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as con:
                text, certified = _meta_get(con, _FACT_UNSENT_KEY), _meta_get(con, _FACT_CERTIFIED_KEY)
        except sqlite3.Error:
            pass
    try:
        value = json.loads(text) if text else empty
        seen = set(json.loads(certified)) if certified else set()
    except ValueError:
        value, seen = empty, set()
    try:
        pending = set(read_gate().tags) - seen
    except Exception:  # noqa: BLE001 - an unreadable registry lists what the copy found
        pending = set()
    return {"strings": sorted(set(value.get("strings") or []) | pending), "ids": list(value.get("ids") or []),
            **({"at": value["at"]} if value.get("at") else {})}


def export_replica_copy(src: Path, dest: Path) -> dict[str, Any]:
    """Write a consistent copy of the replica at ``src`` to ``dest`` for a
    backup archive. A backup never carries fact rows or fact vectors (6.2),
    nor the stamp, so a restore shows none until a copy from the hub. The
    copy is a new, empty database beside ``dest`` (named as a refresh's
    temporary replica, which no backup or restore takes) into which only the
    rows a backup may carry are inserted, in one read of ``src``; it is
    synced, then renamed to ``dest``. No file this export writes ever holds
    a fact row, so a crash leaves none behind."""
    fd, name = tempfile.mkstemp(prefix=".hub_snapshot.", suffix=".sqlite", dir=dest.parent)
    os.close(fd)
    part = Path(name)
    try:
        with closing(sqlite3.connect(str(part), uri=True, isolation_level=None)) as copy:
            copy.execute("ATTACH DATABASE ? AS src", (f"file:{src}?mode=ro",))
            copy.execute("BEGIN")
            _copy_kept_rows(copy)
            copy.execute("COMMIT")
            copy.execute("DETACH DATABASE src")
        _fsync_path(part)
        os.replace(part, dest)
        _fsync_path(dest.parent)
    finally:
        remove_replica_temp(part)
    return {"facts_dropped": True}


def _fsync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_kept_rows(copy: sqlite3.Connection) -> None:
    """Rebuild the attached ``src``'s schema in ``copy`` and insert every
    row but those ``_fact_rows`` names, in the caller's transaction: tables
    (a virtual table's own tables with it, their rows copied as stored),
    then the rows, then indexes, triggers and views, so no trigger fires on
    the copy. Each row keeps its rowid, implicit ones too, so an FTS index
    (keyed by rowid) still names its rows; an FTS index found out of step
    with its content after the copy is rebuilt. Retired fact tables are
    left out."""
    schema = copy.execute("SELECT type, name, tbl_name, sql FROM src.sqlite_master "
                          "WHERE sql IS NOT NULL ORDER BY rowid").fetchall()
    retired = set(_RETIRED_FACT_TABLES)
    tables: list[str] = []
    for kind, name, table, sql in schema:
        if kind != "table" or table in retired or name.startswith("sqlite_"):
            continue
        if sql.lstrip().upper().startswith("CREATE VIRTUAL TABLE"):
            copy.execute(sql)
            continue
        if not copy.execute("SELECT 1 FROM main.sqlite_master WHERE type = 'table' AND name = ?",
                            (name,)).fetchone():
            copy.execute(sql)
        tables.append(name)
    facts = _fact_rows()
    for name in tables:
        quoted = '"' + name.replace('"', '""') + '"'
        cols = [r[1] for r in copy.execute(f"PRAGMA src.table_info({quoted})")]
        where, params, needs = facts.get(name, ("0", (), set()))
        if not needs <= set(cols):
            where, params = "0", ()
        try:
            copy.execute(f"SELECT rowid FROM src.{quoted} LIMIT 0")
            names = ["rowid"]
        except sqlite3.OperationalError:  # WITHOUT ROWID
            names = []
        names += ['"' + c.replace('"', '""') + '"' for c in cols]
        copy.execute(f"DELETE FROM main.{quoted}")  # a virtual table's own rows, made with it
        copy.execute(f"INSERT INTO main.{quoted} ({', '.join(names)}) SELECT {', '.join(names)} "
                     f"FROM src.{quoted} WHERE ({where}) IS NOT 1", params)
    if copy.execute("SELECT 1 FROM src.sqlite_master WHERE name = 'sqlite_sequence'").fetchone() \
            and copy.execute("SELECT 1 FROM main.sqlite_master WHERE name = 'sqlite_sequence'").fetchone():
        copy.execute("DELETE FROM main.sqlite_sequence")
        copy.execute("INSERT INTO main.sqlite_sequence SELECT * FROM src.sqlite_sequence")
    for kind, name, table, sql in schema:
        if kind == "table" and table not in retired and " fts5" in sql.lower().replace("\n", " ").replace("(", " "):
            quoted = '"' + name.replace('"', '""') + '"'
            try:
                copy.execute(f"INSERT INTO main.{quoted}({quoted}, rank) VALUES ('integrity-check', 1)")
            except sqlite3.DatabaseError:
                copy.execute(f"INSERT INTO main.{quoted}({quoted}) VALUES ('rebuild')")
    for kind, name, table, sql in schema:
        if kind in ("index", "trigger", "view") and table not in retired:
            copy.execute(sql)


class _CopyBusy(RuntimeError):
    """Another copy held ``fact-copy.lock`` past this one's wait."""


class _NotAdmitted(RuntimeError):
    """A local publication could not begin before the deadline (6.4, D4)."""


def _copy_hook(stage: str) -> None:
    """Called around a local publication: ``"rows"`` after the fact rows
    are written, ``"commit"`` just before COMMIT, ``"committed"`` just
    after it. A seam for the crash and stall tests; does nothing."""


@contextmanager
def _fact_copy_lock(path: Path, deadline: float):
    """``fact-copy.lock``, waited for until the deadline or
    ``REGISTRY_WAIT_S``, whichever comes first (``_CopyBusy``). Refused
    when this context already holds the memory lock or ``write_gate``: the
    copy lock is always taken first."""
    import fcntl

    from khipu import memory_lock, tombstones

    if memory_lock._HELD.get() is not None or tombstones._OWNED_WRITER_LOCK.get() is not None:
        raise RuntimeError("fact-copy.lock taken inside the memory lock or write_gate")
    fd = os.open(str(path.parent / FACT_COPY_LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        until = min(deadline, time.monotonic() + tombstones.REGISTRY_WAIT_S)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= until:
                    raise _CopyBusy("another fact copy held the copy lock until this one gave up") from None
                time.sleep(0.02)
        yield
    finally:
        os.close(fd)


@contextmanager
def _admitted(path: Path, deadline: float):
    """One local publication (6.4, D4): a short SQLite transaction in the
    caller, admitted only before the deadline. The memory lock, the
    registry's ``write_gate`` and ``BEGIN IMMEDIATE`` are each waited for
    no longer than the time left (``_NotAdmitted``). Once admitted it runs
    to COMMIT or ROLLBACK, past the deadline too; the locks go as soon as
    it ends. Yields the connection and the registry snapshot."""
    from khipu import memory_lock, tombstones

    def left() -> float:
        secs = deadline - time.monotonic()
        if secs <= 0:
            raise _NotAdmitted("the deadline passed before the local publication began")
        return secs

    try:
        with memory_lock.shared(wait=left()), tombstones.lock_wait(left()), tombstones.write_gate() as gate:
            con = sqlite3.connect(str(path), timeout=left(), isolation_level=None)
            try:
                try:
                    con.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    raise _NotAdmitted(f"the replica stayed busy until the deadline ({exc})") from exc
                try:
                    left()
                    yield con, gate
                    _copy_hook("commit")
                    con.execute("COMMIT")
                    _copy_hook("committed")
                except BaseException:
                    if con.in_transaction:
                        con.execute("ROLLBACK")
                    raise
            finally:
                con.close()
    except (tombstones.RegistryBusy, memory_lock.MemoryLockError) as exc:
        raise _NotAdmitted(f"a local lock stayed busy until the deadline ({exc})") from exc


def _conninfo_parts(dsn: str) -> dict[str, str]:
    """A libpq connection string's fields, URI or ``key=value`` form."""
    from urllib.parse import parse_qsl, unquote, urlsplit

    if dsn.startswith(("postgresql://", "postgres://")):
        url = urlsplit(dsn)
        parts = {"host": unquote(url.hostname or ""), "port": str(url.port or ""),
                 "dbname": unquote(url.path.lstrip("/")), "user": unquote(url.username or "")}
        parts.update((k, v) for k, v in parse_qsl(url.query) if k in ("host", "hostaddr", "port", "dbname", "user"))
        return {k: v for k, v in parts.items() if v}
    import re

    return {k: re.sub(r"\\(.)", r"\1", v[1:-1]) if v.startswith("'") else v
            for k, v in re.findall(r"(\w+)\s*=\s*('(?:\\.|[^'\\])*'|\S+)", dsn)}


def _hub_destination() -> str:
    """The configured hub endpoint's fingerprint: host, port, database and
    role, never the password. Parsed without the driver: readers check it
    on the replica-only path, which never imports psycopg."""
    try:
        from khipu.db import resolve_dsn

        dsn = resolve_dsn()
    except Exception:  # noqa: BLE001 - no endpoint configured is one value too
        return "unconfigured"
    return _destination_of(dsn)


def _destination_of(dsn: str) -> str:
    parts = _conninfo_parts(dsn)
    keep = {k: str(parts.get(k) or "") for k in ("host", "hostaddr", "port", "dbname", "user")}
    return hashlib.sha256(json.dumps(keep, sort_keys=True).encode("utf-8")).hexdigest()


def _hub_incarnation(cur) -> str:
    """The hub's incarnation (6.1, N3): its cluster's system identifier and
    timeline, and its database's oid."""
    cur.execute(f"SELECT {_INCARNATION_SQL}")
    return str(cur.fetchone()[0])


# The copy's first exchange, one simple query: the end of a handed-over
# pull's read-only transaction (a warning on an idle session), its own
# REPEATABLE READ snapshot,
# the statement bounds (6.4), then one statement with the incarnation, the
# load rule's hub half and the skip: readiness, the snapshot itself, which
# registry strings are hub refusals, which bare ids are live episodes, and,
# when the held stamp names this incarnation, whether any fact change is
# missing from its snapshot S (``min(xid) <= xmin(S)`` and no row at or
# above ``xmin(S)`` invisible in S; an empty table never skips).
_INCARNATION_SQL = ("(SELECT system_identifier::text FROM pg_control_system()) || ':' || "
                    "(SELECT timeline_id::text FROM pg_control_checkpoint()) || ':' || "
                    "(SELECT oid::text FROM pg_database WHERE datname = current_database())")
# The incarnation alone, read in the session's first exchange (a fresh
# session's bounds, a pull's reads) so it reaches the caller before any read
# a lock can block (D2); it cannot change during one session.
_INCARNATION_READ = f"SELECT {_INCARNATION_SQL}"
_COPY_CHECK_SQL = f"""
ROLLBACK;
BEGIN ISOLATION LEVEL REPEATABLE READ;
SELECT set_config('statement_timeout', %(ms)s, true), set_config('lock_timeout', %(ms)s, true);
WITH inc AS (SELECT {_INCARNATION_SQL} AS v),
     held AS (SELECT CASE WHEN (SELECT v FROM inc) = %(held_incarnation)s
                          THEN %(held)s::pg_snapshot END AS s)
SELECT
  (SELECT v FROM inc),
  (SELECT state FROM fact_copy_ready),
  pg_current_snapshot()::text,
  ARRAY(SELECT identity FROM forgotten_sources WHERE identity = ANY(%(forms)s::text[])),
  ARRAY(SELECT json_build_object('id', id, 'summary', summary)::text FROM episodes
        WHERE id = ANY(%(ids)s::bigint[]) AND deleted_at IS NULL ORDER BY id),
  CASE WHEN (SELECT s FROM held) IS NULL THEN false ELSE
    coalesce((SELECT min(xid) FROM fact_changes) <= pg_snapshot_xmin((SELECT s FROM held)), false)
    AND NOT EXISTS (SELECT 1 FROM fact_changes
                    WHERE xid >= pg_snapshot_xmin((SELECT s FROM held))
                      AND NOT pg_visible_in_snapshot(xid, (SELECT s FROM held))) END
"""


def _read_fact_rows(cur) -> dict[str, Any]:
    """Every hub decision and deliverable a hub reader returns (none
    anchored to a forgotten episode), read on ``cur``."""
    out: dict[str, Any] = {"decisions": [], "deliverables": []}
    for table, wanted in (("decisions", _DECISION_COLS), ("deliverables", _DELIVERABLE_COLS)):
        cols = [c for c in wanted if c in _pg_columns(cur, table)]
        if "id" not in cols:
            continue
        live = ""
        if "episode_id" in cols and "deleted_at" in _pg_columns(cur, "episodes"):
            live = (f" WHERE (episode_id IS NULL OR NOT EXISTS (SELECT 1 FROM episodes e "
                    f"WHERE e.id = {table}.episode_id AND e.deleted_at IS NOT NULL))")
        cur.execute(f"SELECT {', '.join(cols)} FROM {table}{live} ORDER BY id")
        out[table] = [dict(zip(cols, r)) for r in cur.fetchall()]
    return out


_VECTOR_LIVE = ("NOT EXISTS (SELECT 1 FROM episodes e WHERE e.id = c.opened_episode "
                "AND e.deleted_at IS NOT NULL)")


def _read_commitment_vectors(cur, held: Mapping[tuple, tuple]) -> tuple[dict[tuple, tuple], list[dict[str, Any]]]:
    """The hub's commitment vectors for the commitments the copy reads: every
    key ``(profile, ref, chunk_idx)`` with its ``(built_at, md5 of the
    commitment's text)`` in this snapshot, and the rows themselves only for
    keys the held copy does not have with the same pair. Each row carries
    its wording hash (``fact_wording``) for ``_commitment_vector_ok``."""
    emb_cols = _pg_columns(cur, "memory_embeddings")
    names = [c for c in _EMBED_COLS if c in emb_cols]
    if not {"profile", "kind", "ref", "chunk_idx", "chunk_text", "built_at"} <= set(names) \
            or not {"id", "text", "opened_episode"} <= _pg_columns(cur, "commitments"):
        return {}, []
    cur.execute("SELECT m.profile, m.ref, m.chunk_idx, m.built_at, md5(c.text) FROM memory_embeddings m "
                f"JOIN commitments c ON c.id::text = m.ref WHERE m.kind = 'commitment' AND {_VECTOR_LIVE}")
    keys = {(str(p), str(r), int(i)): (_ts_text(b), h) for p, r, i, b, h in cur.fetchall()}
    want = sorted(k for k, v in keys.items() if held.get(k) != v)
    sel = ", ".join("m.embedding::text AS embedding" if c == "embedding" else f"m.{c}" for c in names)
    rows: list[dict[str, Any]] = []
    for part in _chunks(want):
        cur.execute(f"SELECT {sel}, md5(c.text) FROM memory_embeddings m "
                    "JOIN commitments c ON c.id::text = m.ref "
                    "JOIN unnest(%s::text[], %s::text[], %s::int[]) AS w(profile, ref, chunk_idx) "
                    "ON w.profile = m.profile::text AND w.ref = m.ref AND w.chunk_idx = m.chunk_idx "
                    "WHERE m.kind = 'commitment'",
                    ([k[0] for k in part], [k[1] for k in part], [k[2] for k in part]))
        rows.extend({**dict(zip(names, r[:-1])), "fact_wording": r[-1]} for r in cur.fetchall())
    return keys, rows


def _copy_read(tx, local: Mapping[str, Any]) -> dict[str, Any]:
    """The copy's hub work, on the worker (``provenance.hub_call``), in one
    REPEATABLE READ snapshot. The session's incarnation (``tx.prelude``,
    read before anything a lock can hold up) is handed to the caller first
    (D2); then one statement reads the incarnation again with the load
    rule's hub half and the skip, and unless skipped or refused the facts
    follow. Writes nothing locally."""
    from psycopg import ClientCursor
    from psycopg.errors import UndefinedTable

    from khipu import provenance
    from khipu.tombstones import hub_form

    conn = tx.conn
    if not conn.autocommit:  # a handed-over pull session already is, mid-transaction
        conn.autocommit = True  # the transaction is the statement's own BEGIN

    def end() -> None:
        tx.rollback()
        conn.autocommit = False

    stamp = local.get("stamp")
    held = stamp if stamp and stamp.get("destination") == local["destination"] else None
    forms = {t: hub_form(t) for t in local["tags"]}
    early = None if tx.prelude is None else str(tx.prelude)
    if early is not None and tx.handoff(early):
        end()
        return {"incarnation": early, "restart": True}
    ms = str(provenance.remaining_ms(tx.deadline))
    try:
        with ClientCursor(conn) as cur:
            cur.execute(_COPY_CHECK_SQL, {
                "ms": ms, "forms": sorted({f for f in forms.values() if f}), "ids": sorted(local["ids"]),
                "held_incarnation": held and held.get("incarnation"), "held": held and held.get("snapshot")})
            while cur.nextset():
                pass
            incarnation, state, snapshot, present, unmet, unchanged = cur.fetchone()
    except UndefinedTable:
        # A hub before 0047: the incarnation alone, still handed over first.
        end()
        out: dict[str, Any] = {"incarnation": early or _hub_incarnation(tx.cur)}
        if out["incarnation"] != early and tx.handoff(out["incarnation"]):
            return {**out, "restart": True}
        return {**out, "not_ready": "the hub has no fact provenance yet (migration 0047)"}
    out = {"incarnation": str(incarnation)}
    if out["incarnation"] != early and tx.handoff(out["incarnation"]):
        end()
        return {**out, "restart": True}
    if state != "ready":
        end()
        return {**out, "not_ready": ("the hub's readiness row is missing" if state is None
                                     else "the hub is still indexing its history")}
    present = set(present or ())
    out.update(snapshot=snapshot, missing=sorted(t for t, f in forms.items() if f not in present),
               unmet=[json.loads(u) for u in unmet or ()])
    if out["missing"] or out["unmet"]:
        end()
        return out
    if unchanged:
        end()
        return {**out, "skip": True}
    cur = tx.cur
    out["commitments"] = _hub_commitments(cur)
    out.update(_read_fact_rows(cur))
    out["vector_keys"], out["vectors"] = _read_commitment_vectors(cur, _held_vectors(local))
    end()
    return out


def _held_vectors(local: Mapping[str, Any]) -> dict[tuple, tuple]:
    """The held commitment vectors, read once and only when a copy changes
    (a skip never needs them)."""
    held = local.get("held_vectors")
    return (held() if callable(held) else held) or {}


def _held_commitment_vectors(con: sqlite3.Connection) -> dict[tuple, tuple]:
    """``{(profile, ref, chunk_idx): (built_at, md5 of the text)}`` of the
    commitment vectors the replica holds, against its commitments' text."""
    if not {"kind", "ref", "chunk_idx", "built_at"} <= _snapshot_table_columns(con, "memory_embeddings") \
            or not {"id", "text"} <= _snapshot_table_columns(con, "commitments"):
        return {}
    return {(str(p), str(r), int(i)): (b, hashlib.md5(t.encode("utf-8")).hexdigest())
            for p, r, i, b, t in con.execute(
                "SELECT m.profile, m.ref, m.chunk_idx, m.built_at, c.text FROM memory_embeddings m "
                "JOIN commitments c ON CAST(c.id AS TEXT) = m.ref WHERE m.kind = 'commitment'")
            if isinstance(t, str) and isinstance(i, int)}


def _read_stamp(con: sqlite3.Connection) -> dict[str, Any] | None:
    digest = _meta_get(con, _FACT_DIGEST_KEY)
    text = _meta_get(con, _FACT_STAMP_KEY)
    if digest is None or text is None:
        return None
    try:
        stamp = json.loads(text)
    except ValueError:
        return None
    return {**stamp, "fingerprint": digest} if isinstance(stamp, dict) else None


def _local_copy_state(path: Path) -> dict[str, Any]:
    """What a copy reads locally before the hub: the registry (its strings,
    bare ids and fingerprint), the stamp, the invalidation revision, the
    listing and the held commitment vectors. ``RegistryError`` or
    ``sqlite3.Error`` when either cannot be read."""
    from khipu.tombstones import read_gate

    gate = read_gate()
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as con:
        state = {"meta": bool(_snapshot_table_columns(con, "snapshot_meta")),
                 "stamp": _read_stamp(con), "revision": _revision(con),
                 "listed": _meta_get(con, _FACT_UNSENT_KEY) is not None}
        try:
            state["certified"] = json.loads(_meta_get(con, _FACT_CERTIFIED_KEY) or "null")
        except ValueError:
            state["certified"] = None

    @functools.lru_cache(maxsize=1)
    def held() -> dict[tuple, tuple]:
        # Fact vectors change only under the copy lock this copy holds.
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as con:
            return _held_commitment_vectors(con)

    state["held_vectors"] = held
    state["endpoint_changes"] = _endpoint_changes(path)
    # Read before resolving: a save landing after it makes this record stale.
    try:
        generation: int | None = _endpoint_generation(path)
    except OSError as exc:
        _record_write_error(_FACT_TABLES, f"endpoint generation: {type(exc).__name__}: {exc}")
        generation = None
    destination = _hub_destination()
    if _env_dsn() is None and generation is not None:
        # The saved endpoint itself: recorded for readers, which never ask
        # the Keychain (a Mac configured before any save records it here).
        try:
            _record_destination(path, destination, _endpoint_source(destination), expect=generation)
        except OSError as exc:  # readers then show no fact row; the copy goes on
            _record_write_error(_FACT_TABLES, f"endpoint fingerprint: {type(exc).__name__}: {exc}")
    return {**state, "fingerprint": _refusal_digest(gate, path), "tags": sorted(gate.tags),
            "ids": sorted(gate.episode_ids), "destination": destination}


def _write_fact_deliverables(con: sqlite3.Connection, rows: Iterable[Mapping[str, Any]]) -> int:
    rejected = _rejected_episode_ids(con)
    sel = ", ".join(_DELIVERABLE_COLS)
    written = 0
    for row in rows:
        if row.get("episode_id") in rejected:
            continue
        con.execute(f"INSERT OR REPLACE INTO deliverables ({sel}) VALUES ({', '.join('?' * len(_DELIVERABLE_COLS))})",
                    [_ts_text(row.get(c)) if c == "created_at" else row.get(c) for c in _DELIVERABLE_COLS])
        written += 1
    return written


def _commitment_vector_ok(text: Any, row: Mapping[str, Any]) -> bool:
    """A commitment vector is stored only against the wording it was built
    from: it must carry the md5 of its commitment's text from the snapshot
    it was read in (``fact_wording``), equal to the text the replica holds,
    and its chunk must be that text's chunk at its index."""
    if not isinstance(text, str) or row.get("fact_wording") != hashlib.md5(text.encode("utf-8")).hexdigest():
        return False
    from khipu.embed import chunk_text

    chunks = chunk_text(text.strip())
    idx = row.get("chunk_idx")
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return False
    return 0 <= idx < len(chunks) and chunks[idx] == row.get("chunk_text")


def _insert_vector_row(con: sqlite3.Connection, erow: Mapping[str, Any]) -> None:
    ecols = [c for c in _EMBED_COLS if c in erow]
    evals = []
    for c in ecols:
        v = erow.get(c)
        if c == "embedding":
            v = _vector_to_blob(v)
        elif c == "built_at":
            v = _ts_text(v)
        evals.append(v)
    con.execute(f"INSERT OR REPLACE INTO memory_embeddings ({', '.join(ecols)}) "
                f"VALUES ({', '.join('?' * len(ecols))})", evals)


def _write_copy(con: sqlite3.Connection, local: Mapping[str, Any], read: Mapping[str, Any]) -> dict[str, int]:
    """Replace the fact tables whole with a copy's read and write its stamp,
    in the caller's publication transaction. Commitment vectors whose key,
    build time and wording the hub still has are kept; the rest are
    replaced by the read's rows that pass ``_commitment_vector_ok``."""
    _drop_retired(con)
    for table in _FACT_TABLES:
        if _snapshot_table_columns(con, table):
            con.execute(f"DELETE FROM {table}")
    counts = {"commitments": 0, "decisions": 0, "deliverables": 0, "vectors": 0}
    if read.get("commitments") is not None and _snapshot_table_columns(con, "commitments"):
        counts["commitments"] = _write_commitment_rows(con, *read["commitments"])
    if _snapshot_table_columns(con, "decisions"):
        counts["decisions"] = _write_decision_rows(con, read.get("decisions") or ()) or 0
    if set(_DELIVERABLE_COLS) <= _snapshot_table_columns(con, "deliverables"):
        counts["deliverables"] = _write_fact_deliverables(con, read.get("deliverables") or ())
    if {"kind", "ref", "chunk_idx"} <= _snapshot_table_columns(con, "memory_embeddings"):
        texts: dict[str, str] = {}
        if {"id", "text", "status"} <= _snapshot_table_columns(con, "commitments"):
            texts = {str(i): t for i, t in con.execute("SELECT id, text FROM commitments WHERE status = 'open'")}
        keys = read.get("vector_keys") or {}
        held = _held_vectors(local)
        keep = {k for k, v in keys.items() if held.get(k) == v and k[1] in texts}
        con.execute("DELETE FROM memory_embeddings WHERE kind IN "
                    f"({', '.join('?' * len(_FACT_VECTOR_KINDS[1:]))})", _FACT_VECTOR_KINDS[1:])
        for rowid, p, r, i in con.execute("SELECT rowid, profile, ref, chunk_idx FROM memory_embeddings "
                                          "WHERE kind = 'commitment'").fetchall():
            if (str(p), str(r), i) not in keep:
                con.execute("DELETE FROM memory_embeddings WHERE rowid = ?", (rowid,))
        for row in read.get("vectors") or ():
            if _commitment_vector_ok(texts.get(str(row.get("ref"))), row):
                _insert_vector_row(con, row)
                counts["vectors"] += 1
        counts["vectors_kept"] = len(keep)
    _copy_hook("rows")
    now = _utcnow_iso()
    for table, cols in (("decisions", ("created_at", "superseded_at", "retracted_at")),
                        ("deliverables", ("created_at",))):
        rows = read.get(table) or ()
        _advance_cursor(con, table, synced_at=now, reconciled_at=now,
                        newest=_newest_ts(r.get(c) for r in rows for c in cols),
                        newest_id=max((int(r["id"]) for r in rows if r.get("id") is not None), default=None))
    _meta_put(con, _FACT_DIGEST_KEY, local["fingerprint"])
    _meta_put(con, _FACT_STAMP_KEY, json.dumps({"incarnation": read["incarnation"],
                                                "destination": local["destination"],
                                                "snapshot": read["snapshot"]}, sort_keys=True))
    _meta_put(con, _FACT_UNSENT_KEY, None)
    _meta_put(con, _FACT_CERTIFIED_KEY, json.dumps(local.get("tags", [])))
    return counts


def _publish_copy(con: sqlite3.Connection, gate: Any, local: Mapping[str, Any], read: Mapping[str, Any],
                  path: Path) -> dict[str, Any]:
    """The load rule's local half, then the publication, inside the
    caller's admitted transaction (``_admitted``): the registry fingerprint
    and the invalidation revision must be the ones read before the hub, and
    the destination the one copied from. Forgets the hub lacks are listed
    instead, and nothing is published."""
    if _refusal_digest(gate, path) != local["fingerprint"]:
        return {"ok": False, "refreshed": False, "error": "the registry changed during the copy"}
    if _revision(con) != local["revision"]:
        return {"ok": False, "refreshed": False, "invalidated": True,
                "error": "a remote forget was learned during the copy"}
    if _hub_destination() != local["destination"] or _endpoint_changes(path) != local["endpoint_changes"]:
        return {"ok": False, "refreshed": False, "error": "the hub endpoint changed during the copy"}
    if read["missing"] or read["unmet"]:
        listing = {"strings": read["missing"], "ids": read["unmet"]}
        held = _meta_get(con, _FACT_UNSENT_KEY)
        if held is None or {k: v for k, v in json.loads(held).items() if k != "at"} != listing:
            _meta_put(con, _FACT_UNSENT_KEY, json.dumps({**listing, "at": _utcnow_iso()}, sort_keys=True))
        # Facts are hidden while anything is listed, even when the registry
        # is unchanged (a bare id the hub's own sequence has reached).
        if _meta_get(con, _FACT_DIGEST_KEY) is not None:
            _meta_put(con, _FACT_DIGEST_KEY, None)
            _meta_put(con, _FACT_STAMP_KEY, None)
        return {"ok": False, "refreshed": False, "unsent": len(read["missing"]) + len(read["unmet"]),
                "error": "this Mac holds forgets the hub lacks; facts stay hidden until they are resolved"}
    if read.get("skip"):
        if _meta_get(con, _FACT_DIGEST_KEY) != local["fingerprint"]:
            _meta_put(con, _FACT_DIGEST_KEY, local["fingerprint"])
        if _meta_get(con, _FACT_CERTIFIED_KEY) != json.dumps(local["tags"]):
            _meta_put(con, _FACT_CERTIFIED_KEY, json.dumps(local["tags"]))
        if local["listed"]:
            _meta_put(con, _FACT_UNSENT_KEY, None)
        return {"ok": True, "skipped": True}
    return {"ok": True, "copied": True, **_write_copy(con, local, read)}


def _not_refreshed(error: Any, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "refreshed": False, "error": str(error)[:300], **extra}


def _copy_once(path: Path, deadline: float, connect: Any, *, may_restart: bool) -> dict[str, Any]:
    from khipu import provenance
    from khipu.tombstones import RegistryError

    try:
        local = _local_copy_state(path)
        if not local["meta"]:
            return _not_refreshed("this replica is too old to hold a fact copy; refresh it", needs_dump=True)
        stamp = local["stamp"]
        if stamp is not None and stamp.get("destination") != local["destination"]:
            # N3: another configured hub; its copy certifies itself afresh.
            with _admitted(path, deadline) as (con, _gate):
                _invalidate_facts(con)
            local = _local_copy_state(path)
    except (RegistryError, sqlite3.Error, _NotAdmitted) as exc:
        return _not_refreshed(f"{type(exc).__name__}: {exc}")

    def on_handoff(incarnation: str) -> bool:
        # D2: a detected new incarnation hides the old copy at once, whether
        # or not this copy later succeeds.
        held = local["stamp"]
        if held is None or held.get("incarnation") == incarnation:
            return False
        with _admitted(path, deadline) as (con, _gate):
            _invalidate_facts(con)
        return True

    opener = connect or (lambda **kw: try_hub_connect(**kw))
    try:
        read = provenance.hub_call(lambda tx: _copy_read(tx, local), deadline=deadline,
                                   connect=opener, on_handoff=on_handoff, prelude=_INCARNATION_READ)
    except _NotAdmitted as exc:
        return _not_refreshed(exc)
    except provenance.NotDone as exc:
        return _not_refreshed(f"facts not refreshed: {exc}", uncertain=exc.uncertain)
    except Exception as exc:  # noqa: BLE001 - fail-open: the last good copy stays
        _record_write_error(_FACT_TABLES, f"fact copy: {type(exc).__name__}: {exc}")
        return _not_refreshed(f"{type(exc).__name__}: {exc}", hub_unreachable=hub_connection_failed(exc))
    if read.get("restart"):
        if may_restart:
            return {**_copy_once(path, deadline, connect, may_restart=False), "incarnation_changed": True}
        return _not_refreshed("the hub's incarnation changed twice during one copy")
    if read.get("not_ready"):
        return _not_refreshed(read["not_ready"], not_ready=True)
    held = local["stamp"]
    if read.get("skip") and not local["listed"] and held is not None and not local["endpoint_changes"] \
            and held.get("fingerprint") == local["fingerprint"] and local["certified"] == local["tags"]:
        # Nothing changed on the hub or here: nothing to write, unless an
        # invalidation landed since the local read (then the admitted path
        # reports it).
        try:
            with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as con:
                unchanged = _revision(con) == local["revision"]
        except sqlite3.Error:
            unchanged = False
        if unchanged:
            return {"ok": True, "skipped": True}
    try:
        with _admitted(path, deadline) as (con, gate):
            out = _publish_copy(con, gate, local, read, path)
        if out.get("ok"):
            # This copy began after those endpoint saves and certified itself.
            for name in local["endpoint_changes"]:
                (path.parent / _ENDPOINT_CHANGES_DIR / name).unlink(missing_ok=True)
        return out
    except _NotAdmitted as exc:
        # A lock wait that ran out, not a fault: the next copy reconciles.
        return _not_refreshed(exc, deferred=True)
    except Exception as exc:  # noqa: BLE001 - rolled back: the previous copy and stamp stay
        _record_write_error(_FACT_TABLES, f"fact copy: {type(exc).__name__}: {exc}")
        return _not_refreshed(f"{type(exc).__name__}: {exc}")


def copy_facts(*, connect: Any = None, deadline: float | None = None, session: Any = None) -> dict[str, Any]:
    """Copy the hub's facts into this Mac's replica (design section 6.2):
    under ``fact-copy.lock``, read the hub in one snapshot on a worker
    bounded by one deadline (``FORGET_RETRY_BUDGET_S``, section 6.4), then
    publish in one SQLite transaction only if the load rule holds; an
    unchanged hub is skipped. ``connect`` takes ``connect_timeout`` (the
    hub's by default). A copy that fails publishes nothing and the last
    good copy stays, unless an invalidation cleared it. Never raises, but
    for being called inside the memory lock or ``write_gate`` (the copy
    lock is outermost: a ``RuntimeError``). ``session``: an idle hub
    connection the copy uses first instead of connecting (a pull's), closed
    by the copy whether it is used or not."""
    if session is not None:
        fallback = connect or (lambda **kw: try_hub_connect(**kw))
        handed = [session]

        def connect(**kw):
            if handed:
                return handed.pop()
            return fallback(**kw)

        try:
            return copy_facts(connect=connect, deadline=deadline)
        finally:
            for unused in handed:
                _close_quietly(unused)
    path = snapshot_path()
    if not path.is_file():
        return {"ok": True, "skipped": True, "reason": "no snapshot exists"}
    if deadline is None:
        from khipu.forget import FORGET_RETRY_BUDGET_S

        deadline = time.monotonic() + FORGET_RETRY_BUDGET_S
    started = time.monotonic()
    if started >= deadline:
        return {**_not_refreshed("the operation's deadline passed; a later copy reconciles"), "seconds": 0.0}
    try:
        with _fact_copy_lock(path, deadline):
            out = _copy_once(path, deadline, connect, may_restart=True)
    except _CopyBusy as exc:
        # Another copy is running and holds the lock: this one defers to it
        # (a later copy reconciles), which callers must not read as a fault.
        out = _not_refreshed(exc, deferred=True)
    out["seconds"] = round(time.monotonic() - started, 3)
    return out


# Rows per kind one pull applies. Past it a full dump is the cheaper way to
# catch up (the live hub took about 900 episodes in a day, 2026-10-15), and a
# bounded pull is one that cannot hold the refresh lock for long.
PULL_LIMIT = 2000
# How long a live replica patch (a capture's, an outbox replay's) waits for
# another replica write before it is deferred to the next pull.
LIVE_PATCH_WAIT_S = float(os.environ.get("KHIPU_LIVE_PATCH_WAIT_S", "2"))
_PULL_BATCH = 100


def _pull_needs_dump(limit: int) -> dict[str, Any]:
    return {"ok": False, "needs_dump": True, "error": f"more than {limit} changes since the last update"}


def _replica_rows(table: str, key: str, keys: Sequence[Any]) -> tuple[set[Any], set[Any]]:
    """``(live, deleted)``: which of ``keys`` the replica's ``table`` holds."""
    if not keys:
        return set(), set()
    live: set[Any] = set()
    deleted: set[Any] = set()
    try:
        con = sqlite3.connect(f"file:{snapshot_path()}?mode=ro", uri=True)
    except sqlite3.Error:
        return live, deleted
    try:
        cols = _snapshot_table_columns(con, table)
        if key not in cols:
            return live, deleted
        mark = ", deleted_at IS NOT NULL" if "deleted_at" in cols else ", 0"
        for part in _chunks(list(keys)):
            for k, gone in con.execute(
                    f"SELECT {key}{mark} FROM main.{table} WHERE {key} IN ({', '.join('?' * len(part))})", part):
                (deleted if gone else live).add(k)
    finally:
        con.close()
    return live, deleted


def _cursor_after(old: tuple[datetime, int], newest: tuple[datetime, int] | None,
                  cap: datetime) -> tuple[datetime, int]:
    """Where a cursor moves: to the newest row read, but never past ``cap``
    (the hub's now minus the overlap), and never back."""
    new = (cap, 0)
    if newest is not None and newest[0] <= cap:
        new = newest
    return max(old, new)


def _read_ledger_pull(cur, limit: int) -> dict[str, Any] | None:
    """Compare row versions and history tails, never timestamps: a delayed
    write can carry an old authored time. Projects and vocabulary have no
    revisions, so each pull compares their complete, small manifests and
    passes only changed projects to the writer."""
    if "row_id" not in _pg_columns(cur, "ledger_rows"):
        return None
    with closing(sqlite3.connect(f"file:{snapshot_path()}?mode=ro", uri=True)) as con:
        if not _snapshot_table_columns(con, "ledger_rows"):
            return None
        if not _snapshot_table_columns(con, "ledger_gone"):
            return {"ok": False, "needs_refresh": True, "error": "ledger copy has no removal markers; refresh it"}
        held_projects = {p: (name, hidden, created) for p, name, hidden, created in con.execute(
            "SELECT project, name, hidden, created_at FROM ledger_projects")}
        held_vocab: dict[str, dict[str, list]] = {}
        for p, field, word, meaning in con.execute(
                "SELECT project, field, word, meaning FROM ledger_vocab ORDER BY project, field, position"):
            words = held_vocab.setdefault(p, {"state": [], "kind": []})
            words[field].append([word, meaning] if field == "state" else word)
        held = {(p, r): (v, h) for p, r, v, h in con.execute(
            "SELECT r.project, r.row_id, r.version, coalesce(max(h.id), 0) "
            "FROM ledger_rows r LEFT JOIN ledger_history h "
            "ON h.project=r.project AND h.row_id=r.row_id GROUP BY r.project, r.row_id, r.version")}
        marks = {(p, r): v for p, r, v in con.execute("SELECT project, row_id, version FROM ledger_gone")}
    cur.execute("SELECT r.project, r.row_id, r.version, r.removed_at, coalesce(max(h.id), 0) "
                "FROM ledger_rows r LEFT JOIN ledger_history h "
                "ON h.project=r.project AND h.row_id=r.row_id "
                "GROUP BY r.project, r.row_id, r.version, r.removed_at")
    changed: dict[str, list[str]] = {}
    gone: dict[str, dict[str, int]] = {}
    for project, row_id, version, removed, tail in cur.fetchall():
        old = held.get((project, row_id))
        mark = marks.get((project, row_id), 0)
        if removed is not None:
            if max(mark, old[0] if old else 0) <= version:
                if mark < version or old is not None:
                    gone.setdefault(project, {})[row_id] = version
        elif mark < version and (old is None or (version, tail) > old):
            changed.setdefault(project, []).append(row_id)
    cur.execute("SELECT project, row_id FROM ledger_removed WHERE purged_at IS NOT NULL")
    for project, row_id in cur.fetchall():
        if marks.get((project, row_id), 0) < LEDGER_PURGED_VERSION or (project, row_id) in held:
            gone.setdefault(project, {})[row_id] = LEDGER_PURGED_VERSION
    if sum(map(len, changed.values())) + sum(map(len, gone.values())) > limit:
        return {"ok": False, "needs_refresh": True,
                "error": f"more than {limit} ledger changes; refresh the ledger copy"}
    cur.execute("SELECT project, name, hidden, created_at FROM ledger_projects")
    projects = {p: (name, hidden, created) for p, name, hidden, created in cur.fetchall()}
    vocab: dict[str, dict[str, list]] = {}
    cur.execute("SELECT project, field, word, meaning, position FROM ledger_vocab ORDER BY project, field, position")
    for p, field, word, meaning, _position in cur.fetchall():
        words = vocab.setdefault(p, {"state": [], "kind": []})
        words[field].append([word, meaning] if field == "state" else word)
    # Only changed manifests cross into the writer. Row-only changes leave
    # project metadata and vocabulary untouched.
    vocab = {p: vocab.get(p, {"state": [], "kind": []}) for p in set(projects) | set(held_vocab)
             if vocab.get(p, {"state": [], "kind": []}) != held_vocab.get(p, {"state": [], "kind": []})}
    projects = {p: value for p, value in projects.items()
                if (value[0], int(bool(value[1])), _ts_text(value[2])) != held_projects.get(p)}
    rows: dict[str, list] = {}
    history: dict[str, list] = {}
    for project, ids in changed.items():
        cur.execute(f"SELECT {', '.join(_LEDGER_ROW_COLS)} FROM ledger_rows "
                    "WHERE project = %s AND row_id = ANY(%s) AND removed_at IS NULL", (project, ids))
        rows[project] = [dict(zip(_LEDGER_ROW_COLS, r, strict=True)) for r in cur.fetchall()]
        cur.execute(f"SELECT {', '.join(_LEDGER_HIST_COLS)} FROM ledger_history "
                    "WHERE project = %s AND row_id = ANY(%s) ORDER BY id", (project, ids))
        history[project] = [dict(zip(_LEDGER_HIST_COLS, r, strict=True)) for r in cur.fetchall()]
    return {"ok": True, "projects": projects, "vocab": vocab, "rows": rows, "history": history, "gone": gone,
            "receipts": _read_ledger_receipts(cur, _queued_op_ids())}


def _write_ledger_pull(payload: Mapping[str, Any] | None, *, read_at: float) -> dict[str, Any]:
    if payload is None:
        return {"ok": True, "not_applicable": True}
    return ledger_write_snapshot("", None, {}, [], [], [], _pull=payload, read_at=read_at)


def _apply_ledger_pull(con: sqlite3.Connection, payload: Mapping[str, Any], *,
                       read_at: float) -> dict[str, Any]:
    """Apply the complete ledger part on the registered replica writer's transaction."""
    # A dump published since this hub read already contains a newer
    # ledger. Its data and freshness marker stay together.
    if _held_read_at(con, "ledger", "*") > read_at:
        return {"ok": True, "skipped": "the replica holds a newer copy"}
    projects = payload["projects"]
    for project in set(projects) | set(payload["vocab"]) | set(payload["gone"]) | set(payload["rows"]):
        _write_ledger_project(
            con, project, projects.get(project),
            payload["vocab"].get(project),
            payload["rows"].get(project, []), payload["history"].get(project, []),
            payload["gone"].get(project, {}), read_at=read_at)
    _write_ledger_receipts(con, payload.get("receipts"))
    if _held_read_at(con, "ledger", "*") < read_at:
        _record_read_at(con, "ledger", "*", read_at)
        con.execute("INSERT OR REPLACE INTO snapshot_meta(key,value) VALUES ('ledger_caught_up_at', ?)",
                    (datetime.fromtimestamp(read_at, timezone.utc).isoformat(),))
    return {"ok": True}


def _one_exchange(cur, reads: Sequence[tuple[str, str, Sequence[Any]]], *,
                  prelude: str | None = None, on_prelude: Callable[[str], Any] | None = None) -> dict[str, list]:
    """``reads`` in two simple queries, so two round trips. First the
    session bounds (``provenance.SESSION_SETTINGS``, D3) and ``prelude`` (a
    single-value read, kept on the session as ``khipu_prelude`` for
    ``hub_call``), outside a transaction so a later ROLLBACK cannot undo the
    bounds; ``on_prelude`` gets the prelude's value once that exchange is
    complete, before the reads are sent (D2), so no later SQL or transport
    failure can discard it. Then a REPEATABLE READ transaction and each
    read, bound client side. Returns each read's rows by name; the
    transaction stays open on ``conn`` (autocommit, so the caller's later
    statements run inside it and its ``rollback`` ends it). The session is
    marked bounded, so a copy handed it (``copy_facts``) does not set the
    bounds again."""
    from psycopg import ClientCursor, Connection

    from khipu.provenance import SESSION_SETTINGS

    conn = getattr(cur, "connection", None)
    if not isinstance(conn, Connection):
        # A DB-API cursor with no psycopg connection to batch on: one by one.
        out: dict[str, list] = {}
        for name, sql, params in reads:
            cur.execute(sql, params)
            out[name] = list(cur.fetchall())
        return out
    conn.rollback()  # a schema probe may have opened a transaction; nothing is read yet
    conn.autocommit = True
    with ClientCursor(conn) as first:
        first.execute("; ".join([*SESSION_SETTINGS, *([prelude] if prelude else [])]))
        if prelude:
            while first.nextset():
                pass
            value = str(first.fetchone()[0])
            conn.khipu_prelude = {prelude: value}
            if on_prelude is not None:
                on_prelude(value)
    head = ["BEGIN ISOLATION LEVEL REPEATABLE READ"]
    with ClientCursor(conn) as batch:
        batch.execute("; ".join([*head, *(batch.mogrify(sql, params) for _name, sql, params in reads)]))
        results: list[list] = []
        while True:
            results.append(batch.fetchall() if batch.description else [])
            if not batch.nextset():
                break
    conn.khipu_bounded = True
    return {name: rows for (name, _sql, _params), rows in zip(reads, results[len(head):])}


def _pull_apply(cur, state: Mapping[str, Any], limit: int, *, include_ledger: bool = True) -> dict[str, Any]:
    """The hub half of ``pull_changes``, on its one cursor. Everything the
    memory limit can refuse is read before the first write. Ledger failures
    are reported separately and never refuse the memory part."""
    from datetime import timedelta

    # Taken before the transaction snapshot, never after its first read.
    read_at = time.time()
    hub_cols = _pg_columns(cur, "episodes")
    emb_cols = _pg_columns(cur, "memory_embeddings")
    topic_hub_cols = _pg_columns(cur, "topics")
    alias_cols = _pg_columns(cur, "project_aliases")
    cols = [c for c in _EPISODE_COLS if c in hub_cols]
    sel = ", ".join(cols)
    after = (state["episodes_after"], state["episodes_after_id"])
    # Every read below depends on nothing read here, so all go in one
    # exchange (``_one_exchange``) that also opens the snapshot.
    reads: list[tuple[str, str, Sequence[Any]]] = [("now", "SELECT now()", ())]
    # Episodes by their change stamp (migration 0032), so an episode edited in
    # place (a capture merged into it) is read again; a row the stamp has not
    # reached yet, or a hub without it, by its ingest time.
    stamped = "changed_at" in hub_cols
    if stamped:
        reads.append(("fresh", f"SELECT {sel}, changed_at FROM episodes WHERE (changed_at, id) > (%s, %s) "
                      "OR (changed_at IS NULL AND (ingested_at, id) > (%s, %s)) "
                      "ORDER BY coalesce(changed_at, ingested_at), id LIMIT %s", (*after, *after, limit + 1)))
    else:
        reads.append(("fresh", f"SELECT {sel} FROM episodes WHERE (ingested_at, id) > (%s, %s) "
                      "ORDER BY ingested_at, id LIMIT %s", (*after, limit + 1)))
    if "deleted_at" in hub_cols:
        reads.append(("gone", f"SELECT {sel} FROM episodes WHERE deleted_at > %s ORDER BY deleted_at, id LIMIT %s",
                      (state["tombstones_after"], limit + 1)))
    if {"profile", "kind", "ref", "built_at"} <= emb_cols:
        reads.append(("groups", "SELECT profile, kind, ref, max(built_at) FROM memory_embeddings WHERE built_at > %s "
                      "GROUP BY profile, kind, ref ORDER BY max(built_at), profile, kind, ref LIMIT %s",
                      (state["vectors_after"], limit + 1)))
    held_batches: list[list[str]] = []
    tcols = [c for c in _TOPIC_COLS if c in topic_hub_cols]
    if {"slug", "updated_at"} <= topic_hub_cols:
        where, params = "updated_at > %s", [state["topics_after"]]
        if "deleted_at" in topic_hub_cols:
            where += " OR deleted_at > %s"
            params.append(state["topics_after"])
        reads.append(("topics", f"SELECT {', '.join(tcols)} FROM topics WHERE {where} "
                      "ORDER BY updated_at, slug LIMIT %s", (*params, limit + 1)))
        # A hard purge removes even the tombstone. Compare held keys with
        # this same hub snapshot: a change cursor cannot discover absent rows.
        with closing(sqlite3.connect(f"file:{snapshot_path()}?mode=ro", uri=True)) as con:
            held_topics = ({r[0] for r in con.execute("SELECT slug FROM topics")}
                           if "slug" in _snapshot_table_columns(con, "topics") else set())
        held_batches = list(_chunks(sorted(held_topics)))
        reads.extend((f"held{i}", "SELECT slug FROM topics WHERE slug = ANY(%s)", (b,))
                     for i, b in enumerate(held_batches))
    acols = [c for c in ("alias", "project", "created_at", "source", "evidence") if c in alias_cols]
    if {"alias", "project"} <= alias_cols:
        reads.append(("aliases", f"SELECT {', '.join(acols)} FROM project_aliases ORDER BY alias", ()))
    # D2: the incarnation is learned in its own exchange before the reads
    # are sent, so a failed pull (a lock held past its timeout, a dropped
    # connection) still hides another incarnation's facts.
    got = _one_exchange(cur, reads, prelude=_INCARNATION_READ, on_prelude=_learn_incarnation)
    hub_now = _utc(got["now"][0][0]) or datetime.now(timezone.utc)
    cap = hub_now - timedelta(seconds=FRESHNESS_OVERLAP_S)
    fresh = [dict(zip([*cols, "changed_at"] if stamped else cols, r)) for r in got["fresh"]]
    # Where each row puts the episodes cursor; the stamp itself is hub-only
    # and never written into the replica.
    fresh_stamps = [(r.pop("changed_at", None) or r.get("ingested_at"), r.get("id")) for r in fresh]
    gone = [dict(zip(cols, r)) for r in got.get("gone", ())]
    groups: list[tuple[Any, ...]] = list(got.get("groups", ()))
    # Topic bodies by their change stamp. ``None``: the hub has no such table.
    topic_rows = [dict(zip(tcols, r)) for r in got["topics"]] if "topics" in got else None
    topic_purges: set[str] = set()
    for i, batch in enumerate(held_batches):
        topic_purges.update(set(batch) - {r[0] for r in got[f"held{i}"]})
    aliases = (acols, got["aliases"]) if "aliases" in got else None
    if max(len(fresh), len(gone), len(groups), len(topic_rows or ()) + len(topic_purges)) > limit:
        return _pull_needs_dump(limit)

    ledger_payload = None
    if include_ledger:
        # Even schema discovery belongs to the savepoint: any failed
        # PostgreSQL statement would otherwise abort the memory transaction.
        cur.execute("SAVEPOINT ledger_pull")
        try:
            ledger_payload = _read_ledger_pull(cur, limit)
        except Exception as exc:  # noqa: BLE001 - the ledger is an independent part
            cur.execute("ROLLBACK TO SAVEPOINT ledger_pull")
            ledger_payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
        finally:
            cur.execute("RELEASE SAVEPOINT ledger_pull")

    changed: dict[int, dict[str, Any]] = {int(r["id"]): r for r in [*fresh, *gone]}
    want = set(state["missed"])
    # A vector group is applied alone only when its episode is live in the
    # replica; one for an episode the replica lacks goes through the whole
    # episode, so the forgotten-source fence sees it first.
    ep_refs = [int(g[2]) for g in groups if g[1] == "episode" and str(g[2]).isdigit()]
    live, deleted = _replica_rows("episodes", "id", ep_refs)
    topic_live, topic_deleted = _replica_rows("topics", "slug", [g[2] for g in groups if g[1] == "topic"])
    apply_groups: list[tuple[Any, ...]] = []
    for g in groups:
        if g[1] in _FACT_VECTOR_KINDS:
            # Fact vectors come with the fact copy (copy_facts), never alone.
            continue
        if g[1] == "episode":
            if not str(g[2]).isdigit() or int(g[2]) in changed or int(g[2]) in deleted:
                continue
            if int(g[2]) not in live:
                want.add(int(g[2]))
                continue
        elif g[1] == "topic" and g[2] in topic_deleted:
            continue
        apply_groups.append(g)
    need = sorted(want - set(changed))
    for part in _chunks(need):
        cur.execute(f"SELECT {sel} FROM episodes WHERE id = ANY(%s)", (part,))
        changed.update({int(r[cols.index("id")]): dict(zip(cols, r)) for r in cur.fetchall()})
    absent = want - set(changed)

    emb_sel = ", ".join("embedding::text AS embedding" if c == "embedding" else c
                        for c in _EMBED_COLS if c in emb_cols)
    emb_names = [c for c in _EMBED_COLS if c in emb_cols]
    done: set[int] = set()
    live_count = tomb_count = forgotten = kept_newer = 0
    ordered = sorted(changed)
    # The episode fence reads every identity an episode represents, its
    # retained source receipts too; vectors applied alone are checked by
    # their episode's identities, read in this same snapshot.
    receipts = _read_receipts(cur, [i for i in ordered if changed[i].get("deleted_at") is None])
    vector_anchors = read_anchor_identities(
        cur, {int(g[2]) for g in apply_groups if g[1] == "episode" and str(g[2]).isdigit()})
    for start in range(0, len(ordered), _PULL_BATCH):
        batch = ordered[start:start + _PULL_BATCH]
        alive = [i for i in batch if changed[i].get("deleted_at") is None]
        vecs: dict[int, list[dict[str, Any]]] = {}
        if alive:
            cur.execute(f"SELECT {emb_sel} FROM memory_embeddings WHERE kind = 'episode' AND ref = ANY(%s)",
                        ([str(i) for i in alive],))
            for r in cur.fetchall():
                row = dict(zip(emb_names, r))
                vecs.setdefault(int(row["ref"]), []).append(row)
        for eid in batch:
            row = changed[eid]
            tomb = row.get("deleted_at") is not None
            out = upsert_episode(row, [] if tomb else vecs.get(eid, []), read_at=read_at,
                                 receipts=receipts.get(eid, ()))
            error = str(out.get("error") or "")
            if not out.get("ok") and tomb and "forgotten source" in error:
                # The fence refuses a forgotten source's row, tombstone
                # included; erasing it can never restore one, so it is
                # applied the way a forget on this Mac applies it.
                out = forget_episode_in_snapshot(eid)
                error = str(out.get("error") or "")
            if out.get("skipped"):
                kept_newer += 1
            elif out.get("ok"):
                tomb_count += tomb
                live_count += not tomb
            elif "forgotten source" in error:
                forgotten += 1
            elif "in progress" in error or out.get("deferred"):
                return {"ok": False, "error": "hub snapshot refresh in progress"}
            else:
                return {"ok": False, "error": error or "episode write refused"}
            done.add(eid)

    vector_count = 0
    by_kind: dict[tuple[Any, Any], list[Any]] = {}
    for profile, kind, ref, _built in apply_groups:
        by_kind.setdefault((profile, kind), []).append(ref)
    for (profile, kind), refs in by_kind.items():
        for part in _chunks(refs):
            cur.execute(f"SELECT {emb_sel} FROM memory_embeddings WHERE profile = %s AND kind = %s "
                        "AND ref = ANY(%s)", (profile, kind, part))
            rows = [dict(zip(emb_names, r)) for r in cur.fetchall()]
            out = upsert_embeddings(rows, replace_refs=[(profile, kind, ref) for ref in part], read_at=read_at,
                                    anchors={int(r): vector_anchors.get(int(r), frozenset())
                                             for r in part if kind == "episode" and str(r).isdigit()})
            if not out.get("ok"):
                error = str(out.get("error") or "")
                return {"ok": False, "error": "hub snapshot refresh in progress" if "in progress" in error
                        else error or "vector write refused"}
            vector_count += len(rows)

    def newest(rows: Sequence[Mapping[str, Any]], col: str) -> tuple[datetime, int] | None:
        stamp = _utc(rows[-1].get(col)) if rows else None
        return None if stamp is None else (stamp, int(rows[-1]["id"]))

    last_fresh = _utc(fresh_stamps[-1][0]) if fresh_stamps else None

    parts = _write_pulled_tables(topic_rows, aliases, read_at=read_at,
                                topic_purges=topic_purges, purge_hub_at=hub_now)
    if ledger_payload is not None and not ledger_payload.get("ok"):
        parts["ledger"] = ledger_payload
    else:
        try:
            parts["ledger"] = _write_ledger_pull(ledger_payload, read_at=read_at)
        except Exception as exc:  # noqa: BLE001 - memory writes already succeeded
            parts["ledger"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    last_group = _utc(groups[-1][3]) if groups else None
    cursors = {
        "episodes": _cursor_after(after, None if last_fresh is None else (last_fresh, int(fresh_stamps[-1][1])),
                                  cap),
        "tombstones": _cursor_after((state["tombstones_after"], 0), newest(gone, "deleted_at"), cap),
        "vectors": _cursor_after((state["vectors_after"], 0),
                                 None if last_group is None else (last_group, 0), cap),
    }
    if (parts.get("topics") or {}).get("ok"):
        topic_newest = _newest_ts(r.get(c) for r in topic_rows or () for c in ("updated_at", "deleted_at"))
        cursors["topics"] = _cursor_after((state["topics_after"], 0),
                                          None if topic_newest is None else (_utc(topic_newest), 0), cap)
    return {
        "ok": True,
        "episodes": live_count, "tombstones": tomb_count, "vectors": vector_count,
        "forgotten_skipped": forgotten, "kept_newer": kept_newer,
        "missed_retried": len(set(state["missed"]) & (done | absent)),
        "cleared": set(state["missed"]) & (done | absent),
        "parts": parts,
        "cursors": cursors,
    }


def _write_pulled_tables(topic_rows: Sequence[Mapping[str, Any]] | None,
                         aliases: tuple[list[str], list[Any]] | None, *,
                         read_at: float, topic_purges: Iterable[str] = (),
                         purge_hub_at: datetime | None = None,
                         ) -> dict[str, dict[str, Any]]:
    """The pull's topic bodies and project aliases, under the refresh lock,
    in one transaction. Each part's result by name; a part the hub or this
    replica has no table for is ``not_applicable``. A topic row older than
    the one held (by ``updated_at``) never replaces it, a tombstone held
    stays, and a purged topic is written back only when its updated_at is
    newer than the purge marker. ``project_aliases`` (tiny) is copied whole
    on every pull, replacing the replica's rows in the same transaction, so
    a deleted alias arrives too, unless the copy held was read after
    ``read_at`` (a dump or pull that finished meanwhile). ``topic_purges``
    names held keys absent from the same hub transaction; a newer local
    topic publication or dump stays when that older absence is applied.
    ``purge_hub_at`` is the hub's clock in that transaction, recorded in
    each purge marker. The facts come with the copy (``copy_facts``). One
    fenced write (``_replica_write_gate``) for all of it."""
    parts: dict[str, dict[str, Any]] = {}
    path = snapshot_path()
    con = sqlite3.connect(str(path))
    try:
        if topic_rows is None or not {"slug", "updated_at"} <= _snapshot_table_columns(con, "topics"):
            parts["topics"] = {"ok": True, "not_applicable": True}
        if aliases is None or not _snapshot_table_columns(con, "project_aliases"):
            parts["project_aliases"] = {"ok": True, "not_applicable": True}
    finally:
        con.close()
    wanted = [name for name in ("topics", "project_aliases") if name not in parts]
    if not wanted:
        return parts
    recreated: dict[str, Any] = {}

    def clear_recreated() -> None:
        # Clear only the observed fence after the row commits, still under
        # the refresh lock.
        for slug, marker_version in recreated.items():
            _clear_purge_marker_version(path, slug, marker_version)

    try:
        with _replica_write_gate(path, on_commit=clear_recreated) as fence:
            con = fence.con
            if "topics" in wanted:
                topic_cols = _snapshot_table_columns(con, "topics")
                purged = _purged_topic_slugs(path)
                written = 0
                removed = 0
                for slug in sorted(topic_purges):
                    if _held_read_at(con, "topics", slug) > read_at:
                        continue
                    if not _mark_purged_topic(slug, path, hub_at=purge_hub_at):
                        raise OSError("could not record the remote topic purge")
                    purged.add(slug)
                    # Old replicas can lack the external-content delete
                    # trigger; clear their indexed terms explicitly.
                    fts = {r[0] for r in con.execute(
                        "SELECT name FROM sqlite_master WHERE name IN ('topics_fts', 'topics_fts_ad')")}
                    if "topics_fts" in fts and "topics_fts_ad" not in fts:
                        fields = ", ".join(_TOPIC_FTS_COLS)
                        con.execute(f"INSERT INTO topics_fts(topics_fts, rowid, {fields}) "
                                    f"SELECT 'delete', rowid, {fields} FROM topics WHERE slug = ?", (slug,))
                    if {"kind", "ref"} <= _snapshot_table_columns(con, "memory_embeddings"):
                        con.execute("DELETE FROM memory_embeddings WHERE kind = 'topic' AND ref = ?", (slug,))
                    if "slug" in _snapshot_table_columns(con, "topic_revisions"):
                        con.execute("DELETE FROM topic_revisions WHERE slug = ?", (slug,))
                    removed += con.execute("DELETE FROM topics WHERE slug = ?", (slug,)).rowcount
                    _record_read_at(con, "topics", slug, read_at)
                for row in topic_rows or ():
                    row = _topic_routing_metadata(row)
                    slug = row.get("slug")
                    if slug is None:
                        continue
                    marker_version = None
                    if slug in purged:
                        incoming = _utc(row.get("updated_at"))
                        # A marker a concurrent dump just cleared is no marker.
                        marker = _purge_marker_version(path, slug)
                        if marker is not None:
                            marker_stamp, marker_version = marker
                            if incoming is None or incoming <= marker_stamp:
                                continue
                    old = con.execute("SELECT updated_at, deleted_at FROM topics WHERE slug = ?", (slug,)).fetchone()
                    if old is not None and row.get("deleted_at") is None:
                        held, incoming = _utc(old[0]), _utc(row.get("updated_at"))
                        if old[1] is not None or (held and incoming and held > incoming):
                            continue
                    cols = [c for c in _TOPIC_COLS if c in topic_cols and c in row]
                    vals = [_json_text(row[c]) if c in ("links", "frontmatter") else
                            _ts_text(row[c]) if c.endswith("_at") else row[c] for c in cols]
                    con.execute(
                        f"INSERT INTO topics ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
                        "ON CONFLICT(slug) DO UPDATE SET "
                        + ", ".join(f"{c}=excluded.{c}" for c in cols if c != "slug"),
                        vals,
                    )
                    _record_read_at(con, "topics", slug, read_at)
                    if slug in purged and marker_version is not None:
                        recreated[slug] = marker_version
                    written += 1
                if written or removed:
                    _advance_cursor(con, "topics", synced_at=_utcnow_iso(),
                                    newest=_newest_ts(r.get("updated_at") for r in topic_rows or ()))
                parts["topics"] = {"ok": True, "topics": written, "purged": removed}
            if "project_aliases" in wanted:
                cols, rows = aliases  # type: ignore[misc]
                if _held_read_at(con, "project_aliases", "*") > read_at:
                    parts["project_aliases"] = {"ok": True, "project_aliases": 0,
                                                "skipped": "the replica holds a newer copy"}
                elif _alias_rows_held(con, cols) == [tuple(_alias_value(c, v) for c, v in zip(cols, r))
                                                     for r in rows]:
                    # Unchanged: an unchanged pull writes nothing.
                    parts["project_aliases"] = {"ok": True, "project_aliases": 0}
                else:
                    con.execute("DELETE FROM project_aliases")
                    _write_project_alias_rows(con, cols, rows)
                    parts["project_aliases"] = {"ok": True, "project_aliases": len(rows)}
                    _record_read_at(con, "project_aliases", "*", read_at)
    except _ReplicaBusy:
        _record_write_error(wanted, "hub snapshot refresh in progress")
        return {**parts, **{n: {"ok": False, "error": "hub snapshot refresh in progress"} for n in wanted}}
    except Exception as exc:  # noqa: BLE001 - reported as the failed parts
        error = f"{type(exc).__name__}: {exc}"[:300]
        _record_write_error(wanted, error)
        return {**{n: p for n, p in parts.items() if n not in wanted},
                **{n: {"ok": False, "error": error} for n in wanted}}
    return parts


def pull_changes(*, limit: int = PULL_LIMIT, include_ledger: bool = True) -> dict[str, Any]:
    """Bring the replica level with the hub without a full dump: episodes
    changed (by ``changed_at``, or ``ingested_at`` on a hub or row without
    it) or tombstoned since this replica's cursors, the episodes whose
    patch was missed (``_queue_missed_patch``), re-built vectors and topic
    bodies changed since its cursor; then, outside every lock, one fact copy
    (``copy_facts``, design section 6.2). Every episode goes through
    ``upsert_episode``, so the forgotten-source fence, queued erasures,
    tombstones and the freshness rule apply as for a capture. ``ok`` covers
    the memory parts; the fact copy (``facts``) and the ledger are returned
    separately: a copy that fails keeps the last good copy, or none shown.
    A failed part is named in ``failed``. Heal can defer the ledger read with
    ``include_ledger=False`` while a failed ledger refresh is in backoff.

    The cursors live in the sidecar beside ``last_pull_at``; a full dump
    replaces the sidecar and so resets them to its own hub time. Each moves to
    the newest row read but never past the hub's now minus
    ``FRESHNESS_OVERLAP_S``: a stamp is taken inside a transaction, so a long
    one can commit an older stamp after a newer one was read, and the
    overlap re-reads those minutes (the upserts are idempotent). More than
    ``limit`` memory changes of a kind writes nothing and asks for a full dump.
    Ledger overflow asks for a ledger refresh without stopping memory; only
    memory parts determine the memory caught-up stamp. Never raises."""
    from datetime import timedelta

    path = snapshot_path()
    m = meta()
    dumped = _utc(m.get("hub_dumped_at"))
    has_meta = False
    if path.is_file():
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                has_meta = bool(_snapshot_table_columns(con, "snapshot_meta"))
            finally:
                con.close()
        except sqlite3.Error:
            has_meta = False
    if dumped is None or not has_meta:
        return {"ok": False, "needs_dump": True, "error": "no full dump on record"}
    side = _read_sidecar()
    floor = dumped - timedelta(seconds=FRESHNESS_OVERLAP_S)
    mine = side.get("pull_dump") == m.get("hub_dumped_at")

    def stored(key: str) -> datetime:
        return (_utc(side.get(key)) if mine else None) or floor

    try:
        after_id = int(side.get("pull_episodes_after_id") or 0) if mine else 0
    except (TypeError, ValueError):
        after_id = 0
    state = {
        "episodes_after": stored("pull_episodes_after"), "episodes_after_id": after_id,
        "tombstones_after": stored("pull_tombstones_after"), "vectors_after": stored("pull_vectors_after"),
        "topics_after": stored("pull_topics_after"),
        "missed": _missed_patch_ids(path),
    }
    started = time.time()
    pg = None
    try:
        pg = try_hub_connect(connect_timeout=HUB_CONNECT_TIMEOUT_S)
        with pg.cursor() as cur:
            result = _pull_apply(cur, state, limit, include_ledger=include_ledger)
        # The read-only transaction stays open: the copy's first exchange
        # ends it (``_COPY_CHECK_SQL``), or closing the session does.
    except Exception as exc:  # noqa: BLE001 - reported to the caller, which decides what to do next
        _close_quietly(pg)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300],
                "hub_unreachable": hub_connection_failed(exc)}
    try:
        return _pull_finish(pg, result, m, path, started)
    finally:
        _close_quietly(pg)


def _close_quietly(conn) -> None:
    try:
        if conn is not None:
            conn.close()
    except Exception:  # noqa: BLE001 - a closed or broken session is closed
        pass


def _pull_finish(pg, result: dict[str, Any], m: Mapping[str, Any], path: Path, started: float) -> dict[str, Any]:
    """The local half of ``pull_changes`` after its hub read: cursors, the
    missed patches, then the fact copy on the pull's own hub session."""
    if not result.get("ok"):
        return result
    parts = dict(result["parts"])
    # Ledger repair has its own retry clock; only a memory failure asks the
    # caller to rebuild the complete copy.
    failed = [name for name, part in parts.items() if not part.get("ok") and not part.get("not_applicable")]
    if "ledger" in failed:
        parts["ledger"]["needs_refresh"] = True
    try:
        side = _read_sidecar() or meta()
        for name, (at, at_id) in result["cursors"].items():
            side[f"pull_{name}_after"] = at.isoformat()
            if name == "episodes":
                side["pull_episodes_after_id"] = at_id
        side["pull_dump"] = m.get("hub_dumped_at")
        side["last_pull_at"] = _utcnow_iso()
        if not any(name != "ledger" for name in failed):
            # Ledger freshness lives in SQLite, independently of memory.
            # The start, not the end: a change committed while the pull ran
            # may not have been read.
            side[PULL_CAUGHT_UP_KEY] = datetime.fromtimestamp(started, timezone.utc).isoformat()
        _write_meta(side)
    except OSError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    _clear_missed_patches(result["cleared"], path=path, written_before=started)
    facts = copy_facts(session=pg)
    out: dict[str, Any] = {
        "ok": not any(name != "ledger" for name in failed),
        **{k: result[k] for k in ("episodes", "tombstones", "vectors", "missed_retried",
                                  "forgotten_skipped", "kept_newer")},
        "topics": parts["topics"].get("topics", 0), "commitments": facts.get("commitments", 0),
        "project_aliases": parts["project_aliases"].get("project_aliases", 0),
        "facts": facts, "ledger": parts["ledger"],
    }
    if failed:
        out["failed"] = failed
        out["error"] = "; ".join(f"{name}: {parts[name].get('error') or 'failed'}" for name in failed)[:300]
        if any(parts[name].get("hub_unreachable") for name in failed if name != "ledger"):
            out["hub_unreachable"] = True
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
        # A new file has no stamp, so it shows no fact row until a copy
        # (copy_facts) writes both; a dump's or an import's new file is
        # invalidated too (_invalidate_facts).
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
    if version >= 6:
        con.executescript(
            """
            CREATE TABLE commitments (
                id INTEGER PRIMARY KEY,
                text TEXT,
                project TEXT,
                owner TEXT,
                kind TEXT,
                opened_episode INTEGER,
                opened_at TEXT,
                due_after TEXT,
                status TEXT,
                closed_episode INTEGER,
                closed_at TEXT,
                close_reason TEXT,
                last_seen_at TEXT,
                seen_count INTEGER,
                future_trigger INTEGER
            );
            CREATE INDEX idx_snapshot_commitments_project_status ON commitments (project, status);
            """
        )
    if version >= 7:
        _add_commitment_lineage_cols(con)
    if version >= 9:
        _add_commitment_close_cols(con)
    if version >= 8:
        con.executescript(
            """
            CREATE TABLE project_aliases (
                alias TEXT PRIMARY KEY,
                project TEXT NOT NULL,
                created_at TEXT,
                source TEXT,
                evidence TEXT
            );
            CREATE INDEX idx_snapshot_project_aliases_project ON project_aliases (project);
            """
        )
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
CREATE TABLE IF NOT EXISTS ledger_receipts (
    op_id TEXT PRIMARY KEY, project TEXT, ok INTEGER
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


def _create_ledger_schema(con: sqlite3.Connection) -> None:
    # executescript commits an open transaction before running its statements.
    # Ledger-only rebuilds must keep schema repairs and data in one transaction.
    for statement in _LEDGER_SCHEMA_SQL.split(";"):
        if statement.strip():
            con.execute(statement)


def _queued_op_ids(project: str | None = None, row_ids: Sequence[str] | None = None) -> list[str]:
    """The op ids of this Mac's waiting ledger writes (of ``project`` and
    ``row_ids`` when given): the receipts worth copying."""
    from khipu import ledger

    wanted = set(row_ids) if row_ids is not None else None
    return [op["op_id"] for op in ledger.queued_ops() if isinstance(op.get("op_id"), str)
            and (project is None or op.get("project") == project)
            and (wanted is None or op.get("row_id") in wanted)]


def _read_ledger_receipts(cur, op_ids: Sequence[str]) -> list[tuple[str, str, int]]:
    """Which of ``op_ids`` the hub has bound (``ledger_ops``), read in the
    same transaction as the rows they touched: an op the hub holds is in
    those rows already (or was refused), so a reader never applies it again
    on top of them. A receipt never changes once written."""
    if not op_ids:
        return []
    cur.execute("SELECT op_id, project, (result->>'ok') IS DISTINCT FROM 'false' FROM ledger_ops "
                "WHERE op_id = ANY(%s) AND result <> '{}'::jsonb", (list(op_ids),))
    return [(op_id, project, int(bool(ok))) for op_id, project, ok in cur.fetchall()]


def _write_ledger_receipts(con: sqlite3.Connection, receipts: Sequence[Sequence[Any]] | None) -> None:
    if receipts:
        con.executemany("INSERT OR REPLACE INTO ledger_receipts VALUES (?, ?, ?)", [tuple(r) for r in receipts])


def refresh_ledger() -> dict[str, Any]:
    """Rebuild only the ledger under the replica writer lock. All hub reads
    share one repeatable-read snapshot; data, removals and freshness publish
    in one SQLite transaction. Memory tables and their cursors stay untouched."""
    lock = _acquire_refresh_lock()
    if lock is None:
        return {"ok": False, "needs_refresh": True, "error": "hub snapshot refresh in progress"}
    try:
        path = snapshot_path()
        if not path.is_file():
            return {"ok": False, "needs_refresh": True, "error": "hub snapshot missing"}
        with try_hub_connect(connect_timeout=REFRESH_CONNECT_TIMEOUT_S) as pg:
            from psycopg import IsolationLevel

            pg.isolation_level = IsolationLevel.REPEATABLE_READ
            read_at = time.time()
            with pg.cursor() as cur:
                if "row_id" not in _pg_columns(cur, "ledger_rows"):
                    return {"ok": True, "not_applicable": True}
                with closing(sqlite3.connect(str(path))) as con, con:
                    con.execute("BEGIN IMMEDIATE")
                    _create_ledger_schema(con)
                    for table in ("ledger_projects", "ledger_vocab", "ledger_rows", "ledger_history", "ledger_gone"):
                        con.execute(f"DELETE FROM {table}")
                    count = _insert_ledger(cur, con)
                    _record_read_at(con, "ledger", "*", read_at)
                    con.execute("INSERT OR REPLACE INTO snapshot_meta(key,value) VALUES ('ledger_caught_up_at', ?)",
                                (datetime.fromtimestamp(read_at, timezone.utc).isoformat(),))
        return {"ok": True, "rows": count}
    except Exception as exc:  # noqa: BLE001 - reported separately from memory health
        return {"ok": False, "needs_refresh": True, "error": f"{type(exc).__name__}: {exc}"[:300]}
    finally:
        _release_refresh_lock(lock)


def _insert_ledger(cur, con: sqlite3.Connection) -> int:
    """The ledger's live rows (never a removed one), their history, and each
    project's words. Empty (0, no error) on a hub without migration 0028."""
    if "row_id" not in _pg_columns(cur, "ledger_rows"):
        return 0
    _create_ledger_schema(con)
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
    _write_ledger_receipts(con, _read_ledger_receipts(cur, _queued_op_ids()))
    return len(rows)


def _write_ledger_project(con: sqlite3.Connection, project: str, proj: Sequence[Any] | None,
                          vocab: Mapping[str, list] | None, rows: Sequence[Mapping[str, Any]],
                          history: Sequence[Mapping[str, Any]], gone: Mapping[str, int], *,
                          read_at: float) -> dict[str, Any]:
    """Apply one project on the caller's locked SQLite transaction."""
    live = {r["row_id"]: r for r in rows}

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

    metadata_at = max(_held_read_at(con, "ledger_projects", project), _held_read_at(con, "ledger", "*"))
    if metadata_at <= read_at and (proj is not None or vocab is not None):
        if proj is not None:
            value = (proj[0], int(bool(proj[1])), _ts_text(proj[2]))
            old = con.execute("SELECT name, hidden, created_at FROM ledger_projects WHERE project = ?",
                              (project,)).fetchone()
            if old != value:
                con.execute("INSERT OR REPLACE INTO ledger_projects VALUES (?, ?, ?, ?)", (project, *value))
        if vocab is not None:
            words = [("state", w, m, i) for i, (w, m) in enumerate(vocab.get("state") or [])]
            words += [("kind", w, "", i) for i, w in enumerate(vocab.get("kind") or [])]
            old_words = con.execute("SELECT field, word, meaning, position FROM ledger_vocab WHERE project = ? "
                                    "ORDER BY field, position", (project,)).fetchall()
            if old_words != sorted(words, key=lambda w: (w[0], w[3])):
                con.execute("DELETE FROM ledger_vocab WHERE project = ?", (project,))
                con.executemany("INSERT INTO ledger_vocab VALUES (?, ?, ?, ?, ?)",
                                [(project, *w) for w in words])
        if metadata_at < read_at:
            _record_read_at(con, "ledger_projects", project, read_at)
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
    return {"ok": True, "rows": len(written), "stale": stale}


def ledger_write_snapshot(project: str, proj: Sequence[Any] | None, vocab: Mapping[str, list],
                          rows: Sequence[Mapping[str, Any]], history: Sequence[Mapping[str, Any]],
                          row_ids: Sequence[str], gone: Mapping[str, int] | None = None, *,
                          _pull: Mapping[str, Any] | None = None, read_at: float | None = None,
                          receipts: Sequence[Sequence[Any]] | None = None) -> dict[str, Any]:
    """Write ``row_ids`` of ``project`` through to the replica: each live one
    in ``rows`` (with its whole ``history``), and each removed one in ``gone``
    at the version it was removed at (``LEDGER_PURGED_VERSION`` for a purge
    the hub recorded). Any other id is left as it is: a row missing from the
    hub may be a write not committed yet, so absence is never a purge. A row and its
    history are replaced whole, and only by a newer or equal revision: its
    version, then its last history id (a late conditional write adds history
    without a new version). A removed or purged id leaves a marker, so a writer
    that read the hub before a newer change and lands after it changes
    nothing. Under the refresh lock, like every incremental write here.
    The incremental pull passes its complete ledger part through this same
    transaction owner. Fail-open."""
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
    try:
        con = sqlite3.connect(str(path))
        try:
            con.executescript(_LEDGER_SCHEMA_SQL)

            at = time.time() if read_at is None else read_at
            result = (_apply_ledger_pull(con, _pull, read_at=at) if _pull is not None else
                      _write_ledger_project(con, project, proj, vocab, rows, history, gone, read_at=at))
            _write_ledger_receipts(con, receipts)
            if con.in_transaction:
                con.commit()
        finally:
            con.close()
        return result
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


def ledger_gone_version(project: str, row_id: str) -> int | None:
    """The replica's gone marker version for one row; None when the replica,
    its marker table or the marker is missing. Unlike the readers above it
    raises when the replica is there but cannot be read: a caller deciding
    whether a row was purged must not take an unreadable copy for "no"."""
    try:
        os.stat(snapshot_path())
    except FileNotFoundError:
        return None
    con = sqlite3.connect(f"file:{snapshot_path()}?mode=ro", uri=True)
    try:
        if con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ledger_gone'").fetchone() is None:
            return None
        got = con.execute("SELECT version FROM ledger_gone WHERE project = ? AND row_id = ?",
                          (project, row_id)).fetchone()
    finally:
        con.close()
    return int(got[0]) if got else None


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
        con.execute("BEGIN")
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
        rows = [r for r in rows if r["row_id"] not in gone or gone[r["row_id"]] < int(r["version"] or 0)]
        live = {r["row_id"] for r in rows}
        history = [h for h in history if h["row_id"] in live]
        receipts: dict[str, bool] = {}
        if _snapshot_table_columns(con, "ledger_receipts"):
            receipts = {op_id: bool(ok) for op_id, ok in con.execute(
                "SELECT op_id, ok FROM ledger_receipts WHERE project = ?", (project,))}
        image_meta = dict(con.execute("SELECT key, value FROM snapshot_meta")) if (
            _snapshot_table_columns(con, "snapshot_meta")) else {}
    except (sqlite3.Error, ValueError):
        return None
    finally:
        con.close()
    return {"vocab": vocab, "rows": rows, "history": history, "gone": gone, "receipts": receipts,
            "as_of": caught_up_at(image_meta, ledger=True)}


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
        prepared = _topic_routing_metadata(dict(zip(cols, row, strict=True)))
        vals = []
        for col in cols:
            val = prepared[col]
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
    # Fact vectors come with the fact copy (copy_facts), never with a dump.
    kinds = ", ".join(f"'{k}'" for k in _FACT_VECTOR_KINDS)
    cur.execute(f"SELECT {sel} FROM memory_embeddings WHERE kind NOT IN ({kinds})")
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


def _hub_project_aliases(cur) -> tuple[list[str], list[Any]] | None:
    """The hub's whole ``project_aliases`` table, or ``None`` on a hub with none;
    ``source``/``evidence`` come only from a hub at migration 0039."""
    hub_cols = _pg_columns(cur, "project_aliases")
    if not {"alias", "project"} <= hub_cols:
        return None
    cols = [c for c in ("alias", "project", "created_at", "source", "evidence") if c in hub_cols]
    cur.execute(f"SELECT {', '.join(cols)} FROM project_aliases ORDER BY alias")
    return cols, cur.fetchall()


def _alias_value(col: str, val: Any) -> Any:
    return _json_text(val) if col == "evidence" else _ts_text(val) if col == "created_at" else val


def _alias_rows_held(con: sqlite3.Connection, cols: Sequence[str]) -> list[tuple] | None:
    """The replica's alias rows in the hub read's column order and order."""
    if not set(cols) <= _snapshot_table_columns(con, "project_aliases"):
        return None
    return [tuple(r) for r in con.execute(f"SELECT {', '.join(cols)} FROM project_aliases ORDER BY alias")]


def _write_project_alias_rows(con: sqlite3.Connection, cols: list[str], rows: list[Any]) -> int:
    for row in rows:
        vals = [_alias_value(c, v) for c, v in zip(cols, row, strict=True)]
        con.execute(f"INSERT INTO project_aliases ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
    return len(rows)


def _insert_project_aliases(cur, con: sqlite3.Connection) -> int:
    """Empty (returns 0, no error) on a hub with no ``project_aliases`` table."""
    got = _hub_project_aliases(cur)
    return _write_project_alias_rows(con, *got) if got else 0


def snapshot_project_group(con: sqlite3.Connection, project: str | None) -> tuple[str | None, list[str]]:
    """The replica's ``identity.project_canonical_group``: ``(canonical, group)``
    with the canonical first, ``(project, [project])`` when it has no aliases,
    the table is missing (a replica older than version 8) or the read fails."""
    if not project:
        return project, [project] if project else []
    try:
        row = con.execute("SELECT project FROM project_aliases WHERE alias = ?", (project,)).fetchone()
        canonical = str(row[0]) if row and row[0] else project
        rest = [str(r[0]) for r in con.execute(
            "SELECT alias FROM project_aliases WHERE project = ? ORDER BY alias", (canonical,)).fetchall()]
        return canonical, [canonical] + [a for a in rest if a != canonical]
    except Exception:  # noqa: BLE001 — grouping is best-effort, like the hub's
        return project, [project]


def snapshot_resolve_project(con: sqlite3.Connection, project: str | None) -> str | None:
    """The replica's ``identity.resolve_project_name``: a short or
    differently-cased name read as the stored project it means; an ambiguous
    one raises ``identity.AmbiguousProject``. Unchanged when the replica
    cannot say."""
    return snapshot_resolve_project_known(con, project)[0]


def snapshot_resolve_project_known(con: sqlite3.Connection, project: str | None) -> tuple[str | None, bool]:
    """``snapshot_resolve_project`` and whether the answer is a visible
    project (``identity.resolve_project``'s contract)."""
    if not project:
        return project, True
    from khipu import identity

    found: list[list[tuple]] = []
    # Names of commitments opened by a forgotten episode, or one whose erasure
    # is queued, never resolve a name: the replica's forget fence, like every
    # other replica read of them. An alias counts only for a group one of the
    # fenced names belongs to.
    for sql in ("SELECT DISTINCT project FROM commitments WHERE project IS NOT NULL"
                + forget_fence_sql(con, "commitments", "opened_episode"),
                "SELECT alias, project FROM project_aliases"):
        try:
            found.append(list(con.execute(sql).fetchall()))
        except sqlite3.Error:
            found.append([])
    known = identity.visible_project_names((r[0] for r in found[0]), found[1])
    return identity.pick_project_known(project, known, lambda n: snapshot_project_group(con, n)[0] or n)


def _hub_commitments(cur) -> tuple[list[str], list[Any]] | None:
    """``(columns, rows)`` of every hub commitment not opened by a forgotten
    episode, or ``None`` on a hub with no ``commitments`` table. The dump
    and the pull both read them here: a commitment has no change stamp, so
    a pull takes the whole table, as a dump does."""
    hub_cols = _pg_columns(cur, "commitments")
    if not {"id", "text", "status"} <= hub_cols:
        return None
    cols = [c for c in _COMMITMENT_COLS if c in hub_cols]
    from khipu.commitments import close_reason_sql, settle_suggestion_sql

    # A suggestion or close reason quoting a forgotten episode's evidence never reaches the replica.
    sel = ", ".join(settle_suggestion_sql(cur) if c == "settle_suggestion"
                    else close_reason_sql(cur) if c == "close_reason" else c for c in cols)
    where = ""
    if "opened_episode" in cols and "deleted_at" in _pg_columns(cur, "episodes"):
        where = (
            " WHERE NOT EXISTS (SELECT 1 FROM episodes e WHERE e.id = commitments.opened_episode "
            "AND e.deleted_at IS NOT NULL)"
        )
    cur.execute(f"SELECT {sel} FROM commitments{where} ORDER BY id")
    return cols, list(cur.fetchall())


def _add_commitment_lineage_cols(con: sqlite3.Connection) -> None:
    """Give a replica's ``commitments`` table the version 7 columns it lacks."""
    have = _snapshot_table_columns(con, "commitments")
    if not have:
        return
    for col, kind in _COMMITMENT_LINEAGE_COLS:
        if col not in have:
            con.execute(f"ALTER TABLE commitments ADD COLUMN {col} {kind}")


def _add_commitment_close_cols(con: sqlite3.Connection) -> None:
    """Upgrade older replicas before writing close-rule data on a pull."""
    have = _snapshot_table_columns(con, "commitments")
    if not have:
        return
    for col, kind in _COMMITMENT_CLOSE_COLS:
        if col not in have:
            con.execute(f"ALTER TABLE commitments ADD COLUMN {col} {kind}")


def _write_commitment_rows(con: sqlite3.Connection, cols: Sequence[str], rows: Sequence[Any]) -> int:
    """Insert hub commitment rows (``cols`` order) into ``con``, for a fact
    copy (``_write_copy``). A replica of an older version first gains the
    owner's lineage and close-rule columns, so its owners and owed filters
    match the hub's. Other missing columns are left out. Caller clears the
    table first and commits. Rows opened by an episode this replica holds
    tombstoned or queued for erasure are left out."""
    _add_commitment_lineage_cols(con)
    _add_commitment_close_cols(con)
    have = _snapshot_table_columns(con, "commitments")
    keep = [i for i, c in enumerate(cols) if c in have]
    sel = ", ".join(cols[i] for i in keep)
    ts_cols = {"opened_at", "due_after", "closed_at", "last_seen_at", "owner_reassigned_at"}
    rejected = _rejected_episode_ids(con)
    names = list(cols)
    written = 0
    for raw_row in rows:
        row = dict(zip(names, raw_row))
        admitted = None if row.get("opened_episode") in rejected else row
        if admitted is None:
            continue
        vals = []
        for i in keep:
            col = cols[i]
            val = admitted.get(col)
            if col in ("linked_ids", "prs"):
                val = _json_list_value(val)
                vals.append(json.dumps([int(v) for v in val]) if val is not None else None)
                continue
            if col in ("settle_suggestion", "pr_check"):
                vals.append(json.dumps(val) if val is not None and not isinstance(val, str) else val)
                continue
            vals.append(_ts_text(val) if col in ts_cols else (int(bool(val)) if col == "future_trigger" else val))
        con.execute(
            f"INSERT INTO commitments ({sel}) VALUES ({', '.join('?' * len(keep))})",
            vals,
        )
        written += 1
    return written


class DumpDeadline(TimeoutError):
    """A full dump's hub reads did not finish by their deadline."""


@contextmanager
def _dump_deadline(pg: Any, seconds: float):
    """Bound a full dump's hub reads as a whole: at ``seconds`` a timer
    abandons the session (``provenance._abandon``: the cancel sent without
    waiting, the socket shut down), so a read the hub stopped answering
    fails at once and the dump unwinds, releasing the refresh lock. The
    dump's error then reads as :class:`DumpDeadline`."""
    from khipu.provenance import _abandon

    expired = threading.Event()

    def fire() -> None:
        expired.set()
        _abandon(pg)

    timer = threading.Timer(seconds, fire)
    timer.daemon = True
    timer.start()
    message = f"the hub dump did not finish within {seconds:.0f} s; its session was abandoned"
    try:
        yield
    except Exception as exc:
        if expired.is_set():
            raise DumpDeadline(message) from exc
        raise
    finally:
        timer.cancel()
    if expired.is_set():
        raise DumpDeadline(message)


def refresh() -> dict[str, Any]:
    """Dump hub tables into a fresh sqlite file and atomically replace the snapshot.

    Holds a cross-process flock for the whole dump so doctor ``maybe_refresh``
    and ``khipu snapshot refresh`` serialize. If another dump is live, returns
    ``{"ok": False, "error": ...}`` instead of dumping anyway.

    No memory lock is held over the hub reads (``_dump_hub``, bounded as a
    whole by ``REFRESH_DEADLINE_S``), so a stalled hub never holds up a
    forget. The publication takes it: the source fence's ``write_gate`` holds
    the memory lock from the queued erasures and the registry scrub through
    the replace, so anything forgotten while the dump read is dropped from the
    new file before it becomes the replica.

    The dump carries no fact row: after it is published, and outside every
    lock, one fact copy (``copy_facts``) fills them (``facts``).
    """
    from khipu import memory_lock

    try:
        out = _refresh_locked()
    except memory_lock.MemoryLockError as exc:
        return {"ok": False, "error": str(exc)}
    if out.get("ok"):
        out["facts"] = copy_facts()
    return out


def _refresh_locked() -> dict[str, Any]:
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
        started_at = _utcnow_iso()
        # Markers older than this are reflected in the dump that follows.
        dump_started = time.time()
        tmp_dir = dest.parent
        _sweep_stale_temps_locked(tmp_dir)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".hub_snapshot.", suffix=".sqlite", dir=tmp_dir
        )
        os.close(fd)
        tmp_path = Path(tmp_name)
        counts: dict[str, int] = {}
        try:
            con = sqlite3.connect(str(tmp_path))
            _create_schema(con)
            # Every statement ends on the server within the dump's deadline,
            # and the deadline itself ends the whole read (_dump_deadline).
            with try_hub_connect(connect_timeout=REFRESH_CONNECT_TIMEOUT_S,
                                 statement_timeout_s=REFRESH_DEADLINE_S) as pg, \
                    _dump_deadline(pg, REFRESH_DEADLINE_S):
                from psycopg import IsolationLevel

                pg.isolation_level = IsolationLevel.REPEATABLE_READ
                with pg.cursor() as cur:
                    # The hub's own clock at the start of the dump, before any
                    # table is read: freshness takes every cutoff and floor
                    # from this, never from this Mac's clock.
                    cur.execute("SELECT now()")
                    row = cur.fetchone()
                    hub_dumped_at = _ts_text(_utc(row[0] if row else None))
                    counts["episodes"] = _insert_episodes(cur, con)
                    counts["topics"] = _insert_topics(cur, con)
                    counts["topic_revisions"] = _insert_topic_revisions(cur, con)
                    counts["nodes"] = _insert_nodes(cur, con)
                    counts["edges"] = _insert_edges(cur, con)
                    counts["embedding_profiles"] = _insert_profiles(cur, con)
                    counts["memory_embeddings"] = _insert_memory_embeddings(
                        cur, con
                    )
                    counts["project_aliases"] = _insert_project_aliases(cur, con)
                    counts["ledger_rows"] = _insert_ledger(cur, con)
            refreshed_at = _utcnow_iso()
            if _snapshot_table_columns(con, "snapshot_meta"):
                con.execute("INSERT OR REPLACE INTO snapshot_meta (key, value) VALUES ('refreshed_at', ?)", (refreshed_at,))
                if hub_dumped_at:
                    con.execute("INSERT OR REPLACE INTO snapshot_meta (key, value) VALUES ('hub_dumped_at', ?)",
                                (hub_dumped_at,))
                # The ledger is as fresh as the dump's start: its one
                # repeatable-read snapshot was taken after this, and a change
                # committed while the dump ran is not in it.
                if counts.get("ledger_rows") is not None:
                    con.execute("INSERT OR REPLACE INTO snapshot_meta (key, value) VALUES ('ledger_caught_up_at', ?)",
                                (started_at,))
                # Every row here was read after this: an older pull read
                # never replaces one (the freshness rule, _held_read_at).
                con.execute("INSERT OR REPLACE INTO snapshot_meta (key, value) VALUES (?, ?)",
                            (_ROWS_READ_FLOOR_KEY, repr(dump_started)))
            _apply_pending_erasures(con, dest)
            _dump_cursors(con, refreshed_at)
            from khipu.tombstones import RegistryError

            try:
                # The source fence, held from the scrub through the replace:
                # a forget publishes under this lease before it erases the
                # replica, so the dump cannot land a source forgotten after
                # the scrub read the registry. The new file holds no fact
                # row and no stamp, and takes the next invalidation revision,
                # so no copy read before it publishes into it (6.2); the
                # fact copy after the dump fills it (refresh).
                with _replica_write_gate(dest, con=con, refresh_lock_held=True, scrub=True,
                                         on_commit=lambda: os.replace(tmp_path, dest)) as fence:
                    _invalidate_facts(con, revision=_file_revision(dest))
                con.close()
                counts["forgotten_scrubbed"] = fence.scrubbed
            except RegistryError as exc:
                con.close()
                remove_replica_temp(tmp_path)
                return {"ok": False, "error": f"tombstone registry unavailable: {exc}"}
            _clear_purged_topics(dest, dumped_after=dump_started)
            _clear_missed_patches(path=dest, written_before=dump_started)
            size_bytes = dest.stat().st_size
            new_meta: dict[str, Any] = {"schema_version": SNAPSHOT_SCHEMA_VERSION, "refreshed_at": refreshed_at, "size_bytes": size_bytes, "counts": counts}
            if hub_dumped_at:
                new_meta["hub_dumped_at"] = hub_dumped_at
            # Read again now, not at the start: anything noted while the dump
            # ran must survive it. The decision-sync cursor is not evidence
            # about this dump, so a dump must not reset it: with the keys gone
            # the next sync starts from the oldest decision and replays months
            # of history 500 rows per drain (seen live 2026-10-11, cursor a
            # month behind). A write error noted before the dump started is
            # repaired by it (the dump read the hub after that write was
            # committed there); one noted during the dump may not be.
            previous_meta = _read_sidecar()
            for key in _SYNC_CURSOR_KEYS:
                if key in previous_meta:
                    new_meta[key] = previous_meta[key]
            errors = previous_meta.get("write_errors")
            if isinstance(errors, dict):
                kept = {t: e for t, e in errors.items()
                        if isinstance(e, dict) and _ts_not_before(e.get("at"), started_at)}
                if kept:
                    new_meta["write_errors"] = kept
            _write_meta(new_meta)
            return {
                "ok": True,
                "path": str(dest),
                "refreshed_at": refreshed_at,
                "hub_dumped_at": hub_dumped_at,
                "size_bytes": size_bytes,
                "counts": counts,
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
            }
        except BaseException as exc:
            # A signal or an interrupt too: the temp and its journal never
            # outlive the dump that made them.
            try:
                con.close()
            except Exception:  # noqa: BLE001 — not opened yet, or already closed
                pass
            remove_replica_temp(tmp_path)
            if not isinstance(exc, Exception):
                raise
            try:
                from khipu.storage import describe_os_error

                side = _read_sidecar()
                side["refresh_error"] = {"at": _utcnow_iso(), "error": describe_os_error(exc)[:300]}
                _write_meta(side)
            except Exception:  # noqa: BLE001 — the dump's own error is what the caller sees
                pass
            raise
    finally:
        _release_refresh_lock(lock)


def _ts_not_before(val: Any, ref: Any) -> bool:
    a, b = _utc(val), _utc(ref)
    return a is not None and b is not None and a >= b


# A dump or a staged import that died (killed, crashed, power lost) leaves
# its ``.hub_snapshot.<random>.sqlite`` and journal beside the replica, and
# no reader ever opens them again. One idle this long is a leftover: a live
# dump or a staged import writes its file continuously, and a staged import
# publishes within seconds of finishing it.
STALE_TEMP_AGE_S = 10 * 60
_TEMP_SUFFIXES = ("", "-journal", "-wal", "-shm")


def is_replica_temp(name: str) -> bool:
    """A temporary replica file or one of its sidecars (journal, WAL, shared
    memory); never the replica itself or the meta temp files."""
    if not name.startswith(".hub_snapshot.") or name.startswith(".hub_snapshot.meta."):
        return False
    return any(name.endswith(".sqlite" + suffix) for suffix in _TEMP_SUFFIXES)


def remove_replica_temp(path: Path) -> None:
    """Remove a temporary replica file and every sidecar sqlite left with it."""
    for suffix in _TEMP_SUFFIXES:
        Path(str(path) + suffix).unlink(missing_ok=True)


def stale_replica_temps(directory: Path, *, now: float | None = None) -> list[Path]:
    """Leftover temporary replica files in ``directory``. The caller holds the
    refresh lock, so no dump is writing one; a young file may still be a staged
    import and is left alone."""
    now = time.time() if now is None else now
    out = []
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return []
    for entry in entries:
        if not is_replica_temp(entry.name):
            continue
        try:
            if entry.is_file(follow_symlinks=False) and now - entry.stat().st_mtime >= STALE_TEMP_AGE_S:
                out.append(Path(entry.path))
        except OSError:
            continue
    return out


def _sweep_stale_temps_locked(directory: Path) -> list[str]:
    removed = []
    for path in stale_replica_temps(directory):
        try:
            path.unlink(missing_ok=True)
            removed.append(path.name)
        except OSError:
            continue
    return removed


def sweep_stale_temps() -> dict[str, Any]:
    """Remove leftover temporary replica files under the refresh lock; a dump
    in progress means nothing is checked this time."""
    lock = _acquire_refresh_lock()
    if lock is None:
        return {"ok": True, "removed": [], "skipped": "a replica refresh is running"}
    try:
        return {"ok": True, "removed": _sweep_stale_temps_locked(snapshot_path().parent)}
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


# The freshness rule for every replica episode writer: each write records when
# the copy it holds was read from the hub (``read_at``, this Mac's clock), and
# a write read earlier than the copy the replica holds never replaces it. A
# full dump's rows count as read when the dump started (its floor in
# ``snapshot_meta``); a live patch as read when it is written (its hub write
# has just committed); a pull's rows when the pull began to read. Every writer
# goes through ``_upsert_episode_unfenced``, the dump through ``refresh``
# (tests/test_replica_pull_integrity.py enumerates them).
_ROW_VERSIONS_TABLE = "replica_row_versions"
_ROWS_READ_FLOOR_KEY = "rows_read_floor"


def _held_read_at(con: sqlite3.Connection, table: str, key: Any) -> float:
    """When the replica's copy of one row was read from the hub (0: unknown)."""
    held = 0.0
    if _snapshot_table_columns(con, "snapshot_meta"):
        row = con.execute("SELECT value FROM snapshot_meta WHERE key = ?", (_ROWS_READ_FLOOR_KEY,)).fetchone()
        try:
            held = float(row[0]) if row else 0.0
        except (TypeError, ValueError):
            held = 0.0
    if _snapshot_table_columns(con, _ROW_VERSIONS_TABLE):
        row = con.execute(f"SELECT read_at FROM {_ROW_VERSIONS_TABLE} WHERE tbl = ? AND key = ?",
                          (table, str(key))).fetchone()
        if row and row[0] is not None:
            held = max(held, float(row[0]))
    return held


def _record_read_at(con: sqlite3.Connection, table: str, key: Any, read_at: float) -> None:
    """Note on ``con`` when the copy just written was read (caller commits).
    Never moves back."""
    con.execute(f"CREATE TABLE IF NOT EXISTS {_ROW_VERSIONS_TABLE} (tbl TEXT NOT NULL, key TEXT NOT NULL, "
                "read_at REAL NOT NULL, PRIMARY KEY (tbl, key))")
    con.execute(f"INSERT INTO {_ROW_VERSIONS_TABLE} (tbl, key, read_at) VALUES (?, ?, ?) "
                "ON CONFLICT(tbl, key) DO UPDATE SET read_at = max(read_at, excluded.read_at)",
                (table, str(key), float(read_at)))


def _vector_key(profile: Any, kind: Any, ref: Any) -> str:
    """The freshness rule's key for one ref's vectors under one profile."""
    return f"{profile}\x1f{kind}\x1f{ref}"



def _tombstone_payload_for_episode(episode_row: Mapping[str, Any]) -> dict[str, Any]:
    """A capture-shaped identity for the local tombstone fence.

    Incremental snapshot callers also pass DB presentation fields such as
    ``id`` and aware ``ingested_at`` datetimes. Those are not capture
    identity, and feeding them to canonical JSON would reject an otherwise
    supported snapshot update.
    """
    raw = episode_row.get("raw")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    if isinstance(raw, dict):
        return dict(raw)
    payload: dict[str, Any] = {}
    for field, value in episode_row.items():
        if field in {"id", "ingested_at", "deleted_at", "raw"}:
            continue
        payload[field] = value.isoformat() if hasattr(value, "isoformat") else value
    return payload


def _tombstone_payloads_for_episode(episode_row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every identity forget may have registered for this row: its raw (or
    its columns when raw is gone), the natural source identity its own
    columns hold, and those columns completed by what raw still carries.
    An older or edit-rewritten raw can lack the source fields while the
    columns still name the forgotten source (see ``forget._natural_identities``)."""
    from khipu.capture_identity import SOURCE_FIELDS, has_natural_key

    payloads = [_tombstone_payload_for_episode(episode_row)]
    column = {k: (None if episode_row.get(k) is None else str(episode_row.get(k)))
              for k in SOURCE_FIELDS}
    if not has_natural_key(column):
        return payloads
    payloads.append(column)
    raw = payloads[0]
    merged = {k: column[k] if column[k] is not None else
              (None if raw.get(k) is None else str(raw.get(k))) for k in SOURCE_FIELDS}
    if merged != column:
        payloads.append(merged)
    return payloads


def _scrub_forgotten(con: sqlite3.Connection, gate: Any) -> int:
    """Erase every live replica episode whose source the gate refuses, with
    its vectors, commitments, decisions and deliverables, and queue its id
    for erasure. The hub can hold a forgotten source under another id
    (written before the forget, or by a Mac that never saw it), and a dump
    must not carry it into this replica. Rows already marked forgotten keep
    their tombstone. A live row whose id the registry records as forgotten
    is refused too: a backup can hold that episode under an older raw that
    no source identity matches. Every source identity a row represents
    counts (its merged captures, and the receipts the replica keeps,
    ``_anchor_identities``). Reads every episode in batches of
    ``_SCRUB_BATCH``, never all at once. What a refused episode contributed
    to other facts is not patched here: the caller's fence drops the fact
    state and it is rebuilt (design section 6). ``RegistryError``
    propagates. Caller commits."""
    cols = _snapshot_table_columns(con, "episodes")
    if "id" not in cols or gate.empty:
        return 0
    live = " AND deleted_at IS NULL" if "deleted_at" in cols else ""
    forgotten_ids = gate.episode_ids
    refused: list[int] = []
    last = -(1 << 63)
    while True:
        cursor = con.execute(f"SELECT * FROM episodes WHERE id > ?{live} ORDER BY id LIMIT ?",
                             (last, _SCRUB_BATCH))
        names = [d[0] for d in cursor.description]
        rows = [dict(zip(names, values, strict=True)) for values in cursor.fetchall()]
        if not rows:
            break
        last = rows[-1]["id"]
        for row in rows:
            if row["id"] in forgotten_ids or gate.refuses(_anchor_identities(row)):
                refused.append(int(row["id"]))
    has_decisions = "episode_id" in _snapshot_table_columns(con, "decisions")
    has_deliverables = "episode_id" in _snapshot_table_columns(con, "deliverables")
    has_commitments = {"id", "opened_episode"} <= _snapshot_table_columns(con, "commitments")
    for eid in refused:
        if has_commitments:
            commitment_ids = [str(row[0]) for row in con.execute(
                "DELETE FROM commitments WHERE opened_episode = ? RETURNING id", (eid,))]
            for batch in _chunks(commitment_ids):
                con.execute("DELETE FROM memory_embeddings WHERE kind = 'commitment' "
                            f"AND ref IN ({', '.join('?' for _ in batch)})", batch)
        con.execute("DELETE FROM episodes WHERE id = ?", (eid,))
        # Missing parents are permitted in partial replicas. Keep the refused
        # id in the durable erasure registry so independently pulled facts
        # cannot use that exception, even across another complete refresh.
        if not _queue_erasure(int(eid)):
            raise OSError("could not record rejected replica source")
        con.execute("DELETE FROM memory_embeddings WHERE kind = 'episode' AND ref = ?", (str(eid),))
        if has_decisions:
            con.execute("DELETE FROM decisions WHERE episode_id = ?", (eid,))
        if has_deliverables:
            con.execute("DELETE FROM deliverables WHERE episode_id = ?", (eid,))
    return len(refused)


def _rejected_episode_ids(con: sqlite3.Connection) -> set[int]:
    """Rejections shared by every derivative writer, including old replicas."""
    rejected = _pending_erasure_ids()
    if "deleted_at" in _snapshot_table_columns(con, "episodes"):
        rejected.update(row[0] for row in con.execute(
            "SELECT id FROM main.episodes WHERE deleted_at IS NOT NULL"))
    return rejected


def scrub_forgotten_snapshot() -> dict[str, Any]:
    """Scrub the existing replica of forgotten sources (after a backup import
    brought in an older one). One fenced write (``_replica_write_gate``,
    which takes write_gate first): the registry, then the refresh lock
    without waiting. Then, outside every lock, one fact copy."""
    from khipu.tombstones import RegistryError

    path = snapshot_path()
    if not path.is_file():
        return {"ok": True, "scrubbed": 0, "snapshot": False}
    try:
        with _replica_write_gate(path, scrub=True) as fence:
            pass
    except _ReplicaBusy:
        return {"ok": False, "error": "hub snapshot refresh in progress"}
    except RegistryError as exc:
        return {"ok": False, "error": f"tombstone registry unavailable: {exc}"}
    return {"ok": True, "scrubbed": fence.scrubbed, "snapshot": True, "facts": copy_facts()}


def staging_path() -> Path:
    """A fresh temporary file beside the replica, named the way a refresh
    names its dump, so forget's crash-left temp check sees it too."""
    from khipu.paths import ensure_data_dir

    ensure_data_dir()
    dest = snapshot_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".hub_snapshot.", suffix=".sqlite", dir=dest.parent)
    os.close(fd)
    return Path(name)


def publish_imported_snapshot(staged: Path) -> dict[str, Any]:
    """Scrub a replica copy a backup import staged, then atomically make it
    the replica. Readers never see the copy before its scrub: it is not the
    replica until ``os.replace``. One fenced write (``_replica_write_gate``,
    which takes write_gate first) on the staged copy: the registry, then the
    refresh lock without waiting; the replace happens before the lease is
    released. When the scrub cannot run, the staged copy is discarded and
    the current replica is left as it was. A restored replica carries no
    fact row and no stamp (6.2) and takes the next invalidation revision:
    its facts come with the next copy, which the caller runs once it holds
    no lock."""
    from khipu.tombstones import RegistryError

    try:
        # Erasures this Mac queued but has not applied yet name episode ids
        # the backup may hold live (the fence's scrub applies them first).
        with _replica_write_gate(snapshot_path(), target=staged, scrub=True,
                                 on_commit=lambda: os.replace(staged, snapshot_path())) as fence:
            _drop_facts(fence.con)
            _invalidate_facts(fence.con, revision=_file_revision(snapshot_path()))
    except _ReplicaBusy:
        staged.unlink(missing_ok=True)
        return {"ok": False, "imported": False,
                "error": "hub snapshot refresh in progress; the next refresh rebuilds it"}
    except (RegistryError, sqlite3.Error, OSError) as exc:
        remove_replica_temp(staged)
        return {"ok": False, "imported": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "imported": True, "scrubbed": fence.scrubbed, "snapshot": True}


def upsert_episode(
    episode_row: Mapping[str, Any],
    embedding_rows: Sequence[Mapping[str, Any]],
    *,
    read_at: float | None = None,
    receipts: Iterable[Sequence[Any]] = (),
) -> dict[str, Any]:
    """Write-through fenced by the durable forgotten-source registry.
    ``read_at``: when the caller read this row from the hub (``time.time()``),
    for the freshness rule above; ``None`` is a live patch, read now.
    ``receipts``: the episode's retained source receipts ``(source_key,
    payload_sha256)``, read with the row, for the episode fence
    (``_anchor_identities``). Facts never come this way (``copy_facts``).

    A live patch (``read_at`` None: a capture, an outbox replay) waits at
    most ``LIVE_PATCH_WAIT_S`` for another replica write; past that it is
    deferred (queued as a missed patch, which the next pull reads again),
    so a held replica write never blocks a capture (third Astra review #5)."""
    from khipu.tombstones import RegistryBusy, RegistryError, lock_wait, write_guard

    payloads = _tombstone_payloads_for_episode(episode_row)
    wait = lock_wait(LIVE_PATCH_WAIT_S) if read_at is None else nullcontext()
    try:
        # The registry lease, kept through the SQLite transaction in
        # _upsert_episode_unfenced, whose source fence checks every identity
        # the row represents. Forget takes it before its replica erase, so a
        # late incremental write cannot restore a source after forget returns.
        with wait, write_guard(*payloads) as allowed:
            if not allowed:
                # A source can first arrive under a new hub id during an
                # incremental pull. Remember that id before independent
                # commitment/decision imports get a chance to use it.
                eid = episode_row.get("id")
                if eid is not None and not _queue_erasure(int(eid)):
                    return {"ok": False, "error": "could not queue rejected source erasure"}
                return {"ok": False, "error": "forgotten source"}
            return _upsert_episode_unfenced(episode_row, embedding_rows, read_at=read_at,
                                            receipts=receipts)
    except RegistryBusy as exc:
        eid = episode_row.get("id")
        if eid is not None:
            _queue_missed_patch(eid, snapshot_path())
        return {"ok": False, "deferred": True, "error": f"replica write deferred: {exc}"}
    except RegistryError as exc:
        return {"ok": False, "error": f"tombstone registry unavailable: {exc}"}


def _upsert_episode_unfenced(
    episode_row: Mapping[str, Any],
    embedding_rows: Sequence[Mapping[str, Any]],
    *,
    read_at: float | None = None,
    receipts: Iterable[Sequence[Any]] = (),
) -> dict[str, Any]:
    """Incremental snapshot update: one episode + its embedding chunks
    (W2.4). One fenced write (``_replica_write_gate``): the episode is
    checked by every source identity it represents before anything is
    written, so a full dump never interleaves with either half. (The name
    predates the fence; ``upsert_episode`` is its only caller.) A tombstone
    landing here is a remote forget learned: the fact stamp is cleared in
    the same transaction (``_invalidate_facts``).

    Called from ``embed.embed_on_capture`` right after a successful embed, so
    a search that falls back to the sqlite replica (hub unreachable) sees a
    just-captured episode without waiting for the next full ``refresh()``.
    Takes the same cross-process lock as ``refresh()`` so an incremental
    upsert and a full dump never interleave; a dump in progress makes this a
    no-op ``{"ok": False}`` rather than blocking (the caller is fail-open).

    ``episode_row`` may carry any subset of the episode columns the sqlite
    schema knows (``_EPISODE_COLS``) — a fresh capture payload typically
    lacks ``ingested_at``/``edges``/``raw``, which is fine; those stay NULL.

    A row read from the hub before the copy the replica holds (``read_at``
    older than that copy's) never replaces it: only its tombstone lands, and
    the id is queued as a missed patch so the next pull reads it again.
    A forgotten source returns ``{"ok": False, "error": "forgotten source"}``
    with its id queued for erasure.
    """
    path = snapshot_path()
    eid = episode_row.get("id")
    if not path.is_file():
        return {
            "ok": False,
            "error": "hub snapshot missing; run `khipu snapshot refresh` first",
        }
    touched = ("episodes", "vectors")
    if eid is None:
        return {"ok": False, "error": "episode_row missing id"}
    identities = _anchor_identities(episode_row, receipts)
    try:
        with _replica_write_gate(path, live=read_at is None) as fence:
            con = fence.con
            if int(eid) in fence.check({int(eid): identities}):
                # A source can first arrive under a new hub id during an
                # incremental pull. The fence queued that id for erasure
                # before any independent commitment/decision import can use
                # it, and erases what this replica held for it.
                return {"ok": False, "error": "forgotten source"}
            pending_erasure = int(eid) in _pending_erasure_ids(path)
            version = time.time() if read_at is None else float(read_at)
            older = _held_read_at(con, "episodes", eid) > version
            if older:
                _queue_missed_patch(eid, path)
                if episode_row.get("deleted_at") is None:
                    return {"ok": True, "episode_id": eid, "embeddings": 0,
                            "skipped": "the replica holds a newer copy"}
                # A tombstone always lands: forgetting can only make the copy safer.
                episode_row = {"id": eid, "deleted_at": episode_row.get("deleted_at")}
                embedding_rows = ()
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
            if merged.get("deleted_at") is not None and not (
                    existing and "deleted_at" in names and existing[names.index("deleted_at")] is not None):
                # A tombstone landing: a remote forget learned. No fact row
                # shows until a copy that starts after it (design 6.2, F5).
                _invalidate_facts(con)
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
                # A profile whose vectors were read after this row keeps them.
                profiles = {row["profile"] for row in embedding_rows
                            if _held_read_at(con, "vectors", _vector_key(row["profile"], "episode", eid)) <= version}
                embedding_rows = [row for row in embedding_rows if row["profile"] in profiles]
                for profile in profiles:
                    con.execute(
                        "DELETE FROM memory_embeddings WHERE kind = 'episode' AND ref = ? AND profile = ?",
                        (str(eid), profile),
                    )
                    _record_read_at(con, "vectors", _vector_key(profile, "episode", eid), version)
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
            if not older:
                _record_read_at(con, "episodes", eid, version)
            now = _utcnow_iso()
            _advance_cursor(con, "episodes", synced_at=now, newest=merged.get("ingested_at"),
                            newest_id=int(eid))
            if merged.get("deleted_at") is not None:
                _advance_cursor(con, "tombstones", synced_at=now, newest=merged.get("deleted_at"))
            elif embedding_rows:
                _advance_cursor(con, "vectors", synced_at=now,
                                newest=_newest_ts(r.get("built_at") for r in embedding_rows))
        m = meta()
        m["last_incremental_upsert_at"] = _utcnow_iso()
        m["last_incremental_episode_id"] = eid
        _write_meta(m)
        return {"ok": True, "episode_id": eid, "embeddings": len(embedding_rows)}
    except _ReplicaBusy:
        _record_write_error(touched, "hub snapshot refresh in progress")
        _queue_missed_patch(eid, path)
        return {"ok": False, "error": "hub snapshot refresh in progress"}
    except Exception as exc:
        _record_write_error(touched, f"{type(exc).__name__}: {exc}")
        _queue_missed_patch(eid, path)
        raise


def upsert_embeddings(
    embedding_rows: Sequence[Mapping[str, Any]], *,
    topic_rows: Sequence[Mapping[str, Any]] = (),
    replace_refs: Sequence[tuple[str, str, str]] = (),
    read_at: float | None = None,
    anchors: Mapping[int, Any] | None = None,
) -> dict[str, Any]:
    """Incremental vectors with optional topic sources and complete ref replacement.
    One fenced write (``_replica_write_gate``): an episode's vectors carry its
    text, so ``anchors`` (``read_anchor_identities`` for the episode refs,
    read with the vectors) go through the source fence first, and a vector
    of an episode the fence refuses is never written.
    Replacements affect only the named profiles, including an empty chunk set.
    ``read_at``: when the caller read these vectors from the hub, for the
    freshness rule; ``None`` is a live write, read now. A ref whose held
    vectors (or, for an episode, its row) were read later is left alone.
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
    from khipu.tombstones import RegistryError

    touched = ("vectors", *(("topics",) if topic_rows else ()))
    try:
        with _replica_write_gate(path, live=read_at is None) as fence:
            con = fence.con
            fence.check(anchors or {})
            topic_cols = _snapshot_table_columns(con, "topics")
            # A catch-up that read a topic before it was purged must not put
            # it back: its hub write may have committed before the purge.
            purged = _purged_topic_slugs(path)
            topic_version = time.time() if read_at is None else float(read_at)
            stale_topics: set[str] = set()
            for row in topic_rows:
                if row["slug"] in purged:
                    continue
                row = _topic_routing_metadata(row)
                old = con.execute("SELECT deleted_at, updated_at FROM topics WHERE slug = ?",
                                  (row["slug"],)).fetchone()
                if old and old[0] is not None:
                    continue
                # The freshness rule, as for a pull: a topic read before the
                # copy the replica holds (an embed that read its source, then
                # waited on the provider) never replaces it.
                held, incoming = (_utc(old[1]) if old else None), _utc(row.get("updated_at"))
                if (_held_read_at(con, "topics", row["slug"]) > topic_version
                        or (held and incoming and held > incoming)):
                    # Its vectors were built from that older body too.
                    stale_topics.add(str(row["slug"]))
                    continue
                cols = [c for c in _TOPIC_COLS if c in topic_cols and c in row]
                vals = [_json_text(row[c]) if c in ("links", "frontmatter") else
                        _ts_text(row[c]) if c.endswith("_at") else row[c] for c in cols]
                con.execute(
                    f"INSERT INTO topics ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
                    "ON CONFLICT(slug) DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in cols if c != "slug"),
                    vals,
                )
                _record_read_at(con, "topics", row["slug"], topic_version)
            version = time.time() if read_at is None else float(read_at)
            refs = {(str(p), str(k), str(r)) for p, k, r in replace_refs}
            refs |= {(str(e.get("profile")), str(e.get("kind")), str(e.get("ref"))) for e in embedding_rows}
            # An episode's or topic's vectors are also as new as the source row
            # held beside them: vectors read before that row was are built from
            # an older body, with or without ``topic_rows`` in this call.
            newer = {(p, k, r) for p, k, r in refs
                     if max(_held_read_at(con, "vectors", _vector_key(p, k, r)),
                            _held_read_at(con, "episodes", r) if k == "episode" else
                            _held_read_at(con, "topics", r) if k == "topic" else 0.0) > version
                     or (k == "topic" and r in stale_topics)}
            for profile, kind, ref in replace_refs:
                if (str(profile), str(kind), str(ref)) in newer or kind in _FACT_VECTOR_KINDS:
                    continue
                con.execute("DELETE FROM memory_embeddings WHERE profile = ? AND kind = ? AND ref = ?",
                            (profile, kind, ref))
            written = 0
            for erow in embedding_rows:
                if (str(erow.get("profile")), str(erow.get("kind")), str(erow.get("ref"))) in newer:
                    continue
                if erow.get("kind") == "episode" and (
                        str(erow.get("ref")) in {str(i) for i in _pending_erasure_ids(path)}
                        or (str(erow.get("ref")).isdigit() and not fence.admits(int(erow["ref"])))):
                    continue
                if erow.get("kind") == "topic" and str(erow.get("ref")) in purged:
                    continue
                # A fact vector comes only with the fact copy (copy_facts).
                if erow.get("kind") in _FACT_VECTOR_KINDS:
                    continue
                _insert_vector_row(con, erow)
                written += 1
            for ref in refs - newer:
                _record_read_at(con, "vectors", _vector_key(*ref), version)
            now = _utcnow_iso()
            _advance_cursor(con, "vectors", synced_at=now,
                            newest=_newest_ts(r.get("built_at") for r in embedding_rows))
            if topic_rows:
                # Not a reconcile: only these topics were written, so
                # reconciled_at stays at the last full dump.
                _advance_cursor(con, "topics", synced_at=now,
                                newest=_newest_ts(r.get("updated_at") for r in topic_rows))
        m = meta()
        m["last_incremental_upsert_at"] = _utcnow_iso()
        _write_meta(m)
        out: dict[str, Any] = {"ok": True, "embeddings": written}
        if newer:
            out["kept_newer"] = len(newer)
        return out
    except _ReplicaBusy:
        _record_write_error(touched, "hub snapshot refresh in progress")
        return {"ok": False, "error": "hub snapshot refresh in progress"}
    except RegistryError as exc:
        _record_write_error(touched, f"tombstone registry unavailable: {exc}")
        return {"ok": False, "error": f"tombstone registry unavailable: {exc}"}
    except Exception as exc:
        _record_write_error(touched, f"{type(exc).__name__}: {exc}")
        raise


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
        _record_write_error(("tombstones",), "hub snapshot refresh in progress (erasure queued)")
        return {"ok": False, "episode_id": episode_id, "pending": True, "error": "hub snapshot refresh in progress"}
    try:
        con = sqlite3.connect(str(path))
        try:
            now = _utcnow_iso()
            cur = con.execute(
                "UPDATE episodes SET deleted_at = ? WHERE id = ?",
                (now, episode_id),
            )
            updated = cur.rowcount
            if updated:
                # A tombstone written: no fact row shows until the next copy
                # (design 6.2).
                _invalidate_facts(con)
            con.execute(
                "DELETE FROM memory_embeddings WHERE kind = 'episode' AND ref = ?",
                (str(episode_id),),
            )
            if _snapshot_table_columns(con, "deliverables"):
                con.execute("DELETE FROM deliverables WHERE episode_id = ?", (episode_id,))
            _erase_episode_commitments(con, episode_id)
            _advance_cursor(con, "tombstones", synced_at=now, newest=now)
            con.commit()
        finally:
            con.close()
        return {"ok": True, "episode_id": episode_id, "updated": bool(updated)}
    except Exception as exc:  # noqa: BLE001 — the hub write already succeeded
        _record_write_error(("tombstones",), f"{type(exc).__name__}: {exc} (erasure queued)")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        _release_refresh_lock(lock)


def purge_topic_in_snapshot(slug: str) -> dict[str, Any]:
    """Remove one purged topic from the local sqlite replica: its ``topics``
    row (the ``topics_fts`` entry goes with it), its ``topic_revisions`` and
    its ``memory_embeddings`` rows. ``khipu topic purge``'s replica half,
    under the same refresh lock as ``refresh()`` and
    ``forget_episode_in_snapshot``.

    ``{"ok": True, "applicable": False}`` when this Mac has no replica (never
    creates one). ``{"ok": False, "reason": ...}`` when the replica could not
    be changed, so the caller never reports the topic gone from this Mac while
    the replica still serves it. Never raises."""
    path = snapshot_path()
    if not path.is_file():
        return {"ok": True, "applicable": False, "reason": "this Mac has no local replica"}
    if not _mark_purged_topic(slug, path):
        return {"ok": False, "applicable": True,
                "reason": "could not record the purge beside the replica"}
    lock = _acquire_refresh_lock()
    if lock is None:
        return {"ok": False, "applicable": True, "reason": "a replica refresh is in progress"}
    try:
        # mode=rw: a replica removed since the check above is not recreated.
        con = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=2.0)
        try:
            if not {"slug"} <= _snapshot_table_columns(con, "topics"):
                return {"ok": False, "applicable": True, "reason": "the replica has no topics table"}
            fts_tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE name IN ('topics_fts', 'topics_fts_ad')"
            )}
            if "topics_fts" in fts_tables and "topics_fts_ad" not in fts_tables:
                # An external-content index keeps a deleted row's terms
                # unless the exact indexed values are deleted from it.
                cols = ", ".join(_TOPIC_FTS_COLS)
                con.execute(
                    f"INSERT INTO topics_fts(topics_fts, rowid, {cols}) "
                    f"SELECT 'delete', rowid, {cols} FROM topics WHERE slug = ?",
                    (slug,),
                )
            embeddings = 0
            if {"kind", "ref"} <= _snapshot_table_columns(con, "memory_embeddings"):
                embeddings = con.execute(
                    "DELETE FROM memory_embeddings WHERE kind = 'topic' AND ref = ?", (slug,)
                ).rowcount
            revisions = 0
            if "slug" in _snapshot_table_columns(con, "topic_revisions"):
                revisions = con.execute(
                    "DELETE FROM topic_revisions WHERE slug = ?", (slug,)
                ).rowcount
            topics = con.execute("DELETE FROM topics WHERE slug = ?", (slug,)).rowcount
            con.commit()
        finally:
            con.close()
        return {
            "ok": True, "applicable": True, "topic_removed": bool(topics),
            "revisions_removed": revisions, "embeddings_removed": embeddings,
        }
    except Exception as exc:  # noqa: BLE001 - reported to the caller, never raised
        return {"ok": False, "applicable": True, "reason": f"{type(exc).__name__}: {exc}"}
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
    """Age and size for doctor/status — separate from hub liveness.
    ``age_seconds`` is the time since the copy last caught up with the hub
    (``caught_up_at``); ``refreshed_at`` and ``dump_age_seconds`` are the
    last full dump's, which names the dump and schedules the next one."""
    path = snapshot_path()
    m = meta()
    if not path.is_file():
        return {
            "ok": False,
            "exists": False,
            "path": str(path),
            "refreshed_at": m.get("refreshed_at"),
            "caught_up_at": None,
            "size_bytes": None,
            "age_seconds": None,
            "dump_age_seconds": None,
        }
    st = path.stat()
    refreshed_at = m.get("refreshed_at")
    current_at = caught_up_at(m)
    now = datetime.now(timezone.utc)

    def age_of(val: Any) -> int | None:
        at = _utc(val)
        return None if at is None else int((now - at).total_seconds())

    age_s = age_of(current_at)
    return {
        "ok": age_s is not None,
        "exists": True,
        "path": str(path),
        "refreshed_at": refreshed_at,
        "caught_up_at": current_at,
        "size_bytes": st.st_size,
        "age_seconds": age_s,
        "dump_age_seconds": age_of(refreshed_at),
        "counts": m.get("counts"),
        "schema_version": m.get("schema_version"),
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


# ---- Honest freshness (replica freshness, phase 1) --------------------------
# A table is "behind" by the age of the oldest hub change the replica does not
# hold: the hub's now() minus that change's own timestamp. No time from this
# Mac's clock is ever compared with one from the hub's: the cutoff (changes
# since the last full dump, less an overlap for ingested_at being transaction
# start, not commit) and the floor for a change with no time of its own both
# come from ``hub_dumped_at``, the hub's now() read at the start of the dump.
# A replica dumped before that was recorded is not measured.
SNAPSHOT_LAG_BEHIND_S = SNAPSHOT_BEHIND_INGEST_MAX_S
FRESHNESS_OVERLAP_S = 10 * 60
# Per table per measurement. The live hub saw about 3,000 decision changes and
# 900 episodes in a day (2026-10-15); a cut list is reported as truncated and
# never counts as in step.
FRESHNESS_PROBE_LIMIT = 5000
# How long a measurement that found every table in step lets the per-prompt
# recall lane keep trusting a replica whose last full dump is overdue.
LAST_KNOWN_TRUST_S = 60 * 60
FRESHNESS_NAME = "hub_snapshot.sqlite.freshness.json"
_ID_CHUNK = 500


def freshness_path() -> Path:
    from khipu.paths import data_dir

    return data_dir() / FRESHNESS_NAME


def _lag_from(hub_now: datetime, stamps: Sequence[Any]) -> float:
    oldest = min((d for d in (_utc(s) for s in stamps) if d is not None), default=None)
    return max(0.0, (hub_now - oldest).total_seconds()) if oldest is not None else 0.0


def _chunks(items: Sequence[Any]) -> Any:
    for i in range(0, len(items), _ID_CHUNK):
        yield items[i:i + _ID_CHUNK]


def _present(con: sqlite3.Connection, sql: str, keys: Sequence[Any]) -> set[Any]:
    """Which of ``keys`` the replica's ``sql`` (with one ``{}`` for the
    placeholder list) returns as its first column."""
    found: set[Any] = set()
    for part in _chunks(list(keys)):
        found.update(r[0] for r in con.execute(sql.format(", ".join("?" * len(part))), part))
    return found


# Episode columns the hub edits in place, with no time of their own: a
# capture merged into an existing episode (summary, and always one more
# ``raw.merged_from`` entry), a summary correction, and hygiene filling
# project and repo_root. ``raw`` is compared by that count alone.
_EPISODE_TRACKED = ("summary", "project", "repo_root", "raw")
_EPISODE_BUCKET_IDS = 256


def _md5_text(text: str) -> str:
    import hashlib

    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _episode_line_sql(cols: Sequence[str]) -> str:
    parts = ["id::text"]
    for c in cols:
        if c == "summary":
            parts.append("md5(COALESCE(summary, ''))")
        elif c == "raw":
            parts.append("COALESCE(jsonb_array_length(CASE WHEN jsonb_typeof(raw::jsonb -> 'merged_from') = 'array' "
                         "THEN raw::jsonb -> 'merged_from' END), 0)::text")
        else:
            parts.append(f"COALESCE({c}::text, '')")
    return " || E'\\t' || ".join(parts)


def _episode_line(eid: int, vals: Sequence[tuple[str, Any]]) -> str:
    parts = [str(eid)]
    for c, v in vals:
        if c == "summary":
            parts.append(_md5_text(v or ""))
        elif c == "raw":
            try:
                raw = json.loads(v) if isinstance(v, str) else v
            except ValueError:
                raw = None
            merged = raw.get("merged_from") if isinstance(raw, dict) else None
            parts.append(str(len(merged)) if isinstance(merged, list) else "0")
        else:
            parts.append("" if v is None else str(v))
    return "\t".join(parts)


def _lag_episodes(cur, con, since, hub_now, limit, hub_dump, pending: set[int]) -> dict[str, Any]:
    """Episodes ingested since the cutoff, dated by their own time; then
    every live episode on both sides, compared by bucket digest of
    ``_episode_line``, which catches what no cutoff can: an edit in place,
    an episode written outside the window, and one the replica holds live
    that the hub tombstoned (dated by that) or no longer has (dated from
    the dump). Queued local erasures are left out on both sides."""
    hub_cols = _pg_columns(cur, "episodes")
    has_del = "deleted_at" in hub_cols
    live = " AND deleted_at IS NULL" if has_del else ""
    cur.execute(
        f"SELECT id, ingested_at FROM episodes WHERE ingested_at > %s{live} "
        "ORDER BY ingested_at, id LIMIT %s",
        (since, limit),
    )
    rows = cur.fetchall()
    held = _present(con, "SELECT id FROM main.episodes WHERE id IN ({})", [int(r[0]) for r in rows])
    stale: dict[int, Any] = {int(r[0]): r[1] for r in rows if int(r[0]) not in held}
    checked = len(rows)
    truncated = len(rows) >= limit

    rep_cols = _snapshot_table_columns(con, "episodes")
    cols = [c for c in _EPISODE_TRACKED if c in hub_cols and c in rep_cols]
    replica: dict[int, str] = {}
    for r in con.execute(f"SELECT {', '.join(['id', *cols])} FROM main.episodes"
                         + (" WHERE deleted_at IS NULL" if "deleted_at" in rep_cols else "")):
        if int(r[0]) not in pending:
            replica[int(r[0])] = _episode_line(int(r[0]), list(zip(cols, r[1:])))
    rep_buckets: dict[int, list[int]] = {}
    for eid in replica:
        rep_buckets.setdefault(eid // _EPISODE_BUCKET_IDS, []).append(eid)
    rep_digests = {b: _md5_text("\n".join(replica[i] for i in sorted(ids))) for b, ids in rep_buckets.items()}
    where = f"WHERE id <> ALL(%s::bigint[]){live}"
    skip = sorted(pending)
    cur.execute(
        f"SELECT id / {_EPISODE_BUCKET_IDS} AS bucket, "
        f"md5(string_agg({_episode_line_sql(cols)}, E'\\n' ORDER BY id)) FROM episodes {where} GROUP BY 1",
        (skip,),
    )
    hub_digests = {int(b): d for b, d in cur.fetchall()}
    differing = sorted(b for b in set(hub_digests) | set(rep_digests) if hub_digests.get(b) != rep_digests.get(b))
    floor = _utc(hub_dump)
    if differing:
        room = max(limit - checked, 0)
        cur.execute(
            f"SELECT id, ingested_at, {_episode_line_sql(cols)} FROM episodes {where} "
            f"AND id / {_EPISODE_BUCKET_IDS} = ANY(%s::bigint[]) ORDER BY id LIMIT %s",
            (skip, differing, room + 1),
        )
        hub_rows = cur.fetchall()
        whole = set(differing)
        if len(hub_rows) > room:
            truncated = True
            # Only buckets read to their end can say what the hub lacks.
            cut = int(hub_rows[room][0]) // _EPISODE_BUCKET_IDS
            whole = {b for b in differing if b < cut}
            hub_rows = hub_rows[:room]
        checked += len(hub_rows)
        hub_ids: set[int] = set()
        for eid, ingested, line in hub_rows:
            hub_ids.add(int(eid))
            if replica.get(int(eid)) != line and int(eid) not in stale:
                stale[int(eid)] = _since_dump(ingested, floor)
        gone = sorted(i for b in whole for i in rep_buckets.get(b, ()) if i not in hub_ids)
        if gone:
            deleted: dict[int, Any] = {}
            if has_del:
                cur.execute("SELECT id, deleted_at FROM episodes WHERE id = ANY(%s::bigint[])", (gone,))
                deleted = {int(i): d for i, d in cur.fetchall()}
            for eid in gone:
                stale[eid] = _since_dump(deleted.get(eid), floor)
    return {"lag_seconds": _lag_from(hub_now, list(stale.values())), "missing": len(stale),
            "checked": checked, "buckets_differing": len(differing), "truncated": truncated}


def _lag_tombstones(cur, con, since, hub_now, limit, pending: set[int]) -> dict[str, Any]:
    unapplied: list[Any] = []
    checked = 0
    truncated = False
    if "deleted_at" in _pg_columns(cur, "episodes"):
        cur.execute("SELECT id, deleted_at FROM episodes WHERE deleted_at > %s ORDER BY deleted_at, id LIMIT %s",
                    (since, limit))
        rows = cur.fetchall()
        checked += len(rows)
        truncated = truncated or len(rows) >= limit
        ids = [int(r[0]) for r in rows if int(r[0]) not in pending]
        live = _present(con, "SELECT id FROM main.episodes WHERE deleted_at IS NULL AND id IN ({})", ids)
        unapplied += [r[1] for r in rows if int(r[0]) in live]
    if "deleted_at" in _pg_columns(cur, "topics"):
        cur.execute("SELECT slug, deleted_at FROM topics WHERE deleted_at > %s ORDER BY deleted_at, slug LIMIT %s",
                    (since, limit))
        rows = cur.fetchall()
        checked += len(rows)
        truncated = truncated or len(rows) >= limit
        live = _present(con, "SELECT slug FROM main.topics WHERE deleted_at IS NULL AND slug IN ({})",
                        [r[0] for r in rows])
        unapplied += [r[1] for r in rows if r[0] in live]
    return {"lag_seconds": _lag_from(hub_now, unapplied), "missing": len(unapplied),
            "checked": checked, "truncated": truncated}


def _since_dump(when: Any, floor: datetime | None) -> datetime | None:
    """A change the replica lacks happened after the dump (else the dump
    would hold it): its own time, but never earlier than the dump's, both
    on the hub's clock."""
    when = _utc(when)
    return max(when, floor) if when is not None and floor is not None else (floor or when)


# Topic columns besides content_hash that change with no new content: a
# supersession (organise) and an event time (notes).
_TOPIC_TRACKED = ("status", "superseded_by", "event_at")


def _manifest_field_sql(col: str) -> str:
    if col.endswith("_at"):
        return f"COALESCE(floor(extract(epoch FROM {col}) * 1000000)::bigint::text, '')"
    return f"COALESCE({col}::text, '')"


def _manifest_field(col: str, val: Any) -> str:
    if col.endswith("_at"):
        return _vec_built_key(val)
    return "" if val is None else str(val)


def _topic_line(slug: Any, chash: Any, extra: Sequence[tuple[str, Any]]) -> str:
    return "\t".join([str(slug), chash or "", *(_manifest_field(c, v) for c, v in extra)])


def _topic_manifest_digest(lines: Sequence[tuple[Any, str]]) -> str | None:
    import hashlib

    if not lines:
        return None
    text = "\n".join(line for _slug, line in sorted(lines, key=lambda p: str(p[0])))
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _lag_topics(cur, con, hub_now, hub_dump) -> dict[str, Any]:
    """Topics are compared whole, by ``(slug, content_hash, status,
    superseded_by, event_at)`` of every live topic on both sides:
    ``updated_at`` is backdated by the file mirror, so no timestamp cursor
    can see a correction. A digest first (one row from the hub); the full
    manifest only when the digests differ. A topic the replica holds live
    and the hub does not was tombstoned (dated by that) or purged there
    (dated from the dump)."""
    hub_cols = _pg_columns(cur, "topics")
    if not {"slug", "content_hash"} <= hub_cols:
        return {"lag_seconds": None, "not_applicable": "hub topics have no content_hash"}
    rep_cols = _snapshot_table_columns(con, "topics")
    extra = [c for c in _TOPIC_TRACKED if c in hub_cols and c in rep_cols]
    live = " WHERE deleted_at IS NULL" if "deleted_at" in hub_cols else ""
    replica = {
        str(r[0]): _topic_line(r[0], r[1], list(zip(extra, r[2:])))
        for r in con.execute(
            f"SELECT {', '.join(['slug', 'content_hash', *extra])} FROM main.topics"
            + (" WHERE deleted_at IS NULL" if "deleted_at" in rep_cols else "")
        )
    }
    line_sql = " || E'\\t' || ".join(["slug", "COALESCE(content_hash, '')", *map(_manifest_field_sql, extra)])
    cur.execute(f"SELECT md5(string_agg({line_sql}, E'\\n' ORDER BY slug COLLATE \"C\")) FROM topics{live}")
    row = cur.fetchone()
    if (row[0] if row else None) == _topic_manifest_digest(list(replica.items())):
        return {"lag_seconds": 0.0, "missing": 0, "checked": len(replica), "truncated": False}
    upd = "updated_at" if "updated_at" in hub_cols else "NULL"
    cur.execute(f"SELECT {', '.join(['slug', 'content_hash', upd, *extra])} FROM topics{live}")
    hub_rows = cur.fetchall()
    floor = _utc(hub_dump)
    stamps = [_since_dump(r[2], floor) for r in hub_rows
              if replica.get(str(r[0])) != _topic_line(r[0], r[1], list(zip(extra, r[3:])))]
    gone = sorted(set(replica) - {str(r[0]) for r in hub_rows})
    if gone:
        deleted: dict[str, Any] = {}
        if "deleted_at" in hub_cols:
            cur.execute("SELECT slug, deleted_at FROM topics WHERE slug = ANY(%s::text[])", (gone,))
            deleted = {str(s): d for s, d in cur.fetchall()}
        stamps += [_since_dump(deleted.get(slug), floor) for slug in gone]
    return {"lag_seconds": _lag_from(hub_now, stamps), "missing": len(stamps), "checked": len(hub_rows),
            "truncated": False}


def _decision_digest_line(row: Sequence[Any]) -> str:
    """One decision as the whole-table digest spells it: id, then each
    tracked column, times as whole microseconds since the epoch."""
    parts = [str(int(row[0]))]
    for val in row[1:]:
        if val is None:
            parts.append("")
        elif isinstance(val, int) and not isinstance(val, bool):
            parts.append(str(val))
        else:
            parts.append(_vec_built_key(val) if _utc(val) is not None else str(val))
    return "\t".join(parts)


def _lag_decisions(cur, con, since, hub_now, limit, hub_dump) -> dict[str, Any]:
    """Decisions changed since the cutoff, compared with the replica one by
    one and dated by their own time. Then a digest of every decision's
    tracked columns on both sides, with those changes laid over the
    replica's: a difference left over lies outside the cutoff (a restore or
    un-retract clears the times the cutoff is keyed on), and is dated from
    the dump on the hub's clock."""
    cols = _pg_columns(cur, "decisions")
    if not {"id", "created_at"} <= cols:
        return {"lag_seconds": None, "not_applicable": "hub decisions table not migrated"}
    if not _snapshot_table_columns(con, "decisions"):
        return {"lag_seconds": None, "not_applicable": "replica has no decisions table"}
    tracked = [c for c in ("superseded_at", "retracted_at", "superseded_by") if c in cols]
    wm = "created_at"
    for c in ("superseded_at", "retracted_at"):
        if c in cols:
            wm = f"GREATEST({wm}, COALESCE({c}, {wm}))"
    sel = ", ".join(["id", *tracked])
    cur.execute(
        f"SELECT {sel}, {wm} AS _wm FROM decisions WHERE {wm} > %s ORDER BY _wm, id LIMIT %s",
        (since, limit),
    )
    rows = cur.fetchall()
    rep_cols = _snapshot_table_columns(con, "decisions")
    rep_tracked = [c for c in tracked if c in rep_cols]
    held: dict[int, tuple[Any, ...]] = {}
    for part in _chunks([int(r[0]) for r in rows]):
        for r in con.execute(
            f"SELECT {', '.join(['id', *rep_tracked])} FROM decisions WHERE id IN ({', '.join('?' * len(part))})",
            part,
        ):
            held[int(r[0])] = r[1:]

    def same(hub_val: Any, rep_val: Any, col: str) -> bool:
        if col.endswith("_at"):
            return _utc(hub_val) == _utc(rep_val)
        return (hub_val if hub_val is None else int(hub_val)) == (rep_val if rep_val is None else int(rep_val))

    unapplied = []
    for r in rows:
        mine = held.get(int(r[0]))
        hub_vals = dict(zip(tracked, r[1:-1]))
        if mine is None or not all(same(hub_vals[c], v, c) for c, v in zip(rep_tracked, mine)):
            unapplied.append(r[-1])
    truncated = len(rows) >= limit
    if not truncated:
        import hashlib

        def micros(col: str) -> str:
            if col.endswith("_at"):
                return f"COALESCE(floor(extract(epoch FROM {col}) * 1000000)::bigint::text, '')"
            return f"COALESCE({col}::text, '')"

        line = " || E'\\t' || ".join(["id::text", *(micros(c) for c in rep_tracked)])
        cur.execute(f"SELECT md5(string_agg({line}, E'\\n' ORDER BY id)) FROM decisions")
        got = cur.fetchone()
        mine_all = {int(r[0]): tuple(r[1:]) for r in con.execute(
            f"SELECT {', '.join(['id', *rep_tracked])} FROM decisions")}
        for r in rows:
            hub_vals = dict(zip(tracked, r[1:-1]))
            mine_all[int(r[0])] = tuple(hub_vals[c] for c in rep_tracked)
        text = "\n".join(_decision_digest_line((i, *v)) for i, v in sorted(mine_all.items()))
        if (got[0] if got else None) != (hashlib.md5(text.encode("utf-8")).hexdigest() if mine_all else None):
            unapplied.append(hub_dump)
    return {"lag_seconds": _lag_from(hub_now, unapplied), "missing": len(unapplied),
            "checked": len(rows), "truncated": truncated}


def _lag_deliverables(cur, con, since, hub_now, limit) -> dict[str, Any]:
    hub_cols = _pg_columns(cur, "deliverables")
    if not {"id", "created_at"} <= hub_cols:
        return {"lag_seconds": None, "not_applicable": "hub has no deliverables table"}
    if "id" not in _snapshot_table_columns(con, "deliverables"):
        return {"lag_seconds": None, "not_applicable": "replica has no deliverables table"}
    where = ""
    if "episode_id" in hub_cols and "deleted_at" in _pg_columns(cur, "episodes"):
        # The same exclusion the dump applies: a forgotten episode's rows are
        # never exported, so their absence is not a lag.
        where = (" AND (episode_id IS NULL OR NOT EXISTS (SELECT 1 FROM episodes e "
                 "WHERE e.id = deliverables.episode_id AND e.deleted_at IS NOT NULL))")
    cur.execute(f"SELECT id, created_at FROM deliverables WHERE created_at > %s{where} "
                "ORDER BY created_at, id LIMIT %s", (since, limit))
    rows = cur.fetchall()
    held = _present(con, "SELECT id FROM deliverables WHERE id IN ({})", [int(r[0]) for r in rows])
    missing = [r[1] for r in rows if int(r[0]) not in held]
    return {"lag_seconds": _lag_from(hub_now, missing), "missing": len(missing),
            "checked": len(rows), "truncated": len(rows) >= limit}


# The vector manifest is compared in buckets keyed by the first hex digits of
# md5(kind TAB ref), so a hub-sized profile costs one row per bucket from the
# hub when nothing differs, and only differing buckets are read, in pages, up
# to the probe's row limit.
_VEC_BUCKET_HEX = 3
_VEC_BUCKET_BATCH = 64
_VEC_PAGE_ROWS = 2000


def _vec_bucket(kind: Any, ref: Any) -> str:
    import hashlib

    return hashlib.md5(f"{kind}\t{ref}".encode("utf-8")).hexdigest()[:_VEC_BUCKET_HEX]


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _vec_built_key(val: Any) -> str:
    """A build time as the vector digest spells it on both sides: whole
    microseconds since the epoch, or empty when there is none."""
    from datetime import timedelta

    when = _utc(val)
    return "" if when is None else str((when - _EPOCH) // timedelta(microseconds=1))


def _vec_manifest_digests(rows: Sequence[tuple[Any, Any, Any, Any, Any]]) -> dict[str, str]:
    """``(kind, ref, chunk_idx, content_hash, built_at)`` rows as one md5 per
    bucket, the same text the hub builds with ``string_agg`` in
    ``_lag_vectors`` (byte order, as ``COLLATE "C"`` sorts)."""
    import hashlib

    by_bucket: dict[str, list[tuple[str, str, int, str, str]]] = {}
    for kind, ref, idx, chash, built in rows:
        by_bucket.setdefault(_vec_bucket(kind, ref), []).append(
            (str(kind), str(ref), int(idx), chash or "", _vec_built_key(built)))
    return {
        b: hashlib.md5("\n".join(f"{k}\t{r}\t{i}\t{h}\t{t}" for k, r, i, h, t in sorted(items))
                       .encode("utf-8")).hexdigest()
        for b, items in by_bucket.items()
    }


# What makes two embedding profiles the same to recall, besides the id.
_PROFILE_SPEC = ("provider", "model", "dim", "normalize", "endpoint")


def _profile_spec(record: Mapping[str, Any], cols: Sequence[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"id": str(record.get("id"))}
    for c in cols:
        v = record.get(c)
        if c == "dim":
            v = int(v) if v is not None else None
        elif c == "normalize":
            v = v or "l2"
        elif c == "endpoint":
            v = v or None
        out[c] = v
    return out


def _replica_active_profile(con: sqlite3.Connection, cols: Sequence[str]) -> dict[str, Any] | None:
    """The profile ``active_snapshot_profile_spec`` would pick, with the
    endpoint the dump saved beside it."""
    have = _snapshot_table_columns(con, "embedding_profiles")
    if not {"id", "is_active"} <= have:
        return None
    row_cols = [c for c in cols if c in have]
    row = con.execute(f"SELECT {', '.join(['id', *row_cols])} FROM embedding_profiles "
                      "WHERE is_active = 1 LIMIT 1").fetchone()
    if not row:
        return None
    record: dict[str, Any] = dict(zip(["id", *row_cols], row))
    if "endpoint" in cols and _snapshot_table_columns(con, "snapshot_meta"):
        saved = con.execute("SELECT value FROM snapshot_meta WHERE key = ?",
                            (f"embedding_profile:{record['id']}",)).fetchone()
        try:
            loaded = json.loads(saved[0]) if saved else {}
        except (TypeError, ValueError):
            loaded = {}
        record["endpoint"] = loaded.get("endpoint") if isinstance(loaded, dict) else None
    return _profile_spec(record, cols)


def _lag_vectors(cur, con, hub_dump, hub_now, limit, pending: set[int]) -> dict[str, Any]:
    """Vectors of the active profile, reconciled chunk by chunk, with no
    cutoff.

    Every live ``(kind, ref, chunk_idx, content_hash, built_at)`` on both
    sides is compared by bucket digest, then chunk by chunk in the buckets
    that differ. The build time is the generation: every replica writer
    copies the hub's own ``built_at`` (the dump reads it, a catch-up gets it
    back from the write), so a chunk the replica lacks, holds with other
    text, holds from another build, or holds after the hub pruned or deleted
    it is seen whatever its age. A replica chunk with the hub's text and no
    build time cannot be told apart, so it is counted ``unverified`` and the
    state is unknown.

    Bucket reads are paged and stop at ``limit`` rows, marking the result
    truncated. A replica chunk is judged deleted on the hub only in a bucket
    read to its end, and dated from ``hub_dump`` (the hub's clock), as is
    any change older than the dump."""
    prof_cols = _pg_columns(cur, "embedding_profiles")
    if not {"id", "is_active"} <= prof_cols:
        return {"lag_seconds": None, "not_applicable": "hub has no embedding profiles"}
    spec_cols = [c for c in _PROFILE_SPEC if c in prof_cols]
    cur.execute(f"SELECT {', '.join(['id', *spec_cols])} FROM embedding_profiles WHERE is_active ORDER BY id LIMIT 1")
    prof = cur.fetchone()
    if not prof:
        return {"lag_seconds": None, "not_applicable": "no active embedding profile"}
    profile = prof[0]
    # Recall on this Mac uses the replica's own active profile: matching
    # vectors under the hub's profile are no use if that is another one.
    hub_spec = _profile_spec(dict(zip(["id", *spec_cols], prof)), spec_cols)
    rep_spec = _replica_active_profile(con, spec_cols)
    gone = ""
    if "deleted_at" in _pg_columns(cur, "episodes"):
        gone = (" AND NOT (kind = 'episode' AND EXISTS (SELECT 1 FROM episodes e "
                "WHERE e.id::text = memory_embeddings.ref AND e.deleted_at IS NOT NULL))")

    def skip(kind: Any, ref: Any) -> bool:
        text = str(ref)
        return kind == "episode" and text.isascii() and text.isdigit() and int(text) in pending

    rep_gone = ""
    if "deleted_at" in _snapshot_table_columns(con, "episodes"):
        rep_gone = (" AND NOT (kind = 'episode' AND ref IN "
                    "(SELECT CAST(id AS TEXT) FROM main.episodes WHERE deleted_at IS NOT NULL))")
    replica: dict[tuple[str, str, int], tuple[Any, Any]] = {}
    for kind, ref, idx, chash, built in con.execute(
        "SELECT kind, ref, chunk_idx, content_hash, built_at FROM main.memory_embeddings "
        f"WHERE profile = ?{rep_gone}", (profile,),
    ):
        if not skip(kind, ref):
            replica[(str(kind), str(ref), int(idx))] = (chash or "", built)

    key_text = "kind || E'\\t' || ref"
    built_text = "COALESCE(floor(extract(epoch FROM built_at) * 1000000)::bigint::text, '')"
    cur.execute(
        f"SELECT substr(md5({key_text}), 1, {_VEC_BUCKET_HEX}) AS bucket, "
        f"md5(string_agg({key_text} || E'\\t' || chunk_idx::text || E'\\t' || COALESCE(content_hash, '') "
        f"|| E'\\t' || {built_text}, "
        "E'\\n' ORDER BY kind COLLATE \"C\", ref COLLATE \"C\", chunk_idx)) "
        f"FROM memory_embeddings WHERE profile = %s{gone} GROUP BY 1",
        (profile,),
    )
    hub_digests = {str(b): d for b, d in cur.fetchall()}
    rep_digests = _vec_manifest_digests([(k, r, i, v[0], v[1]) for (k, r, i), v in replica.items()])
    rep_by_bucket: dict[str, list[tuple[str, str, int]]] = {}
    for key in replica:
        rep_by_bucket.setdefault(_vec_bucket(key[0], key[1]), []).append(key)
    differing = sorted(b for b in set(hub_digests) | set(rep_digests) if hub_digests.get(b) != rep_digests.get(b))
    differing_set = set(differing)
    # Where the digests agree both sides hold the same build times, so a
    # missing one is missing on both: still no evidence of the generation.
    unverified = sum(1 for key, (_h, built) in replica.items()
                     if _utc(built) is None and _vec_bucket(key[0], key[1]) not in differing_set)

    floor = _utc(hub_dump)
    stale: dict[tuple[str, str, int], Any] = {}
    if rep_spec != hub_spec:
        # Switched or respecified on the hub after the dump; when is unknown.
        stale[("profile", str(profile), -1)] = floor
    checked = 0
    truncated = False
    bucket_expr = f"substr(md5({key_text}), 1, {_VEC_BUCKET_HEX})"
    page_sql = (
        "SELECT bucket, kind, ref, chunk_idx, content_hash, built_at FROM ("
        f"SELECT {bucket_expr} COLLATE \"C\" AS bucket, kind, ref, chunk_idx, content_hash, built_at "
        f"FROM memory_embeddings WHERE profile = %s AND {bucket_expr} = ANY(%s){gone}) AS v "
        "WHERE (bucket, kind, ref, chunk_idx) > (%s, %s, %s, %s) "
        "ORDER BY bucket, kind, ref, chunk_idx LIMIT %s"
    )

    def since_dump(when: datetime | None) -> datetime | None:
        return max(when, floor) if when is not None and floor is not None else (floor or when)

    for start in range(0, len(differing), _VEC_BUCKET_BATCH):
        batch = differing[start:start + _VEC_BUCKET_BATCH]
        after: tuple[Any, ...] = ("", "", "", -1)
        hub_keys: set[tuple[str, str, int]] = set()
        unread: str | None = None  # first bucket not read to its end
        upcoming = ""  # bucket of the next row not yet read
        while True:
            room = limit - checked
            if room <= 0:
                unread = upcoming
                break
            page = min(room, _VEC_PAGE_ROWS)
            # One row past the page says whether the batch goes on.
            cur.execute(page_sql, (profile, list(batch), *after, page + 1))
            rows = cur.fetchall()
            more = len(rows) > page
            if more:
                upcoming = str(rows[page][0])
            rows = rows[:page]
            checked += len(rows)
            for bucket, kind, ref, idx, chash, built in rows:
                if skip(kind, ref):
                    continue
                key = (str(kind), str(ref), int(idx))
                hub_keys.add(key)
                mine = replica.get(key)
                hub_built = _utc(built)
                if mine is None or mine[0] != (chash or ""):
                    stale[key] = since_dump(hub_built)
                    continue
                held = _utc(mine[1])
                if held is None or hub_built is None:
                    unverified += 1
                elif held != hub_built:
                    stale[key] = since_dump(hub_built)
            if not more:
                break
            after = tuple(rows[-1][:4])
        whole = [b for b in batch if unread is None or b < unread]
        for key in (k for b in whole for k in rep_by_bucket.get(b, ())):
            if key not in hub_keys:
                # Pruned or deleted on the hub after the dump; when is unknown.
                stale[key] = floor
        if unread is not None:
            truncated = True
            break

    out = {"lag_seconds": _lag_from(hub_now, list(stale.values())), "missing": len(stale), "profile": profile,
           "checked": checked, "buckets_differing": len(differing), "truncated": truncated}
    if rep_spec != hub_spec:
        out["replica_profile"] = rep_spec.get("id") if rep_spec else None
    if unverified:
        out["unverified"] = unverified
    return out


def measure_lag(cur, *, limit: int = FRESHNESS_PROBE_LIMIT) -> dict[str, Any]:
    """Compare the replica with the hub, table by table, on a cursor the
    caller already holds (``drift.status_payload``'s), as its last use of
    that cursor: a failing hub query aborts the transaction, so the tables
    after it are reported unmeasured. Read-only on both sides. Never raises for a missing or unusable replica: ``measured`` is False and
    ``reason`` says why."""
    path = snapshot_path()
    if not path.is_file():
        return {"measured": False, "reason": "replica missing"}
    m = meta()
    if _utc(m.get("refreshed_at")) is None:
        return {"measured": False, "reason": "no full dump on record"}
    hub_dump = _utc(m.get("hub_dumped_at"))
    if hub_dump is None:
        return {"measured": False, "reason": "no hub dump time on record"}
    from datetime import timedelta

    since = hub_dump - timedelta(seconds=FRESHNESS_OVERLAP_S)
    cur.execute("SELECT now()")
    hub_now = _utc(cur.fetchone()[0]) or datetime.now(timezone.utc)
    pending = _pending_erasure_ids(path)
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        probes = {
            "episodes": lambda: _lag_episodes(cur, con, since, hub_now, limit, hub_dump, pending),
            "tombstones": lambda: _lag_tombstones(cur, con, since, hub_now, limit, pending),
            "topics": lambda: _lag_topics(cur, con, hub_now, hub_dump),
            "decisions": lambda: _lag_decisions(cur, con, since, hub_now, limit, hub_dump),
            "deliverables": lambda: _lag_deliverables(cur, con, since, hub_now, limit),
            "vectors": lambda: _lag_vectors(cur, con, hub_dump, hub_now, limit, pending),
        }
        tables: dict[str, dict[str, Any]] = {}
        failed: str | None = None
        for name, probe in probes.items():
            if failed is not None:
                tables[name] = {"lag_seconds": None, "error": f"not measured: {failed} failed first"}
                continue
            try:
                tables[name] = probe()
            except Exception as exc:  # noqa: BLE001 — reported in the table's place
                # A failed query aborts the caller's transaction, so nothing
                # after it can run on this cursor. No savepoint per table:
                # those cost two round trips each on every status call.
                failed = name
                tables[name] = {"lag_seconds": None, "error": f"{type(exc).__name__}: {exc}"[:300]}
    finally:
        con.close()
    return {"measured": True, "measured_at": hub_now.isoformat(), "since": since.isoformat(), "tables": tables}


def _read_last_known() -> dict[str, Any] | None:
    try:
        loaded = json.loads(freshness_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _write_last_known(record: Mapping[str, Any]) -> None:
    from khipu.paths import isolated_dir

    if isolated_dir() is not None:
        return
    dest = freshness_path()
    try:
        fd, name = tempfile.mkstemp(prefix=".hub_snapshot.freshness.", dir=dest.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(record, fh, indent=2)
            os.replace(name, dest)
        finally:
            Path(name).unlink(missing_ok=True)
    except OSError:
        pass


def _local_state(health: Mapping[str, Any], sidecar: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """``("stale", reason)`` when this Mac's own evidence says the scheduled
    full dump is missing, overdue or failed after its last success; else
    ``(None, None)``: the hub has to say more."""
    if not health.get("exists"):
        return "stale", "snapshot_missing"
    age = health.get("age_seconds")
    if age is None:
        return "stale", "refresh_evidence_unknown"
    if age > SNAPSHOT_MAX_AGE_S:
        return "stale", "snapshot_refresh_overdue"
    failed = sidecar.get("refresh_error")
    if isinstance(failed, dict) and _ts_not_before(failed.get("at"), health.get("refreshed_at")):
        return "stale", "snapshot_refresh_failed"
    return None, None


def _legacy_behind_ingest(latest_ingested_at: str | None, hub_dumped_at: Any) -> float | None:
    """PR #144's figure, for when the hub answered but the per-table
    measurement did not: the hub's newest ingest against the newest the
    replica holds (or the dump on the hub's clock, whichever is later)."""
    ing, ref = _utc(latest_ingested_at), _utc(hub_dumped_at)
    if ing is None or ref is None:
        return None
    held = _replica_latest_ingested_at()
    if held is not None:
        ref = max(ref, _utc(held) or ref)
    return (ing - ref).total_seconds()


def snapshot_freshness(
    latest_ingested_at: str | None,
    health: dict[str, Any] | None = None,
    *,
    lag: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """``snapshot_health()`` plus the replica's freshness per table and one
    overall ``state``:

    - ``stale``: this Mac's evidence says the scheduled full dump is
      missing, overdue (past ``SNAPSHOT_MAX_AGE_S``) or failed after its
      last success. The only state that turns ``ok`` false.
    - ``behind``: the hub answered and some table lacks a change older than
      ``SNAPSHOT_LAG_BEHIND_S``. Shown, never red: a hub ingest since the
      last dump is not a fault.
    - ``fresh``: the hub answered and every table it could measure is
      within that bound, with nothing cut short.
    - ``unknown``: the hub did not answer (or a table could not be
      measured, or a list was cut short); ``last_known`` carries the last
      measurement this Mac made.

    ``lag`` is ``measure_lag()``'s result, which ``drift.status_payload``
    computes on its own hub connection; this function never opens one.
    ``behind_ingest_seconds`` stays for older readers: the episode lag when
    measured, else PR #144's figure.
    """
    if health is None:
        health = snapshot_health()
    out = dict(health)
    sidecar = _read_sidecar()
    cursors: dict[str, dict[str, Any]] = {}
    try:
        con = sqlite3.connect(f"file:{snapshot_path()}?mode=ro", uri=True)
        try:
            cursors = _read_cursors(con)
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        cursors = {}
    errors = sidecar.get("write_errors") if isinstance(sidecar.get("write_errors"), dict) else {}
    measured = bool(lag and lag.get("measured"))
    lag_tables = (lag or {}).get("tables") or {}
    tables: dict[str, dict[str, Any]] = {}
    for name in FRESHNESS_TABLES:
        row: dict[str, Any] = {}
        cur_row = cursors.get(name) or {}
        for key in ("synced_at", "newest", "newest_id", "reconciled_at"):
            if cur_row.get(key) is not None:
                row[key] = cur_row[key]
        if isinstance(errors.get(name), dict):
            row["last_error"] = errors[name]
        if measured:
            row.update(lag_tables.get(name) or {"lag_seconds": None, "error": "not measured"})
        tables[name] = row
    out["tables"] = tables
    if isinstance(sidecar.get("refresh_error"), dict):
        out["refresh_error"] = sidecar["refresh_error"]

    state, reason = _local_state(health, sidecar)
    if measured:
        lags = [t.get("lag_seconds") for t in tables.values()]
        worst = max((v for v in lags if isinstance(v, (int, float))), default=0.0)
        unmeasured = [n for n, t in tables.items() if t.get("error")]
        truncated = [n for n, t in tables.items() if t.get("truncated")]
        unverified = [n for n, t in tables.items() if t.get("unverified")]
        out["lag_seconds"] = worst
        out["measured_at"] = lag.get("measured_at")
        if state is None:
            if worst > SNAPSHOT_LAG_BEHIND_S:
                state, reason = "behind", "snapshot_behind"
            elif unmeasured or truncated or unverified:
                state = "unknown"
                reason = ("table_unmeasured" if unmeasured else "measurement_truncated" if truncated
                          else "version_evidence_missing")
            else:
                state = "fresh"
        ep = tables["episodes"].get("lag_seconds")
        out["behind_ingest_seconds"] = ep if isinstance(ep, (int, float)) else None
        _write_last_known({
            "measured_at": lag.get("measured_at"),
            "checked_at": _utcnow_iso(),
            "refreshed_at": health.get("refreshed_at"),
            "state": state,
            "lag_seconds": worst,
            "complete": not unmeasured and not truncated and not unverified,
            # The hub's verdict alone, apart from this Mac's dump evidence:
            # what lets the recall lane trust an overdue dump (snapshot_is_fresh).
            "in_step": worst <= SNAPSHOT_LAG_BEHIND_S and not unmeasured and not truncated and not unverified,
            "tables": {n: {k: t.get(k) for k in ("lag_seconds", "missing", "truncated", "unverified", "error")
                           if k in t}
                       for n, t in tables.items()},
        })
    else:
        out["behind_ingest_seconds"] = _legacy_behind_ingest(latest_ingested_at, meta().get("hub_dumped_at"))
        last = _read_last_known()
        if last is not None:
            out["last_known"] = last
        if state is None:
            state = "unknown"
            reason = (lag or {}).get("reason") or "hub_not_measured"
    out["state"] = state
    if reason:
        out["reason"] = reason
    if state == "stale":
        out["ok"] = False
    return out


def stale_fields() -> dict[str, Any]:
    m = meta()
    out = {
        "stale": True,
        # When the copy last caught up with the hub, not the dump alone.
        "snapshot_refreshed_at": caught_up_at(m),
    }
    if facts_hidden():
        # No commitments, decisions or deliverables until the next fact copy
        # certifies them (design section 6.2).
        out["facts_hidden"] = True
    return out


def _topic_timestamp_expr(con: sqlite3.Connection) -> str:
    """Hub event-date precedence, including replicas predating event_at."""
    columns = _snapshot_table_columns(con, "topics")
    available = [name for name in ("event_at", "updated_at", "created_at") if name in columns]
    return ("COALESCE(" + ", ".join(available) + ")" if len(available) > 1
            else available[0] if available else "NULL")


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
    indexed_tokens_only: bool = False,
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
    if indexed_tokens_only and any(len(tok) >= _FTS_MIN_TOKEN for tok in tokens):
        # A two-digit number (a date or a time) is too short for the trigram
        # index, and one such token sent the whole search to a LIKE scan of
        # every row: 100 times slower, past the prompt lane's 1.2 s on a large
        # replica. The lane matches on the words the index can find.
        tokens = [tok for tok in tokens if len(tok) >= _FTS_MIN_TOKEN]
    con = open_snapshot()
    proj_keys = snapshot_project_group(con, project.strip())[1] if project else []
    # SQLite's LIKE is wildcard/case behaviour, not the callers' exact
    # Python filter semantics. Register deterministic predicates before the
    # LIMIT so a rejected row cannot consume the result budget.
    con.create_function("khipu_project_match", 2, lambda value, wanted: any(w.lower() in str(value or "").lower() for w in str(wanted or "").split("\x1f")))
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
        topic_ts = _topic_timestamp_expr(con)
        topic_time_clause = ""
        topic_time_params: list[Any] = []
        if time_filtered:
            topic_time_clause = f" AND khipu_in_range({topic_ts}, ?, ?)"
            topic_time_params.extend((since_dt.isoformat() if since_dt else None, until_dt.isoformat() if until_dt else None))
        sql = f"""
            SELECT 'topic' AS kind, slug AS id, COALESCE(title, slug) AS label,
                   body AS snippet, {topic_ts} AS ts
            FROM topics
            WHERE deleted_at IS NULL AND ({topic_where}){topic_time_clause}
            ORDER BY {topic_order}
            LIMIT ?
        """
        rows = fts_rows(
            f"""
            SELECT 'topic' AS kind, slug AS id, COALESCE(title, slug) AS label,
                   body AS snippet, {topic_ts} AS ts
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
                    **({"rank_text": f"{r[1]} {r[2] or ''} {r[3] or ''}"} if indexed_tokens_only else {}),
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
            identity_params.append("\x1f".join(snapshot_project_group(con, project.strip())[1]))
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
                   {project_expr} AS proj, {harness_expr} AS harn,
                   COALESCE(topics, '') || char(10) || COALESCE(decisions, '') || char(10) ||
                   COALESCE(preferences, '') || char(10) || COALESCE(people, '') AS extracts
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
                   {project_expr} AS proj, {harness_expr} AS harn,
                   COALESCE(topics, '') || char(10) || COALESCE(decisions, '') || char(10) ||
                   COALESCE(preferences, '') || char(10) || COALESCE(people, '') AS extracts
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
            if project and not any(k.lower() in (r[6] or "").lower() for k in proj_keys):
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
                    **({"rank_text": f"{r[2] or ''} {r[8] or ''}"} if indexed_tokens_only else {}),
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
    """``results`` plus each capture still waiting in the outbox that passes
    the query and filters and whose source has not been forgotten. When the
    forgotten-source registry cannot be read, no pending capture is added."""
    from khipu.outbox import jobs
    from khipu.snippets import LABEL_LIMIT, SNIPPET_LIMIT, clip_snippet
    from khipu.tombstones import read_gate

    merged = list(results)
    seen: set[str] = {f"{r.get('kind')}:{r.get('id')}" for r in merged}
    from khipu.search_text import parse_time_filter, search_tokens
    if kind not in (None, "episode"):
        return merged
    since_dt = parse_time_filter(since) if since else None
    until_dt = parse_time_filter(until) if until else None
    query_tokens = [token.lower() for token in search_tokens(query or "")]
    found: list[tuple[Path, dict[str, Any], str]] = []
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
            found.append((jp, payload, summary))
        except (OSError, ValueError, TypeError):
            continue
    if not found:
        return merged
    try:
        # Read after the jobs, so a forget published before this point is seen.
        gate = read_gate()
        found = [m for m in found if gate.allows(m[1])]
    except Exception:  # noqa: BLE001 — RegistryError or anything else: fail closed
        return merged
    for jp, payload, summary in found:
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
                ORDER BY (CASE WHEN e.src IN ({ph}) THEN e.dst ELSE e.src END) ASC, e.type ASC
                LIMIT ?
                """,
                (*aliases, *aliases, *aliases, limit),
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
            batch = sorted(frontier)
            rows = con.execute(
                f"""
                SELECT src, dst, type FROM edges
                WHERE src IN ({ph}) OR dst IN ({ph})
                """,
                (*batch, *batch),
            ).fetchall()
            candidates = sorted(
                (other, etype, node)
                for src, dst, etype in rows for node in batch
                for other in ([dst] if src == node else [src] if dst == node else [])
            )
            next_frontier: set[str] = set()
            for other, etype, node in candidates:
                if other in seen_nodes:
                    continue
                seen_nodes.add(other)
                walk.append((other, node, etype, hop))
                next_frontier.add(other)
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
    carries ``deleted_at`` (``_create_schema``), so no gating is needed here.
    When the replica carries a ``decisions`` table, also attach
    ``decision_states`` / ``validity`` so hub-down ``khipu_get`` matches the
    live path."""
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
    out = {
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
    out.update(_decision_states_for_episode_snapshot(con, episode_id))
    return out


def _decision_states_for_episode_snapshot(
    con: sqlite3.Connection, episode_id: int
) -> dict[str, Any]:
    """Replica twin of ``decisions.decision_states_for_episode`` for rows
    owned by this episode id only. Unlike the hub path
    (``briefs._decision_rows`` / ``decision_states_for_episode``), this does
    not attach decisions by normalized text within the project and dedupe
    window — a restatement whose row lives on an earlier episode therefore
    still has empty ``decision_states`` when the hub is down. Empty/zeroed
    when the table is absent."""
    empty = {
        "decision_states": [],
        "validity": {"current": 0, "superseded": 0, "retracted": 0},
    }
    cols = _snapshot_table_columns(con, "decisions")
    if not cols or "episode_id" not in cols:
        return empty
    select = ["id", "text", "superseded_by"]
    has_retract = "retracted_at" in cols
    if has_retract:
        select.append("retracted_at")
    fence = forget_fence_sql(con, "decisions", "episode_id")
    try:
        rows = con.execute(
            f"SELECT {', '.join(select)} FROM decisions WHERE episode_id = ?{fence} ORDER BY id",
            (episode_id,),
        ).fetchall()
    except sqlite3.Error:
        return empty
    from khipu.decisions import state_of

    states = []
    counts = {"current": 0, "superseded": 0, "retracted": 0}
    for row in rows:
        payload = {"id": row[0], "text": row[1], "superseded_by": row[2]}
        if has_retract:
            payload["retracted_at"] = row[3]
        state = state_of(payload)
        counts[state] += 1
        states.append({
            "id": payload["id"],
            "text": payload["text"],
            "state": state,
            "superseded_by": payload.get("superseded_by"),
        })
    return {"decision_states": states, "validity": counts}


def topic_detail_snapshot(slug: str, *, offset: int = 0,
                          max_bytes: int = PAGE_BYTES_DEFAULT) -> dict[str, Any] | None:
    """The replica's answer to ``activity.topic_detail``: same arguments, same
    page and fields. Only the page's bytes are read from the blob."""
    slug = (slug or "").strip()
    if not slug:
        return None
    offset, max_bytes = page_args(offset, max_bytes)
    con = open_snapshot()
    # A replica written before content_hash was exported has no such column.
    hash_col = (
        "content_hash" if "content_hash" in _snapshot_table_columns(con, "topics")
        else "NULL"
    )
    row = con.execute(
        f"""
        SELECT slug, title,
               substr(CAST(body AS BLOB), ?, ?), length(CAST(body AS BLOB)),
               status, created_at, updated_at, links, source_path, {hash_col}
        FROM topics WHERE slug = ? AND deleted_at IS NULL
        """,
        (db_start(offset), fetch_bytes(max_bytes), slug),
    ).fetchone()
    if not row:
        return None
    page = page_utf8(row[2], offset, max_bytes, row[3])
    return {
        "id": row[0],
        "slug": row[0],
        "title": row[1],
        "status": row[4],
        "created_at": row[5],
        "updated_at": row[6],
        "links": json.loads(row[7] or "[]"),
        "source_path": row[8],
        "content_hash": row[9],
        **page,
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
        "hub_snapshot": snapshot_freshness(None),
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
    proj_keys = snapshot_project_group(con, project.strip())[1] if project else []
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
                f"SELECT COALESCE(title, slug), {_topic_timestamp_expr(con)} "
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
                if project and not any(k.lower() in (row_proj or "").lower() for k in proj_keys):
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


def _recent_measurement(health: Mapping[str, Any]) -> dict[str, Any] | None:
    """The last measurement against the hub when it was made of this same
    dump within ``LAST_KNOWN_TRUST_S``; else None."""
    last = _read_last_known()
    if not last or last.get("refreshed_at") != health.get("refreshed_at"):
        return None
    # checked_at is this Mac's clock, compared only with this Mac's clock;
    # measured_at is the hub's and never is.
    checked = _utc(last.get("checked_at"))
    if checked is None or not 0 <= (datetime.now(timezone.utc) - checked).total_seconds() <= LAST_KNOWN_TRUST_S:
        return None
    # A pull changes the copy after a measurement was made of it: the figure
    # no longer describes the copy, so it neither distrusts nor trusts it.
    pulled = _utc(_read_sidecar().get("last_pull_at"))
    if pulled is not None and checked < pulled:
        return None
    return last


def snapshot_is_fresh(*, max_age_s: int = SNAPSHOT_MAX_AGE_S) -> tuple[bool, dict[str, Any]]:
    """(is_fresh, health) for the per-prompt recall lane, from this Mac's
    evidence only (it runs inside a hook and never reaches the hub).

    A recent measurement of this same dump (``_recent_measurement``)
    decides first: one that found a table behind is never trusted, whatever
    the dump's age (``health["distrusted_by"] == "measurement"``), and one
    that found every table in step with nothing cut short is trusted even
    past ``max_age_s`` (``health["trusted_by"] == "measurement"``). With no
    such verdict, a dump younger than ``max_age_s`` is trusted. A missing
    file, or a dump with no time on record, is never trusted. ``health``
    carries the age and reason for logging either way.
    """
    health = snapshot_health()
    if not health.get("exists"):
        return False, health
    age = health.get("age_seconds")
    if age is None:
        return False, health
    last = _recent_measurement(health)
    lag = (last or {}).get("lag_seconds")
    if last is not None and isinstance(lag, (int, float)) and lag > SNAPSHOT_LAG_BEHIND_S:
        health["distrusted_by"] = "measurement"
        health["last_known"] = last
        return False, health
    if age <= max_age_s:
        return True, health
    if last is not None and last.get("in_step") is True:
        health["trusted_by"] = "measurement"
        health["last_known"] = last
        return True, health
    return False, health


def prompt_recall_snapshot_status() -> dict[str, Any]:
    """R1 follow-up (doctor): is the local replica usable for the per-prompt
    recall lane right now? Never red on its own — khipu.recall_prompt
    already degrades safely to the hub when this is false — but that
    degrade is otherwise invisible: every prompt then pays the slower,
    sometimes-timing-out hub round trip with no sign anywhere that it is
    happening. This is the sign.
    """
    fresh, health = snapshot_is_fresh()
    schema = replica_schema_mismatch(health)
    if fresh:
        return {"ok": True, "fresh": True, **schema}
    return {
        "ok": True,
        "fresh": False,
        "reason": (
            "prompt-time recall is running against the hub and may time "
            "out; press Refresh offline copy in the app"
        ),
        "exists": bool(health.get("exists")),
        "age_seconds": health.get("age_seconds"),
        **schema,
    }


def replica_schema_mismatch(health: Mapping[str, Any]) -> dict[str, Any]:
    """Amber, never red, while the replica on disk was built at another
    schema than this code's: the replica job rebuilds it on its next check
    (khipu.replica_heal), so the warning clears by itself. Empty when the
    schemas match or the replica has none on record."""
    disk = health.get("schema_version")
    if not health.get("exists") or disk is None or disk == SNAPSHOT_SCHEMA_VERSION:
        return {}
    which = "an older" if isinstance(disk, int) and disk < SNAPSHOT_SCHEMA_VERSION else "a different"
    return {
        "level": "amber",
        "schema_on_disk": disk,
        "schema_expected": SNAPSHOT_SCHEMA_VERSION,
        "message": (
            f"This Mac's offline copy was built for {which} version of Khipu "
            f"(layout {disk}; this version uses {SNAPSHOT_SCHEMA_VERSION}). "
            "It is rebuilt at the next check of the offline copy; until then "
            "recall before each prompt may miss recent work."
        ),
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


def _close_matrix_reader(reader: dict[str, Any] | None) -> None:
    if reader and reader.get("con") is not None:
        reader["con"].close()
        reader.clear()


class _MatrixCache(dict):
    def clear(self) -> None:
        _close_matrix_reader(self.get("reader"))
        super().clear()


_MATRIX_CACHE: dict[str, Any] = _MatrixCache()
_MATRIX_RELOADING = False


def _matrix_file_identity(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_dev, st.st_ino


def _matrix_tombstone_sources(con: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return [(table, key, knd)
            for table, key, knd in (("episodes", "id", "episode"), ("topics", "slug", "topic"))
            if {key, "deleted_at"} <= _snapshot_table_columns(con, table)]


def _open_matrix_reader(path: Path) -> dict[str, Any]:
    identity = _matrix_file_identity(path)
    con = open_snapshot(shared=True)
    try:
        if _matrix_file_identity(path) != identity:
            raise OSError("replica replaced while opening matrix reader")
        return {"con": con, "identity": identity,
                "sources": _matrix_tombstone_sources(con),
                "schema_version": con.execute("PRAGMA schema_version").fetchone()[0]}
    except BaseException:
        con.close()
        raise


def _current_matrix_rows(path: Path, rows: list, reader: dict[str, Any]) -> list:
    """Called only under _MATRIX_LOCK; no vector reload or held read transaction.

    The existing load connection also observes current tombstones. data_version
    notices commits by other connections, including WAL-only changes; replacing
    the file reopens this lightweight reader before any candidate budget.
    """
    if not reader:
        return rows  # A supplied matrix without an underlying snapshot reader.
    if reader["identity"] != _matrix_file_identity(path):
        replacement = _open_matrix_reader(path)
        _close_matrix_reader(reader)
        reader.update(replacement)
    con = reader["con"]
    version = con.execute("PRAGMA data_version").fetchone()[0]
    if reader.get("version") != version or "deleted" not in reader:
        schema_version = con.execute("PRAGMA schema_version").fetchone()[0]
        if reader.get("schema_version") != schema_version:
            reader.update(sources=_matrix_tombstone_sources(con), schema_version=schema_version)
        deleted = {}
        for table, key, knd in reader["sources"]:
            deleted[knd] = {str(r[0]) for r in con.execute(
                f"SELECT {key} FROM main.{table} WHERE deleted_at IS NOT NULL"
            ).fetchall()}
        reader.update(version=version, deleted=deleted)
    deleted = reader["deleted"]
    return [r for r in rows if str(r[2]) not in deleted.get(r[1], set())]


def _load_matrix(
    path: Path, profile: str, kind: str | None, *, deadline: float | None = None,
    reader: dict[str, Any] | None = None,
) -> tuple[list[tuple[Any, ...]], dict[int, tuple[str, str, int, str]]]:
    opened = _open_matrix_reader(path)
    con = opened["con"]
    retained = False
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
        if reader is not None:
            # A load deadline must not poison this long-lived metadata reader.
            con.set_progress_handler(None, 0)
            reader.update(opened)
            retained = True
    finally:
        if not retained:
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
    reader: dict[str, Any] = {}
    try:
        rows, by_rowid = _load_matrix(path, profile, kind, reader=reader)
    except Exception as exc:  # noqa: BLE001 — the stale generation keeps serving
        _log(f"replica matrix background reload failed: {type(exc).__name__}: {exc}")
        rows = None
    with _MATRIX_LOCK:
        old = _MATRIX_CACHE.get("key")
        # A synchronous load for another profile may have replaced the entry
        # meanwhile; only a newer generation of the same one is swapped in.
        if rows is not None and old is not None and old[0] == key[0] and old[3:] == key[3:]:
            _close_matrix_reader(_MATRIX_CACHE.get("reader"))
            _MATRIX_CACHE.update({"key": key, "rows": rows, "by_rowid": by_rowid, "reader": reader})
        else:
            _close_matrix_reader(reader)
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
    from the previous vector generation while exactly one daemon thread loads
    the new one outside the lock. Current tombstones are observed separately
    through the retained reader before any cached candidates are returned."""
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
            return (_current_matrix_rows(path, _MATRIX_CACHE["rows"], _MATRIX_CACHE.get("reader", {})),
                    _MATRIX_CACHE["by_rowid"])
        if key is not None and cached is not None and cached[0] == key[0] and cached[3:] == key[3:]:
            if not _MATRIX_RELOADING:
                _MATRIX_RELOADING = True
                threading.Thread(
                    target=_reload_matrix, args=(path, profile, kind, key), daemon=True,
                ).start()
            return (_current_matrix_rows(path, _MATRIX_CACHE["rows"], _MATRIX_CACHE.get("reader", {})),
                    _MATRIX_CACHE["by_rowid"])
        reader: dict[str, Any] = {}
        rows, by_rowid = _load_matrix(path, profile, kind, deadline=deadline, reader=reader)
        _MATRIX_CACHE.clear()
        if key is not None:
            _MATRIX_CACHE.update({"key": key, "rows": rows, "by_rowid": by_rowid, "reader": reader})
        else:
            _close_matrix_reader(reader)
        return _current_matrix_rows(path, rows, reader), by_rowid
    finally:
        _MATRIX_LOCK.release()


def warm_matrix(profile: str, kind: str | None = None) -> None:
    """Load the replica's vectors for ``profile`` into the generation cache,
    so a scan that starts later finds them there (or waits on this load under
    the lock instead of starting its own). Never raises: the scan reports any
    failure itself."""
    try:
        _matrix_rows(snapshot_path(), profile, kind)
    except Exception as exc:  # noqa: BLE001 — a warm-up only
        _log(f"replica matrix warm-up skipped: {type(exc).__name__}")


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
    # Pending erasures are a separate owner-choice fence even while a SQLite
    # source row is still live. _matrix_rows fences current DB tombstones.
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


def _topic_routing_metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    """Derive legacy-note metadata when writing a replica, never in search.

    Imported notes already carry this boolean. Old hub rows acquire it on
    replica import as well, without adding body text to metadata or payloads.
    """
    item = dict(row)
    if "body" not in row or "frontmatter" not in row:
        return item
    try:
        fm = row["frontmatter"]
        fm = json.loads(fm) if isinstance(fm, str) else fm
        if not isinstance(fm, dict):
            return item
        if type(fm.get("_routing_state_claim")) is bool:
            return item
        if not fm.get("note_source") and not str(row.get("slug") or "").startswith("note:"):
            return item
        from khipu.routing_scanner import has_routing_claim

        item["frontmatter"] = {
            **fm, "_routing_state_claim": has_routing_claim(
                str(fm.get("note_source") or row.get("slug") or ""),
                str(row.get("body") or ""),
            ),
        }
    except Exception:  # noqa: BLE001 — advisory; never log source or exception text
        pass
    return item


def snapshot_filter_live_sources(
    con: sqlite3.Connection, rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Fence source liveness before fusion limits, without metadata enrichment.

    Real snapshot readers always return SQLite connections. A synthetic caller
    supplying a metadata stand-in can keep its own source contract.
    """
    if not rows or not isinstance(con, sqlite3.Connection):
        return list(rows)
    live: dict[str, set[str]] = {}
    for knd, table, key in (("episode", "episodes", "id"), ("topic", "topics", "slug")):
        ids = sorted({str(r["id"]) for r in rows if r.get("kind") == knd})
        if not ids:
            continue
        columns = _snapshot_table_columns(con, table)
        live[knd] = set()
        if key not in columns:
            continue
        predicate = " AND deleted_at IS NULL" if "deleted_at" in columns else ""
        live[knd] = {str(r[0]) for r in con.execute(
            f"SELECT {key} FROM {table} WHERE CAST({key} AS TEXT) IN "
            f"({','.join('?' for _ in ids)}){predicate}", ids,
        )}
    return [r for r in rows if r.get("kind") not in live
            or str(r["id"]) in live[r["kind"]]]


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
            ts_expr = _topic_timestamp_expr(con)
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
                routing_claim = None
                if frontmatter:
                    try:
                        fm = json.loads(frontmatter)
                        proj = fm.get("project")
                        flag = fm.get("_routing_state_claim")
                        if type(flag) is bool:
                            routing_claim = flag
                    except (ValueError, AttributeError, TypeError):
                        proj = None
                meta[("topic", slug)] = {
                    "ts": ts, "status": status or "active", "project": proj,
                    "deleted": deleted is not None, "superseded_by": superseded_by,
                    "_routing_state_claim": routing_claim,
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
        if type(m.get("_routing_state_claim")) is bool:
            item["_routing_state_claim"] = m["_routing_state_claim"]
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


def _filter_expanded_snapshot_rows(
    con, rows, *, kind=None, since=None, until=None, project=None,
    session_id=None, harness=None, t3_thread=None, via=None,
):
    """Graph neighbours must pass the same requested filters as seed rows."""
    from khipu.search_text import parse_time_filter

    rows = _filter_t3_snapshot_rows(con, rows, t3_thread, via)
    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
    episode_only = bool(project or session_id or harness or t3_thread or via)
    if episode_only:
        rows = [r for r in rows if r.get("kind") == "episode"]
    since_dt = parse_time_filter(since) if since else None
    until_dt = parse_time_filter(until) if until else None
    if not (episode_only or since_dt is not None or until_dt is not None):
        return rows

    # Outbox rows have no replica source yet; merge_outbox_episodes has
    # already checked their metadata against these same filters.
    local = snapshot_row_metadata(con, [r for r in rows if not r.get("outbox")])
    by_key = {(r["kind"], str(r["id"])): r for r in local}
    episodes = {}
    ids = [str(r["id"]) for r in local if r.get("kind") == "episode"]
    if ids and episode_only:
        cols = _snapshot_table_columns(con, "episodes")
        sid_expr = "session_id" if "session_id" in cols else "NULL"
        harness_expr = "harness" if "harness" in cols else "NULL"
        for eid, sid, harn in con.execute(
            f"SELECT CAST(id AS TEXT), {sid_expr}, {harness_expr} FROM episodes "
            f"WHERE CAST(id AS TEXT) IN ({','.join('?' for _ in ids)})", ids,
        ):
            episodes[str(eid)] = (str(sid or ""), harn)
    projects = snapshot_project_group(con, project.strip())[1] if project else []
    node_times = {}
    node_ids = [str(r["id"]) for r in local if r.get("kind") == "node"]
    if node_ids and (since_dt is not None or until_dt is not None):
        if "built_at" in _snapshot_table_columns(con, "nodes"):
            node_times = dict(con.execute(
                f"SELECT id, built_at FROM nodes WHERE id IN ({','.join('?' for _ in node_ids)})",
                node_ids,
            ))
    out = []
    for row in rows:
        if row.get("outbox"):
            out.append(row)
            continue
        item = by_key.get((row["kind"], str(row["id"])))
        if item is None:
            continue
        if episode_only:
            sid, harn = episodes.get(str(item["id"]), ("", None))
            if project and not any(p.lower() in str(item.get("project") or "").lower() for p in projects):
                continue
            if session_id and not sid.startswith(session_id):
                continue
            if harness and (harn or sid.split(":", 1)[0]) != harness:
                continue
        if since_dt is not None or until_dt is not None:
            value = node_times.get(str(item["id"])) if item["kind"] == "node" else item.get("ts")
            ts = _parse_snapshot_ts(value)
            if ts is None or (since_dt is not None and ts < since_dt) or (until_dt is not None and ts > until_dt):
                continue
        out.append(item)
    return out



def _snapshot_search_signals(query: str, rows: list[dict[str, Any]]) -> str:
    from khipu.embed import search_confidence
    from khipu.search_text import _row_text, search_tokens, token_hit_count

    tokens = search_tokens(query)
    for rank, row in enumerate(rows, 1):
        row.setdefault("score", 1.0 / (60 + rank))
        row.setdefault("cosine", None)
        row["lexical_hits"] = max(int(row.get("lexical_hits") or 0),
                                  token_hit_count(_row_text(row), tokens))
    return search_confidence(rows, token_count=len(tokens))


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
        results = _filter_expanded_snapshot_rows(
            con, results, kind=kind, since=since, until=until, project=project,
            session_id=session_id, harness=harness, t3_thread=t3_thread, via=via,
        )
        results = enrich_search_results_snapshot(con, results)
        results = _annotate_snapshot_validity(con, results)
        out = {
            "query": query,
            "mode": "semantic",
            "results": results,
            "confidence": _snapshot_search_signals(query, results),
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
    results = _filter_expanded_snapshot_rows(
        con, results, kind=kind, since=since, until=until, project=project,
        session_id=session_id, harness=harness, t3_thread=t3_thread, via=via,
    )
    results = enrich_search_results_snapshot(con, results)
    results = _annotate_snapshot_validity(con, results)
    out = {
        "query": query,
        "mode": "literal",
        "results": results[:limit],
        "confidence": _snapshot_search_signals(query, results[:limit]),
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
