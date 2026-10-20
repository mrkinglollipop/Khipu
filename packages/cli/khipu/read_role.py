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
Only a hub that cannot host the role at all (the owner has neither
CREATEROLE nor superuser, and no password is kept) searches as the owner,
labelled ``khipu/owner-read/...`` in ``application_name``; doctor says so.
Anywhere else a read role that cannot be used right now (refused password
that could not be repaired, a failed provisioning, an existing role with
more rights than search needs, a full role) raises ``ReadRoleUnavailable``
and is never answered by an owner connection.

The three role settings are the owner's in the app; saving one runs
``ALTER ROLE`` for that setting only, through the owner connection (0 means
no limit), and the app shows the values the hub holds.

The password never appears in statement text: the role gets a SCRAM-SHA-256
verifier computed here, and the plaintext kept for other clients is written
as a bound parameter with statement and parameter logging off for that
transaction where the owner may turn it off.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import socket
import time
from pathlib import Path
from typing import Any

import psycopg

from khipu.output_guard import secret_result

READ_ROLE = "khipu_read"
SECRETS_TABLE = "khipu_role_secrets"
READ_DSN_ENV = "KHIPU_READ_DATABASE_URL"
READ_DSN_ACCOUNT = "read_database_url"
# A failed provisioning is not retried on every search.
PROVISION_RETRY_AFTER_S = 600
# A hub found unable to host the role is asked again this often.
CANNOT_HOST_RECHECK_S = 3600
_ADVISORY_KEY = "khipu_read_role"
SCRAM_ITERATIONS = 4096
# Role attributes search never needs; an existing khipu_read holding any is
# refused, not altered.
_UNSAFE_ATTRIBUTES = ("rolsuper", "rolcreaterole", "rolcreatedb", "rolreplication", "rolbypassrls")


class ReadRoleUnavailable(psycopg.OperationalError):
    """The hub has, or can have, a read role, but this client cannot use it
    now. Search answers as for an unreachable hub (khipu.hub_errors), never
    through the owner role."""


class UnsafeReadRole(RuntimeError):
    """An existing ``khipu_read`` has rights beyond SELECT-only search."""

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


def _duration_seconds(text: str) -> float:
    match = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*(us|ms|s|min|h|d)?\s*", text)
    if not match:
        raise ValueError(text)
    scale = {"us": 1e-6, "ms": 1e-3, "s": 1, "min": 60, "h": 3600, "d": 86400}[match.group(2) or "ms"]
    seconds = float(match.group(1)) * scale
    return int(seconds) if seconds == int(seconds) else seconds


def hub_settings(conn) -> dict[str, Any] | None:
    """The three settings as the hub holds them on ``khipu_read`` (0: no
    limit), or None when the role does not exist."""
    with conn.cursor() as cur:
        cur.execute("SELECT rolconnlimit, rolconfig FROM pg_roles WHERE rolname = %s", (READ_ROLE,))
        row = cur.fetchone()
    conn.rollback()
    if row is None:
        return None
    limit, config = row
    gucs = dict(item.split("=", 1) for item in (config or []) if "=" in item)
    out: dict[str, Any] = {"read_role_connection_limit": max(int(limit), 0)}
    for guc, key in (("statement_timeout", "read_role_statement_timeout"),
                     ("idle_in_transaction_session_timeout", "read_role_idle_in_transaction_timeout")):
        try:
            out[key] = _duration_seconds(gucs[guc]) if guc in gucs else 0
        except ValueError:
            out[key] = None
    return out


def _hub_settings_quietly(conn=None) -> dict[str, Any] | None:
    from khipu import hub_reach

    opened = None
    try:
        if conn is None:
            if hub_reach.known_unreachable() is not None:
                return None
            from khipu.db import connect

            opened = conn = connect(role="owner", connect_timeout=3)
        return hub_settings(conn)
    except Exception:  # noqa: BLE001 - the stored values are shown instead, marked so
        return None
    finally:
        if opened is not None:
            try:
                opened.close()
            except Exception:  # noqa: BLE001
                pass


