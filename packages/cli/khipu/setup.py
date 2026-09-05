# --bypass-harness (sonnet lane): authored by a dispatched on-sub
# Agent(model="sonnet") subagent for Phase 1 of docs/plans/2026-09-05-
# setup-that-cannot-strand-you.md — the exact lane the code-routing policy
# names as the destination for non-trivial code; no further hop applies.
"""One pipeline, shared by every entry point that connects Khipu to a database.

``khipu db connect --dsn ... --json`` (CLI) and the desktop's Database step
both call :func:`connect_database`. It runs the stages from
``docs/plans/2026-09-05-setup-that-cannot-strand-you.md`` in order and stops
at the first failed stage — every later stage is reported ``"skipped"``, never
silently omitted, so the caller always has one screenful of "what happened."

Every failure carries a plain-words ``title``, ``detail``, and a one-action
``fix`` — never a raw error code and never a term without its gloss. The
password never appears in any returned dict; see :func:`mask_dsn`.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

# The nightly LaunchAgent's fixed run time (launchd/com.matt.khipu-nightly.plist
# StartCalendarInterval Hour=2 Minute=5) — display text only, so a person is
# never told "it's scheduled" without being told when.
NIGHTLY_SCHEDULE_TEXT = "02:05"

STAGE_ORDER: tuple[str, ...] = (
    "reach",
    "version",
    "privileges",
    "schema",
    "graph",
    "store",
    "upkeep",
    "prove",
    "summary",
)


def mask_dsn(dsn: str) -> str:
    """``postgres://user@host:port/dbname`` — the password is never read,
    let alone returned, so there is nothing to accidentally leak here."""
    try:
        from psycopg.conninfo import conninfo_to_dict

        params = conninfo_to_dict(dsn)
    except Exception:
        return "***"
    host = params.get("host") or "?"
    port = params.get("port")
    dbname = params.get("dbname") or params.get("db") or "?"
    user = params.get("user") or params.get("username")
    netloc = f"{user}@{host}" if user else str(host)
    if port:
        netloc = f"{netloc}:{port}"
    return f"postgres://{netloc}/{dbname}"


def host_kind(dsn: str) -> str:
    """``local-docker`` | ``this-mac`` | ``remote`` — used by ``khipu db
    status`` and the Second-Mac flow (offer Move before a join kit when the
    stored connection is local to this machine)."""
    try:
        from psycopg.conninfo import conninfo_to_dict

        host = str(conninfo_to_dict(dsn).get("host") or "").strip().lower()
    except Exception:
        return "remote"
    if not host:
        return "remote"
    if host in {"localhost", "127.0.0.1", "::1"}:
        return "this-mac"
    if host.startswith("172.") or host in {"host.docker.internal", "docker.local"}:
        return "local-docker"
    return "remote"


def explain_connection_error(exc: BaseException) -> tuple[str, str, str]:
    """(title, detail, fix) in plain words for any exception raised while
    reaching, authenticating against, or querying a Postgres server.

    Matches on the libpq/psycopg message text (the same style already used by
    ``khipu.join.classify_hub_connect_error``), since the exception class is
    almost always ``psycopg.OperationalError`` regardless of which of these
    it actually is.
    """
    text = str(exc)
    low = text.lower()

    def result(title: str, fix: str) -> tuple[str, str, str]:
        return title, text, fix

    if (
        "could not translate host name" in low
        or "name or service not known" in low
        or "nodename nor servname" in low
        or "temporary failure in name resolution" in low
    ):
        return result(
            "Khipu could not find that server",
            "Check the host name and port, and that this Mac can reach it "
            "(DNS, VPN, or Tailscale).",
        )
    if "connection refused" in low:
        return result(
            "Khipu could not reach that server",
            "Check the host and port, and that the server allows connections "
            "from this Mac.",
        )
    if "timeout expired" in low or "timed out" in low:
        return result(
            "Khipu could not reach that server",
            "Check the host and port, and that the server allows connections "
            "from this Mac.",
        )
    if "password authentication failed" in low or (
        "authentication failed" in low and "certificate" not in low
    ):
        return result(
            "The username or password is wrong",
            "Double check the username and password in the connection string, "
            "then try again.",
        )
    if "database" in low and "does not exist" in low:
        return result(
            "That database does not exist yet",
            "Create it on the server (`CREATE DATABASE name;`) or ask your "
            "host to create it, then try again.",
        )
    if (
        "certificate verify failed" in low
        or "certificate has expired" in low
        or "self signed certificate" in low
        or "unable to get local issuer certificate" in low
        or "unknown ca" in low
        or "sslrootcert" in low
        or "root certificate" in low
    ):
        return result(
            "The server's certificate is not trusted",
            "Paste the certificate file your host gave you (often called "
            "root.crt or ca.pem).",
        )
    if "server does not support ssl" in low or (
        "sslmode" in low and ("invalid" in low or "unsupported" in low or "not supported" in low)
    ):
        return result(
            "This server does not support the requested encrypted connection",
            "Ask your host what sslmode it expects (usually require or "
            "verify-full) and try again with that.",
        )
    if "insufficientprivilege" in low or "permission denied" in low or "must be owner" in low:
        return result(
            "This account does not have enough privileges",
            "Ask your host to grant CREATE on the database to this role, or "
            "use an account that can.",
        )
    return result(
        "Khipu could not connect",
        "Check the connection string and try again; if this keeps happening, "
        "share this message when asking for help.",
    )


def _connect(dsn: str, root_crt: str | None, *, timeout: float = 10.0):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = {k: v for k, v in conninfo_to_dict(dsn).items() if v is not None}
    if root_crt:
        params["sslrootcert"] = str(Path(root_crt).expanduser().resolve())
    else:
        from khipu.paths import root_cert_file

        cert = root_cert_file()
        if cert.is_file():
            params["sslrootcert"] = str(cert.resolve())
    conninfo = make_conninfo(**params)
    return psycopg.connect(conninfo, autocommit=False, connect_timeout=timeout)


def _stage_version(conn) -> dict[str, Any]:
    from khipu.components_postgres import (
        require_pg19_num,
        server_version_num,
        server_version_string,
    )

    num = server_version_num(conn)
    ver_str = server_version_string(conn)
    gate = require_pg19_num(num)
    if not gate.get("ok"):
        return {
            "id": "version",
            "ok": False,
            "title": "This server's Postgres is too old",
            "detail": f"Your server runs {ver_str}; Khipu needs Postgres 19 or newer.",
            "fix": "Upgrade the server to Postgres 19+, or point Khipu at a server "
            "that already runs it.",
        }
    return {"id": "version", "ok": True, "title": "Version", "detail": f"Postgres {ver_str}."}


def _stage_privileges(conn) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        dbname = cur.fetchone()[0]
        cur.execute("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")
        can_create = bool(cur.fetchone()[0])
        if not can_create:
            conn.rollback()
            return {
                "id": "privileges",
                "ok": False,
                "title": "This account cannot create tables",
                "detail": f"The connected role has no CREATE privilege on schema "
                f"public of database {dbname}.",
                "fix": "Ask your host to grant CREATE on schema public to this "
                "role, or use an account that can.",
            }
        cur.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
        available = cur.fetchone() is not None
        if not available:
            conn.rollback()
            return {
                "id": "privileges",
                "ok": False,
                "title": "The vector extension is not available on this server",
                "detail": f"pg_available_extensions has no 'vector' entry on "
                f"database {dbname}.",
                "fix": "Ask your host to install the pgvector extension package "
                "on the server.",
            }
        cur.execute("SAVEPOINT khipu_probe_ext")
        try:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except Exception as exc:  # noqa: BLE001 — reported as the stage failure
            cur.execute("ROLLBACK TO SAVEPOINT khipu_probe_ext")
            conn.rollback()
            return {
                "id": "privileges",
                "ok": False,
                "title": "This account cannot enable the vector extension",
                "detail": f"{type(exc).__name__}: {exc}",
                "fix": f"Ask your host to run CREATE EXTENSION vector; on "
                f"database {dbname} (managed providers: enable the vector "
                "extension in their console).",
            }
    conn.commit()
    return {
        "id": "privileges",
        "ok": True,
        "title": "Privileges",
        "detail": "This role can create tables and enable pgvector.",
    }


def _stage_schema(conn) -> dict[str, Any]:
    from khipu.migrate import available, migrations_dir, run as migrate_run

    # An install with no migration files would otherwise report "schema is
    # current" against an EMPTY database and let the graph probe fail one
    # stage later with a raw "relation does not exist" (caught by the Docker
    # first-run oracle, 2026-09-05).
    if not available():
        return {
            "id": "schema",
            "ok": False,
            "title": "Khipu's schema files are missing from this install",
            "detail": f"No migration files were found under {migrations_dir()}.",
            "fix": "Reinstall Khipu (or, from a source checkout, make sure "
            "ops/migrations is present), then try again.",
        }
    out = migrate_run(dry_run=False, conn=conn)
    if out["pending"]:
        return {
            "id": "schema",
            "ok": False,
            "title": "A migration failed to apply",
            "detail": f"Still pending: {', '.join(out['pending'])}.",
            "fix": "Check the server logs for the failing migration, then ask "
            "for help with the exact error.",
        }
    applied_total = len(out.get("applied") or []) if isinstance(out.get("applied"), (list, set, tuple)) else None
    return {
        "id": "schema",
        "ok": True,
        "title": "Schema is current",
        "detail": (
            f"Applied: {', '.join(out['ran'])}." if out["ran"]
            else "Already at the newest version; nothing to apply."
        ),
        "ran": out["ran"],
        **({"applied_total": applied_total} if applied_total is not None else {}),
    }


def _stage_graph(conn) -> dict[str, Any]:
    from khipu.components_postgres import probe_graph_table

    out = probe_graph_table(conn)
    if not out.get("ok"):
        conn.rollback()
        return {
            "id": "graph",
            "ok": False,
            "title": "The knowledge-graph query failed",
            "detail": out.get("error", "unknown error"),
            "fix": "Re-run setup; if it keeps failing, ask for help with the "
            "exact error.",
        }
    return {"id": "graph", "ok": True, "title": "Graph", "detail": "GRAPH_TABLE query works."}


def _stage_store(dsn: str, root_crt: str | None) -> dict[str, Any]:
    from khipu.keychain import set_dsn

    try:
        set_dsn(dsn)
        if root_crt:
            from khipu.paths import root_cert_file

            dest = root_cert_file()
            dest.write_bytes(Path(root_crt).expanduser().read_bytes())
            dest.chmod(0o600)
    except Exception as exc:  # noqa: BLE001
        return {
            "id": "store",
            "ok": False,
            "title": "Could not save the connection",
            "detail": f"{type(exc).__name__}: {exc}",
            "fix": "Check Keychain access for Khipu and try again.",
        }
    return {"id": "store", "ok": True, "title": "Saved", "detail": f"Connection saved for {mask_dsn(dsn)}."}


def _stage_upkeep() -> dict[str, Any]:
    from khipu.launchd_gen import ensure_scheduled_jobs

    try:
        out = ensure_scheduled_jobs()
    except FileNotFoundError:
        return {
            "id": "upkeep",
            "ok": True,
            "status": "skipped",
            "title": "Upkeep skipped",
            "detail": "launchd is not available on this platform (not macOS); "
            "nightly upkeep must be scheduled another way.",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "id": "upkeep",
            "ok": False,
            "title": "Could not schedule nightly upkeep",
            "detail": f"{type(exc).__name__}: {exc}",
            "fix": "Try again, or run `khipu jobs install` from a Terminal.",
        }
    if not out.get("ok"):
        return {
            "id": "upkeep",
            "ok": False,
            "title": "Could not schedule nightly upkeep",
            "detail": str(out.get("error") or out),
            "fix": "Try again, or run `khipu jobs install` from a Terminal.",
        }
    parts = []
    if out.get("installed"):
        parts.append(f"scheduled {', '.join(out['installed'])}")
    if out.get("refreshed"):
        parts.append(f"refreshed {', '.join(out['refreshed'])}")
    if out.get("current"):
        parts.append(f"already scheduled: {', '.join(out['current'])}")
    if out.get("external"):
        parts.append(f"managed outside the app, left alone: {', '.join(out['external'])}")
    return {
        "id": "upkeep",
        "ok": True,
        "title": "Upkeep scheduled",
        "detail": (("; ".join(parts) or "nothing to do") + ". Nightly at 02:05, graph at 02:17, monthly on the 1st.").capitalize(),
    }


def _stage_prove() -> dict[str, Any]:
    from khipu.config import capture_mode
    from khipu.keychain import get_gemini_key, get_openai_compat_key

    if capture_mode() == "legacy":
        return {
            "id": "prove",
            "ok": True,
            "status": "skipped",
            "title": "Skipped",
            "detail": "Capture mode is legacy (file wiki only) — this proves "
            "nothing about the memory hub. A harness's own Verify will prove "
            "memory once capture v2 is on.",
        }
    if not (get_gemini_key() or get_openai_compat_key()):
        return {
            "id": "prove",
            "ok": True,
            "status": "skipped",
            "title": "Skipped — no model key yet",
            "detail": "No embedding model key is configured, so Khipu cannot "
            "prove a real capture-to-search round trip here.",
            "fix": "Add a model key in Settings → Secrets; a harness's own "
            "Verify will prove memory works once one is set.",
        }
    from khipu.probe import run_probe

    out = run_probe("app")
    if not out.get("ok"):
        return {
            "id": "prove",
            "ok": False,
            "title": "The memory round trip failed",
            "detail": out.get("error") or "unknown error",
            "fix": "Try again; if it keeps failing, check that the model key "
            "is valid and the network is up.",
        }
    return {
        "id": "prove",
        "ok": True,
        "title": "Memory round trip",
        "detail": f"Memory round trip: {out.get('seconds')} s.",
        "episode_id": out.get("episode_id"),
    }


def _stage_summary(conn, dsn: str) -> tuple[dict[str, Any], dict[str, Any]]:
    from psycopg.conninfo import conninfo_to_dict

    episodes: int | None
    topics: int | None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM episodes WHERE deleted_at IS NULL")
            episodes = int(cur.fetchone()[0])
            cur.execute("SELECT COUNT(*) FROM topics")
            topics = int(cur.fetchone()[0])
    except Exception:  # noqa: BLE001 — counts are informational, never fatal here
        conn.rollback()
        episodes = topics = None
    params = conninfo_to_dict(dsn)
    host = params.get("host") or "?"
    dbname = params.get("dbname") or params.get("db") or "?"
    summary = {
        "host": host,
        "database": dbname,
        "episodes": episodes,
        "topics": topics,
        "nightly_schedule": NIGHTLY_SCHEDULE_TEXT,
    }
    if episodes is None:
        detail = f"{host}/{dbname} — connected, but could not read counts."
    else:
        detail = f"Working. {host} · {episodes} sessions remembered · nightly upkeep at {NIGHTLY_SCHEDULE_TEXT}."
    entry = {"id": "summary", "ok": True, "title": "Working", "detail": detail}
    return entry, summary


def connect_database(
    dsn: str,
    *,
    store: bool = True,
    install_jobs: bool = True,
    prove: bool = True,
    root_crt: str | None = None,
) -> dict[str, Any]:
    """Run every setup stage in order against ``dsn``, stopping at the first
    failure. See the module docstring and
    ``docs/plans/2026-09-05-setup-that-cannot-strand-you.md``."""
    stages: list[dict[str, Any]] = []
    out: dict[str, Any] = {"ok": True, "stages": stages, "summary": {}}
    conn = None

    def record(entry: dict[str, Any], seconds: float) -> bool:
        entry = dict(entry)
        entry["seconds"] = round(seconds, 3)
        stages.append(entry)
        return bool(entry.get("ok"))

    def skip_rest(remaining: tuple[str, ...], why: str) -> None:
        for stage_id in remaining:
            stages.append(
                {"id": stage_id, "ok": True, "status": "skipped", "title": "Skipped", "detail": why, "seconds": 0.0}
            )

    def fail(remaining: tuple[str, ...]) -> dict[str, Any]:
        skip_rest(remaining, "skipped: an earlier stage failed")
        out["ok"] = False
        if conn is not None:
            conn.close()
        return out

    # 1. reach
    t0 = time.monotonic()
    try:
        conn = _connect(dsn, root_crt)
        ok = record(
            {"id": "reach", "ok": True, "title": "Working", "detail": f"Connected to {mask_dsn(dsn)}."},
            time.monotonic() - t0,
        )
    except Exception as exc:  # noqa: BLE001
        title, detail, fix = explain_connection_error(exc)
        ok = record({"id": "reach", "ok": False, "title": title, "detail": detail, "fix": fix}, time.monotonic() - t0)
    if not ok:
        return fail(STAGE_ORDER[1:])

    # 2. version
    t0 = time.monotonic()
    try:
        entry = _stage_version(conn)
    except Exception as exc:  # noqa: BLE001
        title, detail, fix = explain_connection_error(exc)
        entry = {"id": "version", "ok": False, "title": title, "detail": detail, "fix": fix}
    if not record(entry, time.monotonic() - t0):
        return fail(STAGE_ORDER[2:])

    # 3. privileges
    t0 = time.monotonic()
    try:
        entry = _stage_privileges(conn)
    except Exception as exc:  # noqa: BLE001
        title, detail, fix = explain_connection_error(exc)
        entry = {"id": "privileges", "ok": False, "title": title, "detail": detail, "fix": fix}
    if not record(entry, time.monotonic() - t0):
        return fail(STAGE_ORDER[3:])

    # 4. schema
    t0 = time.monotonic()
    try:
        entry = _stage_schema(conn)
    except Exception as exc:  # noqa: BLE001
        title, detail, fix = explain_connection_error(exc)
        entry = {"id": "schema", "ok": False, "title": title, "detail": detail, "fix": fix}
    if not record(entry, time.monotonic() - t0):
        return fail(STAGE_ORDER[4:])

    # 5. graph
    t0 = time.monotonic()
    try:
        entry = _stage_graph(conn)
    except Exception as exc:  # noqa: BLE001
        title, detail, fix = explain_connection_error(exc)
        entry = {"id": "graph", "ok": False, "title": title, "detail": detail, "fix": fix}
    if not record(entry, time.monotonic() - t0):
        return fail(STAGE_ORDER[5:])

    # 6. store
    if store:
        t0 = time.monotonic()
        entry = _stage_store(dsn, root_crt)
        if not record(entry, time.monotonic() - t0):
            return fail(STAGE_ORDER[6:])
    else:
        skip_rest(("store",), "store=False (preflight — nothing written)")

    # 7. upkeep
    if install_jobs:
        t0 = time.monotonic()
        entry = _stage_upkeep()
        if not record(entry, time.monotonic() - t0):
            return fail(STAGE_ORDER[7:])
    else:
        skip_rest(("upkeep",), "install_jobs=False")

    # 8. prove
    if prove:
        t0 = time.monotonic()
        entry = _stage_prove()
        if not record(entry, time.monotonic() - t0):
            return fail(STAGE_ORDER[8:])
    else:
        skip_rest(("prove",), "prove=False")

    # 9. summary
    t0 = time.monotonic()
    entry, summary = _stage_summary(conn, dsn)
    record(entry, time.monotonic() - t0)
    out["summary"] = summary
    conn.close()
    return out


def provision_local_move_target() -> dict[str, Any]:
    """Create a fresh local-Docker Postgres to hand to :func:`khipu.dbmove.
    move_database` as a target — the "a new database on this Mac" choice in
    Settings › Database › Move (``khipu db move --new-local``).

    ``components_postgres.install_local_postgres`` stores the DSN it creates
    into Keychain itself (the same call the Welcome "set up a new database on
    this Mac" step uses). Called as-is here, that would switch Khipu's active
    connection to the new, empty database *before* anything has been copied
    into it — and then ``move_database``'s own ``current_dsn = resolve_dsn()``
    would read that new database as the source, refuse with
    ``target_is_current``, and the move would never happen.

    So: read the current DSN first, let ``install_local_postgres`` do its
    thing, capture the DSN it just stored, then put the original DSN back —
    all before ``move_database`` ever looks at ``resolve_dsn()``. The
    generated local password never leaves this process: the caller
    (``khipu db move --new-local``) uses the returned DSN to call
    ``move_database`` directly, in the same process, and never prints it.
    """
    from khipu.components_postgres import install_local_postgres
    from khipu.db import resolve_dsn
    from khipu.keychain import set_dsn

    try:
        original_dsn: str | None = resolve_dsn()
    except Exception:  # noqa: BLE001 — no prior DSN configured is fine here
        original_dsn = None

    out = install_local_postgres()
    if not out.get("ok"):
        return out

    try:
        new_dsn = resolve_dsn()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"could_not_read_new_local_dsn: {exc}"}

    if original_dsn:
        try:
            set_dsn(original_dsn)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": "could_not_restore_source_dsn",
                "detail": f"{type(exc).__name__}: {exc}",
                "fix": "The new local database was created, but Khipu could "
                "not put the original connection back to run the copy. "
                "Check Keychain access and try again.",
            }

    return {"ok": True, "dsn": new_dsn}
