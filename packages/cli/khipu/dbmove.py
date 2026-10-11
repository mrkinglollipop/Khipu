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
    "embeddings",
    "memory_embeddings",
    "memory_query_cache",
    "topic_aliases",
    "media_assets",
    "commitments",
    "ops_events",
    "project_aliases",
    "decisions",
    "decision_links",
    "deliverables",
    "briefs",
    "topic_hits",
    "library_sources",
    "library_documents",
    "library_chunks",
    "library_embeddings",
)

_TARGET_METADATA_TABLES = frozenset({"schema_migrations"})
_BOOTSTRAP_EMBEDDING_PROFILES = frozenset({
    ("gemini-embedding-001@768", "gemini", "gemini-embedding-001", 768, "l2", True,
     "P1 lock (plan.md Models/LLM); cloud default through P3, local is P4"),
    ("gemini-embedding-2@768", "gemini", "gemini-embedding-2", 768, "l2", False,
     "Gemini Embedding 2 @768; task prefixes at API time; activate after full memory backfill"),
})


def _existing_tables(cur) -> set[str]:
    cur.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
    )
    return {r[0] for r in cur.fetchall()}


def _copy_order(
    source_tables: set[str],
    target_tables: set[str],
    dependencies: dict[str, set[str]] | None = None,
) -> list[str]:
    """Return a stable parent-before-child order.

    ``TABLE_ORDER`` covers the shipped schema.  Constraint metadata keeps a
    same-version future table from being copied alphabetically before its
    parent.  A cycle is left in stable explicit/name order; PostgreSQL will
    reject an immediate cyclic FK during COPY rather than silently omitting it.
    """
    common = source_tables & target_tables
    rank = {table: index for index, table in enumerate(TABLE_ORDER)}
    pending = {table: set((dependencies or {}).get(table, set())) & common for table in common}
    ordered: list[str] = []
    while pending:
        ready = sorted(
            (table for table, parents in pending.items() if not parents),
            key=lambda table: (rank.get(table, len(rank)), table),
        )
        if not ready:
            ordered.extend(sorted(pending, key=lambda table: (rank.get(table, len(rank)), table)))
            break
        for table in ready:
            ordered.append(table)
            pending.pop(table)
        for parents in pending.values():
            parents.difference_update(ready)
    return ordered


def _foreign_key_dependencies(cur, tables: set[str]) -> dict[str, set[str]]:
    cur.execute(
        "SELECT child.relname, parent.relname "
        "FROM pg_constraint con "
        "JOIN pg_class child ON child.oid = con.conrelid "
        "JOIN pg_namespace child_ns ON child_ns.oid = child.relnamespace "
        "JOIN pg_class parent ON parent.oid = con.confrelid "
        "JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace "
        "WHERE con.contype = 'f' AND child_ns.nspname = 'public' "
        "AND parent_ns.nspname = 'public'"
    )
    dependencies = {table: set() for table in tables}
    for child, parent in cur.fetchall():
        if child in dependencies and parent in tables and child != parent:
            dependencies[child].add(parent)
    return dependencies


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


def _connected_identity(cur) -> tuple[str, str, str, int] | None:
    """Server/database identity catches DSN aliases before target mutation.

    ``pg_postmaster_start_time`` identifies a Unix-socket server where libpq
    cannot expose an inet address, while database name distinguishes databases
    on that server.
    """
    cur.execute(
        "SELECT current_database(), pg_postmaster_start_time()::text, "
        "COALESCE(inet_server_addr()::text, ''), COALESCE(inet_server_port(), 0)"
    )
    row = cur.fetchone()
    if not row or any(value is None for value in row):
        return None
    return (str(row[0]), str(row[1]), str(row[2]), int(row[3]))


def _set_move_timeouts(cur) -> None:
    # A move must fail closed instead of waiting forever on a locked target.
    cur.execute("SET LOCAL statement_timeout = '30s'")
    cur.execute("SET LOCAL lock_timeout = '10s'")


def _rollback_target(conn) -> None:
    rollback = getattr(conn, "rollback", None)
    if callable(rollback):
        rollback()