def settings_status(*, read_hub: bool = False, conn=None) -> dict[str, dict]:
    """Each setting with its default and this Mac's stored source. With
    ``read_hub`` (or ``conn``) ``value`` is what the hub holds, and
    ``on_hub`` says whether the hub could be read; the role is the hub's,
    so another Mac's change shows here, not this Mac's stored copy."""
    from khipu.config import load_config

    config = load_config()
    values = settings(config)
    hub = _hub_settings_quietly(conn) if (read_hub or conn is not None) else None
    out = {}
    for key, (kind, default, words) in SETTINGS.items():
        source = "default"
        if config.get(key) is not None:
            try:
                _coerce(kind, config[key], key)
                source = "file"
            except (ValueError, TypeError):
                source = "invalid"
        row = {"value": values[key], "default": default, "kind": kind, "source": source, "words": words}
        if read_hub or conn is not None:
            on_hub = hub is not None and hub.get(key) is not None
            row["on_hub"] = on_hub
            if on_hub:
                row["value"] = hub[key]
        out[key] = row
    return out


def set_setting(key: str, raw: Any, *, conn=None) -> Path:
    """Apply one role setting on the hub, then store it (``None``: default).

    The hub is changed first: a value this Mac stored but the hub never took
    would read as in force when it is not. Raises ValueError when the hub
    cannot be changed."""
    from khipu.config import config_file, mutate_config

    if key not in SETTINGS:
        raise KeyError(f"unknown read role setting {key!r}")
    kind, default, _words = SETTINGS[key]
    value = default if raw is None else _coerce(kind, raw, key)
    # Only this setting: the other two are the hub's, possibly set from
    # another Mac, and this Mac's stored copies may be stale.
    values = {key: value}
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


def scram_verifier(password: str, *, salt: bytes | None = None, iterations: int = SCRAM_ITERATIONS) -> str:
    """The SCRAM-SHA-256 verifier PostgreSQL stores for ``password`` (RFC
    7677; the generated passwords are ASCII, which SASLprep leaves alone).
    Given as the role's password, the hub keeps it as is, so the plaintext
    never reaches statement text or the server log."""
    salt = secrets.token_bytes(16) if salt is None else salt
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", "sha256").digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", "sha256").digest()

    def b64(raw: bytes) -> str:
        return base64.b64encode(raw).decode("ascii")

    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def _role_attributes(cur) -> dict[str, Any] | None:
    cur.execute("SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls, rolcanlogin,"
                " rolconnlimit FROM pg_roles WHERE rolname = %s", (READ_ROLE,))
    row = cur.fetchone()
    if row is None:
        return None
    names = (*_UNSAFE_ATTRIBUTES, "rolcanlogin", "rolconnlimit")
    return dict(zip(names, row))


def _unsafe_reason(cur, attributes: dict[str, Any]) -> str | None:
    """Why an existing ``khipu_read`` is not fit for search, or None.
    Altering such a role blindly could fail (a CREATEROLE owner may not
    change these attributes) or quietly widen what another role granted."""
    held = [name.removeprefix("rol") for name in _UNSAFE_ATTRIBUTES if attributes.get(name)]
    if held:
        return f"the existing {READ_ROLE} role has {', '.join(held)}"
    cur.execute("SELECT string_agg(g.rolname, ', ' ORDER BY g.rolname) FROM pg_auth_members m"
                " JOIN pg_roles g ON g.oid = m.roleid JOIN pg_roles r ON r.oid = m.member"
                " WHERE r.rolname = %s", (READ_ROLE,))
    member_of = cur.fetchone()[0]
    if member_of:
        return f"the existing {READ_ROLE} role is a member of {member_of}"
    cur.execute("SELECT string_agg(c.relname, ', ' ORDER BY c.relname) FROM pg_class c"
                " JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')"
                " AND (has_table_privilege(%s, c.oid, 'INSERT') OR has_table_privilege(%s, c.oid, 'UPDATE')"
                " OR has_table_privilege(%s, c.oid, 'DELETE') OR has_table_privilege(%s, c.oid, 'TRUNCATE'))",
                (READ_ROLE,) * 4)
    writable = cur.fetchone()[0]
    if writable:
        return f"the existing {READ_ROLE} role can write to {writable[:120]}"
    return None


