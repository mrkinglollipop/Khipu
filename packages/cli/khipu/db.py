"""Postgres connection helpers for Khipu CLI / mirror."""
from __future__ import annotations

import os
import threading
from contextvars import ContextVar
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
def resolve_dsn() -> str:
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
        if not _MIGRATION_DONE and isolated_dir() is None:
            # doctor connects from several threads at once; one migrates.
            with _MIGRATION_LOCK:
                if not _MIGRATION_DONE:
                    migrate_legacy_secrets()
                    _MIGRATION_DONE = True
        kc = get_dsn()
        if kc:
            return kc
    except Exception:
        # Never block connect on Keychain flakiness — fall through to file.
        pass
    try:
        from khipu.paths import dsn_file

        configured = dsn_file()
    except Exception:
        configured = Path(_env("KHIPU_DSN_FILE", "ALZY_DSN_FILE") or str(DEFAULT_DSN_FILE))
    path = Path(_env("KHIPU_DSN_FILE", "ALZY_DSN_FILE") or str(configured))
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    if LEGACY_DSN_FILE.is_file():
        return LEGACY_DSN_FILE.read_text(encoding="utf-8").strip()
    raise RuntimeError(
        "No Khipu DSN. Set KHIPU_DATABASE_URL, store in Keychain "
        "(pipe it to `khipu secrets --set database_url`), or create "
        f"{path} (chmod 600). See packages/cli/README.md"
    )


def conninfo_with_local_root_cert(dsn: str) -> str:
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
    from khipu.paths import root_cert_file

    cert = root_cert_file()
    if cert.is_file():
        params["sslrootcert"] = str(cert.resolve())
    return make_conninfo(**params)


def _keepalive_kwargs() -> dict:
    """TCP keepalives so a connection a network change silenced is given up
    after ``hub_dead_connection_after`` seconds, not the system's hours."""
    from khipu import offline

    after = int(offline.setting("hub_dead_connection_after") or 0)
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


# The role the next connect() takes when its caller names none. Set only for
# the length of one connect call by ``read_connection``, so nothing opened
# later in the same block (a write) inherits it.
_ROLE: ContextVar[str] = ContextVar("khipu_connect_role", default="owner")


def current_role() -> str:
    return _ROLE.get()


def read_connection(opener, **kwargs):
    """``opener(**kwargs)`` (``connect``, or the replica module's hub
    connect) as the search role. The opener is passed in so a test's stand-in for it
    still intercepts the call."""
    token = _ROLE.set("read")
    try:
        return opener(**kwargs)
    finally:
        _ROLE.reset(token)


def connect(*, autocommit: bool = False, statement_timeout_s: float | None = None,
            role: str | None = None, connect_timeout: float | None = None):
    """Bound connection setup; SQL timeouts belong to the operation's caller
    (``statement_timeout_s`` is how a caller asks for one).

    ``role="read"`` is for search and the other read tools: it connects as the
    hub's ``khipu_read`` role (``read_role``), provisioning it on first use.
    It connects as the owner, labelled ``owner-read``, only on a hub that
    cannot host a read role (and in an isolated run, which never sets one
    up). A full role, a refused password that could not be repaired or a
    failed provisioning raises (``read_role.ReadRoleUnavailable`` for the
    last two) and never takes an owner connection. Captures and every other
    write keep the default owner role."""
    role = role or _ROLE.get()
    owner_dsn = resolve_dsn()
    timeout = CONNECT_TIMEOUT_SECONDS if connect_timeout is None else connect_timeout
    if role == "read":
        return _connect_read(owner_dsn, autocommit=autocommit, statement_timeout_s=statement_timeout_s,
                             connect_timeout=timeout)
    if role != "owner":
        raise ValueError(f"unknown connection role {role!r}")
    return _open(owner_dsn, "owner", autocommit=autocommit, statement_timeout_s=statement_timeout_s,
                 connect_timeout=timeout)


def _open(dsn: str, label: str, *, autocommit: bool, statement_timeout_s: float | None,
          connect_timeout: float):
    from khipu import hub_reach, read_role

    conninfo = conninfo_with_local_root_cert(dsn)
    if statement_timeout_s is not None and statement_timeout_s > 0:
        conninfo = _with_statement_timeout(conninfo, statement_timeout_s)
    # A failed connect writes the hub-down memo and a success clears it, so the
    # prompt hook and the search tools skip a connect they know will fail. A
    # full read role is a hub that answered, not an outage.
    try:
        conn = psycopg.connect(
            conninfo,
            autocommit=autocommit,
            connect_timeout=connect_timeout,
            application_name=read_role.application_name(label),
            **_keepalive_kwargs(),
        )
    except psycopg.OperationalError as exc:
        if not read_role.role_limit_error(exc):
            hub_reach.note_unreachable(f"{type(exc).__name__}: {exc}")
        raise
    hub_reach.note_reachable()
    return conn


def _connect_read(owner_dsn: str, **kw):
    import os

    from khipu import read_role
    from khipu.paths import isolated_dir

    read = read_role.read_conninfo(owner_dsn)
    if read is None:
        if isolated_dir() is not None:
            return _open(owner_dsn, "owner-read", **kw)
        read = read_role.provision(owner_dsn=owner_dsn)
        if read is None:
            # The hub cannot host a read role: searches share the owner's
            # connections there rather than stop (doctor says so).
            return _open(owner_dsn, "owner-read", **kw)
    try:
        conn = _open(read, "read", **kw)
    except psycopg.OperationalError as exc:
        if not read_role.auth_error(exc):
            raise
        refusal = exc
    else:
        read_role.cache_validated(owner_dsn, read)
        return conn
    read_role.invalidate_cache()
    if (os.environ.get(read_role.READ_DSN_ENV) or "").strip():
        raise read_role.ReadRoleUnavailable(
            f"the hub refused {read_role.READ_DSN_ENV} ({type(refusal).__name__})") from refusal
    # The stored password was refused (rotated, or the role was dropped):
    # take the hub's current one, or a new one when that is the one refused.
    fresh = read_role.provision(owner_dsn=owner_dsn, refused=read, force=True)
    if fresh is not None and fresh != read:
        try:
            conn = _open(fresh, "read", **kw)
        except psycopg.OperationalError as exc:
            if not read_role.auth_error(exc):
                raise
            refusal = exc
        else:
            read_role.cache_validated(owner_dsn, fresh)
            return conn
    read_role.forget_read_dsn()
    raise read_role.ReadRoleUnavailable(
        f"the hub refused the search role's password and it could not be repaired ({type(refusal).__name__})"
    ) from refusal


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
    nothing, and ``table_columns`` asks again table by table."""
    missing = [t for t in tables if t not in _TABLE_COLUMNS_CACHE]
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
        return
    _TABLE_COLUMNS_CACHE.update(got)


def has_columns(cur, table: str, *names: str) -> bool:
    cols = table_columns(cur, table)
    return all(name in cols for name in names)


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
