"""Move the Khipu database to another host: copy, verify, switch.

``khipu db move --to DSN`` and the desktop's "Move this memory to another
database…" both call :func:`move_database`. The source is never modified —
every write happens on the target connection, and the source connection is
read/COPY-TO only.
"""

from __future__ import annotations

import time
from typing import Any, Callable

# FK-derived dependency order: a table only appears after every table it
# references. ``schema_migrations`` goes first so the target's applied-version
# rows match the source byte-for-byte, not just set-equal. Anything present in
# the source but not in this tuple (a future migration this list has not
# caught up with) is discovered from information_schema and appended after —
# see ``_copy_order`` — so nothing new is silently skipped.
TABLE_ORDER: tuple[str, ...] = (
    "schema_migrations",
    "embedding_profiles",
    "episodes",
    "topics",
    "topic_revisions",
    "nodes",
    "edges",
    "memory_embeddings",
    "embeddings",
    "topic_aliases",
    "media_assets",
    "commitments",
    "memory_query_cache",
    "ops_events",
)


def _existing_tables(cur) -> set[str]:
    cur.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
    )
    return {r[0] for r in cur.fetchall()}


def _copy_order(source_tables: set[str], target_tables: set[str]) -> list[str]:
    common = source_tables & target_tables
    ordered = [t for t in TABLE_ORDER if t in common]
    extra = sorted(common - set(ordered))
    return ordered + extra


def _row_count(cur, table: str) -> int:
    cur.execute(f'SELECT COUNT(*) FROM "{table}"')  # noqa: S608 — table from information_schema, not user input
    return int(cur.fetchone()[0])


def _copy_table(source_conn, target_conn, table: str) -> None:
    with source_conn.cursor() as scur, target_conn.cursor() as tcur:
        with scur.copy(f'COPY "{table}" TO STDOUT (FORMAT BINARY)') as src_copy:
            with tcur.copy(f'COPY "{table}" FROM STDIN (FORMAT BINARY)') as dst_copy:
                for block in src_copy:
                    dst_copy.write(bytes(block))


def _reset_sequences(cur, table: str) -> None:
    """setval() every serial/identity column owned by ``table`` to its
    current max — COPY (unlike INSERT) never advances a sequence."""
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s "
        "AND (column_default LIKE 'nextval(%%' OR is_identity = 'YES')",
        (table,),
    )
    for (col,) in cur.fetchall():
        cur.execute("SELECT pg_get_serial_sequence(%s, %s)", (table, col))
        row = cur.fetchone()
        seq = row[0] if row else None
        if not seq:
            continue
        cur.execute(f'SELECT COALESCE(MAX("{col}"), 0) FROM "{table}"')  # noqa: S608
        max_id = cur.fetchone()[0]
        cur.execute("SELECT setval(%s, %s, %s)", (seq, max(int(max_id), 1), max_id > 0))


def _same_target(current_dsn: str, target_dsn: str) -> bool:
    from psycopg.conninfo import conninfo_to_dict

    cur = conninfo_to_dict(current_dsn)
    tgt = conninfo_to_dict(target_dsn)
    return (cur.get("host"), cur.get("port"), cur.get("dbname") or cur.get("db")) == (
        tgt.get("host"),
        tgt.get("port"),
        tgt.get("dbname") or tgt.get("db"),
    )


_PREFLIGHT_STAGES = ("reach", "version", "privileges", "schema", "graph")


def move_database(
    target_dsn: str,
    *,
    dry_run: bool = False,
    allow_nonempty: bool = False,
    progress: "Callable[[str, int], None] | None" = None,
) -> dict[str, Any]:
    """Copy every table into ``target_dsn`` in FK order, verify row counts,
    and (on a real, successful run) switch the stored connection to it. The
    source database is never written to — the source connection only ever
    runs ``SELECT`` / ``COPY … TO STDOUT``.
    """
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    from khipu.db import conninfo_with_local_root_cert, resolve_dsn
    from khipu.setup import connect_database, mask_dsn

    current_dsn = resolve_dsn()
    if _same_target(current_dsn, target_dsn):
        return {
            "ok": False,
            "error": "target_is_current",
            "detail": "The target is the database Khipu is already using.",
        }

    preflight = connect_database(target_dsn, store=False, install_jobs=False, prove=False)
    preflight_ids = {s["id"] for s in preflight["stages"]}
    preflight_failed = [
        s for s in preflight["stages"] if s["id"] in _PREFLIGHT_STAGES and not s.get("ok")
    ]
    if preflight_failed or not set(_PREFLIGHT_STAGES) <= preflight_ids:
        return {
            "ok": False,
            "error": "target_preflight_failed",
            "detail": f"The target ({mask_dsn(target_dsn)}) is not ready to receive the move.",
            "preflight": preflight,
        }

    source_conn = psycopg.connect(conninfo_with_local_root_cert(current_dsn), autocommit=False)
    target_conn = psycopg.connect(conninfo_with_local_root_cert(target_dsn), autocommit=False)
    try:
        with target_conn.cursor() as cur:
            existing_episodes = _row_count(cur, "episodes") if "episodes" in _existing_tables(cur) else 0
        with target_conn.cursor() as cur:
            existing_topics = _row_count(cur, "topics") if "topics" in _existing_tables(cur) else 0
        if (existing_episodes > 0 or existing_topics > 0) and not allow_nonempty:
            return {
                "ok": False,
                "error": "target_not_empty",
                "detail": f"The target already holds {existing_episodes} episodes and "
                f"{existing_topics} topics.",
                "fix": "Pass --into-nonempty to copy anyway, or choose an empty database.",
            }

        with source_conn.cursor() as scur, target_conn.cursor() as tcur:
            order = _copy_order(_existing_tables(scur), _existing_tables(tcur))

        tables_report: list[dict[str, Any]] = []
        mismatches: list[str] = []
        for table in order:
            t0 = time.monotonic()
            with source_conn.cursor() as scur:
                source_rows = _row_count(scur, table)
            if dry_run:
                tables_report.append(
                    {"name": table, "source_rows": source_rows, "target_rows": None, "seconds": round(time.monotonic() - t0, 3)}
                )
                if progress:
                    progress(table, source_rows)
                continue
            with target_conn.cursor() as tcur:
                tcur.execute(f'TRUNCATE TABLE "{table}" CASCADE')  # noqa: S608
            _copy_table(source_conn, target_conn, table)
            with target_conn.cursor() as tcur:
                _reset_sequences(tcur, table)
            target_conn.commit()
            with target_conn.cursor() as tcur:
                target_rows = _row_count(tcur, table)
            if target_rows != source_rows:
                mismatches.append(table)
            tables_report.append(
                {"name": table, "source_rows": source_rows, "target_rows": target_rows, "seconds": round(time.monotonic() - t0, 3)}
            )
            if progress:
                progress(table, target_rows)

        if dry_run:
            return {"ok": True, "dry_run": True, "tables": tables_report, "switched": False}

        if mismatches:
            return {
                "ok": False,
                "error": "row_count_mismatch",
                "detail": f"Row counts did not match after copy: {', '.join(mismatches)}.",
                "tables": tables_report,
                "switched": False,
            }
    finally:
        source_conn.close()
        target_conn.close()

    switch = connect_database(target_dsn, store=True, install_jobs=True, prove=True)
    return {
        "ok": bool(switch.get("ok")),
        "tables": tables_report,
        "switched": True,
        "connect": switch,
        "remaining": [
            "Other Macs need a new join kit: Settings › Another Mac › Save join kit…",
        ],
    }