def _apply_settings(cur, values: dict[str, Any]) -> None:
    """``ALTER ROLE`` for exactly the settings in ``values``."""
    from psycopg import sql

    role = sql.Identifier(READ_ROLE)
    if "read_role_connection_limit" in values:
        limit = int(values["read_role_connection_limit"])
        cur.execute(sql.SQL("ALTER ROLE {} CONNECTION LIMIT {}").format(
            role, sql.Literal(limit if limit > 0 else -1)))
    for guc, key in (("statement_timeout", "read_role_statement_timeout"),
                     ("idle_in_transaction_session_timeout", "read_role_idle_in_transaction_timeout")):
        if key in values:
            cur.execute(sql.SQL("ALTER ROLE {} SET {} = {}").format(
                role, sql.Identifier(guc), sql.Literal(_seconds_literal(values[key]))))


def _apply_grants(cur) -> None:
    from psycopg import sql

    role = sql.Identifier(READ_ROLE)
    cur.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(role))
    cur.execute("SELECT current_database()")
    database = cur.fetchone()[0]
    cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), role))
    cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
    cur.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(role))
    # Tables a later migration creates (as this owner) are readable too.
    cur.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {}").format(role))
    cur.execute(sql.SQL("REVOKE ALL ON {} FROM PUBLIC, {}").format(sql.Identifier(SECRETS_TABLE), role))


# Logging off for the transaction that writes the plaintext. Statement and
# duration logging are superuser settings (or granted with GRANT SET); a
# CREATEROLE owner without them still writes the secret as a bound
# parameter, so only a hub configured to log every statement's parameters
# would keep it.
_QUIET_LOGGING = (("log_statement", "none"), ("log_min_duration_statement", "-1"),
                  ("log_parameter_max_length", "0"), ("log_parameter_max_length_on_error", "0"))


def _quiet_logging(cur) -> list[str]:
    quiet = []
    for name, value in _QUIET_LOGGING:
        cur.execute("SAVEPOINT khipu_quiet")
        try:
            cur.execute("SELECT set_config(%s, %s, true)", (name, value))
            cur.execute("RELEASE SAVEPOINT khipu_quiet")
            quiet.append(name)
        except psycopg.Error:
            cur.execute("ROLLBACK TO SAVEPOINT khipu_quiet")
    return quiet


def _set_password(cur, *, exists: bool, limit: int | None) -> None:
    """A new password: the verifier on the role, the plaintext (a bound
    parameter, logging off) in the secrets table for other clients."""
    from psycopg import sql

    password = secrets.token_urlsafe(32)
    if exists:
        cur.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(
            sql.Identifier(READ_ROLE), sql.Literal(scram_verifier(password))))
    else:
        cur.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} CONNECTION LIMIT {}").format(
            sql.Identifier(READ_ROLE), sql.Literal(scram_verifier(password)),
            sql.Literal(limit if limit and limit > 0 else -1)))
    _quiet_logging(cur)
    cur.execute(sql.SQL(
        "INSERT INTO {} (role, secret) VALUES (%s, %s) ON CONFLICT (role) DO UPDATE"
        " SET secret = EXCLUDED.secret, rotated_at = now()").format(sql.Identifier(SECRETS_TABLE)),
        (READ_ROLE, password))


def ensure_read_role(conn, *, values: dict[str, Any] | None = None,
                     refused_password: str | None = None) -> dict[str, Any]:
    """Create or bring up to date ``khipu_read`` on the owner connection
    ``conn`` and return ``{"ok", "created", "password_reset", "reason"}``
    (plus ``"cannot_host"`` or ``"unsafe"`` naming why it refused).

    Idempotent and safe to run from several clients at once (an advisory
    lock serialises them). A new role takes every setting (``values`` over
    this Mac's stored ones); an existing one changes only the settings in
    ``values``. ``refused_password`` is the password the hub just refused
    this client: it is replaced only when it is still the one kept, so two
    clients repairing at once rotate once. Never returns the password."""
    from psycopg import sql

    out: dict[str, Any] = {"ok": False, "created": False, "password_reset": False}
    try:
        with conn.cursor() as cur:
            if not _can_create_roles(cur):
                conn.rollback()
                out["cannot_host"] = True
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
            current = row[0] if row else None
            attributes = _role_attributes(cur)
            if attributes is not None:
                unsafe = _unsafe_reason(cur, attributes)
                if unsafe:
                    conn.rollback()
                    out["unsafe"] = True
                    out["reason"] = f"{unsafe}; it was left as it is (drop it or remove those rights)"
                    return out
            if attributes is None:
                initial = {**settings(), **(values or {})}
                _set_password(cur, exists=False, limit=int(initial["read_role_connection_limit"]))
                _apply_settings(cur, {k: v for k, v in initial.items() if k != "read_role_connection_limit"})
                out["created"] = True
            else:
                if not attributes["rolcanlogin"]:
                    cur.execute(sql.SQL("ALTER ROLE {} LOGIN").format(sql.Identifier(READ_ROLE)))
                if current is None or (refused_password is not None and refused_password == current):
                    _set_password(cur, exists=True, limit=None)
                    out["password_reset"] = True
                _apply_settings(cur, values or {})
            _apply_grants(cur)
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


