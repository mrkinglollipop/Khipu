"""The hub's read role for search (docs/plans/2026-10-19-swarm-ready.md, A).

Search, owed, decisions, the graph and the gateway's read tools connect as
``khipu_read``: no superuser, ``SELECT`` only, a connection limit and two
timeouts. Captures, the outbox drain, migrations and the nightly stay on the
owner role, so a crowd of searches can fill ``khipu_read`` without taking a
connection a save needs.

Provisioning needs no owner step. The read password is derived on each
client from the owner DSN's password (``derived_password``), so every client
holding the owner DSN knows it and nothing secret is stored on the hub or on
disk. The first search the hub refuses runs ``ensure_read_role`` as the
owner: it creates the role, or sets the derived password, from a SCRAM
verifier computed here. Only a hub confirmed unable to host the role (the
owner has neither CREATEROLE nor superuser and the role does not exist)
searches as the owner,
labelled ``khipu/owner-read/...`` in ``application_name``; doctor says so.
Anywhere else a read role that cannot be used right now (refused password
that could not be repaired, a failed provisioning, an existing role with
more rights than search needs, a full role) raises ``ReadRoleUnavailable``
and is never answered by an owner connection.

The three role settings are the owner's in the app; saving one runs
``ALTER ROLE`` for that setting only, through the owner connection (0 means
no limit), and the app shows the values the hub holds.

No plaintext password is ever sent to the hub: statement text carries only
the SCRAM-SHA-256 verifier, so a hub logging every statement logs nothing a
client could log in with.
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
LEGACY_SECRETS_TABLE = "khipu_role_secrets"
READ_DSN_ENV = "KHIPU_READ_DATABASE_URL"
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
    # An earlier build kept the password in plaintext here; nothing reads it now.
    cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(LEGACY_SECRETS_TABLE)))


def _password_clause(owner_dsn: str):
    """``PASSWORD <verifier>``, or ``PASSWORD NULL`` when the owner DSN has
    no password (trust or peer auth): the read role then logs in the same way."""
    from psycopg import sql

    password = derived_password(owner_dsn)
    if password is None:
        return sql.SQL("PASSWORD NULL")
    return sql.SQL("PASSWORD {}").format(sql.Literal(scram_verifier(password)))


def ensure_read_role(conn, *, owner_dsn: str | None = None, values: dict[str, Any] | None = None,
                     repair: bool = False) -> dict[str, Any]:
    """Create or bring up to date ``khipu_read`` on the owner connection
    ``conn`` and return ``{"ok", "created", "password_reset", "reason"}``
    (plus ``"cannot_host"`` or ``"unsafe"`` naming why it refused).

    Idempotent and safe to run from several clients at once (an advisory
    lock serialises them; every client derives the same password, so two
    repairs set the same one). A new role takes every setting (``values``
    over this Mac's stored ones); an existing one changes only the settings
    in ``values``, and its password only with ``repair`` (the hub refused
    the derived one). A lookup that fails raises; only a confirmed answer
    decides ``cannot_host``."""
    from psycopg import sql

    out: dict[str, Any] = {"ok": False, "created": False, "password_reset": False}
    try:
        with conn.cursor() as cur:
            can_create = _can_create_roles(cur)
            attributes = _role_attributes(cur)
            if not can_create:
                conn.rollback()
                if attributes is None:
                    out["cannot_host"] = True
                    out["reason"] = "the owner role cannot create roles (needs CREATEROLE or superuser)"
                else:
                    out["reason"] = (f"{READ_ROLE} exists but the owner role cannot change it "
                                     "(needs CREATEROLE or superuser)")
                return out
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_ADVISORY_KEY,))
            attributes = _role_attributes(cur)
            if attributes is not None:
                unsafe = _unsafe_reason(cur, attributes)
                if unsafe:
                    conn.rollback()
                    out["unsafe"] = True
                    out["reason"] = f"{unsafe}; it was left as it is (drop it or remove those rights)"
                    return out
            if owner_dsn is None and (attributes is None or repair):
                from khipu.db import resolve_dsn

                owner_dsn = resolve_dsn()
            role = sql.Identifier(READ_ROLE)
            if attributes is None:
                initial = {**settings(), **(values or {})}
                limit = int(initial["read_role_connection_limit"])
                cur.execute(sql.SQL("CREATE ROLE {} LOGIN {} CONNECTION LIMIT {}").format(
                    role, _password_clause(owner_dsn), sql.Literal(limit if limit > 0 else -1)))
                _apply_settings(cur, {k: v for k, v in initial.items() if k != "read_role_connection_limit"})
                out["created"] = True
            else:
                if not attributes["rolcanlogin"]:
                    cur.execute(sql.SQL("ALTER ROLE {} LOGIN").format(role))
                if repair:
                    cur.execute(sql.SQL("ALTER ROLE {} {}").format(role, _password_clause(owner_dsn)))
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
        out["error"] = exc
        return out
    out["ok"] = True
    return out


# ---- this client's read DSN -----------------------------------------------------
#
# The read password is derived from the owner DSN's password, so every client
# that holds the owner DSN knows it and none is ever written to the hub or to
# disk. It follows the owner password: when that changes, the next refused
# search sets the new one.

def _owner_params(owner_dsn: str) -> dict[str, str]:
    from psycopg.conninfo import conninfo_to_dict

    params = {key: str(val) for key, val in conninfo_to_dict(owner_dsn).items() if val is not None}
    if not params.get("dbname") and params.get("user"):
        params["dbname"] = params["user"]
    return params


def hub_key(owner_dsn: str) -> str:
    """Which hub and owner a memo or cache entry is about (no secret in it)."""
    params = _owner_params(owner_dsn)
    ident = "|".join(params.get(k, "") for k in ("host", "hostaddr", "port", "dbname", "user"))
    return hashlib.sha256(ident.encode("utf-8")).hexdigest()[:16]


@secret_result("read_role_password")
def derived_password(owner_dsn: str) -> str | None:
    """HMAC-SHA256 of the owner's password over this role and the owner's
    name; None when the owner DSN carries no password. Not over the host
    (each Mac may reach the hub by another name) nor the database (a role's
    password is the whole server's, and two databases must not take turns
    replacing it)."""
    params = _owner_params(owner_dsn)
    secret = params.get("password")
    if not secret:
        return None
    message = f"{READ_ROLE}/{params.get('user', '')}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), message, "sha256").digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


@secret_result("database_url")
def read_conninfo_for(owner_dsn: str) -> str:
    """The owner DSN with the read role's user and derived password."""
    from psycopg.conninfo import make_conninfo

    params = _owner_params(owner_dsn)
    params["user"] = READ_ROLE
    password = derived_password(owner_dsn)
    if password is None:
        params.pop("password", None)
    else:
        params["password"] = password
    return make_conninfo(**params)


@secret_result("database_url")
def read_conninfo(owner_dsn: str) -> str:
    """``KHIPU_READ_DATABASE_URL`` when set, else the derived read DSN."""
    import os

    env = (os.environ.get(READ_DSN_ENV) or "").strip()
    return env or read_conninfo_for(owner_dsn)


def _memo_file() -> Path:
    from khipu.paths import data_dir

    return data_dir() / "read_role_memo.json"


# Hubs whose read role passed ``check_connection`` in this process.
_VALIDATED: set[str] = set()


def forget_validated(owner_dsn: str) -> None:
    """The hub refused this process's read connection: check again next time."""
    _VALIDATED.discard(hub_key(owner_dsn))


def invalidate_cache(owner_dsn: str | None = None) -> None:
    """Forget what this process and the memo know about one hub (all with
    no ``owner_dsn``): the next search checks again."""
    if owner_dsn is None:
        _VALIDATED.clear()
        _write_memo({})
        return
    key = hub_key(owner_dsn)
    _VALIDATED.discard(key)
    memo = _memo()
    if memo.pop(key, None) is not None:
        _write_memo(memo)


def _memo() -> dict[str, Any]:
    try:
        data = json.loads(_memo_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
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


def _note(owner_dsn: str, entry: dict[str, Any] | None) -> None:
    memo = _memo()
    key = hub_key(owner_dsn)
    if entry is None:
        memo.pop(key, None)
    else:
        memo[key] = entry
    _write_memo(memo)


def note_failure(owner_dsn: str, reason: str) -> None:
    _note(owner_dsn, {"failed_at": time.time(), "reason": reason})


def _entry(owner_dsn: str) -> dict[str, Any]:
    entry = _memo().get(hub_key(owner_dsn))
    return entry if isinstance(entry, dict) else {}


def provision_blocked(owner_dsn: str) -> str | None:
    """Why provisioning this hub failed within the retry window, or None."""
    entry = _entry(owner_dsn)
    failed_at = entry.get("failed_at")
    if isinstance(failed_at, (int, float)) and time.time() - failed_at < PROVISION_RETRY_AFTER_S:
        return str(entry.get("reason") or "provisioning failed recently")
    return None


def cannot_host(owner_dsn: str) -> str | None:
    """Why this hub, for this owner, cannot host a read role, while that
    finding is fresh, or None. Only this case searches as the owner."""
    entry = _entry(owner_dsn)
    checked_at = entry.get("cannot_host_at")
    if isinstance(checked_at, (int, float)) and time.time() - checked_at < CANNOT_HOST_RECHECK_S:
        return str(entry.get("reason") or "the owner role cannot create roles")
    return None


def _unavailable(reason: str) -> ReadRoleUnavailable:
    return ReadRoleUnavailable(f"the hub's search role ({READ_ROLE}) cannot be used now: {reason}")


def confirmed_cannot_host(owner_conn) -> bool:
    """Read-only: the owner has neither CREATEROLE nor superuser and the
    role does not exist. A failed lookup raises, never answers yes."""
    with owner_conn.cursor() as cur:
        can_create = _can_create_roles(cur)
        attributes = _role_attributes(cur)
    owner_conn.rollback()
    return not can_create and attributes is None


def recheck_capability(owner_conn, owner_dsn: str) -> None:
    """On an owner-read connection already open: drop the cannot-host memo
    the moment the owner gains the right, so the next search sets the role
    up instead of waiting out the memo. Never raises."""
    try:
        if not confirmed_cannot_host(owner_conn):
            _note(owner_dsn, None)
    except Exception:  # noqa: BLE001
        try:
            owner_conn.rollback()
        except Exception:  # noqa: BLE001
            pass


def provision(owner_conn=None, owner_dsn: str | None = None, *, repair: bool = True,
              force: bool = False) -> bool:
    """Make sure the hub's read role exists, is safe and takes this
    client's derived password. Returns True when it does, False only when
    the hub confirmed it cannot host one (the owner has neither CREATEROLE
    nor superuser and the role does not exist): search then shares the
    owner's connections. Raises ``ReadRoleUnavailable`` when the role exists
    or could be made but this attempt failed, and lets a hub that cannot be
    reached raise as itself. Isolated runs (the promotion gate) never write."""
    from khipu import hub_errors
    from khipu.paths import isolated_dir

    if isolated_dir() is not None:
        raise _unavailable("an isolated run never sets up the role")
    from khipu import db

    owner_dsn = owner_dsn or db.resolve_dsn()
    if not force:
        blocked = provision_blocked(owner_dsn)
        if blocked:
            raise _unavailable(blocked)
    own = owner_conn
    opened = None
    try:
        if own is None:
            opened = db.connect(role="owner")
            own = opened
        result = ensure_read_role(own, owner_dsn=owner_dsn, repair=repair)
        if not result.get("ok"):
            error = result.get("error")
            if error is not None and hub_errors.unreachable_error(error):
                raise error
            if result.get("cannot_host"):
                _note(owner_dsn, {"cannot_host_at": time.time(), "reason": result.get("reason")})
                return False
            _note(owner_dsn, {"failed_at": time.time(), "reason": result.get("reason"),
                              **({"unsafe": True} if result.get("unsafe") else {})})
            raise _unavailable(str(result.get("reason")))
        _note(owner_dsn, {"provisioned_at": time.time()})
        return True
    except ReadRoleUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        if hub_errors.unreachable_error(exc):
            raise
        reason = f"{type(exc).__name__}: {exc}"[:300]
        _note(owner_dsn, {"failed_at": time.time(), "reason": reason})
        raise _unavailable(reason) from exc
    finally:
        if opened is not None:
            try:
                opened.close()
            except Exception:  # noqa: BLE001
                pass


def check_connection(conn, owner_dsn: str) -> bool:
    """Before this process first searches on a read connection: the role it
    logged in as has no rights beyond SELECT-only search, checked on the
    read connection itself, so a role made or widened by someone else is
    caught even where no owner step ran (raises ``ReadRoleUnavailable``).
    Returns False when the role can log in but was never granted this
    database (a role is the whole server's; grants are per database)."""
    key = hub_key(owner_dsn)
    if key in _VALIDATED:
        return True
    with conn.cursor() as cur:
        attributes = _role_attributes(cur)
        reason = (f"{READ_ROLE} is missing" if attributes is None else _unsafe_reason(cur, attributes))
        cur.execute("SELECT to_regclass('public.episodes') IS NULL"
                    " OR has_table_privilege(current_user, 'public.episodes', 'SELECT')")
        granted = bool(cur.fetchone()[0])
    conn.rollback()
    if reason:
        _note(owner_dsn, {"failed_at": time.time(), "reason": reason, "unsafe": True})
        raise _unavailable(reason)
    if not granted:
        return False
    _VALIDATED.add(key)
    return True


def role_limit_error(exc: BaseException) -> bool:
    """The read role is full: overflow, never a fallback to the owner."""
    text = str(exc).lower()
    return "too many connections for role" in text


def auth_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return ("password authentication failed" in text or f'role "{READ_ROLE}" does not exist' in text
            or "permission denied for database" in text or "is not permitted to log in" in text)


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

def _read_login_problem(owner_dsn: str) -> str | None:
    """Why this Mac cannot log in as the read role right now, or None."""
    from khipu import db

    blocked = provision_blocked(owner_dsn)
    if blocked:
        return blocked
    try:
        db._open(read_conninfo(owner_dsn), "read", autocommit=True, statement_timeout_s=None,
                 connect_timeout=db.CONNECT_TIMEOUT_SECONDS).close()
    except psycopg.OperationalError as exc:
        if auth_error(exc):
            return "the hub refused this Mac's search password; the next search sets it"
        if role_limit_error(exc):
            return None
        raise
    return None


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
    opened = None
    try:
        if conn is None:
            opened = db.connect(role="owner")
            conn = opened
        with conn.cursor() as cur:
            can_create = _can_create_roles(cur)
            attributes = _role_attributes(cur)
            unsafe = _unsafe_reason(cur, attributes) if attributes is not None else None
        conn.rollback()
        out["role_exists"] = attributes is not None
        if attributes is not None:
            out["role_connection_limit"] = attributes["rolconnlimit"]
        if not can_create and attributes is None:
            out["state"] = "cannot_host"
            out["level"] = "amber"
            out["message"] = (
                "Searches share the save connections on this hub: its owner role cannot create roles, so it "
                "has no separate search role. To fix, give the owner role CREATEROLE on the hub (as a "
                "superuser: ALTER ROLE <owner> CREATEROLE); the next search then sets up the search role.")
            out["warnings"].append(out["message"])
        else:
            if cannot_host(owner_dsn):
                _note(owner_dsn, None)  # the owner gained the right: the next search sets the role up
            reason = unsafe or _read_login_problem(owner_dsn)
            if reason is None:
                out["state"] = "read"
            else:
                out["state"] = "unavailable"
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
        out.setdefault("state", "unknown")
        out["error"] = f"{type(exc).__name__}: {exc}"[:200]
    finally:
        if opened is not None:
            try:
                opened.close()
            except Exception:  # noqa: BLE001
                pass
    return out
