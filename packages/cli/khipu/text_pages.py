"""Bounded pages of a stored text body, addressed by UTF-8 byte offset.

Shared by the hub (``activity.topic_detail``) and the offline replica
(``hub_snapshot.topic_detail_snapshot``) so the same id and arguments answer
identically on both paths. Callers fetch at most ``fetch_bytes(max_bytes)``
bytes starting at ``offset`` from the database, then hand them here.
"""
from __future__ import annotations

from typing import Any

PAGE_BYTES_DEFAULT = 64 * 1024
PAGE_BYTES_MAX = 512 * 1024
# A UTF-8 code point is at most 4 bytes; a smaller page could not always hold
# one whole character and still make progress.
PAGE_BYTES_MIN = 4


def _is_continuation(byte: int) -> bool:
    return 0x80 <= byte <= 0xBF


def page_args(offset: Any = None, max_bytes: Any = None) -> tuple[int, int]:
    """Validate caller arguments: offset >= 0 (default 0); max_bytes clamped
    to [PAGE_BYTES_MIN, PAGE_BYTES_MAX] (default PAGE_BYTES_DEFAULT)."""
    try:
        off = 0 if offset is None or offset == "" else int(offset)
        size = PAGE_BYTES_DEFAULT if max_bytes is None or max_bytes == "" else int(max_bytes)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("offset and max_bytes must be integers") from None
    if isinstance(offset, bool) or isinstance(max_bytes, bool):
        raise ValueError("offset and max_bytes must be integers")
    if off < 0:
        raise ValueError("offset must be 0 or more")
    return off, min(max(size, PAGE_BYTES_MIN), PAGE_BYTES_MAX)


# Neither database stores a value over 1 GiB (Postgres field limit; SQLite's
# default SQLITE_MAX_LENGTH is 1e9), so a read starting here is past every
# body. Capping the start keeps a huge offset an empty last page instead of a
# Postgres int4 type error or a SQLite integer overflow.
DB_OFFSET_CEILING = 1 << 30


def db_start(offset: int) -> int:
    """The 1-based start position for ``substring``/``substr``."""
    return min(offset, DB_OFFSET_CEILING) + 1


def fetch_bytes(max_bytes: int) -> int:
    """How many bytes to read from ``offset``: the page, plus room to skip up
    to 3 continuation bytes at the start and to see the byte after the end."""
    return max_bytes + 4


def page_utf8(chunk: bytes | bytearray | memoryview | None, offset: int,
              max_bytes: int, total_bytes: int) -> dict:
    """One page from ``chunk``, the stored body's bytes starting at ``offset``
    (at most ``fetch_bytes(max_bytes)`` of them), with ``total_bytes`` the
    body's full UTF-8 length.

    A page never splits a code point: an ``offset`` that lands inside a
    character starts at the next character instead (``body_offset`` reports
    where the page really starts), and the end is moved back to a character
    boundary. An ``offset`` at or past the end is an empty, complete page.
    """
    raw = bytes(chunk or b"")
    total = max(int(total_bytes or 0), 0)
    start = 0
    if offset > 0:
        while start < len(raw) and start < 3 and _is_continuation(raw[start]):
            start += 1
    begin = offset + start
    if begin >= total:
        return {
            "body": "",
            "body_total_bytes": total,
            "body_offset": offset,
            "body_next_offset": None,
            "complete": True,
        }
    end = start + max_bytes
    if offset + end >= total:
        end = total - offset
    else:
        while end > start and end < len(raw) and _is_continuation(raw[end]):
            end -= 1
    end = min(end, len(raw))
    stop = offset + end
    return {
        "body": raw[start:end].decode("utf-8", "replace"),
        "body_total_bytes": total,
        "body_offset": begin,
        "body_next_offset": stop if stop < total else None,
        "complete": stop >= total,
    }
