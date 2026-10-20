"""The hub's read role for search (docs/plans/2026-10-19-swarm-ready.md, A).

Search, owed, decisions, the graph and the gateway's read tools connect as
``khipu_read``: no superuser, ``SELECT`` only, a connection limit and two
timeouts. Captures, the outbox drain, migrations and the nightly stay on the
owner role, so a crowd of searches can fill ``khipu_read`` without taking a
connection a save needs.

Provisioning needs no owner step. The first upgraded client that connects as
owner runs ``ensure_read_role``: it creates the role with a generated password
and keeps that password in ``khipu_role_secrets``, which only the owner can
read. Every other client holding the owner DSN reads the password from there
and stores the read DSN (Keychain, plus a 0600 file for hosts without one).
A client that cannot provision keeps searching as the owner, labelled
``khipu/owner-read/...`` in ``application_name`` so doctor can name it.

The three role settings are the owner's in the app; saving one runs
``ALTER ROLE`` through the owner connection (0 means no limit).
"""
from __future__ import annotations

import json
import re
import secrets
import socket
import time
from pathlib import Path
from typing import Any

from khipu.output_guard import secret_result

READ_ROLE = "khipu_read"
SECRETS_TABLE = "khipu_role_secrets"
READ_DSN_ENV = "KHIPU_READ_DATABASE_URL"
READ_DSN_ACCOUNT = "read_database_url"
# A failed provisioning is not retried on every search.
PROVISION_RETRY_AFTER_S = 600
_ADVISORY_KEY = "khipu_read_role"

SETTINGS: dict[str, tuple[str, Any, str]] = {
    "read_role_connection_limit": (
        "count", 20,
        "Most hub connections searches may hold at once, across every Mac and the gateway (0: no limit)"),
    "read_role_statement_timeout": (
        "duration", 30,
        "Longest one search statement may run on the hub, in seconds (0: no limit)"),
    "read_role_idle_in_transaction_timeout": (
        "duration", 60,
        "How soon the hub closes a search connection left idle in a transaction, in seconds (0: no limit)"),
}

# ``reserved_connections`` is postmaster context: written now, in force after
# the hub restarts. It keeps 6 slots (plus superuser_reserved_connections) that
# khipu_read can never take. ALTER SYSTEM cannot run inside a transaction.
RESERVED_CONNECTIONS = 6
RESERVED_CONNECTIONS_CHECK_SQL = "SELECT context FROM pg_settings WHERE name = 'reserved_connections'"
RESERVED_CONNECTIONS_SQL = f"ALTER SYSTEM SET reserved_connections = {RESERVED_CONNECTIONS}"
RESERVED_CONNECTIONS_ROLLBACK_SQL = "ALTER SYSTEM RESET reserved_connections"


# ---- settings ---------------------------------------------------------------

def _coerce(kind: str, value: Any, key: str) -> Any:
    from khipu.routing import _coerce as routing_coerce

    return routing_coerce(kind, value, key)


def settings(config: dict | None = None) -> dict[str, Any]:
    if config is None:
        from khipu.config import load_config

        config = load_config()
    out: dict[str, Any] = {}
    for key, (kind, default, _words) in SETTINGS.items():
        value = default
        if config.get(key) is not None:
            try:
                value = _coerce(kind, config[key], key)
            except (ValueError, TypeError):
                value = default
        out[key] = value
    return out


def settings_status() -> dict[str, dict]:
    from khipu.config import load_config

    config = load_config()
    values = settings(config)
    out = {}
    for key, (kind, default, words) in SETTINGS.items():
        source = "default"
        if config.get(key) is not None:
            try:
                _coerce(kind, config[key], key)
                source = "file"
            except (ValueError, TypeError):
                source = "invalid"
        out[key] = {"value": values[key], "default": default, "kind": kind, "source": source, "words": words}
    return out


def set_setting(key: str, raw: Any, *, conn=None) -> Path:
    """Apply one role setting on the hub, then store it (``None``: default).

    The hub is changed first: a value this Mac stored but the hub never took
    would read as in force when it is not. Raises ValueError when the hub
    cannot be changed."""
    from khipu.config import config_file, load_config, mutate_config

    if key not in SETTINGS:
        raise KeyError(f"unknown read role setting {key!r}")
    kind, default, _words = SETTINGS[key]
    value = default if raw is None else _coerce(kind, raw, key)
    values = {**settings(load_config()), key: value}
    try:
        if conn is not None:
            result = ensure_read_role(conn, values=values)
        else:
            from khipu.db import connect

            with connect(role="owner") as own:
                result = ensure_read_role(own, values=values)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"not saved: the hub could not be changed ({type(exc).__name__}: {exc})") from exc
    if not result.get("ok"):
        raise ValueError(f"not saved: {result.get('reason') or 'the hub refused the change'}")
    with mutate_config() as data:
        if raw is None:
            data.pop(key, None)
        else:
            data[key] = value
    return config_file()


