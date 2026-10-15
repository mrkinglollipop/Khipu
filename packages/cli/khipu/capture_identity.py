"""Immutable source identity for captured episodes.

Scope: docs/plans/2026-10-11-capture-identity.md (decision 1). Every capture
carries a ``source_key``, computed here and nowhere else:

* Natural key, when the payload has a non-empty ``session_id`` AND a non-empty
  ``transcript_range``::

      sha256_hex(utf8("khipu-source-v1" US harness US session_id US
                      transcript_range US window_id US part))

  where US is the ASCII unit separator (0x1F), a missing or null field is the
  empty string and any other value is ``str(value)``.

* Payload key, otherwise (gateway summary-only captures, the legacy writer)::

      sha256_hex(utf8(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                 separators=(",", ":"))))

  This is also ``payload_sha256`` for every capture, and it is byte-for-byte
  the merge receipt already stored as ``raw.merged_from[].source_id``.

* Episode key, only for historical rows whose original payload cannot be
  reproduced (an origin row with no natural key, or a second episode that
  claims a natural key another episode already holds)::

      sha256_hex(utf8("khipu-episode-v1" US episode_id))

ops/migrations/0027_episode_sources.sql computes the natural and episode keys
in SQL with the same canonical form; a test pins both sides to fixed hashes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

SOURCE_FIELDS = ("harness", "session_id", "transcript_range", "window_id", "part")
_US = "\x1f"


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _field(value: Any) -> str:
    return "" if value is None else str(value)


def payload_sha256(payload: dict[str, Any]) -> str:
    return _sha256_hex(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                  separators=(",", ":")))


def has_natural_key(payload: dict[str, Any]) -> bool:
    return bool(_field(payload.get("session_id"))) and bool(_field(payload.get("transcript_range")))


def source_key(payload: dict[str, Any]) -> str:
    if has_natural_key(payload):
        return _sha256_hex(_US.join(["khipu-source-v1",
                                     *(_field(payload.get(k)) for k in SOURCE_FIELDS)]))
    return payload_sha256(payload)


def episode_key(episode_id: int) -> str:
    return _sha256_hex(f"khipu-episode-v1{_US}{int(episode_id)}")


def source_identity(payload: dict[str, Any]) -> dict[str, Any]:
    """The source row's identity columns for one payload, as one snapshot.
    Field values are stored as text (or NULL), the same form the key hashes."""
    fields = {k: (None if payload.get(k) is None else str(payload.get(k)))
              for k in SOURCE_FIELDS}
    return {"source_key": source_key(payload), "payload_sha256": payload_sha256(payload),
            **fields}


_INSERT_COLS = ("source_key, episode_id, role, harness, session_id, transcript_range, "
                "window_id, part, ts, summary_md5, payload_sha256")


def _insert_source(cur, key: str, episode_id: int, role: str, ident: dict[str, Any],
                   ts: Any, summary: Any) -> bool:
    fields = [ident.get(k) for k in SOURCE_FIELDS]
    if role == "origin":
        # ts and summary_md5 are read off the row just written, so they are the
        # legacy identity exactly as the unique index sees it.
        cur.execute(
            f"INSERT INTO episode_sources ({_INSERT_COLS}) "
            "SELECT %s, id, 'origin', %s, %s, %s, %s, %s, ts, md5(summary), %s "
            "FROM episodes WHERE id = %s ON CONFLICT (source_key) DO NOTHING",
            (key, *fields, ident.get("payload_sha256"), episode_id),
        )
    else:
        summary_md5 = (hashlib.md5(summary.encode("utf-8")).hexdigest()
                       if isinstance(summary, str) else None)
        cur.execute(
            f"INSERT INTO episode_sources ({_INSERT_COLS}) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz, %s, %s) "
            "ON CONFLICT (source_key) DO NOTHING",
            (key, episode_id, role, *fields, ts or None, summary_md5,
             ident.get("payload_sha256")),
        )
    return cur.rowcount > 0


def record_source(cur, episode_id: int, role: str, ident: dict[str, Any], *,
                  ts: Any = None, summary: Any = None) -> str | None:
    """Write one episode_sources row in the caller's transaction (phase 1 dual
    write; nothing reads the table yet). Returns the key recorded, or None when
    the table is absent (a hub without 0027), the source is already recorded,
    or the write failed. Fail-open under its own savepoint: the episode is the
    durable part and a missing source row is never worth losing it for.

    An origin whose key another episode already holds gets the episode key,
    the same rule the 0027 backfill applies, so every episode keeps an origin
    row. A merged source already recorded keeps its first row."""
    import sys

    from khipu.db import has_table

    # The savepoint comes first so that even a failed probe cannot leave the
    # caller's transaction aborted.
    try:
        cur.execute("SAVEPOINT capture_source_row")
    except Exception:  # noqa: BLE001 — no savepoint support (autocommit)
        pass
    try:
        if not has_table(cur, "episode_sources"):
            key = None
        else:
            key = ident["source_key"]
            if not _insert_source(cur, key, episode_id, role, ident, ts, summary):
                if role != "origin":
                    key = None
                else:
                    key = episode_key(episode_id)
                    if not _insert_source(cur, key, episode_id, role, ident, ts, summary):
                        key = None
        try:
            cur.execute("RELEASE SAVEPOINT capture_source_row")
        except Exception:  # noqa: BLE001
            pass
        return key
    except Exception as exc:  # noqa: BLE001 — the episode stays; fail-open
        try:
            cur.execute("ROLLBACK TO SAVEPOINT capture_source_row")
        except Exception:  # noqa: BLE001
            pass
        print(f"[khipu-capture] source row not recorded ({type(exc).__name__}: {exc})",
              file=sys.stderr, flush=True)
        return None
