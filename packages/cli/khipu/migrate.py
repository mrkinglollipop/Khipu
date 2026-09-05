# --bypass-harness (sonnet lane): this file is edited from inside a dispatched
# on-sub Agent(model="sonnet") subagent (Phase 1 of the setup-that-cannot-
# strand-you plan) — the exact lane the code-routing policy names, so no
# further hop is possible or appropriate.
"""Apply the SQL migrations under ops/migrations to the configured database.

Every migration file records itself in ``schema_migrations`` (``INSERT … ON
CONFLICT DO NOTHING``), so applying is idempotent at the SQL level; this runner
additionally skips files whose version is already recorded, so a re-run on a
live database executes nothing. Files run in name order inside one transaction
each: a failing migration leaves the database at the previous version.

Until this existed the schema was applied by hand from a private runbook, which
is fine for one operator and useless for anyone else setting Khipu up.
"""

from __future__ import annotations

import re
from pathlib import Path

_VERSION_RE = re.compile(r"^(\d{4}_[a-z0-9_]+)\.sql$")


def migrations_dir() -> Path:
    from khipu.paths import repo_root

    return repo_root() / "ops" / "migrations"


def available(directory: Path | None = None) -> list[tuple[str, Path]]:
    """(version, path) for every migration file, in apply order."""
    d = directory or migrations_dir()
    out = []
    for p in sorted(d.glob("*.sql")):
        m = _VERSION_RE.match(p.name)
        if m:
            out.append((m.group(1), p))
    return out


def applied(cur) -> set[str]:
    cur.execute(
        "SELECT to_regclass('public.schema_migrations') IS NOT NULL"
    )
    if not cur.fetchone()[0]:
        return set()
    cur.execute("SELECT version FROM schema_migrations")
    return {r[0] for r in cur.fetchall()}


def plan(cur, directory: Path | None = None) -> dict:
    files = available(directory)
    done = applied(cur)
    pending = [(v, p) for v, p in files if v not in done]
    return {
        "available": [v for v, _ in files],
        "applied": sorted(done & {v for v, _ in files}),
        "pending": [v for v, _ in pending],
        "unknown_applied": sorted(done - {v for v, _ in files}),
        "_pending_paths": pending,
    }


def run(*, dry_run: bool = False, directory: Path | None = None, conn=None) -> dict:
    """Apply pending migrations against ``conn``, or the resolved hub when
    ``conn`` is omitted (the historical, still-default behaviour).

    ``conn`` lets a caller that already opened a connection to a SPECIFIC
    DSN — ``khipu.setup.connect_database(dsn)`` validating a database that
    may not be the one currently configured, or a live dry-run preflight —
    run the schema stage against exactly that connection instead of
    whatever ``khipu.db.resolve_dsn()`` currently points at. The connection
    is never closed here; the caller owns its lifecycle either way.
    """
    if conn is not None:
        return _run_migrations(conn, dry_run=dry_run, directory=directory)
    from khipu.db import connect

    with connect() as owned_conn:
        return _run_migrations(owned_conn, dry_run=dry_run, directory=directory)


def _run_migrations(conn, *, dry_run: bool, directory: Path | None) -> dict:
    with conn.cursor() as cur:
        p = plan(cur, directory)
    pending = p.pop("_pending_paths")
    result = {k: v for k, v in p.items()}
    result["dry_run"] = dry_run
    result["ran"] = []
    if dry_run:
        return result
    for version, path in pending:
        sql = path.read_text(encoding="utf-8")
        # One transaction per file. psycopg's connection is a transaction
        # already; commit after each so a later failure cannot roll back an
        # earlier, successful migration.
        with conn.cursor() as cur:
            cur.execute(sql)
            # Belt and braces: the file records itself, but if a migration
            # forgets to, the runner would re-apply it forever.
            cur.execute(
                "INSERT INTO schema_migrations (version, note) VALUES (%s, %s) "
                "ON CONFLICT (version) DO NOTHING",
                (version, f"applied by khipu migrate from {path.name}"),
            )
        conn.commit()
        result["ran"].append(version)
    result["pending"] = []
    return result
