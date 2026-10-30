"""Postgres connection helpers for Khipu CLI / mirror."""
from __future__ import annotations

import os
import threading
from typing import Any
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from khipu.output_guard import secret_result

DEFAULT_DSN_FILE = Path.home() / ".config" / "khipu" / "dsn"
LEGACY_DSN_FILE = Path.home() / ".config" / "alzy" / "dsn"
CONNECT_TIMEOUT_SECONDS = 5


def _env(*names: str) -> str:
    for name in names:
        val = (os.environ.get(name) or "").strip()
        if val:
            return val
    return ""


# The Alzy→Khipu secret migration is a one-time job, but it used to run inside
# every resolve_dsn() — so every connect() paid up to four `security` subprocess
# spawns plus a legacy-directory walk before a single query ran, and doctor
# opens a connection per check (audit 2026-08-17).
_MIGRATION_DONE = False
_MIGRATION_LOCK = threading.Lock()


@secret_result("database_url")
def resolve_dsn(*, migrate_legacy: bool = True) -> str:
    """Order: KHIPU_/ALZY_ DATABASE_URL → Keychain → data_dir/dsn."""
    global _MIGRATION_DONE

    env = _env("KHIPU_DATABASE_URL", "ALZY_DATABASE_URL")
    if env:
        return env
    try:
        from khipu.keychain import get_dsn, migrate_legacy_secrets

        from khipu.paths import isolated_dir

        # The migration copies legacy files into the live config dir; an
        # isolated run (the promotion gate) only reads.
        if migrate_legacy and not _MIGRATION_DONE and isolated_dir() is None:
            # doctor connects from several threads at once; one migrates.
            with _MIGRATION_LOCK:
                if not _MIGRATION_DONE:
                    migrate_legacy_secrets()
                    _MIGRATION_DONE = True
        if migrate_legacy:
            kc = get_dsn()
        else:
            # get_dsn itself copies a legacy Keychain item on read. Preview
            # must only read, including when that is the configured secret.
            from khipu.keychain import DSN_ACCOUNT, LEGACY_SERVICE, get_password

            kc = get_password(DSN_ACCOUNT) or get_password(DSN_ACCOUNT, service=LEGACY_SERVICE)
        if kc:
            return kc
    except Exception:
        # Never block connect on Keychain flakiness — fall through to file.
        pass
    try:
        from khipu.paths import credential_dir, dsn_file

        configured = dsn_file() if migrate_legacy else credential_dir() / "dsn"
    except Exception:
        configured = Path(_env("KHIPU_DSN_FILE", "ALZY_DSN_FILE") or str(DEFAULT_DSN_FILE))
    path = Path(_env("KHIPU_DSN_FILE", "ALZY_DSN_FILE") or str(configured))
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    if not _explicit_dsn_location():
        # A moved data folder holds no DSN (set_data_dir never copies it);
        # the config one comes first, the order the desktop reads them.
        for fallback in (DEFAULT_DSN_FILE, LEGACY_DSN_FILE):
            if fallback.is_file():
                return fallback.read_text(encoding="utf-8").strip()
    raise RuntimeError(
        "No Khipu DSN. Set KHIPU_DATABASE_URL, store in Keychain "
        "(pipe it to `khipu secrets --set database_url`), or create "
        f"{path} (chmod 600). See packages/cli/README.md"
    )


def _explicit_dsn_location() -> bool:
    from khipu.paths import explicit_dsn_location

    return explicit_dsn_location()


def conninfo_with_local_root_cert(dsn: str, *, create_data_dir: bool = True) -> str:
    """Keyword conninfo for libpq, with this Mac's ``root.crt`` forced in.

    Stored DSNs are ``postgres://`` URIs. Putting ``sslrootcert=/Users/…``
    in the query string has shown up at TLS time as
    ``root certificate file "/Users/<16 chars>" does not exist`` (the first
    16 chars of the path) even when ``~/.config/khipu/root.crt`` was on
    disk. Percent-encoding slashes in the URI does not survive libpq's
    decode. Keyword/value conninfo plus an overwrite from the local cert
    file is the connect-time contract; ``resolve_dsn()`` stays the raw
    Keychain/file URI so password parsers that split on ``://`` still work.
    """
    params = {key: val for key, val in conninfo_to_dict(dsn).items() if val is not None}
    from khipu.paths import credential_dir, root_cert_file

    cert = root_cert_file() if create_data_dir else credential_dir() / "root.crt"
    if cert.is_file():
        params["sslrootcert"] = str(cert.resolve())
    return make_conninfo(**params)


