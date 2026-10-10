"""Shared live-test safety helpers."""
import os
import re
from pathlib import Path


def validate_scratch_dsn(dsn):
    from psycopg.conninfo import conninfo_to_dict

    if os.environ.get("KHIPU_LIVE_TESTS") != "1":
        raise ValueError("scratch tests require KHIPU_LIVE_TESTS=1")
    nonce = os.environ.get("KHIPU_SCRATCH_NONCE", "")
    info = conninfo_to_dict(dsn)
    if not re.fullmatch(r"[0-9a-f]{32}", nonce) or info.get("dbname") != "khipu_scratch_" + nonce:
        raise ValueError("scratch database must have a nonce-scoped test identity")
    host = info.get("host", "")
    socket = Path(host)
    local_socket = socket.is_absolute() and any(
        socket.resolve().is_relative_to(root) for root in (Path("/tmp").resolve(), Path("/private/tmp"))
    )
    if host not in ("127.0.0.1", "::1") and not local_socket:
        raise ValueError("scratch database must use loopback or a temporary local socket")
    if info.get("hostaddr") and info["hostaddr"] not in ("127.0.0.1", "::1"):
        raise ValueError("scratch hostaddr must be loopback")
    if info.get("service"):
        raise ValueError("scratch database must not use a service override")
    if any(os.environ.get(key) for key in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE")):
        raise ValueError("scratch database must not inherit libpq address/service overrides")
    return nonce


def connect_scratch(dsn):
    """Only a separately provisioned database with an ownership sentinel may receive DDL."""
    import psycopg

    nonce = validate_scratch_dsn(dsn)
    conn = psycopg.connect(dsn, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), nonce FROM public.khipu_test_owner")
            if cur.fetchall() != [("khipu_scratch_" + nonce, nonce)]:
                raise ValueError("scratch database ownership sentinel does not match")
        conn.rollback()
    except Exception:
        conn.close()
        raise
    return conn
