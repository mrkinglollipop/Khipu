# --bypass-harness (sonnet lane) — authored directly by the dispatched on-sub
# Sonnet build agent (brief: do not delegate); no further agent to route to.
"""Library sources: a folder of .txt/.md files indexed as a search space of its
own (scope: docs/plans/2026-10-07-library-sources-byoe.md, Session B).

A library is a row of ``library_sources`` (name, root, embedding profile,
enabled), separate from the graph-membership registry in ``sources.json``.
This module owns the whole write side:

* ``scan``      walk the root, record documents and chunks, no model call;
* ``backfill``  embed the chunks that have no vector under the source's
                profile (or whose text changed since they were embedded);
* ``import_index`` read vectors computed elsewhere (graphify's SQLite
                ``embeddings`` table, or JSONL with the same fields) so nobody
                re-embeds what they already paid for;
* ``add_source/list_sources/source_status/remove_source/set_enabled`` the
                registry;
* ``backfill_all`` the nightly hook; ``summary`` the doctor block.

Every function takes an open connection and commits its own batches. Failures
that are the caller's to fix raise ``LibraryError``; the CLI turns that into
``{"ok": false, "error": ...}`` and exit 2.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from khipu import embed
from khipu import profiles

NAME_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
EXTENSIONS = (".txt", ".md")
MAX_FILE_BYTES = 64 * 1024 * 1024
SCAN_COMMIT_EVERY = 25          # documents per commit
IMPORT_BATCH = 500              # rows per executemany + commit
STATUS_SAMPLE = 10
MAX_TITLE = 500
MAX_AUTHOR = 200
MAX_TAGS = 50
MAX_TAG_CHARS = 80
CORRUPT_RATIO = 0.01            # same threshold as the biblical library's consult.py


class LibraryError(Exception):
    """A refusal or validation failure: the message says what to fix."""


def _log(msg: str) -> None:
    print(f"[khipu-library] {msg}", file=sys.stderr, flush=True)


@dataclass(frozen=True)
class Source:
    name: str
    root: str
    profile: str
    enabled: bool = True


# ---- small helpers --------------------------------------------------------------

def validate_name(name: str) -> str:
    if not NAME_RE.match(name or ""):
        raise LibraryError(f"library name {name!r} must match [a-z0-9_-]{{1,40}}")
    return name


def _clean(text: str) -> str:
    """Postgres TEXT cannot hold NUL, and a lone surrogate cannot be encoded."""
    return text.replace("\x00", "").encode("utf-8", "replace").decode("utf-8")


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _pct(done: int, total: int) -> float:
    """Never round a gap away: only true completeness reads 100."""
    if not total:
        return 0.0
    if done >= total:
        return 100.0
    return min(99.9, int(1000 * done / total) / 10)


def _iso(ts: Any) -> str | None:
    if ts is None:
        return None
    return ts.isoformat() if hasattr(ts, "isoformat") else str(ts)


def is_corrupt_chunk_text(chunk_text: str | None) -> bool:
    """True when an index row's chunk text looks like mis-extracted binary.

    Copied (not imported) from ``_is_corrupt_embedding_chunk_text`` in the
    biblical library's consult.py: control characters (anything below 0x20
    except newline, return, tab) above 1% of the text. Extended with the same
    ratio for U+FFFD, the replacement character a bad decode leaves behind.
    """
    s = chunk_text or ""
    if not s:
        return False
    ctrl = sum(1 for c in s if ord(c) < 32 and c not in "\n\r\t")
    if ctrl / len(s) > CORRUPT_RATIO:
        return True
    return s.count("�") / len(s) > CORRUPT_RATIO


# ---- front matter ---------------------------------------------------------------

_FM_RE = re.compile(
    r"\A﻿?---[ \t]*\r?\n(?:(.*?)\r?\n)?---[ \t]*(?:\r?\n|\Z)", re.S
)
_KEY_RE = re.compile(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$")


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v.strip()


def _as_list(raw: str, items: list[str]) -> list[str]:
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        return [_unquote(p) for p in raw[1:-1].split(",") if _unquote(p)]
    if raw:
        return [_unquote(p) for p in raw.split(",") if _unquote(p)]
    return [_unquote(i) for i in items if _unquote(i)]


def parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """(fields, body). ``fields`` holds ``title``, ``author`` and ``tags`` only
    when present; a file without a closed front-matter block returns ({}, text).
    A small YAML subset: ``key: value``, ``key: [a, b]``, ``key: a, b`` and a
    ``- item`` list under a bare key."""
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    block = m.group(1) or ""
    body = text[m.end():]
    raw: dict[str, tuple[str, list[str]]] = {}
    current: str | None = None
    for line in block.splitlines():
        km = _KEY_RE.match(line)
        if km and not line.startswith((" ", "\t", "-")):
            current = km.group(1).lower()
            raw[current] = (km.group(2), [])
        elif current is not None and line.strip().startswith("-"):
            raw[current][1].append(line.strip()[1:].strip())
    out: dict[str, Any] = {}
    title = _unquote(raw["title"][0]) if "title" in raw else ""
    if title:
        out["title"] = title
    if "author" in raw:
        a_raw, a_items = raw["author"]
        authors = _as_list(a_raw, a_items)
        if authors:
            out["author"] = ", ".join(authors)
    for key in ("tags", "topic_tags"):
        if key in raw:
            tags = _as_list(raw[key][0], raw[key][1])
            if tags:
                out["tags"] = tags
                break
    return out, body


def document_metadata(text: str, rel_path: str) -> tuple[str, str | None, list[str], str]:
    """(title, author, tags, body): front matter wins, else the path
    (title = file stem, author = first folder under the root, tags = [])."""
    fm, body = parse_front_matter(text)
    parts = PurePosixPath(rel_path).parts
    title = fm.get("title") or PurePosixPath(rel_path).stem
    author = fm.get("author") or (parts[0] if len(parts) > 1 else None)
    tags: list[str] = []
    for t in fm.get("tags", []):
        t = _clean(t)[:MAX_TAG_CHARS]
        if t and t not in tags:
            tags.append(t)
    return (
        _clean(title)[:MAX_TITLE],
        _clean(author)[:MAX_AUTHOR] if author else None,
        tags[:MAX_TAGS],
        body,
    )


# ---- the walk -------------------------------------------------------------------

def _under(path_real: str, root_real: str) -> bool:
    return path_real == root_real or path_real.startswith(root_real.rstrip(os.sep) + os.sep)


def _iter_files(root: Path, skipped: dict[str, int]) -> Iterator[tuple[str, Path, os.stat_result]]:
    """(rel_posix, path, stat) for every eligible file under ``root``: .txt and
    .md, no dotfiles or hidden directories, no symlink that leaves the root,
    nothing over MAX_FILE_BYTES (counted in ``skipped``, not yielded)."""
    root_real = os.path.realpath(root)

    def skip(why: str) -> None:
        skipped[why] = skipped.get(why, 0) + 1

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for fn in sorted(filenames):
            if fn.startswith(".") or not fn.lower().endswith(EXTENSIONS):
                continue
            p = Path(dirpath) / fn
            try:
                if p.is_symlink() and not _under(os.path.realpath(p), root_real):
                    skip("symlink_outside_root")
                    continue
                st = p.stat()
            except OSError:
                skip("unreadable")
                continue
            if not os.path.isfile(p):
                skip("not_a_file")
                continue
            if st.st_size > MAX_FILE_BYTES:
                skip("too_large")
                continue
            yield p.relative_to(root).as_posix(), p, st


def eligible_file(root: Path, root_real: str, rel: str) -> Path | None:
    """The file ``rel`` names under ``root`` if a scan would also pick it up,
    else None (so an import can never create a document the next scan deletes)."""
    parts = PurePosixPath(rel).parts
    if not parts or any(p in ("..", ".", "/") or p.startswith(".") for p in parts):
        return None
    if not parts[-1].lower().endswith(EXTENSIONS):
        return None
    cur = root
    for part in parts[:-1]:
        cur = cur / part
        if cur.is_symlink():
            return None
    p = root.joinpath(*parts)
    try:
        if p.is_symlink() and not _under(os.path.realpath(p), root_real):
            return None
        if not p.is_file() or p.stat().st_size > MAX_FILE_BYTES:
            return None
    except OSError:
        return None
    return p


# ---- registry -------------------------------------------------------------------

_SRC_COLS = "name, root, profile, enabled"


def get_source(cur, name: str) -> Source:
    cur.execute(f"SELECT {_SRC_COLS} FROM library_sources WHERE name = %s", (name,))
    row = cur.fetchone()
    if not row:
        raise LibraryError(f"no library named {name!r}; see `khipu library list`")
    return Source(row[0], row[1], row[2], bool(row[3]))


def prepare_add(name: str, root: str) -> Path:
    """The checks that need no database: name shape, root is a directory."""
    validate_name(name)
    p = Path(os.path.abspath(os.path.expanduser(root)))
    if not p.is_dir():
        raise LibraryError(f"root {str(p)!r} does not exist or is not a directory")
    return p


def add_source(conn, name: str, root: str, profile: str) -> dict[str, Any]:
    p = prepare_add(name, root)
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM library_sources WHERE name = %s", (name,))
        if cur.fetchone():
            raise LibraryError(f"library {name!r} already exists")
        if profiles.load_spec(cur, profile) is None:
            raise LibraryError(
                f"unknown embedding profile {profile!r}; add it with `khipu embed profiles add`"
            )
        cur.execute(
            "INSERT INTO library_sources (name, root, profile) VALUES (%s, %s, %s)",
            (name, str(p), profile),
        )
    conn.commit()
    return {"ok": True, "name": name, "root": str(p), "profile": profile, "enabled": True}


_COUNTS_SQL = (
    "SELECT COUNT(DISTINCT d.id), COUNT(c.chunk_idx), COUNT(e.chunk_idx),"
    " COUNT(*) FILTER (WHERE e.content_hash IS NOT NULL AND e.content_hash <> c.content_hash)"
    " FROM library_documents d"
    " LEFT JOIN library_chunks c ON c.document = d.id"
    " LEFT JOIN library_embeddings e ON e.document = c.document"
    " AND e.chunk_idx = c.chunk_idx AND e.profile = %s"
    " WHERE d.source = %s"
)


def _counts(cur, src: Source) -> dict[str, Any]:
    cur.execute(_COUNTS_SQL, (src.profile, src.name))
    docs, chunks, embedded, stale = (int(x or 0) for x in cur.fetchone())
    return {
        "documents": docs, "chunks": chunks, "embedded": embedded,
        "missing": max(0, chunks - embedded), "stale": stale,
        "pct": _pct(max(0, embedded - stale), chunks),
    }


def _row(src: Source, counts: dict[str, Any]) -> dict[str, Any]:
    return {"name": src.name, "root": src.root, "profile": src.profile,
            "enabled": src.enabled, **counts}


def list_sources(conn) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_SRC_COLS} FROM library_sources ORDER BY name")
        sources = [Source(r[0], r[1], r[2], bool(r[3])) for r in cur.fetchall()]
        return [_row(s, _counts(cur, s)) for s in sources]


_JOIN_E = (
    " FROM library_chunks c JOIN library_documents d ON d.id = c.document"
    " LEFT JOIN library_embeddings e ON e.document = c.document"
    " AND e.chunk_idx = c.chunk_idx AND e.profile = %s WHERE d.source = %s"
)
_TODO_MISSING = " AND e.document IS NULL"
_TODO_STALE = (
    " AND (e.document IS NULL OR (e.content_hash IS NOT NULL"
    " AND e.content_hash <> c.content_hash))"
)
_ONLY_STALE = " AND e.content_hash IS NOT NULL AND e.content_hash <> c.content_hash"


def source_status(conn, name: str, *, sample: int = STATUS_SAMPLE) -> dict[str, Any]:
    with conn.cursor() as cur:
        src = get_source(cur, name)
        out = _row(src, _counts(cur, src))
        cur.execute("SELECT MAX(scanned_at) FROM library_documents WHERE source = %s", (name,))
        out["last_scan"] = _iso((cur.fetchone() or [None])[0])
        cur.execute(
            "SELECT MAX(e.built_at) FROM library_embeddings e"
            " JOIN library_documents d ON d.id = e.document"
            " WHERE d.source = %s AND e.profile = %s",
            (name, src.profile),
        )
        out["last_backfill"] = _iso((cur.fetchone() or [None])[0])
        for key, cond in (("sample_missing", _TODO_MISSING), ("sample_stale", _ONLY_STALE)):
            cur.execute(
                "SELECT DISTINCT d.rel_path" + _JOIN_E + cond
                + " ORDER BY d.rel_path LIMIT %s", (src.profile, name, sample),
            )
            out[key] = [r[0] for r in cur.fetchall()]
        return out


def remove_source(conn, name: str, *, yes: bool) -> dict[str, Any]:
    with conn.cursor() as cur:
        src = get_source(cur, name)
        counts = _counts(cur, src)
        if not yes:
            raise LibraryError(
                f"refusing to remove {name!r} without --yes: this deletes "
                f"{counts['documents']} documents, {counts['chunks']} chunks and "
                f"{counts['embedded']} vectors from the hub (the files stay)"
            )
        cur.execute("DELETE FROM library_sources WHERE name = %s", (name,))
    conn.commit()
    return {"ok": True, "removed": name, "documents": counts["documents"],
            "chunks": counts["chunks"], "embedded": counts["embedded"]}


def set_enabled(conn, name: str, enabled: bool) -> dict[str, Any]:
    with conn.cursor() as cur:
        get_source(cur, name)
        cur.execute("UPDATE library_sources SET enabled = %s WHERE name = %s", (enabled, name))
    conn.commit()
    return {"ok": True, "name": name, "enabled": enabled}


# ---- scan -----------------------------------------------------------------------

_UPSERT_DOC = (
    "INSERT INTO library_documents"
    " (source, rel_path, title, author, tags, bytes, mtime, content_hash, scanned_at)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())"
    " ON CONFLICT (source, rel_path) DO UPDATE SET"
    " title = EXCLUDED.title, author = EXCLUDED.author, tags = EXCLUDED.tags,"
    " bytes = EXCLUDED.bytes, mtime = EXCLUDED.mtime,"
    " content_hash = EXCLUDED.content_hash, scanned_at = now()"
    " RETURNING id"
)
_INSERT_CHUNK = (
    "INSERT INTO library_chunks (document, chunk_idx, chunk_text, content_hash)"
    " VALUES (%s, %s, %s, %s)"
    " ON CONFLICT (document, chunk_idx) DO UPDATE SET"
    " chunk_text = EXCLUDED.chunk_text, content_hash = EXCLUDED.content_hash"
)
_INSERT_EMBEDDING = (
    "INSERT INTO library_embeddings"
    " (profile, document, chunk_idx, embedding, content_hash, built_at)"
    " VALUES (%s, %s, %s, %s::vector, %s, now())"
    " ON CONFLICT (profile, document, chunk_idx) DO UPDATE SET"
    " embedding = EXCLUDED.embedding, content_hash = EXCLUDED.content_hash, built_at = now()"
)


def _upsert_document(cur, source: str, rel: str, title: str, author: str | None,
                     tags: list[str], size: int, st: os.stat_result, digest: str) -> int:
    mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    cur.execute(_UPSERT_DOC, (source, rel, title, author, tags, size, mtime, digest))
    return int(cur.fetchone()[0])


def scan(conn, name: str) -> dict[str, Any]:
    """Walk the root and make ``library_documents`` / ``library_chunks`` match
    the files. A document is re-chunked only when its content hash changed
    (its old chunks, and their embeddings, go first); documents whose file is
    gone are deleted. No model call."""
    with conn.cursor() as cur:
        src = get_source(cur, name)
        root = Path(src.root)
        if not root.is_dir():
            raise LibraryError(
                f"root {src.root!r} is not a directory (is the volume mounted?); "
                "nothing was changed"
            )
        cur.execute(
            "SELECT id, rel_path, content_hash FROM library_documents WHERE source = %s", (name,)
        )
        existing = {r[1]: (int(r[0]), r[2]) for r in cur.fetchall()}
        skipped: dict[str, int] = {}
        seen: set[str] = set()
        added = updated = unchanged = pending = 0
        for rel, path, st in _iter_files(root, skipped):
            seen.add(rel)
            try:
                data = path.read_bytes()
            except OSError:
                skipped["unreadable"] = skipped.get("unreadable", 0) + 1
                continue
            digest = hashlib.sha256(data).hexdigest()
            prior = existing.get(rel)
            if prior and prior[1] == digest:
                unchanged += 1
                continue
            title, author, tags, body = document_metadata(
                data.decode("utf-8", errors="replace"), rel
            )
            doc_id = _upsert_document(cur, name, rel, title, author, tags, len(data), st, digest)
            if prior:
                cur.execute("DELETE FROM library_chunks WHERE document = %s", (doc_id,))
                updated += 1
            else:
                added += 1
            rows = []
            for idx, chunk in embed.chunk_text_indexed(_clean(body)):
                rows.append((doc_id, idx, chunk, _md5(chunk)))
            if rows:
                cur.executemany(_INSERT_CHUNK, rows)
            pending += 1
            if pending >= SCAN_COMMIT_EVERY:
                conn.commit()
                pending = 0
        gone = [existing[rel][0] for rel in existing if rel not in seen]
        if gone and not seen:
            conn.rollback()
            raise LibraryError(
                f"{src.root!r} has no .txt or .md files but {len(gone)} documents are "
                "indexed; refusing to delete them all (use `khipu library remove` if the "
                "folder was emptied on purpose)"
            )
        if gone:
            cur.execute("DELETE FROM library_documents WHERE id = ANY(%s)", (gone,))
        cur.execute("UPDATE library_documents SET scanned_at = now() WHERE source = %s", (name,))
        conn.commit()
        counts = _counts(cur, src)
    return {
        "ok": True, "source": name, "added": added, "updated": updated,
        "removed": len(gone), "unchanged": unchanged,
        "skipped": sum(skipped.values()), "skipped_detail": skipped,
        "documents": counts["documents"], "chunks": counts["chunks"],
    }


# ---- backfill -------------------------------------------------------------------

def _remaining(cur, src: Source, stale: bool) -> int:
    cur.execute(
        "SELECT COUNT(*)" + _JOIN_E + (_TODO_STALE if stale else _TODO_MISSING),
        (src.profile, src.name),
    )
    return int(cur.fetchone()[0])


def backfill(conn, name: str, *, limit: int | None = None, stale: bool = False) -> dict[str, Any]:
    """Embed this library's chunks that have no vector under its profile
    (``stale=True`` also those whose text changed since they were embedded).
    Same per-batch isolation and daily budget as the memory backfill: a failed
    batch is counted and skipped, an exhausted budget stops the run."""
    with conn.cursor() as cur:
        src = get_source(cur, name)
        spec = profiles.resolve_spec(src.profile, cur)
        stats: dict[str, Any] = {
            "ok": True, "source": name, "profile": src.profile,
            "embedded": 0, "failed": 0, "remaining": 0, "batches": 0,
        }
        cond = _TODO_STALE if stale else _TODO_MISSING
        last = (-1, -1)
        attempted = 0
        while True:
            size = embed.BATCH if limit is None else min(embed.BATCH, limit - attempted)
            if size <= 0:
                break
            cur.execute(
                "SELECT c.document, c.chunk_idx, c.chunk_text, c.content_hash, d.title"
                + _JOIN_E + cond
                + " AND (c.document, c.chunk_idx) > (%s, %s)"
                " ORDER BY c.document, c.chunk_idx LIMIT %s",
                (src.profile, name, last[0], last[1], size),
            )
            batch = cur.fetchall()
            if not batch:
                break
            last = (batch[-1][0], batch[-1][1])
            attempted += len(batch)
            api = embed._api_texts(src.profile, [(r[4] or "", r[2]) for r in batch])
            try:
                vecs = embed.embed_batch(
                    api, profile=src.profile, retries=embed.BACKFILL_RETRIES,
                    delay=embed.BACKFILL_DELAY_S, input_type="document",
                )
                for v in vecs:
                    # The column is untyped, so the database will not reject a
                    # wrong width; this is the check that does.
                    if len(v) != spec.dim:
                        raise RuntimeError(
                            f"vector of length {len(v)} for profile {spec.id} (dim {spec.dim})"
                        )
            except RuntimeError as exc:
                if "budget exhausted" in str(exc):
                    stats["budget_exhausted"] = True
                    _log(f"{name}: stopping: {exc}")
                    break
                stats["failed"] += len(batch)
                if "API key not found" in str(exc) and not stats.get("embed_provider"):
                    stats["embed_provider"] = "missing key"
                _log(f"{name}: batch failed ({type(exc).__name__}): {exc}; continuing")
                time.sleep(embed.BACKFILL_PAUSE_S)
                continue
            cur.executemany(
                _INSERT_EMBEDDING,
                [(src.profile, r[0], r[1], embed._vec_literal(v), r[3])
                 for r, v in zip(batch, vecs)],
            )
            conn.commit()
            stats["embedded"] += len(batch)
            stats["batches"] += 1
            if len(batch) == size:
                time.sleep(embed.BACKFILL_PAUSE_S)
        if stats["embedded"]:
            # After the inserts, not before: one build over finished rows is far
            # cheaper than maintaining the graph per batch. A no-op once it exists.
            profiles.ensure_profile_index(cur, src.profile, "library_embeddings", quiet=True)
            conn.commit()
        stats["remaining"] = _remaining(cur, src, stale)
    return stats


# ---- nightly --------------------------------------------------------------------

def backfill_all(conn) -> dict[str, Any]:
    """The nightly step: for every enabled library, scan (no model) then
    backfill. One library failing never stops the next; an exhausted budget
    skips the rest. A hub without the library tables is a skip, not a failure."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", ("public.library_sources",))
        row = cur.fetchone()
        if not (row and row[0]):
            return {"ok": True, "libraries": 0, "skipped": "library tables not migrated"}
        cur.execute(f"SELECT {_SRC_COLS} FROM library_sources WHERE enabled ORDER BY name")
        names = [r[0] for r in cur.fetchall()]
    out: dict[str, Any] = {"ok": True, "libraries": len(names), "results": []}
    budget_out = False
    for name in names:
        item: dict[str, Any] = {"name": name}
        out["results"].append(item)
        if budget_out:
            item["skipped"] = "embed budget exhausted"
            continue
        try:
            sc = scan(conn, name)
            bf = backfill(conn, name)
            item.update(
                added=sc["added"], updated=sc["updated"], removed=sc["removed"],
                embedded=bf["embedded"], failed=bf["failed"], remaining=bf["remaining"],
            )
            budget_out = bool(bf.get("budget_exhausted"))
            if budget_out:
                item["budget_exhausted"] = True
        except Exception as exc:  # noqa: BLE001 - one library never takes the nightly down
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            item["error"] = f"{type(exc).__name__}: {exc}"
            out["ok"] = False
    return out


