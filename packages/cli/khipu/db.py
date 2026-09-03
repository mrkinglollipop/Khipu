"""Postgres connection helpers for Khipu CLI / mirror."""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

DEFAULT_DSN_FILE = Path.home() / ".config" / "khipu" / "dsn"
LEGACY_DSN_FILE = Path.home() / ".config" / "alzy" / "dsn"


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


def resolve_dsn() -> str:
    """Order: KHIPU_/ALZY_ DATABASE_URL → Keychain → data_dir/dsn."""
    global _MIGRATION_DONE

    env = _env("KHIPU_DATABASE_URL", "ALZY_DATABASE_URL")
    if env:
        return env
    try:
        from khipu.keychain import get_dsn, migrate_legacy_secrets

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
    ``root certificate file "/Users/matthewsc" does not exist`` (the first
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


def connect(*, autocommit: bool = False):
    return psycopg.connect(conninfo_with_local_root_cert(resolve_dsn()), autocommit=autocommit)


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


def has_columns(cur, table: str, *names: str) -> bool:
    cols = table_columns(cur, table)
    return all(name in cols for name in names)