def _password_of(conninfo: str) -> str | None:
    from psycopg.conninfo import conninfo_to_dict

    try:
        return conninfo_to_dict(conninfo).get("password") or None
    except Exception:  # noqa: BLE001
        return None


def _read_dsn_file() -> Path:
    from khipu.paths import dsn_file

    return dsn_file().with_name("read_dsn")


def _memo_file() -> Path:
    from khipu.paths import data_dir

    return data_dir() / "read_role_memo.json"


# The validated read DSN, so a search does not read the file or ask the
# Keychain (a subprocess) on every connection. Dropped when the hub refuses it.
_CACHED: dict[str, str] = {}


def invalidate_cache() -> None:
    _CACHED.clear()


def cache_validated(owner_dsn: str, conninfo: str) -> None:
    """Keep ``conninfo`` for this process once the hub accepted it."""
    try:
        _CACHED["|".join(_target(owner_dsn))] = conninfo
    except Exception:  # noqa: BLE001
        pass


@secret_result("database_url")
def stored_read_dsn() -> str | None:
    """Environment, then the private 0600 file, then the Keychain: the file
    answers without a subprocess, and both hold the same value."""
    import os

    env = (os.environ.get(READ_DSN_ENV) or "").strip()
    if env:
        return env
    try:
        path = _read_dsn_file()
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
    except OSError:
        pass
    try:
        from khipu.keychain import get_password

        value = get_password(READ_DSN_ACCOUNT)
        if value:
            return value
    except Exception:  # noqa: BLE001
        pass
    return None


def store_read_dsn(conninfo: str) -> None:
    from khipu.join import write_private_file
    from khipu.keychain import keychain_available, set_password

    invalidate_cache()
    write_private_file(_read_dsn_file(), (conninfo.strip() + "\n").encode("utf-8"))
    if keychain_available():
        set_password(READ_DSN_ACCOUNT, conninfo.strip())


def forget_read_dsn() -> None:
    invalidate_cache()
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
    try:
        cached = _CACHED.get("|".join(_target(owner_dsn)))
    except Exception:  # noqa: BLE001
        return None
    if cached:
        return cached
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
    """Why provisioning failed within the retry window, or None."""
    memo = _memo()
    failed_at = memo.get("failed_at")
    if isinstance(failed_at, (int, float)) and time.time() - failed_at < PROVISION_RETRY_AFTER_S:
        return str(memo.get("reason") or "provisioning failed recently")
    return None


def cannot_host() -> str | None:
    """Why this hub cannot host a read role, while that finding is fresh, or
    None. Only this case searches as the owner."""
    memo = _memo()
    checked_at = memo.get("cannot_host_at")
    if isinstance(checked_at, (int, float)) and time.time() - checked_at < CANNOT_HOST_RECHECK_S:
        return str(memo.get("reason") or "the owner role cannot create roles")
    return None


def _unavailable(reason: str) -> ReadRoleUnavailable:
    return ReadRoleUnavailable(f"the hub's search role ({READ_ROLE}) cannot be used now: {reason}")