def _target_has_user_data(cur, table: str, count: int) -> bool:
    if count <= 0 or table in _TARGET_METADATA_TABLES:
        return False
    if table == "embedding_profiles":
        # created_at varies with provisioning; every seeded value must match.
        cur.execute("SELECT id, provider, model, dim, normalize, is_active, note FROM embedding_profiles")
        rows = cur.fetchall()
        return len(rows) != count or any(tuple(row) not in _BOOTSTRAP_EMBEDDING_PROFILES for row in rows)
    return True


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

    source_conn = psycopg.connect(
        conninfo_with_local_root_cert(current_dsn), autocommit=False, connect_timeout=10
    )
    target_conn = psycopg.connect(
        conninfo_with_local_root_cert(target_dsn), autocommit=False, connect_timeout=10
    )
    try:
        with source_conn.cursor() as scur, target_conn.cursor() as tcur:
            _set_move_timeouts(scur)
            _set_move_timeouts(tcur)
            source_identity = _connected_identity(scur)
            target_identity = _connected_identity(tcur)
            if source_identity is None or target_identity is None:
                return {
                    "ok": False,
                    "error": "target_identity_unavailable",
                    "detail": "Khipu could not prove the source and target are different databases.",
                }
            if source_identity == target_identity:
                return {
                    "ok": False,
                    "error": "target_is_current",
                    "detail": "The target resolves to the database Khipu is already using.",
                }
            source_tables = _existing_tables(scur)
            target_tables = _existing_tables(tcur)
            if source_tables != target_tables:
                return {
                    "ok": False,
                    "error": "schema_mismatch",
                    "source_only": sorted(source_tables - target_tables),
                    "target_only": sorted(target_tables - source_tables),
                    "detail": "Source and target public table sets differ; migrate both before moving.",
                }
            dependencies = _foreign_key_dependencies(scur, source_tables)
            order = _copy_order(source_tables, target_tables, dependencies)

        existing_counts: dict[str, int] = {}
        with target_conn.cursor() as cur:
            if not dry_run:
                for table in order:
                    cur.execute(f'LOCK TABLE "{table}" IN ACCESS EXCLUSIVE MODE')  # noqa: S608
            for table in order:
                existing_counts[table] = _row_count(cur, table)
        with target_conn.cursor() as cur:
            nonempty = {
                table: count
                for table, count in existing_counts.items()
                if _target_has_user_data(cur, table, count)
            }
        if nonempty and not allow_nonempty:
            return {
                "ok": False,
                "error": "target_not_empty",
                "detail": "The target already holds data: " + ", ".join(
                    f"{table}={count}" for table, count in sorted(nonempty.items())
                ) + ".",
                "fix": "Pass --into-nonempty to copy anyway, or choose an empty database.",
            }

        tables_report: list[dict[str, Any]] = []
        mismatches: list[str] = []
        try:
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
                    tcur.execute(f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE')  # noqa: S608
                _copy_table(source_conn, target_conn, table)
                with target_conn.cursor() as tcur:
                    target_rows = _row_count(tcur, table)
                if target_rows != source_rows:
                    mismatches.append(table)
                tables_report.append(
                    {"name": table, "source_rows": source_rows, "target_rows": target_rows, "seconds": round(time.monotonic() - t0, 3)}
                )
                if progress:
                    progress(table, target_rows)
        except Exception as exc:  # noqa: BLE001 — preserve target on every replacement failure
            _rollback_target(target_conn)
            return {
                "ok": False,
                "error": "copy_failed",
                "detail": f"{type(exc).__name__}: {exc}",
                "tables": tables_report,
                "switched": False,
            }

        if dry_run:
            return {"ok": True, "dry_run": True, "tables": tables_report, "switched": False}

        if mismatches:
            _rollback_target(target_conn)
            return {
                "ok": False,
                "error": "row_count_mismatch",
                "detail": f"Row counts did not match after copy: {', '.join(mismatches)}.",
                "tables": tables_report,
                "switched": False,
            }
        try:
            with target_conn.cursor() as tcur:
                for table in order:
                    _reset_sequences(tcur, table)
        except Exception as exc:  # noqa: BLE001 — preserve target replacement on sequence failure
            _rollback_target(target_conn)
            return {
                "ok": False,
                "error": "copy_failed",
                "detail": f"{type(exc).__name__}: {exc}",
                "tables": tables_report,
                "switched": False,
            }
        target_conn.commit()
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