def nightly() -> dict[str, Any]:
    """``backfill_all`` over one short connection (the job's entry point)."""
    from khipu.db import connect

    with connect() as conn:
        return backfill_all(conn)


# ---- doctor ---------------------------------------------------------------------

def summary(conn) -> list[dict[str, Any]]:
    """Per library: profile, enabled and coverage. [] on a hub without the tables."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", ("public.library_sources",))
        row = cur.fetchone()
        if not (row and row[0]):
            return []
    keys = ("name", "profile", "enabled", "documents", "chunks", "embedded",
            "missing", "stale", "pct")
    return [{k: r[k] for k in keys} for r in list_sources(conn)]


def doctor_block() -> list[dict[str, Any]]:
    from khipu.db import connect

    with connect() as conn:
        return summary(conn)


# ---- import ---------------------------------------------------------------------

def _iter_sqlite(path: Path) -> Iterator[dict[str, Any]]:
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise LibraryError(f"cannot open {path} as SQLite: {exc}") from exc
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(embeddings)")}
        if not cols:
            raise LibraryError(f"{path} has no `embeddings` table")
        missing = {"chunk_idx", "source_file", "chunk_text", "embedding"} - cols
        if missing:
            raise LibraryError(f"{path} `embeddings` table lacks column(s): {sorted(missing)}")
        want = ["node_id", "chunk_idx", "source_file", "chunk_text", "embedding", "model", "dims"]
        select = ", ".join(c if c in cols else f"NULL AS {c}" for c in want)
        cur = conn.execute(f"SELECT {select} FROM embeddings ORDER BY rowid")
        while True:
            rows = cur.fetchmany(2000)
            if not rows:
                break
            for r in rows:
                yield dict(zip(want, r))
    except sqlite3.DatabaseError as exc:
        raise LibraryError(f"cannot read {path} as a SQLite index: {exc}") from exc
    finally:
        conn.close()


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                yield {"_bad": True}
                continue
            yield obj if isinstance(obj, dict) else {"_bad": True}


def _decode_vector(raw: Any) -> list[float] | None:
    if isinstance(raw, (bytes, bytearray, memoryview)):
        b = bytes(raw)
        if not b or len(b) % 4:
            return None
        vec = list(struct.unpack(f"<{len(b) // 4}f", b))
    elif isinstance(raw, list) and raw:
        try:
            vec = [float(x) for x in raw]
        except (TypeError, ValueError):
            return None
    else:
        return None
    return vec if math.isfinite(sum(vec)) else None


def _infer_provider(model: str) -> str:
    m = model.lower()
    if m.startswith("voyage"):
        return "voyage"
    if m.startswith("gemini"):
        return "gemini"
    return "openai-compatible"


def _resolve_import_profile(cur, model: Any, dims: Any) -> profiles.ProfileSpec:
    """The profile for rows that name only ``model@dims``: reuse it if the hub has
    it, else create it with the provider inferred from the model name. An
    inferred openai-compatible profile has no endpoint, so it is never created
    here (it could not embed anything later)."""
    if not model or not dims:
        raise LibraryError(
            "rows carry no model/dims; pass --profile to say which profile these vectors belong to"
        )
    pid = f"{model}@{dims}"
    try:
        existing = profiles.cached_spec(pid) or profiles.load_spec(cur, pid)
        if existing is not None:
            return existing
        provider = _infer_provider(str(model))
        if provider == "openai-compatible":
            raise LibraryError(
                f"no profile {pid!r} and its provider cannot be inferred from the model "
                "name; create it with `khipu embed profiles add` (it needs --endpoint) "
                "and pass --profile"
            )
        spec = profiles.validate_spec(pid, provider=provider, model=str(model), dim=int(dims))
    except ValueError as exc:
        raise LibraryError(str(exc)) from exc
    profiles.add_profile(cur, spec, note="created by khipu library import")
    return spec


def _map_path(source_file: str, prefix: str, roots: tuple[str, ...]) -> str | None:
    """Row path -> posix path relative to the root, or None. The prefix is
    removed when present; an absolute path must lie under the root."""
    sf = source_file.replace("\\", "/")
    if prefix and sf.startswith(prefix):
        sf = sf[len(prefix):]
    if sf.startswith("/"):
        for base in roots:
            b = base.rstrip("/") + "/"
            if sf.startswith(b):
                sf = sf[len(b):]
                break
        else:
            return None
    return sf.lstrip("/") or None


def import_index(
    conn, name: str, path: str, *, strip_prefix: str = "", profile: str | None = None,
) -> dict[str, Any]:
    """Import vectors computed elsewhere into library ``name``.

    ``path`` is graphify's SQLite file (``embeddings`` table) or a ``.jsonl``
    with the same fields. A row is counted, in this order, as ``unmapped`` (its
    path, minus ``strip_prefix``, is not a .txt/.md file under the root),
    ``skipped_corrupt`` (binary-garbage chunk text), ``skipped_bad_vector``
    (wrong length for the profile, or not finite) or ``skipped_bad_row``
    (malformed), else written. For every document that gets rows, the imported
    chunks are authoritative: chunks a scan made for the same file under a
    different window layout are replaced. Idempotent on (document, chunk_idx,
    profile); streams in batches, never loads the file.
    """
    src_path = Path(os.path.expanduser(path))
    if not src_path.is_file():
        raise LibraryError(f"{path!r} is not a file")
    reader = _iter_jsonl(src_path) if src_path.suffix.lower() == ".jsonl" else _iter_sqlite(src_path)
    stats = {"read": 0, "imported": 0, "skipped_corrupt": 0, "skipped_bad_vector": 0,
             "skipped_bad_row": 0, "unmapped": 0}
    with conn.cursor() as cur:
        src = get_source(cur, name)
        root = Path(src.root)
        if not root.is_dir():
            raise LibraryError(f"root {src.root!r} is not a directory; nothing was changed")
        root_real = os.path.realpath(root)
        roots = (str(root), root_real)
        fixed: profiles.ProfileSpec | None = None
        if profile:
            fixed = profiles.load_spec(cur, profile)
            if fixed is None:
                raise LibraryError(f"unknown embedding profile {profile!r}")
        by_key: dict[tuple[Any, Any], profiles.ProfileSpec] = {}
        used: dict[str, profiles.ProfileSpec] = {}
        eligible: dict[str, Path | None] = {}
        doc_ids: dict[str, int] = {}
        touched: dict[int, set[int]] = {}
        chunk_rows: list[tuple] = []
        emb_rows: list[tuple] = []

        def flush() -> None:
            if chunk_rows:
                cur.executemany(_INSERT_CHUNK, chunk_rows)
                cur.executemany(_INSERT_EMBEDDING, emb_rows)
                conn.commit()
                chunk_rows.clear()
                emb_rows.clear()

        def document_for(rel: str, p: Path) -> int | None:
            if rel in doc_ids:
                return doc_ids[rel]
            try:
                data = p.read_bytes()
                st = p.stat()
            except OSError:
                return None
            title, author, tags, _body = document_metadata(
                data.decode("utf-8", errors="replace"), rel
            )
            doc_id = _upsert_document(
                cur, name, rel, title, author, tags, len(data), st,
                hashlib.sha256(data).hexdigest(),
            )
            doc_ids[rel] = doc_id
            touched[doc_id] = set()
            return doc_id

        for row in reader:
            stats["read"] += 1
            if stats["read"] % 20000 == 0:
                _log(f"{name}: read {stats['read']} imported {stats['imported']}")
            if row.get("_bad"):
                stats["skipped_bad_row"] += 1
                continue
            rel = _map_path(str(row.get("source_file") or ""), strip_prefix or "", roots)
            if rel is None:
                stats["unmapped"] += 1
                continue
            if rel not in eligible:
                eligible[rel] = eligible_file(root, root_real, rel)
            if eligible[rel] is None:
                stats["unmapped"] += 1
                continue
            text = row.get("chunk_text")
            if not isinstance(text, str) or is_corrupt_chunk_text(text):
                stats["skipped_corrupt"] += 1
                continue
            vec = _decode_vector(row.get("embedding"))
            try:
                idx = int(row.get("chunk_idx"))
                dims = int(row["dims"]) if row.get("dims") is not None else None
            except (TypeError, ValueError):
                stats["skipped_bad_row"] += 1
                continue
            if vec is None:
                stats["skipped_bad_vector"] += 1
                continue
            if fixed is not None:
                spec = fixed
            else:
                key = (row.get("model"), dims or len(vec))
                spec = by_key.get(key)
                if spec is None:
                    spec = by_key[key] = _resolve_import_profile(cur, key[0], key[1])
            if len(vec) != spec.dim or (dims is not None and dims != len(vec)):
                stats["skipped_bad_vector"] += 1
                continue
            doc_id = document_for(rel, eligible[rel])
            if doc_id is None:
                stats["unmapped"] += 1
                continue
            used[spec.id] = spec
            clean = _clean(text)
            digest = _md5(clean)
            touched[doc_id].add(idx)
            chunk_rows.append((doc_id, idx, clean, digest))
            emb_rows.append((spec.id, doc_id, idx, embed._vec_literal(vec), digest))
            stats["imported"] += 1
            if len(chunk_rows) >= IMPORT_BATCH:
                flush()
        flush()
        # The imported chunks define each document they touched.
        for doc_id, idxs in touched.items():
            cur.execute(
                "DELETE FROM library_chunks WHERE document = %s AND NOT (chunk_idx = ANY(%s))",
                (doc_id, sorted(idxs)),
            )
        conn.commit()
        for spec in used.values():
            profiles.ensure_profile_index(
                cur, spec.id, "library_embeddings", dim=spec.dim, quiet=True
            )
        conn.commit()
    ids = sorted(used)
    out: dict[str, Any] = {
        "ok": True, "source": name, **stats, "documents": len(touched),
        "profile": ids[0] if ids else (profile or None), "profiles": ids,
        "source_profile": src.profile,
    }
    if ids and src.profile not in ids:
        out["note"] = (
            f"vectors went under {ids}; library {name!r} searches with {src.profile!r}, "
            "so they are not used until the library's profile matches"
        )
    return out