# ---- application_name ---------------------------------------------------------

def _machine() -> str:
    name = socket.gethostname().split(".")[0]
    return re.sub(r"[^A-Za-z0-9-]", "-", name)[:24] or "unknown"


def application_name(role: str) -> str:
    """``khipu/<role>/<machine>/<version>``, within Postgres's 63 bytes."""
    from khipu import __version__

    return f"khipu/{role}/{_machine()}/{__version__}"[:63]


# ---- the role on the hub --------------------------------------------------------

def _seconds_literal(seconds: Any) -> str:
    return f"{int(round(float(seconds) * 1000))}ms"


def _can_create_roles(cur) -> bool:
    cur.execute("SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname = current_user")
    row = cur.fetchone()
    return bool(row and row[0])


def _apply_role(cur, values: dict[str, Any]) -> None:
    from psycopg import sql

    role = sql.Identifier(READ_ROLE)
    limit = int(values["read_role_connection_limit"])
    cur.execute(sql.SQL(
        "ALTER ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
        " CONNECTION LIMIT {}").format(role, sql.Literal(limit if limit > 0 else -1)))
    for guc, key in (("statement_timeout", "read_role_statement_timeout"),
                     ("idle_in_transaction_session_timeout", "read_role_idle_in_transaction_timeout")):
        cur.execute(sql.SQL("ALTER ROLE {} SET {} = {}").format(
            role, sql.Identifier(guc), sql.Literal(_seconds_literal(values[key]))))
    cur.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(role))
    cur.execute("SELECT current_database()")
    database = cur.fetchone()[0]
    cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), role))
    cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
    cur.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(role))
    # Tables a later migration creates (as this owner) are readable too.
    cur.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {}").format(role))
    cur.execute(sql.SQL("REVOKE ALL ON {} FROM PUBLIC, {}").format(sql.Identifier(SECRETS_TABLE), role))