@secret_result("database_url")
def provision(owner_conn=None, owner_dsn: str | None = None, *, refused: str | None = None,
              force: bool = False) -> str | None:
    """Make sure this client holds a working read DSN and return it.

    Returns None only when the hub cannot host a read role (the owner has
    neither CREATEROLE nor superuser and no password is kept): search then
    shares the owner's connections. Raises ``ReadRoleUnavailable`` when the
    role exists or could be made but this attempt failed, and lets a hub
    that cannot be reached raise as itself. ``refused`` is the read DSN the
    hub just refused: the kept password is replaced only when it is that one."""
    from khipu import hub_errors
    from khipu.paths import isolated_dir

    if isolated_dir() is not None:
        raise _unavailable("an isolated run never sets up the role")
    if not force:
        if cannot_host() and refused is None:
            return None
        blocked = provision_blocked()
        if blocked:
            raise _unavailable(blocked)
    from khipu import db

    owner_dsn = owner_dsn or db.resolve_dsn()
    own = owner_conn
    opened = None
    try:
        if own is None:
            opened = db.connect(role="owner")
            own = opened
        password = hub_password(own)
        refused_password = _password_of(refused) if refused else None
        if not password or (refused_password is not None and password == refused_password):
            result = ensure_read_role(own, refused_password=refused_password)
            if not result.get("ok"):
                if result.get("cannot_host") and refused is None and not password:
                    _write_memo({"cannot_host_at": time.time(), "reason": result.get("reason")})
                    return None
                _write_memo({"failed_at": time.time(), "reason": result.get("reason"),
                             **({"unsafe": True} if result.get("unsafe") else {})})
                raise _unavailable(str(result.get("reason")))
            password = hub_password(own)
        if not password:
            _write_memo({"failed_at": time.time(), "reason": "no read role password on the hub"})
            raise _unavailable("no read role password on the hub")
        conninfo = read_conninfo_for(owner_dsn, password)
        store_read_dsn(conninfo)
        _write_memo({"provisioned_at": time.time()})
        return conninfo
    except ReadRoleUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        if hub_errors.unreachable_error(exc):
            raise
        reason = f"{type(exc).__name__}: {exc}"[:300]
        _write_memo({"failed_at": time.time(), "reason": reason})
        raise _unavailable(reason) from exc
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
    searching as the owner. Warns; never gates doctor's ``ok``.

    ``state`` is ``read`` (searches as khipu_read), ``cannot_host`` (the
    hub cannot have a read role, so searches share the save connections:
    ``level`` amber, with the fix), ``unavailable`` (the role exists or
    could, but this Mac cannot use it: searches answer from this Mac's copy,
    never as the owner) or ``not_configured``."""
    from khipu import db

    out: dict[str, Any] = {"settings": settings(), "warnings": []}
    try:
        owner_dsn = db.resolve_dsn()
    except RuntimeError:
        out["state"] = "not_configured"
        return out
    has_read = read_conninfo(owner_dsn) is not None
    opened = None
    try:
        if conn is None:
            opened = db.connect(role="owner")
            conn = opened
        with conn.cursor() as cur:
            can_create = _can_create_roles(cur)
            cur.execute("SELECT rolconnlimit FROM pg_roles WHERE rolname = %s", (READ_ROLE,))
            row = cur.fetchone()
            out["role_exists"] = row is not None
            if row is not None:
                out["role_connection_limit"] = row[0]
        conn.rollback()
        if has_read:
            out["state"] = "read"
        elif not can_create and not hub_password(conn):
            out["state"] = "cannot_host"
            out["level"] = "amber"
            out["message"] = (
                "Searches share the save connections on this hub: its owner role cannot create roles, so it "
                "has no separate search role. To fix, give the owner role CREATEROLE on the hub (as a "
                "superuser: ALTER ROLE <owner> CREATEROLE); the next search then sets up the search role.")
            out["warnings"].append(out["message"])
        else:
            out["state"] = "unavailable"
            reason = provision_blocked() or "this Mac has no search role password yet; the next search sets it up"
            out["warnings"].append(
                f"This Mac cannot search as {READ_ROLE} ({reason}). Searches answer from this Mac's copy "
                "until it can; they never borrow the connections saves need.")
        with conn.cursor() as cur:
            # On a hub that cannot host the role, owner-read clients are expected.
            labelled = "" if out["state"] == "cannot_host" else " OR application_name LIKE 'khipu/owner-read/%%'"
            cur.execute(
                "SELECT COALESCE(NULLIF(application_name, ''), '(no label)'),"
                " COALESCE(host(client_addr), 'local'), count(*)"
                " FROM pg_stat_activity WHERE usename = current_user AND pid <> pg_backend_pid()"
                " AND backend_type = 'client backend'"
                f" AND (application_name NOT LIKE 'khipu/%%'{labelled})"
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
        out.setdefault("state", "read" if has_read else "unavailable")
        out["error"] = f"{type(exc).__name__}: {exc}"[:200]
    finally:
        if opened is not None:
            try:
                opened.close()
            except Exception:  # noqa: BLE001
                pass
    return out
