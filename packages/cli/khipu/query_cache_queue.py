"""Query-vector cache writes, kept off the search connection.

Search connects as the read role, which may not write. A provider vector is
kept in this process at once (the next identical query here skips the
provider) and queued in a local file; ``outbox.drain`` writes the queue to
``memory_query_cache`` in one batch on an owner connection, so a crowd of
searches opens no owner connections. A cache hit queues a usage bump the
same way. The queue holds hashes and vectors only, never query text.
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import threading
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from typing import Any

MEMORY_MAX = 512
# A queue left undrained stops growing here; the cost is a re-embed later.
QUEUE_MAX_BYTES = 20 * 1024 * 1024

_MEMORY: "OrderedDict[tuple[str, str], list[float]]" = OrderedDict()
_MEMORY_LOCK = threading.Lock()


def _log(msg: str) -> None:
    print(f"[khipu-query-cache] {msg}", file=sys.stderr, flush=True)


def remembered(profile: str, query_hash: str) -> list[float] | None:
    with _MEMORY_LOCK:
        vec = _MEMORY.get((profile, query_hash))
        if vec is not None:
            _MEMORY.move_to_end((profile, query_hash))
        return vec


def remember(profile: str, query_hash: str, vec: list[float]) -> None:
    with _MEMORY_LOCK:
        _MEMORY[(profile, query_hash)] = list(vec)
        _MEMORY.move_to_end((profile, query_hash))
        while len(_MEMORY) > MEMORY_MAX:
            _MEMORY.popitem(last=False)


def queue_dir() -> Path:
    from khipu.outbox import outbox_dir

    return outbox_dir().parent / "query-cache-queue"


def queue_file() -> Path:
    return queue_dir() / "queue.jsonl"


@contextmanager
def _locked():
    d = queue_dir()
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(d / ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _append(lines: list[dict[str, Any]]) -> None:
    try:
        with _locked():
            path = queue_file()
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                size = 0
            if size >= QUEUE_MAX_BYTES:
                return
            with open(path, "a", encoding="utf-8") as fh:
                os.fchmod(fh.fileno(), 0o600)
                for line in lines:
                    fh.write(json.dumps(line, separators=(",", ":")) + "\n")
    except OSError as exc:
        _log(f"queue write skipped: {type(exc).__name__}")


def queue_put(profile: str, query_hash: str, vec: list[float]) -> None:
    _append([{"op": "put", "profile": profile, "hash": query_hash, "vec": list(vec)}])


def queue_hit(profile: str, query_hash: str) -> None:
    _append([{"op": "hit", "profile": profile, "hash": query_hash}])


def pending() -> int:
    try:
        with open(queue_file(), encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return 0


def _take() -> list[dict[str, Any]]:
    with _locked():
        path = queue_file()
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        path.unlink()
    out = []
    for raw in text.splitlines():
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("op") in ("put", "hit"):
            out.append(item)
    return out


def flush(conn) -> dict[str, int]:
    """Write the queue to ``memory_query_cache`` on the owner connection
    ``conn`` in one transaction. A failed write puts the lines back."""
    items = _take()
    if not items:
        return {"put": 0, "hit": 0}
    from khipu.embed import _vec_literal

    puts: dict[tuple[str, str], list[float]] = {}
    hits: dict[tuple[str, str], int] = {}
    for item in items:
        key = (str(item["profile"]), str(item["hash"]))
        if item["op"] == "put" and isinstance(item.get("vec"), list):
            puts[key] = item["vec"]
        elif item["op"] == "hit":
            hits[key] = hits.get(key, 0) + 1
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.memory_query_cache') IS NOT NULL")
            if not cur.fetchone()[0]:
                conn.rollback()
                return {"put": 0, "hit": 0}
            for (profile, qh), vec in puts.items():
                cur.execute(
                    "INSERT INTO memory_query_cache (profile, query_hash, embedding)"
                    " VALUES (%s, %s, %s::vector)"
                    " ON CONFLICT (profile, query_hash) DO UPDATE"
                    " SET embedding = EXCLUDED.embedding, last_used_at = now()",
                    (profile, qh, _vec_literal(vec)),
                )
            for (profile, qh), n in hits.items():
                cur.execute(
                    "UPDATE memory_query_cache SET last_used_at = now(), hits = hits + %s"
                    " WHERE profile = %s AND query_hash = %s",
                    (n, profile, qh),
                )
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - a cache write never fails a drain
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        _append(items)
        _log(f"queue flush failed, kept: {type(exc).__name__}")
        raise
    return {"put": len(puts), "hit": sum(hits.values())}


def flush_on_owner() -> dict[str, Any]:
    """Flush through one owner connection, opened only when there is work."""
    if not queue_file().is_file():
        return {"put": 0, "hit": 0}
    from khipu.db import connect

    try:
        with connect(role="owner") as conn:
            return flush(conn)
    except Exception as exc:  # noqa: BLE001
        return {"put": 0, "hit": 0, "error": f"{type(exc).__name__}: {exc}"[:200]}