def ensure_read_role(conn, *, values: dict[str, Any] | None = None,
                     reset_password: bool = False) -> dict[str, Any]:
    """Create or bring up to date ``khipu_read`` on the owner connection
    ``conn`` and return ``{"ok", "created", "password_reset", "reason"}``.

    Idempotent and safe to run from several clients at once (an advisory
    lock serialises them). The password is generated once and kept in
    ``khipu_role_secrets``; ``reset_password`` replaces it (a stored one the
    hub no longer accepts). Never returns the password."""
    from psycopg import sql

    values = {**settings(), **(values or {})}
    out: dict[str, Any] = {"ok": False, "created": False, "password_reset": False}
    try:
        with conn.cursor() as cur:
            if not _can_create_roles(cur):
                conn.rollback()
                out["reason"] = "the owner role cannot create roles (needs CREATEROLE or superuser)"
                return out
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_ADVISORY_KEY,))
            cur.execute(sql.SQL(
                "CREATE TABLE IF NOT EXISTS {} (role text PRIMARY KEY, secret text NOT NULL,"
                " created_at timestamptz NOT NULL DEFAULT now(),"
                " rotated_at timestamptz NOT NULL DEFAULT now())").format(sql.Identifier(SECRETS_TABLE)))
            cur.execute(sql.SQL("REVOKE ALL ON {} FROM PUBLIC").format(sql.Identifier(SECRETS_TABLE)))
            cur.execute(sql.SQL("SELECT secret FROM {} WHERE role = %s").format(sql.Identifier(SECRETS_TABLE)),
                        (READ_ROLE,))
            row = cur.fetchone()
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (READ_ROLE,))
            exists = cur.fetchone() is not None
            password = row[0] if row else None
            if password is None or reset_password or not exists:
                password = password if (password and not reset_password) else secrets.token_urlsafe(32)
                verb = "ALTER" if exists else "CREATE"
                cur.execute(sql.SQL(verb + " ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(READ_ROLE), sql.Literal(password)))
                cur.execute(sql.SQL(
                    "INSERT INTO {} (role, secret) VALUES (%s, %s) ON CONFLICT (role) DO UPDATE"
                    " SET secret = EXCLUDED.secret, rotated_at = now()").format(sql.Identifier(SECRETS_TABLE)),
                    (READ_ROLE, password))
                out["created"] = not exists
                out["password_reset"] = exists
            _apply_role(cur, values)
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        out["reason"] = f"{type(exc).__name__}: {exc}"[:300]
        return out
    out["ok"] = True
    return out


@secret_result("read_role_password")
def hub_password(conn) -> str | None:
    """The read role's password as the hub keeps it (owner connection)."""
    from psycopg import sql

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{SECRETS_TABLE}",))
            if not cur.fetchone()[0]:
                conn.rollback()
                return None
            cur.execute(sql.SQL("SELECT secret FROM {} WHERE role = %s").format(sql.Identifier(SECRETS_TABLE)),
                        (READ_ROLE,))
            row = cur.fetchone()
        conn.rollback()
        return row[0] if row else None
    except Exception:  # noqa: BLE001
        conn.rollback()
        return None


# ---- this client's read DSN -----------------------------------------------------

def _target(conninfo: str) -> tuple[str, str, str]:
    from psycopg.conninfo import conninfo_to_dict

    params = conninfo_to_dict(conninfo)
    return (str(params.get("host") or params.get("hostaddr") or ""), str(params.get("port") or "5432"),
            str(params.get("dbname") or params.get("user") or ""))


def read_conninfo_for(owner_dsn: str, password: str) -> str:
    """The owner DSN with the read role's user and password."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = {key: val for key, val in conninfo_to_dict(owner_dsn).items() if val is not None}
    if not params.get("dbname") and params.get("user"):
        params["dbname"] = params["user"]
    params["user"] = READ_ROLE
    params["password"] = password
    return make_conninfo(**params)


def _read_dsn_file() -> Path:
    from khipu.paths import dsn_file

    return dsn_file().with_name("read_dsn")


def _memo_file() -> Path:
    from khipu.paths import data_dir

    return data_dir() / "read_role_memo.json"


@secret_result("database_url")
def stored_read_dsn() -> str | None:
    import os

    env = (os.environ.get(READ_DSN_ENV) or "").strip()
    if env:
        return env
    try:
        from khipu.keychain import get_password

        value = get_password(READ_DSN_ACCOUNT)
        if value:
            return value
    except Exception:  # noqa: BLE001
        pass
    try:
        path = _read_dsn_file()
        if path.is_file():
            return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        pass
    return None


def store_read_dsn(conninfo: str) -> None:
    from khipu.join import write_private_file
    from khipu.keychain import keychain_available, set_password

    write_private_file(_read_dsn_file(), (conninfo.strip() + "\n").encode("utf-8"))
    if keychain_available():
        set_password(READ_DSN_ACCOUNT, conninfo.strip())


def forget_read_dsn() -> None:
    try:
        _read_dsn_file().unlink()
    except OSError:
        pass
    try:
        from khipu.keychain import SERVICE, _security, keychain_available
        from khipu.paths import isolated_dir

        if keychain_available() and isolated_dir() is None:
            _security("delete-generic-password", "-s", SERVICE, "-a", READ_DSN_ACCOUNT)
    except Exception:  # noqa: BLE001
        pass


@secret_result("database_url")
def read_conninfo(owner_dsn: str) -> str | None:
    """This client's read DSN when it targets the owner DSN's database."""
    import os

    if (os.environ.get(READ_DSN_ENV) or "").strip():
        return os.environ[READ_DSN_ENV].strip()
    stored = stored_read_dsn()
    if not stored:
        return None
    try:
        if _target(stored) != _target(owner_dsn):
            return None
    except Exception:  # noqa: BLE001
        return None
    return stored


def _memo() -> dict[str, Any]:
    try:
        return json.loads(_memo_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_memo(data: dict[str, Any]) -> None:
    from khipu.paths import isolated_dir

    if isolated_dir() is not None:
        return
    try:
        path = _memo_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def provision_blocked() -> str | None:
    memo = _memo()
    failed_at = memo.get("failed_at")
    if isinstance(failed_at, (int, float)) and time.time() - failed_at < PROVISION_RETRY_AFTER_S:
        return str(memo.get("reason") or "provisioning failed recently")
    return None


@secret_result("database_url")
def provision(owner_conn=None, owner_dsn: str | None = None, *, stale_password: bool = False,
              force: bool = False) -> str | None:
    """Make sure this client holds a working read DSN; return it, or None.

    Reads the hub's password when one is kept, else creates the role. With
    ``stale_password`` (the stored DSN was refused) the hub's copy is re-read
    and, only when it is the one that was refused, replaced. Isolated runs
    (the promotion gate) never write, so they search as the owner."""
    from khipu.paths import isolated_dir

    if isolated_dir() is not None:
        return None
    if not force and provision_blocked():
        return None
    from khipu import db

    owner_dsn = owner_dsn or db.resolve_dsn()
    own = owner_conn
    opened = None
    try:
        if own is None:
            opened = db.connect(role="owner")
            own = opened
        previous = stored_read_dsn() if stale_password else None
        password = hub_password(own)
        refused = False
        if password and previous:
            refused = read_conninfo_for(owner_dsn, password) == previous
        if not password or refused:
            result = ensure_read_role(own, reset_password=refused)
            if not result.get("ok"):
                _write_memo({"failed_at": time.time(), "reason": result.get("reason")})
                return None
            password = hub_password(own)
        if not password:
            _write_memo({"failed_at": time.time(), "reason": "no read role password on the hub"})
            return None
        conninfo = read_conninfo_for(owner_dsn, password)
        store_read_dsn(conninfo)
        _write_memo({"provisioned_at": time.time()})
        return conninfo
    except Exception as exc:  # noqa: BLE001 - search must never fail on provisioning
        _write_memo({"failed_at": time.time(), "reason": f"{type(exc).__name__}: {exc}"[:300]})
        return None
    finally:
        if opened is not None:
            try:
                opened.close()
            except Exception:  # noqa: BLE001
                pass


def role_limit_error(exc: BaseException) -> bool:
    """The read role is full: overflow, never a fallback to the owner."""
    text = str(exc).lower()
    return "too many connections for role" in text


def auth_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return ("password authentication failed" in text or f'role "{READ_ROLE}" does not exist' in text
            or "permission denied for database" in text)


def stage_reserved_connections(conn) -> dict[str, Any]:
    """Write ``reserved_connections`` for the next hub restart (autocommit
    owner connection). Refuses unless the server reports it as postmaster
    context, the only context this plan relies on."""
    with conn.cursor() as cur:
        cur.execute(RESERVED_CONNECTIONS_CHECK_SQL)
        row = cur.fetchone()
        if not row or row[0] != "postmaster":
            return {"ok": False, "reason": f"reserved_connections context is {row[0] if row else 'missing'}"}
        cur.execute(RESERVED_CONNECTIONS_SQL)
    return {"ok": True, "statement": RESERVED_CONNECTIONS_SQL, "rollback": RESERVED_CONNECTIONS_ROLLBACK_SQL,
            "in_force_after": "the hub restarts"}


# ---- doctor ------------------------------------------------------------------------

def doctor_block(conn=None) -> dict[str, Any]:
    """This client's search role, the role settings, and clients still
    searching as the owner. Warns; never gates doctor's ``ok``."""
    from khipu import db

    out: dict[str, Any] = {"settings": settings(), "warnings": []}
    try:
        owner_dsn = db.resolve_dsn()
    except RuntimeError:
        out["state"] = "not_configured"
        return out
    out["state"] = "read" if read_conninfo(owner_dsn) else "owner"
    blocked = provision_blocked()
    if out["state"] == "owner":
        out["warnings"].append(
            "This Mac searches as the hub owner"
            + (f" (the read role could not be set up: {blocked})" if blocked else "")
            + "; a crowd of searches can then take connections captures need.")
    opened = None
    try:
        if conn is None:
            opened = db.connect(role="owner")
            conn = opened
        with conn.cursor() as cur:
            cur.execute("SELECT rolconnlimit FROM pg_roles WHERE rolname = %s", (READ_ROLE,))
            row = cur.fetchone()
            out["role_exists"] = row is not None
            if row is not None:
                out["role_connection_limit"] = row[0]
            cur.execute(
                "SELECT COALESCE(NULLIF(application_name, ''), '(no label)'),"
                " COALESCE(host(client_addr), 'local'), count(*)"
                " FROM pg_stat_activity WHERE usename = current_user AND pid <> pg_backend_pid()"
                " AND backend_type = 'client backend'"
                " AND (application_name NOT LIKE 'khipu/%%' OR application_name LIKE 'khipu/owner-read/%%')"
                " GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 20")
            clients = [{"application_name": a, "address": h, "connections": int(n)} for a, h, n in cur.fetchall()]
        conn.rollback()
        out["owner_searchers"] = clients
        if clients:
            named = ", ".join(f"{c['application_name']} at {c['address']} ({c['connections']})" for c in clients[:5])
            out["warnings"].append(
                "Clients connected as the hub owner that are not labelled as capture or upkeep, so they may be "
                f"searching as the owner (not upgraded, or no read role): {named}.")
    except Exception as exc:  # noqa: BLE001 - a failed check must not crash doctor
        out["error"] = f"{type(exc).__name__}: {exc}"[:200]
    finally:
        if opened is not None:
            try:
                opened.close()
            except Exception:  # noqa: BLE001
                pass
    return out