def _keepalive_kwargs(*, read_only: bool = False) -> dict:
    """TCP keepalives so a connection a network change silenced is given up
    after ``hub_dead_connection_after`` seconds, not the system's hours."""
    from khipu import offline

    if read_only:
        from khipu.config import read_config_unsettled

        # load_config may finish a pending account write. A preview reads the
        # document as stored without initiating that recovery.
        setting = offline.settings(config=read_config_unsettled())["hub_dead_connection_after"]
    else:
        setting = offline.setting("hub_dead_connection_after")
    after = int(setting or 0)
    if after <= 0:
        return {}
    return {
        "keepalives": 1,
        "keepalives_idle": max(1, after // 2),
        "keepalives_interval": max(1, after // 6),
        "keepalives_count": 3,
    }


def _with_statement_timeout(conninfo: str, seconds: float) -> str:
    """``conninfo`` with ``-c statement_timeout`` added to its options."""
    params = {key: val for key, val in conninfo_to_dict(conninfo).items() if val is not None}
    flag = f"-c statement_timeout={int(seconds * 1000)}"
    existing = (params.get("options") or "").strip()
    params["options"] = f"{existing} {flag}" if existing else flag
    return make_conninfo(**params)


def connect(*, autocommit: bool = False, statement_timeout_s: float | None = None,
            connect_timeout: int | None = None, record_reachability: bool = True):
    """Bound connection setup; SQL timeouts belong to the operation's caller
    (``statement_timeout_s`` is how a caller asks for one, ``connect_timeout``
    one bounded by its own deadline). Read-only callers can disable local
    reachability-memo writes, directory creation and local recovery with
    ``record_reachability=False``."""
    dsn = resolve_dsn() if record_reachability else resolve_dsn(migrate_legacy=False)
    conninfo = (conninfo_with_local_root_cert(dsn) if record_reachability
                else conninfo_with_local_root_cert(dsn, create_data_dir=False))
    if statement_timeout_s is not None and statement_timeout_s > 0:
        conninfo = _with_statement_timeout(conninfo, statement_timeout_s)
    from khipu import hub_reach

    # A failed connect writes the hub-down memo and a success clears it, so the
    # prompt hook and the search tools skip a connect they know will fail.
    try:
        conn = psycopg.connect(
            conninfo,
            autocommit=autocommit,
            connect_timeout=CONNECT_TIMEOUT_SECONDS if connect_timeout is None else connect_timeout,
            **_keepalive_kwargs(read_only=not record_reachability),
        )
    except psycopg.OperationalError as exc:
        if record_reachability:
            hub_reach.note_unreachable(f"{type(exc).__name__}: {exc}")
        raise
    if record_reachability:
        hub_reach.note_reachable()
    return conn


def dsn_configured() -> bool:
    try:
        resolve_dsn()
        return True
    except RuntimeError:
        return False


# ---- schema introspection (consolidates embed._episode_schema_flags,
# drift._has_column, and the sqlite-replica module's column-check helper
# into one helper) -----------------------------------------------------
#
# A per-process cache: schema does not change mid-process (a migration lands
# between runs, not mid-run), and doctor/search/drift each open a fresh
# connection per check — without this every one of them re-queries
# information_schema for the same table.

_TABLE_COLUMNS_CACHE: dict[str, set[str]] = {}

# What ``prime_table_columns`` read, per database: the process cache above is
# not keyed by database, so a process that talks to more than one hub (the
# warm recall daemon) must not take one hub's columns as another's.
_SCHEMA_SEEN: dict[Any, dict[str, set[str]]] = {}


def database_identity(conn_or_cur) -> Any:
    """Which database a connection (or its cursor) talks to: host, port and
    name. A stand-in without connection info is told apart by identity."""
    conn = getattr(conn_or_cur, "connection", conn_or_cur)
    info = getattr(conn, "info", None)
    try:
        return (info.host, info.port, info.dbname)
    except AttributeError:
        return id(conn)


def columns_seen(cur, table: str) -> set[str] | None:
    """``table``'s columns as ``prime_table_columns`` read them from THIS
    cursor's database, or ``None`` when it never read that database."""
    return _SCHEMA_SEEN.get(database_identity(cur), {}).get(table)


def table_columns(cur, table: str) -> set[str]:
    """Column names for ``table`` on THIS connection's database, cached per
    process. Callers that need to see a just-applied migration mid-process
    (tests, ``khipu migrate``) should clear ``db._TABLE_COLUMNS_CACHE``."""
    if table not in _TABLE_COLUMNS_CACHE:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s",
            (table,),
        )
        _TABLE_COLUMNS_CACHE[table] = {r[0] for r in cur.fetchall()}
    return _TABLE_COLUMNS_CACHE[table]


def prime_table_columns(conn, *tables: str) -> None:
    """``table_columns`` for every one of ``tables`` not cached yet, in one
    query instead of one each. Best-effort: an answer it can't read caches
    nothing, and ``table_columns`` asks again table by table. Call it before
    other work in a transaction: a failed read rolls the transaction back so
    the per-table reads can run."""
    seen = _SCHEMA_SEEN.setdefault(database_identity(conn), {})
    missing = [t for t in tables if t not in _TABLE_COLUMNS_CACHE or t not in seen]
    if not missing:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = ANY(%s)",
                (missing,),
            )
            got: dict[str, set[str]] = {t: set() for t in missing}
            for table, column in cur.fetchall():
                got[table].add(column)
    except Exception:  # noqa: BLE001  (the per-table reads still answer)
        if not getattr(conn, "autocommit", False):
            try:
                conn.rollback()  # a failed statement aborts the transaction
            except Exception:  # noqa: BLE001
                pass
        return
    _TABLE_COLUMNS_CACHE.update(got)
    seen.update(got)


def has_columns(cur, table: str, *names: str) -> bool:
    """Whether ``table`` has every named column. Only a yes is cached: a
    long-lived process (MCP server, gateway) that started before a migration
    must see the new columns on its next write, not after a restart — same
    rule ``has_table`` uses for a missing table."""
    cols = table_columns(cur, table)
    if all(name in cols for name in names):
        return True
    _TABLE_COLUMNS_CACHE.pop(table, None)
    return False


def has_table(cur, table: str) -> bool:
    """Whether ``table`` exists, sharing ``table_columns``' cache. Only a yes
    is cached: a long-lived process (MCP server, gateway) that started before
    a migration must see the new table on its next write, not after a restart."""
    if _TABLE_COLUMNS_CACHE.get(table):
        return True
    _TABLE_COLUMNS_CACHE.pop(table, None)
    cols = table_columns(cur, table)
    if not cols:
        _TABLE_COLUMNS_CACHE.pop(table, None)
    return bool(cols)
