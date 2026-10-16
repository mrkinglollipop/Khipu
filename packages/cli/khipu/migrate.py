"""Apply the SQL migrations under ops/migrations to the configured database.

Every migration file records itself in ``schema_migrations`` (``INSERT … ON
CONFLICT DO NOTHING``), so applying is idempotent at the SQL level; this runner
additionally skips files whose version is already recorded, so a re-run on a
live database executes nothing. Files run in name order inside one transaction
each: a failing migration leaves the database at the previous version. A file
may split itself with top-level step marker lines (two dashes, a space and
"khipu:step", at the start of a line); each section is then its own
transaction, so a lock taken by one step is released before the next, and the
file records its version only in its last step: a failed step leaves the
version unrecorded and a re-run applies the whole file again (each step must
therefore be idempotent).

Until this existed the schema was applied by hand from a private runbook, which
is fine for one operator and useless for anyone else setting Khipu up.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

_VERSION_RE = re.compile(r"^(\d{4}_[a-z0-9_]+)\.sql$")

# A migration that names this setting gets this machine's local origin
# (khipu/machine_identity.py) for its transaction only (0029_episode_origin).
LOCAL_ORIGIN_SETTING = "khipu.local_origin"
# The value that applies such a migration without claiming any existing row.
LOCAL_ORIGIN_NONE = "none"
# A step that hits its own lock_timeout (0030) is retried this many times.
LOCK_RETRIES = 3
LOCK_RETRY_DELAY_S = 2.0
_STEP_MARKER = "-- khipu:step"
_STEP_ANYWHERE_RE = re.compile(r"-- khipu:step\b")


class MigrationStepError(ValueError):
    """A step marker that is not a top-level line of its own: inside a
    string, a quoted identifier, a dollar-quoted body or a comment. Splitting
    there would cut that construct in two, so the file is refused instead."""


def _ident_start(c: str) -> bool:
    # PostgreSQL's scanner takes every byte at or above 0x80 as an identifier
    # letter, so any non-ASCII character counts, whatever its plane.
    return ("a" <= c.lower() <= "z") or c == "_" or ord(c) > 127


def _ident_char(c: str) -> bool:
    return _ident_start(c) or c.isdigit() and c.isascii() or c == "$"


def _dollar_tag(sql: str, i: int) -> str | None:
    """The whole opening delimiter ($tag$ or $$) at i, else None."""
    j = i + 1
    if j < len(sql) and _ident_start(sql[j]):
        j += 1
        while j < len(sql) and _ident_char(sql[j]) and sql[j] != "$":
            j += 1
    return sql[i:j + 1] if j < len(sql) and sql[j] == "$" else None


def _continuation(sql: str, i: int) -> int | None:
    """Where a string closed just before i continues: PostgreSQL joins two
    quoted strings separated only by whitespace (line comments included) that
    holds a newline, and the continuation keeps the first part's mode. The
    position of the continuing quote, else None."""
    newline, n = False, len(sql)
    while i < n:
        if sql[i] == "\n":
            newline, i = True, i + 1
        elif sql[i] in " \t\r\f":
            i += 1
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end < 0 else end
        else:
            break
    return i if newline and i < n and sql[i] == "'" else None


def _lex(sql: str) -> tuple[list[tuple[int, int]], list[bool]]:
    """Top-level step marker lines (start, end), and for each section between
    them whether it holds anything but whitespace and comments. Follows the
    PostgreSQL lexical rules a marker could hide in: single-quoted strings
    ('' and, after E, backslash escapes), double-quoted identifiers, dollar
    quotes with optional tags, line comments and nested block comments."""
    markers: list[tuple[int, int]] = []
    has_code = [False]
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "-" and sql.startswith("--", i):
            end = sql.find("\n", i)
            end = n if end < 0 else end
            if (i == 0 or sql[i - 1] == "\n") and sql.startswith(_STEP_MARKER, i) \
                    and _STEP_ANYWHERE_RE.match(sql, i):
                markers.append((i, end))
                has_code.append(False)
            i = end
        elif c == "/" and sql.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
        elif c in "'\"":
            escapes = c == "'" and i > 0 and sql[i - 1] in "eE" \
                and (i < 2 or not _ident_char(sql[i - 2]))
            has_code[-1] = True
            i += 1
            while i < n:
                if escapes and sql[i] == "\\":
                    i += 2
                elif sql[i] == c:
                    if sql.startswith(c * 2, i):
                        i += 2
                        continue
                    i += 1
                    # Comments skipped here are inside the string: a marker
                    # among them is never recorded, so it is refused.
                    nxt = _continuation(sql, i) if c == "'" else None
                    if nxt is None:
                        break
                    i = nxt + 1
                else:
                    i += 1
        elif c == "$" and (i == 0 or not _ident_char(sql[i - 1])) \
                and (tag := _dollar_tag(sql, i)):
            has_code[-1] = True
            close = sql.find(tag, i + len(tag))
            i = n if close < 0 else close + len(tag)
        else:
            if not c.isspace():
                has_code[-1] = True
            i += 1
    return markers, has_code


def steps(sql: str) -> list[str]:
    """The transactions a migration file runs as, in order. Raises
    MigrationStepError when marker text appears anywhere but as a top-level
    line of its own."""
    markers, has_code = _lex(sql)
    starts = {start for start, _ in markers}
    stray = [m.start() for m in _STEP_ANYWHERE_RE.finditer(sql) if m.start() not in starts]
    if stray:
        line = sql.count("\n", 0, stray[0]) + 1
        raise MigrationStepError(
            f"'{_STEP_MARKER}' on line {line} is inside a string, quoted identifier, "
            "dollar-quoted body or comment; a step marker must be a top-level line of its own")
    if not markers:
        return [sql]
    bounds = [0, *(x for m in markers for x in m), len(sql)]
    parts = [sql[bounds[k]:bounds[k + 1]] for k in range(0, len(bounds), 2)]
    # A section of comments alone (the file's header) is not a transaction.
    return [p for p, code in zip(parts, has_code) if code] or [sql]


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


def run(*, dry_run: bool = False, directory: Path | None = None, conn=None,
        claim_local_rows: bool = False) -> dict:
    """Apply pending migrations against ``conn``, or the resolved hub when
    ``conn`` is omitted (the historical, still-default behaviour).

    ``conn`` lets a caller that already opened a connection to a SPECIFIC
    DSN — ``khipu.setup.connect_database(dsn)`` validating a database that
    may not be the one currently configured, or a live dry-run preflight —
    run the schema stage against exactly that connection instead of
    whatever ``khipu.db.resolve_dsn()`` currently points at. The connection
    is never closed here; the caller owns its lifecycle either way.

    ``claim_local_rows`` lets a backfill stamp existing local-harness rows as
    this machine's (0029). Only the owner's own ``khipu migrate`` and a fresh
    local database pass it: setup against an existing hub or ``khipu join
    import`` may run on a second machine, which must not claim the first
    machine's rows.
    """
    if conn is not None:
        return _run_migrations(conn, dry_run=dry_run, directory=directory,
                               claim_local_rows=claim_local_rows)
    from khipu.db import connect

    with connect() as owned_conn:
        return _run_migrations(owned_conn, dry_run=dry_run, directory=directory,
                               claim_local_rows=claim_local_rows)


def _run_migrations(conn, *, dry_run: bool, directory: Path | None,
                    claim_local_rows: bool = False) -> dict:
    with conn.cursor() as cur:
        p = plan(cur, directory)
    pending = p.pop("_pending_paths")
    result = {k: v for k, v in p.items()}
    result["dry_run"] = dry_run
    result["ran"] = []
    if dry_run:
        return result
    # Every file is split before any runs, so a refused file never leaves
    # the database partly migrated.
    split = [(version, path, path.read_text(encoding="utf-8")) for version, path in pending]
    split = [(version, path, sql, steps(sql)) for version, path, sql in split]
    for version, path, sql, parts in split:
        value = None
        if LOCAL_ORIGIN_SETTING in sql:
            # Raises MachineIdentityError before the file runs when this
            # machine has no readable identity.
            from khipu.machine_identity import local_origin

            value = local_origin() if claim_local_rows else LOCAL_ORIGIN_NONE
        # One transaction per file, or per step of a file that splits itself.
        # psycopg's connection is a transaction already; commit after each so
        # a later failure cannot roll back an earlier, successful one.
        for i, part in enumerate(parts):
            for attempt in range(LOCK_RETRIES + 1):
                try:
                    with conn.cursor() as cur:
                        if value is not None:
                            # Transaction-local, so every step sets it again.
                            cur.execute("SELECT set_config(%s, %s, true)",
                                        (LOCAL_ORIGIN_SETTING, value))
                        cur.execute(part)
                        if i == len(parts) - 1:
                            # Belt and braces: the file records itself, but if a
                            # migration forgets to, the runner would re-apply it forever.
                            cur.execute(
                                "INSERT INTO schema_migrations (version, note) VALUES (%s, %s) "
                                "ON CONFLICT (version) DO NOTHING",
                                (version, f"applied by khipu migrate from {path.name}"),
                            )
                    conn.commit()
                    break
                except Exception as exc:
                    # A step that sets lock_timeout gives up rather than queue
                    # every capture behind a long transaction; it is retried
                    # whole, a bounded number of times, then the error stands.
                    if type(exc).__name__ != "LockNotAvailable" or attempt == LOCK_RETRIES:
                        raise
                    conn.rollback()
                    time.sleep(LOCK_RETRY_DELAY_S * (attempt + 1))
        result["ran"].append(version)
    result["pending"] = []
    return result
