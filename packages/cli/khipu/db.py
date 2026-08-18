"""Postgres connection helpers for Khipu CLI / mirror."""
from __future__ import annotations

import os
from pathlib import Path

import psycopg

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


def connect(*, autocommit: bool = False):
    return psycopg.connect(resolve_dsn(), autocommit=autocommit)


def dsn_configured() -> bool:
    try:
        resolve_dsn()
        return True
    except RuntimeError:
        return False
